#strategies/vwap_breakout.py — NEW: breakout + VWAP + volume confirmation
from __future__ import annotations
from typing import Any, Dict, List, Optional
from collections import deque
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class VWAPBreakoutStrategy(BaseStrategy):
    """
    Triple-confirmation breakout:
    1. Price breaks above previous day high (or below previous day low)
    2. Price is above (or below) VWAP — trend alignment
    3. Current candle volume > 1.5× rolling 20-bar average

    SL = VWAP (dynamic). Target = 2× range from entry to PDH/PDL.
    One trade per day. 15-min timeframe.
    """

    def __init__(self, symbol: str, qty: int,
                 vol_multiplier: float = 1.5,
                 target_mult: float    = 2.0) -> None:
        super().__init__(symbol, qty)
        self.vol_multiplier = vol_multiplier
        self.target_mult    = target_mult
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self.pdh:           Optional[float] = None
        self.pdl:           Optional[float] = None
        self._day_high:     Optional[float] = None
        self._day_low:      Optional[float] = None
        self._cum_pv:       float           = 0.0
        self._cum_v:        float           = 0.0
        self._vwap:         Optional[float] = None
        self._volumes:      deque           = deque(maxlen=20)
        self.entry_price:   Optional[float] = None
        self.target:        Optional[float] = None
        self.sl:            Optional[float] = None
        self.current_date:  Optional[str]   = None
        self.traded_today:  bool            = False

    def _new_day(self) -> None:
        if self._day_high is not None:
            self.pdh = self._day_high
            self.pdl = self._day_low
        self._day_high    = None
        self._day_low     = None
        self._cum_pv      = 0.0
        self._cum_v       = 0.0
        self._vwap        = None
        self.traded_today = False
        self.entry_price  = None
        self.target       = None
        self.sl           = None
        self.position     = 0

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._new_day()

        self._day_high = candle.high if self._day_high is None else max(self._day_high, candle.high)
        self._day_low  = candle.low  if self._day_low  is None else min(self._day_low,  candle.low)

        tp  = (candle.high + candle.low + candle.close) / 3
        vol = candle.volume if candle.volume > 0 else 1.0
        self._cum_pv += tp * vol
        self._cum_v  += vol
        self._vwap    = self._cum_pv / self._cum_v
        self._volumes.append(vol)

        if h > EOD_EXIT_HOUR or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            return signals

        # Manage open position
        if self.position == 1:
            if candle.high >= self.target:
                signals.append(self._signal("EXIT", self.target, "target"))
                self.position = 0
            elif candle.low <= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | vwap={self._vwap:.2f}"))
                self.position = 0
            else:
                self.sl = max(self.sl, self._vwap)  # trail SL up to VWAP
            return signals

        if self.position == -1:
            if candle.low <= self.target:
                signals.append(self._signal("EXIT", self.target, "target"))
                self.position = 0
            elif candle.high >= self.sl:
                signals.append(self._signal("EXIT", self.sl, f"SL | vwap={self._vwap:.2f}"))
                self.position = 0
            else:
                self.sl = min(self.sl, self._vwap)
            return signals

        if self.traded_today or self.pdh is None or len(self._volumes) < 10:
            return signals

        avg_vol  = sum(self._volumes) / len(self._volumes)
        vol_ok   = vol >= self.vol_multiplier * avg_vol

        # BUY: break above PDH + price above VWAP + volume spike
        if candle.close > self.pdh and candle.close > self._vwap and vol_ok:
            entry            = candle.close
            self.entry_price = entry
            self.sl          = self._vwap
            distance         = entry - self._vwap
            self.target      = entry + self.target_mult * distance
            self.position    = 1
            self.traded_today = True
            signals.append(self._signal("BUY", entry,
                f"PDH break + above VWAP + vol {vol/avg_vol:.1f}× | tgt={self.target:.2f}"))

        elif candle.close < self.pdl and candle.close < self._vwap and vol_ok:
            entry            = candle.close
            self.entry_price = entry
            self.sl          = self._vwap
            distance         = self._vwap - entry
            self.target      = entry - self.target_mult * distance
            self.position    = -1
            self.traded_today = True
            signals.append(self._signal("SELL", entry,
                f"PDL break + below VWAP + vol {vol/avg_vol:.1f}× | tgt={self.target:.2f}"))

        return signals
