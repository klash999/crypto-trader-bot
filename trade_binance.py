import time, hmac, hashlib, math
from typing import Dict, Any, Tuple
import requests
from config import CFG

BASE_LIVE = "https://api.binance.com"
BASE_TEST = "https://testnet.binance.vision"

class BinanceREST:
    def __init__(self):
        self.api_key = CFG["BINANCE"]["api_key"]
        self.api_secret = (CFG["BINANCE"]["api_secret"] or "").encode()
        self.base = BASE_TEST if CFG["BINANCE"]["testnet"] else BASE_LIVE
        self.s = requests.Session()
        if self.api_key:
            self.s.headers.update({"X-MBX-APIKEY": self.api_key})
        self._drift_ms = 0
        try:
            self.sync_time()
        except Exception:
            pass

    def sync_time(self):
        js = self._get("/api/v3/time")
        server = int(js["serverTime"])
        local = int(time.time() * 1000)
        self._drift_ms = server - local
        return self._drift_ms

    def _ts(self) -> int:
        return int(time.time() * 1000 + self._drift_ms)

    def _sign(self, qs: str) -> str:
        return hmac.new(self.api_secret, qs.encode(), hashlib.sha256).hexdigest()

    def _raise(self, r: requests.Response):
        try:
            js = r.json()
        except Exception:
            r.raise_for_status()
        code = js.get("code")
        msg = js.get("msg")
        raise RuntimeError(f"Binance error {code}: {msg}")

    def _get(self, path: str, params: Dict[str, Any] = None, signed=False):
        url = self.base + path
        params = params or {}
        if signed:
            params.update({"timestamp": self._ts(), "recvWindow": 5000})
            qs = "&".join([f"{k}={v}" for k, v in params.items()])
            params["signature"] = self._sign(qs)
        r = self.s.get(url, params=params, timeout=12)
        if r.status_code != 200:
            self._raise(r)
        return r.json()

    def _post(self, path: str, params: Dict[str, Any], signed=True):
        url = self.base + path
        params = params or {}
        if signed:
            params.update({"timestamp": self._ts(), "recvWindow": 5000})
            qs = "&".join([f"{k}={v}" for k, v in params.items()])
            params["signature"] = self._sign(qs)
        r = self.s.post(url, params=params, timeout=12)
        if r.status_code != 200:
            try:
                js = r.json()
                if js.get("code") == -1021:
                    try: self.sync_time()
                    except Exception: pass
            except Exception:
                pass
            self._raise(r)
        return r.json()

    # ---------- Public ----------
    def ticker_price(self, symbol: str) -> float:
        js = self._get("/api/v3/ticker/price", {"symbol": symbol})
        return float(js["price"]) if isinstance(js, dict) else float(js[0]["price"])

    def exchange_info(self, symbol: str) -> dict:
        js = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        return (js.get("symbols") or [])[0]

    def klines(self, symbol: str, interval: str = "1m", limit: int = 500):
        return self._get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)})

    def all_24h_tickers(self):
        return self._get("/api/v3/ticker/24hr")  # list of dicts

    # ---------- Helpers for filters ----------
    def _filters(self, symbol_info: dict) -> Tuple[float, float, float]:
        step = min_qty = min_notional = None
        for f in symbol_info.get("filters", []):
            t = f.get("filterType")
            if t == "LOT_SIZE":
                step = float(f.get("stepSize"))
                min_qty = float(f.get("minQty"))
            elif t == "MIN_NOTIONAL":
                min_notional = float(f.get("minNotional"))
        return step or 0.0, min_qty or 0.0, min_notional or 0.0

    def normalize_qty(self, symbol: str, qty: float) -> float:
        info = self.exchange_info(symbol)
        step, min_qty, _ = self._filters(info)
        if step and step > 0:
            qty = math.floor(qty / step) * step
        if min_qty and qty < min_qty:
            qty = 0.0
        return float(qty)

    # ---------- Signed ----------
    def account(self) -> dict:
        return self._get("/api/v3/account", signed=True)

    def order_market_buy_quote(self, symbol: str, quote_qty: float):
        info = self.exchange_info(symbol)
        _, _, min_notional = self._filters(info)
        if min_notional and quote_qty < min_notional:
            raise RuntimeError(f"القيمة {quote_qty} أقل من الحد الأدنى للرمز ({min_notional} USDT)")
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": quote_qty,
            "newOrderRespType": "FULL",
        })

    def order_market_sell_qty(self, symbol: str, qty: float):
        qn = self.normalize_qty(symbol, qty)
        if qn <= 0:
            raise RuntimeError(f"الكمية بعد التطبيع ({qty}→{qn}) أقل من الحد الأدنى، تحقق من الرصيد/الحدود.")
        return self._post("/api/v3/order", {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": qn,
            "newOrderRespType": "FULL",
        })
