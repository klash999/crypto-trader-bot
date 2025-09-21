# fscanner.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Iterable, Optional
import math

DEFAULT_EXCLUDES = ["UP", "DOWN", "BULL", "BEAR", "TRY", "EUR", "BRL", "BUSD", "FDUSD"]

@dataclass
class FCandidate:
    symbol: str
    score: float
    change_pct: float   # 24h Δ (0.05 = +5%)
    quote_vol: float    # تقريباً USDT

def _bad(sym: str, excludes: Iterable[str]) -> bool:
    s = sym.upper().strip()
    if not s.endswith("USDT"):
        return True
    base = s[:-4]
    if len(base) < 2:
        return True
    for t in excludes:
        t = str(t).upper().strip()
        if t and t in s:
            return True
    return False

def _score(change_pct: float, quote_vol: float) -> float:
    lv = math.log10(max(quote_vol, 1.0))
    return 0.65 * change_pct + 0.35 * lv

def fut_best_symbols(br, *, min_qv_usd: float = 5_000_000, excludes: Optional[Iterable[str]] = None, limit: int = 200) -> List[FCandidate]:
    ex = [str(e).upper().strip() for e in (excludes or DEFAULT_EXCLUDES)]
    js = br._get("/fapi/v1/ticker/24hr")
    out: List[FCandidate] = []
    for e in js:
        try:
            sym = str(e.get("symbol", "")).upper()
            if _bad(sym, ex):
                continue
            qv = float(e.get("quoteVolume", 0) or 0.0)
            if qv < min_qv_usd:
                continue
            chg = float(e.get("priceChangePercent", 0) or 0.0) / 100.0
            out.append(FCandidate(symbol=sym, score=_score(chg, qv), change_pct=chg, quote_vol=qv))
        except Exception:
            continue
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:limit]

def fut_format(cands: List[FCandidate], current: Optional[str] = None) -> str:
    if not cands:
        return "لا يوجد مرشحون مناسبون الآن (Futures)."
    lines = ["أفضل مرشحي USDT-M (تصنيف داخلي):"]
    for i, c in enumerate(cands, 1):
        star = " ← الحالي" if current and current.upper() == c.symbol.upper() else ""
        lines.append(f"{i}) {c.symbol} | score={c.score:.2f} | 24hΔ={c.change_pct*100:.2f}% | vol≈{c.quote_vol:,.0f} USDT{star}")
    return "\n".join(lines)
