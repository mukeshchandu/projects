#!/usr/bin/env python3
"""
Drill into WHY ~32% of trades hit Hard SL in the baseline production config
(mult=1.5, atr=14, breakeven=1.0xATR, trail/tp off) on the 60d x 15m Yahoo
basket. Tracks per-trade holding period (in closed candles) and per-symbol
breakdown so we can tell "stopped out fast after a false flip" from
"ground down slowly on a genuine but wrong-way move".

Usage: python3 bt_hardsl_analysis.py
"""
from __future__ import annotations
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fill_price, round_trip_cost, is_eod, SYMS
from bt_robust import load_bars

MULT, ATR_PERIOD, BREAKEVEN = 1.5, 14, 1.0


def run(bars_by_sym, *, capital=5000):
    st_mod.BREAKEVEN_TRIGGER_MULT = BREAKEVEN
    st_mod.TRAIL_PEAK_MULT = 0.0
    st_mod.TAKE_PROFIT_MULT = 0.0
    trades = []
    for sym, (ticker, mode) in SYMS.items():
        bars = bars_by_sym.get(sym, [])
        if not bars:
            continue
        strat = SupertrendStrategy(sym, qty=1, atr_period=ATR_PERIOD, multiplier=MULT,
                                   long_only=(mode == "CNC"))
        pos = None
        prev_eod = False
        candle_idx = 0

        def open_pos(side, px, idx):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            lev = 4 if mode == "MIS" else 1
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital * lev / ef)),
                   "entry_idx": idx, "entry_atr": strat._entry_atr}

        def close_pos(px, reason, idx):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            gross = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            cost = round_trip_cost(pos["ef"] * q, xf * q, mode)
            trades.append({"sym": sym, "gross": gross, "cost": cost, "net": gross - cost,
                           "reason": reason, "held": idx - pos["entry_idx"],
                           "entry_atr": pos["entry_atr"], "ef": pos["ef"]})
            pos = None

        for dt, o, h, l, c in bars:
            if is_eod(dt):
                if pos is not None and mode == "MIS" and not prev_eod:
                    close_pos(c, "EOD", candle_idx)
                prev_eod = True
                continue
            prev_eod = False
            for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    open_pos("LONG" if a == "BUY" else "SHORT", sig["price"], candle_idx)
                elif a == "EXIT" and pos is not None:
                    close_pos(sig["price"], sig["reason"].split("|")[0].strip(), candle_idx)
            candle_idx += 1
        if pos is not None:
            close_pos(bars[-1][4], "END", candle_idx)
    return trades


def main():
    print("Loading 60d x 15m bars (cache if present)...\n")
    bars = load_bars()
    trades = run(bars)
    hard = [t for t in trades if t["reason"] == "HARD SL"]
    others = [t for t in trades if t["reason"] != "HARD SL"]

    print(f"Total trades={len(trades)}  Hard-SL trades={len(hard)} ({len(hard)/len(trades)*100:.0f}%)\n")

    def held_stats(ts, label):
        if not ts:
            return
        holds = sorted(t["held"] for t in ts)
        n = len(holds)
        p50 = holds[n // 2]
        under2 = sum(1 for h in holds if h <= 1) / n * 100
        print(f"{label:12s} n={n:3d}  held(candles) min={holds[0]:2d} p50={p50:2d} max={holds[-1]:3d}  "
              f"<=1 candle={under2:4.0f}%")

    print("== Holding period: Hard-SL trades vs everything else ==")
    held_stats(hard, "HARD SL")
    held_stats(others, "OTHER")
    print()

    print("== Hard-SL count/net by holding bucket (candles held before stop) ==")
    buckets = [(0, 1, "0-1 (flip reverses ~immediately)"), (2, 3, "2-3"),
               (4, 7, "4-7"), (8, 999, "8+")]
    for lo, hi, label in buckets:
        b = [t for t in hard if lo <= t["held"] <= hi]
        if not b:
            continue
        print(f"  {label:38s} n={len(b):3d}  net=Rs{sum(t['net'] for t in b):+8.0f}")
    print()

    print("== Per-symbol: Hard-SL share of that symbol's trades ==")
    per_sym = {}
    for t in trades:
        d = per_sym.setdefault(t["sym"], {"n": 0, "hard": 0, "net": 0.0, "hard_net": 0.0})
        d["n"] += 1
        d["net"] += t["net"]
        if t["reason"] == "HARD SL":
            d["hard"] += 1
            d["hard_net"] += t["net"]
    for sym, d in sorted(per_sym.items(), key=lambda kv: -kv[1]["hard"] / kv[1]["n"]):
        pct = d["hard"] / d["n"] * 100
        print(f"  {sym:12s} trades={d['n']:3d}  hardSL={d['hard']:3d} ({pct:4.0f}%)  "
              f"symbol_net=Rs{d['net']:+7.0f}  hardSL_net=Rs{d['hard_net']:+7.0f}")
    print()

    print("== Avg loss per Hard-SL trade vs the 1.5xATR it's sized to ==")
    avg_atr = sum(t["entry_atr"] for t in hard if t["entry_atr"]) / len(hard)
    avg_loss = sum(t["gross"] for t in hard) / len(hard)
    print(f"  avg entry_atr={avg_atr:.3f}  1.5xATR (theoretical stop distance)~Rs{1.5*avg_atr:.2f}/share")
    print(f"  avg gross loss per Hard-SL trade=Rs{avg_loss:+.2f}")


if __name__ == "__main__":
    main()
