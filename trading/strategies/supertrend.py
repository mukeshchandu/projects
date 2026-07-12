#strategies/supertrend.py
from __future__ import annotations
import json, os
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy

STATE_DIR    = "data/st_state"
HARD_SL_MULT = 1.5   # exit if price moves 1.5×ATR against entry price


class SupertrendStrategy(BaseStrategy):

    def __init__(self, symbol: str, qty: int,
                 atr_period: int = 14, multiplier: float = 1.5) -> None:
        super().__init__(symbol, qty)
        self.atr_period = atr_period
        self.multiplier = multiplier
        self._reset_all()
        self._load_state()

    def _state_path(self) -> str:
        os.makedirs(STATE_DIR, exist_ok=True)
        return f"{STATE_DIR}/{self.symbol}.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if not os.path.exists(path):
            return
        try:
            s = json.load(open(path))
            self._atr        = s.get("atr")
            self._upper      = s.get("upper")
            self._lower      = s.get("lower")
            self._supertrend = s.get("supertrend")
            self._trend      = s.get("trend", 0)
            self._entry_price = s.get("entry_price")
            self._entry_atr   = s.get("entry_atr")
            candles_raw      = s.get("candles", [])
            from marketdata import Candle
            from datetime import datetime
            from config import IST
            self._candles = []
            for c in candles_raw:
                self._candles.append(Candle(
                    start  = datetime.fromtimestamp(c["ts"], tz=IST),
                    open   = c["o"], high = c["h"],
                    low    = c["l"], close = c["c"]
                ))
            print(f"[ST] {self.symbol} loaded state — atr={self._atr:.4f} trend={self._trend} candles={len(self._candles)}")
        except Exception as e:
            print(f"[ST] {self.symbol} state load failed: {e} — starting fresh")
            self._reset_all()

    def save_state(self) -> None:
        path = self._state_path()
        s = {
            "atr":         self._atr,
            "upper":       self._upper,
            "lower":       self._lower,
            "supertrend":  self._supertrend,
            "trend":       self._trend,
            "entry_price": self._entry_price,
            "entry_atr":   self._entry_atr,
            "candles": [
                {"ts": int(c.start.timestamp()),
                 "o": c.open, "h": c.high, "l": c.low, "c": c.close}
                for c in self._candles
            ]
        }
        json.dump(s, open(path, "w"))

    def reset(self) -> None:
        super().reset()
        self._reset_all()

    def _reset_all(self) -> None:
        self._candles:     List[Candle]    = []
        self._atr:         Optional[float] = None
        self._upper:       Optional[float] = None
        self._lower:       Optional[float] = None
        self._supertrend:  Optional[float] = None
        self._trend:       int             = 0
        self.current_date: Optional[str]   = None
        self._entry_price: Optional[float] = None
        self._entry_atr:   Optional[float] = None

    def _reset_day(self) -> None:
        self.position     = 0
        self._entry_price = None
        self._entry_atr   = None

    def _tr(self, candle: Candle) -> float:
        if not self._candles:
            return candle.high - candle.low
        p = self._candles[-1]
        return max(candle.high - candle.low,
                   abs(candle.high - p.close),
                   abs(candle.low  - p.close))

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
                self.position     = 0
                self._entry_price = None
                self._entry_atr   = None
            self.save_state()
            return signals

        tr = self._tr(candle)
        self._atr = tr if self._atr is None else (self._atr * (self.atr_period - 1) + tr) / self.atr_period
        self._candles.append(candle)
        if len(self._candles) > self.atr_period + 5:
            self._candles.pop(0)
        if len(self._candles) < self.atr_period:
            return signals

        hl2         = (candle.high + candle.low) / 2
        basic_upper = hl2 + self.multiplier * self._atr
        basic_lower = hl2 - self.multiplier * self._atr
        prev_close  = self._candles[-2].close if len(self._candles) >= 2 else candle.close

        if self._upper is None:
            upper, lower = basic_upper, basic_lower
        else:
            upper = basic_upper if (basic_upper < self._upper or prev_close > self._upper) else self._upper
            lower = basic_lower if (basic_lower > self._lower or prev_close < self._lower) else self._lower

        if self._supertrend is None:
            new_st, new_trend = (lower, 1) if candle.close > hl2 else (upper, -1)
        elif self._supertrend == self._upper:
            new_st, new_trend = (lower, 1) if candle.close > upper else (upper, -1)
        else:
            new_st, new_trend = (upper, -1) if candle.close < lower else (lower, 1)

        prev_trend       = self._trend
        self._trend      = new_trend
        self._supertrend = new_st
        self._upper      = upper
        self._lower      = lower

        # ── Hard ATR stop loss (fires before trailing SL) ──────────────
        hard_sl_hit = False
        if self.position == 1 and self._entry_price is not None and self._entry_atr is not None:
            sl = self._entry_price - HARD_SL_MULT * self._entry_atr
            if candle.close < sl:
                signals.append(self._signal("EXIT", candle.close,
                    f"HARD SL | entry={self._entry_price:.2f} sl={sl:.2f} atr={self._entry_atr:.2f}"))
                self.position     = 0
                self._entry_price = None
                self._entry_atr   = None
                hard_sl_hit       = True

        elif self.position == -1 and self._entry_price is not None and self._entry_atr is not None:
            sl = self._entry_price + HARD_SL_MULT * self._entry_atr
            if candle.close > sl:
                signals.append(self._signal("EXIT", candle.close,
                    f"HARD SL | entry={self._entry_price:.2f} sl={sl:.2f} atr={self._entry_atr:.2f}"))
                self.position     = 0
                self._entry_price = None
                self._entry_atr   = None
                hard_sl_hit       = True

        if hard_sl_hit:
            return signals   # skip trailing SL + re-entry on same candle

        # ── Trailing supertrend SL ──────────────────────────────────────
        if self.position == 1 and candle.close < new_st:
            signals.append(self._signal("EXIT", new_st, f"trail SL | st={new_st:.2f}"))
            self.position     = 0
            self._entry_price = None
            self._entry_atr   = None
        elif self.position == -1 and candle.close > new_st:
            signals.append(self._signal("EXIT", new_st, f"trail SL | st={new_st:.2f}"))
            self.position     = 0
            self._entry_price = None
            self._entry_atr   = None

        # ── Entry on trend flip ─────────────────────────────────────────
        if prev_trend != 0 and new_trend != prev_trend and self.position == 0:
            if new_trend == 1:
                self.position     = 1
                self._entry_price = candle.close
                self._entry_atr   = self._atr
                signals.append(self._signal("BUY", candle.close,
                    f"flip UP | atr={self._atr:.2f} st={new_st:.2f} sl={candle.close - HARD_SL_MULT*self._atr:.2f}"))
            else:
                self.position     = -1
                self._entry_price = candle.close
                self._entry_atr   = self._atr
                signals.append(self._signal("SELL", candle.close,
                    f"flip DOWN | atr={self._atr:.2f} st={new_st:.2f} sl={candle.close + HARD_SL_MULT*self._atr:.2f}"))

        return signals
