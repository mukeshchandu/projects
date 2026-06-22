#strategies/prev_day_breakout.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class PrevDayBreakoutStrategy(BaseStrategy):
    """
    Previous Day High/Low breakout. No indicators — pure price action.
    BUY  when price closes above yesterday's high.
    SELL when price closes below yesterday's low.
    SL = midpoint of yesterday's range. One trade per day.
    """

    def __init__(self, symbol: str, qty: int) -> None:
        super().__init__(symbol, qty)
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self.pdh:          Optional[float] = None  # previous day high
        self.pdl:          Optional[float] = None  # previous day low
        self._day_high:    Optional[float] = None  # accumulate today's high
        self._day_low:     Optional[float] = None
        self.current_date: Optional[str]   = None
        self.traded_today: bool            = False
        self.sl:           Optional[float] = None

    def _new_day(self) -> None:
        # Save today → yesterday
        if self._day_high is not None:
            self.pdh = self._day_high
            self.pdl = self._day_low
        self._day_high    = None
        self._day_low     = None
        self.traded_today = False
        self.sl           = None
        self.position     = 0

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._new_day()

        # Track today's range
        self._day_high = candle.high  if self._day_high is None else max(self._day_high, candle.high)
        self._day_low  = candle.low   if self._day_low  is None else min(self._day_low,  candle.low)

        if self.pdh is None:
            return signals  # no previous day data yet

        if h > EOD_EXIT_HOUR or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            return signals

        pdm = (self.pdh + self.pdl) / 2  # midpoint = stop-loss level

        # Manage open position
        if self.position == 1:
            if candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL hit | pdm={pdm:.2f}"))
                self.position = 0
            return signals

        if self.position == -1:
            if candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL hit | pdm={pdm:.2f}"))
                self.position = 0
            return signals

        if self.traded_today:
            return signals

        if candle.close > self.pdh:
            self.sl           = pdm
            self.position     = 1
            self.traded_today = True
            signals.append(self._signal("BUY", candle.close,
                f"PDH breakout | pdh={self.pdh:.2f} sl={self.sl:.2f}"))

        elif candle.close < self.pdl:
            self.sl           = pdm
            self.position     = -1
            self.traded_today = True
            signals.append(self._signal("SELL", candle.close,
                f"PDL breakout | pdl={self.pdl:.2f} sl={self.sl:.2f}"))

        return signals
