# Changelog

_All entries authored by Claude (AI assistant). Most recent on top._

## 2026-07-15 — Real-time strategy-state log
Each closed 15-min candle now writes the live strategy state to `logs/strategy_<date>.csv`
(time, OHLC, atr, supertrend, trend, upper, lower, ema, position, signal, reason). This is
the ground truth of what the strategy saw and decided, so behaviour can be reviewed/charted
from logged values instead of recomputing (which risked warm-up leaks / replay drift).
