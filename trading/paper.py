#paper
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List


@dataclass
class PaperFill:
    ts: datetime
    symbol: str
    side: str
    qty: int
    price: float
    reason: str = ""


@dataclass
class PaperPosition:
    symbol: str
    side: str
    qty: int
    avg_price: float
    unrealized_pnl: float = 0.0


class PaperBroker:
    def __init__(self, slippage_bps: float = 3.0, brokerage_per_order: float = 0.0) -> None:
        self.slippage_bps = slippage_bps
        self.brokerage_per_order = brokerage_per_order
        self.positions: Dict[str, PaperPosition] = {}
        self.fills: List[PaperFill] = []
        self.daily_pnl: float = 0.0

    def simulate_fill(self, symbol: str, side: str, qty: int, mid_price: float, reason: str = "") -> PaperFill:
        slip = mid_price * (self.slippage_bps / 10000.0)
        fill_price = mid_price + slip if side.upper() == "BUY" else mid_price - slip
        fill = PaperFill(datetime.now(timezone.utc), symbol, side.upper(), qty, fill_price, reason)
        self.fills.append(fill)
        self._update_position(fill)
        return fill

    def _update_position(self, fill: PaperFill) -> None:
        pos = self.positions.get(fill.symbol)

        if pos is None:
            self.positions[fill.symbol] = PaperPosition(fill.symbol, fill.side, fill.qty, fill.price)
            return

        if pos.side == fill.side:
            total_qty = pos.qty + fill.qty
            pos.avg_price = ((pos.avg_price * pos.qty) + (fill.price * fill.qty)) / total_qty
            pos.qty = total_qty
            return

        if fill.qty < pos.qty:
            pos.qty -= fill.qty
        elif fill.qty == pos.qty:
            self.positions.pop(fill.symbol, None)
        else:
            new_qty = fill.qty - pos.qty
            self.positions[fill.symbol] = PaperPosition(fill.symbol, fill.side, new_qty, fill.price)