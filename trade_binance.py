# trade_binance.py
from __future__ import annotations
import os
import time
import hmac
import math
import hashlib
import logging
from typing import Any, Dict, Optional, List, Tuple

import requests
import pandas as pd
from zoneinfo import ZoneInfo

from config import CFG

log = logging.getLogger("binance")
log.setLevel(logging.INFO)

# =========================
# Utilities
# =========================
def _to_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

# =========================
# REST Client
# =========================
class BinanceREST:
    """
    سبوت فقط (ليس Futures). يدعم:
      - klines() [سبوت]
      - order_market_buy_quote()
      - order_market_sell_qty()
      - ticker_24h_all()
      - get_free() / account_info()
      - exchange_info() + دوال التقريب للفلاتر
      - sync_time()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        testnet: Optional[bool] = None,
        recv_window: int = 5000,
        timeout: int = 15,
    ):
        self.api_key = api_key or os.getenv("BINANCE_API_KEY") or CFG.get("BINANCE_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET") or CFG.get("BINANCE_API_SECRET", "")
        self.testnet = _to_bool(testnet if testnet is not None else (os.getenv("BINANCE_TESTNET") or CFG.get("BINANCE_TESTNET", "0")))
        self.recv_window = int(recv_window)
        self.timeout = int(timeout)

        self._base = "https://testnet.binance.vision" if self.testnet else "https://api.binance.com"
        self._time_offset_ms = 0  # serverTime - local_ms
        self._exinfo_cache: Dict[str, Any] = {}
        self._tz = ZoneInfo(str(CFG.get("TZ", "Asia/Riyadh")))

        if not self.api_key or not self.api_secret:
            log.warning("BINANCE_API_KEY / BINANCE_API_SECRET not set. Public endpoints only.")

    # ------------- low-level http -------------
    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self.api_key} if self.api_key else {}

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if "timestamp" not in params:
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        if "recvWindow" not in params:
            params["recvWindow"] = self.recv_window
        q = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
        sig = hmac.new(self.api_secret.encode("utf-8"), q.encode("utf-8"), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _get(self, path: str, signed: bool = False, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self._base}{path}"
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        r = requests.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        # لو فشل JSON، ارفع استثناء واضح
        try:
            js = r.json()
        except Exception:
            r.raise_for_status()
            raise
        if r.status_code != 200:
            self._raise_api_error(js)
        return js

    def _post(self, path: str, signed: bool = False, data: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self._base}{path}"
        data = dict(data or {})
        if signed:
            data = self._sign(data)
        r = requests.post(url, headers=self._headers(), data=data, timeout=self.timeout)
        try:
            js = r.json()
        except Exception:
            r.raise_for_status()
            raise
        if r.status_code != 200:
            self._raise_api_error(js)
        return js

    def _delete(self, path: str, signed: bool = False, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self._base}{path}"
        params = dict(params or {})
        if signed:
            params = self._sign(params)
        r = requests.delete(url, headers=self._headers(), params=params, timeout=self.timeout)
        try:
            js = r.json()
        except Exception:
            r.raise_for_status()
            raise
        if r.status_code != 200:
            self._raise_api_error(js)
        return js

    def _raise_api_error(self, js: Any):
        """
        يرفع استثناءً مفيدًا من استجابات باينانس.
        مثال: {"code": -2010, "msg": "Account has insufficient balance for requested action."}
        """
        code = None
        msg = None
        if isinstance(js, dict):
            code = js.get("code")
            msg = js.get("msg")
        raise RuntimeError(f"Binance error {code}: {msg}")

    # ------------- time sync -------------
    def server_time(self) -> int:
        js = self._get("/api/v3/time")
        return int(js.get("serverTime", 0))

    def sync_time(self) -> int:
        """
        يحدث إزاحة الوقت الداخلية لاستخدامها في التوقيع.
        """
        local_ms = int(time.time() * 1000)
        st = self.server_time()
        self._time_offset_ms = st - local_ms
        log.info(f"[sync_time] offset_ms={self._time_offset_ms}")
        return self._time_offset_ms

    # ------------- exchange info / filters -------------
    def exchange_info(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        يخزّن ExchangeInfo في كاش داخلي. يعيد dict لرمز واحد أو القاموس كله.
        """
        if not self._exinfo_cache:
            js = self._get("/api/v3/exchangeInfo")
            m = {}
            for s in js.get("symbols", []):
                sym = s.get("symbol")
                m[sym] = s
            self._exinfo_cache = m
        if symbol:
            return self._exinfo_cache.get(symbol.upper(), {})
        return self._exinfo_cache

    def _filters(self, symbol: str) -> Dict[str, Any]:
        s = self.exchange_info(symbol)
        out = {"tickSize": None, "stepSize": None, "minQty": None, "minNotional": None}
        for f in s.get("filters", []):
            t = f.get("filterType")
            if t == "PRICE_FILTER":
                out["tickSize"] = _safe_float(f.get("tickSize"), 0.0)
            elif t == "LOT_SIZE":
                out["stepSize"] = _safe_float(f.get("stepSize"), 0.0)
                out["minQty"] = _safe_float(f.get("minQty"), 0.0)
            elif t == "MIN_NOTIONAL":
                out["minNotional"] = _safe_float(f.get("minNotional"), 0.0)
            elif t == "NOTIONAL":
                # بعض الأزواج الحديثة تستخدم NOTIONAL بدلاً من MIN_NOTIONAL
                out["minNotional"] = _safe_float(f.get("minNotional"), 0.0)
        return out

    def round_price(self, symbol: str, price: float) -> float:
        f = self._filters(symbol)
        tick = f["tickSize"] or 0.0
        if tick <= 0:
            return float(f"{price:.8f}")
        # أرضية ليتوافق مع tickSize
        k = int(price / tick)
        return round(k * tick, 8)

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self._filters(symbol)
        step = f["stepSize"] or 0.0
        if step <= 0:
            return float(f"{qty:.8f}")
        k = int(qty / step)
        q = k * step
        # لا تقل عن minQty
        if f["minQty"] and q < f["minQty"]:
            q = f["minQty"]
        return float(f"{q:.8f}")

    # ------------- public market data -------------
    def klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 1000,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        /api/v3/klines سبوت.
        يرجّع DataFrame يحتوي الأعمدة:
          OpenTime, Open, High, Low, Close, Volume, CloseTime, QuoteAssetVolume, NumberOfTrades, TakerBuyBase, TakerBuyQuote
        وفهرس زمني (Timezone من CFG['TZ']).
        """
        limit = max(1, min(int(limit), 1000))
        params: Dict[str, Any] = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        if start_ms:
            params["startTime"] = int(start_ms)
        if end_ms:
            params["endTime"] = int(end_ms)

        url = f"{self._base}/api/v3/klines"
        r = requests.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data:
            return pd.DataFrame()

        cols_all = [
            "OpenTime", "Open", "High", "Low", "Close", "Volume",
            "CloseTime", "QuoteAssetVolume", "NumberOfTrades",
            "TakerBuyBase", "TakerBuyQuote", "Ignore"
        ]
        df = pd.DataFrame(data, columns=cols_all)
        # تحويل الأنواع
        for c in ("Open", "High", "Low", "Close", "Volume", "QuoteAssetVolume", "TakerBuyBase", "TakerBuyQuote"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["NumberOfTrades"] = pd.to_numeric(df["NumberOfTrades"], errors="coerce").fillna(0).astype(int)

        # فهرس زمني TZ-aware
        idx = pd.to_datetime(df["OpenTime"], unit="ms", utc=True).tz_convert(self._tz)
        df.index = idx

        return df[[
            "Open", "High", "Low", "Close", "Volume",
            "CloseTime", "QuoteAssetVolume", "NumberOfTrades",
            "TakerBuyBase", "TakerBuyQuote"
        ]]

    def ticker_24h_all(self) -> List[Dict[str, Any]]:
        """
        كل أزواج 24h — نستخدمها في السكانر.
        """
        return self._get("/api/v3/ticker/24hr")

    def ticker_price(self, symbol: str) -> float:
        js = self._get("/api/v3/ticker/price", params={"symbol": symbol.upper()})
        return _safe_float(js.get("price"))

    # ------------- account/balances -------------
    def account_info(self) -> Dict[str, Any]:
        return self._get("/api/v3/account", signed=True)

    def get_free(self, asset: str) -> float:
        info = self.account_info()
        for b in info.get("balances", []):
            if b.get("asset") == asset.upper():
                return _safe_float(b.get("free"))
        return 0.0

    # ------------- trading (MARKET) -------------
    def order_market_buy_quote(self, symbol: str, quote_qty: float) -> Dict[str, Any]:
        """
        شراء Market بمبلغ (quoteOrderQty) — مثال: BTCUSDT بمبلغ 50 USDT.
        يراعي minNotional تلقائيًا.
        """
        symbol = symbol.upper()
        # تحقق من حد أدنى للنوتيونال
        f = self._filters(symbol)
        # احسب سعر تقريبي لتأكيد النوتيونال
        px = self.ticker_price(symbol)
        notional_est = quote_qty  # لأن quote = USDT
        min_notional = f["minNotional"] or 0.0
        if min_notional and notional_est < min_notional:
            raise RuntimeError(f"Quote {quote_qty} < minNotional {min_notional} for {symbol}")

        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_qty:.2f}",  # باينانس يقبل كسور، لكن 2 decimals كافية لـ USDT
            "newOrderRespType": "FULL",
        }
        try:
            return self._post("/api/v3/order", signed=True, data=params)
        except RuntimeError as e:
            # تمرير رسائل واضحة
            raise

    def order_market_sell_qty(self, symbol: str, qty: float) -> Dict[str, Any]:
        """
        بيع Market بكمية (quantity) — يُراعي LOT_SIZE.
        """
        symbol = symbol.upper()
        q = self.round_qty(symbol, float(qty))
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{q:.8f}",
            "newOrderRespType": "FULL",
        }
        try:
            return self._post("/api/v3/order", signed=True, data=params)
        except RuntimeError as e:
            raise

    # ------------- open orders / cancel (اختياري) -------------
    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        p = {}
        if symbol:
            p["symbol"] = symbol.upper()
        return self._get("/api/v3/openOrders", signed=True, params=p)

    def cancel_all(self, symbol: str) -> List[Dict[str, Any]]:
        return self._delete("/api/v3/openOrders", signed=True, params={"symbol": symbol.upper()})
