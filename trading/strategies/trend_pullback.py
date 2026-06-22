#strategies/trend_pullback.py — NEW: buy dips in established uptrends
from __future__ import annotations
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class TrendPullbackStrategy(BaseStrategy):
    """
    Buy pullbacks in uptrends, sell rallies in downtrends.
    - Trend: EMA(20) > EMA(50) = uptrend ; reverse for down.
    - Entry: RSI dips < 35 in uptrend (oversold pullback) → BUY
             RSI spikes > 65 in downtrend (overbought rally) → SELL
    - Exit: RSI returns to 50 OR price hits SL.
    5-min timeframe.
    """

    def __init__(self, symbol: str, qty: int,
                 ema_fast: int = 20, ema_slow: int = 50,
                 rsi_period: int = 14,
                 rsi_oversold: float = 35.0,
                 rsi_overbought: float = 65.0,
                 sl_pct: float = 0.008) -> None:
        super().__init__(symbol, qty)
        self.ema_fast       = ema_fast
        self.ema_slow       = ema_slow
        self.rsi_period     = rsi_period
        self.rsi_oversold   = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.sl_pct         = sl_pct
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._closes:      List[float]     = []
        self._ema_f:       Optional[float] = None
        self._ema_s:       Optional[float] = None
        self.sl:           Optional[float] = None
        self.current_date: Optional[str]   = None

    def _reset_day(self) -> None:
        self.position = 0
        self.sl       = None

    def _update_ema(self, price: float) -> None:
        k_f = 2.0 / (self.ema_fast + 1)
        k_s = 2.0 / (self.ema_slow + 1)
        self._ema_f = price if self._ema_f is None else price * k_f + self._ema_f * (1 - k_f)
        self._ema_s = price if self._ema_s is None else price * k_s + self._ema_s * (1 - k_s)

    def _rsi(self) -> float:
        if len(self._closes) < self.rsi_period + 1:
            return 50.0
        diffs  = [self._closes[i] - self._closes[i - 1] for i in range(-self.rsi_period, 0)]
        gains  = sum(max(d, 0) for d in diffs) / self.rsi_period
        losses = sum(max(-d, 0) for d in diffs) / self.rsi_period
        if losses == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + gains / losses)

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

        self._update_ema(candle.close)
        self._closes.append(candle.close)
        if len(self._closes) > self.rsi_period + 50:
            self._closes.pop(0)

        if self._ema_s is None or len(self._closes) < max(self.rsi_period + 2, self.ema_slow):
            return signals

        rsi      = self._rsi()
        uptrend  = self._ema_f > self._ema_s
        dntrend  = self._ema_f < self._ema_s

        # Manage open position
        if self.position == 1:
            if candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | rsi={rsi:.1f}"))
                self.position = 0
            elif rsi >= 50:
                signals.append(self._signal("EXIT", candle.close, f"RSI back to neutral={rsi:.1f}"))
                self.position = 0
            return signals

        if self.position == -1:
            if candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | rsi={rsi:.1f}"))
                self.position = 0
            elif rsi <= 50:
                signals.append(self._signal("EXIT", candle.close, f"RSI back to neutral={rsi:.1f}"))
                self.position = 0
            return signals

        # Entry
        if uptrend and rsi < self.rsi_oversold:
            self.sl       = candle.close * (1 - self.sl_pct)
            self.position = 1
            signals.append(self._signal("BUY", candle.close,
                f"pullback in uptrend | rsi={rsi:.1f} ema_f={self._ema_f:.2f} ema_s={self._ema_s:.2f}"))

        elif dntrend and rsi > self.rsi_overbought:
            self.sl       = candle.close * (1 + self.sl_pct)
            self.position = -1
            signals.append(self._signal("SELL", candle.close,
                f"rally in downtrend | rsi={rsi:.1f} ema_f={self._ema_f:.2f} ema_s={self._ema_s:.2f}"))

        return signals
