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

# 自動填上訂單金額
def update_prices_and_totals():
    try:
        # 抓取訂單清單和價格表
        order_ws = client.open("coffee_orders").worksheet("訂單清單")
        price_ws = client.open("coffee_orders").worksheet("價格表")

        order_data = order_ws.get_all_values()
        price_data = price_ws.get_all_values()

        order_df = pd.DataFrame(order_data[1:], columns=order_data[0])
        price_df = pd.DataFrame(price_data[1:], columns=price_data[0])

        # 移除空白列（若有）
        order_df = order_df[order_df["咖啡名稱"].notna()]
        price_df = price_df[price_df["咖啡名稱"].notna()]

        # 數量轉成數字
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors='coerce')

        # 合併訂單與價格
        merged_df = order_df.merge(price_df, how="left", on=["咖啡名稱", "掛耳包/豆子"], suffixes=('', '_價格'))

        # 若原表中已有「單價」「總金額」欄位則覆蓋，沒有則新增
        merged_df["單價"] = pd.to_numeric(merged_df["單價_價格"], errors='coerce')
        merged_df["總金額"] = merged_df["單價"] * merged_df["數量"]

        # 清理欄位順序（確保與原始順序一致）
        final_columns = order_data[0]
        for col in ["單價", "總金額"]:
            if col not in final_columns:
                final_columns.append(col)

        # 依照欄位順序重新整理 DataFrame
        merged_df = merged_df.reindex(columns=final_columns)

        # 更新 Google Sheet（含標題列）
        order_ws.update([final_columns] + merged_df.astype(str).values.tolist())

        print("✅ 價格與總金額更新完成")
    except Exception as e:
        print(f"❌ 更新價格時發生錯誤：{e}")

# 月報表
def generate_monthly_summary():
    try:
        order_ws = client.open("coffee_orders").worksheet("訂單清單")

        order_data = order_ws.get_all_values()
        order_df = pd.DataFrame(order_data[1:], columns=order_data[0])

        # 確保資料正確型別
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors="coerce").fillna(0)
        order_df["單價"] = pd.to_numeric(order_df["單價"], errors="coerce").fillna(0)
        order_df["總金額"] = pd.to_numeric(order_df["總金額"], errors="coerce").fillna(0)

        # 擷取月份（預計取貨日期）
        order_df["月份"] = pd.to_datetime(order_df["預計取貨日期"], errors="coerce").dt.to_period("M").astype(str)

        # 群組統計
        summary_df = order_df.groupby(["月份", "咖啡名稱", "掛耳包/豆子", "單價"], as_index=False).agg({
            "數量": "sum",
            "總金額": "sum"
        })

        # 欄位排序
        summary_df = summary_df[["月份", "咖啡名稱", "掛耳包/豆子", "單價", "數量", "總金額"]]

        # 建立 / 更新「每月統計」工作表
        try:
            summary_ws = client.open("coffee_orders").worksheet("每月統計")
        except:
            summary_ws = client.open("coffee_orders").add_worksheet(title="每月統計", rows="1000", cols="10")

        # 寫入統計資料
        summary_ws.clear()
        summary_ws.update([summary_df.columns.tolist()] + summary_df.astype(str).values.tolist())

        print("✅ 每月統計已更新")
    except Exception as e:
        print(f"❌ 無法產生統計：{e}")

# 顧客購買分析
def generate_customer_summary():
    try:
        order_ws = client.open("coffee_orders").worksheet("訂單清單")

        order_data = order_ws.get_all_values()
        order_df = pd.DataFrame(order_data[1:], columns=order_data[0])

        # 確保欄位格式正確
        order_df["數量"] = pd.to_numeric(order_df["數量"], errors="coerce").fillna(0)
        order_df["總金額"] = pd.to_numeric(order_df["總金額"], errors="coerce").fillna(0)

        # 統計：依姓名 + 咖啡名稱 + 樣式 群組
        customer_df = order_df.groupby(["姓名", "咖啡名稱", "掛耳包/豆子"], as_index=False).agg({
            "數量": "count",    # 購買次數（筆數）
            "總金額": "sum"
        })

        # 欄位名稱調整
        customer_df.rename(columns={"數量": "購買次數"}, inplace=True)
        customer_df = customer_df[["姓名", "咖啡名稱", "掛耳包/豆子", "購買次數", "總金額"]]

        # 建立 / 更新「客群統計」工作表
        try:
            customer_ws = client.open("coffee_orders").worksheet("客群統計")
        except:
            customer_ws = client.open("coffee_orders").add_worksheet(title="客群統計", rows="1000", cols="10")

        customer_ws.clear()
        customer_ws.update([customer_df.columns.tolist()] + customer_df.astype(str).values.tolist())

        print("✅ 客群統計已更新")
    except Exception as e:
        print(f"❌ 無法產生客群統計：{e}")


# 啟用每日排程（早上 8 點）
scheduler = BackgroundScheduler()
scheduler.add_job(daily_pickup_reminder, 'cron', hour=8, minute=0)
scheduler.start()

if __name__ == "__main__":
    update_prices_and_totals()
    generate_monthly_summary()
    generate_customer_summary()
    app.run()
