from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import re
import uuid
import pandas as pd
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ---------- LINE 設定 ----------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ---------- Google Sheets 初始化 ----------
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/coffee-bot-468008-86e28eaa87f3.json", scope)
client = gspread.authorize(creds)

# 試算表名稱（請與你的 Google Sheets 名稱一致）
SPREADSHEET_NAME = "coffee_orders"

# 預期欄位順序（移除預計取貨日期）
EXPECTED_HEADERS = [
    "訂單編號", "姓名", "電話", "咖啡名稱", "付款方式",
    "樣式", "數量", "送達地址", "備註",
    "下單時間", "顧客編號"
]

# 取得 worksheet（若不存在會建立），並確保標題列
def get_or_create_ws(title, rows=1000, cols=20):
    try:
        ws = client.open(SPREADSHEET_NAME).worksheet(title)
    except Exception:
        ws = client.open(SPREADSHEET_NAME).add_worksheet(title=title, rows=str(rows), cols=str(cols))
    values = ws.get_all_values()
    if not values or values[0] != EXPECTED_HEADERS:
        try:
            ws.clear()
        except Exception:
            pass
        ws.update([EXPECTED_HEADERS])
    return ws

# 主訂單表與已取消表
sheet = get_or_create_ws("訂單清單")
backup_sheet = get_or_create_ws("已取消訂單")

# ---------- 使用者狀態 ----------
user_states = {}

# ---------- 訂單解析（新增電話欄位，備註可留空） ----------
def parse_order_fields(text):
    parts = [p.strip() for p in text.strip().split('\n')]
    # 檢查是否為「欄位名稱：欄位值」的格式
    
    data_dict = {}
    for part in parts:
        if "：" in part:
            key, value = part.split("：", 1)
            data_dict[key.strip()] = value.strip()
    
    # 檢查所有預期欄位是否存在
    required_fields = ["姓名", "電話", "咖啡品名", "樣式", "數量", "送達地址"]
    if not all(field in data_dict for field in required_fields):
        return None
    
    # 驗證電話和數量
    if not re.match(r'^09\d{8}$', data_dict.get("電話", "")) or not data_dict.get("數量", "").isdigit():
        return None
        
    return {
        "name": data_dict.get("姓名", ""),
        "phone": data_dict.get("電話", ""),
        "coffee": data_dict.get("咖啡品名", ""),
        "style": data_dict.get("樣式", ""),
        "qty": int(data_dict.get("數量", "0")),
        "method": data_dict.get("送達地址", ""),
        "remark": data_dict.get("備註", "") if data_dict.get("備註") else ""
    }

# ---------- Flask / LINE webhook ----------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    msg = event.message.text.strip()
    state = user_states.get(user_id, "init")

    # ----- waiting_payment：處理使用者輸入付款方式 -----
    if state == "waiting_payment":
        temp = user_states.get(f"{user_id}_temp_order")
        if not temp:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 訂單資料遺失，請重新下單。"))
            user_states[user_id] = "init"
            user_states.pop(f"{user_id}_temp_order", None)
            return

        pm = msg.replace(" ", "")
        if "匯款" in pm:
            payment_method = "匯款"
        elif "付現" in pm or "現付" in pm:
            payment_method = "付現"
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 付款方式請輸入『匯款』或『付現』，請重新輸入。"))
            return

        order_id = str(uuid.uuid4())[:8]
        order_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
        row_dict = {
            "訂單編號": order_id,
            "姓名": temp["name"],
            "電話": temp["phone"],
            "咖啡名稱": temp["coffee"],
            "付款方式": payment_method,
            "樣式": temp["style"],
            "數量": str(temp["qty"]),
            "送達地址": temp["method"],
            "備註": temp["remark"],
            "下單時間": order_time,
            "顧客編號": user_id
        }

        try:
            row = [row_dict.get(h, "") for h in EXPECTED_HEADERS]
            sheet.append_row(row)
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 寫入訂單時發生錯誤，請稍後再試。錯誤：{e}"))
            return

        data_display = (
            f"【訂單編號】：{order_id}\n"
            f"【姓名】：{temp['name']}\n"
            f"【電話】：{temp['phone']}\n"
            f"【咖啡名稱】：{temp['coffee']}\n"
            f"【樣式】：{temp['style']}\n"
            f"【數量】：{temp['qty']}\n"
            f"【送達地址】：{temp['method']}\n"
            f"【備註】：{temp['remark'] if temp['remark'] else '無'}\n"
            f"【付款方式】：{payment_method}"
        )

        reply_messages = [TextSendMessage(text="✅ 訂單已成立！\n以下是您的訂單資訊：\n---\n" + data_display)]
        if payment_method == "匯款":
            bank_info = ("💳 匯款資訊：\n"
                         "銀行：示範銀行\n"
                         "分行：示範分行\n"
                         "帳號：1234567890123\n"
                         "戶名：示範戶名")
            reply_messages.append(TextSendMessage(text=bank_info))

        line_bot_api.reply_message(event.reply_token, reply_messages)
        user_states.pop(user_id, None)
        user_states.pop(f"{user_id}_temp_order", None)
        return

    # ----- waiting_delete_id：處理使用者輸入刪除訂單編號 -----
    if state == "waiting_delete_id":
        query = msg
        records = sheet.get_all_values()
        if not records or len(records) < 2:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 尚無訂單資料。"))
            user_states[user_id] = "init"
            return
        
        headers = records[0]
        found = False
        for idx in range(1, len(records)):
            row = records[idx]
            if len(row) > 0 and query == row[0] and len(row) > headers.index("顧客編號") and user_id == row[headers.index("顧客編號")]:
                try:
                    target_row = [row[headers.index(h)] if h in headers else "" for h in EXPECTED_HEADERS]
                    backup_sheet.append_row(target_row)
                    sheet.delete_rows(idx + 1)
                    found = True

                    visible_fields = [f"【{h}】：{v}" for h, v in zip(headers, row) if h not in ["顧客編號"] and v]
                    reply_text = "✅ 已為您刪除以下訂單：\n---\n" + "\n".join(visible_fields)
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                except Exception as e:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 刪除訂單時發生錯誤，請稍後再試。錯誤：{e}"))
                break

        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無符合的訂單編號或您無權刪除此訂單。"))
        
        user_states.pop(user_id, None)
        return
    
    # ----- waiting_modify_id：處理使用者輸入修改訂單編號 -----
    if state == "waiting_modify_id":
        query = msg
        records = sheet.get_all_values()
        if not records or len(records) < 2:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 尚無訂單資料。"))
            user_states[user_id] = "init"
            return
        
        headers = records[0]
        found = False
        for idx in range(1, len(records)):
            row = records[idx]
            if len(row) > 0 and query == row[0] and len(row) > headers.index("顧客編號") and user_id == row[headers.index("顧客編號")]:
                found = True
                user_states[user_id] = "modifying"
                user_states[f"{user_id}_temp_modify"] = {"row_index": idx + 1, "order_id": query, "original_data": row}
                
                # 獨立訊息區塊，方便使用者複製
                data_for_copy = (
                    f"姓名：{row[headers.index('姓名')]}\n"
                    f"電話：{row[headers.index('電話')]}\n"
                    f"咖啡品名：{row[headers.index('咖啡名稱')]}\n"
                    f"樣式：{row[headers.index('樣式')]}\n"
                    f"數量：{row[headers.index('數量')]}\n"
                    f"送達地址：{row[headers.index('送達地址')]}\n"
                    f"備註：{row[headers.index('備註')]}\n"
                )
                
                instruction_text = (
                    f"📝訂單編號： {query}！請複製下方原訂單資料後進行修改並回傳：\n\n" 
                    "註：\n咖啡品名【請於基本檔案頁面先確認現有販售品項】\n樣式【掛耳包/豆子 擇一填寫】\n送達地址【宅配地址/花蓮吉安地區可面交】\n備註【選填】\n"
                )
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=instruction_text),
                    TextSendMessage(text=data_for_copy)
                ])
                break
        
        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無符合的訂單編號或您無權修改此訂單。"))
            user_states.pop(user_id, None)
        return

    # ----- modifying：處理修改後的資料 -----
    if state == "modifying":
        temp_modify = user_states.get(f"{user_id}_temp_modify")
        if not temp_modify:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 訂單資料遺失，請重新操作。"))
            user_states.pop(user_id, None)
            return

        new_data = parse_order_fields(msg)
        if not new_data:
            # 將指示說明與錯誤格式的資料分開
            instruction_text = (
                "⚠️ 輸入格式錯誤，請重新參照各欄位說明並複製以下欄位進行下單流程：\n\n"
                "例：\n姓名：王大明\n電話：0900123456\n咖啡品名：耶加雪菲\n樣式：掛耳包\n數量：2\n送達地址：台北市大安區羅斯福路1號\n備註：酸感多一點\n\n"
                "註：\n咖啡品名【請於基本檔案頁面先確認現有販售品項】\n樣式【掛耳包/豆子 擇一填寫】\n送達地址【宅配地址/花蓮吉安地區可面交】\n備註【選填】\n"
            )
            fields_text = (
                "姓名：\n"
                "電話：\n"
                "咖啡品名：\n"
                "樣式：\n"
                "數量：\n"
                "送達地址：\n"
                "備註："
            )
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text="❌ 格式錯誤！"),
                TextSendMessage(text=instruction_text),
                TextSendMessage(text=fields_text)
            ])
            return
        
        # 準備更新的資料列
        headers = sheet.get_all_values()[0]
        order_id = temp_modify['order_id']
        original_data = temp_modify['original_data']
        
        new_row_dict = {
            "訂單編號": order_id,
            "姓名": new_data["name"],
            "電話": new_data["phone"],
            "咖啡名稱": new_data["coffee"],
            "付款方式": original_data[headers.index("付款方式")],
            "樣式": new_data["style"],
            "數量": str(new_data["qty"]),
            "送達地址": new_data["method"],
            "備註": new_data["remark"],
            "下單時間": original_data[headers.index("下單時間")],
            "顧客編號": user_id
        }
        
        updated_row = [new_row_dict.get(h, "") for h in EXPECTED_HEADERS]

        try:
            sheet.update(f"A{temp_modify['row_index']}", [updated_row])
            
            data_display = (
                f"【訂單編號】：{order_id}\n"
                f"【姓名】：{new_data['name']}\n"
                f"【電話】：{new_data['phone']}\n"
                f"【咖啡名稱】：{new_data['coffee']}\n"
                f"【樣式】：{new_data['style']}\n"
                f"【數量】：{new_data['qty']}\n"
                f"【送達地址】：{new_data['method']}\n"
                f"【備註】：{new_data['remark'] if new_data['remark'] else '無'}"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 訂單已成功修改！\n以下是修改後的訂單資訊：\n---\n" + data_display))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 修改訂單時發生錯誤，請稍後再試。錯誤：{e}"))
        
        user_states.pop(user_id, None)
        user_states.pop(f"{user_id}_temp_modify", None)
        return

    # ----- 主指令處理區 -----
    if msg == "下單":
        user_states[user_id] = "ordering"
        instruction_text = (
            "請參照各欄位說明並複製以下欄位進行下單流程：\n\n"
            "例：\n姓名：王大明\n電話：0900123456\n咖啡品名：耶加雪菲\n樣式：掛耳包\n數量：2\n送達地址：台北市大安區羅斯福路1號\n備註：酸感多一點\n\n"
            "註：\n咖啡品名【請於基本檔案頁面先確認現有販售品項】\n樣式【掛耳包/豆子 擇一填寫】\n送達地址【宅配地址/花蓮吉安地區可面交】\n備註【選填】\n"
        )
        fields_text = (
            "姓名：\n"
            "電話：\n"
            "咖啡品名：\n"
            "樣式：\n"
            "數量：\n"
            "送達地址：\n"
            "備註："
        )
        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(text=instruction_text),
            TextSendMessage(text=fields_text)
        ])
        return

    if msg == "刪除訂單":
        user_states[user_id] = "waiting_delete_id"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入您的『訂單編號』以刪除訂單："))
        return
    
    if msg == "修改訂單":
        user_states[user_id] = "waiting_modify_id"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入您的『訂單編號』以修改訂單："))
        return

    # ----- ordering：收到 7 行下單內容 -----
    if state == "ordering":
        data = parse_order_fields(msg)
        if not data:
            instruction_text = (
                "⚠️ 輸入格式錯誤，請重新參照各欄位說明並複製以下欄位進行下單流程：\n\n"
                "例：\n姓名：王大明\n電話：0900123456\n咖啡品名：耶加雪菲\n樣式：掛耳包\n數量：2\n送達地址：台北市大安區羅斯福路1號\n備註：酸感多一點\n\n"
                "註：\n咖啡品名【請於基本檔案頁面先確認現有販售品項】\n樣式【掛耳包/豆子 擇一填寫】\n送達地址【宅配地址/花蓮吉安地區可面交】\n備註【選填】\n"
            )
            fields_text = (
                "姓名：\n"
                "電話：\n"
                "咖啡品名：\n"
                "樣式：\n"
                "數量：\n"
                "送達地址：\n"
                "備註："
            )
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text="❌ 格式錯誤！"),
                TextSendMessage(text=instruction_text),
                TextSendMessage(text=fields_text)
            ])
            return

        user_states[f"{user_id}_temp_order"] = data
        user_states[user_id] = "waiting_payment"
        
        # 將下單資料以【欄位名稱】：【使用者填入資料】形式呈現
        data_block = (
            f"【姓名】：{data['name']}\n"
            f"【電話】：{data['phone']}\n"
            f"【咖啡品名】：{data['coffee']}\n"
            f"【樣式】：{data['style']}\n"
            f"【數量】：{data['qty']}\n"
            f"【送達地址】：{data['method']}\n"
            f"【備註】：{data['remark'] if data['remark'] else '無'}"
        )
        
        # 分兩個區塊傳送
        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(text="以下為您的訂單資料，請確認後選擇付款方式："),
            TextSendMessage(text=data_block),
            TextSendMessage(text="請問付款方式為『匯款』或『付現』？")
        ])
        return

    # ----- 其他（預設） -----
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="👋 您好，請輸入『下單』開始新訂單，或輸入『刪除訂單』或『修改訂單』來處理現有訂單。"))
    user_states[user_id] = "init"
    return

# ---------- 定時任務（提醒 / 更新 / 統計） ----------
def update_prices_and_totals():
    try:
        order_ws = client.open(SPREADSHEET_NAME).worksheet("訂單清單")
        price_ws = client.open(SPREADSHEET_NAME).worksheet("價格表")
        order_data = order_ws.get_all_values()
        price_data = price_ws.get_all_values()
        if len(order_data) < 2 or len(price_data) < 2:
            return
        order_df = pd.DataFrame(order_data[1:], columns=order_data[0])
        price_df = pd.DataFrame(price_data[1:], columns=price_data[0])
        order_df = order_df[order_df["咖啡名稱"].notna()]
        price_df = price_df[price_df["咖啡名稱"].notna()]
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors='coerce')
        merged_df = order_df.merge(price_df, how="left", on=["咖啡名稱", "樣式"], suffixes=('', '_價格'))
        merged_df["單價"] = pd.to_numeric(merged_df.get("單價_價格", pd.Series()), errors='coerce')
        merged_df["總金額"] = merged_df["單價"] * merged_df["數量"]
        final_columns = order_data[0]
        for col in ["單價", "總金額"]:
            if col not in final_columns:
                final_columns.append(col)
        merged_df = merged_df.reindex(columns=final_columns)
        order_ws.update([final_columns] + merged_df.fillna("").astype(str).values.tolist())
    except Exception as e:
        print("更新價格時錯誤：", e)

def generate_monthly_summary():
    try:
        order_ws = client.open(SPREADSHEET_NAME).worksheet("訂單清單")
        order_data = order_ws.get_all_values()
        if len(order_data) < 2:
            return
        order_df = pd.DataFrame(order_data[1:], columns=order_data[0])
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors="coerce").fillna(0)
        order_df["單價"] = pd.to_numeric(order_df.get("單價", 0), errors="coerce").fillna(0)
        order_df["總金額"] = pd.to_numeric(order_df.get("總金額", 0), errors="coerce").fillna(0)
        
        order_df["月份"] = pd.to_datetime(order_df["下單時間"], errors="coerce").dt.to_period("M").astype(str)
        
        summary_df = order_df.groupby(["月份", "咖啡名稱", "樣式", "單價"], as_index=False).agg({
            "數量": "sum",
            "總金額": "sum"
        })
        summary_df = summary_df[["月份", "咖啡名稱", "樣式", "單價", "數量", "總金額"]]
        try:
            summary_ws = client.open(SPREADSHEET_NAME).worksheet("每月統計")
        except:
            summary_ws = client.open(SPREADSHEET_NAME).add_worksheet(title="每月統計", rows="1000", cols="10")
        summary_ws.clear()
        summary_ws.update([summary_df.columns.tolist()] + summary_df.astype(str).values.tolist())
    except Exception as e:
        print("無法產生每月統計：", e)

def generate_customer_summary():
    try:
        order_ws = client.open(SPREADSHEET_NAME).worksheet("訂單清單")
        order_data = order_ws.get_all_values()
        if len(order_data) < 2:
            return
        order_df = pd.DataFrame(order_data[1:], columns=order_data[0])
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors="coerce").fillna(0)
        order_df["總金額"] = pd.to_numeric(order_df.get("總金額", 0), errors="coerce").fillna(0)
        customer_df = order_df.groupby(["姓名", "咖啡名稱", "樣式"], as_index=False).agg({
            "數量": "count",
            "總金額": "sum"
        })
        customer_df.rename(columns={"數量": "購買次數"}, inplace=True)
        customer_df = customer_df[["姓名", "咖啡名稱", "樣式", "購買次數", "總金額"]]
        try:
            customer_ws = client.open(SPREADSHEET_NAME).worksheet("客群統計")
        except:
            customer_ws = client.open(SPREADSHEET_NAME).add_worksheet(title="客群統計", rows="1000", cols="10")
        customer_ws.clear()
        customer_ws.update([customer_df.columns.tolist()] + customer_df.astype(str).values.tolist())
    except Exception as e:
        print("無法產生客群統計：", e)

# ---------- 啟用 scheduler（示範排程） ----------
scheduler = BackgroundScheduler()
scheduler.add_job(update_prices_and_totals, 'interval', minutes=1)
scheduler.add_job(generate_monthly_summary, 'interval', minutes=1)
scheduler.add_job(generate_customer_summary, 'interval', minutes=1)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
