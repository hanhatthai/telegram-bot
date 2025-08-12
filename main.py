# main.py
import os
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
import statistics
import time

app = Flask(__name__)

# ====== Config từ Environment Variables ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")  # chat id của bạn (string)
SCHEDULE_SECRET = os.getenv("SCHEDULE_SECRET")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN chưa được đặt trong Environment Variables")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ===== Helpers Telegram =====
def send_message(chat_id, text, parse_mode="Markdown"):
    try:
        url = TELEGRAM_API + "/sendMessage"
        resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, timeout=15)
        return resp.json()
    except Exception as e:
        print("send_message error:", e)
        return None


# ===== Data fetch + safe helpers =====
def fetch_coingecko_global():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=15)
        r.raise_for_status()
        return r.json().get("data", {})
    except Exception as e:
        print("coingecko global error:", e)
        return {}


def coingecko_market_chart(id_, days=7, vs_currency="usd"):
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{id_}/market_chart"
        r = requests.get(url, params={"vs_currency": vs_currency, "days": days}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("market_chart error", id_, e)
        return {}


def get_btc_dominance():
    data = fetch_coingecko_global()
    try:
        return float(data.get("market_cap_percentage", {}).get("btc"))
    except Exception:
        return None


def get_total_marketcap():
    data = fetch_coingecko_global()
    try:
        return float(data.get("total_market_cap", {}).get("usd"))
    except Exception:
        return None


def get_altcoin_marketcap_estimated():
    data = fetch_coingecko_global()
    try:
        btc_dom = float(data.get("market_cap_percentage", {}).get("btc", 0))
        eth_dom = float(data.get("market_cap_percentage", {}).get("eth", 0))
        total = float(data.get("total_market_cap", {}).get("usd", 0))
        if total:
            return total * (1 - (btc_dom + eth_dom) / 100.0)
    except Exception:
        pass
    return None


def get_ethbtc_7d_pct_change():
    """Trả về % thay đổi ETH/BTC trong 7 ngày, hoặc None nếu không đủ data."""
    try:
        eth = coingecko_market_chart("ethereum", days=7)
        btc = coingecko_market_chart("bitcoin", days=7)
        eth_prices = eth.get("prices") or []
        btc_prices = btc.get("prices") or []
        if len(eth_prices) < 2 or len(btc_prices) < 2:
            print("ethbtc: not enough price points")
            return None
        eth_start = eth_prices[0][1]
        eth_end = eth_prices[-1][1]
        btc_start = btc_prices[0][1]
        btc_end = btc_prices[-1][1]
        ratio_start = eth_start / btc_start
        ratio_end = eth_end / btc_end
        pct = (ratio_end - ratio_start) / ratio_start * 100.0
        return round(pct, 2)
    except Exception as e:
        print("ethbtc calc error", e)
        return None


def get_defi_tvl_7d_pct_change():
    """
    Sử dụng DefiLlama v2 historicalChainTVL endpoint cho 'all' chains.
    Trả về % thay đổi 7 ngày (float) hoặc None nếu lỗi.
    """
    try:
        url = "https://api.llama.fi/v2/historicalChainTVL/all"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            print("defillama: not enough data")
            return None

        # data items: [{"date": 169..., "tvl": 12345}, ...] (date timestamp)
        # sắp xếp theo date nếu cần
        data.sort(key=lambda x: x.get("date", 0))
        # latest
        latest = data[-1]
        latest_tvl = latest.get("tvl") or latest.get("totalLiquidityUSD") or None
        if latest_tvl is None:
            print("defillama: latest tvl missing")
            return None

        latest_date = datetime.utcfromtimestamp(int(latest.get("date")))
        target_date = latest_date - timedelta(days=7)

        # tìm entry gần nhất với target_date (dùng abs days)
        tvl_7d = None
        best_diff = None
        for entry in data:
            try:
                ed = datetime.utcfromtimestamp(int(entry.get("date")))
                diff_days = abs((ed - target_date).days)
                if best_diff is None or diff_days < best_diff:
                    best_diff = diff_days
                    tvl_7d = entry.get("tvl") or entry.get("totalLiquidityUSD")
            except Exception:
                continue

        if tvl_7d is None:
            print("defillama: no 7d-back point found")
            return None

        pct = (latest_tvl - tvl_7d) / tvl_7d * 100.0
        return round(pct, 2)
    except requests.HTTPError as e:
        print("defillama HTTP error", e)
        return None
    except Exception as e:
        print("defillama error", e)
        return None


def get_funding_sample_avg():
    # sample một vài perpetuals từ Binance USD-M để ước tính funding
    symbols = ["SOLUSDT", "MATICUSDT", "AVAXUSDT", "XRPUSDT", "LINKUSDT"]
    vals = []
    for s in symbols:
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": s, "limit": 4}, timeout=10)
            r.raise_for_status()
            arr = r.json()
            for it in arr:
                if "fundingRate" in it:
                    vals.append(float(it["fundingRate"]))
            time.sleep(0.12)
        except Exception as e:
            print("binance funding error", s, e)
            continue
    if not vals:
        return None
    return round(statistics.mean(vals), 8)


# ===== Build message =====
def build_report_text():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    btc_dom = get_btc_dominance()
    total_mc = get_total_marketcap()
    alt_mc = get_altcoin_marketcap_estimated()
    ethbtc_7d = get_ethbtc_7d_pct_change()
    tvl_7d = get_defi_tvl_7d_pct_change()
    funding_avg = get_funding_sample_avg()

    lines = []
    lines.append(f"📊 *Crypto Daily Report* — {now}\n")
    lines.append(f"1) BTC Dominance: {f'{btc_dom:.2f}%' if btc_dom is not None else '_N/A_'}")
    lines.append(f"2) Total Market Cap: {f'${total_mc:,.0f}' if total_mc is not None else '_N/A_'}")
    lines.append(f"3) Altcoin Market Cap (est): {f'${alt_mc:,.0f}' if alt_mc is not None else '_N/A_'}")
    lines.append(f"4) ETH/BTC 7d change: {f'{ethbtc_7d:+.2f}%' if ethbtc_7d is not None else '_N/A_'}")
    lines.append(f"5) DeFi TVL 7d change: {f'{tvl_7d:+.2f}%' if tvl_7d is not None else '_N/A_'}")
    lines.append(f"6) Funding avg sample: {f'{funding_avg:+.6f}' if funding_avg is not None else '_N/A_'}\n")

    # Simple signals
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


# ===== Routes =====
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"ok": False, "error": "no json"}), 400

        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return jsonify({"ok": True})

        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if text.startswith("/start"):
            send_message(chat_id, "Xin chào! Gõ /report để nhận báo cáo.")
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
