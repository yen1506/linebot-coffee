from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import re
import uuid
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# LINE API 設定
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheets 初始化
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/coffee-bot-468008-86e28eaa87f3.json", scope)
client = gspread.authorize(creds)
sheet = client.open("coffee_orders").sheet1

# 建立備份工作表（若不存在會新增）
try:
    backup_sheet = client.open("coffee_orders").worksheet("DeletedOrders")
except:
    backup_sheet = client.open("coffee_orders").add_worksheet(title="DeletedOrders", rows="1000", cols="20")

# 使用者狀態記憶（簡單版）
user_states = {}

# 訂單解析
def parse_order_fields(text):
    parts = text.strip().split('\n')
    if len(parts) != 7:
        return None
    name, phone, coffee, style, qty, date, method = parts
    if not re.match(r'^09\d{8}$', phone):
        return None
    if not qty.isdigit():
        return None
    return {
        "name": name.strip(),
        "phone": phone.strip(),
        "coffee": coffee.strip(),
        "style": style.strip(),
        "qty": int(qty),
        "date": date.strip(),
        "method": method.strip()
    }

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
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

    if msg == "下單":
        user_states[user_id] = "ordering"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請依序輸入以下7個欄位（每行一項）：\n姓名\n電話（09xxxxxxxx）\n咖啡名稱\n樣式\n數量\n取貨日期（格式：YYYYMMDD，例如 20250810）\n取貨方式")
        )
        return

    elif msg == "修改訂單":
        user_states[user_id] = "editing"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請輸入您的『姓名』或『訂單編號』以查詢訂單：")
        )
        return

    elif state == "ordering":
        data = parse_order_fields(msg)
        if not data:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 輸入格式錯誤，請依照每行一項重新輸入共七項資訊。")
            )
            return

        # 處理日期格式轉換（YYYYMMDD → %Y-%m-%d）
        try:
            pickup_date = datetime.strptime(data['date'], "%Y%m%d")
            formatted_pickup_date = pickup_date.strftime("%Y-%m-%d")
        except ValueError:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 預計取貨日期格式錯誤，請輸入 8 位數格式（例如：20250810）")
            )
            return

        order_time = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
        order_id = str(uuid.uuid4())[:8]

        # 寫入 Google Sheet（含 user_id）
        sheet.append_row([
            order_id, data['name'], data['phone'], data['coffee'], data['style'],
            data['qty'], formatted_pickup_date, data['method'], order_time, user_id
        ])

        reply_text = f"✅ 訂單已完成：{data['coffee']} x{data['qty']}\n📌 訂單編號：{order_id}"
        today_str = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d')
        if formatted_pickup_date == today_str:
            reply_text += "\n⚠️ 溫馨提醒：您今天需取貨！"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        user_states[user_id] = "init"
        return

    elif state == "editing":
        query = msg
        records = sheet.get_all_values()
        headers = records[0]
        found = False

        for idx in range(len(records) - 1, 0, -1):
            row = records[idx]
            if query == row[0] or query == row[1]:
                backup_sheet.append_row(row)
                sheet.delete_rows(idx + 1)

                user_states[user_id] = "ordering"
                visible_fields = [f"{h}: {v}" for h, v in zip(headers, row) if v]
                reply_text = "✅ 找到並刪除以下訂單，請重新下單：\n" + "\n".join(visible_fields) + \
                             "\n\n請重新輸入7欄位資訊（不含訂單編號與送單時間）"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                found = True
                return

        if not found:
            user_states[user_id] = "init"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 查無符合的訂單編號或姓名，請再確認。"))
        return

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="👋 請輸入『下單』開始新訂單\n或輸入『修改訂單』來變更您的訂單")
        )
        user_states[user_id] = "init"

# ⏰ 自動提醒任務
def daily_pickup_reminder():
    records = sheet.get_all_values()
    today_str = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d')
    for row in records[1:]:
        try:
            pickup_date = row[6]
            user_id = row[9]
            if pickup_date == today_str:
                coffee = row[3]
                qty = row[5]
                msg = f"📦 溫馨提醒：您今天有咖啡訂單要取貨！（{coffee} x{qty}）"
                line_bot_api.push_message(user_id, TextSendMessage(text=msg))
        except IndexError:
            continue

# 啟用每日排程（早上 8 點）
scheduler = BackgroundScheduler()
scheduler.add_job(daily_pickup_reminder, 'cron', hour=8, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run()
