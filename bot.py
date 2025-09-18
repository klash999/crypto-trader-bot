# bot.py
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Tuple

import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import CFG
from data_providers import PriceProvider
from indicators import rsi, macd
from charting import plot_hourly_with_targets
from news import fetch_top_news
from utils import now_local
from trade_binance import BinanceREST
from strategy import SignalEngine
from scanner import best_symbols, format_candidates, Candidate

# ====== إعدادات مع افتراضات آمنة لو ما كانت موجودة في config.py ======
TRADING = (CFG.get("TRADING") or {})
AUTOSCAN = (CFG.get("AUTOSCAN") or {})

TP_PCT = float(TRADING.get("tp_pct", 0.10))              # 10% سقف ربح
SL_PCT = float(TRADING.get("sl_pct", 0.01))              # 1% وقف
TRAIL_PCT = float(TRADING.get("trail_pct", 0.02))        # 2% تتبع افتراضي
QUOTE_QTY = float(TRADING.get("quote_qty", CFG.get("ORDER_QUOTE_QTY", 50.0)))
COOLDOWN = int(TRADING.get("cooldown_s", CFG.get("COOLDOWN_S", 60)))
AUTO_DAYS = int(TRADING.get("auto_shutdown_days", CFG.get("AUTO_SHUTDOWN_DAYS", 7)))
FAST_WIN = int(TRADING.get("fast_window_s", 180))        # نافذة اعتبار "سريع"
PUMP_LOOKBACK_MIN = int(TRADING.get("pump_lookback_min", 5))
PUMP_PCT = float(TRADING.get("pump_pct", 0.10))          # 10% ضخ سريع
LOCK_EPS = float(TRADING.get("lock_eps", 0.005))         # 0.5% هامش قفل

AUTOSCAN_ENABLED = bool(AUTOSCAN.get("enabled", True))
AUTOSCAN_INTERVAL_MIN = int(AUTOSCAN.get("interval_min", 60))  # كل ساعة

# ====== حالة التشغيل ======
br = BinanceREST()
engine = SignalEngine(tp_pct=TP_PCT, sl_pct=SL_PCT)

TRADING_ENABLED: bool = False
CURRENT_SYMBOL: str = str(CFG.get("CRYPTO_SYMBOL", "BTCUSDT")).upper()
LAST_KLINES: Optional[pd.DataFrame] = None
LAST_MINUTE: Optional[int] = None
SHUTDOWN_AT = now_local() + pd.Timedelta(days=AUTO_DAYS) if AUTO_DAYS > 0 else now_local() + pd.Timedelta(days=36500)
LAST_TRADE_TS = now_local() - pd.Timedelta(seconds=COOLDOWN)
LAST_SCAN: Optional[pd.Timestamp] = None
LAST_BEST: List[Candidate] = []

@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    sl: float
    high: float
    entry_ts: pd.Timestamp
    fast_mode: bool = False  # وضع الجري السريع (قفل ربح 10% ومتابعة تتبّع)

OPEN_POS: Optional[Position] = None


# ====== أدوات مساعدة ======
def _is_admin(update: Update) -> bool:
    try:
        u = update.effective_user
        if not u:
            return False
        uid_ok = (u.id == CFG.get("TELEGRAM_ADMIN")) if CFG.get("TELEGRAM_ADMIN") else False
        uname_env = (CFG.get("TELEGRAM_ADMIN_USERNAME") or "").lstrip("@").lower()
        uname_ok = (u.username or "").lower() == uname_env if uname_env else False
        return uid_ok or uname_ok
    except Exception:
        return False

def _ensure_dt_index(df: pd.DataFrame) -> pd.DataFrame:
    """يضمن أن الـ index زمنّي ومؤقّت بشكل صحيح (UTC) قبل أي resample."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        if "OpenTime" in out.columns:
            idx = pd.to_datetime(out["OpenTime"], unit="ms", utc=True, errors="coerce")
        else:
            idx = pd.to_datetime(out.index, utc=True, errors="coerce")
        out.index = idx
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    return out

async def _fetch_1m(symbol: str) -> Tuple[Optional[pd.DataFrame], str]:
    prov = PriceProvider()
    df = await asyncio.to_thread(prov.get_recent_1m, symbol, 900)
    df = _ensure_dt_index(df)
    return df, (prov.last_symbol or symbol)

def _pump_fast(close: pd.Series) -> bool:
    if close is None or len(close) < max(PUMP_LOOKBACK_MIN + 1, 5):
        return False
    prev = float(close.iloc[-1 - PUMP_LOOKBACK_MIN])
    nowv = float(close.iloc[-1])
    return (nowv / prev - 1.0) >= PUMP_PCT

async def _autoscan_once() -> List[Candidate]:
    """إجراء سكان سريع وإرجاع أفضل المرشحين (قد يُرفع استثناء لو الشبكة فشلت)."""
    cands = best_symbols(br)
    return cands

async def _auto_switch_after_trade(context: ContextTypes.DEFAULT_TYPE, prev_symbol: str):
    """بعد الخروج من الصفقة: إن كان AutoScan مفعّل، بدّل للرمز الأعلى مباشرة."""
    global CURRENT_SYMBOL, LAST_BEST, LAST_SCAN
    if not AUTOSCAN_ENABLED:
        return
    try:
        cands = await _autoscan_once()
        if not cands:
            return
        LAST_BEST = cands[:5]
        LAST_SCAN = now_local()
        best = cands[0].symbol
        if best != prev_symbol:
            CURRENT_SYMBOL = best
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=f"🔄 تبديل تلقائي بعد الإغلاق: {prev_symbol} → {CURRENT_SYMBOL} (أفضل مرشح حاليًا)."
            )
    except Exception as e:
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ AutoSwitch فشل: {e}")


# ====== المهام المجدولة ======
async def autoscan_tick(context: ContextTypes.DEFAULT_TYPE):
    """فحص دوري لأفضل الأزواج وتحديث CURRENT_SYMBOL إن لا توجد صفقة."""
    global LAST_SCAN, LAST_BEST, CURRENT_SYMBOL
    if not AUTOSCAN_ENABLED:
        return
    if LAST_SCAN and (now_local() - LAST_SCAN).total_seconds() < AUTOSCAN_INTERVAL_MIN * 60:
        return
    try:
        cands = await _autoscan_once()
        if not cands:
            return
        LAST_BEST = cands[:5]
        top = cands[0]
        LAST_SCAN = now_local()
        if OPEN_POS is None and top.symbol != CURRENT_SYMBOL:
            old = CURRENT_SYMBOL
            CURRENT_SYMBOL = top.symbol
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=f"🔎 AutoScan: تغيير الرمز {old} → {CURRENT_SYMBOL} (score={top.score:.2f}, 24hΔ={top.change_pct*100:.2f}%, vol≈{top.quote_vol:,.0f})."
            )
    except Exception as e:
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ AutoScan فشل: {e}")


async def trade_tick(context: ContextTypes.DEFAULT_TYPE):
    """حلقة التداول: إدارة الصفقة المفتوحة + البحث عن دخول جديد."""
    global LAST_KLINES, LAST_MINUTE, OPEN_POS, LAST_TRADE_TS, TRADING_ENABLED, CURRENT_SYMBOL

    # إيقاف تلقائي عند نهاية المدة
    if AUTO_DAYS > 0 and now_local() >= SHUTDOWN_AT and TRADING_ENABLED:
        TRADING_ENABLED = False
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text="⏹️ تم إيقاف التداول تلقائيًا لانتهاء المدة.")
        return

    if not TRADING_ENABLED:
        return

    symbol = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL

    # تحديث الشموع كل دقيقة فقط لتخفيف الضغط
    cur_minute = now_local().minute
    if LAST_KLINES is None or cur_minute != LAST_MINUTE:
        try:
            df, _ = await _fetch_1m(symbol)
            if df is not None and not df.empty:
                LAST_KLINES = df
                LAST_MINUTE = cur_minute
        except Exception as e:
            await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ تعذّر تحديث الشموع: {e}")
            return

    if LAST_KLINES is None or LAST_KLINES.empty:
        return

    close = LAST_KLINES["Close"].astype(float)
    price = float(close.iloc[-1])

    # إدارة الصفقة المفتوحة
    if OPEN_POS:
        if price > OPEN_POS.high:
            OPEN_POS.high = price

        since = (now_local() - OPEN_POS.entry_ts).total_seconds()
        hit_10 = price >= OPEN_POS.entry * (1 + TP_PCT)

        # تفعيل وضع الجري السريع
        if hit_10 and not OPEN_POS.fast_mode and (since <= FAST_WIN or _pump_fast(close)):
            OPEN_POS.fast_mode = True
            lock = OPEN_POS.entry * (1 + TP_PCT - LOCK_EPS)
            if lock > OPEN_POS.sl:
                OPEN_POS.sl = lock
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=f"🏃‍♂️ Fast-runner ON ({OPEN_POS.symbol}) — قفل ربح ≥ {TP_PCT*100:.0f}%، SL≥{OPEN_POS.sl:.6f} وتتبع لاحق."
            )

        # تتبع وقف ديناميكي في وضع الجري السريع
        if OPEN_POS.fast_mode:
            trail = OPEN_POS.high * (1 - max(0.0, TRAIL_PCT))
            lock_min = OPEN_POS.entry * (1 + TP_PCT - LOCK_EPS)
            new_sl = max(OPEN_POS.sl, trail, lock_min)
            if new_sl > OPEN_POS.sl:
                OPEN_POS.sl = new_sl

        # خروج SL (يتضمن القفل)
        if price <= OPEN_POS.sl:
            prev_sym = OPEN_POS.symbol
            try:
                br.order_market_sell_qty(OPEN_POS.symbol, qty=OPEN_POS.qty)
                mode = "Fast-runner" if OPEN_POS.fast_mode else "Normal"
                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"🔔 خروج {mode} {OPEN_POS.symbol} عند {price:.6f} (SL/قفل)."
                )
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل بيع SL: {e}")
            OPEN_POS = None
            LAST_TRADE_TS = now_local()
            await _auto_switch_after_trade(context, prev_sym)
            return

        # خروج TP (10%) في الوضع العادي
        if hit_10 and not OPEN_POS.fast_mode:
            prev_sym = OPEN_POS.symbol
            try:
                br.order_market_sell_qty(OPEN_POS.symbol, qty=OPEN_POS.qty)
                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"✅ TP تحقق {TP_PCT*100:.0f}% {OPEN_POS.symbol} عند {price:.6f}"
                )
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل بيع TP: {e}")
            OPEN_POS = None
            LAST_TRADE_TS = now_local()
            await _auto_switch_after_trade(context, prev_sym)
            return

    # تبريد بين الصفقات
    if (now_local() - LAST_TRADE_TS).total_seconds() < COOLDOWN:
        return

    # دخول جديد إذا لا توجد صفقة
    if OPEN_POS is None:
        df, _ = await _fetch_1m(CURRENT_SYMBOL)
        if df is None or df.empty:
            return
        close = df["Close"].astype(float)
        if engine.entry_long(close):
            try:
                try:
                    br.sync_time()
                except Exception:
                    pass
                od = br.order_market_buy_quote(CURRENT_SYMBOL, quote_qty=QUOTE_QTY)
                executed_qty = float(od.get("executedQty", 0.0))
                fills = od.get("fills", [])
                if executed_qty <= 0 and fills:
                    executed_qty = sum(float(f.get("qty", 0) or f.get("qty", 0.0)) for f in fills)
                px = float(close.iloc[-1])
                avg_price = px
                if fills:
                    qty_sum = sum(float(f.get("qty", 0) or 0.0) for f in fills)
                    if qty_sum > 0:
                        avg_price = sum(float(f.get("price", px)) * float(f.get("qty", 0) or 0.0) for f in fills) / qty_sum
                entry = float(avg_price)
                sl = entry * (1 - SL_PCT)
                OPEN_POS = Position(
                    symbol=CURRENT_SYMBOL,
                    qty=executed_qty,
                    entry=entry,
                    sl=sl,
                    high=entry,
                    entry_ts=now_local(),
                    fast_mode=False
                )
                LAST_TRADE_TS = now_local()
                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"📥 شراء {CURRENT_SYMBOL} Market | Qty={executed_qty:.6f} | Entry={entry:.6f} | SL={sl:.6f} | Trailing={TRAIL_PCT*100:.1f}%"
                )
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل أمر الشراء: {e}")
                return


# ====== أوامر تيليجرام ======
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً 👋\n\n"
        "بوت تداول Binance Spot مع AutoScan + Fast-Runner.\n"
        "- AutoScan: يختار أفضل عملة USDT كل فترة محددة.\n"
        "- Fast-Runner: عند +10% سريع، نقفل جزء الربح ونواصل بوقف متحرك.\n"
        "الأوامر:\n"
        "/go — تشغيل (أدمن)\n"
        "/stop — إيقاف (أدمن)\n"
        "/status — الحالة\n"
        "/chart — شارت ساعة\n"
        "/news — أخبار\n"
        "/best — أفضل المرشحين الآن\n"
        "/autoscan — عرض/تبديل الحالة: /autoscan on|off\n"
        "/debug — معلومات فنية"
    )

async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("🚫 غير مُصرّح — للأدمن فقط.")
        return
    global TRADING_ENABLED
    TRADING_ENABLED = True
    await update.message.reply_text("▶️ تم تشغيل التداول.")

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("🚫 غير مُصرّح — للأدمن فقط.")
        return
    global TRADING_ENABLED
    TRADING_ENABLED = False
    await update.message.reply_text("⏹️ تم إيقاف التداول.")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL
    df, _ = await _fetch_1m(sym)
    if df is None or df.empty:
        await update.message.reply_text("لا تتوفر بيانات كافية الآن.")
        return
    last = float(df["Close"].iloc[-1])
    open_line = "لا توجد" if OPEN_POS is None else (
        f"{OPEN_POS.symbol} | Qty={OPEN_POS.qty:.6f}, Entry={OPEN_POS.entry:.6f}, SL={OPEN_POS.sl:.6f}, High={OPEN_POS.high:.6f}, Fast={OPEN_POS.fast_mode}"
    )
    await update.message.reply_text(
        f"⏱ {now_local():%Y-%m-%d %H:%M}\n"
        f"💱 الرمز الحالي: {sym}\n"
        f"📈 السعر: {last:.6f}\n"
        f"🤖 التداول: {'نشط' if TRADING_ENABLED else 'متوقف'} | 🔎 AutoScan: {'ON' if AUTOSCAN_ENABLED else 'OFF'}\n"
        f"📦 الصفقة المفتوحة: {open_line}"
    )

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL
    df, _ = await _fetch_1m(sym)
    if df is None or df.empty:
        await update.message.reply_text("لا تتوفر بيانات كافية لعرض الشارت حالياً.")
        return
    last = float(df["Close"].iloc[-1])
    targets = [last * (1 + TP_PCT), last * (1 + TP_PCT * 1.5), last * (1 + TP_PCT * 2.0)]
    stop = last * (1 - SL_PCT)
    # تأكد أن index زمنّي قبل التجميع
    df = _ensure_dt_index(df)
    df_h = df.resample("60T").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    img = plot_hourly_with_targets(df_h, targets, stop, title=f"{sym} H1 — Targets & Trailing")
    await update.message.reply_photo(photo=img, caption=f"{sym} — المصدر: Binance")

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = await fetch_top_news(limit=6, lang="en")
    await update.message.reply_text("\n\n".join(items))

async def cmd_best(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LAST_BEST
    try:
        LAST_BEST = await _autoscan_once()
        text = format_candidates(LAST_BEST[:10], current=(OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL))
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذّر جلب المرشحين: {e}")

async def cmd_autoscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global AUTOSCAN_ENABLED
    text = (update.message.text or "").strip().lower()
    if " on" in text:
        AUTOSCAN_ENABLED = True
        await update.message.reply_text("🔎 AutoScan: ON")
    elif " off" in text:
        AUTOSCAN_ENABLED = False
        await update.message.reply_text("🔎 AutoScan: OFF")
    else:
        await update.message.reply_text(f"🔎 AutoScan: {'ON' if AUTOSCAN_ENABLED else 'OFF'} (interval={AUTOSCAN_INTERVAL_MIN}m)")

async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    df, _ = await _fetch_1m(CURRENT_SYMBOL)
    lines = [
        f"المصدر: Binance",
        f"SymbolNow: {OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL}",
        f"صفوف 1m: {0 if df is None else len(df)}",
    ]
    if df is not None and not df.empty:
        lines.append(f"أول شمعة: {df.index[0]}")
        lines.append(f"آخر شمعة: {df.index[-1]}")
    await update.message.reply_text("\n".join(lines))


# ====== تشغيل التطبيق ======
def main():
    token = CFG.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في .env")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("autoscan", cmd_autoscan))
    app.add_handler(CommandHandler("debug", cmd_debug))

    if app.job_queue is None:
        print('⚠️ JobQueue غير مفعّل. ثبّت: pip install "python-telegram-bot[job-queue]==21.4"')
    else:
        app.job_queue.run_repeating(autoscan_tick, interval=AUTOSCAN_INTERVAL_MIN * 60, first=5)
        app.job_queue.run_repeating(trade_tick, interval=5, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
