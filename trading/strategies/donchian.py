#strategies/donchian.py — Turtle-style channel breakout (trend following)
from __future__ import annotations
from collections import deque
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class DonchianBreakoutStrategy(BaseStrategy):
    """
    Classic Turtle breakout. Pure price action — no smoothing lag.
    BUY  when close breaks above highest high of last `entry_period` bars.
    SELL when close breaks below lowest low of last `entry_period` bars.
    Exit on opposite `exit_period` channel (faster exit than entry).
    15-min. Channels carry across days (overnight gaps create real breakouts).
    """

    def __init__(self, symbol: str, qty: int,
                 entry_period: int = 20, exit_period: int = 10) -> None:
        super().__init__(symbol, qty)
        self.entry_period = entry_period
        self.exit_period  = exit_period
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._highs:       deque           = deque(maxlen=self.entry_period)
        self._lows:        deque           = deque(maxlen=self.entry_period)
        self.entry_price:  Optional[float] = None
        self.current_date: Optional[str]   = None

    def _reset_day(self) -> None:
        self.position    = 0
        self.entry_price = None

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._reset_day()

        if h > EOD_EXIT_HOUR or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            self._highs.append(candle.high)
            self._lows.append(candle.low)
            return signals

        # Channels from PRIOR bars (current candle excluded)
        full        = len(self._highs) == self.entry_period
        upper       = max(self._highs) if full else None
        lower       = min(self._lows)  if full else None
        have_exit   = len(self._highs) >= self.exit_period
        exit_high   = max(list(self._highs)[-self.exit_period:]) if have_exit else None
        exit_low    = min(list(self._lows)[-self.exit_period:])  if have_exit else None

        # Manage open position (fast exit channel)
        if self.position == 1 and exit_low is not None and candle.close < exit_low:
            signals.append(self._signal("EXIT", candle.close,
                f"exit {self.exit_period}-low={exit_low:.2f}"))
            self.position = 0
        elif self.position == -1 and exit_high is not None and candle.close > exit_high:
            signals.append(self._signal("EXIT", candle.close,
                f"exit {self.exit_period}-high={exit_high:.2f}"))
            self.position = 0

        # Entry on breakout (only when flat)
        if self.position == 0 and upper is not None:
            if candle.close > upper:
                self.position    = 1
                self.entry_price = candle.close
                signals.append(self._signal("BUY", candle.close,
                    f"break {self.entry_period}-high={upper:.2f}"))
            elif candle.close < lower:
                self.position    = -1
                self.entry_price = candle.close
                signals.append(self._signal("SELL", candle.close,
                    f"break {self.entry_period}-low={lower:.2f}"))

        self._highs.append(candle.high)
        self._lows.append(candle.low)
        return signals
