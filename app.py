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
# 修改這裡：確保 json 路徑正確或改用 gspread.service_account(...)
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/coffee-bot-468008-86e28eaa87f3.json", scope)
client = gspread.authorize(creds)

# 試算表名稱（請與你的 Google Sheets 名稱一致）
SPREADSHEET_NAME = "coffee_orders"

# 預期欄位順序（我們把付款方式放在咖啡名稱之後）
EXPECTED_HEADERS = [
    "訂單編號", "姓名", "咖啡名稱", "付款方式",
    "掛耳包/豆子", "數量", "預計取貨日期", "取貨方式",
    "備註", "下單時間", "顧客編號"
]

# 取得 worksheet（若不存在會建立），並確保標題列
def get_or_create_ws(title, rows=1000, cols=20):
    try:
        ws = client.open(SPREADSHEET_NAME).worksheet(title)
    except Exception:
        ws = client.open(SPREADSHEET_NAME).add_worksheet(title=title, rows=str(rows), cols=str(cols))
    # 若 header 不存在或不一致，寫入 EXPECTED_HEADERS
    values = ws.get_all_values()
    if not values or values[0] != EXPECTED_HEADERS:
        # 清空並寫入標題
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
# user_states 會存放簡單狀態機；暫存訂單請放在 user_states[f"{user_id}_temp_order"]
user_states = {}

# ---------- 訂單解析（先不含付款方式） ----------
# 輸入為 8 行：
# 姓名
# 電話
# 咖啡品名
# 樣式（掛耳包/豆子）
# 數量（阿拉伯數字）
# 取貨日期（任意格式，將原文存入）
# 取貨方式（面交 / 郵寄地址）
# 備註（不可為空）
def parse_order_fields(text):
    parts = [p.strip() for p in text.strip().split('\n')]
    if len(parts) != 8:
        return None
    name, phone, coffee, style, qty, date, method, remark = parts
    if not re.match(r'^09\d{8}$', phone):
        return None
    if not qty.isdigit():
        return None
    if not remark:
        return None
    return {
        "name": name,
        "phone": phone,
        "coffee": coffee,
        "style": style,
        "qty": int(qty),
        "date": date,  # 不驗證格式
        "method": method,
        "remark": remark
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
        # 先檢查暫存訂單是否存在
        temp = user_states.get(f"{user_id}_temp_order")
        if not temp:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 訂單資料遺失，請重新下單。"))
            user_states[user_id] = "init"
            user_states.pop(f"{user_id}_temp_order", None)
            return

        # 模糊比對付款方式
        pm = msg.replace(" ", "")
        if "匯款" in pm:
            payment_method = "匯款"
        elif "付現" in pm or "現付" in pm:
            payment_method = "付現"
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 付款方式請輸入『匯款』或『付現』，請重新輸入。"))
            return  # 不改變狀態，讓使用者再輸入一次

        # 準備寫入欄位（按照 EXPECTED_HEADERS 順序）
        order_id = str(uuid.uuid4())[:8]
        order_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
        row_dict = {
            "訂單編號": order_id,
            "姓名": temp["name"],
            "咖啡名稱": temp["coffee"],
            "付款方式": payment_method,
            "掛耳包/豆子": temp["style"],
            "數量": str(temp["qty"]),
            "預計取貨日期": temp["date"],
            "取貨方式": temp["method"],
            "備註": temp["remark"],
            "下單時間": order_time,
            "顧客編號": user_id
        }

        # 依 header 產生 row list，並確保長度
        headers = sheet.get_all_values()[0]
        row = [row_dict.get(h, "") for h in headers]
        # 寫入 Google Sheet
        try:
            sheet.append_row(row)
        except Exception as e:
            # 若寫入失敗，回覆並保留暫存讓使用者重試
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 寫入訂單時發生錯誤，請稍後再試。錯誤：{e}"))
            return

        # 根據付款方式回覆
        if payment_method == "付現":
            reply_text = f"✅ 訂單已完成：{temp['coffee']} - {temp['style']}x{temp['qty']}\n📌 訂單編號：{order_id}\n於取貨時交付，謝謝購買。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        else:
            # 匯款資訊（範例，請自行修改）
            bank_info = ("💳 匯款資訊：\n"
                        "銀行：示範銀行\n"
                        "分行：示範分行\n"
                        "帳號：1234567890123\n"
                        "戶名：示範戶名\n\n"
                        "感謝購買！")
            reply_messages = [
                TextSendMessage(text=f"✅ 訂單已完成：{temp['coffee']} - {temp['style']}x{temp['qty']}\n📌 訂單編號：{order_id}"),
                TextSendMessage(text=bank_info)
            ]
            line_bot_api.reply_message(event.reply_token, reply_messages)

        # 清除狀態與暫存
        user_states.pop(user_id, None)
        user_states.pop(f"{user_id}_temp_order", None)
        return

    # ----- 使用者開始下單 -----
    if msg == "下單":
        user_states[user_id] = "ordering"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=(
                "請依序輸入以下資料（換行填寫）：\n\n"
                "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n取貨日期：\n取貨方式：\n備註：\n\n"
                "註：\n咖啡品名【請先確認現有販售品項】\n樣式【掛耳包/豆子擇一填寫】\n數量【請填入阿拉伯數字】\n取貨日期【YYYYMMDD】\n取貨方式【宅配地址/花蓮吉安地區可面交】\n備註【若沒有則填無】"
            ))
        )
        return

    # ----- 編輯訂單（取消/轉移到已取消訂單） -----
    if msg == "編輯訂單":
        user_states[user_id] = "editing"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入您的『訂單編號』以查詢訂單："))
        return

    # ----- ordering：收到 8 行下單內容 -----
    if state == "ordering":
        data = parse_order_fields(msg)
        if not data:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=(
                    "⚠️ 輸入格式錯誤，請重新填入以下資料（換行填寫）：\n\n"
                     "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n取貨日期：\n取貨方式：\n備註：\n\n"
                "註：\n咖啡品名【請先確認現有販售品項】\n樣式【掛耳包/豆子擇一填寫】\n數量【請填入阿拉伯數字】\n取貨日期【YYYYMMDD】\n取貨方式【宅配地址/花蓮吉安地區可面交】\n備註【若沒有則填無】"
                ))
            )
            return

        # 暫存訂單（尚未有付款方式）
        # 我們存 dict 方便後續使用
        user_states[f"{user_id}_temp_order"] = data
        user_states[user_id] = "waiting_payment"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請問付款方式為『匯款』或『付現』？"))
        return

    # ----- editing：由上而下搜尋訂單編號 -----
    if state == "editing":
        query = msg
        records = sheet.get_all_values()
        if not records or len(records) < 1:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 尚無訂單資料。"))
            user_states[user_id] = "init"
            return
        headers = records[0]
        found = False

        # 由上而下搜尋（跳過標題列）
        for idx in range(1, len(records)):
            row = records[idx]
            if len(row) >= 1 and query == row[0]:
                # 備份該列到已取消訂單（保持欄位數）
                # 確保 backup_sheet header 與主表一致
                b_headers = backup_sheet.get_all_values()
                if not b_headers or b_headers[0] != EXPECTED_HEADERS:
                    backup_sheet.clear()
                    backup_sheet.update([EXPECTED_HEADERS])
                # 若該列長度不符 header 就補空
                target_row = row[:]
                if len(target_row) < len(EXPECTED_HEADERS):
                    target_row += [""] * (len(EXPECTED_HEADERS) - len(target_row))
                elif len(target_row) > len(EXPECTED_HEADERS):
                    target_row = target_row[:len(EXPECTED_HEADERS)]
                backup_sheet.append_row(target_row)
                # 刪除主表該列（index 是 1-based，header 為第1列）
                sheet.delete_rows(idx + 1)

                user_states[user_id] = "confirm_reorder"
                visible_fields = [f"{h}: {v}" for h, v in zip(headers, target_row) if h != "顧客編號" and v]
                reply_text = "✅ 已取消以下訂單：\n" + "\n".join(visible_fields) + "\n\n❓是否要重新下單？請輸入『是』或『否』"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                found = True
                break

        if not found:
            user_states[user_id] = "init"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無符合的訂單編號，請再確認。"))
        return

    # ----- confirm_reorder：取消後是否要重新下單 -----
    if state == "confirm_reorder":
        if msg == "是":
            user_states[user_id] = "ordering"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=(
                    "請再次輸入以下資料（換行填寫）：\n\n"
                     "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n取貨日期：\n取貨方式：\n備註：\n\n"
                "註：\n咖啡品名【請先確認現有販售品項】\n樣式【掛耳包/豆子擇一填寫】\n數量【請填入阿拉伯數字】\n取貨日期【YYYYMMDD】\n取貨方式【宅配地址/花蓮吉安地區可面交】\n備註【若沒有則填無】"
                ))
            )
        else:
            user_states[user_id] = "init"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="☕ 期待下次光臨！"))
        return

    # ----- confirm_continue：下單完成是否繼續（這裡用不到，因為我們在付款回覆後直接問） -----
    if state == "confirm_continue":
        if msg == "是":
            user_states[user_id] = "ordering"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=(
                    "請輸入資料（換行填寫）：\n\n"
                    "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n取貨日期：\n取貨方式：\n備註：\n\n"
                "註：\n咖啡品名【請先確認現有販售品項】\n樣式【掛耳包/豆子擇一填寫】\n數量【請填入阿拉伯數字】\n取貨日期【YYYYMMDD】\n備註【若沒有則填無】"
                ))
            )
        else:
            user_states[user_id] = "init"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="☕ 期待下次光臨！"))
        return

    # ----- 其他（預設） -----
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="👋 請輸入『下單』開始新訂單，或輸入『編輯訂單』來取消訂單"))
    user_states[user_id] = "init"
    return

# ---------- 定時任務（提醒 / 更新 / 統計） ----------
def daily_pickup_reminder():
    try:
        records = sheet.get_all_values()
        if not records or len(records) < 2:
            return
        today_str = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d')
        # 預計取貨日期欄位 index 根據 EXPECTED_HEADERS
        idx_pickup = EXPECTED_HEADERS.index("預計取貨日期")
        idx_name = EXPECTED_HEADERS.index("姓名")
        idx_coffee = EXPECTED_HEADERS.index("咖啡名稱")
        idx_qty = EXPECTED_HEADERS.index("數量")
        idx_userid = EXPECTED_HEADERS.index("顧客編號")
        for row in records[1:]:
            try:
                pickup_date = row[idx_pickup] if len(row) > idx_pickup else ""
                user_id = row[idx_userid] if len(row) > idx_userid else ""
                if pickup_date == today_str and user_id:
                    coffee = row[idx_coffee] if len(row) > idx_coffee else ""
                    qty = row[idx_qty] if len(row) > idx_qty else ""
                    msg = f"📦 溫馨提醒：您今天有咖啡訂單要取貨！（{coffee} x{qty}）"
                    line_bot_api.push_message(user_id, TextSendMessage(text=msg))
            except Exception:
                continue
    except Exception:
        return

def update_prices_and_totals():
    # 保持原先邏輯，但須確保 price worksheet 欄位名稱與你現有一致
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
        merged_df = order_df.merge(price_df, how="left", on=["咖啡名稱", "掛耳包/豆子"], suffixes=('', '_價格'))
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
        order_df["月份"] = pd.to_datetime(order_df["預計取貨日期"], errors="coerce").dt.to_period("M").astype(str)
        summary_df = order_df.groupby(["月份", "咖啡名稱", "掛耳包/豆子", "單價"], as_index=False).agg({
            "數量": "sum",
            "總金額": "sum"
        })
        summary_df = summary_df[["月份", "咖啡名稱", "掛耳包/豆子", "單價", "數量", "總金額"]]
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
        customer_df = order_df.groupby(["姓名", "咖啡名稱", "掛耳包/豆子"], as_index=False).agg({
            "數量": "count",
            "總金額": "sum"
        })
        customer_df.rename(columns={"數量": "購買次數"}, inplace=True)
        customer_df = customer_df[["姓名", "咖啡名稱", "掛耳包/豆子", "購買次數", "總金額"]]
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
# 每日提醒（每天觸發一次）
scheduler.add_job(daily_pickup_reminder, 'interval', days=1)
# 更新價格 / 總金額 (每 10 分鐘為例)
scheduler.add_job(update_prices_and_totals, 'interval', minutes=10)
# 每 12 小時產生統計
scheduler.add_job(generate_monthly_summary, 'interval', hours=12)
scheduler.add_job(generate_customer_summary, 'interval', hours=12)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
