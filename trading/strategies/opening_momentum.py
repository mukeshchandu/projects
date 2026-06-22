#strategies/opening_momentum.py — reverted to proven v1 (target 2x, no entry filters)
from __future__ import annotations
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class OpeningMomentumStrategy(BaseStrategy):
    """
    First 5-min candle direction sets the day's trade.
    Bullish first candle (close > open) → BUY at open of next candle.
    Bearish first candle → SELL at open of next candle.
    SL = first candle's opposite extreme. Target = target_mult x range.
    One trade per day. (v2 entry filters removed — they regressed P&L.)
    """

    def __init__(self, symbol: str, qty: int, target_mult: float = 2.0) -> None:
        super().__init__(symbol, qty)
        self.target_mult = target_mult
        self._reset_all()

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._first_candle: Optional[Candle] = None
        self._entered:      bool             = False
        self.target:        Optional[float]  = None
        self.sl:            Optional[float]  = None
        self.current_date:  Optional[str]    = None

    def _reset_day(self) -> None:
        self._first_candle = None
        self._entered      = False
        self.target        = None
        self.sl            = None
        self.position      = 0

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

        if h == 9 and m == 15 and self._first_candle is None:
            self._first_candle = candle
            return signals

        if self._first_candle is None or self._entered:
            if self.position == 1:
                if candle.high >= self.target:
                    signals.append(self._signal("EXIT", self.target, "target hit"))
                    self.position = 0
                elif candle.low <= self.sl:
                    signals.append(self._signal("EXIT", self.sl, "SL hit"))
                    self.position = 0
            elif self.position == -1:
                if candle.low <= self.target:
                    signals.append(self._signal("EXIT", self.target, "target hit"))
                    self.position = 0
                elif candle.high >= self.sl:
                    signals.append(self._signal("EXIT", self.sl, "SL hit"))
                    self.position = 0
            return signals

        if not (h == 9 and m == 20):
            return signals

        fc       = self._first_candle
        fc_range = fc.high - fc.low
        if fc_range <= 0:
            return signals
        self._entered = True

        if fc.close > fc.open:
            entry         = candle.open
            self.sl       = fc.low
            self.target   = entry + self.target_mult * fc_range
            self.position = 1
            signals.append(self._signal("BUY", entry,
                f"bull open | range={fc_range:.2f} tgt={self.target:.2f} sl={self.sl:.2f}"))
        elif fc.close < fc.open:
            entry         = candle.open
            self.sl       = fc.high
            self.target   = entry - self.target_mult * fc_range
            self.position = -1
            signals.append(self._signal("SELL", entry,
                f"bear open | range={fc_range:.2f} tgt={self.target:.2f} sl={self.sl:.2f}"))

        return signals
