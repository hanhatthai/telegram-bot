import os
import re
import math
import datetime as dt
import pytz
import requests
from typing import Optional, List
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
CRYPTOQUANT_API_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")
HCM_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# ----------------- Helpers -----------------
def _fmt_usd(n: Optional[float]) -> str:
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "N/A"
    return f"${n:,.2f}" if n < 1e6 else f"${n:,.0f}"

def _safe_get_json(url: str, **kwargs):
    try:
        r = requests.get(url, timeout=25, **kwargs)
        r.raise_for_status()
        return r.json()
    except:
        return None

def _safe_get_text(url: str, **kwargs):
    try:
        r = requests.get(url, timeout=25, **kwargs)
        r.raise_for_status()
        return r.text
    except:
        return None

def _parse_number_candidates(text: str) -> List[float]:
    nums = []
    for m in re.finditer(r"[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[-+]?\d+(?:\.\d+)?", text):
        s = m.group(0).replace(",", "")
        try:
            nums.append(float(s))
        except:
            pass
    return nums

# ----------------- Data fetchers -----------------
def get_global_from_coingecko():
    return _safe_get_json("https://api.coingecko.com/api/v3/global")

def get_btc_dominance():
    g = get_global_from_coingecko()
    try:
        return float(g["data"]["market_cap_percentage"]["btc"])
    except:
        return None

def get_total_market_cap_usd():
    g = get_global_from_coingecko()
    try:
        return float(g["data"]["total_market_cap"]["usd"])
    except:
        return None

def get_altcoin_market_cap_est():
    g = get_global_from_coingecko()
    try:
        total = float(g["data"]["total_market_cap"]["usd"])
        btc_pct = float(g["data"]["market_cap_percentage"]["btc"])
        return max(total - total * btc_pct / 100, 0.0)
    except:
        return None

def get_eth_btc_change_7d_pct():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "btc", "ids": "ethereum", "price_change_percentage": "7d"}
    data = _safe_get_json(url, params=params)
    try:
        return float(data[0]["price_change_percentage_7d_in_currency"])
    except:
        return None

def get_defi_tvl_change_7d_pct():
    url = "https://api.llama.fi/overview/defi?excludeTotalChart=true&excludeTotalDataChart=true"
    data = _safe_get_json(url)
    try:
        arr = [p.get("change_7d") for p in data.get("protocols", []) if p.get("change_7d") is not None]
        return sum(arr) / len(arr) if arr else None
    except:
        return None

def get_funding_rate_avg():
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    data = _safe_get_json(url)
    try:
        rates = [float(x.get("lastFundingRate") or 0) for x in data if x.get("lastFundingRate") is not None]
        return sum(rates) / len(rates) if rates else None
    except:
        return None

def get_stablecoin_netflow_cex_usd():
    if CRYPTOQUANT_API_KEY:
        try:
            url = "https://api.cryptoquant.com/v1/stablecoin/exchange-flows/netflow"
            headers = {"Authorization": f"Bearer {CRYPTOQUANT_API_KEY}"}
            params = {"window": "day", "exchange": "all_exchange"}
            js = _safe_get_json(url, headers=headers, params=params)
            if js and "netflow_total" in js:
                return float(js["netflow_total"]) / 1_000_000
        except:
            pass
    try:
        html = _safe_get_text(
            "https://cryptoquant.com/asset/stablecoin/chart/exchange-flows/netflow/all_exchange",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if not html:
            return None
        pattern = r'netflow_total["\']?\s*[:=]\s*([-+]?\d+(?:\.\d+)?)'
        candidates = [float(m.group(1)) for m in re.finditer(pattern, html)]
        if not candidates:
            tail = html[-5000:]
            candidates.extend(_parse_number_candidates(tail))
        filtered = [x for x in candidates if abs(x) > 1e2]
        return filtered[-1] / 1_000_000 if filtered else None
    except:
        return None

def get_alt_btc_spot_volume_ratio():
    base_url = "https://api.coingecko.com/api/v3/coins/markets"
    btc_vol, alt_vol = 0, 0
    for p in range(1, 4):
        data = _safe_get_json(base_url, params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": p})
        if not data:
            break
        for coin in data:
            vol = float(coin.get("total_volume") or 0)
            if coin.get("id") == "bitcoin":
                btc_vol += vol
            else:
                alt_vol += vol
    return alt_vol / btc_vol if btc_vol > 0 else None

def get_altcoin_season_index():
    data = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season-index")
    try:
        return int(round(float(data.get("seasonIndex"))))
    except:
        return None

# ----------------- Report -----------------
def build_report():
    now = dt.datetime.now(HCM_TZ).strftime("%Y-%m-%d %H:%M")
    btc_dom = get_btc_dominance()
    total_mc = get_total_market_cap_usd()
    altcap = get_altcoin_market_cap_est()
    ethbtc_7d = get_eth_btc_change_7d_pct()
    defi_7d = get_defi_tvl_change_7d_pct()
    funding_avg = get_funding_rate_avg()
    netflow_m = get_stablecoin_netflow_cex_usd()
    alt_btc_ratio = get_alt_btc_spot_volume_ratio()
    season_idx = get_altcoin_season_index()

    s_ethbtc = ethbtc_7d and ethbtc_7d > 3
    s_funding = funding_avg and funding_avg > 0
    s_netflow = netflow_m and netflow_m > 0
    s_ratio = alt_btc_ratio and alt_btc_ratio > 1.5
    s_index = season_idx and season_idx > 75
    count_active = sum(bool(x) for x in [s_ethbtc, s_funding, s_netflow, s_ratio, s_index])

    level = None
    if count_active >= 4 and s_index:
        level = "Altseason Confirmed"
    elif count_active >= 4:
        level = "Strong Signal"
    elif 2 <= count_active <= 3:
        level = "Early Signal"

    lines = [
        f"📊 Crypto Daily Report — {now} (GMT+7)",
        "",
        f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊" if btc_dom else "1️⃣ BTC Dominance: N/A 🧊",
        f"2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰",
        f"3️⃣ Altcoin Market Cap (est): {_fmt_usd(altcap)} 🔷",
        f"4️⃣ ETH/BTC 7d change: {ethbtc_7d:+.2f}% ✅" if ethbtc_7d else "4️⃣ ETH/BTC 7d change: N/A ❔",
        f"5️⃣ DeFi TVL 7d change: {defi_7d:+.2f}% 🧭" if defi_7d else "5️⃣ DeFi TVL 7d change: N/A 🧭",
        f"6️⃣ Funding Rate avg: {funding_avg:+.6f} {'📈' if funding_avg >=0 else '📉'}" if funding_avg else "6️⃣ Funding Rate avg: N/A 📈",
        f"7️⃣ Stablecoin Netflow (CEX): {netflow_m:+.0f} M {'🔼' if netflow_m>=0 else '🔽'}" if netflow_m else "7️⃣ Stablecoin Netflow (CEX): N/A",
        f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} ✅" if s_ratio else f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f}" if alt_btc_ratio else "8️⃣ Alt/BTC Volume Ratio: N/A",
        f"9️⃣ Altcoin Season Index (BC): {season_idx} 🟢" if s_index else f"9️⃣ Altcoin Season Index (BC): {season_idx}" if season_idx else "9️⃣ Altcoin Season Index (BC): N/A",
    ]
    if level:
        lines.append("")
        lines.append("— *Cảnh báo Altseason*:")
        if level == "Altseason Confirmed":
            lines.append("🔥 Altseason Confirmed — khả năng trong ~1–2 tuần")
        elif level == "Strong Signal":
            lines.append("🔥 Strong Signal — nhiều điều kiện đã kích hoạt")
        elif level == "Early Signal":
            lines.append("🔥 Early Signal — đang hình thành, cần theo dõi")
    return "\n".join(lines)

# ----------------- Telegram -----------------
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_report(), disable_web_page_preview=True)

async def send_daily(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHAT_ID, text=build_report(), disable_web_page_preview=True)

# ----------------- Flask + Thread -----------------
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def start_bot():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN env.")
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("check", check))
    tg_app.job_queue.run_daily(send_daily, time=dt.time(hour=7, tzinfo=HCM_TZ))
    tg_app.run_polling()

# Chạy bot song song với Flask
threading.Thread(target=start_bot).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
