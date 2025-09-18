import os
from dotenv import load_dotenv
load_dotenv()

def _as_bool(v: str, default=False):
    if v is None: return default
    return str(v).strip().lower() in ("1","true","yes","on")

CFG = {
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_ADMIN": int(os.getenv("TELEGRAM_ADMIN_ID", "0")),
    "TELEGRAM_ADMIN_USERNAME": os.getenv("TELEGRAM_ADMIN_USERNAME", ""),
    "TZ": os.getenv("TZ", "Asia/Riyadh"),
    "OFFLINE_MODE": _as_bool(os.getenv("OFFLINE_MODE","0")),
    "BINANCE": {
        "api_key": os.getenv("BINANCE_API_KEY", ""),
        "api_secret": os.getenv("BINANCE_API_SECRET", ""),
        "testnet": _as_bool(os.getenv("BINANCE_TESTNET","0")),
    },
    "TRADING": {
        "tp_pct": float(os.getenv("TP_PCT", 0.10)),      # سقف الربح 10%
        "sl_pct": float(os.getenv("SL_PCT", 0.01)),
        "quote_qty": float(os.getenv("ORDER_QUOTE_QTY", 50)),
        "cooldown_s": int(os.getenv("COOLDOWN_S", 60)),
        "auto_shutdown_days": int(os.getenv("AUTO_SHUTDOWN_DAYS", 7)),
    },
    "CRYPTO_SYMBOL": os.getenv("CRYPTO_SYMBOL", "BTCUSDT"),
    "AUTOSCAN": {
        "enabled": _as_bool(os.getenv("AUTOSCAN_ENABLED","1")),
        "interval_min": int(os.getenv("AUTOSCAN_INTERVAL_MIN","60")),
        # مرشحات المسح
        "min_quote_vol_usd": float(os.getenv("AUTOSCAN_MIN_QV","3000000")),  # ≥ 3M USDT
        "exclude_tokens": os.getenv("AUTOSCAN_EXCLUDE","UP,DOWN,BULL,BEAR,TRY,EUR,BRL,FDUSD,BUSD").split(","),
        "max_symbols": int(os.getenv("AUTOSCAN_MAX","200")),
    },
}
