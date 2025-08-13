import os
import requests
import logging
from datetime import datetime
import pytz
from flask import Flask, request
from telegram import Bot

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # ID nhóm hoặc cá nhân Telegram
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN chưa được đặt trong Environment Variables")

bot = Bot(token=BOT_TOKEN)
app = Flask(__name__)

# Hàm lấy dữ liệu an toàn
def safe_request(url, headers=None):
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"Lỗi khi lấy dữ liệu từ {url}: {e}")
        return None

# API lấy dữ liệu
def get_btc_dominance():
    data = safe_request("https://api.coingecko.com/api/v3/global")
    if data:
        return round(data["data"]["market_cap_percentage"]["btc"], 2)
    return "N/A"

def get_market_caps():
    data = safe_request("https://api.coingecko.com/api/v3/global")
    if data:
        total = data["data"]["total_market_cap"]["usd"]
        btc_d = data["data"]["market_cap_percentage"]["btc"] / 100
        alt = total * (1 - btc_d)
        return total, alt
    return "N/A", "N/A"

def get_eth_btc_change():
    data = safe_request(
        "https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=btc&days=7"
    )
    if data and "prices" in data:
        prices = [p[1] for p in data["prices"]]
        change = ((prices[-1] - prices[0]) / prices[0]) * 100
        return round(change, 2)
    return "N/A"

def get_defi_tvl_change():
    data = safe_request("https://api.llama.fi/v2/historicalChainTvl")
    if data:
        if isinstance(data, list) and len(data) >= 8:
            last = sum(chain.get("tvl", 0) for chain in data[-1].values())
            prev = sum(chain.get("tvl", 0) for chain in data[-8].values())
            change = ((last - prev) / prev) * 100 if prev else 0
            return round(change, 2)
    return "N/A"

def get_funding_rate():
    data = safe_request("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=10")
    if data:
        avg_rate = sum(float(x["fundingRate"]) for x in data) / len(data)
        return round(avg_rate, 8)
    return "N/A"

def get_altcoin_season_index():
    headers = {"User-Agent": "Mozilla/5.0"}
    data = safe_request("https://www.blockchaincenter.net/api/altcoin-season-index", headers=headers)
    if data and "season" in data:
        return data["season"]
    return "N/A"

# Format số
def fmt_usd(x):
    if isinstance(x, (int, float)):
        return "${:,.0f}".format(x)
    return x

# Hàm tạo báo cáo
def generate_report():
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    btc_d = get_btc_dominance()
    total_mc, alt_mc = get_market_caps()
    eth_btc = get_eth_btc_change()
    defi_tvl = get_defi_tvl_change()
    funding = get_funding_rate()
    alt_season = get_altcoin_season_index()

    signals = []
    if isinstance(eth_btc, (int, float)) and eth_btc > 3:
        signals.append("ETH/BTC > +3% (7d)")
    if isinstance(funding, (int, float)) and funding > 0:
        signals.append("Funding Rate positive")
    if isinstance(alt_season, (int, float)) and alt_season >= 75:
        signals.append("Altcoin Season Index >= 75")

    report = f"""📊 Crypto Daily Report — {now} (GMT+7)

1) BTC Dominance: {btc_d}%
2) Total Market Cap: {fmt_usd(total_mc)}
3) Altcoin Market Cap (est): {fmt_usd(alt_mc)}
4) ETH/BTC 7d change: {eth_btc}%
5) DeFi TVL 7d change: {defi_tvl}%
6) Funding avg sample: {funding}
7) Altcoin Season Index: {alt_season}

⚠️ Signals triggered: {len(signals)} — {", ".join(signals) if signals else "Không có tín hiệu"}

📌 Chú ý:
- ≥2 tín hiệu mạnh ⇒ có thể 2–4 tuần tới altcoin season.
- Altcoin Season Index ≥ 75 ⇒ thường đang trong giai đoạn altseason.
- Funding rate dương ⇒ phe long chiếm ưu thế.

💡 Điều kiện để mua mạnh hơn: BTC.D giảm + ETH/BTC tăng mạnh + Funding Rate dương.

Code by: HNT
"""
    return report

# Endpoint Telegram webhook
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]
        if text.strip().lower() == "/check":
            report = generate_report()
            bot.send_message(chat_id=chat_id, text=report)
    return "OK"

# Endpoint test
@app.route("/")
def index():
    return "Bot is running."

# Gửi tự động 7h sáng
import threading, time

def auto_send():
    while True:
        tz = pytz.timezone("Asia/Ho_Chi_Minh")
        now = datetime.now(tz)
        if now.hour == 7 and now.minute == 0:
            report = generate_report()
            if CHAT_ID:
                bot.send_message(chat_id=CHAT_ID, text=report)
            time.sleep(60)
        time.sleep(20)

threading.Thread(target=auto_send, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
