#!/usr/bin/env python3
"""
tf_backtest.py — Does the strategy survive on faster candles? Runs the SAME strategy
(EMA-50 filter, trailtight exit) on 1 / 3 / 5 / 15-min candles across the last N tick-days
and reports net P&L, trade count, win% and how many days were profitable per timeframe.

Warm-up resamples to each timeframe via CandleBuilder, so ATR/EMA are scale-correct.

Usage:  python3 tf_backtest.py [N_DAYS]     (default 7)
"""
from __future__ import annotations
import glob, os, sys
warnings_mod = __import__("warnings"); warnings_mod.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_chart as S

N_DAYS   = int(sys.argv[1]) if len(sys.argv) > 1 else 7
INTERVALS = [(f"{m}m", m * 60) for m in (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)]
HERE = os.path.dirname(os.path.abspath(__file__))

# cache warm-up per (symbol, date) — identical raw ticks regardless of candle size
_orig_warm = S._warm_sequences
_wc = {}
def _cached_warm(sym):
    k = (sym, S.DATE)
    if k not in _wc:
        _wc[k] = _orig_warm(sym)
    return _wc[k]
S._warm_sequences = _cached_warm


def main():
    days = sorted(os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(HERE, "data", "*", "ticks.jsonl")))[-N_DAYS:]
    print(f"timeframe survival · {len(days)} days: {days[0]} → {days[-1]} · "
          f"EMA-50 + trailtight exit · MIS\n")

    print(f"{'candle':>7s} {'net Rs':>10s} {'trades':>7s} {'wins':>5s} {'win%':>6s} {'+days':>6s}")
    for label, secs in INTERVALS:
        S.ST_INTERVAL = secs
        net = tr = win = 0
        pos_days = 0
        for day in days:
            S.DATE = day
            today = S._load_ticks(os.path.join(HERE, "data", day, "ticks.jsonl"))
            dnet = 0.0
            for sym, seq in today.items():
                if len(seq) < 100:
                    continue
                r = S.simulate(sym, seq, exit_mode="trailtight")
                dnet += r["net"]; tr += len(r["trades"])
                win += sum(1 for t in r["trades"] if t["pnl"] > 0)
            net += dnet
            pos_days += (1 if dnet > 0 else 0)
        wr = (100.0 * win / tr) if tr else 0.0
        print(f"{label:>7s} {net:+10.1f} {tr:7d} {win:5d} {wr:5.1f}% {pos_days:4d}/{len(days)}")


if __name__ == "__main__":
    main()
