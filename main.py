# main.py
import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import statistics
import time

app = Flask(__name__)

# ====== CONFIG - từ Environment Variables trên Railway ======
BOT_TOKEN = os.getenv("BOT_TOKEN")          # token từ @BotFather
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")  # chat id bạn muốn nhận daily report
SCHEDULE_SECRET = os.getenv("SCHEDULE_SECRET")  # bí mật để cron gọi /daily
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN chưa được đặt trong Environment Variables")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ====== Helpers Telegram ======
def send_message(chat_id, text, parse_mode="Markdown"):
    url = TELEGRAM_API + "/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, timeout=15)
        return r.json()
    except Exception as e:
        print("send_message error:", e)
        return None

# ====== Data fetch functions ======
def fetch_coingecko_global():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=15)
        r.raise_for_status()
        return r.json().get("data", {})
    except Exception as e:
        print("coingecko global error:", e)
        return {}

def get_btc_dominance():
    data = fetch_coingecko_global()
    return data.get("market_cap_percentage", {}).get("btc")

def get_total_marketcap():
    data = fetch_coingecko_global()
    return data.get("total_market_cap", {}).get("usd")

def get_altcoin_marketcap_estimated():
    data = fetch_coingecko_global()
    btc_dom = data.get("market_cap_percentage", {}).get("btc", 0)
    eth_dom = data.get("market_cap_percentage", {}).get("eth", 0)
    total = data.get("total_market_cap", {}).get("usd", 0)
    # ước lượng altcap = total * (1 - btc_dom - eth_dom)
    return total * (1 - (btc_dom + eth_dom)/100.0) if total else None

def coingecko_market_chart(id_, days=7, vs_currency="usd"):
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{id_}/market_chart"
        r = requests.get(url, params={"vs_currency": vs_currency, "days": days}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("market_chart error", id_, e)
        return {}

def get_ethbtc_7d_pct_change():
    eth = coingecko_market_chart("ethereum", days=7)
    btc = coingecko_market_chart("bitcoin", days=7)
    try:
        eth_start = eth["prices"][0][1]
        eth_end = eth["prices"][-1][1]
        btc_start = btc["prices"][0][1]
        btc_end = btc["prices"][-1][1]
        ratio_start = eth_start / btc_start
        ratio_end = eth_end / btc_end
        return (ratio_end - ratio_start) / ratio_start * 100.0
    except Exception as e:
        print("ethbtc calc error", e)
        return None

def get_defi_tvl_7d_pct_change():
    try:
        r = requests.get("https://api.llama.fi/tvl", timeout=20)
        r.raise_for_status()
        series = r.json()
        if not isinstance(series, list) or len(series) < 8:
            return None
        latest = series[-1]["totalLiquidityUSD"]
        prev = series[-8]["totalLiquidityUSD"]
        return (latest - prev) / prev * 100.0 if prev else None
    except Exception as e:
        print("defillama error", e)
        return None

def get_funding_sample_avg():
    symbols = ["SOLUSDT","MATICUSDT","AVAXUSDT","XRPUSDT","LINKUSDT"]
    vals = []
    for s in symbols:
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": s, "limit": 6}, timeout=10)
            r.raise_for_status()
            arr = r.json()
            for it in arr:
                if "fundingRate" in it:
                    vals.append(float(it["fundingRate"]))
            time.sleep(0.15)
        except Exception as e:
            print("binance funding error", s, e)
            continue
    return statistics.mean(vals) if vals else None

# ====== Build report text (human friendly) ======
def build_report_text():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    btc_dom = get_btc_dominance()
    total_mc = get_total_marketcap()
    alt_mc = get_altcoin_marketcap_estimated()
    ethbtc_7d = get_ethbtc_7d_pct_change()
    tvl_7d = get_defi_tvl_7d_pct_change()
    funding_avg = get_funding_sample_avg()

    lines = []
    lines.append(f"📊 *Crypto Daily Report* — {now}")
    lines.append("")
    lines.append(f"1) BTC Dominance: *{btc_dom:.2f}%*") if btc_dom is not None else lines.append("1) BTC Dominance: _N/A_")
    lines.append(f"2) Total Market Cap: *${total_mc:,.0f}*") if total_mc is not None else lines.append("2) Total Market Cap: _N/A_")
    lines.append(f"3) Altcoin Market Cap (est): *${alt_mc:,.0f}*") if alt_mc is not None else lines.append("3) Altcoin Market Cap (est): _N/A_")
    lines.append(f"4) ETH/BTC 7d change: *{ethbtc_7d:+.2f}%*") if ethbtc_7d is not None else lines.append("4) ETH/BTC 7d change: _N/A_")
    lines.append(f"5) DeFi TVL 7d change: *{tvl_7d:+.2f}%*") if tvl_7d is not None else lines.append("5) DeFi TVL 7d change: _N/A_")
    lines.append(f"6) Funding avg sample: *{funding_avg:+.6f}*") if funding_avg is not None else lines.append("6) Funding avg sample: _N/A_")
    lines.append("")
    # Simple signal summary
    signals = []
    if btc_dom is not None and btc_dom < 55.0:
        signals.append("BTC dominance < 55%")
    if ethbtc_7d is not None and ethbtc_7d > 3.0:
        signals.append("ETH/BTC + >3% (7d)")
    if tvl_7d is not None and tvl_7d > 3.0:
        signals.append("DeFi TVL + >3% (7d)")
    if funding_avg is not None and funding_avg > 0.0:
        signals.append("Funding avg positive")

    lines.append(f"*Signals triggered:* {len(signals)} — " + (", ".join(signals) if signals else "None"))
    return "\n".join(lines)

# ====== Routes ======
# Webhook endpoint — Telegram will POST updates here when messages arrive.
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "no json"}), 400

        # handle messages:
        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return jsonify({"ok": True})

        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        # Commands:
        if text.startswith("/start"):
            send_message(chat_id, "Xin chào! Gõ /report để nhận báo cáo hàng ngày.")
        elif text.startswith("/report"):
            send_message(chat_id, "Đang tổng hợp báo cáo... (có thể vài giây)")
            report = build_report_text()
            send_message(chat_id, report)
        else:
            send_message(chat_id, "Không hiểu lệnh. Gõ /report để nhận báo cáo.")

        return jsonify({"ok": True})
    except Exception as e:
        print("webhook exception:", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# Cron-callable route to send daily report to OWNER_CHAT_ID
@app.route("/daily", methods=["GET"])
def daily_route():
    secret = request.args.get("secret", "")
    if not SCHEDULE_SECRET or secret != SCHEDULE_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    if not OWNER_CHAT_ID:
        return jsonify({"ok": False, "error": "OWNER_CHAT_ID not set"}), 500
    try:
        send_message(OWNER_CHAT_ID, "📣 Báo cáo theo lịch hàng ngày — đang tổng hợp...")
        report = build_report_text()
        send_message(OWNER_CHAT_ID, report)
        return jsonify({"ok": True})
    except Exception as e:
        print("daily error", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "Bot is alive."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))