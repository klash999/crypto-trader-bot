# trade_binance.py
# واجهة REST مبسطة لـ Binance Spot + USDT-M Futures مع دوال مساعدة للفلاتر والأرصدة والرافعة

import time
import hmac
import hashlib
import requests
import math
from urllib.parse import urlencode

from config import CFG

# إعدادات عامة من config مع قيم افتراضية
BIN = CFG.get("BINANCE", {})
API_KEY     = BIN.get("api_key", "")
API_SECRET  = BIN.get("api_secret", "")
TESTNET     = int(BIN.get("testnet", BIN.get("use_testnet", 0)))  # 0=Prod, 1=Testnet
RECV_WINDOW = int(BIN.get("recv_window", 5000))

# نهايات Binance
SPOT_BASE_PROD    = "https://api.binance.com"
SPOT_BASE_TESTNET = "https://testnet.binance.vision"
FAPI_BASE_PROD    = "https://fapi.binance.com"
FAPI_BASE_TESTNET = "https://testnet.binancefuture.com"

def _now_ms():
    return int(time.time() * 1000)

class BinanceREST:
    def __init__(self):
        self.api_key    = API_KEY
        self.api_secret = API_SECRET.encode("utf-8") if API_SECRET else b""
        self.spot_base  = SPOT_BASE_TESTNET if TESTNET else SPOT_BASE_PROD
        self.fapi_base  = FAPI_BASE_TESTNET if TESTNET else FAPI_BASE_PROD
        self.time_offset_spot  = 0
        self.time_offset_fut   = 0

        # جلسة HTTP واحدة
        self.s = requests.Session()
        if self.api_key:
            self.s.headers.update({"X-MBX-APIKEY": self.api_key})

    # ------------------------ أدوات داخلية ------------------------

    def sync_time(self):
        """تزامن الوقت مع Binance (Spot + Futures)."""
        try:
            t_spot = self._http("GET", f"{self.spot_base}/api/v3/time")
            srv = int(t_spot.get("serverTime"))
            self.time_offset_spot = srv - _now_ms()
        except Exception:
            pass
        try:
            t_fut = self._http("GET", f"{self.fapi_base}/fapi/v1/time")
            srv = int(t_fut.get("serverTime"))
            self.time_offset_fut = srv - _now_ms()
        except Exception:
            pass

    def _ts_spot(self):
        return _now_ms() + self.time_offset_spot

    def _ts_fut(self):
        return _now_ms() + self.time_offset_fut

    def _http(self, method: str, url: str, params=None, headers=None, timeout=15):
        params = params or {}
        headers = headers or {}
        r = self.s.request(method=method, url=url, params=params if method=="GET" else None,
                           data=params if method!="GET" else None, headers=headers, timeout=timeout)
        r.raise_for_status()
        if r.text and r.headers.get("Content-Type","").startswith("application/json"):
            return r.json()
        # بعض ردود testnet ترجع نصاً عند بعض الأخطاء، نعطيها كما هي
        try:
            return r.json()
        except Exception:
            return {"raw": r.text, "status": r.status_code}

    def _sign(self, query: dict) -> str:
        q = urlencode(query, doseq=True)
        sig = hmac.new(self.api_secret, q.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{q}&signature={sig}"

    # ------------------------ Spot REST ------------------------

    def _public(self, method: str, path: str, params=None):
        url = f"{self.spot_base}{path}"
        return self._http(method, url, params=params or {})

    def _signed(self, method: str, path: str, params=None):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("ضع BINANCE_API_KEY و BINANCE_API_SECRET في .env/CFG")
        url = f"{self.spot_base}{path}"
        q = params.copy() if params else {}
        q.setdefault("recvWindow", RECV_WINDOW)
        q["timestamp"] = self._ts_spot()
        query = self._sign(q)
        headers = {"X-MBX-APIKEY": self.api_key}
        if method == "GET":
            return self._http("GET", url, params=query, headers=headers)
        else:
            return self._http(method, url, params=query, headers=headers)

    def order_market_buy_quote(self, symbol: str, quote_qty: float):
        """شراء Spot بقيمة Quote (USDT) — يستخدم quoteOrderQty."""
        params = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{float(quote_qty):.2f}",
        }
        return self._signed("POST", "/api/v3/order", params=params)

    def order_market_sell_qty(self, symbol: str, qty: float):
        """بيع Spot بكمية رمزية."""
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{float(qty):.8f}",
        }
        return self._signed("POST", "/api/v3/order", params=params)

    def balance_free(self, asset: str) -> float:
        acc = self._signed("GET", "/api/v3/account", params={})
        for b in acc.get("balances", []):
            if b.get("asset") == asset:
                try:
                    return float(b.get("free", 0))
                except:
                    return 0.0
        return 0.0

    def symbol_filters_spot(self, symbol: str) -> dict:
        info = self._public("GET", "/api/v3/exchangeInfo", params={"symbol": symbol}) or {}
        arr = info.get("symbols", [])
        if not arr: return {}
        out = {}
        for f in arr[0].get("filters", []):
            t = f.get("filterType")
            if t in ("MIN_NOTIONAL","NOTIONAL"):
                if "minNotional" in f:
                    out["min_notional"] = float(f["minNotional"])
                if "notional" in f:
                    out["min_notional"] = max(float(out.get("min_notional", 0) or 0), float(f["notional"]))
            elif t == "LOT_SIZE":
                out["min_qty"] = float(f.get("minQty", 0))
                out["step_size"] = float(f.get("stepSize", 0))
            elif t == "PRICE_FILTER":
                out["tick_size"] = float(f.get("tickSize", 0))
        out.setdefault("min_notional", 5.0)
        return out

    # ------------------------ Futures (USDT-M) REST ------------------------

    def _public_futures(self, method: str, path: str, params=None):
        url = f"{self.fapi_base}{path}"
        return self._http(method, url, params=params or {})

    def _signed_futures(self, method: str, path: str, params=None):
        if not self.api_key or not self.api_secret:
            raise RuntimeError("ضع BINANCE_API_KEY و BINANCE_API_SECRET في .env/CFG")
        url = f"{self.fapi_base}{path}"
        q = params.copy() if params else {}
        q.setdefault("recvWindow", RECV_WINDOW)
        q["timestamp"] = self._ts_fut()
        query = self._sign(q)
        headers = {"X-MBX-APIKEY": self.api_key}
        if method == "GET":
            return self._http("GET", url, params=query, headers=headers)
        else:
            return self._http(method, url, params=query, headers=headers)

    def futures_balance_usdt(self) -> float:
        arr = self._signed_futures("GET", "/fapi/v2/balance", params={}) or []
        for it in arr:
            if it.get("asset") == "USDT":
                try:
                    return float(it.get("availableBalance", 0))
                except:
                    return 0.0
        return 0.0

    def futures_symbol_filters(self, symbol: str) -> dict:
        info = self._public_futures("GET", "/fapi/v1/exchangeInfo", params={"symbol": symbol}) or {}
        arr = info.get("symbols", [])
        if not arr: return {}
        out = {}
        for f in arr[0].get("filters", []):
            t = f.get("filterType")
            if t in ("MIN_NOTIONAL","NOTIONAL"):
                if "minNotional" in f:
                    out["min_notional"] = float(f["minNotional"])
                if "notional" in f:
                    out["min_notional"] = max(float(out.get("min_notional", 0) or 0), float(f["notional"]))
            elif t in ("LOT_SIZE","MARKET_LOT_SIZE"):
                out["min_qty"]  = float(f.get("minQty", 0))
                out["step_size"] = float(f.get("stepSize", 0))
            elif t == "PRICE_FILTER":
                out["tick_size"] = float(f.get("tickSize", 0))
        out.setdefault("min_notional", 5.0)
        return out

    def futures_set_leverage(self, symbol: str, leverage: int):
        return self._signed_futures("POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": leverage})

    def futures_set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        # قد يُرجع 400/409 لو مضبوط مسبقًا
        try:
            return self._signed_futures("POST", "/fapi/v1/marginType", params={"symbol": symbol, "marginType": margin_type})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (400, 409):
                return {"info": "marginType already set"}
            raise

    def futures_get_position_mode(self) -> str:
        r = self._signed_futures("GET", "/fapi/v1/positionSide/dual", params={}) or {}
        return "HEDGE" if r.get("dualSidePosition") else "ONEWAY"

    def futures_get_symbol_leverage(self, symbol: str):
        arr = self._signed_futures("GET", "/fapi/v2/positionRisk", params={"symbol": symbol}) or []
        if isinstance(arr, list) and arr:
            lev = arr[0].get("leverage")
            try:
                return int(float(lev))
            except:
                return None
        return None

    def futures_order_market(self, symbol: str, side: str, quantity: float):
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{float(quantity):.8f}",
        }
        return self._signed_futures("POST", "/fapi/v1/order", params=params)
