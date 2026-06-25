#client.py
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests
import websocket

from config import REST_BASE, WS_URL

HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}


def _rest(endpoint: str, payload: Dict[str, Any], token: str) -> Any:
    body = "jData=" + json.dumps(payload) + "&jKey=" + token
    r = requests.post(f"{REST_BASE}/{endpoint}", data=body, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


class FlattradeClient:
    def __init__(self) -> None:
        self.uid:   Optional[str] = None
        self.token: Optional[str] = None
        self.ws:    Optional[websocket.WebSocketApp] = None

        self.on_tick:  Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_order: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_open:  Optional[Callable[["FlattradeClient"], None]] = None
        self.on_close: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Any], None]] = None

    def set_session(self, user_id: str, token: str) -> None:
        self.uid   = user_id
        self.token = token

    def _req(self, endpoint: str, extra: Optional[Dict[str, Any]] = None) -> Any:
        if not self.uid or not self.token:
            raise RuntimeError("Session not set — call set_session() first")
        payload: Dict[str, Any] = {"uid": self.uid, "actid": self.uid}
        if extra:
            payload.update(extra)
        return _rest(endpoint, payload, self.token)

    # ── Market data ───────────────────────────────────────────────────

    def search_scrip(self, exchange: str, search_text: str) -> List[Dict[str, Any]]:
        """Returns list of matching scrips with token, tsym, ti, ls, pp."""
        result = self._req("SearchScrip", {"exch": exchange, "stext": search_text})
        if isinstance(result, dict) and result.get("stat") == "Ok":
            return result.get("values", [])
        return []

    def get_quotes(self, exchange: str, token: str) -> Dict[str, Any]:
        """Returns quote with ti (tick size), ls (lot size), lp, etc."""
        return self._req("GetQuotes", {"exch": exchange, "token": token})

    def get_index_list(self, exchange: str = "NSE") -> List[Dict[str, Any]]:
        """Returns list of {idxname, token} for all indices."""
        result = self._req("GetIndexList", {"exch": exchange})
        if isinstance(result, dict):
            return result.get("values", [])
        return []

    def get_time_price_series(
        self,
        exchange: str,
        token: str,
        start_dt: datetime,
        end_dt: datetime,
        interval: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Fetches OHLCV candles.
        interval: 1 / 3 / 5 / 10 / 15 / 30 / 60 / 120 (minutes)
        Returns list of dicts with: time, into, inth, intl, intc, intvwap, intv, v
        """
        if not self.uid or not self.token:
            raise RuntimeError("Session not set — call set_session() first")
        result = _rest("TPSeries", {
            "ordersource": "API",
            "uid":         self.uid,
            "exch":        exchange,
            "token":       token,
            "st":          str(int(start_dt.timestamp())),
            "et":          str(int(end_dt.timestamp())),
            "intrv":       str(interval),
        }, self.token)
        if isinstance(result, list):
            return result
        emsg = result.get("emsg", str(result)) if isinstance(result, dict) else str(result)
        print(f"  [TPSeries warn] {emsg}")
        return []

    # ── Orders ────────────────────────────────────────────────────────

    def place_order(
        self,
        buy_or_sell: str,
        product_type: str,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        price_type: str = "MKT",
        price: float = 0,
        trigger_price: Optional[float] = None,
        retention: str = "DAY",
        remarks: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "exch":        exchange,
            "tsym":        tradingsymbol,
            "qty":         str(quantity),
            "prc":         str(price),
            "prd":         product_type,
            "trantype":    buy_or_sell,
            "prctyp":      price_type,
            "ret":         retention,
            "dscqty":      "0",
            "ordersource": "API",
        }
        if trigger_price is not None:
            payload["trgprc"] = str(trigger_price)
        if remarks:
            payload["remarks"] = remarks
        return self._req("PlaceOrder", payload)

    def get_limits(self) -> dict:
        return self._req("Limits", {"product": "I", "segment": "EQ", "exchange": "NSE"})

    def cancel_order(self, order_no: str) -> Dict[str, Any]:
        return self._req("CancelOrder", {"norenordno": order_no})

    def order_book(self)    -> Any: return self._req("OrderBook")
    def trade_book(self)    -> Any: return self._req("TradeBook")
    def positions(self)     -> Any: return self._req("PositionBook")
    def holdings(self, prd: str = "C") -> Any: return self._req("Holdings", {"prd": prd})

    # ── WebSocket ─────────────────────────────────────────────────────

    def subscribe(self, scrip_key: str, feed_type: str = "t") -> None:
        """scrip_key format: 'NSE|26000' or 'NSE|26000#NSE|26009'"""
        if self.ws:
            self.ws.send(json.dumps({"t": feed_type, "k": scrip_key}))

    def subscribe_orders(self) -> None:
        if self.ws:
            self.ws.send(json.dumps({"t": "o", "actid": self.uid}))

    def start_websocket(
        self,
        on_tick:  Optional[Callable[[Dict[str, Any]], None]] = None,
        on_order: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_open:  Optional[Callable[["FlattradeClient"], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Any], None]] = None,
    ) -> None:
        self.on_tick  = on_tick
        self.on_order = on_order
        self.on_open  = on_open
        self.on_close = on_close
        self.on_error = on_error
        self.ws = websocket.WebSocketApp(
            WS_URL,
            on_open=self._ws_open,
            on_message=self._ws_message,
            on_error=self._ws_error,
            on_close=self._ws_close,
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def _ws_open(self, ws: websocket.WebSocketApp) -> None:
        ws.send(json.dumps({
            "t": "a", "uid": self.uid, "actid": self.uid,
            "source": "API", "accesstoken": self.token,
        }))

    def _ws_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("t", "")
        if t == "ak":
            if msg.get("s") == "OK" and self.on_open:
                self.on_open(self)
        elif t in ("tk", "tf", "dk", "df"):
            if self.on_tick:
                self.on_tick(msg)
        elif t == "om":
            if self.on_order:
                self.on_order(msg)

    def _ws_error(self, ws: websocket.WebSocketApp, err: Any) -> None:
        if self.on_error:
            self.on_error(err)

    def _ws_close(self, ws: websocket.WebSocketApp, code: Any, msg: Any) -> None:
        if self.on_close:
            self.on_close(code, msg)