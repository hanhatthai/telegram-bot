import os
import re
import math
import json
import datetime as dt
import pytz
import requests
from typing import Optional, List, Tuple

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ====== ENV / TZ ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
HCM_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

HTTP_TIMEOUT = 25
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36"

# ====== HTTP helpers ======
def _safe_get_json(url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=headers, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[HTTP][JSON][FAIL] {url} -> {e}")
        return None

def _safe_get_text(url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", UA)
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers=headers, **kwargs)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"[HTTP][TEXT][FAIL] {url} -> {e}")
        return None

# ====== format helpers ======
def _fmt_usd(n: Optional[float]) -> str:
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "N/A"
    return f"${n:,.2f}" if n < 1_000_000 else f\"${n:,.0f}\"

def _pct(v: Optional[float]) -> Optional[str]:
    return f"{v:+.2f}%" if v is not None else None

# ====== metrics: BTC dominance / total cap / alt cap / ETHBTC / funding / volumes ======
def get_coingecko_global():
    url = "https://api.coingecko.com/api/v3/global"
    js = _safe_get_json(url)
    if js:
        print("[CG][global] ok")
    return js

def get_btc_dominance() -> Optional[float]:
    g = get_coingecko_global()
    try:
        v = float(g["data"]["market_cap_percentage"]["btc"])
        print(f"[BTC.D] {v}")
        return v
    except Exception as e:
        print(f"[BTC.D][ERR] {e}")
        return None

def get_total_market_cap_usd() -> Optional[float]:
    g = get_coingecko_global()
    try:
        v = float(g["data"]["total_market_cap"]["usd"])
        print(f"[TOTAL MC] {v}")
        return v
    except Exception as e:
        print(f"[TOTAL MC][ERR] {e}")
        return None

def get_altcoin_market_cap_est() -> Optional[float]:
    g = get_coingecko_global()
    try:
        total = float(g["data"]["total_market_cap"]["usd"])
        btc_pct = float(g["data"]["market_cap_percentage"]["btc"])
        v = max(total - total * btc_pct / 100, 0.0)
        print(f"[ALTCAP est] {v}")
        return v
    except Exception as e:
        print(f"[ALTCAP est][ERR] {e}")
        return None

def get_eth_btc_change_7d_pct() -> Optional[float]:
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "btc", "ids": "ethereum", "price_change_percentage": "7d"}
    js = _safe_get_json(url, params=params)
    try:
        v = float(js[0]["price_change_percentage_7d_in_currency"])
        print(f"[ETH/BTC 7d] {v}")
        return v
    except Exception as e:
        print(f"[ETH/BTC 7d][ERR] {e}")
        return None

def get_funding_rate_avg() -> Optional[float]:
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    js = _safe_get_json(url)
    try:
        rates = [float(x["lastFundingRate"]) for x in js if x.get("lastFundingRate") is not None]
        v = sum(rates) / len(rates) if rates else None
        print(f"[Funding avg] {v}")
        return v
    except Exception as e:
        print(f"[Funding avg][ERR] {e}")
        return None

def get_alt_btc_spot_volume_ratio() -> Optional[float]:
    base = "https://api.coingecko.com/api/v3/coins/markets"
    btc_vol, alt_vol = 0.0, 0.0
    try:
        for p in range(1, 4):
            js = _safe_get_json(base, params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": p})
            if not js:
                break
            for coin in js:
                vol = float(coin.get("total_volume") or 0)
                if coin.get("id") == "bitcoin":
                    btc_vol += vol
                else:
                    alt_vol += vol
        v = (alt_vol / btc_vol) if btc_vol > 0 else None
        print(f"[Alt/BTC vol ratio] alt={alt_vol} / btc={btc_vol} -> {v}")
        return v
    except Exception as e:
        print(f"[Alt/BTC vol ratio][ERR] {e}")
        return None

# ====== 5) DeFi TVL 7d: multi-source ======
def _tvl_7d_from_series(series: List[List[float]]) -> Optional[float]:
    if not series or len(series) < 8:
        return None
    try:
        last = float(series[-1][1])
        prev = float(series[-8][1])
        if prev != 0:
            return (last - prev) / prev * 100.0
    except Exception as e:
        print(f"[TVL calc][ERR] {e}")
    return None

def get_defi_tvl_change_7d_pct() -> Optional[float]:
    # Source A: charts/defi
    js = _safe_get_json("https://api.llama.fi/charts/defi")
    if isinstance(js, list) and js:
        v = _tvl_7d_from_series(js)
        if v is not None:
            print("[TVL] charts/defi OK")
            return v
        else:
            print("[TVL] charts/defi series insufficient")

    # Source B: overview/total?excludeTotalChart=false  -> totalDataChart
    js2 = _safe_get_json("https://api.llama.fi/overview/total?excludeTotalChart=false&excludeTotalDataChart=false")
    if js2:
        series = js2.get("totalDataChart") or js2.get("totalDefiChart")
        if isinstance(series, list) and series:
            v = _tvl_7d_from_series(series)
            if v is not None:
                print("[TVL] overview/total OK")
                return v

    # Source C: scrape HTML (best-effort)
    html = _safe_get_text("https://defillama.com/")
    if html:
        # Find JSON array like [[ts,val],...]
        m = re.search(r"\[\s*\[\s*\d{10,13}\s*,\s*\d+(\.\d+)?\s*\](?:\s*,\s*\[\s*\d{10,13}\s*,\s*\d+(\.\d+)?\s*\]\s*){50,}\]", html)
        if m:
            try:
                arr = json.loads(m.group(0))
                v = _tvl_7d_from_series(arr)
                if v is not None:
                    print("[TVL] scrape HTML OK")
                    return v
            except Exception as e:
                print(f"[TVL][HTML parse][ERR] {e}")

    print("[TVL] all sources failed")
    return None

# ====== 7) Stablecoin Netflow (CEX): multi-source ======
def get_stablecoin_netflow_cex_usd() -> Optional[float]:
    # Source A: WhalePortal JSON (list of dicts with 'netflow')
    js = _safe_get_json("https://whaleportal.com/api/stablecoin-netflows")
    if js:
        try:
            if isinstance(js, list) and len(js) > 0:
                latest = js[-1]
                if isinstance(latest, dict) and "netflow" in latest:
                    v = float(latest["netflow"]) / 1_000_000  # to million USD
                    print("[Netflow] WhalePortal JSON OK")
                    return v
            # Some variants return dict with 'netflow'
            if isinstance(js, dict) and "netflow" in js:
                v = float(js["netflow"]) / 1_000_000
                print("[Netflow] WhalePortal JSON(d) OK")
                return v
        except Exception as e:
            print(f"[Netflow][WhalePortal JSON][ERR] {e}")

    # Source B: WhalePortal HTML (very-best-effort)
    html = _safe_get_text("https://whaleportal.com/stablecoin-netflows")
    if html:
        try:
            # Look for something like: data-netflow="12345678" or "netflow":12345678 in embedded scripts
            m = re.search(r'netflow["\']?\s*[:=]\s*([-+]?\d+(?:\.\d+)?)', html, re.I)
            if m:
                v = float(m.group(1)) / 1_000_000
                print("[Netflow] WhalePortal HTML OK")
                return v
        except Exception as e:
            print(f"[Netflow][WhalePortal HTML][ERR] {e}")

    # Source C: CryptoQuant HTML (can be blocked on cloud)
    html2 = _safe_get_text("https://cryptoquant.com/asset/stablecoin/chart/exchange-flows/netflow/all_exchange")
    if html2:
        try:
            m = re.search(r'netflow_total["\']?\s*[:=]\s*([-+]?\d+(?:\.\d+)?)', html2)
            if m:
                v = float(m.group(1)) / 1_000_000
                print("[Netflow] CryptoQuant HTML OK")
                return v
        except Exception as e:
            print(f"[Netflow][CQ HTML][ERR] {e}")

    print("[Netflow] all sources failed")
    return None

# ====== 9) Altcoin Season Index: multi-source ======
def get_altcoin_season_index() -> Optional[int]:
    # Source A: official API (new)
    js = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season")
    if js and isinstance(js, dict) and "index" in js:
        try:
            v = int(round(float(js["index"])))
            if 0 <= v <= 100:
                print("[ASI] api/altcoin-season OK")
                return v
        except Exception as e:
            print(f"[ASI][api altcoin-season][ERR] {e}")

    # Source B: old API variant
    js2 = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season-index")
    if js2:
        for key in ("seasonIndex", "altcoinSeasonIndex", "index"):
            try:
                if key in js2:
                    v = int(round(float(js2[key])))
                    if 0 <= v <= 100:
                        print("[ASI] api/altcoin-season-index OK")
                        return v
            except Exception as e:
                print(f"[ASI][api altcoin-season-index][{key}][ERR] {e}")

    # Source C: HTML scrape (only if phrase present)
    html = _safe_get_text("https://www.blockchaincenter.net/altcoin-season-index/")
    if html and ("Altcoin Season Index" in html):
        # strictly capture the number right after the phrase to avoid random "1"
        m = re.search(r'Altcoin Season Index[^0-9]{0,40}([0-9]{1,3})', html, re.I)
        if m:
            try:
                v = int(m.group(1))
                if 0 <= v <= 100:
                    print("[ASI] HTML scrape OK")
                    return v
            except Exception as e:
                print(f"[ASI][HTML parse][ERR] {e}")

    print("[ASI] all sources failed")
    return None

# ====== Report builder ======
def build_report() -> str:
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

    # Signals
    s_ethbtc = (ethbtc_7d is not None) and (ethbtc_7d > 3)
    s_funding = (funding_avg is not None) and (funding_avg > 0)
    s_netflow = (netflow_m is not None) and (netflow_m > 0)
    s_ratio = (alt_btc_ratio is not None) and (alt_btc_ratio > 1.5)
    s_index = (season_idx is not None) and (season_idx > 75)
    count_active = sum([s_ethbtc, s_funding, s_netflow, s_ratio, s_index])

    level = None
    if count_active >= 4 and s_index:
        level = "Altseason Confirmed"
    elif count_active >= 4:
        level = "Strong Signal"
    elif 2 <= count_active <= 3:
        level = "Early Signal"

    lines = [f"📊 <b>Crypto Daily Report</b> — {now} (GMT+7)", ""]

    lines.append(f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊" if btc_dom is not None else "1️⃣ BTC Dominance: N/A 🧊")
    lines.append(f"2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰")
    lines.append(f"3️⃣ Altcoin Market Cap (est): {_fmt_usd(altcap)} 🔷")
    lines.append(f"4️⃣ ETH/BTC 7d change: {_pct(ethbtc_7d) or 'N/A'} {'✅' if s_ethbtc else ''}")
    lines.append(f"5️⃣ DeFi TVL 7d change: {_pct(defi_7d) or 'N/A'} 🧭")
    if funding_avg is not None:
        lines.append(f"6️⃣ Funding Rate avg: {funding_avg:+.6f} {'📈' if funding_avg >= 0 else '📉'}")
    else:
        lines.append("6️⃣ Funding Rate avg: N/A")
    lines.append(
        f"7️⃣ Stablecoin Netflow (CEX): {netflow_m:+.0f} M {'🔼' if (netflow_m or 0) >= 0 else '🔽'}"
        if netflow_m is not None else "7️⃣ Stablecoin Netflow (CEX): N/A"
    )
    lines.append(
        f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} {'✅' if s_ratio else ''}"
        if alt_btc_ratio is not None else "8️⃣ Alt/BTC Volume Ratio: N/A"
    )
    lines.append(
        f"9️⃣ Altcoin Season Index (BC): {season_idx} {'🟢' if s_index else ''}"
        if season_idx is not None else "9️⃣ Altcoin Season Index (BC): N/A"
    )

    # Signals
    lines += ["", "— <b>Tín hiệu kích hoạt</b>:"]
    lines.append(f"{'✅' if s_ethbtc else '❌'} ETH/BTC > +3% (7d)")
    lines.append(f"{'✅' if s_funding else '❌'} Funding Rate dương")
    lines.append(f"{'✅' if s_netflow else '❌'} Stablecoin Netflow > 0")
    lines.append(f"{'✅' if s_ratio else '❌'} Alt/BTC Volume Ratio > 1.5")
    lines.append(f"{'✅' if s_index else '❌'} Altcoin Season Index > 75")

    if level:
        lines += ["", "— <b>Cảnh báo Altseason</b>:"]
        lines.append(
            "🔥 <b>Altseason Confirmed</b> — khả năng trong ~1–2 tuần" if level == "Altseason Confirmed"
            else "🔥 <b>Strong Signal</b> — nhiều điều kiện đã kích hoạt" if level == "Strong Signal"
            else "🔥 <b>Early Signal</b> — đang hình thành, cần theo dõi"
        )

    lines += [
        "",
        "— <i>Ghi chú</i>:",
        "• Stablecoin netflow dương ⇒ dòng tiền sắp giải ngân.",
        "• Alt/BTC volume ratio > 1.5 ⇒ altcoin volume vượt BTC.",
        "• Altseason Index > 75 ⇒ xu hướng altseason rõ ràng.",
        "<i>Code by: HNT</i>",
    ]
    return "\n".join(lines)

# ====== Telegram handlers ======
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

# ====== Flask + thread for Telegram bot ======
from flask import Flask
import threading
import asyncio

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def start_bot():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN env.")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("check", check))
    # schedule daily 07:00 GMT+7
    tg_app.job_queue.run_daily(send_daily, time=dt.time(hour=7, tzinfo=HCM_TZ))
    # allow run in non-main thread (Gunicorn worker)
    tg_app.run_polling(stop_signals=None)

# Start Telegram bot thread immediately on import
threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
