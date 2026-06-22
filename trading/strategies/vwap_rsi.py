#strategies/vwap_rsi.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class VWAPRSIStrategy(BaseStrategy):
    """
    Dual-confirmation reversion: price must be on wrong side of VWAP AND RSI extreme.
    BUY  when price < VWAP AND RSI < rsi_lo  (double oversold)
    SELL when price > VWAP AND RSI > rsi_hi  (double overbought)
    Exit when price crosses back to VWAP or SL hit.
    Tighter entries than either indicator alone.
    """

    def __init__(self, symbol: str, qty: int,
                 rsi_period: int = 14,
                 rsi_lo: float = 40.0,
                 rsi_hi: float = 60.0,
                 min_vwap_dev: float = 0.003,
                 sl_pct: float = 0.008) -> None:
        super().__init__(symbol, qty)
        self.rsi_period   = rsi_period
        self.rsi_lo       = rsi_lo
        self.rsi_hi       = rsi_hi
        self.min_vwap_dev = min_vwap_dev
        self.sl_pct       = sl_pct
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._closes:      List[float]     = []
        self._cum_pv:      float           = 0.0
        self._cum_v:       float           = 0.0
        self._vwap:        Optional[float] = None
        self._n:           int             = 0
        self.current_date: Optional[str]   = None
        self.sl:           Optional[float] = None

    def _reset_day(self) -> None:
        self._cum_pv = 0.0
        self._cum_v  = 0.0
        self._vwap   = None
        self._n      = 0
        self.position = 0
        self.sl       = None

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

        # Update VWAP (use volume=1 fallback for index data)
        tp   = (candle.high + candle.low + candle.close) / 3
        vol  = candle.volume if candle.volume > 0 else 1.0
        self._cum_pv += tp * vol
        self._cum_v  += vol
        self._vwap    = self._cum_pv / self._cum_v
        self._n      += 1

        self._closes.append(candle.close)
        if len(self._closes) > self.rsi_period + 20:
            self._closes.pop(0)

        if self._n < self.rsi_period + 2:
            return signals

        rsi = self._rsi()
        dev = (candle.close - self._vwap) / self._vwap

        # Manage open position
        if self.position == 1:
            if candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | rsi={rsi:.1f}"))
                self.position = 0
            elif candle.close >= self._vwap:
                signals.append(self._signal("EXIT", candle.close,
                    f"returned to VWAP | rsi={rsi:.1f} vwap={self._vwap:.2f}"))
                self.position = 0
            return signals

        if self.position == -1:
            if candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | rsi={rsi:.1f}"))
                self.position = 0
            elif candle.close <= self._vwap:
                signals.append(self._signal("EXIT", candle.close,
                    f"returned to VWAP | rsi={rsi:.1f} vwap={self._vwap:.2f}"))
                self.position = 0
            return signals

        # Entry
        if dev < -self.min_vwap_dev and rsi < self.rsi_lo:
            self.sl       = candle.close * (1 - self.sl_pct)
            self.position = 1
            signals.append(self._signal("BUY", candle.close,
                f"below VWAP {dev*100:.2f}% + RSI {rsi:.1f} | vwap={self._vwap:.2f}"))

        elif dev > self.min_vwap_dev and rsi > self.rsi_hi:
            self.sl       = candle.close * (1 + self.sl_pct)
            self.position = -1
            signals.append(self._signal("SELL", candle.close,
                f"above VWAP {dev*100:.2f}% + RSI {rsi:.1f} | vwap={self._vwap:.2f}"))

        return signals
