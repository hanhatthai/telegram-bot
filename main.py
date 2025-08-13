
import os
import math
import datetime as dt
import pytz
import requests
from typing import Optional, Tuple, List

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ===========================
# ENV
# ===========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")  # for auto-send
COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "")
CRYPTOQUANT_API_KEY = os.getenv("CRYPTOQUANT_API_KEY", "")  # optional, if you have one

HCM_TZ = pytz.timezone("Asia/Ho_Chi_Minh")


# ===========================
# Helpers
# ===========================
def _fmt_usd(n: Optional[float]) -> str:
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "N/A"
    try:
        return f"${n:,.2f}" if n < 1e6 else f"${n:,.0f}"
    except Exception:
        return "N/A"


def _safe_get(url: str, **kwargs):
    try:
        r = requests.get(url, timeout=25, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ===========================
# Data fetchers
# ===========================
def get_global_from_coingecko():
    """
    Returns coingecko global payload or None.
    """
    return _safe_get("https://api.coingecko.com/api/v3/global")


def get_btc_dominance() -> Optional[float]:
    g = get_global_from_coingecko()
    if not g:
        return None
    try:
        return float(g["data"]["market_cap_percentage"]["btc"])
    except Exception:
        return None


def get_total_market_cap_usd() -> Optional[float]:
    g = get_global_from_coingecko()
    if not g:
        return None
    try:
        return float(g["data"]["total_market_cap"]["usd"])
    except Exception:
        return None


def get_altcoin_market_cap_est() -> Optional[float]:
    """
    Estimate altcoin market cap = Total - BTC mcap (simple estimate).
    """
    g = get_global_from_coingecko()
    if not g:
        return None
    try:
        total = float(g["data"]["total_market_cap"]["usd"])
        mcap_pct = g["data"]["market_cap_percentage"]
        btc_pct = float(mcap_pct["btc"])
        btc_mcap = total * btc_pct / 100.0
        return max(total - btc_mcap, 0.0)
    except Exception:
        return None


def get_eth_btc_change_7d_pct() -> Optional[float]:
    """
    7d change of ETH priced in BTC.
    """
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "btc",
        "ids": "ethereum",
        "price_change_percentage": "7d",
        "per_page": 1,
        "page": 1,
    }
    data = _safe_get(url, params=params)
    try:
        return float(data[0]["price_change_percentage_7d_in_currency"])
    except Exception:
        return None


def get_defi_tvl_change_7d_pct() -> Optional[float]:
    """
    DeFiLlama: rough average of protocol 7d TVL change (fallback).
    """
    url = "https://api.llama.fi/overview/defi?excludeTotalChart=true&excludeTotalDataChart=true"
    data = _safe_get(url)
    try:
        protocols = data.get("protocols", [])
        arr = [p.get("change_7d") for p in protocols if p.get("change_7d") is not None]
        if not arr:
            return None
        return sum(arr) / len(arr)
    except Exception:
        return None


def get_funding_rate_avg() -> Optional[float]:
    """
    Average perpetual funding rate from Coinglass (requires API key).
    """
    if not COINGLASS_API_KEY:
        return None
    url = "https://api.coinglass.com/api/futures/funding_rates"
    headers = {"coinglassSecret": COINGLASS_API_KEY}
    try:
        r = requests.get(url, headers=headers, timeout=25)
        r.raise_for_status()
        js = r.json()
        rates = [x.get("fundingRate") for x in js.get("data", []) if x.get("fundingRate") is not None]
        if not rates:
            return None
        return sum(rates) / len(rates)
    except Exception:
        return None


def get_stablecoin_netflow_cex_usd() -> Optional[float]:
    """
    Tries CryptoQuant first (if key available), else returns None.
    Positive = inflow to exchanges (risk-on).
    NOTE: You should supply your CRYPTOQUANT_API_KEY for this to work.
    """
    if not CRYPTOQUANT_API_KEY:
        return None

    # This is a generic pattern; adjust endpoint to your CQ plan.
    # Example endpoint (may vary by plan);
    # Replace 'stablecoin_exchange_netflow' with your exact metric slug if different.
    url = "https://api.cryptoquant.com/v1/btc/market-data/stablecoin_exchange_netflow"
    headers = {"Authorization": f"Bearer {CRYPTOQUANT_API_KEY}"}
    params = {"interval": "day", "window": "1d", "limit": 1}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=25)
        r.raise_for_status()
        js = r.json()
        # Expected format: {'data': [{'value': 12345.6, 'symbol': 'USD', ...}]}
        data = js.get("data")
        if isinstance(data, list) and data:
            val = data[-1].get("value")
            return float(val) if val is not None else None
        return None
    except Exception:
        return None


def get_alt_btc_spot_volume_ratio() -> Optional[float]:
    """
    Approximate Alt/BTC spot volume ratio using CoinGecko markets pages.
    Sums 24h volume of BTC vs non-BTC across top pages.
    """
    base_url = "https://api.coingecko.com/api/v3/coins/markets"
    per_page = 250
    pages = 4  # up to top 1000 coins for a better estimate

    btc_volume = 0.0
    alt_volume = 0.0

    for p in range(1, pages + 1):
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": p,
            "price_change_percentage": "24h",
        }
        data = _safe_get(base_url, params=params)
        if not isinstance(data, list):
            break
        if not data:
            break
        for coin in data:
            try:
                vol = float(coin.get("total_volume") or 0.0)
            except Exception:
                vol = 0.0
            if coin.get("symbol", "").lower() == "btc" or coin.get("id") == "bitcoin" or coin.get("name","").lower()=="bitcoin":
                btc_volume += vol
            else:
                alt_volume += vol

    if btc_volume <= 0:
        return None
    return alt_volume / btc_volume


def get_altcoin_season_index() -> Optional[int]:
    data = _safe_get("https://api.blockchaincenter.net/api/altcoin-season-index")
    try:
        return int(round(float(data.get("seasonIndex"))))
    except Exception:
        return None


# ===========================
# Report & Signals
# ===========================
def build_report() -> Tuple[str, dict]:
    now = dt.datetime.now(HCM_TZ).strftime("%Y-%m-%d %H:%M")

    btc_dom = get_btc_dominance()
    total_mc = get_total_market_cap_usd()
    altcap = get_altcoin_market_cap_est()
    ethbtc_7d = get_eth_btc_change_7d_pct()
    defi_7d = get_defi_tvl_change_7d_pct()
    funding_avg = get_funding_rate_avg()
    netflow = get_stablecoin_netflow_cex_usd()
    alt_btc_ratio = get_alt_btc_spot_volume_ratio()
    season_idx = get_altcoin_season_index()

    # Signals
    s_ethbtc = (ethbtc_7d is not None) and (ethbtc_7d > 3.0)
    s_funding = (funding_avg is not None) and (funding_avg > 0)
    s_netflow = (netflow is not None) and (netflow > 0)
    s_ratio = (alt_btc_ratio is not None) and (alt_btc_ratio > 1.5)
    s_index = (season_idx is not None) and (season_idx > 75)

    active_signals = [s for s in [s_ethbtc, s_funding, s_netflow, s_ratio, s_index] if s]
    count_active = len(active_signals)

    # Level
    level = None
    if count_active >= 4 and s_index:
        level = "Altseason Confirmed"
    elif count_active >= 4:
        level = "Strong Signal"
    elif 2 <= count_active <= 3:
        level = "Early Signal"

    # Compose report
    lines: List[str] = []
    lines.append(f"📊 Crypto Daily Report — {now} (GMT+7)")
    lines.append("")
    lines.append(f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊" if btc_dom is not None else "1️⃣ BTC Dominance: N/A 🧊")
    lines.append(f"2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰")
    lines.append(f"3️⃣ Altcoin Market Cap (est): {_fmt_usd(altcap)} 🔷")
    lines.append(f"4️⃣ ETH/BTC 7d change: {ethbtc_7d:+.2f}% ✅" if ethbtc_7d is not None else "4️⃣ ETH/BTC 7d change: N/A ❔")
    lines.append(f"5️⃣ DeFi TVL 7d change: {defi_7d:+.2f}% 🧭" if defi_7d is not None else "5️⃣ DeFi TVL 7d change: N/A 🧭")
    if funding_avg is not None:
        lines.append(f"6️⃣ Funding Rate avg: {funding_avg:+.6f} 📈")
    else:
        lines.append("6️⃣ Funding Rate avg: N/A 📈")
    if netflow is not None:
        sign = "🔼" if netflow >= 0 else "🔽"
        lines.append(f"7️⃣ Stablecoin Netflow (CEX): {netflow:,.0f} {sign}")
    else:
        lines.append("7️⃣ Stablecoin Netflow (CEX): N/A")
    if alt_btc_ratio is not None:
        lines.append(f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} ✅" if s_ratio else f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f}")
    else:
        lines.append("8️⃣ Alt/BTC Volume Ratio: N/A")
    if season_idx is not None:
        lines.append(f"9️⃣ Altcoin Season Index (BC): {season_idx} 🟢" if s_index else f"9️⃣ Altcoin Season Index (BC): {season_idx}")
    else:
        lines.append("9️⃣ Altcoin Season Index (BC): N/A")

    lines.append("")
    lines.append("— *Tín hiệu kích hoạt*:")
    lines.append(f"✅ ETH/BTC > +3% (7d) {'🟢' if s_ethbtc else '⚪'}")
    lines.append(f"✅ Funding Rate dương {'🟢' if s_funding else '⚪'}")
    lines.append(f"✅ Stablecoin NetFlow > 0 {'🟢' if s_netflow else '⚪'}")
    lines.append(f"✅ Alt/BTC Volume Ratio > 1.5 {'🟢' if s_ratio else '⚪'}")
    lines.append(f"✅ Altcoin Season Index > 75 {'🟢' if s_index else '⚪'}")

    if level:
        lines.append("")
        lines.append("— *Cảnh báo Altseason*:")
        if level == "Altseason Confirmed":
            lines.append("🔥 Altseason Confirmed — khả năng trong ~1–2 tuần")
        elif level == "Strong Signal":
            lines.append("🔥 Strong Signal — nhiều điều kiện đã kích hoạt")
        elif level == "Early Signal":
            lines.append("🔥 Early Signal — đang hình thành, cần theo dõi")

    lines.append("")
    lines.append("— *Ghi chú*:")
    lines.append("- Stablecoin inflow ⇒ dòng tiền sắp giải ngân.")
    lines.append("- Alt/BTC Volume Ratio > 1.5 ⇒ altcoin volume vượt BTC.")
    lines.append("- Altcoin Season Index > 75 ⇒ altseason rõ ràng.")
    lines.append("*Code by: HNT*")

    return "\n".join(lines), {
        "signals_active": count_active,
        "s_ethbtc": s_ethbtc,
        "s_funding": s_funding,
        "s_netflow": s_netflow,
        "s_ratio": s_ratio,
        "s_index": s_index,
        "level": level or "None",
    }


# ===========================
# Telegram handlers
# ===========================
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, _ = build_report()
    await update.message.reply_text(text, disable_web_page_preview=True)


async def send_daily(context: ContextTypes.DEFAULT_TYPE):
    text, _ = build_report()
    chat_id = CHAT_ID or context.job.chat_id
    if chat_id:
        await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN env.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # /check command
    app.add_handler(CommandHandler("check", check))

    # Auto send daily 07:00 GMT+7
    run_time = dt.time(hour=7, minute=0, tzinfo=HCM_TZ)
    app.job_queue.run_daily(send_daily, time=run_time, name="daily_report", chat_id=CHAT_ID if CHAT_ID else None)

    app.run_polling()


if __name__ == "__main__":
    main()
