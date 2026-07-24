#!/usr/bin/env python3
"""
Parameter sweep on 60d x 15m Yahoo bars — target the diagnosed problem (overtrading/costs).
Tests supertrend multiplier x ATR-period grid, and a "trade all as MIS" experiment to
isolate how much the CNC DP charge alone is costing. Reuses helpers from bt_yahoo.
"""
from __future__ import annotations
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fetch, fill_price, round_trip_cost, is_eod, SYMS


def run_grid(sb, *, mult, atr_period, breakeven=1.0, force_mis=False, capital=5000):
    st_mod.BREAKEVEN_TRIGGER_MULT = breakeven
    st_mod.TRAIL_PEAK_MULT = 0.0
    st_mod.TAKE_PROFIT_MULT = 0.0
    trades = []
    for sym, (ticker, base_mode) in SYMS.items():
        bars = sb.get(sym, [])
        if not bars:
            continue
        mode = "MIS" if force_mis else base_mode
        long_only = (mode == "CNC")
        strat = SupertrendStrategy(sym, qty=1, atr_period=atr_period, multiplier=mult, long_only=long_only)
        pos = None
        prev_eod = False

        def open_pos(side, px):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            lev = 4 if mode == "MIS" else 1
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital * lev / ef))}

        def close_pos(px):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            gross = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            cost = round_trip_cost(pos["ef"] * q, xf * q, mode)
            trades.append({"gross": gross, "cost": cost, "net": gross - cost})
            pos = None

        for dt, o, h, l, c in bars:
            if is_eod(dt):
                if pos is not None and mode == "MIS" and not prev_eod:
                    close_pos(c)
                prev_eod = True
                continue
            prev_eod = False
            for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    open_pos("LONG" if a == "BUY" else "SHORT", sig["price"])
                elif a == "EXIT" and pos is not None:
                    close_pos(sig["price"])
        if pos is not None:
            close_pos(bars[-1][4])
    n = len(trades)
    if n == 0:
        return (0, 0, 0, 0)
    return (n, sum(t["gross"] for t in trades), sum(t["cost"] for t in trades), sum(t["net"] for t in trades))


def main():
    print("Downloading bars...\n")
    sb = {sym: fetch(t) for sym, (t, m) in SYMS.items()}

    print("SWEEP: multiplier x ATR-period  (production exits, long-only CNC, 15m)")
    print(f"{'mult':>5} {'atr':>4} | {'trades':>6} {'gross':>10} {'costs':>9} {'NET':>10}")
    print("-" * 52)
    best = None
    for mult in (1.5, 2.0, 2.5, 3.0, 4.0):
        for atr in (10, 14, 21):
            n, g, c, net = run_grid(sb, mult=mult, atr_period=atr)
            print(f"{mult:>5} {atr:>4} | {n:>6} {g:>+10.0f} {c:>9.0f} {net:>+10.0f}")
            if best is None or net > best[0]:
                best = (net, mult, atr)
        print()
    print(f"BEST net in grid: Rs{best[0]:+.0f} at multiplier={best[1]} atr={best[2]}\n")

    print("EXPERIMENT: how much is the CNC DP charge costing?")
    print("  (same signals, but trade ALL names as MIS = no DP charge, no overnight hold)")
    for mult in (1.5, 3.0):
        n1, g1, c1, net1 = run_grid(sb, mult=mult, atr_period=14, force_mis=False)
        n2, g2, c2, net2 = run_grid(sb, mult=mult, atr_period=14, force_mis=True)
        print(f"  mult={mult}: CNC/MIS mix -> NET Rs{net1:+.0f} (costs {c1:.0f}) | "
              f"all-MIS -> NET Rs{net2:+.0f} (costs {c2:.0f})  => DP+delivery drag ~Rs{c1-c2:.0f}")


if __name__ == "__main__":
    main()
