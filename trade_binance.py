import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode

class BinanceREST:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, timeout=15):
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": api_key})
        self.recv_window = 5000
        self.timeout = timeout
        self._time_offset_ms = 0
        self.base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"

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
            raise ValueError("method not supported")
        r.raise_for_status()
        return r.json()

    def _signed_request(self, method, path, params=None):
        return self._request(method, path, params=params or {}, signed=True)

    # ---------- time sync ----------
    def ping(self):
        return self._request("GET", "/api/v3/ping")

    def server_time(self):
        return self._request("GET", "/api/v3/time").get("serverTime")

    def sync_time(self):
        try:
            st = int(self.server_time())
            lt = int(time.time() * 1000)
            self._time_offset_ms = st - lt
        except Exception:
            self._time_offset_ms = 0

    # ---------- market ----------
    def klines(self, symbol: str, interval: str = "1m", limit: int = 500):
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        return self._request("GET", "/api/v3/klines", params=params)

    def ticker_price(self, symbol: str):
        return self._request("GET", "/api/v3/ticker/price", params={"symbol": symbol})

    def exchange_info_symbol(self, symbol: str) -> dict:
        js = self._request("GET", "/api/v3/exchangeInfo", params={"symbol": symbol})
        arr = js.get("symbols", [])
        if not arr:
            raise RuntimeError(f"Symbol {symbol} not found")
        return arr[0]

    # ---------- account / balances ----------
    def account(self):
        return self._signed_request("GET", "/api/v3/account")

    def get_free_usdt(self) -> float:
        acc = self.account()
        for b in acc.get("balances", []):
            if b.get("asset") == "USDT":
                try:
                    return float(b.get("free", "0"))
                except:
                    return 0.0
        return 0.0

    # ---------- filters ----------
    def symbol_min_notional(self, symbol: str) -> float:
        info = self.exchange_info_symbol(symbol)
        mn = 5.0
        for f in info.get("filters", []):
            t = f.get("filterType")
            if t in ("MIN_NOTIONAL", "NOTIONAL"):
                try:
                    mn = max(5.0, float(f.get("minNotional", f.get("notional", "5"))))
                except:
                    mn = max(5.0, mn)
        return mn

    def lot_step_tick(self, symbol: str):
        info = self.exchange_info_symbol(symbol)
        min_qty = 0.0
        step = 0.0
        tick = 0.0
        for f in info.get("filters", []):
            t = f.get("filterType")
            if t == "LOT_SIZE":
                min_qty = float(f.get("minQty", "0"))
                step = float(f.get("stepSize", "0"))
            elif t == "PRICE_FILTER":
                tick = float(f.get("tickSize", "0"))
        return min_qty, step, tick

    @staticmethod
    def _round_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        return int(qty / step) * step

    def round_qty_price(self, symbol: str, qty: float, price: float) -> tuple[float, float]:
        min_qty, step, tick = self.lot_step_tick(symbol)
        if step > 0:
            qty = max(self._round_step(qty, step), min_qty)
        if tick > 0 and price > 0:
            price = int(price / tick) * tick
        return qty, price

    # ---------- orders ----------
    def order_market_buy_quote(self, symbol: str, quote_qty: float):
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_qty:.8f}",
            "newOrderRespType": "FULL",
        }
        return self._signed_request("POST", "/api/v3/order", params=params)

    def order_market_sell_qty(self, symbol: str, qty: float):
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "newOrderRespType": "FULL",
        }
        return self._signed_request("POST", "/api/v3/order", params=params)
