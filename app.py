from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import re

app = Flask(__name__)

# LINE API 密鑰
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Google Sheet 初始化
scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets',
         "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/etc/secrets/coffee-bot-468008-86e28eaa87f3.json", scope)
client = gspread.authorize(creds)
sheet = client.open("coffee_orders").sheet1

# 建立備份工作表（若不存在會建立）
try:
    backup_sheet = client.open("coffee_orders").worksheet("DeletedOrders")
except:
    backup_sheet = client.open("coffee_orders").add_worksheet(title="DeletedOrders", rows="1000", cols="20")

# 狀態記憶暫存（開發用簡易版本，實務建議使用資料庫）
user_states = {}
user_order_temp = {}

# 解析訂單格式
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
            TextSendMessage(text="請依序輸入以下7個欄位（每行一項）：\n姓名\n電話（09xxxxxxxx）\n咖啡名稱\n樣式\n數量\n取貨日期\n取貨方式")
        )
        return

    elif msg == "修改訂單":
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
                TextSendMessage(text="⚠️ 輸入格式錯誤，請依照每行一項重新輸入共七項資訊。")
            )
            return
        sheet.append_row([
            data['name'], data['phone'], data['coffee'], data['style'],
            data['qty'], data['date'], data['method']
        ])
        user_states[user_id] = "init"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"✅ 已成功訂購：{data['coffee']} x{data['qty']}，謝謝您的購買！")
        )
        return

    elif state == "editing":
        name = msg.strip()
        records = sheet.get_all_values()
        found = False
        for idx in range(len(records)-1, 0, -1):
            row = records[idx]
            if row[0] == name:
                # 將資料移至備份
                backup_sheet.append_row(row)
                # 刪除原資料列
                sheet.delete_rows(idx+1)
                user_states[user_id] = "init"
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"已刪除以下訂單資料：\n{' / '.join(row)}\n請重新下單。")
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
        # 初始或非關鍵字訊息
        reply = "👋 哈囉！請選擇您要執行的動作：\n➡️ 請輸入『下單』開始新訂單\n✏️ 或輸入『修改訂單』更新您的訂單"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        user_states[user_id] = "init"
