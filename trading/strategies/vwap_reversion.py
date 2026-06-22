#strategies/vwap_reversion.py
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy


class VWAPReversionStrategy(BaseStrategy):
    """
    VWAP Reversion — 5-min candles.
    Buy when price is deviation_pct% below VWAP.
    Short when price is deviation_pct% above VWAP.
    Exit on VWAP touch or 0.5% SL.
    Trades only between 10:00 and 14:00 IST.
    Uses TWAP fallback when volume = 0 (common on index data).
    """

    def __init__(self, symbol: str, qty: int, deviation_pct: float = 0.5) -> None:
        super().__init__(symbol, qty)
        self.deviation_pct  = deviation_pct
        self.cum_tp_vol:    float          = 0.0
        self.cum_vol:       float          = 0.0
        self.candle_count:  int            = 0
        self.cum_tp:        float          = 0.0
        self.vwap:          float          = 0.0
        self.entry_price:   Optional[float] = None
        self.current_date:  Optional[str]  = None

    def _reset_day(self) -> None:
        self.cum_tp_vol  = 0.0
        self.cum_vol     = 0.0
        self.candle_count = 0
        self.cum_tp      = 0.0
        self.vwap        = 0.0
        self.entry_price = None
        self.position    = 0

    def on_candle(self, candle: Candle) -> List[Dict[str, Any]]:
        signals: List[Dict[str, Any]] = []
        date = candle.start.strftime("%Y-%m-%d")
        h, m = candle.start.hour, candle.start.minute

        if self.current_date != date:
            self.current_date = date
            self._reset_day()

        tp  = (candle.high + candle.low + candle.close) / 3
        vol = candle.volume if candle.volume > 0 else 0.0

        self.cum_tp_vol   += tp * vol
        self.cum_vol      += vol
        self.candle_count += 1
        self.cum_tp       += tp

        # VWAP if volume available, else TWAP
        if self.cum_vol > 0:
            self.vwap = self.cum_tp_vol / self.cum_vol
        else:
            self.vwap = self.cum_tp / self.candle_count

        # EOD exit
        if (h > EOD_EXIT_HOUR) or (h == EOD_EXIT_HOUR and m >= EOD_EXIT_MINUTE):
            if self.position != 0:
                signals.append(self._signal("EXIT", candle.close, "EOD exit"))
                self.position = 0
            return signals

        # Only trade 10:00–14:00
        if h < 10 or h >= 14:
            return signals

        deviation = (candle.close - self.vwap) / self.vwap * 100

        if self.position == 1:
            if candle.close >= self.vwap:
                signals.append(self._signal("EXIT", candle.close,
                    f"VWAP touch | dev={deviation:.2f}%"))
                self.position = 0
            elif self.entry_price and candle.close < self.entry_price * 0.995:
                signals.append(self._signal("EXIT", candle.close, "SL 0.5%"))
                self.position = 0

        elif self.position == -1:
            if candle.close <= self.vwap:
                signals.append(self._signal("EXIT", candle.close,
                    f"VWAP touch | dev={deviation:.2f}%"))
                self.position = 0
            elif self.entry_price and candle.close > self.entry_price * 1.005:
                signals.append(self._signal("EXIT", candle.close, "SL 0.5%"))
                self.position = 0

        elif self.position == 0:
            if deviation < -self.deviation_pct:
                self.entry_price = candle.close
                self.position    = 1
                signals.append(self._signal("BUY", candle.close,
                    f"below VWAP {deviation:.2f}% | vwap={self.vwap:.2f}"))
            elif deviation > self.deviation_pct:
                self.entry_price = candle.close
                self.position    = -1
                signals.append(self._signal("SELL", candle.close,
                    f"above VWAP {deviation:.2f}% | vwap={self.vwap:.2f}"))

        return signals