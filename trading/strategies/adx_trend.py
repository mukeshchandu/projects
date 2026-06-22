#strategies/adx_trend.py — ADX-filtered directional trend
from __future__ import annotations
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class ADXTrendStrategy(BaseStrategy):
    """
    Trade only when trend is strong. ADX measures trend STRENGTH;
    +DI/-DI give DIRECTION.
    BUY  when ADX > threshold AND +DI > -DI  (strong uptrend).
    SELL when ADX > threshold AND -DI > +DI  (strong downtrend).
    Exit when ADX weakens below adx_exit, DI crosses against, or SL.
    The ADX gate filters out chop — fewer but higher-quality trades. 15-min.
    """

    def __init__(self, symbol: str, qty: int,
                 period: int = 14,
                 adx_threshold: float = 25.0,
                 adx_exit: float = 20.0,
                 sl_pct: float = 0.01) -> None:
        super().__init__(symbol, qty)
        self.period        = period
        self.adx_threshold = adx_threshold
        self.adx_exit      = adx_exit
        self.sl_pct        = sl_pct
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._prev_high:   Optional[float] = None
        self._prev_low:    Optional[float] = None
        self._prev_close:  Optional[float] = None
        self._atr:         Optional[float] = None
        self._plus_dm:     Optional[float] = None
        self._minus_dm:    Optional[float] = None
        self._adx:         Optional[float] = None
        self._n:           int             = 0
        self.sl:           Optional[float] = None
        self.current_date: Optional[str]   = None

    def _reset_day(self) -> None:
        self.position = 0
        self.sl       = None

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
            self._prev_high, self._prev_low, self._prev_close = candle.high, candle.low, candle.close
            return signals

        if self._prev_close is None:
            self._prev_high, self._prev_low, self._prev_close = candle.high, candle.low, candle.close
            return signals

        # Directional movement
        up_move   = candle.high - self._prev_high
        down_move = self._prev_low - candle.low
        plus_dm   = up_move   if (up_move > down_move and up_move > 0)   else 0.0
        minus_dm  = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(candle.high - candle.low,
                 abs(candle.high - self._prev_close),
                 abs(candle.low  - self._prev_close))

        p = self.period
        self._atr      = tr       if self._atr      is None else (self._atr      * (p - 1) + tr)       / p
        self._plus_dm  = plus_dm  if self._plus_dm  is None else (self._plus_dm  * (p - 1) + plus_dm)  / p
        self._minus_dm = minus_dm if self._minus_dm is None else (self._minus_dm * (p - 1) + minus_dm) / p
        self._prev_high, self._prev_low, self._prev_close = candle.high, candle.low, candle.close
        self._n += 1

        if self._atr == 0:
            return signals

        plus_di  = 100.0 * self._plus_dm  / self._atr
        minus_di = 100.0 * self._minus_dm / self._atr
        denom    = plus_di + minus_di
        dx       = 100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0
        self._adx = dx if self._adx is None else (self._adx * (p - 1) + dx) / p

        if self._n < 2 * p:   # ADX needs ~2x period to stabilize
            return signals
        adx = self._adx

        # Manage open position
        if self.position == 1:
            if candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | adx={adx:.0f}"))
                self.position = 0
            elif adx < self.adx_exit or minus_di > plus_di:
                signals.append(self._signal("EXIT", candle.close, f"trend weak adx={adx:.0f}"))
                self.position = 0
            return signals
        if self.position == -1:
            if candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | adx={adx:.0f}"))
                self.position = 0
            elif adx < self.adx_exit or plus_di > minus_di:
                signals.append(self._signal("EXIT", candle.close, f"trend weak adx={adx:.0f}"))
                self.position = 0
            return signals

        # Entry — only in strong trend
        if adx > self.adx_threshold:
            if plus_di > minus_di:
                self.sl       = candle.close * (1 - self.sl_pct)
                self.position = 1
                signals.append(self._signal("BUY", candle.close,
                    f"strong uptrend adx={adx:.0f} +DI={plus_di:.0f} -DI={minus_di:.0f}"))
            elif minus_di > plus_di:
                self.sl       = candle.close * (1 + self.sl_pct)
                self.position = -1
                signals.append(self._signal("SELL", candle.close,
                    f"strong downtrend adx={adx:.0f} +DI={plus_di:.0f} -DI={minus_di:.0f}"))

        return signals
