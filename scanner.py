# scanner.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Iterable, Optional
import math

try:
    # إن كانت لديك إعدادات AUTOSCAN في config.py سنستخدمها
    from config import CFG
    _CFG_AUTOSCAN = (CFG or {}).get("AUTOSCAN") or {}
    _DEFAULT_MIN_QV = float(_CFG_AUTOSCAN.get("min_quote_vol_usd", 3_000_000))
    _DEFAULT_EXCLUDES = [s.strip().upper() for s in _CFG_AUTOSCAN.get("exclude_tokens", []) if str(s).strip()]
    _DEFAULT_LIMIT = int(_CFG_AUTOSCAN.get("max_symbols", 200))
except Exception:
    # قيَم افتراضية في حال عدم وجود AUTOSCAN في config.py
    _DEFAULT_MIN_QV = 3_000_000.0
    _DEFAULT_EXCLUDES = ["UP", "DOWN", "BULL", "BEAR", "TRY", "EUR", "BRL", "FDUSD", "BUSD"]
    _DEFAULT_LIMIT = 200


@dataclass
class Candidate:
    symbol: str
    score: float
    change_pct: float  # نسبة التغير 24h (0.05 = +5%)
    quote_vol: float   # سيولة اليوم بالدولار (USDT تقريباً)


def _bad_symbol(sym: str, excludes: Iterable[str]) -> bool:
    """
    استبعاد الأزواج غير USDT، والرموز ذات الرافعة (UP/DOWN/BULL/BEAR) وبعض العملات/العملات الورقية.
    """
    s = sym.upper().strip()
    if not s.endswith("USDT"):
        return True
    base = s[:-4]
    # استبعاد رموز قصيرة جداً
    if len(base) < 2:
        return True
    # استبعاد الكلمات المحظورة
    for tok in excludes:
        tok = str(tok).upper().strip()
        if tok and tok in s:
            return True
    return False


def _score(change_pct: float, quote_vol: float) -> float:
    """
    معادلة تصنيف بسيطة تمزج بين العائد النسبي والسيولة (لوغاريتمية).
    - وزن أعلى للاتجاه الإيجابي
    - السيولة (log10) لتفضيل الأزواج النشطة
    """
    chg = change_pct  # مثال: 0.07 = +7%
    lv = math.log10(max(quote_vol, 1.0))
    # مزيج محافظ
    return 0.65 * chg + 0.35 * lv


def best_symbols(
    br, 
    min_qv_usd: Optional[float] = None, 
    excludes: Optional[Iterable[str]] = None, 
    limit: Optional[int] = None
) -> List[Candidate]:
    """
    يجلب قائمة بكل أزواج Binance ويصنّف أفضل أزواج USDT بناءً على:
      - نسبة التغيّر خلال 24h
      - السيولة (quoteVolume)
    يعيد أعلى المرشحين تنازلياً حسب score.
    """
    min_qv = float(min_qv_usd if min_qv_usd is not None else _DEFAULT_MIN_QV)
    ex = [str(e).upper().strip() for e in (excludes if excludes is not None else _DEFAULT_EXCLUDES)]
    topn = int(limit if limit is not None else _DEFAULT_LIMIT)

    # /api/v3/ticker/24hr يُعيد قائمة قوامها dicts
    js = br._get("/api/v3/ticker/24hr")
    cands: List[Candidate] = []

    for e in js:
        try:
            sym = str(e.get("symbol", "")).upper()
            if _bad_symbol(sym, ex):
                continue

            qv = float(e.get("quoteVolume", 0) or 0.0)  # بالدولار (USDT) تقريباً
            if qv < min_qv:
                continue

            chg_pct = float(e.get("priceChangePercent", 0) or 0.0) / 100.0  # 5 => 0.05
            s = _score(chg_pct, qv)
            cands.append(Candidate(symbol=sym, score=s, change_pct=chg_pct, quote_vol=qv))
        except Exception:
            # تجاهل أي عنصر به بيانات غير متوقعة
            continue

    # ترتيب حسب score تنازلياً
    cands.sort(key=lambda c: c.score, reverse=True)
    return cands[:topn]


def pick_best_symbol(
    br, 
    min_qv_usd: Optional[float] = None, 
    excludes: Optional[Iterable[str]] = None
) -> Optional[str]:
    """
    راجع أفضل مرشح واحد فقط (رمز النصي) أو None إذا لم يوجد.
    """
    cands = best_symbols(br, min_qv_usd=min_qv_usd, excludes=excludes, limit=1)
    return cands[0].symbol if cands else None


def format_candidates(cands: List[Candidate], current: Optional[str] = None) -> str:
    """
    صياغة نص جاهز للإرسال إلى تليجرام.
    """
    if not cands:
        return "لم يُعثر على مرشحين مناسبين الآن."
    lines = ["أفضل المرشحين (تصنيف داخلي):"]
    for i, c in enumerate(cands, start=1):
        star = " ← الحالي" if current and current.upper() == c.symbol.upper() else ""
        lines.append(f"{i}) {c.symbol} | score={c.score:.2f} | 24hΔ={c.change_pct*100:.2f}% | vol≈{c.quote_vol:,.0f} USDT{star}")
    return "\n".join(lines)
