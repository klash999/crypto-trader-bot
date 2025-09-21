import os
from dotenv import load_dotenv

load_dotenv()

def _b(v, default=False):
    if v is None: return default
    return str(v).strip() in ("1","true","True","yes","YES","on","ON")

def _f(v, d=0.0):
    try: return float(v)
    except: return d

def _i(v, d=0):
    try: return int(v)
    except: return d

CFG = {
    "TELEGRAM_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    "TELEGRAM_ADMIN": _i(os.getenv("TELEGRAM_ADMIN_ID", "")),
    "TELEGRAM_ADMIN_USERNAME": os.getenv("TELEGRAM_ADMIN_USERNAME", "").strip(),
    "TZ": os.getenv("TZ", "Asia/Riyadh"),
    "OFFLINE": _b(os.getenv("OFFLINE_MODE", "0")),
    "BINANCE": {
        "key": os.getenv("BINANCE_API_KEY", "").strip(),
        "secret": os.getenv("BINANCE_API_SECRET", "").strip(),
        "testnet": _b(os.getenv("BINANCE_TESTNET", "0")),
    },
    "CRYPTO_SYMBOL": os.getenv("CRYPTO_SYMBOL", "BTCUSDT").strip(),
    "ALLOCATION": {
        "mode": os.getenv("ALLOCATION_MODE", "all").strip(),     # all/fixed/percent
        "fixed_quote": _f(os.getenv("ORDER_QUOTE_QTY", "50")),
        "percent": _f(os.getenv("ORDER_PCT", "0.25")),
        "reserve": _f(os.getenv("RESERVE_USDT", "0")),
        "hard_cap": _f(os.getenv("HARD_CAP_USDT", "0")),
    },
    "TRADING": {
        "tp_pct": _f(os.getenv("TP_PCT", "0.10")),
        "sl_pct": _f(os.getenv("SL_PCT", "0.01")),
        "trail_pct": _f(os.getenv("TRAIL_PCT", "0.02")),
        "lock_eps": _f(os.getenv("LOCK_EPS", "0.002")),
        "cooldown_s": _i(os.getenv("COOLDOWN_S", "60")),
        "auto_shutdown_days": _i(os.getenv("AUTO_SHUTDOWN_DAYS", "7")),
        "fast_window_s": _i(os.getenv("FAST_WINDOW_S", "600")),
        "pump_lookback_min": _i(os.getenv("PUMP_LOOKBACK_MIN", "5")),
        "pump_pct": _f(os.getenv("PUMP_PCT", "0.10")),
    },
    "AUTOSCAN": {
        "enabled": _b(os.getenv("AUTOSCAN_ENABLED", "1")),
        "interval_min": _i(os.getenv("AUTOSCAN_INTERVAL_MIN", "60")),
    }
}
