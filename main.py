import os
import re
import math
import datetime as dt
import pytz
import requests
from typing import Optional, List
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

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
        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", "Mozilla/5.0")
        r = requests.get(url, timeout=25, headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()
    except:
        return None

def _safe_get_text(url: str, **kwargs):
    try:
        headers = kwargs.pop("headers", {})
        headers.setdefault("User-Agent", "Mozilla/5.0")
        r = requests.get(url, timeout=25, headers=headers, **kwargs)
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
    url = "https://api.llama.fi/overview/total?excludeTotalDataChart=false&excludeTotalChart=true"
    data = _safe_get_json(url)
    try:
        chart = (data.get("totalDataChart") or data.get("totalDefiChart") or [])
        if len(chart) < 8:
            return None

        def _ts_sec(ts):
            return ts / 1000 if ts > 1_000_000_000_000 else ts

        last_ts, last_val = chart[-1]
        last_ts = _ts_sec(last_ts)
        target = last_ts - 7 * 86400

        prev_val = None
        for t, v in reversed(chart):
            t = _ts_sec(t)
            if t <= target:
                prev_val = v
                break
        if prev_val is None:
            prev_val = chart[-8][1]

        if prev_val and prev_val != 0:
            return (last_val - prev_val) / prev_val * 100.0
    except:
        pass
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
    # 1. WhalePortal API (public)
    try:
        js = _safe_get_json("https://whaleportal.com/api/stablecoin-netflows")
        if js and "netflow" in js:
            return float(js["netflow"]) / 1_000_000  # đổi sang triệu USD
    except:
        pass

    # 2. CryptoQuant API (nếu có key)
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

    # 3. Fallback scrape CryptoQuant (có thể bị chặn trên cloud)
    try:
        html = _safe_get_text(
            "https://cryptoquant.com/asset/stablecoin/chart/exchange-flows/netflow/all_exchange",
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
        if data:
            for key in ("seasonIndex", "altcoinSeasonIndex", "index"):
                if key in data:
                    return int(round(float(data[key])))
    except:
        pass
    try:
        html = _safe_get_text("https://www.blockchaincenter.net/altcoin-season-index/")
        if not html:
            return None
        m = re.search(r'seasonIndex[^:\d]*[:=]\s*([0-9]{1,3})', html)
        if not m:
            m = re.search(r'Altcoin Season Index[^0-9]+([0-9]{1,3})', html, re.I)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                return val
    except:
        pass
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

    s_ethbtc = bool(ethbtc_7d is not None and ethbtc_7d > 3)
    s_funding = bool(funding_avg is not None and funding_avg > 0)
    s_netflow = bool(netflow_m is not None and netflow_m > 0)
    s_ratio = bool(alt_btc_ratio is not None and alt_btc_ratio > 1.5)
    s_index = bool(season_idx is not None and season_idx > 75)
    count_active = sum([s_ethbtc, s_funding, s_netflow, s_ratio, s_index])

    level = None
    if count_active >= 4 and s_index:
        level = "Altseason Confirmed"
    elif count_active >= 4:
        level = "Strong Signal"
    elif 2 <= count_active <= 3:
        level = "Early Signal"

    lines = [
        f"📊 <b>Crypto Daily Report</b> — {now} (GMT+7)",
        "",
        f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊" if btc_dom is not None else "1️⃣ BTC Dominance: N/A 🧊",
        f"2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰",
        f"3️⃣ Altcoin Market Cap (est): {_fmt_usd(altcap)} 🔷",
        f"4️⃣ ETH/BTC 7d change: {ethbtc_7d:+.2f}% ✅" if ethbtc_7d is not None else "4️⃣ ETH/BTC 7d change: N/A ❔",
        f"5️⃣ DeFi TVL 7d change: {defi_7d:+.2f}% 🧭" if defi_7d is not None else "5️⃣ DeFi TVL 7d change: N/A 🧭",
        f"6️⃣ Funding Rate avg: {funding_avg:+.6f} {'📈' if (funding_avg or 0) >= 0 else '📉'}" if funding_avg is not None else "6️⃣ Funding Rate avg: N/A 📈",
        f"7️⃣ Stablecoin Netflow (CEX): {netflow_m:+.0f} M {'🔼' if (netflow_m or 0) >= 0 else '🔽'}" if netflow_m is not None else "7️⃣ Stablecoin Netflow (CEX): N/A",
        f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} ✅" if s_ratio else (f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f}" if alt_btc_ratio is not None else "8️⃣ Alt/BTC Volume Ratio: N/A"),
        f"9️⃣ Altcoin Season Index (BC): {season_idx} 🟢" if s_index else (f"9️⃣ Altcoin Season Index (BC): {season_idx}" if season_idx is not None else "9️⃣ Altcoin Season Index (BC): N/A"),
        "",
        "— <b>Tín hiệu kích hoạt</b>:",
        f"{'✅' if s_ethbtc else '❌'} ETH/BTC > +3% (7d)",
        f"{'✅' if s_funding else '❌'} Funding Rate dương",
        f"{'✅' if s_netflow else '❌'} Stablecoin Netflow > 0",
        f"{'✅' if s_ratio else '❌'} Alt/BTC Volume Ratio > 1.5",
        f"{'✅' if s_index else '❌'} Altcoin Season Index > 75",
    ]
    if level:
        lines += [
            "",
            "— <b>Cảnh báo Altseason</b>:",
            "🔥 <b>Altseason Confirmed</b> — khả năng trong ~1–2 tuần" if level == "Altseason Confirmed"
            else "🔥 <b>Strong Signal</b> — nhiều điều kiện đã kích hoạt" if level == "Strong Signal"
            else "🔥 <b>Early Signal</b> — đang hình thành, cần theo dõi"
        ]
    lines += [
        "",
        "— <i>Ghi chú</i>:",
        "• Stablecoin netflow dương ⇒ dòng tiền sắp giải ngân.",
        "• Alt/BTC volume ratio > 1.5 ⇒ altcoin volume vượt BTC.",
        "• Altseason Index > 75 ⇒ xu hướng altseason rõ ràng.",
        "<i>Code by: HNT</i>",
    ]
    return "\n".join(lines)

# ----------------- Telegram -----------------
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        build_report(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def send_daily(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=build_report(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

# ----------------- Flask + Thread -----------------
from flask import Flask
import threading
import asyncio

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def start_bot():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN env.")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("check", check))
    tg_app.job_queue.run_daily(send_daily, time=dt.time(hour=7, tzinfo=HCM_TZ))
    tg_app.run_polling(stop_signals=None)

threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
