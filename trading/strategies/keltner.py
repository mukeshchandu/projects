#strategies/keltner.py — EMA+ATR channel breakout (trend following)
from __future__ import annotations
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class KeltnerBreakoutStrategy(BaseStrategy):
    """
    Keltner channel breakout. Same shape as Bollinger but ATR-based and
    traded as TREND (breakout) not reversion — which is why it should win
    where Bollinger lost.
    BUY  when close breaks above EMA + mult*ATR.
    SELL when close breaks below EMA - mult*ATR.
    Exit when price falls back to the EMA midline. 15-min.
    """

    def __init__(self, symbol: str, qty: int,
                 ema_period: int = 20, atr_period: int = 10,
                 multiplier: float = 2.0) -> None:
        super().__init__(symbol, qty)
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.multiplier = multiplier
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._ema:         Optional[float] = None
        self._atr:         Optional[float] = None
        self._prev_close:  Optional[float] = None
        self._n:           int             = 0
        self.current_date: Optional[str]   = None

    def _reset_day(self) -> None:
        self.position = 0

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

        # ATR (Wilder) + EMA
        if self._prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(candle.high - candle.low,
                     abs(candle.high - self._prev_close),
                     abs(candle.low  - self._prev_close))
        self._atr = tr if self._atr is None else (self._atr * (self.atr_period - 1) + tr) / self.atr_period
        k = 2.0 / (self.ema_period + 1)
        self._ema = candle.close if self._ema is None else candle.close * k + self._ema * (1 - k)
        self._prev_close = candle.close
        self._n += 1
        if self._n < self.ema_period:
            return signals

        upper = self._ema + self.multiplier * self._atr
        lower = self._ema - self.multiplier * self._atr

        # Manage open position: exit back to midline
        if self.position == 1:
            if candle.close < self._ema:
                signals.append(self._signal("EXIT", candle.close, f"back to EMA mid={self._ema:.2f}"))
                self.position = 0
            return signals
        if self.position == -1:
            if candle.close > self._ema:
                signals.append(self._signal("EXIT", candle.close, f"back to EMA mid={self._ema:.2f}"))
                self.position = 0
            return signals

        # Entry on breakout
        if candle.close > upper:
            self.position = 1
            signals.append(self._signal("BUY", candle.close,
                f"break upper={upper:.2f} atr={self._atr:.2f}"))
        elif candle.close < lower:
            self.position = -1
            signals.append(self._signal("SELL", candle.close,
                f"break lower={lower:.2f} atr={self._atr:.2f}"))

        return signals
