
import os
import requests
import datetime
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# --------- Hàm lấy dữ liệu ---------
def get_btc_dominance():
    url = "https://api.coingecko.com/api/v3/global"
    r = requests.get(url).json()
    return r["data"]["market_cap_percentage"]["btc"]

def get_total_market_cap():
    url = "https://api.coingecko.com/api/v3/global"
    r = requests.get(url).json()
    return r["data"]["total_market_cap"]["usd"]

def get_eth_btc_change_7d():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {"vs_currency": "btc", "ids": "ethereum"}
    r = requests.get(url, params=params).json()
    return r[0]["price_change_percentage_7d_in_currency"]

def get_defi_tvl_change_7d():
    # API mới của DefiLlama
    url = "https://api.llama.fi/overview/defi?excludeTotalChart=true&excludeTotalDataChart=true"
    r = requests.get(url).json()
    chains = r.get("protocols", [])
    if not chains:
        return None
    # tính % thay đổi trung bình
    tvl_changes = [p.get("change_7d", 0) for p in chains if p.get("change_7d") is not None]
    if not tvl_changes:
        return None
    return sum(tvl_changes) / len(tvl_changes)

def get_funding_rate_avg():
    url = "https://api.coinglass.com/api/futures/funding_rates"
    headers = {"coinglassSecret": os.getenv("COINGLASS_API_KEY", "")}
    try:
        r = requests.get(url, headers=headers).json()
        rates = [x["fundingRate"] for x in r.get("data", []) if x.get("fundingRate") is not None]
        if rates:
            return sum(rates) / len(rates)
    except:
        return None
    return None

def get_stablecoin_netflow():
    # Ví dụ giả định dùng API CryptoQuant (cần key)
    return None  # Placeholder

def get_alt_btc_volume_ratio():
    # Dùng dữ liệu khối lượng từ CoinGecko
    url = "https://api.coingecko.com/api/v3/global"
    r = requests.get(url).json()
    total_volume = r["data"]["total_volume"]["usd"]
    btc_volume = total_volume * (r["data"]["market_cap_percentage"]["btc"] / 100)
    alt_volume = total_volume - btc_volume
    return alt_volume / btc_volume if btc_volume > 0 else None

def get_altcoin_season_index():
    try:
        r = requests.get("https://api.blockchaincenter.net/api/altcoin-season-index").json()
        return r.get("seasonIndex")
    except:
        return None

# --------- Hàm tạo báo cáo ---------
def create_report():
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    now = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    btc_dom = get_btc_dominance()
    total_mc = get_total_market_cap()
    eth_btc_7d = get_eth_btc_change_7d()
    defi_tvl_7d = get_defi_tvl_change_7d()
    funding_avg = get_funding_rate_avg()
    alt_btc_ratio = get_alt_btc_volume_ratio()
    alt_season_idx = get_altcoin_season_index()

    report = f"📊 Crypto Daily Report — {now} (GMT+7)\n\n"
    report += f"1) BTC Dominance: {btc_dom:.2f}%\n"
    report += f"2) Total Market Cap: ${total_mc:,.0f}\n"
    report += f"3) ETH/BTC 7d change: {eth_btc_7d:+.2f}%\n"
    report += f"4) DeFi TVL 7d change: {defi_tvl_7d:+.2f}%\n" if defi_tvl_7d is not None else "4) DeFi TVL 7d change: N/A\n"
    report += f"5) Funding avg: {funding_avg:+.6f}\n" if funding_avg is not None else "5) Funding avg: N/A\n"
    report += f"6) Alt/BTC Volume Ratio: {alt_btc_ratio:.2f}\n" if alt_btc_ratio is not None else "6) Alt/BTC Volume Ratio: N/A\n"
    report += f"7) Altcoin Season Index: {alt_season_idx}\n" if alt_season_idx is not None else "7) Altcoin Season Index: N/A\n"

    # Ghi chú cảnh báo
    report += "\n⚠️ Điều kiện cảnh báo Altcoin Season:\n"
    report += "- ETH/BTC tăng > 3% (7d)\n"
    report += "- Funding Rate dương\n"
    report += "- BTC Dominance giảm < 50%\n"
    report += "- Altcoin Season Index > 75\n"
    report += "\nCode by: HNT"

    return report

# --------- Lệnh Telegram ---------
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report = create_report()
    await update.message.reply_text(report)

# --------- Main ---------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("check", check_command))
    app.run_polling()

if __name__ == "__main__":
    main()
