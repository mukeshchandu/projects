#!/usr/bin/env python3
"""
replay_ticks.py
===============
Replay today's ticks.jsonl through CandleBuilder + SupertrendStrategy
to build ATR state so tomorrow's runner needs zero warmup.

Run after market close: python replay_ticks.py
"""

import json
import os
import sys
from datetime import datetime

from config import IST
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

# Token → symbol (from today's SearchScrip resolve)
TOKEN_MAP = {
    "14366": "IDEA",
    "12018": "SUZLON",
    "11915": "YESBANK",
    "17400": "NHPC",
    "2963":  "SAIL",
    "10666": "PNB",
    "15259": "RPOWER",
    "3499":  "TATASTEEL",
    "11184": "IDFCFIRSTB",
}

INTERVAL_S = 900  # 15-min candles

today     = datetime.now(tz=IST).strftime("%Y-%m-%d")
tick_file = f"data/{today}/ticks.jsonl"

if not os.path.exists(tick_file):
    print(f"No tick file found: {tick_file}")
    sys.exit(1)

# ── Load and sort ticks ───────────────────────────────────────────────────
print(f"Loading ticks from {tick_file} ...")
raw_ticks = []
with open(tick_file) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("t") not in ("tk", "tf"):
            continue
        token = msg.get("tk")
        if token not in TOKEN_MAP:
            continue
        lp = msg.get("lp") or msg.get("c")
        ft = msg.get("ft")
        if not lp or not ft:
            continue
        raw_ticks.append((int(ft), token, float(lp),
                          float(msg.get("v", 0) or 0), msg))

raw_ticks.sort(key=lambda x: x[0])
print(f"Loaded {len(raw_ticks):,} valid price ticks across {len(TOKEN_MAP)} symbols")

# ── One CandleBuilder + Strategy per symbol ───────────────────────────────
builders:   dict = {sym: CandleBuilder(INTERVAL_S) for sym in TOKEN_MAP.values()}
strategies: dict = {sym: SupertrendStrategy(sym, qty=1) for sym in TOKEN_MAP.values()}

candle_counts = {sym: 0 for sym in TOKEN_MAP.values()}
signal_counts = {sym: 0 for sym in TOKEN_MAP.values()}

# ── Replay ────────────────────────────────────────────────────────────────
print("Replaying ticks ...")
for ft_int, token, price, vol, msg in raw_ticks:
    symbol  = TOKEN_MAP[token]
    ts      = datetime.fromtimestamp(ft_int, tz=IST)
    tick    = Tick(ts=ts, symbol=token, ltp=price, volume=vol, raw=msg)
    candle  = builders[symbol].update(tick)
    if candle is None:
        continue
    candle_counts[symbol] += 1
    sigs = strategies[symbol].on_candle(candle)
    signal_counts[symbol] += len(sigs)

# ── Force-save state for any symbol that didn't hit EOD in the data ───────
for sym, strat in strategies.items():
    strat.save_state()

# ── Summary ───────────────────────────────────────────────────────────────
print("\n── Replay complete ──────────────────────────────────────────")
print(f"{'Symbol':>12}  {'Candles':>8}  {'Signals':>8}  {'ATR':>10}  {'Trend':>6}")
print("-" * 56)
for sym in sorted(TOKEN_MAP.values()):
    strat = strategies[sym]
    atr   = f"{strat._atr:.4f}" if strat._atr else "warming"
    trend = {1: "UP", -1: "DOWN", 0: "NONE"}.get(strat._trend, "?")
    print(f"{sym:>12}  {candle_counts[sym]:>8}  {signal_counts[sym]:>8}  {atr:>10}  {trend:>6}")

print(f"\nState saved to data/st_state/")
print("Tomorrow's runner will load this state — no warmup needed.")
