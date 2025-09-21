# fdata_providers.py
from __future__ import annotations
import pandas as pd
from zoneinfo import ZoneInfo
from typing import Optional
from config import CFG
from trade_binance_futures import BinanceFuturesREST

class FuturesPriceProvider:
    """جلب شموع 1m من Binance Futures وتحويلها إلى DataFrame بفهرس زمني مضبوط."""
    def __init__(self):
        self.tz = ZoneInfo(CFG["TZ"])
        self.br = BinanceFuturesREST(
            api_key=CFG["BINANCE_API_KEY"],
            api_secret=CFG["BINANCE_API_SECRET"],
            testnet=bool(int(str(CFG.get("BINANCE_TESTNET", 0)))),
            timeout=15,
        )
        self.last_symbol: Optional[str] = None

    def get_recent_1m(self, symbol: str, limit: int = 900) -> pd.DataFrame:
        self.last_symbol = symbol
        kl = self.br.klines(symbol, interval="1m", limit=limit)
        if not kl:
            return pd.DataFrame(columns=["Open","High","Low","Close","Volume"])

        cols = [
            "OpenTime","Open","High","Low","Close","Volume",
            "CloseTime","QuoteAssetVolume","NumberOfTrades",
            "TakerBuyBaseVolume","TakerBuyQuoteVolume","Ignore"
        ]
        df = pd.DataFrame(kl, columns=cols)
        # تحويل أرقام
        for c in ["Open","High","Low","Close","Volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        # OpenTime(ms) -> DatetimeIndex محلي
        ot_ms = pd.to_numeric(df["OpenTime"], errors="coerce")
        ts_utc = pd.to_datetime(ot_ms, unit="ms", utc=True)
        idx = pd.DatetimeIndex(ts_utc.tz_convert(self.tz), name="Date")
        df.index = idx

        # تنظيف أعمدة
        df = df[["Open","High","Low","Close","Volume"]].dropna()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
