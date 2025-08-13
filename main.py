import os
import requests
from flask import Flask, request
from datetime import datetime
import pytz
import threading
import time
import schedule

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

def get_crypto_data():
    try:
        btc_dom = requests.get("https://api.coingecko.com/api/v3/global").json()
        btc_dominance = btc_dom["data"]["market_cap_percentage"]["btc"]
        total_mcap = btc_dom["data"]["total_market_cap"]["usd"]

        eth_btc = requests.get(
            "https://api.coingecko.com/api/v3/coins/ethereum"
        ).json()["market_data"]["price_change_percentage_7d_in_currency"]["btc"]

        # New DeFiLlama endpoint
        defi_data = requests.get("https://api.llama.fi/overview/tvl").json()
        defi_tvl_change_7d = defi_data["change_7d"]

        funding_data = requests.get(
            "https://api.coinglass.com/api/funding"
        ).json()  # giả định bạn có API key

        funding_avg = sum([x["fundingRate"] for x in funding_data["data"]]) / len(funding_data["data"])

        alt_index = requests.get(
            "https://www.blockchaincenter.net/api/altcoin-season-index"
        ).json()["seasonIndex"]

        alt_mcap = total_mcap * (1 - btc_dominance / 100)

        signals = []
        if eth_btc > 3:
            signals.append("ETH/BTC > +3% (7d)")
        if funding_avg > 0:
            signals.append("Funding Rate dương")
        if alt_index >= 75:
            signals.append("Altcoin Season Index >= 75")

        tz = pytz.timezone("Asia/Ho_Chi_Minh")
        now_vn = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        report = f"""📊 Crypto Daily Report — {now_vn} VN

1) BTC Dominance: {btc_dominance:.2f}%
2) Total Market Cap: ${total_mcap:,.0f}
3) Altcoin Market Cap (est): ${alt_mcap:,.0f}
4) ETH/BTC 7d change: {eth_btc:+.2f}%
5) DeFi TVL 7d change: {defi_tvl_change_7d:+.2f}%
6) Funding avg sample: {funding_avg:+.6f}
7) Altcoin Season Index: {alt_index}

⚡ Signals triggered: {len(signals)}
- """ + "\n- ".join(signals) + """

📌 Ghi chú:
- ETH/BTC > +3% (7d) + Funding Rate dương + Altcoin Season Index >= 75 → Altcoin Season có thể đến trong 2–6 tuần.
- Theo dõi hằng ngày để xác nhận xu hướng.
- Đây không phải lời khuyên đầu tư.

Code by: HNT
"""
        return report
    except Exception as e:
        return f"Lỗi khi lấy dữ liệu: {e}"

def send_report():
    text = get_crypto_data()
    if BOT_TOKEN and CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text}
        )

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data and "text" in data["message"]:
        text = data["message"]["text"]
        chat_id = data["message"]["chat"]["id"]
        if text.strip().lower() == "/check":
            report = get_crypto_data()
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": report}
            )
    return {"ok": True}

def scheduler():
    schedule.every().day.at("07:00").do(send_report)
    while True:
        schedule.run_pending()
        time.sleep(30)

threading.Thread(target=scheduler, daemon=True).start()
