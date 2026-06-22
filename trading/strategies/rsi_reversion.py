#strategies/rsi_reversion.py
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional

from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class RSIReversionStrategy(BaseStrategy):
    """
    RSI Mean Reversion — 15-min candles.
    RSI < 30 (oversold) → buy.
    RSI > 70 (overbought) → short.
    Exit when RSI crosses back through 50.
    SL = 0.8% against entry.
    """

    def __init__(
        self,
        symbol: str,
        qty: int,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ) -> None:
        super().__init__(symbol, qty)
        self.period     = period
        self.oversold   = oversold
        self.overbought = overbought
        self.closes:    deque          = deque(maxlen=period + 5)
        self.entry_price: Optional[float] = None
        self.current_date: Optional[str]  = None

    def _rsi(self, closes: list) -> Optional[float]:
        if len(closes) < self.period + 1:
            return None
        gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        avg_gain = sum(gains[-self.period:]) / self.period
        avg_loss = sum(losses[-self.period:]) / self.period
        if avg_loss == 0:
            return 100.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    def _reset_day(self) -> None:
        self.closes.clear()
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
        rsi = self._rsi(list(self.closes))

        # EOD exit
        if (h > EOD_EXIT_HOUR) or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            return signals

        if rsi is None:
            return signals

        # SL check
        if self.position == 1 and self.entry_price:
            if candle.close < self.entry_price * 0.992:
                signals.append(self._signal("EXIT", candle.close, f"SL 0.8% | rsi={rsi:.1f}"))
                self.position = 0
        elif self.position == -1 and self.entry_price:
            if candle.close > self.entry_price * 1.008:
                signals.append(self._signal("EXIT", candle.close, f"SL 0.8% | rsi={rsi:.1f}"))
                self.position = 0

        # Exit on RSI mean reversion
        if self.position == 1 and rsi > 50:
            signals.append(self._signal("EXIT", candle.close, f"RSI reverted {rsi:.1f}"))
            self.position = 0
        elif self.position == -1 and rsi < 50:
            signals.append(self._signal("EXIT", candle.close, f"RSI reverted {rsi:.1f}"))
            self.position = 0

        # Entry
        if self.position == 0:
            if rsi < self.oversold:
                self.entry_price = candle.close
                self.position    = 1
                signals.append(self._signal("BUY", candle.close, f"RSI oversold {rsi:.1f}"))
            elif rsi > self.overbought:
                self.entry_price = candle.close
                self.position    = -1
                signals.append(self._signal("SELL", candle.close, f"RSI overbought {rsi:.1f}"))

        return signals