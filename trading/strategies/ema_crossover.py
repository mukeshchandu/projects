#strategies/ema_crossover.py
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional

from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class EMACrossoverStrategy(BaseStrategy):
    """
    9 EMA / 21 EMA crossover — 5-min candles.
    Golden cross (fast > slow) → long.
    Death cross (fast < slow) → short.
    SL = 0.75% against entry. Exit on opposite cross or EOD.
    """

    def __init__(self, symbol: str, qty: int, fast: int = 9, slow: int = 21) -> None:
        super().__init__(symbol, qty)
        self.fast_period = fast
        self.slow_period = slow
        self.closes:     deque = deque(maxlen=slow + 10)
        self.prev_fast:  Optional[float] = None
        self.prev_slow:  Optional[float] = None
        self.entry_price: Optional[float] = None
        self.current_date: Optional[str] = None

    def _ema(self, closes: list, period: int) -> Optional[float]:
        if len(closes) < period:
            return None
        k   = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for v in closes[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _reset_day(self) -> None:
        self.closes.clear()
        self.prev_fast   = None
        self.prev_slow   = None
        self.entry_price = None
        self.position    = 0

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._reset_day()

        self.closes.append(candle.close)
        closes = list(self.closes)
        fast   = self._ema(closes, self.fast_period)
        slow   = self._ema(closes, self.slow_period)

        # EOD exit
        if (h > EOD_EXIT_HOUR) or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            self.prev_fast, self.prev_slow = fast, slow
            return signals

        if fast is None or slow is None:
            self.prev_fast, self.prev_slow = fast, slow
            return signals

        # SL check
        if self.position == 1 and self.entry_price:
            if candle.close < self.entry_price * 0.9925:
                signals.append(self._signal("EXIT", candle.close, "SL 0.75%"))
                self.position = 0
        elif self.position == -1 and self.entry_price:
            if candle.close > self.entry_price * 1.0075:
                signals.append(self._signal("EXIT", candle.close, "SL 0.75%"))
                self.position = 0

        if self.prev_fast is not None and self.prev_slow is not None:
            prev_diff = self.prev_fast - self.prev_slow
            curr_diff = fast - slow

            if prev_diff <= 0 < curr_diff:  # golden cross
                if self.position == -1:
                    signals.append(self._signal("EXIT", candle.close, "cross flip"))
                    self.position = 0
                if self.position == 0:
                    self.entry_price = candle.close
                    self.position    = 1
                    signals.append(self._signal("BUY", candle.close,
                        f"golden cross | fast={fast:.2f} slow={slow:.2f}"))

            elif prev_diff >= 0 > curr_diff:  # death cross
                if self.position == 1:
                    signals.append(self._signal("EXIT", candle.close, "cross flip"))
                    self.position = 0
                if self.position == 0:
                    self.entry_price = candle.close
                    self.position    = -1
                    signals.append(self._signal("SELL", candle.close,
                        f"death cross | fast={fast:.2f} slow={slow:.2f}"))

        self.prev_fast, self.prev_slow = fast, slow
        return signals