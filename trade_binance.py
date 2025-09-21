# trade_binance.py
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode


class BinanceREST:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False, timeout: int = 15):
        self.api_key = api_key or ""
        self.api_secret = (api_secret or "").encode()
        self.session = requests.Session()
        # نمرّر المفتاح دومًا (لا ضرر)، والموقّع فقط وقت الحاجة
        if self.api_key:
            self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        self.recv_window = 5000
        self.timeout = timeout
        self._time_offset_ms = 0
        self.base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"

    # --------------- أدوات داخلية ---------------
    def _sign(self, params: dict) -> dict:
        """توقيع SHA256 لمعاملات الحساب/الأوامر."""
        qs = urlencode(params, doseq=True)
        sig = hmac.new(self.api_secret, qs.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False, timeout: int | None = None):
        """دالة الطلب العامة. scanner.py ممكن يستدعي `_get` المبني عليها."""
        url = self.base + path
        params = dict(params or {})
        to = timeout or self.timeout

        if signed:
            # نضيف timestamp و recvWindow ثم نوقّع
            ts = int(time.time() * 1000) + self._time_offset_ms
            params.setdefault("recvWindow", self.recv_window)
            params["timestamp"] = ts
            params = self._sign(params)

        # Binance تقبل POST بالـ query string أيضًا.
        if method.upper() == "GET":
            r = self.session.get(url, params=params, timeout=to)
        elif method.upper() == "POST":
            r = self.session.post(url, params=params, timeout=to)
        elif method.upper() == "DELETE":
            r = self.session.delete(url, params=params, timeout=to)
        else:
            raise ValueError("Unsupported HTTP method")

        r.raise_for_status()
        # قد يرجّع HTML أو نص عند مشاكل الشبكة/الحظر، نحاول JSON بأمان
        try:
            js = r.json()
        except ValueError:
            raise RuntimeError(f"Binance returned non-JSON from {path}: {r.text[:180]}")
        # أخطاء Binance تكون كائن فيه code<0
        if isinstance(js, dict) and "code" in js and isinstance(js["code"], int) and js["code"] < 0:
            raise RuntimeError(f"Binance error {js.get('code')}: {js.get('msg')}")
        return js

    # واجهات مختصرة متوافقة مع scanner.py
    def _get(self, path: str, params: dict | None = None, signed: bool = False, timeout: int | None = None):
        return self._request("GET", path, params=params, signed=signed, timeout=timeout)

    def _post(self, path: str, params: dict | None = None, signed: bool = True, timeout: int | None = None):
        return self._request("POST", path, params=params, signed=signed, timeout=timeout)

    def _delete(self, path: str, params: dict | None = None, signed: bool = True, timeout: int | None = None):
        return self._request("DELETE", path, params=params, signed=signed, timeout=timeout)

    # --------------- مزامنة الوقت ---------------
    def ping(self):
        return self._get("/api/v3/ping")

    def server_time(self) -> int:
        return self._get("/api/v3/time").get("serverTime")

    def sync_time(self):
        """يضبط انحراف الوقت لتفادي -1021."""
        try:
            st = int(self.server_time())
            lt = int(time.time() * 1000)
            self._time_offset_ms = st - lt
        except Exception:
            self._time_offset_ms = 0

    # --------------- بيانات السوق ---------------
    def klines(self, symbol: str, interval: str = "1m", limit: int = 500):
        limit = max(1, min(int(limit), 1000))
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        return self._get("/api/v3/klines", params=params)

    def ticker_price(self, symbol: str):
        return self._get("/api/v3/ticker/price", params={"symbol": symbol})

    def ticker_24hr(self, symbol: str | None = None):
        """إن قدّمت symbol يرجّع dict واحد؛ بدونها يرجّع list لجميع الأزواج."""
        params = {"symbol": symbol} if symbol else None
        return self._get("/api/v3/ticker/24hr", params=params)

    def exchange_info(self):
        return self._get("/api/v3/exchangeInfo")

    def exchange_info_symbol(self, symbol: str) -> dict:
        js = self._get("/api/v3/exchangeInfo", params={"symbol": symbol})
        arr = js.get("symbols", [])
        if not arr:
            raise RuntimeError(f"Symbol {symbol} not found")
        return arr[0]

    # --------------- الحساب والأرصدة ---------------
    def account(self):
        return self._get("/api/v3/account", signed=True)

    def get_free_usdt(self) -> float:
        acc = self.account()
        for b in acc.get("balances", []):
            if b.get("asset") == "USDT":
                try:
                    return float(b.get("free", "0"))
                except Exception:
                    return 0.0
        return 0.0

    # --------------- قيود التداول (فلاتر) ---------------
    def symbol_min_notional(self, symbol: str) -> float:
        info = self.exchange_info_symbol(symbol)
        mn = 5.0
        for f in info.get("filters", []):
            t = f.get("filterType")
            if t in ("MIN_NOTIONAL", "NOTIONAL"):
                try:
                    mn = max(5.0, float(f.get("minNotional", f.get("notional", "5"))))
                except Exception:
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

    # --------------- الأوامر ---------------
    def order_market_buy_quote(self, symbol: str, quote_qty: float):
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_qty:.8f}",
            "newOrderRespType": "FULL",
        }
        return self._post("/api/v3/order", params=params, signed=True)

    def order_market_sell_qty(self, symbol: str, qty: float):
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "newOrderRespType": "FULL",
        }
        return self._post("/api/v3/order", params=params, signed=True)
