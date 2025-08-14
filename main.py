import os
import re
import math
import datetime as dt
import pytz
import requests
from typing import Optional
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from flask import Flask
import threading
import asyncio

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
HCM_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# ----------------- Helpers -----------------
def _fmt_usd(n: Optional[float]) -> str:
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "N/A"
    return f"${n:,.2f}" if n < 1_000_000 else f"${n:,.0f}"

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

# ----------------- Data fetchers -----------------
def get_btc_dominance():
    data = _safe_get_json("https://api.coingecko.com/api/v3/global")
    try:
        return float(data["data"]["market_cap_percentage"]["btc"])
    except:
        return None

def get_total_market_cap_usd():
    data = _safe_get_json("https://api.coingecko.com/api/v3/global")
    try:
        return float(data["data"]["total_market_cap"]["usd"])
    except:
        return None

def get_altcoin_market_cap_est():
    data = _safe_get_json("https://api.coingecko.com/api/v3/global")
    try:
        total = float(data["data"]["total_market_cap"]["usd"])
        btc_pct = float(data["data"]["market_cap_percentage"]["btc"])
        return total * (1 - btc_pct / 100)
    except:
        return None

def get_eth_btc_change_7d():
    data = _safe_get_json(
        "https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=btc&days=7&interval=daily"
    )
    try:
        prices = [p[1] for p in data["prices"]]
        return ((prices[-1] - prices[0]) / prices[0]) * 100
    except:
        return None

def get_defi_tvl_change_7d():
    data = _safe_get_json("https://api.llama.fi/charts")
    try:
        tvl = [p["totalLiquidityUSD"] for p in data]
        return ((tvl[-1] - tvl[-8]) / tvl[-8]) * 100
    except:
        return None

def get_funding_rate_avg():
    data = _safe_get_json("https://api.coinglass.com/api/pro/v1/futures/funding_rates?symbol=ALL")
    try:
        rates = [float(x["fundingRate"]) for x in data["data"]]
        return sum(rates) / len(rates)
    except:
        return None

def get_stablecoin_netflow_cex_usd():
    """
    Lấy dữ liệu Stablecoin Netflow (CEX) từ CryptoQuant (All Stablecoins ERC20).
    Trả về tuple (current_value_million_usd, avg7d_million_usd).
    """
    url = "https://api.cryptoquant.com/live/v4/ms/61af138856f85872fa84fc3c/charts/preview"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        last_value = j.get("lastValue")
        data = j.get("data") or []
        avg7d = None
        if data and isinstance(data, list):
            values = [v[1] for v in data[-7:] if isinstance(v, list) and len(v) >= 2 and isinstance(v[1], (int, float))]
            if values:
                avg7d = sum(values) / len(values)
        cur_m = round(float(last_value) / 1_000_000.0, 2) if last_value is not None else None
        avg_m = round(float(avg7d) / 1_000_000.0, 2) if avg7d is not None else None
        return cur_m, avg_m
    except Exception as e:
        print("Stablecoin Netflow fetch error:", e)
        return None, None

def get_alt_btc_volume_ratio():
    data = _safe_get_json("https://api.coingecko.com/api/v3/global")
    try:
        btc_vol = float(data["data"]["total_volume"]["btc"])
        alt_vol = float(data["data"]["total_volume"]["usd"]) - btc_vol
        return alt_vol / btc_vol
    except:
        return None

def get_altseason_index():
    data = _safe_get_json("https://www.blockchaincenter.net/api/altseason/current/")
    try:
        return float(data["altcoinSeasonIndex"])
    except:
        return None

# ----------------- Report -----------------
def build_report():
    now = dt.datetime.now(HCM_TZ)
    report = f"📊 Crypto Daily Report — {now.strftime('%Y-%m-%d %H:%M')} (GMT+7)\n\n"

    btc_dom = get_btc_dominance()
    total_mc = get_total_market_cap_usd()
    alt_mc = get_altcoin_market_cap_est()
    eth_btc_7d = get_eth_btc_change_7d()
    defi_tvl_7d = get_defi_tvl_change_7d()
    funding_avg = get_funding_rate_avg()
    netflow_cur, netflow_avg = get_stablecoin_netflow_cex_usd()
    alt_btc_ratio = get_alt_btc_volume_ratio()
    altseason_idx = get_altseason_index()

    report += f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊\n" if btc_dom is not None else "1️⃣ BTC Dominance: N/A\n"
    report += f"2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰\n"
    report += f"3️⃣ Altcoin Market Cap (est): {_fmt_usd(alt_mc)} 🔷\n"
    report += f"4️⃣ ETH/BTC 7d change: {eth_btc_7d:+.2f}% {'✅' if eth_btc_7d and eth_btc_7d > 3 else '🧊'}\n"
    report += f"5️⃣ DeFi TVL 7d change: {defi_tvl_7d:+.2f}% 🧭\n" if defi_tvl_7d is not None else "5️⃣ DeFi TVL 7d change: N/A\n"
    report += f"6️⃣ Funding Rate avg: {funding_avg:+.6f} {'📈' if funding_avg and funding_avg > 0 else '📉'}\n" if funding_avg is not None else "6️⃣ Funding Rate avg: N/A\n"
    report += f"7️⃣ Stablecoin Netflow (CEX): {netflow_cur if netflow_cur is not None else 'N/A'} M (7d avg: {netflow_avg if netflow_avg is not None else 'N/A'} M)\n"
    report += f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} {'✅' if alt_btc_ratio and alt_btc_ratio > 1.5 else '🧊'}\n" if alt_btc_ratio is not None else "8️⃣ Alt/BTC Volume Ratio: N/A\n"
    report += f"9️⃣ Altcoin Season Index (BC): {altseason_idx:.0f} \n" if altseason_idx is not None else "9️⃣ Altcoin Season Index: N/A\n"

    report += "\n— Tín hiệu kích hoạt:\n"
    report += f"{'✅' if eth_btc_7d and eth_btc_7d > 3 else '❌'} ETH/BTC > +3% (7d)\n"
    report += f"{'✅' if funding_avg and funding_avg > 0 else '❌'} Funding Rate dương\n"
    report += f"{'✅' if netflow_avg and netflow_avg > 0 else '❌'} Stablecoin Netflow 7d avg > 0\n"
    report += f"{'✅' if alt_btc_ratio and alt_btc_ratio > 1.5 else '❌'} Alt/BTC Volume Ratio > 1.5\n"
    report += f"{'✅' if altseason_idx and altseason_idx > 75 else '❌'} Altcoin Season Index > 75\n"

    report += "\n— Cảnh báo Altseason:\n"
    if (eth_btc_7d and eth_btc_7d > 3) and (funding_avg and funding_avg > 0) and (netflow_avg and netflow_avg > 0) and (alt_btc_ratio and alt_btc_ratio > 1.5):
        if altseason_idx and altseason_idx > 75:
            report += "🚀 Full Signal — Altseason rõ ràng!\n"
        else:
            report += "🔥 Early Signal — đang hình thành, cần theo dõi\n"
    else:
        report += "🧊 Chưa đủ điều kiện\n"

    report += "\n— Ghi chú:\n"
    report += "• Stablecoin netflow dương ⇒ dòng tiền sắp giải ngân.\n"
    report += "• Alt/BTC volume ratio > 1.5 ⇒ altcoin volume vượt BTC.\n"
    report += "• Altseason Index > 75 ⇒ xu hướng altseason rõ ràng.\n"
    report += "Code by: HNT"

    return report

# ----------------- Telegram Bot -----------------
async def send_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report = build_report()
    await context.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode=ParseMode.MARKDOWN)

async def daily_task():
    while True:
        now = dt.datetime.now(HCM_TZ)
        if now.hour == 11 and now.minute == 45:
            report = build_report()
            app = ApplicationBuilder().token(BOT_TOKEN).build()
            await app.bot.send_message(chat_id=CHAT_ID, text=report, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(60)
        await asyncio.sleep(20)

def run_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("report", send_report))
    threading.Thread(target=lambda: asyncio.run(daily_task()), daemon=True).start()
    app.run_polling()

# ----------------- Flask Keep-alive -----------------
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    threading.Thread(target=lambda: app_flask.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000))), daemon=True).start()
    run_bot()
