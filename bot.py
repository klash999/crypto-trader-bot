import asyncio
from dataclasses import dataclass
from typing import Optional, List, Tuple
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import CFG
from data_providers import PriceProvider
from indicators import rsi, macd   # إن أردت استخدامها في SignalEngine
from charting import plot_hourly_with_targets
from news import fetch_top_news
from utils import now_local
from trade_binance import BinanceREST
from strategy import SignalEngine
from scanner import best_symbols, Candidate, format_candidates

# --- إعدادات أساسية ---
TP_PCT = CFG["TRADING"]["tp_pct"]
SL_PCT = CFG["TRADING"]["sl_pct"]
TRAIL_PCT = CFG["TRADING"]["trail_pct"]
COOLDOWN = CFG["TRADING"]["cooldown_s"]
AUTO_DAYS = CFG["TRADING"]["auto_shutdown_days"]
FAST_WIN = CFG["TRADING"]["fast_window_s"]
PUMP_LOOKBACK_MIN = CFG["TRADING"]["pump_lookback_min"]
PUMP_PCT = CFG["TRADING"]["pump_pct"]
LOCK_EPS = CFG["TRADING"]["lock_eps"]

AUTOSCAN_ENABLED_DEFAULT = CFG["AUTOSCAN"]["enabled"]
AUTOSCAN_INTERVAL_MIN = CFG["AUTOSCAN"]["interval_min"]

# Binance REST
_br_conf = CFG["BINANCE"]
br = BinanceREST(
    api_key=_br_conf["api_key"],
    api_secret=_br_conf["api_secret"],
    testnet=_br_conf["testnet"],
)
engine = SignalEngine(tp_pct=TP_PCT, sl_pct=SL_PCT)

# حالات عامة
TRADING_ENABLED: bool = False
CURRENT_SYMBOL: str = CFG["CRYPTO_SYMBOL"]
LAST_KLINES: Optional[pd.DataFrame] = None
LAST_MINUTE: Optional[int] = None
SHUTDOWN_AT = now_local() + pd.Timedelta(days=AUTO_DAYS)
LAST_TRADE_TS = now_local() - pd.Timedelta(seconds=COOLDOWN)
LAST_SCAN: Optional[pd.Timestamp] = None
LAST_BEST: List[Candidate] = []
AUTOSCAN_ENABLED: bool = AUTOSCAN_ENABLED_DEFAULT

@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    sl: float
    high: float
    entry_ts: pd.Timestamp
    fast_mode: bool = False  # Fast-Runner mode

OPEN_POS: Optional[Position] = None

# -------------- ميزات /start ديناميكية --------------
def _features() -> Tuple[bool, bool, bool]:
    f = (CFG.get("FEATURES") or {})
    return (bool(f.get("autoscan")), bool(f.get("news")), bool(f.get("futures")))

def build_start_text() -> str:
    autoscan, news, futures = _features()
    lines = ["مرحباً 👋"]
    if futures:
        lines.append("بوت تداول Binance Spot/Futures مع AutoScan + Fast-Runner.")
    else:
        lines.append("بوت تداول Binance Spot (استخدام كامل الرصيد المتاح تلقائياً).")
    lines.append("- AutoScan: يختار أفضل عملة USDT دورياً." if autoscan else "- AutoScan: مُعطّل.")
    lines.append("- Fast-Runner: عند +هدف سريع، نقفل الربح ونواصل بوقف متحرك.")
    lines.append("الأوامر:")
    lines.append("/go — تشغيل (أدمن)")
    lines.append("/stop — إيقاف (أدمن)")
    lines.append("/status — الحالة" + ("" if futures else " + الرصيد"))
    lines.append("/chart — شارت ساعة")
    if autoscan:
        lines.append("/best — أفضل المرشحين")
        lines.append("/autoscan — عرض/ضبط الحالة")
    if news:
        lines.append("/news — أخبار")
    if futures:
        lines.append("/mode — وضع Spot/Futures والرافعة")
        lines.append("/fubalance — رصيد USDT-M Futures")
    lines.append("/help — عرض هذه القائمة")
    return "\n".join(lines)

# -------------- صلاحيات الإدمن --------------
def _is_admin(update: Update) -> bool:
    try:
        u = update.effective_user
        if not u:
            return False
        uid_ok = (u.id == CFG["TELEGRAM_ADMIN"]) if CFG["TELEGRAM_ADMIN"] else False
        uname_env = (CFG.get("TELEGRAM_ADMIN_USERNAME") or "").lstrip("@").lower()
        uname_ok = (u.username or "").lower() == uname_env if uname_env else False
        return uid_ok or uname_ok
    except Exception:
        return False

# -------------- جلب بيانات دقيقة --------------
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

# -------------- AutoScan --------------
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
        # لا نبدّل الرمز إذا لدينا صفقة مفتوحة
        if OPEN_POS is None and top.symbol != CURRENT_SYMBOL:
            old = CURRENT_SYMBOL
            CURRENT_SYMBOL = top.symbol
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=f"🔎 AutoScan: تغيير الرمز {old} → {CURRENT_SYMBOL} (score={top.score:.2f}, 24hΔ={top.change_pct*100:.2f}%)."
            )
    except Exception as e:
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ AutoScan فشل: {e}")

# -------------- التداول الدوري --------------
async def trade_tick(context: ContextTypes.DEFAULT_TYPE):
    global LAST_KLINES, LAST_MINUTE, OPEN_POS, LAST_TRADE_TS, TRADING_ENABLED

    # إيقاف تلقائي بانتهاء المدة
    if CFG["TRADING"]["auto_shutdown_days"] > 0 and now_local() >= SHUTDOWN_AT and TRADING_ENABLED:
        TRADING_ENABLED = False
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text="⏹️ تم إيقاف التداول تلقائياً لانتهاء المدة.")
        return

    if not TRADING_ENABLED:
        return

    symbol = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL

    # تحديث الشموع مرة كل دقيقة
    cur_minute = now_local().minute
    if LAST_KLINES is None or cur_minute != LAST_MINUTE:
        try:
            df, _ = await _fetch_1m(symbol)
            if df is not None and not df.empty:
                LAST_KLINES = df
                LAST_MINUTE = cur_minute
        except Exception as e:
            await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ تعذر تحديث الشموع: {e}")
            return

    if LAST_KLINES is None or LAST_KLINES.empty:
        return

    close = LAST_KLINES["Close"].astype(float)
    price = float(close.iloc[-1])

    # إدارة صفقة مفتوحة
    if OPEN_POS:
        if price > OPEN_POS.high:
            OPEN_POS.high = price

        since = (now_local() - OPEN_POS.entry_ts).total_seconds()
        hit_10 = price >= OPEN_POS.entry * (1 + TP_PCT)

        # تفعيل Fast-Runner عندما يتحقق الهدف سريعاً أو يوجد pump سريع
        if hit_10 and not OPEN_POS.fast_mode and (since <= FAST_WIN or _pump_fast(close)):
            OPEN_POS.fast_mode = True
            lock = OPEN_POS.entry * (1 + TP_PCT - LOCK_EPS)
            if lock > OPEN_POS.sl:
                OPEN_POS.sl = lock
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=(f"🏃‍♂️ Fast-runner ON ({OPEN_POS.symbol}) — قفل ربح +{TP_PCT*100:.0f}%، SL≥{OPEN_POS.sl:.6f}.")
            )

        # وقف متحرك في وضع Fast-Runner
        if OPEN_POS.fast_mode:
            trail = OPEN_POS.high * (1 - max(0.0, TRAIL_PCT))
            new_sl = max(OPEN_POS.sl, trail, OPEN_POS.entry * (1 + TP_PCT - LOCK_EPS))
            if new_sl > OPEN_POS.sl:
                OPEN_POS.sl = new_sl

        # خروج بوقف الخسارة/القفل
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

        # خروج عند تحقق الهدف في الوضع العادي
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

                # استخدام كل الرصيد المتاح تلقائياً (Spot)
                free_usdt = float(br.get_free_usdt())
                min_notional = float(br.symbol_min_notional(CURRENT_SYMBOL))
                # لو الرصيد أقل من الحد الأدنى — رسالة فقط
                if free_usdt < min_notional:
                    await context.bot.send_message(
                        chat_id=CFG["TELEGRAM_ADMIN"],
                        text=f"⚠️ Spot: رصيد غير كافٍ. USDT={free_usdt:.2f}، المطلوب ≥ {min_notional:.2f} USDT."
                    )
                    return

                # استخدم تقريباً كامل الرصيد (تترك سنتات للرسوم)
                use_quote = max(min_notional, free_usdt * 0.999)

                od = br.order_market_buy_quote(CURRENT_SYMBOL, quote_qty=use_quote)
                executed_qty = float(od.get("executedQty", 0.0))
                fills = od.get("fills", [])
                if executed_qty <= 0 and fills:
                    executed_qty = sum(float(f.get("qty", 0)) for f in fills)

                px = float(close.iloc[-1])
                avg_price = px
                if fills:
                    qty_sum = sum(float(f.get("qty", 0)) for f in fills)
                    if qty_sum > 0:
                        avg_price = sum(float(f.get("price", px)) * float(f.get("qty", 0)) for f in fills) / qty_sum

                entry = float(avg_price)
                sl = entry * (1 - SL_PCT)

                OPEN_POS = Position(
                    symbol=CURRENT_SYMBOL, qty=executed_qty, entry=entry, sl=sl,
                    high=entry, entry_ts=now_local(), fast_mode=False
                )
                LAST_TRADE_TS = now_local()

                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"📥 شراء {CURRENT_SYMBOL} Market | Qty={executed_qty} | Entry={entry:.6f} | SL={sl:.6f} | استخدام≈{use_quote:.2f} USDT"
                )
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل أمر الشراء: {e}")
                return

# -------------- أوامر تيليجرام --------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_start_text())

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(build_start_text())

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
    last = float(df['Close'].iloc[-1])
    free_usdt = 0.0
    try:
        free_usdt = float(br.get_free_usdt())
    except Exception:
        pass
    open_line = "لا توجد" if OPEN_POS is None else (
        f"{OPEN_POS.symbol} | Qty={OPEN_POS.qty}, Entry={OPEN_POS.entry:.6f}, SL={OPEN_POS.sl:.6f}, High={OPEN_POS.high:.6f}, Fast={OPEN_POS.fast_mode}"
    )
    await update.message.reply_text(
        f"⏱ {now_local():%Y-%m-%d %H:%M} ({CFG['TZ']})\n"
        f"💱 الرمز الحالي: {sym}\n"
        f"📈 السعر: {last:.6f}\n"
        f"💰 رصيد USDT المتاح (Spot): {free_usdt:.2f}\n"
        f"🤖 التداول: {'نشط' if TRADING_ENABLED else 'متوقف'} | 🔎 AutoScan: {'ON' if AUTOSCAN_ENABLED else 'OFF'}\n"
        f"📦 الصفقة المفتوحة: {open_line}"
    )

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL
    df, _ = await _fetch_1m(sym)
    if df is None or df.empty:
        await update.message.reply_text("لا تتوفر بيانات كافية لعرض الشارت حالياً.")
        return
    last = float(df['Close'].iloc[-1])
    targets = [last*(1+TP_PCT), last*(1+TP_PCT*1.5), last*(1+TP_PCT*2.0)]
    stop = last*(1-SL_PCT)
    df_h = df.resample("60T").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    img = plot_hourly_with_targets(df_h, targets, stop, title=f"{sym} H1 — Targets & Trailing")
    await update.message.reply_photo(photo=img, caption=f"{sym} — المصدر: Binance")

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    autoscan, news, futures = _features()
    if not news:
        await update.message.reply_text("ميزة الأخبار غير مفعّلة.")
        return
    items = await fetch_top_news(limit=6, lang="en")
    await update.message.reply_text("\n\n".join(items))

async def cmd_best(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LAST_BEST
    if not AUTOSCAN_ENABLED:
        await update.message.reply_text("AutoScan مُعطّل. استخدم /autoscan on للتفعيل.")
        return
    if not LAST_BEST:
        try:
            LAST_BEST = best_symbols(br)[:5]
        except Exception as e:
            await update.message.reply_text(f"⚠️ تعذر جلب المرشحين: {e}")
            return
    txt = format_candidates(LAST_BEST, current=(OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL))
    await update.message.reply_text(txt)

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

# أوامر Futures (مغلقة افتراضياً، مجرد رسائل توضيحية إن لم تُفعّل)
async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _, _, futures = _features()
    if not futures:
        await update.message.reply_text("وضع Futures غير مفعّل في الإعدادات.")
        return
    await update.message.reply_text("وضع Futures: غير مُنفّذ هنا (يتطلب trade_binance_futures.py).")

async def cmd_fubalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _, _, futures = _features()
    if not futures:
        await update.message.reply_text("وضع Futures غير مفعّل في الإعدادات.")
        return
    await update.message.reply_text("رصيد Futures: غير مُنفّذ هنا (يتطلب مكوّن Futures).")

# -------------- تشغيل التطبيق --------------
def main():
    token = CFG["TELEGRAM_TOKEN"]
    if not token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في .env")

    app = Application.builder().token(token).build()

    # أوامر أساسية
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("go",     cmd_go))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chart",  cmd_chart))

    autoscan, news, futures = _features()

    if autoscan:
        app.add_handler(CommandHandler("best",     cmd_best))
        app.add_handler(CommandHandler("autoscan", cmd_autoscan))

    if news:
        app.add_handler(CommandHandler("news", cmd_news))

    if futures:
        app.add_handler(CommandHandler("mode",      cmd_mode))
        app.add_handler(CommandHandler("fubalance", cmd_fubalance))

    # جدولة المهام
    if app.job_queue is None:
        print('⚠️ JobQueue غير مفعّل. ثبّت: pip install "python-telegram-bot[job-queue]==21.4"')
    else:
        if autoscan:
            app.job_queue.run_repeating(autoscan_tick, interval=AUTOSCAN_INTERVAL_MIN * 60, first=5)
        app.job_queue.run_repeating(trade_tick, interval=5, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
