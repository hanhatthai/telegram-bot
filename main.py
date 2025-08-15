import os
import re
import math
import json
import time
import datetime as dt
import pytz
import requests
from typing import Optional, Dict, Any, List, Tuple
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from flask import Flask
import threading
import asyncio

# =========================
# Config & Timezone
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
HCM_TZ = pytz.timezone("Asia/Ho_Chi_Minh")
REPORT_CACHE_PATH = os.getenv("REPORT_CACHE_PATH", "report_cache.json")  # nơi lưu lịch sử để tính 4h/24h
CACHE_KEEP_HOURS = int(os.getenv("CACHE_KEEP_HOURS", "72"))              # lưu lịch sử 72h để an toàn

# =========================
# Helpers (giữ nguyên format tiền)
# =========================
def _fmt_usd(n: Optional[float]) -> str:
    if n is None or (isinstance(n, float) and (math.isnan(n) or math.isinf(n))):
        return "N/A"
    # Giữ nguyên style cũ: <1M có 2 chữ số thập phân, >=1M làm tròn
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

# =========================
# Data fetchers (NGUYÊN VẸN)
# =========================
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
    # API mới
    data = _safe_get_json("https://api.llama.fi/v2/historicalChainTvl")
    if isinstance(data, list):
        try:
            pct = _compute_7d_change_from_series(data)
            if isinstance(pct, (int, float)):
                return pct
        except:
            pass

    # Fallback scrape CSV
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

def get_stablecoin_netflow_cex_usd():
    try:
        js = _safe_get_json("https://whaleportal.com/api/stablecoin-netflows")
        if isinstance(js, list) and js:
            latest = js[-1]
            if "netflow" in latest:
                return float(latest["netflow"]) / 1_000_000
    except:
        pass
    html = _safe_get_text("https://whaleportal.com/stablecoin-netflows")
    if html:
        m = re.search(r'Netflow[^>]*\+?(-?\d+(?:\.\d+)?)\s*M', html)
        if m:
            try:
                return float(m.group(1))
            except:
                pass
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
    try:
        data = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season")
        if data and "index" in data:
            return int(round(float(data["index"])))
    except:
        pass
    try:
        data = _safe_get_json("https://api.blockchaincenter.net/api/altcoin-season-index")
        if data and isinstance(data, dict) and "index" in data:
            return int(round(float(data["index"])))
    except:
        pass
    html = _safe_get_text("https://www.blockchaincenter.net/altcoin-season-index/")
    if html:
        m = re.search(r'font-size:88px;[^>]*>(\d{1,3})<', html)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 100:
                return val
    return None

# =========================
# Cache lịch sử để tính 4h/24h (không thay đổi logic fetch)
# =========================
def _load_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(REPORT_CACHE_PATH):
            with open(REPORT_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {"history": []}  # mỗi phần tử: {"t": epoch, "vals": {...}}

def _save_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(REPORT_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except:
        pass

def _prune_cache(cache: Dict[str, Any], now_ts: int) -> None:
    keep_from = now_ts - CACHE_KEEP_HOURS * 3600
    cache["history"] = [h for h in cache.get("history", []) if h.get("t", 0) >= keep_from]

def _snapshot_current_values() -> Dict[str, Optional[float]]:
    """Chụp lại các giá trị hiện tại để lưu lịch sử. Không đụng đến logic lấy dữ liệu."""
    return {
        "btc_dom": get_btc_dominance(),
        "total_mc": get_total_market_cap_usd(),
        "altcap": get_altcoin_market_cap_est(),
        "ethbtc_7d": get_eth_btc_change_7d_pct(),
        "defi_7d": get_defi_tvl_change_7d_pct(),
        "funding_avg": get_funding_rate_avg(),
        "netflow_m": get_stablecoin_netflow_cex_usd(),
        "alt_btc_ratio": get_alt_btc_spot_volume_ratio(),
        "season_idx": get_altcoin_season_index(),
    }

def _find_value_at(history: List[Dict[str, Any]], key: str, target_ts: int) -> Optional[float]:
    """
    Tìm giá trị gần nhất nhưng KHÔNG vượt sau target_ts (lấy mốc quá khứ gần nhất).
    """
    candidates = [h for h in history if h.get("t", 0) <= target_ts and key in h.get("vals", {}) and h["vals"][key] is not None]
    if not candidates:
        return None
    # lấy phần tử gần target_ts nhất
    best = max(candidates, key=lambda x: x["t"])
    return best["vals"].get(key)

def _pct_change(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None:
        return None
    try:
        if prev == 0:
            return None
        return (cur - prev) / prev * 100.0
    except:
        return None

def _abs_change(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None:
        return None
    try:
        return cur - prev
    except:
        return None

# =========================
# Diễn giải (ngắn gọn, theo ngưỡng)
# =========================
def _brief_trend_pct(pct: Optional[float], pos_text: str, neg_text: str) -> str:
    if pct is None:
        return "N/A"
    if pct >= 10:
        return f"{pct:+.2f}% ⇒ {pos_text} mạnh"
    if pct >= 3:
        return f"{pct:+.2f}% ⇒ {pos_text}"
    if pct > 0:
        return f"{pct:+.2f}% ⇒ {pos_text} nhẹ"
    if pct <= -10:
        return f"{pct:+.2f}% ⇒ {neg_text} mạnh"
    if pct <= -3:
        return f"{pct:+.2f}% ⇒ {neg_text}"
    if pct < 0:
        return f"{pct:+.2f}% ⇒ {neg_text} nhẹ"
    return f"{pct:+.2f}% ⇒ Đi ngang"

def _brief_trend_pp(pp: Optional[float], pos_text: str, neg_text: str) -> str:
    # đổi đơn vị sang % điểm (pp)
    if pp is None:
        return "N/A"
    val = pp  # đã là chênh lệch điểm %
    if val <= -1.0:
        return f"{val:+.2f} điểm ⇒ {pos_text} mạnh"
    if val < 0:
        return f"{val:+.2f} điểm ⇒ {pos_text}"
    if val >= 1.0:
        return f"{val:+.2f} điểm ⇒ {neg_text} mạnh"
    if val > 0:
        return f"{val:+.2f} điểm ⇒ {neg_text}"
    return f"{val:+.2f} điểm ⇒ Đi ngang"

def _comment_season_idx_delta(d: Optional[float]) -> str:
    if d is None:
        return "N/A"
    d_int = int(round(d))
    if d_int >= 15:
        return f"+{d_int} điểm ⇒ Gần đỉnh Altcoin lịch sử"
    if d_int >= 5:
        return f"+{d_int} điểm ⇒ Bứt tốc mạnh"
    if d_int > 0:
        return f"+{d_int} điểm ⇒ Đang tăng"
    if d_int < 0:
        return f"{d_int:+d} điểm ⇒ Suy yếu"
    return f"{d_int:+d} điểm ⇒ Đi ngang"

# =========================
# Báo cáo (NÂNG CẤP FORMAT)
# =========================
def build_report():
    now_dt = dt.datetime.now(HCM_TZ)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M")
    now_ts = int(time.time())

    # 1) Lấy số liệu hiện tại (KHÔNG thay đổi cách gọi)
    btc_dom = get_btc_dominance()
    total_mc = get_total_market_cap_usd()
    altcap = get_altcoin_market_cap_est()
    ethbtc_7d = get_eth_btc_change_7d_pct()
    defi_7d = get_defi_tvl_change_7d_pct()
    funding_avg = get_funding_rate_avg()
    netflow_m = get_stablecoin_netflow_cex_usd()
    alt_btc_ratio = get_alt_btc_spot_volume_ratio()
    season_idx = get_altcoin_season_index()

    # 2) Tải cache, thêm snapshot hiện tại, dọn lịch sử
    cache = _load_cache()
    _prune_cache(cache, now_ts)
    snapshot_vals = {
        "btc_dom": btc_dom,
        "total_mc": total_mc,
        "altcap": altcap,
        "ethbtc_7d": ethbtc_7d,
        "defi_7d": defi_7d,
        "funding_avg": funding_avg,
        "netflow_m": netflow_m,
        "alt_btc_ratio": alt_btc_ratio,
        "season_idx": season_idx,
    }
    cache["history"].append({"t": now_ts, "vals": snapshot_vals})
    _save_cache(cache)

    # 3) Lấy mốc 4h & 24h trước (nếu có)
    ts_4h = now_ts - 4 * 3600
    ts_24h = now_ts - 24 * 3600

    def prev_values(key: str) -> Tuple[Optional[float], Optional[float]]:
        v4 = _find_value_at(cache["history"], key, ts_4h)
        v24 = _find_value_at(cache["history"], key, ts_24h)
        return v4, v24

    # 4) Tính delta theo đặc thù từng chỉ số
    # BTC Dominance: dùng chênh lệch điểm % (pp)
    btc_dom_4h, btc_dom_24h = prev_values("btc_dom")
    btc_dom_d4_pp = _abs_change(btc_dom, btc_dom_4h)
    btc_dom_d24_pp = _abs_change(btc_dom, btc_dom_24h)

    # Total Market Cap & Altcoin Market Cap: dùng % thay đổi
    total_mc_4h, total_mc_24h = prev_values("total_mc")
    total_mc_d4_pct = _pct_change(total_mc, total_mc_4h)
    total_mc_d24_pct = _pct_change(total_mc, total_mc_24h)

    altcap_4h, altcap_24h = prev_values("altcap")
    altcap_d4_pct = _pct_change(altcap, altcap_4h)
    altcap_d24_pct = _pct_change(altcap, altcap_24h)

    # ETH/BTC 7d & DeFi TVL 7d (bản thân đã là % 7d), ta so theo pp (điểm %) ngắn hạn/dài hạn
    ethbtc_4h, ethbtc_24h = prev_values("ethbtc_7d")
    ethbtc_d4_pp = _abs_change(ethbtc_7d, ethbtc_4h)
    ethbtc_d24_pp = _abs_change(ethbtc_7d, ethbtc_24h)

    defi_4h, defi_24h = prev_values("defi_7d")
    defi_d4_pp = _abs_change(defi_7d, defi_4h)
    defi_d24_pp = _abs_change(defi_7d, defi_24h)

    # Funding rate: so theo chênh lệch tuyệt đối
    fund_4h, fund_24h = prev_values("funding_avg")
    fund_d4 = _abs_change(funding_avg, fund_4h)
    fund_d24 = _abs_change(funding_avg, fund_24h)

    # Stablecoin netflow (triệu USD): so theo chênh lệch tuyệt đối (M)
    net_4h, net_24h = prev_values("netflow_m")
    net_d4 = _abs_change(netflow_m, net_4h)
    net_d24 = _abs_change(netflow_m, net_24h)

    # Alt/BTC Volume Ratio: dùng % thay đổi
    ratio_4h, ratio_24h = prev_values("alt_btc_ratio")
    ratio_d4_pct = _pct_change(alt_btc_ratio, ratio_4h)
    ratio_d24_pct = _pct_change(alt_btc_ratio, ratio_24h)

    # Season Index: chênh lệch điểm
    idx_4h, idx_24h = prev_values("season_idx")
    idx_d4 = _abs_change(season_idx, idx_4h)
    idx_d24 = _abs_change(season_idx, idx_24h)

    # 5) Tín hiệu & mức độ (giữ nguyên logic)
    s_ethbtc = bool(ethbtc_7d and ethbtc_7d > 3)
    s_funding = bool(funding_avg and funding_avg > 0)
    s_netflow = bool(netflow_m and netflow_m > 0)
    s_ratio = bool(alt_btc_ratio and alt_btc_ratio > 1.5)
    s_index = bool(season_idx and season_idx > 75)
    count_active = sum([bool(x) for x in [s_ethbtc, s_funding, s_netflow, s_ratio, s_index]])

    level = None
    if count_active >= 4 and s_index:
        level = "Altseason Confirmed"
    elif count_active >= 4:
        level = "Strong Signal"
    elif 2 <= count_active <= 3:
        level = "Early Signal"

    # 6) Diễn giải cho từng chỉ số theo format mới
    lines = [f"📊 <b>Crypto Daily Report</b> — {now_str} (GMT+7)", ""]

    # 1) BTC Dominance
    if btc_dom is not None:
        lines.append(f"1️⃣ BTC Dominance: {btc_dom:.2f}% 🧊")
        lines.append(f"   ↳ Ngắn hạn (4h): " + _brief_trend_pp(
            btc_dom_d4_pp,
            pos_text="Vốn rời BTC sang altcoin",
            neg_text="Vốn quay về BTC"
        ))
        lines.append(f"   ↳ Dài hạn (24h): " + _brief_trend_pp(
            btc_dom_d24_pp,
            pos_text="Xu hướng dịch chuyển sang altcoin",
            neg_text="Ưu tiên BTC quay lại"
        ))
    else:
        lines.append("1️⃣ BTC Dominance: N/A 🧊")

    # 2) Total Market Cap
    if total_mc is not None:
        lines.append(f"\n2️⃣ Total Market Cap: {_fmt_usd(total_mc)} 💰")
        lines.append(f"   ↳ Ngắn hạn (4h): " + _brief_trend_pct(
            total_mc_d4_pct,
            pos_text="Tăng",
            neg_text="Giảm"
        ))
        lines.append(f"   ↳ Dài hạn (24h): " + _brief_trend_pct(
            total_mc_d24_pct,
            pos_text="Thị trường bùng nổ",
            neg_text="Co hẹp quy mô"
        ))
    else:
        lines.append("\n2️⃣ Total Market Cap: N/A 💰")

    # 3) Altcoin Market Cap
    if altcap is not None:
        lines.append(f"\n3️⃣ Altcoin Market Cap (est): {_fmt_usd(altcap)} 🔷")
        lines.append(f"   ↳ Ngắn hạn (4h): " + _brief_trend_pct(
            altcap_d4_pct,
            pos_text="Altcoin hút vốn",
            neg_text="Altcoin bị rút vốn"
        ))
        lines.append(f"   ↳ Dài hạn (24h): " + _brief_trend_pct(
            altcap_d24_pct,
            pos_text="Dòng tiền vào altcoin mạnh",
            neg_text="Dòng tiền rời altcoin"
        ))
    else:
        lines.append("\n3️⃣ Altcoin Market Cap (est): N/A 🔷")

    # 4) ETH/BTC 7d change
    if ethbtc_7d is not None:
        lines.append(f"\n4️⃣ ETH/BTC 7d change: {ethbtc_7d:+.2f}% {'✅' if s_ethbtc else ''}")
        lines.append(f"   ↳ Ngắn hạn (4h): " + _brief_trend_pp(
            ethbtc_d4_pp,
            pos_text="ETH nhỉnh hơn BTC",
            neg_text="ETH kém BTC"
        ))
        lines.append(f"   ↳ Dài hạn (24h): " + _brief_trend_pp(
            ethbtc_d24_pp,
            pos_text="ETH outperform rõ hơn",
            neg_text="ETH suy yếu tương đối"
        ))
    else:
        lines.append("\n4️⃣ ETH/BTC 7d change: N/A")

    # 5) DeFi TVL 7d change
    if defi_7d is not None:
        lines.append(f"\n5️⃣ DeFi TVL 7d change: {defi_7d:+.2f}% 🧭")
        lines.append(f"   ↳ Ngắn hạn (4h): " + _brief_trend_pp(
            defi_d4_pp,
            pos_text="Dòng tiền vào DeFi",
            neg_text="Dòng tiền rời DeFi"
        ))
        lines.append(f"   ↳ Dài hạn (24h): " + _brief_trend_pp(
            defi_d24_pp,
            pos_text="Niềm tin DeFi gia tăng",
            neg_text="Niềm tin DeFi suy yếu"
        ))
    else:
        lines.append("\n5️⃣ DeFi TVL 7d change: N/A 🧭")

    # 6) Funding Rate avg
    if funding_avg is not None:
        lines.append(f"\n6️⃣ Funding Rate avg: {funding_avg:+.6f} {'📈' if funding_avg >= 0 else '📉'}")
        # hiển thị chênh lệch tuyệt đối
        d4 = "N/A" if fund_d4 is None else f"{fund_d4:+.6f}"
        d24 = "N/A" if fund_d24 is None else f"{fund_d24:+.6f}"
        txt4 = "Trader long thêm" if (fund_d4 is not None and fund_d4 > 0) else ("Trader short thêm" if (fund_d4 is not None and fund_d4 < 0) else "Đi ngang")
        txt24 = "Tâm lý lạc quan hơn" if (fund_d24 is not None and fund_d24 > 0) else ("Tâm lý thận trọng hơn" if (fund_d24 is not None and fund_d24 < 0) else "Đi ngang")
        lines.append(f"   ↳ Ngắn hạn (4h): {d4} ⇒ {txt4}")
        lines.append(f"   ↳ Dài hạn (24h): {d24} ⇒ {txt24}")
    else:
        lines.append("\n6️⃣ Funding Rate avg: N/A")

    # 7) Stablecoin Netflow (CEX)
    if netflow_m is not None:
        lines.append(f"\n7️⃣ Stablecoin Netflow (CEX): {netflow_m:+.0f} M {'🔼' if netflow_m >= 0 else '🔽'}")
        d4 = "N/A" if net_d4 is None else f"{net_d4:+.0f} M"
        d24 = "N/A" if net_d24 is None else f"{net_d24:+.0f} M"
        txt4 = "Dòng tiền nóng vào sàn" if (net_d4 != 'N/A' and net_d4[0] == '+') else ("Rút khỏi sàn" if (net_d4 != 'N/A' and net_d4[0] == '-') else "Đi ngang")
        txt24 = "Dòng tiền mới đổ vào" if (net_d24 != 'N/A' and net_d24[0] == '+') else ("Dòng tiền rút ra" if (net_d24 != 'N/A' and net_d24[0] == '-') else "Đi ngang")
        lines.append(f"   ↳ Ngắn hạn (4h): {d4} ⇒ {txt4}")
        lines.append(f"   ↳ Dài hạn (24h): {d24} ⇒ {txt24}")
    else:
        lines.append("\n7️⃣ Stablecoin Netflow (CEX): N/A")

    # 8) Alt/BTC Volume Ratio
    if alt_btc_ratio is not None:
        s_ratio_flag = '✅' if s_ratio else ''
        lines.append(f"\n8️⃣ Alt/BTC Volume Ratio: {alt_btc_ratio:.2f} {s_ratio_flag}")
        lines.append(f"   ↳ Ngắn hạn (4h): " + _brief_trend_pct(
            ratio_d4_pct,
            pos_text="Altcoin áp đảo",
            neg_text="BTC áp đảo"
        ))
        lines.append(f"   ↳ Dài hạn (24h): " + _brief_trend_pct(
            ratio_d24_pct,
            pos_text="Altcoin giao dịch sôi động hơn BTC",
            neg_text="BTC chiếm ưu thế khối lượng"
        ))
    else:
        lines.append("\n8️⃣ Alt/BTC Volume Ratio: N/A")

    # 9) Altcoin Season Index (BC)
    if season_idx is not None:
        lines.append(f"\n9️⃣ Altcoin Season Index (BC): {season_idx} {'🟢' if s_index else ''}")
        lines.append(f"   ↳ Ngắn hạn (4h): {_comment_season_idx_delta(idx_d4)}")
        lines.append(f"   ↳ Dài hạn (24h): {_comment_season_idx_delta(idx_d24)}")
    else:
        lines.append("\n9️⃣ Altcoin Season Index (BC): N/A")

    # 7) Khối xác nhận & cảnh báo (giữ nguyên + mở rộng)
    lines += ["", "📌 <b>Tín hiệu kích hoạt</b>:"]
    lines.append(f"{'✅' if s_ethbtc else '❌'} ETH/BTC > +3% (7d)")
    lines.append(f"{'✅' if s_funding else '❌'} Funding Rate dương")
    lines.append(f"{'✅' if s_netflow else '❌'} Stablecoin Netflow > 0")
    lines.append(f"{'✅' if s_ratio else '❌'} Alt/BTC Volume Ratio > 1.5")
    lines.append(f"{'✅' if s_index else '❌'} Altcoin Season Index > 75")

    # Tóm tắt xác nhận
    lines += ["", "📊 <b>Xác nhận Altcoin Season</b>:"]
    lines.append(f"✅ {count_active}/5 chỉ báo đang tích cực")
    if level:
        lines.append("✅ Ngắn hạn & dài hạn đồng thuận" if (btc_dom_d4_pp is not None and btc_dom_d24_pp is not None) else "ℹ️ Đang tích lũy dữ liệu cho so sánh 4h/24h")

    # Cảnh báo Altseason
    if level:
        lines += ["", "⚠️ <b>Cảnh báo</b>:"]
        if level == "Altseason Confirmed":
            lines.append("🔥 <b>Altseason Confirmed</b> — khả năng trong ~1–2 tuần")
            lines.append("⏱ Ước tính: 3–5 ngày (Độ tin cậy: cao)")
        elif level == "Strong Signal":
            lines.append("🔥 <b>Strong Signal</b> — nhiều điều kiện đã kích hoạt")
            lines.append("⏱ Ước tính: 5–10 ngày (Độ tin cậy: trung bình–cao)")
        elif level == "Early Signal":
            lines.append("🔥 <b>Early Signal</b> — đang hình thành, cần theo dõi")
            lines.append("⏱ Ước tính: 10–21 ngày (Độ tin cậy: trung bình)")

    # Ghi chú chuyển xuống cuối (như yêu cầu)
    lines += [
        "",
        "📝 <i>Ghi chú</i>:",
        "• Funding Rate cao ⇒ có thể dẫn đến điều chỉnh ngắn hạn.",
        "• Dòng Stablecoin vào sàn mạnh ⇒ có thể là tín hiệu pump ngắn hạn, nên theo dõi khối lượng giao dịch.",
        "• Nên chờ xác nhận 7 ngày để lọc nhiễu.",
        "<i>Code by: HNT</i>",
    ]

    return "\n".join(lines)

# =========================
# Telegram Handlers (giữ nguyên)
# =========================
async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_report(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def send_daily(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=CHAT_ID, text=build_report(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# =========================
# Flask + Thread (giữ nguyên)
# =========================
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
    # chạy tự động mỗi ngày 07:00 GMT+7 (giữ nguyên)
    tg_app.job_queue.run_daily(send_daily, time=dt.time(hour=7, tzinfo=HCM_TZ))
    tg_app.run_polling(stop_signals=None)

threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    # Web server cho healthcheck
    app.run(host="0.0.0.0", port=8080)
