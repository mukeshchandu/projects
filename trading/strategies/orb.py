#strategies/orb.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class ORBStrategy(BaseStrategy):
    """
    Opening Range Breakout — 15-min candle.
    9:15 candle sets the range.
    Close above high → long. Close below low → short.
    Target = 1.5× range from entry. SL = opposite end of range.
    One trade per day. Exit by EOD_EXIT_HOUR:EOD_EXIT_MINUTE.
    """

    def __init__(self, symbol: str, qty: int, target_mult: float = 1.5) -> None:
        super().__init__(symbol, qty)
        self.target_mult  = target_mult
        self.orb_high:    Optional[float] = None
        self.orb_low:     Optional[float] = None
        self.orb_set:     bool            = False
        self.traded_today: bool           = False
        self.entry_price: Optional[float] = None
        self.target:      Optional[float] = None
        self.sl:          Optional[float] = None
        self.current_date: Optional[str]  = None

    def _reset_day(self) -> None:
        self.orb_high     = None
        self.orb_low      = None
        self.orb_set      = False
        self.traded_today = False
        self.entry_price  = None
        self.target       = None
        self.sl           = None
        self.position     = 0

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._reset_day()

        # Set ORB from first 9:15 candle
        if h == 9 and m == 15 and not self.orb_set:
            self.orb_high = candle.high
            self.orb_low  = candle.low
            self.orb_set  = True
            return signals

        if not self.orb_set:
            return signals

        orb_range = self.orb_high - self.orb_low

        # EOD exit
        if (h > EOD_EXIT_HOUR) or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            return signals

        # Manage open long
        if self.position == 1:
            if candle.high >= self.target:
                signals.append(self._signal("EXIT", self.target, "target hit"))
                self.position = 0
            elif candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl, "SL hit"))
                self.position = 0
            return signals

        # Manage open short
        if self.position == -1:
            if candle.low <= self.target:
                signals.append(self._signal("EXIT", self.target, "target hit"))
                self.position = 0
            elif candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl, "SL hit"))
                self.position = 0
            return signals

        # Entry — only one trade per day
        if self.traded_today or orb_range <= 0:
            return signals

        if candle.close > self.orb_high:
            self.entry_price  = candle.close
            self.target       = self.entry_price + orb_range * self.target_mult
            self.sl           = self.entry_price - orb_range
            self.position     = 1
            self.traded_today = True
            signals.append(self._signal(
                "BUY", self.entry_price,
                f"ORB breakout up | range={orb_range:.2f} tgt={self.target:.2f} sl={self.sl:.2f}"
            ))

        elif candle.close < self.orb_low:
            self.entry_price  = candle.close
            self.target       = self.entry_price - orb_range * self.target_mult
            self.sl           = self.entry_price + orb_range
            self.position     = -1
            self.traded_today = True
            signals.append(self._signal(
                "SELL", self.entry_price,
                f"ORB breakout down | range={orb_range:.2f} tgt={self.target:.2f} sl={self.sl:.2f}"
            ))

        return signals