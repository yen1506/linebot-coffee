from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

app = Flask(__name__)

# LINE API 密鑰（請在環境變數或 .env 設定）
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

# 訂單格式：姓名/電話/咖啡名稱/數量
def parse_order(text):
    pattern = r'^(.+?)\/(\d{9,10})\/(.+?)\/(\d+)$'
    match = re.match(pattern, text)
    if match:
        name, phone, coffee, qty = match.groups()
        return name, phone, coffee, int(qty)
    return None

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
    msg = event.message.text.strip()
    order = parse_order(msg)
    if order:
        name, phone, coffee, qty = order
        sheet.append_row([name, phone, coffee, qty])
        reply = f"✅ 訂購成功：{coffee} x{qty}"
    else:
        reply = "⚠️ 格式錯誤，請輸入：姓名/電話/咖啡名稱/數量\n範例：王小明/0912345678/拿鐵/2"
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

if __name__ == "__main__":
    app.run()
