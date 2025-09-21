# bot_futures.py
from __future__ import annotations
import asyncio, json, os
from dataclasses import dataclass
from typing import Optional, List

import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import CFG
from fdata_providers import FuturesPriceProvider
from trade_binance_futures import BinanceFuturesREST
from fscanner import fut_best_symbols, fut_format
from fstrategy import CombinedStrategy
from charting import plot_hourly_with_targets
from utils import now_local

# ------------- إعدادات من .env -------------
FUT_TP = float(CFG.get("FUT_TP_PCT", 0.10))
FUT_SL = float(CFG.get("FUT_SL_PCT", 0.02))
FUT_TRAIL = float(CFG.get("FUT_TRAIL_PCT", 0.02))
FUT_COOLDOWN = int(CFG.get("FUT_COOLDOWN_S", 60))

FUT_AUTOSCAN = bool(int(str(CFG.get("FUT_AUTOSCAN", 1))))
FUT_SCAN_MIN = int(CFG.get("FUT_SCAN_INTERVAL_MIN", 60))
FUT_MIN_QVOL = float(CFG.get("FUT_MIN_QVOL_USD", 5_000_000))

FUT_ORDER_MODE = str(CFG.get("FUT_ORDER_MODE", "ALL")).upper()  # ALL / FIXED
FUT_ORDER_USDT = float(CFG.get("FUT_ORDER_USDT", 25))

FUT_DEFAULT_LEV = int(CFG.get("FUT_LEVERAGE", 10))
FUT_DEFAULT_MARGIN = str(CFG.get("FUT_MARGIN_TYPE", "ISOLATED")).upper()  # ISOLATED/CROSSED
FUT_SYMBOL = str(CFG.get("FUT_SYMBOL", "BTCUSDT")).upper()

# خريطة رافعة لكل رمز: من .env (مثال: BTCUSDT:15,ETHUSDT:10)
def _parse_lev_map(s: str | None) -> dict:
    out = {}
    if not s:
        return out
    for part in str(s).split(","):
        if ":" in part:
            sym, x = part.split(":", 1)
            sym = sym.strip().upper()
            try:
                out[sym] = int(x.strip())
            except:
                pass
    return out

FUT_LEV_MAP = _parse_lev_map(CFG.get("FUT_LEVERAGE_MAP"))

ADMIN_ID = int(CFG.get("TELEGRAM_ADMIN", CFG.get("TELEGRAM_ADMIN_ID", 0)) or 0)

# ------------- كائنات -------------
fbr = BinanceFuturesREST(
    api_key=CFG["BINANCE_API_KEY"],
    api_secret=CFG["BINANCE_API_SECRET"],
    testnet=bool(int(str(CFG.get("BINANCE_TESTNET", 0)))),
    timeout=15,
)
strat = CombinedStrategy(min_votes=int(CFG.get("FUT_MIN_VOTES", 2)))

TRADING_ON = False
CURRENT = FUT_SYMBOL
LAST_KLINES: Optional[pd.DataFrame] = None
LAST_MIN: Optional[int] = None
LAST_SCAN_AT: Optional[pd.Timestamp] = None
LAST_BEST = []
LAST_TRADE_TS = now_local() - pd.Timedelta(seconds=FUT_COOLDOWN)

LEV_FILE = "fut_lev_map.json"

@dataclass
class FPos:
    symbol: str
    is_long: bool   # True: LONG, False: SHORT
    entry: float
    qty: float
    sl: float
    extreme: float   # أعلى قمة منذ الدخول لو LONG، أدنى قاع لو SHORT
    entry_ts: pd.Timestamp
    fast: bool = False

OPEN: Optional[FPos] = None

# ------------- رافعة لكل رمز (حفظ/تحميل) -------------
def _load_lev() -> dict:
    # ترتيب الأولوية: JSON محفوظ -> من .env FUT_LEVERAGE_MAP -> افتراضي
    try:
        if os.path.exists(LEV_FILE):
            with open(LEV_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return {k.upper(): int(v) for k, v in d.items()}
    except Exception:
        pass
    return {**FUT_LEV_MAP}

def _save_lev(d: dict):
    try:
        with open(LEV_FILE, "w", encoding="utf-8") as f:
            json.dump({k.upper(): int(v) for k, v in d.items()}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

LEV_MAP = _load_lev()

def _symbol_lev(symbol: str) -> int:
    return int(LEV_MAP.get(symbol.upper(), FUT_DEFAULT_LEV))

# ------------- صلاحيات -------------
def _is_admin(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if ADMIN_ID and u.id == ADMIN_ID:
        return True
    uname_env = (CFG.get("TELEGRAM_ADMIN_USERNAME") or "").lstrip("@").lower()
    return bool(uname_env and (u.username or "").lower() == uname_env)

# ------------- أدوات -------------
async def _fetch_1m(symbol: str):
    prov = FuturesPriceProvider()
    df = await asyncio.to_thread(prov.get_recent_1m, symbol, 900)
    return df, (prov.last_symbol or symbol)

def _order_quote_budget() -> float:
    if FUT_ORDER_MODE == "FIXED":
        return max(5.0, FUT_ORDER_USDT)
    # ALL: كامل الرصيد المتاح
    try:
        bal = float(fbr.get_free_usdt())
    except Exception:
        bal = 0.0
    return max(5.0, bal)

# ------------- AutoScan -------------
async def autoscan_tick(context: ContextTypes.DEFAULT_TYPE):
    global LAST_SCAN_AT, LAST_BEST, CURRENT
    if not FUT_AUTOSCAN:
        return
    if LAST_SCAN_AT and (now_local() - LAST_SCAN_AT).total_seconds() < FUT_SCAN_MIN*60:
        return
    try:
        cands = fut_best_symbols(fbr, min_qv_usd=FUT_MIN_QVOL, limit=20)
        LAST_BEST = cands[:5]
        LAST_SCAN_AT = now_local()
        if OPEN is None and cands:
            top = cands[0].symbol
            if top != CURRENT:
                old = CURRENT
                CURRENT = top
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔎 Futures AutoScan: {old} → {CURRENT}")
    except Exception as e:
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Futures AutoScan فشل: {e}")

# ------------- التداول الدوري -------------
async def trade_tick(context: ContextTypes.DEFAULT_TYPE):
    global LAST_KLINES, LAST_MIN, OPEN, LAST_TRADE_TS, TRADING_ON

    if not TRADING_ON:
        return

    symbol = OPEN.symbol if OPEN else CURRENT

    # تحديث الشموع مرّة بالدقيقة
    cur_min = now_local().minute
    if LAST_KLINES is None or cur_min != LAST_MIN:
        try:
            df, _ = await _fetch_1m(symbol)
            if df is not None and not df.empty:
                LAST_KLINES, LAST_MIN = df, cur_min
        except Exception as e:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ تعذّر تحديث شموع Futures: {e}")
            return

    if LAST_KLINES is None or LAST_KLINES.empty:
        return

    df = LAST_KLINES
    close = df["Close"].astype(float)
    price = float(close.iloc[-1])

    # إدارة المركز المفتوح
    if OPEN:
        if OPEN.is_long:
            # حد أعلى منذ الدخول
            if price > OPEN.extreme:
                OPEN.extreme = price
            hit_tp = price >= OPEN.entry * (1 + FUT_TP)

            # Fast-runner: قفل ربح مبكر + تريل
            if hit_tp and not OPEN.fast:
                OPEN.fast = True
                lock = OPEN.entry * (1 + FUT_TP * 0.8)
                if lock > OPEN.sl:
                    OPEN.sl = lock
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"🏃‍♂️ Fast-runner LONG — SL≥{OPEN.sl:.6f}")

            if OPEN.fast:
                trail = OPEN.extreme * (1 - max(0.0, FUT_TRAIL))
                OPEN.sl = max(OPEN.sl, trail, OPEN.entry * (1 + FUT_TP * 0.5))

            # خروج
            if price <= OPEN.sl:
                try:
                    fbr.close_all_for_symbol(OPEN.symbol)
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔔 خروج SL LONG {OPEN.symbol} @ {price:.6f}")
                except Exception as e:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ فشل إغلاق SL LONG: {e}")
                OPEN = None
                LAST_TRADE_TS = now_local()
                return

            if hit_tp and not OPEN.fast:
                try:
                    fbr.close_all_for_symbol(OPEN.symbol)
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ TP LONG {OPEN.symbol} @ {price:.6f}")
                except Exception as e:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ فشل إغلاق TP LONG: {e}")
                OPEN = None
                LAST_TRADE_TS = now_local()
                return

        else:
            # SHORT: أدنى قاع منذ الدخول
            if price < OPEN.extreme:
                OPEN.extreme = price
            hit_tp = price <= OPEN.entry * (1 - FUT_TP)

            if hit_tp and not OPEN.fast:
                OPEN.fast = True
                # قفل ربح تحت الدخول
                lock = OPEN.entry * (1 - FUT_TP * 0.8)
                if lock < OPEN.sl:
                    OPEN.sl = lock
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"🏃‍♂️ Fast-runner SHORT — SL≤{OPEN.sl:.6f}")

            if OPEN.fast:
                # تريل للـ SHORT: أعلى من أدنى قاع بنسبة
                trail = OPEN.extreme * (1 + max(0.0, FUT_TRAIL))
                # بالنسبة للـ SHORT، نقلّل الـ SL للأسفل (قيمة أصغر) لحجز ربح
                OPEN.sl = min(OPEN.sl, trail, OPEN.entry * (1 - FUT_TP * 0.5))

            # خروج
            if price >= OPEN.sl:
                try:
                    fbr.close_all_for_symbol(OPEN.symbol)
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"🔔 خروج SL SHORT {OPEN.symbol} @ {price:.6f}")
                except Exception as e:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ فشل إغلاق SL SHORT: {e}")
                OPEN = None
                LAST_TRADE_TS = now_local()
                return

            if hit_tp and not OPEN.fast:
                try:
                    fbr.close_all_for_symbol(OPEN.symbol)
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"✅ TP SHORT {OPEN.symbol} @ {price:.6f}")
                except Exception as e:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ فشل إغلاق TP SHORT: {e}")
                OPEN = None
                LAST_TRADE_TS = now_local()
                return

    # تبريد
    if (now_local() - LAST_TRADE_TS).total_seconds() < FUT_COOLDOWN:
        return

    # دخول جديد
    if OPEN is None:
        df, _ = await _fetch_1m(CURRENT)
        if df is None or df.empty:
            return

        # قرار من الإستراتيجية المجمّعة
        d = strat.decide(df)
        signal = d.signal

        if signal in ("LONG", "SHORT"):
            # اضبط الرافعة والنوع
            try:
                fbr.set_margin_type(CURRENT, FUT_DEFAULT_MARGIN)
            except Exception:
                pass
            try:
                fbr.set_leverage(CURRENT, _symbol_lev(CURRENT))
            except Exception:
                pass

            usdt = _order_quote_budget()
            if usdt < 5.0:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ Futures: رصيد غير كافٍ. USDT={usdt:.2f}")
                return

            px = float(df["Close"].iloc[-1])
            try:
                if signal == "LONG":
                    od = fbr.order_market_quote_notional(CURRENT, "BUY", usdt, _symbol_lev(CURRENT), mark_price=px)
                    notional = usdt * _symbol_lev(CURRENT)
                    qty_est = (notional / px) if px > 0 else 0.0
                    sl = px * (1 - FUT_SL)
                    OPEN = FPos(symbol=CURRENT, is_long=True, entry=px, qty=qty_est, sl=sl, extreme=px, entry_ts=now_local(), fast=False)
                    await context.bot.send_message(chat_id=ADMIN_ID,
                        text=f"📥 LONG {CURRENT} Lev x{_symbol_lev(CURRENT)} | Notional≈{notional:.2f} | Entry≈{px:.6f} | SL≈{sl:.6f}\n" +
                             "؛ ".join(d.reasons[-3:]))
                else:
                    od = fbr.order_market_quote_notional(CURRENT, "SELL", usdt, _symbol_lev(CURRENT), mark_price=px)
                    notional = usdt * _symbol_lev(CURRENT)
                    qty_est = (notional / px) if px > 0 else 0.0
                    sl = px * (1 + FUT_SL)
                    OPEN = FPos(symbol=CURRENT, is_long=False, entry=px, qty=qty_est, sl=sl, extreme=px, entry_ts=now_local(), fast=False)
                    await context.bot.send_message(chat_id=ADMIN_ID,
                        text=f"📥 SHORT {CURRENT} Lev x{_symbol_lev(CURRENT)} | Notional≈{notional:.2f} | Entry≈{px:.6f} | SL≈{sl:.6f}\n" +
                             "؛ ".join(d.reasons[-3:]))
                LAST_TRADE_TS = now_local()
            except Exception as e:
                await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ فشل فتح مركز Futures: {e}")

# ------------- أوامر تيليجرام -------------
async def fstart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً 👋\n\n"
        "بوت Binance USDT-M Futures (LONG/SHORT) — إستراتيجيات مجمّعة + رافعة لكل رمز.\n"
        "الأوامر:\n"
        "/fgo — تشغيل (أدمن)\n"
        "/fstop — إيقاف (أدمن)\n"
        "/fstatus — الحالة + الرصيد\n"
        "/fchart — شارت ساعة\n"
        "/fbest — أفضل المرشحين\n"
        "/fpositions — المراكز المفتوحة\n"
        "/fclose — إغلاق مركز الرمز الحالي\n"
        "/fautoscan — حالة AutoScan\n"
        "/flev — عرض خرائط الرافعة\n"
        "/fsetlev <SYMBOL> <X> — تعيين رافعة وحفظها"
    )

async def fgo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("🚫 للأدمن فقط.")
    global TRADING_ON; TRADING_ON = True
    await update.message.reply_text("▶️ تم تشغيل تداول Futures.")

async def fstop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("🚫 للأدمن فقط.")
    global TRADING_ON; TRADING_ON = False
    await update.message.reply_text("⏹️ تم إيقاف تداول Futures.")

async def fstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bal = 0.0
    try: bal = float(fbr.get_free_usdt())
    except Exception: pass
    sym = OPEN.symbol if OPEN else CURRENT
    df, _ = await _fetch_1m(sym)
    last = float(df["Close"].iloc[-1]) if (df is not None and not df.empty) else 0.0
    pos_line = "لا يوجد" if OPEN is None else (
        f"{OPEN.symbol} | {'LONG' if OPEN.is_long else 'SHORT'} | Qty≈{OPEN.qty:.6f} | Entry≈{OPEN.entry:.6f} "
        f"| SL≈{OPEN.sl:.6f} | Extreme≈{OPEN.extreme:.6f} | Fast={OPEN.fast}"
    )
    await update.message.reply_text(
        f"⏱ {now_local():%Y-%m-%d %H:%M} ({CFG['TZ']})\n"
        f"💱 الرمز الحالي: {sym} (Lev x{_symbol_lev(sym)})\n"
        f"📈 السعر: {last:.6f}\n"
        f"💰 Futures Free USDT: {bal:.2f}\n"
        f"🤖 التداول: {'نشط' if TRADING_ON else 'متوقف'} | 🔎 AutoScan: {'ON' if FUT_AUTOSCAN else 'OFF'}\n"
        f"📦 المركز: {pos_line}"
    )

async def fchart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN.symbol if OPEN else CURRENT
    df, _ = await _fetch_1m(sym)
    if df is None or df.empty:
        return await update.message.reply_text("لا تتوفر بيانات كافية للشارت.")
    last = float(df['Close'].iloc[-1])
    # أهداف تقريبية للعرض فقط
    targets = [last*(1+FUT_TP), last*(1+FUT_TP*1.5), last*(1+FUT_TP*2.0)]
    stop = last*(1-FUT_SL)
    df_h = df.resample("60T").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    img = plot_hourly_with_targets(df_h, targets, stop, title=f"{sym} Futures H1 — Targets")
    await update.message.reply_photo(photo=img, caption=f"{sym} — Binance Futures")

async def fbest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global LAST_BEST
    try:
        if not LAST_BEST:
            LAST_BEST = fut_best_symbols(fbr, min_qv_usd=FUT_MIN_QVOL, limit=20)[:5]
        await update.message.reply_text(fut_format(LAST_BEST, CURRENT))
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذر جلب المرشحين: {e}")

async def fpositions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        risks = fbr.get_position_risk(None)
        lines = ["المراكز المفتوحة:"]
        has = False
        for p in risks:
            amt = float(p.get("positionAmt", "0"))
            if abs(amt) > 0:
                has = True
                sym = p.get("symbol")
                entry = float(p.get("entryPrice", "0"))
                upnl = float(p.get("unRealizedProfit", "0"))
                lev = p.get("leverage")
                side = "LONG" if amt > 0 else "SHORT"
                lines.append(f"- {sym} {side} qty={amt:.6f} | entry={entry:.6f} | uPnL={upnl:.2f} | x{lev}")
        if not has:
            lines.append("لا يوجد.")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"⚠️ تعذر قراءة المراكز: {e}")

async def fclose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sym = OPEN.symbol if OPEN else CURRENT
    try:
        fbr.close_all_for_symbol(sym)
        await update.message.reply_text(f"🧹 تم إرسال أمر إغلاق {sym} (reduceOnly).")
    except Exception as e:
        await update.message.reply_text(f"⚠️ فشل الإغلاق: {e}")

async def fautoscan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FUT_AUTOSCAN
    text = (update.message.text or "").lower()
    if " on" in text:
        FUT_AUTOSCAN = True
    elif " off" in text:
        FUT_AUTOSCAN = False
    await update.message.reply_text(f"🔎 Futures AutoScan: {'ON' if FUT_AUTOSCAN else 'OFF'} (كل {FUT_SCAN_MIN} دقيقة)")

async def flev(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = [f"الرافعة الافتراضية: x{FUT_DEFAULT_LEV}", "خريطة الرافعة لكل رمز:"]
    if not LEV_MAP:
        lines.append("(فارغة)")
    else:
        for k, v in sorted(LEV_MAP.items()):
            lines.append(f"- {k}: x{v}")
    await update.message.reply_text("\n".join(lines))

async def fsetlev(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("🚫 للأدمن فقط.")
    parts = (update.message.text or "").split()
    if len(parts) != 3:
        return await update.message.reply_text("الاستخدام: /fsetlev <SYMBOL> <X>\nمثال: /fsetlev BTCUSDT 20")
    sym = parts[1].upper().strip()
    try:
        x = int(parts[2])
        if x < 1 or x > 125: raise ValueError()
    except Exception:
        return await update.message.reply_text("قيمة الرافعة غير صحيحة. المسموح 1..125")
    LEV_MAP[sym] = x
    _save_lev(LEV_MAP)
    await update.message.reply_text(f"تم ضبط رافعة {sym} إلى x{x} (وحفظها).")

def main():
    token = CFG["TELEGRAM_TOKEN"]
    if not token:
        raise SystemExit("ضع TELEGRAM_BOT_TOKEN في .env")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("fstart", fstart))
    app.add_handler(CommandHandler("fgo", fgo))
    app.add_handler(CommandHandler("fstop", fstop))
    app.add_handler(CommandHandler("fstatus", fstatus))
    app.add_handler(CommandHandler("fchart", fchart))
    app.add_handler(CommandHandler("fbest", fbest))
    app.add_handler(CommandHandler("fpositions", fpositions))
    app.add_handler(CommandHandler("fclose", fclose))
    app.add_handler(CommandHandler("fautoscan", fautoscan))
    app.add_handler(CommandHandler("flev", flev))
    app.add_handler(CommandHandler("fsetlev", fsetlev))

    if app.job_queue:
        app.job_queue.run_repeating(autoscan_tick, interval=FUT_SCAN_MIN*60, first=5)
        app.job_queue.run_repeating(trade_tick, interval=5, first=10)
    else:
        print('⚠️ JobQueue غير مفعّل. ثبّت: pip install "python-telegram-bot[job-queue]==21.4"')

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
