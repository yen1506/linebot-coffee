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

# 預期欄位順序（主表）
EXPECTED_HEADERS = [
    "訂單編號", "姓名", "電話", "咖啡品名", "付款方式",
    "樣式", "數量", "送達地址", "備註", "狀態",
    "下單時間", "顧客編號"
]

# 已取消訂單的預期欄位（多一個刪單時間）
BACKUP_HEADERS = [
    "訂單編號", "姓名", "電話", "咖啡品名", "付款方式",
    "樣式", "數量", "送達地址", "備註", "狀態",
    "下單時間", "顧客編號", "刪單時間"
]

def get_or_create_ws(title, rows=1000, cols=20):
    try:
        ws = client.open(SPREADSHEET_NAME).worksheet(title)
    except Exception:
        ws = client.open(SPREADSHEET_NAME).add_worksheet(title=title, rows=str(rows), cols=str(cols))
    headers_to_check = EXPECTED_HEADERS if title == "訂單清單" else BACKUP_HEADERS
    values = ws.get_all_values()
    if not values or values[0] != headers_to_check:
        try:
            ws.clear()
        except Exception:
            pass
        ws.update([headers_to_check])
    return ws

sheet = get_or_create_ws("訂單清單")
backup_sheet = get_or_create_ws("已取消訂單")

# ---------- 使用者狀態 ----------
user_states = {}

# ---------- 輔助：把欄位 key 正規化 ----------
def normalize_key(k: str) -> str:
    # 去掉方括號、左右空白，接受全形或半形冒號的前段
    if not k:
        return k
    k = k.strip()
    k = re.sub(r'^[\[\【\[]+|[\]\】\]]+$', '', k)  # remove surrounding brackets if any
    k = k.strip()
    return k

# ---------- 訂單解析（接受「欄位：值」多行格式） ----------
def parse_order_fields(text):
    """
    解析使用者送來的多行格式，格式例如：
    姓名：王小明
    電話：0900123456
    咖啡品名：耶加雪菲
    樣式：掛耳包
    數量：2
    送達地址：台北市...
    備註：xxx  (備註可留空)
    支援全形/半形冒號（：或:），key 兩側可包含方括號【】或[]。
    """
    data_dict = {}
    for part in text.strip().splitlines():
        if not part or part.strip() == "":
            continue
        # 支援全形與半形冒號
        if "：" in part:
            raw_key, raw_val = part.split("：", 1)
        elif ":" in part:
            raw_key, raw_val = part.split(":", 1)
        else:
            # 無冒號視為不合法該行，跳過
            continue
        key = normalize_key(raw_key)
        val = raw_val.strip()
        data_dict[key] = val

    required_fields = ["姓名", "電話", "咖啡品名", "樣式", "數量", "送達地址"]
    if not all(field in data_dict for field in required_fields):
        return None

    phone = data_dict.get("電話", "")
    qty = data_dict.get("數量", "")

    # 電話格式驗證 (台灣手機 09xxxxxxxx)
    if not re.match(r'^09\d{8}$', phone):
        return None
    if not str(qty).isdigit():
        return None

    return {
        "name": data_dict.get("姓名", ""),
        "phone": phone,
        "coffee": data_dict.get("咖啡品名", ""),
        "style": data_dict.get("樣式", ""),
        "qty": int(qty),
        "address": data_dict.get("送達地址", ""),
        "remark": data_dict.get("備註", "") or ""
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
            user_states.pop(user_id, None)
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
            "咖啡品名": temp["coffee"],
            "付款方式": payment_method,
            "樣式": temp["style"],
            "數量": str(temp["qty"]),
            "送達地址": temp["address"],
            "備註": temp["remark"],
            "狀態": "處理中",
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
            f"【咖啡品名】：{temp['coffee']}\n"
            f"【樣式】：{temp['style']}\n"
            f"【數量】：{temp['qty']}\n"
            f"【送達地址】：{temp['address']}\n"
            f"【備註】：{temp['remark'] if temp['remark'] else '無'}\n"
            f"【付款方式】：{payment_method}\n"
            f"【狀態】：處理中"
        )

        reply_messages = [TextSendMessage(text="✅ 訂單已成立！\n以下是您的訂單資訊：\n---\n" + data_display + "\n---\n訂單將於3日內出貨，再麻煩您留意到貨通知。\n感謝您的訂購!")]
        if payment_method == "匯款":
            bank_info = ("💳 匯款資訊：\n銀行：示範銀行\n帳號：1234567890123\n戶名：示範戶名")
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
            user_states.pop(user_id, None)
            return

        headers = records[0]
        found = False
        for idx in range(1, len(records)):
            row = records[idx]
            if len(row) > 0 and query == row[0]:
                # 檢查顧客身分
                try:
                    cust_idx = headers.index("顧客編號")
                    if len(row) <= cust_idx or user_id != row[cust_idx]:
                        continue
                except ValueError:
                    # 若 header 沒有顧客編號，以保守方式拒絕
                    continue

                try:
                    # 準備備份資料，並新增刪單時間
                    delete_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
                    row_dict = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers))}
                    row_dict["刪單時間"] = delete_time
                    # 以 BACKUP_HEADERS 順序建立備份列
                    target_row = [row_dict.get(h, "") for h in BACKUP_HEADERS]
                    backup_sheet.append_row(target_row)

                    sheet.delete_rows(idx + 1)
                    found = True

                    visible_fields = [f"【{h}】：{v}" for h, v in zip(headers, row) if h != "顧客編號" and v]
                    reply_text = "✅ 已為您刪除以下訂單：\n---\n" + "\n".join(visible_fields)
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                except Exception as e:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 刪除訂單時發生錯誤，請稍後再試。錯誤：{e}"))
                break

        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無符合的訂單編號或您無權刪除此訂單。"))

        user_states.pop(user_id, None)
        return

    # ----- waiting_modify_id：輸入要修改的訂單編號 -----
    if state == "waiting_modify_id":
        query = msg
        records = sheet.get_all_values()
        if not records or len(records) < 2:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 尚無訂單資料。"))
            user_states.pop(user_id, None)
            return

        headers = records[0]
        found = False
        for idx in range(1, len(records)):
            row = records[idx]
            if len(row) > 0 and query == row[0]:
                # 檢查是否為該使用者的訂單
                try:
                    cust_idx = headers.index("顧客編號")
                    if len(row) <= cust_idx or user_id != row[cust_idx]:
                        continue
                except ValueError:
                    continue

                found = True

                # 檢查是否已修改過（你原先限制只能修改一次）
                try:
                    t_idx = headers.index("下單時間")
                    if len(row) > t_idx and row[t_idx] and "已修改" in str(row[t_idx]):
                        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ 訂單 {query} 只能修改一次，無法再次修改。\n請刪除該訂單後再重新下單。"))
                        user_states.pop(user_id, None)
                        return
                except ValueError:
                    pass

                # 儲存暫存資訊（儲存 row index 為 Google Sheets 的 1-based row）
                user_states[user_id] = "modifying"
                user_states[f"{user_id}_temp_modify"] = {"row_index": idx + 1, "order_id": query, "original_data": row}

                # 準備要讓使用者複製修改的區塊（以欄位：值 的格式列出）
                data_for_copy = []
                for h_i, h in enumerate(headers):
                    # 只取我們關心的欄位供複製，跳過不可修改的欄位（訂單編號/顧客編號/下單時間）
                    if h in ("訂單編號", "顧客編號", "下單時間"):
                        continue
                    v = row[h_i] if h_i < len(row) else ""
                    data_for_copy.append(f"{h}：{v}")
                data_for_copy_text = "\n".join(data_for_copy)

                instruction_text = (
                    f"📝 訂單編號： {query}，請複製下方原訂單資料後直接修改並回傳。\n\n"
                    "註：\n【咖啡品名】請於基本檔案頁面先確認現有販售品項\n【樣式】掛耳包/豆子 擇一填寫\n【送達地址】宅配地址/花蓮吉安地區可面交\n【備註】 選填"
                )
                line_bot_api.reply_message(event.reply_token, [
                    TextSendMessage(text=instruction_text),
                    TextSendMessage(text=data_for_copy_text)
                ])
                break

        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無符合的訂單編號或您無權修改此訂單。"))
            user_states.pop(user_id, None)
        return

    # ----- querying_order_id: 查詢訂單（只回傳該用戶自己的訂單） -----
    if state == "querying_order_id":
        query = msg
        records = sheet.get_all_values()
        if not records or len(records) < 2:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 尚無訂單資料。"))
            user_states.pop(user_id, None)
            return

        headers = records[0]
        found = False
        for row in records[1:]:
            if len(row) > 0 and query == row[0]:
                try:
                    cust_idx = headers.index("顧客編號")
                    if len(row) <= cust_idx or user_id != row[cust_idx]:
                        continue
                except ValueError:
                    continue
                found = True
                order_info = (
                    f"📜 您的訂單詳情：\n---\n"
                    f"【訂單編號】：{row[headers.index('訂單編號')]}\n"
                    f"【姓名】：{row[headers.index('姓名')]}\n"
                    f"【咖啡品名】：{row[headers.index('咖啡品名')]}\n"
                    f"【樣式】：{row[headers.index('樣式')]}\n"
                    f"【數量】：{row[headers.index('數量')]}\n"
                    f"【送達地址】：{row[headers.index('送達地址')]}\n"
                    f"【狀態】：{row[headers.index('狀態')]}\n"
                    f"【下單時間】：{row[headers.index('下單時間')]}"
                )
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=order_info))
                break

        if not found:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無此訂單編號或您無權查看此訂單。"))
        user_states.pop(user_id, None)
        return

    # ----- modifying：處理使用者回傳的修改後資料（欄位：值 多行） -----
    if state == "modifying":
        temp_modify = user_states.get(f"{user_id}_temp_modify")
        if not temp_modify:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 訂單資料遺失，請重新操作。"))
            user_states.pop(user_id, None)
            user_states.pop(f"{user_id}_temp_modify", None)
            return

        new_data = parse_order_fields(msg)
        if not new_data:
            instruction_text = (
                "⚠️ 輸入格式錯誤，請重新參照各欄位說明並複製以下欄位進行修改：\n\n"
                "例：\n姓名：王大明\n電話：0900123456\n咖啡品名：耶加雪菲\n樣式：掛耳包\n數量：2\n送達地址：台北市大安區羅斯福路四段1號\n備註：酸感多一點\n"
                "註：\n【咖啡品名】請於基本檔案頁面先確認現有販售品項\n【樣式】掛耳包/豆子 擇一填寫\n【送達地址】宅配地址/花蓮吉安地區可面交\n【備註】 選填"
            )
            fields_text = (
                "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n送達地址：\n備註："
            )
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text="❌ 格式錯誤！"),
                TextSendMessage(text=instruction_text),
                TextSendMessage(text=fields_text)
            ])
            return

        headers = sheet.get_all_values()[0]
        order_id = temp_modify['order_id']
        original_data = temp_modify['original_data']
        # 標示已修改並保留時間
        updated_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M') + " (已修改)"

        # 若付款方式不在新的輸入中，保留原付款方式
        try:
            payment = original_data[headers.index("付款方式")] if "付款方式" in headers and headers.index("付款方式") < len(original_data) else ""
        except Exception:
            payment = ""

        new_row_dict = {
            "訂單編號": order_id,
            "姓名": new_data["name"],
            "電話": new_data["phone"],
            "咖啡品名": new_data["coffee"],
            "付款方式": payment,
            "樣式": new_data["style"],
            "數量": str(new_data["qty"]),
            "送達地址": new_data["address"],
            "備註": new_data["remark"],
            "狀態": original_data[headers.index("狀態")] if "狀態" in headers and headers.index("狀態") < len(original_data) else "",
            "下單時間": updated_time,
            "顧客編號": user_id
        }

        updated_row = [new_row_dict.get(h, "") for h in EXPECTED_HEADERS]

        try:
            # 使用 A{row} 起始列寫入整列
            sheet.update(f"A{temp_modify['row_index']}", [updated_row])
            data_display = (
                f"【訂單編號】：{order_id}\n"
                f"【姓名】：{new_data['name']}\n"
                f"【電話】：{new_data['phone']}\n"
                f"【咖啡品名】：{new_data['coffee']}\n"
                f"【樣式】：{new_data['style']}\n"
                f"【數量】：{new_data['qty']}\n"
                f"【送達地址】：{new_data['address']}\n"
                f"【備註】：{new_data['remark'] if new_data['remark'] else '無'}\n"
                f"【狀態】：{new_row_dict['狀態']}\n"
                f"【下單時間】：{updated_time}"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="✅ 訂單已成功修改！\n以下是修改後的訂單資訊：\n---\n" + data_display + "\n---\n訂單將於3日內出貨，再麻煩您留意到貨通知。\n感謝您的訂購!"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 修改訂單時發生錯誤，請稍後再試。錯誤：{e}"))

        user_states.pop(user_id, None)
        user_states.pop(f"{user_id}_temp_modify", None)
        return

    # ----- 主指令處理區 -----
    if msg == "下單":
        user_states[user_id] = "ordering"
        instruction_text = (
            "請參照各欄位說明並複製以下欄位進行下單流程（回傳欄位：值 多行）：\n\n"
            "例：\n姓名：王大明\n電話：0900123456\n咖啡品名：耶加雪菲\n樣式：掛耳包\n數量：2\n送達地址：台北市大安區羅斯福路四段1號\n備註：酸感多一點\n\n"
            "註：【咖啡品名】請於基本檔案頁面先確認現有販售品項\n【樣式】掛耳包/豆子 擇一填寫\n【送達地址】宅配地址/花蓮吉安地區可面交\n【備註】選填"
        )
        fields_text = (
            "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n送達地址：\n備註："
        )
        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(text=instruction_text),
            TextSendMessage(text=fields_text)
        ])
        return

    if msg == "查詢訂單":
        user_states[user_id] = "querying_order_id"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入您的『訂單編號』以查詢訂單："))
        return

    if msg == "刪除訂單":
        user_states[user_id] = "waiting_delete_id"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入您的『訂單編號』以刪除訂單："))
        return

    if msg == "修改訂單":
        user_states[user_id] = "waiting_modify_id"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請輸入您的『訂單編號』以修改訂單："))
        return

    # ----- ordering：收到用戶以「欄位：值」格式回傳下單內容 -----
    if state == "ordering":
        data = parse_order_fields(msg)
        if not data:
            instruction_text = (
                "⚠️ 輸入格式錯誤，請重新參照各欄位說明並複製以下欄位進行下單流程：\n\n"
                "例：\n姓名：王大明\n電話：0900123456\n咖啡品名：耶加雪菲\n樣式：掛耳包\n數量：2\n送達地址：台北市大安區羅斯福路四段1號\n備註：酸感多一點\n\n"
                "註：【咖啡品名】請於基本檔案頁面先確認現有販售品項\n【樣式】掛耳包/豆子 擇一填寫\n【送達地址】宅配地址/花蓮吉安地區可面交\n【備註】選填"
            )
            fields_text = (
                "姓名：\n電話：\n咖啡品名：\n樣式：\n數量：\n送達地址：\n備註："
            )
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text="❌ 格式錯誤！"),
                TextSendMessage(text=instruction_text),
                TextSendMessage(text=fields_text)
            ])
            return

        user_states[f"{user_id}_temp_order"] = data
        user_states[user_id] = "waiting_payment"

        data_block = (
            f"【姓名】：{data['name']}\n"
            f"【電話】：{data['phone']}\n"
            f"【咖啡品名】：{data['coffee']}\n"
            f"【樣式】：{data['style']}\n"
            f"【數量】：{data['qty']}\n"
            f"【送達地址】：{data['address']}\n"
            f"【備註】：{data['remark'] if data['remark'] else '無'}"
        )

        line_bot_api.reply_message(event.reply_token, [
            TextSendMessage(text="以下為您的訂單資料，請確認後選擇付款方式："),
            TextSendMessage(text=data_block),
            TextSendMessage(text="請問付款方式為『匯款』或『付現』？")
        ])
        return

    # ----- 其他（預設） -----
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="👋 您好，請輸入『下單』開始新訂單，或輸入『查詢訂單』、『刪除訂單』或『修改訂單』來處理現有訂單。"))
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
        order_df = order_df[order_df["咖啡品名"].notna()]
        price_df = price_df[price_df["咖啡品名"].notna()]
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors='coerce')
        merged_df = order_df.merge(price_df, how="left", on=["咖啡品名", "樣式"], suffixes=('', '_價格'))
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

        # 使用 pd.to_datetime 更健壯地解析「下單時間」，允許含 (已修改) 的字串
        raw_times = order_df.get("下單時間", "").astype(str).str.replace(r'\s*\(已修改\)\s*', '', regex=True)
        order_df["月份"] = pd.to_datetime(raw_times, errors="coerce").dt.to_period("M").astype(str)

        summary_df = order_df.groupby(["月份", "咖啡品名", "樣式", "單價"], as_index=False).agg({
            "數量": "sum",
            "總金額": "sum"
        })
        summary_df = summary_df[["月份", "咖啡品名", "樣式", "單價", "數量", "總金額"]]
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
        customer_df = order_df.groupby(["姓名", "咖啡品名", "樣式"], as_index=False).agg({
            "數量": "count",
            "總金額": "sum"
        })
        customer_df.rename(columns={"數量": "購買次數"}, inplace=True)
        customer_df = customer_df[["姓名", "咖啡品名", "樣式", "購買次數", "總金額"]]
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
scheduler.add_job(update_prices_and_totals, 'interval', minutes=30)
scheduler.add_job(generate_monthly_summary, 'interval', hours=12)
scheduler.add_job(generate_customer_summary, 'interval', hours=24)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))


