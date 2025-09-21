# trade_binance_futures.py
from __future__ import annotations
import time, hmac, hashlib
import requests
from urllib.parse import urlencode

class BinanceFuturesREST:
    """
    REST عميل لـ Binance USDT-M Futures (One-way mode).
    لا يتعارض مع سبوت؛ مسارات مختلفة (/fapi/*).
    """
    def __init__(self, api_key: str, api_secret: str, *, testnet: bool = False, timeout: int = 15):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.timeout = timeout
        self.recv_window = 5000
        self._time_offset_ms = 0
        # قواعد Binance:
        # Live:  https://fapi.binance.com
        # Test:  https://testnet.binancefuture.com
        self.base = ("https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com")

    # ---------- core ----------
    def _sign(self, params: dict) -> dict:
        qs = urlencode(params, doseq=True)
        sig = hmac.new(self.api_secret, qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        url = self.base + path
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = self.recv_window
            params = self._sign(params)

        if method == "GET":
            r = self.session.get(url, params=params, timeout=self.timeout)
        elif method == "POST":
            r = self.session.post(url, params=params, timeout=self.timeout)
        elif method == "DELETE":
            r = self.session.delete(url, params=params, timeout=self.timeout)
        else:
            raise ValueError("Unsupported method")

        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            # أحياناً تُعيد نص/HTML عند ضغط عالي — نرمي استثناء واضح
            raise RuntimeError(f"Bad JSON from {path}: {r.text[:200]}")

    def _get(self, path: str, params: dict | None = None, signed: bool = False):
        return self._request("GET", path, params=params, signed=signed)

    def _post(self, path: str, params: dict | None = None, signed: bool = False):
        return self._request("POST", path, params=params, signed=signed)

    def _delete(self, path: str, params: dict | None = None, signed: bool = False):
        return self._request("DELETE", path, params=params, signed=signed)

    # ---------- time sync ----------
    def ping(self):
        return self._get("/fapi/v1/ping")

    def server_time(self) -> int:
        return int(self._get("/fapi/v1/time").get("serverTime"))

    def sync_time(self):
        try:
            st = self.server_time()
            lt = int(time.time() * 1000)
            self._time_offset_ms = st - lt
        except Exception:
            self._time_offset_ms = 0

    # ---------- market ----------
    def klines(self, symbol: str, interval: str = "1m", limit: int = 500):
        return self._get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})

    def ticker_price(self, symbol: str):
        return self._get("/fapi/v1/ticker/price", {"symbol": symbol})

    def exchange_info_symbol(self, symbol: str) -> dict:
        js = self._get("/fapi/v1/exchangeInfo", {"symbol": symbol})
        arr = js.get("symbols", [])
        if not arr:
            raise RuntimeError(f"Symbol {symbol} not found on Futures")
        return arr[0]

    # ---------- filters / formatting ----------
    @staticmethod
    def _round_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        return (int(value / step) * step)

    def lot_step_tick(self, symbol: str):
        info = self.exchange_info_symbol(symbol)
        min_qty, step, tick = 0.0, 0.0, 0.0
        for f in info.get("filters", []):
            t = f.get("filterType")
            if t == "LOT_SIZE":
                min_qty = float(f.get("minQty", "0"))
                step = float(f.get("stepSize", "0"))
            elif t == "PRICE_FILTER":
                tick = float(f.get("tickSize", "0"))
        return min_qty, step, tick

    def round_qty_price(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        min_qty, step, tick = self.lot_step_tick(symbol)
        if step > 0:
            qty = max(self._round_to_step(qty, step), min_qty)
        if tick > 0 and price > 0:
            price = (int(price / tick) * tick)
        return qty, price

    # ---------- account / balances ----------
    def futures_balance(self):
        return self._get("/fapi/v2/balance", signed=True)

    def futures_account(self):
        return self._get("/fapi/v2/account", signed=True)

    def get_free_usdt(self) -> float:
        """
        في USDT-M، الأفضل استخدام availableBalance من /fapi/v2/balance.
        """
        balances = self.futures_balance()
        for b in balances:
            if str(b.get("asset")).upper() == "USDT":
                try:
                    return float(b.get("availableBalance", "0"))
                except:
                    return 0.0
        return 0.0

    def get_position_risk(self, symbol: str | None = None):
        params = {"symbol": symbol} if symbol else None
        return self._get("/fapi/v2/positionRisk", params, signed=True)

    # ---------- leverage / margin ----------
    def set_leverage(self, symbol: str, leverage: int):
        leverage = max(1, min(int(leverage), 125))
        return self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        mt = (margin_type or "ISOLATED").upper()
        if mt not in ("ISOLATED", "CROSSED"):
            mt = "ISOLATED"
        try:
            return self._post("/fapi/v1/marginType", {"symbol": symbol, "marginType": mt}, signed=True)
        except requests.HTTPError as e:
            # -4046: No need to change margin type.
            if e.response is not None and e.response.status_code == 400 and "No need to change" in e.response.text:
                return {"msg": "No change"}
            raise

    # ---------- orders ----------
    def order_market(self, symbol: str, side: str, qty: float, reduce_only: bool = False):
        """
        أمر سوق Futures. side = BUY/SELL. qty = كمية الأصل (عقود، مثال BTC).
        reduce_only=True لإغلاق المراكز.
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = True
        return self._post("/fapi/v1/order", params, signed=True)

    def order_market_quote_notional(self, symbol: str, side: str, quote_usdt: float, leverage: int, mark_price: float | None = None):
        """
        افتح مركز بالقيمة الاسمية (USDT) مع رافعة. نحسب qty = (quote_usdt * leverage) / price.
        """
        if mark_price is None:
            px = float(self.ticker_price(symbol).get("price"))
        else:
            px = float(mark_price)
        notional = max(0.0, float(quote_usdt)) * max(1, int(leverage))
        if px <= 0 or notional <= 0:
            raise RuntimeError("Bad price or notional")
        raw_qty = notional / px
        qty, _ = self.round_qty_price(symbol, raw_qty, px)
        if qty <= 0:
            raise RuntimeError("Qty too small after filters")
        return self.order_market(symbol, side, qty, reduce_only=False)

    def close_all_for_symbol(self, symbol: str):
        """
        إغلاق أي مركز مفتوح (Long أو Short) بإرسال أمر معاكس reduceOnly.
        """
        risks = self.get_position_risk(symbol)
        total_qty = 0.0
        side_to_close = None
        for p in risks if isinstance(risks, list) else [risks]:
            if p.get("symbol") != symbol:
                continue
            pos_amt = float(p.get("positionAmt", "0"))
            if abs(pos_amt) > 0:
                total_qty += abs(pos_amt)
                side_to_close = "SELL" if pos_amt > 0 else "BUY"
        if total_qty <= 0 or side_to_close is None:
            return {"msg": "no-open-position"}
        min_qty, step, _ = self.lot_step_tick(symbol)
        qty = max(self._round_to_step(total_qty, step), min_qty)
        return self.order_market(symbol, side_to_close, qty, reduce_only=True)
