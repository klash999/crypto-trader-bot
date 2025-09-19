# data_providers.py
from __future__ import annotations

from zoneinfo import ZoneInfo
import pandas as pd

from config import CFG
from trade_binance import BinanceREST

TZ = ZoneInfo(CFG.get("TZ", "Asia/Riyadh"))


class PriceProvider:
    """
    موحِّد جلب الأسعار للشموع الدقيقة من Binance Spot.
    يعيد DataFrame مفهرس زمنيًا (TZ-aware).
    """
    def __init__(self):
        self.br = BinanceREST()
        self.last_symbol: str | None = None

    def get_recent_1m(self, symbol: str, limit: int = 900) -> pd.DataFrame:
        """
        يجلب آخر (limit) شمعة 1m من Binance.
        يعالج الفهرس الزمني لضمان DatetimeIndex TZ-aware.
        """
        df = self.br.klines(symbol, interval="1m", limit=limit)
        self.last_symbol = symbol

        if df is None or df.empty:
            return pd.DataFrame()

        # ✅ تأكيد أن الفهرس DatetimeIndex TZ-aware
        if not isinstance(df.index, pd.DatetimeIndex):
            # محاولة بناء الفهرس من OpenTime
            if "OpenTime" in df.columns:
                ts = pd.to_numeric(df["OpenTime"], errors="coerce").astype("Int64")
                ts = ts.fillna(method="ffill").fillna(method="bfill").astype("int64")
                idx_utc = pd.to_datetime(ts, unit="ms", utc=True)
                df.index = pd.DatetimeIndex(idx_utc).tz_convert(TZ)
            # محاولة بديلة من CloseTime
            elif "CloseTime" in df.columns:
                ts = pd.to_numeric(df["CloseTime"], errors="coerce").astype("Int64")
                ts = ts.fillna(method="ffill").fillna(method="bfill").astype("int64")
                idx_utc = pd.to_datetime(ts, unit="ms", utc=True)
                df.index = pd.DatetimeIndex(idx_utc).tz_convert(TZ)
            else:
                # كحل أخير: تحوّل index الحالي إلى DatetimeIndex إن أمكن
                try:
                    df.index = pd.to_datetime(df.index, utc=True)
                    df.index = pd.DatetimeIndex(df.index).tz_convert(TZ)
                except Exception:
                    pass  # سيؤدي لاحقًا إلى فشل عرض الشارت إن لم يكن صالحًا

        # تنظيف وترتيب
        df = df.sort_index()
        df = df.dropna(subset=["Open", "High", "Low", "Close"])

        return df
