# main.py
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

def _compute_7d_change_from_series(series):
    """series: list of dict with date(int, seconds) and tvl(float)"""
    if not series or len(series) < 8:
        return None
    today_ts = int(dt.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    filtered = [p for p in series if p["date"] <= today_ts]
    if len(filtered) < 8:
        return None
    last_val = float(filtered[-1]["tvl"])
    prev_val = float(filtered[-8]["tvl"])
    if prev_val != 0:
        return (last_val - prev_val) / prev_val * 100
    return None

def get_defi_tvl_change_7d_pct():
    # API mới của DeFiLlama (tổng TVL global theo ngày)
    data = _safe_get_json("https://api.llama.fi/v2/historicalChainTvl")
    if isinstance(data, list):
        try:
            pct = _compute_7d_change_from_series(data)
            if isinstance(pct, (int, float)):
                return pct
        except:
            pass

    # Fallback scrape CSV từ trang chủ DefiLlama (nếu có)
    html = _safe_get_text("https://defillama.com/")
    if html:
        m = re.search(r'href="([^"]+\.csv)"', html)
        if m:
            csv_url = m.group(1)
            if csv_url.startswith("/"):
                csv_url = "https://defillama.com" + csv_url
            csv_text = _safe_get_text(csv_url)
            if csv_text:
                rows = [row.strip() for row in csv_text.splitlines() if row.strip()]
                if rows and ("tvl" in rows[0].lower() or "date" in rows[0].lower()):
                    rows = rows[1:]
                series = []
                for row in rows:
                    parts = row.split(",")
                    if len(parts) >= 2:
                        try:
                            ts = int(dt.datetime.fromisoformat(parts[0].replace("Z","")).timestamp())
                            tvl = float(parts[1])
                            series.append({"date": ts, "tvl": tvl})
                        except:
                            continue
                series.sort(key=lambda x: x["date"])
                return _compute_7d_change_from_series(series)
    return None

def get_funding_rate_avg():
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    data = _safe_get_json(url)
    try:
        rates = [float(x["lastFundingRate"]) for x in data if x.get("lastFundingRate") is not None]
        return sum(rates) / len(rates) if rates else None
    except:
        return None

# ---------- NEW: Stablecoin Netflow (CEX) — ưu tiên full-day hôm qua ----------
def _parse_magnitude_to_millions(s: str) -> Optional[float]:
    """
    Chuyển chuỗi dạng '755.7M', '2.3B', '-12.4K', '246,989,608.74' thành triệu USD (float).
    """
    if not s:
        return None
    ss = s.strip().replace(",", "")
    m = re.match(r'^(-?\d+(?:\.\d+)?)([KMB])?$', ss)
    if m:
        val = float(m.group(1))
        suf = m.group(2)
        if not suf:  # số tuyệt đối
            return val / 1_000_000.0
        if suf == 'K':
            return val / 1_000.0
        if suf == 'M':
            return val
        if suf == 'B':
            return val * 1_000.0
    # số tuyệt đối có phần nghìn, không có suffix
    try:
        return float(ss) / 1_000_000.0
    except:
        return None

def get_stablecoin_netflow_cex_usd():
    """
    Cố gắng lấy Stablecoin Netflow (CEX) theo thứ tự:
      1) Trang chart: lấy cột gần nhất KHÔNG có 'Incomplete data' (tức là hôm qua) -> triệu USD
      2) Fallback trang danh sách metrics 'Exchange Flows': tìm hàng 'Exchange Netflow (Total)' -> triệu USD
      3) Thất bại -> None (để hiển thị N/A)
    """
    # 1) Trang chart (ưu tiên lấy full-day hôm qua)
    chart_url = (
        "https://cryptoquant.com/asset/stablecoin/chart/exchange-flows/exchange-netflow-total"
        "?exchange=all_exchange&window=DAY&sma=0&ema=0&priceScale=linear&chartStyle=column"
    )
    html = _safe_get_text(chart_url)
    if html:
        # Tìm các tooltip dạng:
        # "2025 Aug 14 (Incomplete data), UTC ... Exchange Netflow (Total) 246,989,608.74"
        # "2025 Aug 13, UTC ... Exchange Netflow (Total) 432,123,456.78"
        matches = list(re.finditer(
            r'(\d{4}\s+\w+\s+\d{1,2})(?:\s*\(Incomplete data\))?,\s*UTC.*?Exchange Netflow\s*\(Total\)\s*([\-0-9,\.]+)',
            html, flags=re.DOTALL
        ))
        for mobj in reversed(matches):
            date_str = mobj.group(1)
            # Bỏ qua hôm nay nếu có "Incomplete data" đi kèm (không bắt trong group 1 nhưng có thể xác định bằng cách tìm lại đoạn gần)
            span_start = max(0, mobj.start() - 120)
            snippet = html[span_start:mobj.end()]
            if "Incomplete data" in snippet:
                continue
            val_abs_str = mobj.group(2)
            mm = _parse_magnitude_to_millions(val_abs_str)
            if mm is not None:
                return mm

    # 2) Fallback: trang danh sách metrics (Last Value)
    list_url = "https://cryptoquant.com/asset/stablecoin/chart/exchange-flows"
    html2 = _safe_get_text(list_url)
    if html2:
        # Xác định hàng chứa 'Exchange Netflow (Total)' rồi bắt giá trị trong title="...M|B|K"
        row = re.search(r'Exchange Netflow\s*\(Total\).*?</tr>', html2, flags=re.IGNORECASE | re.DOTALL)
        if row:
            # trước hết thử lấy title="...M|B|K"
            m_title = re.search(r'title="([\-0-9\.,]+[KMB]?)"', row.group(0))
            if m_title:
                mm = _parse_magnitude_to_millions(m_title.group(1))
                if mm is not None:
                    return mm
            # nếu không có title, lấy text trong <div class="metric-value-wrapper ..."> ... </div>
            m_text = re.search(r'metric-value-wrapper[^>]*>([\-0-9\.,KMB]+)<', row.group(0))
            if m_text:
                mm = _parse_magnitude_to_millions(m_text.group(1))
                if mm is not None:
                    return mm

    # 3) Thất bại
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
    # API chính
    try:
        data = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season")
        if data and "index" in data:
            return int(round(float(data["index"])))
    except:
        pass
    # API backup
    try:
        data = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season-index")
        if data and isinstance(data, dict) and "index" in data:
            return int(round(float(data["index"])))
    except:
        pass
    # Fallback scrape
    html = _safe_get_text("https://www.blockchaincenter.net/altcoin-season-index/")
    if html:
        m = re.search(r'font-size:88px;[^>]*>(\d{1,3})<', html)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                return val
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
    netflow_m = get_stablecoin_netflow_cex_usd()  # ĐÃ NÂNG CẤP
    alt_btc_ratio = get_alt_btc_spot_volume_ratio()
    season_idx = get_altcoin_season_index()

    s_ethbtc = ethbtc_7d and ethbtc_7d > 3
    s_funding = funding_avg and funding_avg > 0
    s_netflow = netflow_m and netflow_m > 0
    s_ratio = alt_btc_ratio and alt_btc_ratio > 1.5
    s_index = season_idx and season_idx > 75
    count_active = sum([bool(x) for x in [s_ethbtc, s_funding, s_netflow, s_ratio, s_index]])

    level = None
    if count_active >= 4 and s_index:
        level = "Altseason Confirmed"
    elif count_active >= 4:
        level = "Strong Signal"
    elif 2 <= count_active <= 3:
        level = "Early Signal"

    lines = [f"📊 <b>Crypto Daily Report</b> — {now} (GMT+7)", ""]
    lines.append(f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊" if btc_dom is not None else "1️⃣ BTC Dominance: N/A 🧊")
    lines.append(f"2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰" if total_mc is not None else "2️⃣ Total Market Cap: N/A 💰")
    lines.append(f"3️⃣ Altcoin Market Cap (est): {_fmt_usd(altcap)} 🔷" if altcap is not None else "3️⃣ Altcoin Market Cap (est): N/A 🔷")
    lines.append(f"4️⃣ ETH/BTC 7d change: {ethbtc_7d:+.2f}% {'✅' if s_ethbtc else ''}" if ethbtc_7d is not None else "4️⃣ ETH/BTC 7d change: N/A")
    lines.append(f"5️⃣ DeFi TVL 7d change: {defi_7d:+.2f}% 🧭" if defi_7d is not None else "5️⃣ DeFi TVL 7d change: N/A 🧭")
    lines.append(f"6️⃣ Funding Rate avg: {funding_avg:+.6f} {'📈' if funding_avg >= 0 else '📉'}" if funding_avg is not None else "6️⃣ Funding Rate avg: N/A")
    lines.append(f"7️⃣ Stablecoin Netflow (CEX): {netflow_m:+.0f} M {'🔼' if netflow_m >= 0 else '🔽'}" if netflow_m is not None else "7️⃣ Stablecoin Netflow (CEX): N/A")
    lines.append(f"8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} {'✅' if s_ratio else ''}" if alt_btc_ratio is not None else "8️⃣ Alt/BTC Volume Ratio: N/A")
    lines.append(f"9️⃣ Altcoin Season Index (BC): {season_idx} {'🟢' if s_index else ''}" if season_idx is not None else "9️⃣ Altcoin Season Index (BC): N/A")

    lines += ["", "— <b>Tín hiệu kích hoạt</b>:"]
    lines.append(f"{'✅' if s_ethbtc else '❌'} ETH/BTC > +3% (7d)")
    lines.append(f"{'✅' if s_funding else '❌'} Funding Rate dương")
    lines.append(f"{'✅' if s_netflow else '❌'} Stablecoin Netflow > 0")
    lines.append(f"{'✅' if s_ratio else '❌'} Alt/BTC Volume Ratio > 1.5")
    lines.append(f"{'✅' if s_index else '❌'} Altcoin Season Index > 75")

    if level:
        lines += ["", "— <b>Cảnh báo Altseason</b>:"]
        if level == "Altseason Confirmed":
            lines.append("🔥 <b>Altseason Confirmed</b> — khả năng trong ~1–2 tuần")
        elif level == "Strong Signal":
            lines.append("🔥 <b>Strong Signal</b> — nhiều điều kiện đã kích hoạt")
        elif level == "Early Signal":
            lines.append("🔥 <b>Early Signal</b> — đang hình thành, cần theo dõi")

    lines += ["", "— <i>Ghi chú</i>:", "• Stablecoin netflow dương ⇒ dòng tiền sắp giải ngân.",
              "• Alt/BTC volume ratio > 1.5 ⇒ altcoin volume vượt BTC.",
              "• Altseason Index > 75 ⇒ xu hướng altseason rõ ràng.",
              "<i>Code by: HNT</i>"]
    return "\n".join(lines)

# ----------------- Telegram -----------------
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_report(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def send_daily(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHAT_ID, text=build_report(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ----------------- Flask + Thread -----------------
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
