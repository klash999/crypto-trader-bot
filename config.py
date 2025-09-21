import os
from dotenv import load_dotenv

load_dotenv()

def _str(key: str, default: str = "") -> str:
    v = os.getenv(key, default)
    return "" if v is None else str(v).strip()

def _int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return default

def _float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default

def _bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, None)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

def bool_env(key: str, default: bool) -> bool:
    return _bool(key, default)

CFG = {
    # Telegram
    "TELEGRAM_TOKEN": _str("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_ADMIN": _int("TELEGRAM_ADMIN_ID", 0),
    "TELEGRAM_ADMIN_USERNAME": _str("TELEGRAM_ADMIN_USERNAME", ""),

    # Binance
    "BINANCE": {
        "api_key": _str("BINANCE_API_KEY", ""),
        "api_secret": _str("BINANCE_API_SECRET", ""),
        "testnet": _bool("BINANCE_TESTNET", False),
    },

    # عام
    "TZ": _str("TZ", "Asia/Riyadh"),
    "OFFLINE_MODE": _bool("OFFLINE_MODE", False),

    # إعدادات التداول الأساسية
    "CRYPTO_SYMBOL": _str("CRYPTO_SYMBOL", "BTCUSDT"),
    # ملاحظة: البوت سيستخدم كل الرصيد المتاح تلقائياً، لكن نترك قيمة افتراضية احتياطية
    "ORDER_QUOTE_QTY": _float("ORDER_QUOTE_QTY", 50.0),

    "TRADING": {
        "tp_pct": _float("TP_PCT", 0.10),            # هدف 10% كحد أقصى
        "sl_pct": _float("SL_PCT", 0.01),
        "trail_pct": _float("TRAIL_PCT", 0.03),      # وقف متحرك (لـ Fast-Runner)
        "cooldown_s": _int("COOLDOWN_S", 60),
        "auto_shutdown_days": _int("AUTO_SHUTDOWN_DAYS", 7),

        # Fast Runner / Pump logic
        "fast_window_s": _int("FAST_WINDOW_S", 900),           # 15 دقيقة
        "pump_lookback_min": _int("PUMP_LOOKBACK_MIN", 5),
        "pump_pct": _float("PUMP_PCT", 0.05),                  # +5%/X دقائق
        "lock_eps": _float("LOCK_EPS", 0.002),                 # قفل الربح أقل قليلاً من الهدف
    },

    # AutoScan
    "AUTOSCAN": {
        "enabled": _bool("ENABLE_AUTOSCAN", True),
        "interval_min": _int("AUTOSCAN_INTERVAL_MIN", 60),
        "min_quote_vol_usd": _float("AUTOSCAN_MIN_QVOL", 3_000_000.0),
        # رموز تُستبعد من السكانر
        "exclude_tokens": [t.strip() for t in _str("AUTOSCAN_EXCLUDES", "UP,DOWN,BULL,BEAR,TRY,EUR,BRL,FDUSD,BUSD").split(",") if t.strip()],
        "max_symbols": _int("AUTOSCAN_MAX_SYMBOLS", 200),
    },

    # ميزات اختيارية تُغيّر سلوك /start وقائمة الأوامر
    "FEATURES": {
        "autoscan": _bool("ENABLE_AUTOSCAN", True),
        "news": _bool("ENABLE_NEWS", False),
        "futures": _bool("ENABLE_FUTURES", False),
    },
}
