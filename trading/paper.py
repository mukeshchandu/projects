from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List


# ── NSE tiered tick size ──────────────────────────────────────────────────
def _get_tick(price: float) -> float:
    if   price <=   250: return 0.01
    elif price <= 1_000: return 0.05
    elif price <= 5_000: return 0.10
    elif price <=10_000: return 0.50
    elif price <=20_000: return 1.00
    else:                return 5.00


@dataclass
class PaperFill:
    ts:     datetime
    symbol: str
    side:   str
    qty:    int
    price:  float
    reason: str = ""
    ordno:  str = ""   # broker order id (live), for correlating fill confirmations


@dataclass
class PaperPosition:
    symbol:         str
    side:           str
    qty:            int
    avg_price:      float
    unrealized_pnl: float = 0.0


class PaperBroker:
    """
    Simulates fills at best ask (BUY) / best bid (SELL).
    Uses NSE tiered tick size — no bps fiction.

    BUY  fill = round_up_to_tick(price) + 1 tick  → paying the ask
    SELL fill = round_down_to_tick(price) - 1 tick → receiving the bid
    """

    def __init__(self, brokerage_per_order: float = 0.0) -> None:
        self.brokerage_per_order = brokerage_per_order
        self.positions: Dict[str, PaperPosition] = {}
        self.fills:     List[PaperFill]          = []
        self.daily_pnl: float                    = 0.0

    def simulate_fill(
        self, symbol: str, side: str, qty: int, mid_price: float, reason: str = "",
        quote=None, exit_order: bool = False
    ) -> PaperFill:
        t = _get_tick(mid_price)
        if side.upper() == "BUY":
            # Pay ask: round up to next tick then add 1 tick for spread
            fill_price = (math.ceil(round(mid_price / t, 8)) + 1) * t
        else:
            # Receive bid: round down then subtract 1 tick for spread
            fill_price = (math.floor(round(mid_price / t, 8)) - 1) * t

        fill = PaperFill(datetime.now(timezone.utc), symbol, side.upper(),
                         qty, round(fill_price, 4), reason)
        self.fills.append(fill)
        self._update_position(fill)
        return fill

    def _update_position(self, fill: PaperFill) -> None:
        pos = self.positions.get(fill.symbol)

        if pos is None:
            self.positions[fill.symbol] = PaperPosition(
                fill.symbol, fill.side, fill.qty, fill.price)
            return

        if pos.side == fill.side:
            total_qty  = pos.qty + fill.qty
            pos.avg_price = ((pos.avg_price * pos.qty) + (fill.price * fill.qty)) / total_qty
            pos.qty    = total_qty
            return

        if fill.qty < pos.qty:
            pos.qty -= fill.qty
        elif fill.qty == pos.qty:
            self.positions.pop(fill.symbol, None)
        else:
            new_qty = fill.qty - pos.qty
            self.positions[fill.symbol] = PaperPosition(
                fill.symbol, fill.side, new_qty, fill.price)
