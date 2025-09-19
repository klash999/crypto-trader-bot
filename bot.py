# bot.py
# بوت تيليجرام للتداول: Spot/Futures + AutoScan + Fast-Runner + أوامر /mode و /fubalance

import asyncio
from dataclasses import dataclass
from typing import Optional, List

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
from scanner import best_symbols, Candidate

# ----------------- إعدادات من CFG مع قيم افتراضية -----------------

TR = CFG.get("TRADING", {})
TP_PCT             = float(TR.get("tp_pct", 0.10))      # هدف ربح أساسي 10%
SL_PCT             = float(TR.get("sl_pct", 0.01))      # وقف خسارة 1%
TRAIL_PCT          = float(TR.get("trail_pct", 0.02))   # تتبع 2% افتراضي
QUOTE_QTY          = float(TR.get("quote_qty", CFG.get("ORDER_QUOTE_QTY", 50)))
COOLDOWN           = int(TR.get("cooldown_s", CFG.get("COOLDOWN_S", 60)))
AUTO_DAYS          = int(TR.get("auto_shutdown_days", CFG.get("AUTO_SHUTDOWN_DAYS", 7)))
FAST_WIN           = int(TR.get("fast_window_s", 180))  # 3 دقائق افتراضي
PUMP_LOOKBACK_MIN  = int(TR.get("pump_lookback_min", 3))
PUMP_PCT           = float(TR.get("pump_pct", TP_PCT))  # صعود سريع ≥ هدف الربح
LOCK_EPS           = float(TR.get("lock_eps", 0.002))   # قفل ربح أقل هامشياً من الهدف

AUTO = CFG.get("AUTOSCAN", {})
AUTOSCAN_ENABLED   = bool(int(AUTO.get("enabled", 1)))
AUTOSCAN_INTERVAL_MIN = int(AUTO.get("interval_min", 60))

# Binance / Futures
BIN = CFG.get("BINANCE", {})
USE_FUTURES   = bool(int(BIN.get("use_futures", CFG.get("USE_FUTURES", 0))))
LEVERAGE      = int(BIN.get("leverage", 10))
MARGIN_TYPE   = BIN.get("margin_type", "ISOLATED")  # أو "CROSSED"

# رمز افتراضي إلى أن يعمل السكانر
CURRENT_SYMBOL: str = CFG.get("CRYPTO_SYMBOL", "BTCUSDT")

# ----------------- حالات التشغيل -----------------

br = BinanceREST()
engine = SignalEngine(tp_pct=TP_PCT, sl_pct=SL_PCT)

TRADING_ENABLED: bool = False
LAST_KLINES: Optional[pd.DataFrame] = None
LAST_MINUTE: Optional[int] = None
SHUTDOWN_AT = now_local() + pd.Timedelta(days=AUTO_DAYS)
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
    fast_mode: bool = False  # Fast-Runner mode

OPEN_POS: Optional[Position] = None

# ----------------- أدوات مساعدة -----------------

def _is_admin(update: Update) -> bool:
    try:
        u = update.effective_user
        if not u: return False
        uid_ok = (u.id == CFG.get("TELEGRAM_ADMIN")) if CFG.get("TELEGRAM_ADMIN") else False
        uname_env = (CFG.get("TELEGRAM_ADMIN_USERNAME") or "").lstrip("@").lower()
        uname_ok = (u.username or "").lower() == uname_env if uname_env else False
        return uid_ok or uname_ok
    except Exception:
        return False

async def _fetch_1m(symbol: str):
    prov = PriceProvider()
    df = await asyncio.to_thread(prov.get_recent_1m, symbol, 900)
    return df, symbol

def _pump_fast(close: pd.Series) -> bool:
    if close is None or len(close) < max(PUMP_LOOKBACK_MIN+1, 5):
        return False
    prev = float(close.iloc[-1-PUMP_LOOKBACK_MIN])
    nowv = float(close.iloc[-1])
    return (nowv/prev - 1.0) >= PUMP_PCT

# ----------------- AutoScan -----------------

async def autoscan_tick(context: ContextTypes.DEFAULT_TYPE):
    global LAST_SCAN, LAST_BEST, CURRENT_SYMBOL
    if not AUTOSCAN_ENABLED:
        return
    if LAST_SCAN and (now_local() - LAST_SCAN).total_seconds() < AUTOSCAN_INTERVAL_MIN*60:
        return
    try:
        cands = best_symbols(br)
        if not cands:
            return
        LAST_BEST = cands[:5]
        top = cands[0]
        LAST_SCAN = now_local()
        # لا نبدّل الرمز إن لدينا صفقة مفتوحة
        if OPEN_POS is None and top.symbol != CURRENT_SYMBOL:
            old = CURRENT_SYMBOL
            CURRENT_SYMBOL = top.symbol
            await context.bot.send_message(
                chat_id=CFG["TELEGRAM_ADMIN"],
                text=f"🔎 AutoScan: تغيير الرمز {old} → {CURRENT_SYMBOL} (score={top.score:.2f}, 24hΔ={top.change_pct*100:.2f}%)."
            )
    except Exception as e:
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ AutoScan فشل: {e}")

# ----------------- منطق التداول -----------------

async def trade_tick(context: ContextTypes.DEFAULT_TYPE):
    global LAST_KLINES, LAST_MINUTE, OPEN_POS, LAST_TRADE_TS, TRADING_ENABLED

    # إيقاف تلقائي بعد المدة
    if AUTO_DAYS > 0 and now_local() >= SHUTDOWN_AT and TRADING_ENABLED:
        TRADING_ENABLED = False
        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text="⏹️ تم إيقاف التداول تلقائياً لانتهاء المدة.")
        return

    if not TRADING_ENABLED:
        return

    symbol = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL

    # تحديث الشموع دقيقة بدقيقة
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

        # تفعيل Fast-Runner
        if hit_10 and not OPEN_POS.fast_mode and (since <= FAST_WIN or _pump_fast(close)):
            OPEN_POS.fast_mode = True
            lock = OPEN_POS.entry * (1 + TP_PCT - LOCK_EPS)
            if lock > OPEN_POS.sl:
                OPEN_POS.sl = lock
            await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"],
                text=(f"🏃‍♂️ Fast-runner ON ({OPEN_POS.symbol}) — قفل ربح +{TP_PCT*100:.0f}%, SL≥{OPEN_POS.sl:.6f}"))

        # وقف متحرك
        if OPEN_POS.fast_mode:
            trail = OPEN_POS.high * (1 - max(0.0, TRAIL_PCT))
            new_sl = max(OPEN_POS.sl, trail, OPEN_POS.entry*(1 + TP_PCT - LOCK_EPS))
            if new_sl > OPEN_POS.sl:
                OPEN_POS.sl = new_sl

        # خروج SL
        if price <= OPEN_POS.sl:
            try:
                if USE_FUTURES:
                    br.futures_order_market(OPEN_POS.symbol, "SELL", OPEN_POS.qty)
                else:
                    br.order_market_sell_qty(OPEN_POS.symbol, qty=OPEN_POS.qty)
                mode = "Fast-runner" if OPEN_POS.fast_mode else "Normal"
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"🔔 خروج {mode} {OPEN_POS.symbol} عند {price:.6f} | ربح مضمون ≥ {TP_PCT*100:.0f}%")
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل بيع SL: {e}")
            OPEN_POS = None
            LAST_TRADE_TS = now_local()
            return

        # خروج TP (عادي)
        if hit_10 and not OPEN_POS.fast_mode:
            try:
                if USE_FUTURES:
                    br.futures_order_market(OPEN_POS.symbol, "SELL", OPEN_POS.qty)
                else:
                    br.order_market_sell_qty(OPEN_POS.symbol, qty=OPEN_POS.qty)
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"],
                    text=f"✅ TP تحقق {TP_PCT*100:.0f}% {OPEN_POS.symbol} عند {price:.6f}")
            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل بيع TP: {e}")
            OPEN_POS = None
            LAST_TRADE_TS = now_local()
            return

    # تبريد بين الصفقات
    if (now_local() - LAST_TRADE_TS).total_seconds() < COOLDOWN:
        return

    # دخول جديد إذا لا يوجد صفقة
    if OPEN_POS is None:
        df, _ = await _fetch_1m(CURRENT_SYMBOL)
        if df is None or df.empty:
            return
        close = df["Close"].astype(float)
        price = float(close.iloc[-1])
        if engine.entry_long(close):
            try:
                try: br.sync_time()
                except Exception: pass

                if not USE_FUTURES:
                    # ===== Spot =====
                    try:
                        free_usdt = br.balance_free("USDT")
                    except Exception:
                        free_usdt = 0.0

                    f = {}
                    try: f = br.symbol_filters_spot(CURRENT_SYMBOL) or {}
                    except Exception: f = {}
                    min_notional = float(f.get("min_notional", 5.0))

                    desired = float(QUOTE_QTY)
                    eff_quote = min(desired, max(0.0, free_usdt*0.98))
                    if eff_quote < min_notional:
                        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"],
                            text=f"⚠️ Spot: رصيد غير كافٍ. USDT={free_usdt:.2f}، المطلوب ≥ {min_notional:.2f} USDT.")
                        return

                    eff_quote = float(f"{eff_quote:.2f}")
                    od = br.order_market_buy_quote(CURRENT_SYMBOL, quote_qty=eff_quote)

                    executed_qty = float(od.get("executedQty", 0.0) or 0.0)
                    fills = od.get("fills", [])
                    avg_price = price
                    if fills:
                        qty_sum = sum(float(x.get("qty", 0) or 0) for x in fills)
                        if qty_sum > 0:
                            avg_price = sum(float((x.get("price") or price))*float((x.get("qty") or 0)) for x in fills) / qty_sum
                            executed_qty = qty_sum

                    if executed_qty <= 0:
                        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text="⚠️ Spot: لم تُنفّذ أي كمية.")
                        return

                else:
                    # ===== Futures (USDT-M × LEVERAGE) =====
                    try:
                        br.futures_set_margin_type(CURRENT_SYMBOL, MARGIN_TYPE)
                    except Exception:
                        pass
                    try:
                        br.futures_set_leverage(CURRENT_SYMBOL, LEVERAGE)
                    except Exception:
                        pass

                    try:
                        free_usdt = br.futures_balance_usdt()
                    except Exception:
                        free_usdt = 0.0

                    f = {}
                    try: f = br.futures_symbol_filters(CURRENT_SYMBOL) or {}
                    except Exception: f = {}
                    min_notional = float(f.get("min_notional", 5.0))
                    step_size   = float(f.get("step_size", 0.0))

                    desired = float(QUOTE_QTY)
                    eff_quote = min(desired, max(0.0, free_usdt*0.98))
                    raw_qty = (eff_quote * LEVERAGE) / price
                    qty = raw_qty if step_size <= 0 else (int(raw_qty/step_size) * step_size)
                    qty = max(qty, 0.0)

                    if qty*price < min_notional or qty <= 0:
                        await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"],
                            text=(f"⚠️ Futures: الحجم لا يحقق الحد الأدنى. "
                                  f"qty*price={qty*price:.2f} < {min_notional:.2f}. "
                                  f"freeUSDT={free_usdt:.2f}, desiredQuote={desired:.2f}, lev={LEVERAGE}"))
                        return

                    od = br.futures_order_market(CURRENT_SYMBOL, "BUY", qty)
                    executed_qty = qty
                    avg_price = price

                entry = float(avg_price)
                sl = entry * (1 - SL_PCT)
                OPEN_POS = Position(symbol=CURRENT_SYMBOL, qty=float(executed_qty), entry=entry, sl=sl, high=entry, entry_ts=now_local(), fast_mode=False)
                LAST_TRADE_TS = now_local()

                mode_name = f"Futures x{LEVERAGE}" if USE_FUTURES else "Spot"
                await context.bot.send_message(
                    chat_id=CFG["TELEGRAM_ADMIN"],
                    text=(f"📥 شراء {CURRENT_SYMBOL} ({mode_name}) | "
                          f"Qty={executed_qty:g} | Entry={entry:.6f} | SL={sl:.6f} | Trailing={TRAIL_PCT*100:.1f}%")
                )

            except Exception as e:
                await context.bot.send_message(chat_id=CFG["TELEGRAM_ADMIN"], text=f"⚠️ فشل أمر الشراء: {e}")
                return

# ----------------- أوامر تيليجرام -----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً 👋\n\n"
        "بوت تداول Binance Spot/Futures مع AutoScan + Fast-Runner.\n"
        "- AutoScan: يختار أفضل عملة USDT دورياً.\n"
        "- Fast-Runner: عند +هدف سريع، نقفل الربح ونواصل بوقف متحرك.\n"
        "الأوامر:\n"
        "/go — تشغيل (أدمن)\n"
        "/stop — إيقاف (أدمن)\n"
        "/status — الحالة\n"
        "/chart — شارت ساعة\n"
        "/news — أخبار\n"
        "/best — أفضل المرشحين الآن\n"
        "/autoscan — عرض/ضبط الحالة\n"
        "/mode — إظهار وضع Spot/Futures والرافعة\n"
        "/fubalance — رصيد USDT-M Futures\n"
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
    last = float(df['Close'].iloc[-1])
    open_line = "لا توجد" if OPEN_POS is None else (
        f"{OPEN_POS.symbol} | Qty={OPEN_POS.qty}, Entry={OPEN_POS.entry:.6f}, SL={OPEN_POS.sl:.6f}, High={OPEN_POS.high:.6f}, Fast={OPEN_POS.fast_mode}"
    )
    await update.message.reply_text(
        f"⏱ {now_local():%Y-%m-%d %H:%M} ({CFG['TZ']})\n"
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
    last = float(df['Close'].iloc[-1])
    targets = [last*(1+TP_PCT), last*(1+TP_PCT*1.5), last*(1+TP_PCT*2.0)]
    stop = last*(1-SL_PCT)
    df_h = df.resample("60T").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    img = plot_hourly_with_targets(df_h, targets, stop, title=f"{sym} H1 — Targets & Trailing")
    await update.message.reply_photo(photo=img, caption=f"{sym} — المصدر: Binance")

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    items = await fetch_top_news(limit=6, lang="en")
    await update.message.reply_text("\n\n".join(items))

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
        lines.append(f"{i}) {c.symbol} | score={c.score:.2f} | 24hΔ={c.change_pct*100:.2f}% | vol≈{c.quote_vol:,.0f} USDT")
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
    await update.message.reply_text("\n".join(lines))

async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN_POS.symbol if OPEN_POS else CURRENT_SYMBOL
    if USE_FUTURES:
        try:
            pmode = br.futures_get_position_mode()
        except Exception:
            pmode = BIN.get("position_mode", "ONEWAY")
        try:
            lev = br.futures_get_symbol_leverage(sym)
        except Exception:
            lev = None
        lev_str = str(lev) if lev else str(LEVERAGE)
        msg = (
            "⚙️ الوضع: Futures (USDT-M)\n"
            f"📌 Position Mode: {pmode}\n"
            f"🪙 Leverage: x{lev_str}\n"
            f"🏦 Margin: {MARGIN_TYPE}\n"
            f"💱 الرمز الحالي: {sym}\n"
            f"🤖 التداول: {'نشط' if TRADING_ENABLED else 'متوقف'} | 🔎 AutoScan: {'ON' if AUTOSCAN_ENABLED else 'OFF'}"
        )
    else:
        msg = (
            "⚙️ الوضع: Spot\n"
            f"💱 الرمز الحالي: {sym}\n"
            f"🤖 التداول: {'نشط' if TRADING_ENABLED else 'متوقف'} | 🔎 AutoScan: {'ON' if AUTOSCAN_ENABLED else 'OFF'}"
        )
    await update.message.reply_text(msg)

async def cmd_fubalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not USE_FUTURES:
        await update.message.reply_text("الحساب يعمل Spot حالياً. فعّل USE_FUTURES=1 في .env ثم أعد التشغيل.")
        return
    try:
        bal = br.futures_balance_usdt()
        await update.message.reply_text(f"💰 USDT-M Futures availableBalance: {bal:.2f} USDT")
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذّر جلب رصيد Futures: {e}")

# ----------------- التشغيل -----------------

def main():
    token = CFG.get("TELEGRAM_TOKEN", "")
    if not token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في .env")

    app = Application.builder().token(token).build()

    # تسجيل جميع الأوامر (Handlers)
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("go",       cmd_go))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("chart",    cmd_chart))
    app.add_handler(CommandHandler("news",     cmd_news))
    app.add_handler(CommandHandler("best",     cmd_best))
    app.add_handler(CommandHandler("autoscan", cmd_autoscan))
    app.add_handler(CommandHandler("debug",    cmd_debug))
    app.add_handler(CommandHandler("mode",     cmd_mode))
    app.add_handler(CommandHandler("fubalance",cmd_fubalance))

    # JobQueue: AutoScan + التداول
    if app.job_queue is None:
        print('⚠️ JobQueue غير مفعّل. ثبّت: pip install "python-telegram-bot[job-queue]==21.4"')
    else:
        app.job_queue.run_repeating(autoscan_tick, interval=AUTOSCAN_INTERVAL_MIN*60, first=5)
        app.job_queue.run_repeating(trade_tick,    interval=5,                     first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
