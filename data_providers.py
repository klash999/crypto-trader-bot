# data_providers.py
import pandas as pd
from zoneinfo import ZoneInfo
from config import CFG
from trade_binance import BinanceREST

TZ = ZoneInfo(CFG["TZ"])

class PriceProvider:
    def __init__(self):
        self.br = BinanceREST()
        self.last_symbol = None

    def get_recent_1m(self, symbol: str, lookback_minutes: int = 900) -> pd.DataFrame:
        """
        يجلب شموع 1m من Binance ويعيد DataFrame بأعمدة:
        [Open, High, Low, Close, Volume] وفهرس زمني tz-aware على TZ.
        """
        self.last_symbol = symbol

        # استرجاع الشموع
        kl = self.br.klines(symbol, interval="1m", limit=min(lookback_minutes + 5, 1500))
        if not kl or not isinstance(kl, list):
            return pd.DataFrame()

        # أعمدة الشموع حسب واجهة Binance
        cols = [
            "OpenTime","Open","High","Low","Close","Volume","CloseTime",
            "QuoteAssetVolume","Trades","TakerBuyBase","TakerBuyQuote","Ignore"
        ]
        try:
            df = pd.DataFrame(kl, columns=cols)
        except Exception:
            # في حال تغيّر الشكل غير المتوقع
            return pd.DataFrame()

        # تحويل الأرقام
        for c in ("Open","High","Low","Close","Volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")

        # بناء فهرس زمني tz-aware بشكل صحيح
        # مهم: استخدم to_numpy لتحصل على DatetimeIndex مباشرة (بدل Series.tz_convert)
        try:
            idx = pd.to_datetime(df["OpenTime"].to_numpy(dtype="int64"), unit="ms", utc=True).tz_convert(TZ)
        except Exception:
            # مسار احتياطي إن كان النوع float/str
            idx = pd.to_datetime(pd.to_numeric(df["OpenTime"], errors="coerce"), unit="ms", utc=True)
            # إذا لا يزال كـ Series بوقت واعٍ، حوّله إلى Index ثم tz_convert
            idx = pd.DatetimeIndex(idx).tz_convert(TZ)

        df.index = idx
        df = df[["Open","High","Low","Close","Volume"]].dropna()

        # آخر n دقيقة
        if len(df) > lookback_minutes:
            df = df.tail(lookback_minutes)
        return df
