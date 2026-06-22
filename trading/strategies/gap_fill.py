#strategies/gap_fill.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class GapFillStrategy(BaseStrategy):
    """
    Gap Fill — 15-min candles.
    Compare 9:15 open vs previous day close.
    Gap down (open < prev_close by min_gap_pct%) → long (fill up).
    Gap up  (open > prev_close by min_gap_pct%) → short (fill down).
    Target = prev_close (the gap fill). SL = sl_pct% against entry.
    Only gaps between min_gap_pct and max_gap_pct are traded.
    Exit by 11:00 AM if not already filled.
    """

    def __init__(
        self,
        symbol: str,
        qty: int,
        min_gap_pct: float = 0.3,
        max_gap_pct: float = 3.0,
        sl_pct: float = 1.0,
    ) -> None:
        super().__init__(symbol, qty)
        self.min_gap_pct  = min_gap_pct
        self.max_gap_pct  = max_gap_pct
        self.sl_pct       = sl_pct
        self.prev_close:  Optional[float] = None
        self.gap_target:  Optional[float] = None
        self.entry_price: Optional[float] = None
        self.current_date: Optional[str]  = None

    def _reset_day(self) -> None:
        self.gap_target  = None
        self.entry_price = None
        self.position    = 0

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._reset_day()

        # EOD: save close and exit any open position
        if (h > EOD_EXIT_HOUR) or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            self.prev_close = candle.close
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            return signals

        # Update prev_close continuously (last candle of day becomes prev_close)
        # We only actually use it after day rollover, but keep tracking
        if h < 9 or (h == 9 and m < 15):
            return signals

        # Detect gap on first candle of day (9:15)
        if h == 9 and m == 15 and self.prev_close and self.gap_target is None:
            gap_pct = (candle.open - self.prev_close) / self.prev_close * 100
            abs_gap = abs(gap_pct)

            if self.min_gap_pct <= abs_gap <= self.max_gap_pct:
                self.gap_target = self.prev_close

        # Exit by 11:00 AM if position open
        if h >= 11 and self.position != 0:
            signals.append(self._signal("EXIT", candle.close, "time exit 11 AM"))
            self.position = 0
            return signals

        if self.gap_target is None:
            return signals

        gap_pct = (candle.open - self.prev_close) / self.prev_close * 100 if self.prev_close else 0

        # Manage open position
        if self.position == 1:
            if candle.high >= self.gap_target:
                signals.append(self._signal("EXIT", self.gap_target, "gap filled"))
                self.position = 0
            elif self.entry_price and candle.close < self.entry_price * (1 - self.sl_pct / 100):
                signals.append(self._signal("EXIT", candle.close, f"SL {self.sl_pct}%"))
                self.position = 0

        elif self.position == -1:
            if candle.low <= self.gap_target:
                signals.append(self._signal("EXIT", self.gap_target, "gap filled"))
                self.position = 0
            elif self.entry_price and candle.close > self.entry_price * (1 + self.sl_pct / 100):
                signals.append(self._signal("EXIT", candle.close, f"SL {self.sl_pct}%"))
                self.position = 0

        elif self.position == 0 and h == 9:
            if gap_pct < -self.min_gap_pct:  # gap down → buy
                self.entry_price = candle.close
                self.position    = 1
                signals.append(self._signal("BUY", candle.close,
                    f"gap down {gap_pct:.2f}% | target={self.gap_target:.2f}"))
            elif gap_pct > self.min_gap_pct:  # gap up → short
                self.entry_price = candle.close
                self.position    = -1
                signals.append(self._signal("SELL", candle.close,
                    f"gap up {gap_pct:.2f}% | target={self.gap_target:.2f}"))

        return signals