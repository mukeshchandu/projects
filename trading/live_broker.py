from __future__ import annotations
import logging, math
from datetime import datetime, timezone
from typing import Dict, Optional
from paper import PaperFill, _get_tick

log = logging.getLogger(__name__)

class LiveBroker:
    def __init__(self, client, tsym_map: Dict[str, str],
                 mode_map: Optional[Dict[str, str]] = None) -> None:
        self.client   = client
        self.tsym_map = tsym_map
        self.mode_map = mode_map or {}   # symbol → "MIS" | "CNC"
        self.pending: Dict[str, dict] = {}

    def simulate_fill(self, symbol: str, side: str, qty: int,
                      mid_price: float, reason: str = "") -> Optional[PaperFill]:
        mode     = self.mode_map.get(symbol, "MIS")
        product  = "C" if mode == "CNC" else "I"
        tsym     = self.tsym_map.get(symbol, f"{symbol}-EQ")
        trantype = "B" if side.upper() == "BUY" else "S"

        t = _get_tick(mid_price)
        if side.upper() == "BUY":
            limit_price = round((math.ceil(round(mid_price / t, 8)) + 1) * t, 4)
        else:
            limit_price = round((math.floor(round(mid_price / t, 8)) - 1) * t, 4)

        resp = self.client.place_order(
            buy_or_sell   = trantype,
            product_type  = product,
            exchange      = "NSE",
            tradingsymbol = tsym,
            quantity      = qty,
            price_type    = "LMT",
            price         = limit_price,
            retention     = "IOC",
            remarks       = reason[:20] if reason else "",
        )
        if resp.get("stat") != "Ok":
            log.error("ORDER REJECTED  [%s] %s %s qty=%d  emsg=%s",
                      mode, side, symbol, qty, resp.get("emsg"))
            return None

        norenordno = resp.get("norenordno", "")
        log.info("ORDER OK  [%s] %s %s qty=%d  norenordno=%s  Rs%.4f",
                 mode, side, symbol, qty, norenordno, limit_price)

        if side.upper() in ("SELL", "BUY") and mode == "CNC":
            action = "BUY" if trantype == "B" else "SELL"
            if action == "SELL":
                log.warning("CNC SAME-DAY EXIT %s — check Flattrade dashboard, "
                            "convert to MIS if needed", symbol)

        if norenordno:
            self.pending[norenordno] = {
                "symbol": symbol, "side": side,
                "qty": qty, "est": mid_price, "mode": mode,
            }
        return PaperFill(datetime.now(timezone.utc), symbol, side.upper(),
                         qty, mid_price, reason)
