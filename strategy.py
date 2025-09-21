# fstrategy.py
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass
from typing import Optional

# ===================== مؤشرات بسيطة =====================

def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    delta = s.diff()
    up = (delta.where(delta > 0, 0)).rolling(n).mean()
    down = (-delta.where(delta < 0, 0)).rolling(n).mean()
    rs = up / (down.replace(0, 1e-9))
    return 100 - (100 / (1 + rs))

def macd(s: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(s, fast) - ema(s, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()

# ===================== أدوات =====================

def crossed_over(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2 or len(b) < 2: return False
    return a.iloc[-2] < b.iloc[-2] and a.iloc[-1] > b.iloc[-1]

def crossed_under(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2 or len(b) < 2: return False
    return a.iloc[-2] > b.iloc[-2] and a.iloc[-1] < b.iloc[-1]

# ===================== إستراتيجيات =====================

@dataclass
class Decision:
    signal: Optional[str]  # "LONG" / "SHORT" / None
    reasons: list

class StratRSIMACD:
    """
    LONG: RSI يرتد من منطقة تشبّع بيعي + تقاطع MACD لأعلى + فلتر اتجاه EMA200.
    SHORT: RSI يرتد من منطقة تشبّع شرائي + تقاطع MACD لأسفل + فلتر اتجاه EMA200.
    """
    def __init__(self, rsi_per=14, rsi_lo=35, rsi_hi=65):
        self.rsi_per = rsi_per
        self.rsi_lo = rsi_lo
        self.rsi_hi = rsi_hi

    def decide(self, df: pd.DataFrame) -> Decision:
        close = df["Close"]
        if len(close) < 200:
            return Decision(None, ["RSIMACD: بيانات قليلة (<200)"])
        r = rsi(close, self.rsi_per)
        m, s, h = macd(close)
        ema200 = ema(close, 200)
        reasons = []

        long_cond = (
            r.iloc[-2] < self.rsi_lo and r.iloc[-1] > self.rsi_lo and
            crossed_over(m, s) and close.iloc[-1] > ema200.iloc[-1]
        )
        short_cond = (
            r.iloc[-2] > self.rsi_hi and r.iloc[-1] < self.rsi_hi and
            crossed_under(m, s) and close.iloc[-1] < ema200.iloc[-1]
        )
        if long_cond:
            reasons.append("RSIMACD: RSI ارتداد + MACD تقاطع صاعد + فوق EMA200")
            return Decision("LONG", reasons)
        if short_cond:
            reasons.append("RSIMACD: RSI كسر هابط + MACD تقاطع هابط + تحت EMA200")
            return Decision("SHORT", reasons)
        return Decision(None, ["RSIMACD: لا إشارة"])

class StratEMACross:
    """
    LONG: EMA20 > EMA50 مع تقاطع حديث + السعر فوق EMA200
    SHORT: EMA20 < EMA50 مع تقاطع حديث + السعر تحت EMA200
    """
    def __init__(self, fast=20, slow=50, trend=200):
        self.fast = fast
        self.slow = slow
        self.trend = trend

    def decide(self, df: pd.DataFrame) -> Decision:
        close = df["Close"]
        if len(close) < max(self.slow, self.trend) + 2:
            return Decision(None, ["EMACross: بيانات قليلة"])
        e_fast = ema(close, self.fast)
        e_slow = ema(close, self.slow)
        e_trend = ema(close, self.trend)
        reasons = []
        if crossed_over(e_fast, e_slow) and close.iloc[-1] > e_trend.iloc[-1]:
            reasons.append("EMACross: تقاطع صاعد + فوق EMA200")
            return Decision("LONG", reasons)
        if crossed_under(e_fast, e_slow) and close.iloc[-1] < e_trend.iloc[-1]:
            reasons.append("EMACross: تقاطع هابط + تحت EMA200")
            return Decision("SHORT", reasons)
        return Decision(None, ["EMACross: لا إشارة"])

class StratBreakoutATR:
    """
    LONG: اختراق أعلى نطاق (Donchian 20) + ATR كفاية لتجنّب التقطيع
    SHORT: كسر أسفل نطاق (Donchian 20) + ATR كفاية
    """
    def __init__(self, ch_len=20, atr_len=14, min_atr_frac=0.004):
        self.ch_len = ch_len
        self.atr_len = atr_len
        self.min_atr_frac = min_atr_frac  # ATR / Close

    def decide(self, df: pd.DataFrame) -> Decision:
        if len(df) < max(self.ch_len, self.atr_len) + 2:
            return Decision(None, ["BreakoutATR: بيانات قليلة"])
        high = df["High"].rolling(self.ch_len).max()
        low = df["Low"].rolling(self.ch_len).min()
        close = df["Close"]
        _atr = atr(df, self.atr_len)
        atr_ok = (_atr.iloc[-1] / max(1e-9, close.iloc[-1])) >= self.min_atr_frac
        reasons = []
        if close.iloc[-1] > high.iloc[-2] and atr_ok:
            reasons.append("BreakoutATR: اختراق علوي + ATR كافٍ")
            return Decision("LONG", reasons)
        if close.iloc[-1] < low.iloc[-2] and atr_ok:
            reasons.append("BreakoutATR: كسر سفلي + ATR كافٍ")
            return Decision("SHORT", reasons)
        return Decision(None, ["BreakoutATR: لا إشارة أو ATR ضعيف"])

# ===================== مُجمِّع الإستراتيجيات =====================

class CombinedStrategy:
    """
    يُشغّل كل الإستراتيجيات ويطلب حدًا أدنى من الأصوات لنفس الإتجاه.
    min_votes=2 يعني يلزم استراتيجيتان تتفقان على LONG (أو SHORT).
    """
    def __init__(self, min_votes: int = 2):
        self.min_votes = max(1, int(min_votes))
        self.strats = [
            StratRSIMACD(),
            StratEMACross(),
            StratBreakoutATR(),
        ]

    def decide(self, df: pd.DataFrame) -> Decision:
        votes_long = 0
        votes_short = 0
        all_reasons = []
        for st in self.strats:
            d = st.decide(df)
            all_reasons += d.reasons
            if d.signal == "LONG":
                votes_long += 1
            elif d.signal == "SHORT":
                votes_short += 1

        if votes_long >= self.min_votes and votes_long > votes_short:
            all_reasons.append(f"Combined: أصوات LONG = {votes_long}")
            return Decision("LONG", all_reasons)
        if votes_short >= self.min_votes and votes_short > votes_long:
            all_reasons.append(f"Combined: أصوات SHORT = {votes_short}")
            return Decision("SHORT", all_reasons)
        all_reasons.append("Combined: لا إجماع كافٍ")
        return Decision(None, all_reasons)
