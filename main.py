
import os
import requests
import logging
from flask import Flask, request
from datetime import datetime, timedelta
import pytz

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

app = Flask(__name__)

BINANCE_API = "https://api.binance.com/api/v3/ticker/24hr"
DEFI_LLAMA_TVL_API = "https://api.llama.fi/charts"  # New stable endpoint

def get_binance_price(symbol):
    try:
        r = requests.get(BINANCE_API, params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["lastPrice"])
    except Exception as e:
        logger.error(f"Binance API error for {symbol}: {e}")
        return None

def get_market_data():
    try:
        # Using Binance total market cap not possible directly, fallback to Coingecko (1 call only)
        url = "https://api.coingecko.com/api/v3/global"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        btc_dominance = data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0)
        total_market_cap = data.get("data", {}).get("total_market_cap", {}).get("usd", 0)
        return btc_dominance, total_market_cap
    except Exception as e:
        logger.error(f"Market data error: {e}")
        return None, None

def get_eth_btc_change_7d():
    try:
        url = "https://api.coingecko.com/api/v3/coins/ethereum/market_chart"
        r = requests.get(url, params={"vs_currency": "btc", "days": 7}, timeout=10)
        r.raise_for_status()
        prices = r.json().get("prices", [])
        if len(prices) < 2:
            return None
        start_price = prices[0][1]
        end_price = prices[-1][1]
        return ((end_price - start_price) / start_price) * 100
    except Exception as e:
        logger.error(f"ETH/BTC change error: {e}")
        return None

def get_defi_tvl_change():
    try:
        r = requests.get(DEFI_LLAMA_TVL_API, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 8:
            return None
        last = data[-1]["totalLiquidityUSD"]
        week_ago = data[-8]["totalLiquidityUSD"]
        return ((last - week_ago) / week_ago) * 100
    except Exception as e:
        logger.error(f"DefiLlama error: {e}")
        return None

def generate_report():
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    now_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    btc_dom, total_cap = get_market_data()
    eth_btc_change = get_eth_btc_change_7d()
    defi_tvl_change = get_defi_tvl_change()

    signals = []
    if eth_btc_change and eth_btc_change > 3:
        signals.append("ETH/BTC > +3% (7d)")
    if defi_tvl_change and defi_tvl_change > 0:
        signals.append("DeFi TVL tăng")

    report = f"📊 Crypto Daily Report — {now_str} (VN Time)\n"
    report += f"1) BTC Dominance: {btc_dom:.2f}%\n" if btc_dom else "1) BTC Dominance: N/A\n"
    report += f"2) Total Market Cap: ${total_cap:,.0f}\n" if total_cap else "2) Total Market Cap: N/A\n"
    report += f"3) ETH/BTC 7d change: {eth_btc_change:+.2f}%\n" if eth_btc_change else "3) ETH/BTC 7d change: N/A\n"
    report += f"4) DeFi TVL 7d change: {defi_tvl_change:+.2f}%\n" if defi_tvl_change else "4) DeFi TVL 7d change: N/A\n"
    report += f"\nSignals triggered: {len(signals)} — {', '.join(signals) if signals else 'None'}\n"
    report += "\nCode by: HNT"
    return report

def send_telegram_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Missing BOT_TOKEN or CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text})
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"].strip().lower()
        if text == "/check":
            report = generate_report()
            send_telegram_message(report)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
