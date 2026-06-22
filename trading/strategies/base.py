#strategies/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List

from marketdata import Candle, Tick


class BaseStrategy(ABC):
    def __init__(self, symbol: str, qty: int) -> None:
        self.symbol   = symbol
        self.qty      = qty
        self.position = 0  # 0 = flat, 1 = long, -1 = short

    @abstractmethod
    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        ...

    def on_tick(self, tick: Tick) -> List[Dict[str, Any]]:
        return []

    def reset(self) -> None:
        self.position = 0

    def _signal(self, action: str, price: float, reason: str = "") -> Dict[str, Any]:
        return {
            "action": action,           # BUY / SELL / EXIT
            "symbol": self.symbol,
            "qty":    self.qty,
            "price":  price,
            "reason": reason,
            "side":   "BUY" if action == "BUY" else "SELL",
            "mode":   "paper",
        }