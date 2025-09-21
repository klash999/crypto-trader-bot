import pandas as pd
from zoneinfo import ZoneInfo
from config import CFG
from trade_binance import BinanceREST

TZ = ZoneInfo(CFG["TZ"])

class PriceProvider:
    """جلب شموع 1m من Binance وتحويلها إلى DataFrame بزمن محلي."""
    def __init__(self):
        self.last_symbol = None
        self.br = BinanceREST(
            api_key=CFG["BINANCE"]["key"],
            api_secret=CFG["BINANCE"]["secret"],
            testnet=CFG["BINANCE"]["testnet"],
        )

    def get_recent_1m(self, symbol: str, limit: int = 900) -> pd.DataFrame:
        self.last_symbol = symbol
        kl = self.br.klines(symbol=symbol, interval="1m", limit=limit)
        if not isinstance(kl, list) or not kl:
            return pd.DataFrame()
        cols = ["OpenTime","Open","High","Low","Close","Volume","CloseTime","QAV","Trades","TBBase","TBQuote","Ignore"]
        df = pd.DataFrame(kl, columns=cols)
        # تحويل الأنواع
        for c in ("Open","High","Low","Close","Volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # فهرس زمني
        dt_utc = pd.to_datetime(df["OpenTime"], unit="ms", utc=True)
        df.index = dt_utc.tz_convert(TZ)
        # أعمدة قياسية
        out = df[["Open","High","Low","Close","Volume"]].copy()
        out = out.dropna()
        return out
