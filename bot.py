import asyncio
from dataclasses import dataclass
from typing import Optional, List
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import logging

from config import CFG
from data_providers import PriceProvider
from charting import plot_hourly_with_targets
from utils import now_local
from trade_binance import BinanceREST

# (لو عندك هذه الملفات أتركها؛ إن لم تكن تستخدم السكانر/الاستراتيجية احذف السطور)
try:
    from strategy import SignalEngine
except Exception:
    class SignalEngine:
        def __init__(self, tp_pct: float, sl_pct: float): pass
        def entry_long(self, close: pd.Series) -> bool:
            # دخول بسيط (EMA/RSi يمكن إضافتها لاحقاً)
            if close is None or len(close) < 20: return False
            return close.iloc[-1] > close.iloc[-5]

try:
    from scanner import best_symbols, Candidate
except Exception:
    class Candidate:  # بديل بسيط إن لم يتوفر ملفك
        def __init__(self, symbol, score=0, change_pct=0, quote_vol=0):
            self.symbol = symbol; self.score = score; self.change_pct = change_pct; self.quote_vol = quote_vol
    def best_symbols(br) -> List[Candidate]:
        return [Candidate(CFG["CRYPTO_SYMBOL"], 0.0, 0.0, 0.0)]

# إعدادات
TP_PCT = CFG["TRADING"]["tp_pct"]
SL_PCT = CFG["TRADING"]["sl_pct"]
TRAIL_PCT = CFG["TRADING"]["trail_pct"]
LOCK_EPS = CFG["TRADING"]["lock_eps"]
COOLDOWN = CFG["TRADING"]["cooldown_s"]
AUTO_DAYS = CFG["TRADING"]["auto_shutdown_days"]
FAST_WIN = CFG["TRADING"]["fast_window_s"]
PUMP_LOOKBACK_MIN = CFG["TRADING"]["pump_lookback_min"]
PUMP_PCT = CFG["TRADING"]["pump_pct"]

AUTOSCAN_ENABLED = CFG["AUTOSCAN"]["enabled"]
AUTOSCAN_INTERVAL_MIN = CFG["AUTOSCAN"]["interval_min"]

# Binance + الأسعار
br = BinanceREST(
    api_key=CFG["BINANCE"]["key"],
    api_secret=CFG["BINANCE"]["secret"],
    testnet=CFG["BINANCE"]["testnet"],
)
engine = SignalEngine(tp_pct=TP_PCT, sl_pct=SL_PCT)

# حالة عامة
TRADING_ENABLED: bool = False
CURRENT_SYMBOL: str = CFG["CRYPTO_SYMBOL"]
LAST_KLINES: Optional[pd.DataFrame] = None
LAST_MINUTE: Optional[int] = None
SHUTDOWN_AT = now_local() + pd.Timedelta(days=AUTO_DAYS)
LAST_TRADE_TS = now_local() - pd.Timedelta(seconds=COOLDOWN)
LAST_SCAN: Optional[pd.Timestamp] = None
LAST_BEST: List = []

# لوجينج
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("crypto-bot")


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    sl: float
    high: float
    entry_ts: pd.Timestamp
    fast_mode: bool = False

OPEN_POS: Optional[Position] = None


def _is_admin(update: Update) -> bool:
    try:
        u = update.effective_user
        if not u: return False
        uid_ok = (u.id == CFG["TELEGRAM_ADMIN"]) if CFG["TELEGRAM_ADMIN"] else False
        uname_env = (CFG.get("TELEGRAM_ADMIN_USERNAME") or "").lstrip("@").lower()
        uname_ok = (u.username or "").lower() == uname_env if uname_env else False
        return uid_ok or uname_ok
    except Exception:
        return False


async def _fetch_1m(symbol: str):
    prov = PriceProvider()
    df = await asyncio.to_thread(prov.get_recent_1m, symbol, 900)
    return df, (prov.last_symbol or symbol)


def _pump_fast(close: pd.Series) -> bool:
    if close is None or len(close) < max(PUMP_LOOKBACK_MIN + 1, 5):
        return False
    prev = float(close.iloc[-1 - PUMP_LOOKBACK_MIN])
    nowv = float(close.iloc[-1])
    return (nowv / prev - 1.0) >= PUMP_PCT


async def autoscan_tick(context: ContextTypes.DEFAULT_TYPE):
    global LAST_SCAN, LAST_BEST, CURRENT_SYMBOL
    if not AUTOSCAN_ENABLED:
        return
    if LAST_SCAN and (now_local() - LAST_SCAN).total_seconds() < AUTOSCAN_INTERVAL_MIN * 60:
        return
    try:
        cands = best_symbols(br)
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
                text=f"🔎 AutoScan: تغيير الرمز {old} → {CURRENT_SYMBOL} (score={getattr(top,'score',0):.2f})."
            )
    except Exception as e:
        log.exception("AutoScan failed")
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ AutoScan فشل: {e}")


async def _determine_quote_all(symbol: str) -> float:
    """حدد قيمة الشراء بالدولار تلقائياً من الرصيد الحر مع احترام minNotional/الاحتياطي/السقف."""
    # الرصيد
    try:
        free = br.get_free_usdt()
    except Exception:
        free = 0.0
    reserve = float(CFG["ALLOCATION"]["reserve"])
    usable = max(0.0, free - reserve)

    # الحد الأدنى
    try:
        min_notional = br.symbol_min_notional(symbol)
    except Exception:
        min_notional = 5.0

    if usable < min_notional:
        return 0.0

    mode = CFG["ALLOCATION"]["mode"]
    hard_cap = float(CFG["ALLOCATION"]["hard_cap"])
    if mode == "fixed":
        quote = max(min_notional, float(CFG["ALLOCATION"]["fixed_quote"]))
        quote = min(quote, usable)
    elif mode == "percent":
        pct = max(0.0, min(1.0, float(CFG["ALLOCATION"]["percent"])))
        quote = max(min_notional, usable * pct)
    else:
        # all
        quote = usable

    if hard_cap > 0:
        quote = min(quote, hard_cap)

    return float(quote)


async def trade_tick(context: ContextTypes.DEFAULT_TYPE):
    """حلقة التداول: إدارة الصفقة المفتوحة + فرص الدخول."""
    global LAST_KLINES, LAST_MINUTE, OPEN_POS, LAST_TRADE_TS, TRADING_ENABLED

    # إيقاف تلقائي
    if CFG["TRADING"]["auto_shutdown_days"] > 0 and now_local() >= SHUTDOWN_AT and TRADING_ENABLED:
        TRADING_ENABLED = False
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text="⏹️ تم إيقاف التداول تلقائياً لانتهاء المدة.")
        return

    if not TRADING_ENABLED:
        return

    symbol = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL

    # تحديث الشموع كل دقيقة
    cur_minute = now_local().minute
    if LAST_KLINES is None or cur_minute != LAST_MINUTE:
        try:
            df, _ = await _fetch_1m(symbol)
            if df is not None and not df.empty:
                LAST_KLINES = df
                LAST_MINUTE = cur_minute
        except Exception as e:
            log.exception("update klines failed")
            await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ تعذر تحديث الشموع: {e}")
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

        # تفعيل Fast-runner
        if hit_10 and not OPEN_POS.fast_mode and (since <= FAST_WIN or _pump_fast(close)):
            OPEN_POS.fast_mode = True
            lock = OPEN_POS.entry * (1 + TP_PCT - LOCK_EPS)
            if lock > OPEN_POS.sl:
                OPEN_POS.sl = lock
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=(f"🏃‍♂️ Fast-runner ON ({OPEN_POS.symbol}) — قفل ربح +{TP_PCT*100:.0f}%، SL≥{OPEN_POS.sl:.6f}.")
            )

        # تتبّع وقف متحرك
        if OPEN_POS.fast_mode:
            trail = OPEN_POS.high * (1 - max(0.0, TRAIL_PCT))
            new_sl = max(OPEN_POS.sl, trail, OPEN_POS.entry*(1 + TP_PCT - LOCK_EPS))
            if new_sl > OPEN_POS.sl:
                OPEN_POS.sl = new_sl

        # خروج SL
        if price <= OPEN_POS.sl:
            try:
                br.order_market_sell_qty(OPEN_POS.symbol, qty=OPEN_POS.qty)
                mode = "Fast-runner" if OPEN_POS.fast_mode else "Normal"
                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"🔔 خروج {mode} {OPEN_POS.symbol} عند {price:.6f} | ربح مضمون ≥ {TP_PCT*100:.0f}%"
                )
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل بيع SL: {e}")
            OPEN_POS = None
            LAST_TRADE_TS = now_local()
            return

        # خروج TP (بدون Fast-runner)
        if hit_10 and not OPEN_POS.fast_mode:
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
            return

    # تبريد بين الصفقات
    if (now_local() - LAST_TRADE_TS).total_seconds() < COOLDOWN:
        return

    # دخول جديد
    if OPEN_POS is None:
        df, _ = await _fetch_1m(CURRENT_SYMBOL)
        if df is None or df.empty:
            return
        close = df["Close"].astype(float)

        if engine.entry_long(close):
            try:
                # قيمة الأمر من الرصيد الحر
                quote_usdt = await _determine_quote_all(CURRENT_SYMBOL)
                if quote_usdt <= 0:
                    try:
                        free = br.get_free_usdt()
                        mn = br.symbol_min_notional(CURRENT_SYMBOL)
                    except Exception:
                        free, mn = 0.0, 5.0
                    await context.bot.send_message(
                        chat_id=CFG["TELEGRAM_ADMIN"],
                        text=(f"⚠️ الرصيد غير كافٍ.\n"
                              f"💰 Free USDT={free:.2f} | MinNotional≈{mn:.2f} | Symbol={CURRENT_SYMBOL}")
                    )
                    return

                try: br.sync_time()
                except Exception: pass

                od = br.order_market_buy_quote(CURRENT_SYMBOL, quote_qty=quote_usdt)

                executed_qty = float(od.get("executedQty", 0.0))
                fills = od.get("fills", [])
                px = float(close.iloc[-1])
                avg_price = px
                if fills:
                    qty_sum = sum(float(f.get("qty", 0)) for f in fills)
                    if qty_sum > 0:
                        avg_price = sum(float(f.get("price", px))*float(f.get("qty", 0)) for f in fills)/qty_sum
                        executed_qty = qty_sum

                entry = float(avg_price)
                sl = entry * (1 - SL_PCT)
                OPEN_POS = Position(symbol=CURRENT_SYMBOL, qty=executed_qty, entry=entry, sl=sl, high=entry, entry_ts=now_local(), fast_mode=False)
                LAST_TRADE_TS = now_local()

                try:
                    free_after = br.get_free_usdt()
                except Exception:
                    free_after = 0.0

                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=(f"📥 شراء {CURRENT_SYMBOL} Market\n"
                          f"🧮 Quote≈{quote_usdt:.2f} USDT | Qty≈{executed_qty}\n"
                          f"↗️ Entry={entry:.6f} | SL={sl:.6f}\n"
                          f"💰 Free USDT بعد التنفيذ: {free_after:.2f}")
                )
            except Exception as e:
                log.exception("buy failed")
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل أمر الشراء: {e}")
                return


# -------- أوامر تيليجرام --------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً 👋\n"
        "بوت تداول Binance Spot (استخدام كامل الرصيد المتاح تلقائياً).\n"
        "الأوامر:\n"
        "/go — تشغيل (أدمن)\n"
        "/stop — إيقاف (أدمن)\n"
        "/status — الحالة + الرصيد\n"
        "/chart — شارت ساعة\n"
        "/best — أفضل المرشحين (لو مفعِّل السكانر)\n"
        "/autoscan — حالة AutoScan\n"
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
    try:
        free = br.get_free_usdt()
    except Exception:
        free = 0.0
    if df is None or df.empty:
        await update.message.reply_text(f"لا تتوفر بيانات حالياً.\n💰 الرصيد الحر USDT: {free:.2f}")
        return
    last = float(df["Close"].iloc[-1])
    open_line = "لا توجد" if OPEN_POS is None else (
        f"{OPEN_POS.symbol} | Qty={OPEN_POS.qty:.8f}, Entry={OPEN_POS.entry:.6f}, SL={OPEN_POS.sl:.6f}, High={OPEN_POS.high:.6f}, Fast={OPEN_POS.fast_mode}"
    )
    await update.message.reply_text(
        f"⏱ {now_local():%Y-%m-%d %H:%M} ({CFG['TZ']})\n"
        f"💱 الرمز الحالي: {sym}\n"
        f"📈 السعر: {last:.6f}\n"
        f"💰 الرصيد الحر USDT (Spot): {free:.2f}\n"
        f"🤖 التداول: {'نشط' if TRADING_ENABLED else 'متوقف'}\n"
        f"📦 الصفقة المفتوحة: {open_line}"
    )

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL
    df, _ = await _fetch_1m(sym)
    if df is None or df.empty:
        await update.message.reply_text("لا تتوفر بيانات كافية لعرض الشارت.")
        return
    last = float(df["Close"].iloc[-1])
    targets = [last*(1+TP_PCT), last*(1+TP_PCT*1.5), last*(1+TP_PCT*2.0)]
    stop = last*(1-SL_PCT)
    df_h = df.resample("60T").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    img = plot_hourly_with_targets(df_h, targets, stop, title=f"{sym} H1 — Targets & Trailing")
    await update.message.reply_photo(photo=img, caption=f"{sym} — المصدر: Binance")

async def cmd_best(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LAST_BEST
    if not LAST_BEST:
        try:
            LAST_BEST = best_symbols(br)[:5]
        except Exception as e:
            await update.message.reply_text(f"⚠️ تعذر جلب المرشحين: {e}")
            return
    lines = [f"أفضل المرشحين (آخر فحص):"]
    for i, c in enumerate(LAST_BEST, start=1):
        lines.append(f"{i}) {c.symbol} | score={getattr(c,'score',0):.2f} | 24hΔ={getattr(c,'change_pct',0)*100:.2f}% | vol≈{getattr(c,'quote_vol',0):,.0f} USDT")
    lines.append(f"🔎 الرمز الحالي: {OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL}")
    await update.message.reply_text("\n".join(lines))

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
    if not _is_admin(update):
        await update.message.reply_text("🚫 غير مُصرّح — للأدمن فقط.")
        return
    df, _ = await _fetch_1m(CURRENT_SYMBOL)
    lines = [f"المصدر: Binance", f"SymbolNow: {CURRENT_SYMBOL}", f"صفوف 1m: {0 if df is None else len(df)}"]
    if df is not None and not df.empty:
        lines.append(f"أول شمعة: {df.index[0]}")
        lines.append(f"آخر شمعة: {df.index[-1]}")
    try:
        free = br.get_free_usdt()
        lines.append(f"Free USDT: {free:.2f}")
    except Exception:
        pass
    await update.message.reply_text("\n".join(lines))


def main():
    token = CFG["TELEGRAM_TOKEN"]
    if not token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في .env")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("best", cmd_best))
    app.add_handler(CommandHandler("autoscan", cmd_autoscan))
    app.add_handler(CommandHandler("debug", cmd_debug))

    if app.job_queue is None:
        log.warning('⚠️ JobQueue غير مفعّل. ثبّت: pip install "python-telegram-bot[job-queue]==21.4"')
    else:
        app.job_queue.run_repeating(autoscan_tick, interval=AUTOSCAN_INTERVAL_MIN*60, first=5)
        app.job_queue.run_repeating(trade_tick, interval=5, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
