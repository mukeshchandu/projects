#strategies/supertrend.py
from __future__ import annotations
import json, os
from typing import Any, Dict, List, Optional
from config import EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from strategies.base import BaseStrategy

STATE_DIR    = "data/st_state"
HARD_SL_MULT = 1.5           # hard stop: exit if price moves 1.5×ATR against entry
BREAKEVEN_TRIGGER_MULT = 0.5 # once +0.5×ATR in profit, move stop to entry (0 disables)
TRAIL_PEAK_MULT = 1.5        # chandelier: exit if price retraces 1.5×ATR from peak (0 disables)
TAKE_PROFIT_MULT = 0.0       # fixed take-profit at N×ATR (0 disables; caps winners — backtest first)
# Exit tuning basis (19-day tick backtest, 131 trades — see exit_backtest.py): the chandelier
# trail (1.5×ATR) is the dominant improvement, the 0.5×ATR breakeven is complementary. Together
# they took the exit from ~-2268 to ~-486 vs the old (be=1.0, no trail). Cuts big losers fast +
# locks winners near the peak instead of giving them back to the wide Supertrend trail / EOD.


class SupertrendStrategy(BaseStrategy):

    def __init__(self, symbol: str, qty: int,
                 atr_period: int = 14, multiplier: float = 1.5,
                 long_only: bool = False, ema_period: int = None) -> None:
        super().__init__(symbol, qty)
        self.atr_period = atr_period
        self.multiplier = multiplier
        self.long_only  = long_only   # CNC: no short entries, bearish flip = exit only
        self.ema_period = ema_period  # if set: only long above EMA, short below (trend filter)
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
            self.position     = s.get("position", 0)
            self.current_date = s.get("current_date")
            self._entry_price = s.get("entry_price")
            self._entry_atr   = s.get("entry_atr")
            self._peak        = s.get("peak")
            self._breakeven_armed = s.get("breakeven_armed", False)
            self._ema         = s.get("ema")
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
            "position":    self.position,
            "current_date": self.current_date,
            "entry_price": self._entry_price,
            "entry_atr":   self._entry_atr,
            "peak":        self._peak,
            "breakeven_armed": self._breakeven_armed,
            "ema":         self._ema,
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
        self._peak:        Optional[float] = None   # best price seen since entry
        self._breakeven_armed: bool        = False
        self._ema:         Optional[float] = None   # EMA trend filter value

    def _reset_day(self) -> None:
        if self.long_only:
            return   # CNC held overnight — do not flatten across the day boundary
        self.position     = 0
        self._entry_price = None
        self._entry_atr   = None
        self._peak        = None
        self._breakeven_armed = False

    def _tr(self, candle: Candle) -> float:
        if not self._candles:
            return candle.high - candle.low
        p = self._candles[-1]
        return max(candle.high - candle.low,
                   abs(candle.high - p.close),
                   abs(candle.low  - p.close))

    def _exit_now(self, price: float, why: str) -> Dict[str, Any]:
        sig = self._signal("EXIT", price, why)
        self.position     = 0
        self._entry_price = None
        self._entry_atr   = None
        self._peak        = None
        self._breakeven_armed = False
        return sig

    def check_stops(self, price: float) -> Optional[Dict[str, Any]]:
        """Real-time exit checks — safe to call on every tick AND at candle close.
        Returns an EXIT signal (and flattens internal state) if a stop fires, else None.
        Priority: hard SL → breakeven → peak-trail → take-profit → trailing supertrend."""
        if self.position == 0 or self._entry_price is None:
            return None
        atr   = self._entry_atr or self._atr or 0.0
        entry = self._entry_price

        # track best price seen since entry
        if self._peak is None:
            self._peak = price
        elif self.position == 1:
            self._peak = max(self._peak, price)
        else:
            self._peak = min(self._peak, price)

        if self.position == 1:
            if atr and price <= entry - HARD_SL_MULT * atr:
                return self._exit_now(price, f"HARD SL | entry={entry:.2f} atr={atr:.2f}")
            if atr and BREAKEVEN_TRIGGER_MULT and price >= entry + BREAKEVEN_TRIGGER_MULT * atr:
                self._breakeven_armed = True
            if self._breakeven_armed and price <= entry:
                return self._exit_now(price, f"BREAKEVEN | entry={entry:.2f}")
            if atr and TRAIL_PEAK_MULT and self._peak is not None and price <= self._peak - TRAIL_PEAK_MULT * atr:
                return self._exit_now(price, f"PEAK TRAIL | peak={self._peak:.2f} atr={atr:.2f}")
            if atr and TAKE_PROFIT_MULT and price >= entry + TAKE_PROFIT_MULT * atr:
                return self._exit_now(price, f"TAKE PROFIT | entry={entry:.2f}")
            if self._supertrend is not None and price < self._supertrend:
                return self._exit_now(price, f"trail SL | st={self._supertrend:.2f}")
        else:  # short
            if atr and price >= entry + HARD_SL_MULT * atr:
                return self._exit_now(price, f"HARD SL | entry={entry:.2f} atr={atr:.2f}")
            if atr and BREAKEVEN_TRIGGER_MULT and price <= entry - BREAKEVEN_TRIGGER_MULT * atr:
                self._breakeven_armed = True
            if self._breakeven_armed and price >= entry:
                return self._exit_now(price, f"BREAKEVEN | entry={entry:.2f}")
            if atr and TRAIL_PEAK_MULT and self._peak is not None and price >= self._peak + TRAIL_PEAK_MULT * atr:
                return self._exit_now(price, f"PEAK TRAIL | peak={self._peak:.2f} atr={atr:.2f}")
            if atr and TAKE_PROFIT_MULT and price <= entry - TAKE_PROFIT_MULT * atr:
                return self._exit_now(price, f"TAKE PROFIT | entry={entry:.2f}")
            if self._supertrend is not None and price > self._supertrend:
                return self._exit_now(price, f"trail SL | st={self._supertrend:.2f}")
        return None

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
                self._peak        = None
                self._breakeven_armed = False
            self.save_state()
            return signals

        if self.ema_period:
            k = 2.0 / (self.ema_period + 1)
            self._ema = candle.close if self._ema is None else candle.close * k + self._ema * (1 - k)

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

        # ── Stop management (identical code path used on every live tick) ──
        exit_sig = self.check_stops(candle.close)
        if exit_sig:
            signals.append(exit_sig)
            self.save_state()
            return signals   # exited this candle — no re-entry on the same bar

        # ── Entry on trend flip (optionally gated by EMA trend filter) ──
        if prev_trend != 0 and new_trend != prev_trend and self.position == 0:
            long_ok  = self._ema is None or candle.close > self._ema
            short_ok = self._ema is None or candle.close < self._ema
            if new_trend == 1 and long_ok:
                self.position     = 1
                self._entry_price = candle.close
                self._entry_atr   = self._atr
                self._peak        = candle.close
                self._breakeven_armed = False
                signals.append(self._signal("BUY", candle.close,
                    f"flip UP | atr={self._atr:.2f} st={new_st:.2f} sl={candle.close - HARD_SL_MULT*self._atr:.2f}"))
            elif new_trend == -1 and not self.long_only and short_ok:
                self.position     = -1
                self._entry_price = candle.close
                self._entry_atr   = self._atr
                self._peak        = candle.close
                self._breakeven_armed = False
                signals.append(self._signal("SELL", candle.close,
                    f"flip DOWN | atr={self._atr:.2f} st={new_st:.2f} sl={candle.close + HARD_SL_MULT*self._atr:.2f}"))

        # Persist after every closed candle so a mid-day restart restores position + entry_atr
        self.save_state()
        return signals
