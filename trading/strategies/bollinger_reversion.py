#strategies/bollinger_reversion.py
from __future__ import annotations
import statistics
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class BollingerReversionStrategy(BaseStrategy):
    """
    Bollinger Band mean reversion on 5-min candles.
    BUY  when close breaks below lower band  → reversion to middle expected.
    SELL when close breaks above upper band  → reversion to middle expected.
    Exit at middle band (20-MA) or SL. Uses price-based signal unlike RSI.
    """

    def __init__(self, symbol: str, qty: int,
                 period: int = 20, std_dev: float = 2.0, sl_pct: float = 0.01) -> None:
        super().__init__(symbol, qty)
        self.period  = period
        self.std_dev = std_dev
        self.sl_pct  = sl_pct
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._closes:      List[float]     = []   # carries across days (needs history)
        self.current_date: Optional[str]   = None
        self.sl:           Optional[float] = None

    def _reset_day(self) -> None:
        self.position = 0
        self.sl       = None

    def _bands(self):
        if len(self._closes) < self.period:
            return None, None, None
        recent = self._closes[-self.period:]
        mid    = sum(recent) / self.period
        std    = statistics.pstdev(recent)   # population stdev (no sample correction)
        if std == 0:
            return None, None, None
        return mid + self.std_dev * std, mid, mid - self.std_dev * std

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
            return signals

        self._closes.append(candle.close)
        if len(self._closes) > self.period + 100:
            self._closes.pop(0)

        upper, mid, lower = self._bands()
        if upper is None:
            return signals

        # Manage open position: exit at middle band
        if self.position == 1:
            if candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl,
                    f"SL | mid={mid:.2f}"))
                self.position = 0
            elif candle.close >= mid:
                signals.append(self._signal("EXIT", candle.close,
                    f"mid-band exit | mid={mid:.2f}"))
                self.position = 0
            return signals

        if self.position == -1:
            if candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl,
                    f"SL | mid={mid:.2f}"))
                self.position = 0
            elif candle.close <= mid:
                signals.append(self._signal("EXIT", candle.close,
                    f"mid-band exit | mid={mid:.2f}"))
                self.position = 0
            return signals

        # Entry
        if candle.close < lower:
            self.sl       = candle.close * (1 - self.sl_pct)
            self.position = 1
            signals.append(self._signal("BUY", candle.close,
                f"below lower BB | upper={upper:.2f} lower={lower:.2f} mid={mid:.2f}"))

        elif candle.close > upper:
            self.sl       = candle.close * (1 + self.sl_pct)
            self.position = -1
            signals.append(self._signal("SELL", candle.close,
                f"above upper BB | upper={upper:.2f} lower={lower:.2f} mid={mid:.2f}"))

        return signals
