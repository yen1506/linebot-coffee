from flask import Flask, request, abort
from datetime import datetime
from zoneinfo import ZoneInfo
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os
import re

app = Flask(__name__)

line_bot_api = LineBotApi(os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('/etc/secrets/coffee-bot-468008-86e28eaa87f3.json', scope)
client = gspread.authorize(creds)
sheet = client.open("coffee_orders").sheet1

try:
    backup_sheet = client.open("coffee_orders").worksheet("DeletedOrders")
except:
    backup_sheet = client.open("coffee_orders").add_worksheet(title="DeletedOrders", rows="1000", cols="20")

user_states = {}

def generate_order_id():
    today_str = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
    existing = [row for row in sheet.get_all_values() if row[0].startswith(f"ORD{today_str}")]
    order_num = len(existing) + 1
    return f"ORD{today_str}-{order_num:03}"

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
            TextSendMessage(text="請依序輸入以下資訊（每項換行填寫）：\n姓名\n電話【09xxxxxxxx】\n咖啡名稱\n樣式(掛耳包或豆子)\n數量\n取貨日期【YYYYMMDD】\n取貨方式【填寫面交(限吉安花蓮市區)或郵寄地址】")
        )
        return

    elif msg.startswith("修改訂單"):
        parts = msg.split()
        if len(parts) == 2:
            order_id = parts[1].strip()
            records = sheet.get_all_values()
            for idx in range(len(records)-1, 0, -1):
                row = records[idx]
                if row[0] == order_id:
                    deletion_time = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
                    backup_sheet.append_row(row + [deletion_time, "使用者輸入訂單編號修改"])
                    sheet.delete_rows(idx+1)
                    user_states[user_id] = "ordering"
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text=f"已刪除以下訂單（{order_id}）：\n{' / '.join(row[1:8])}\n請重新下單。")
                    )
                    return
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="❌ 查無此訂單編號，請確認後重新輸入。")
            )
            return
        else:
            user_states[user_id] = "editing"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請輸入您的姓名以查詢訂單：")
            )
            return

    elif state == "ordering":
        data = parse_order_fields(msg)
        if not data:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 輸入格式錯誤，請重新輸入資訊。")
            )
            return

        timestamp = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
        order_id = generate_order_id()
        sheet.append_row([
            order_id,
            data['name'], data['phone'], data['coffee'], data['style'],
            data['qty'], data['date'], data['method'], timestamp, user_id
        ])
        user_states[user_id] = "init"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"✅ 已成功訂購：{data['coffee']} x{data['qty']}\n📄 訂單編號：{order_id}\n謝謝您的購買！")
        )
        return

    elif state == "editing":
        name = msg.strip()
        records = sheet.get_all_values()
        found = False
        for idx in range(len(records)-1, 0, -1):
            row = records[idx]
            if row[1] == name:
                deletion_time = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
                backup_sheet.append_row(row + [deletion_time, "使用者輸入姓名修改"])
                sheet.delete_rows(idx+1)
                short_row = row[1:8]
                user_states[user_id] = "ordering"
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"已刪除以下訂單資料：\n{' / '.join(short_row)}\n請重新下單。")
                )
                found = True
                return
        if not found:
            user_states[user_id] = "init"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="❌ 查無此姓名的訂單，請再次確認或直接下單。")
            )
        return

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="您好！請輸入指令：\n👉 輸入『下單』開始訂購\n👉 輸入『修改訂單』修改資料")
        )

