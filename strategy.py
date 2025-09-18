import pandas as pd
from indicators import ema, rsi, macd

class SignalEngine:
    def __init__(self, tp_pct: float, sl_pct: float):
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def entry_long(self, close: pd.Series) -> bool:
        if close is None or len(close) < 60:
            return False
        e50 = ema(close, 50)
        e200 = ema(close, 200) if len(close) >= 200 else ema(close, 100)
        r = rsi(close)
        m, s, _ = macd(close)
        cond = (close.iloc[-1] > e50.iloc[-1] >= e200.iloc[-1]) and (r.iloc[-1] > 55) and (m.iloc[-1] > s.iloc[-1])
        return bool(cond)
