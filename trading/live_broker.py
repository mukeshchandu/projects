from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Dict
from paper import PaperFill, _get_tick

log = logging.getLogger(__name__)

class LiveBroker:
    def __init__(self, client, tsym_map: Dict[str, str]) -> None:
        self.client   = client
        self.tsym_map = tsym_map

    def simulate_fill(self, symbol: str, side: str, qty: int,
                      mid_price: float, reason: str = "") -> PaperFill:
        tsym     = self.tsym_map.get(symbol, f"{symbol}-EQ")
        trantype = "B" if side.upper() == "BUY" else "S"
        resp = self.client.place_order(
            buy_or_sell   = trantype,
            product_type  = "I",
            exchange      = "NSE",
            tradingsymbol = tsym,
            quantity      = qty,
            price_type    = "MKT",
            remarks       = reason[:20] if reason else "",
        )
        if resp.get("stat") != "Ok":
            log.error("ORDER FAILED  %s %s qty=%d  emsg=%s",
                      side, symbol, qty, resp.get("emsg"))
        else:
            log.info("ORDER OK  %s %s qty=%d  norenordno=%s",
                     side, symbol, qty, resp.get("norenordno"))
        return PaperFill(datetime.now(timezone.utc), symbol, side.upper(),
                         qty, mid_price, reason)
