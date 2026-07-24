#!/usr/bin/env python3
"""
Sweep TRAIL_PEAK_MULT (chandelier profit-lock) and TAKE_PROFIT_MULT (fixed take-profit)
against the current production baseline (both OFF, breakeven-only) to see whether either
locks in more of the "ran up, gave it all back to breakeven" round trips.

Uses the same 60d x 15m Yahoo bars / cost model / OOS split-half harness as
bt_robust.py and bt_yahoo.py (reuses the cached bars_60d_cache.json if present).
Production mult/atr_period (1.5 / 14) and breakeven (1.0xATR) are held fixed; only
TRAIL_PEAK_MULT / TAKE_PROFIT_MULT vary.

Usage: python3 bt_trail_sweep.py
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
from bt_robust import load_bars, split_halves

MULT, ATR_PERIOD, BREAKEVEN = 1.5, 14, 1.0   # current live production config


def run(bars_by_sym, *, trail=0.0, tp=0.0, capital=5000):
    st_mod.BREAKEVEN_TRIGGER_MULT = BREAKEVEN
    st_mod.TRAIL_PEAK_MULT = trail
    st_mod.TAKE_PROFIT_MULT = tp
    trades = []
    for sym, (ticker, mode) in SYMS.items():
        bars = bars_by_sym.get(sym, [])
        if not bars:
            continue
        strat = SupertrendStrategy(sym, qty=1, atr_period=ATR_PERIOD, multiplier=MULT,
                                   long_only=(mode == "CNC"))
        pos = None
        prev_eod = False

        def open_pos(side, px):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            lev = 4 if mode == "MIS" else 1
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital * lev / ef))}

        def close_pos(px, reason):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            gross = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            cost = round_trip_cost(pos["ef"] * q, xf * q, mode)
            trades.append({"sym": sym, "gross": gross, "cost": cost,
                           "net": gross - cost, "reason": reason})
            pos = None

        for dt, o, h, l, c in bars:
            if is_eod(dt):
                if pos is not None and mode == "MIS" and not prev_eod:
                    close_pos(c, "EOD")
                prev_eod = True
                continue
            prev_eod = False
            for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    open_pos("LONG" if a == "BUY" else "SHORT", sig["price"])
                elif a == "EXIT" and pos is not None:
                    close_pos(sig["price"], sig["reason"].split("|")[0].strip())
        if pos is not None:
            close_pos(bars[-1][4], "END")
    return trades


def summ(trades):
    n = len(trades)
    if n == 0:
        return n, 0.0, 0.0, "no trades"
    net = sum(t["net"] for t in trades)
    gross = sum(t["gross"] for t in trades)
    win = sum(1 for t in trades if t["net"] > 0) / n * 100
    return n, net, gross, f"n={n:3d}  win={win:4.0f}%  net=Rs{net:+8.0f}"


def reason_breakdown(trades):
    out = {}
    for t in trades:
        out.setdefault(t["reason"], [0, 0.0])
        out[t["reason"]][0] += 1
        out[t["reason"]][1] += t["net"]
    return out


def main():
    print("Loading 60d x 15m bars (cache if present)...\n")
    bars = load_bars()
    ok = [s for s in bars if bars[s]]
    print(f"Symbols with data: {len(ok)}  ({', '.join(ok)})\n")
    first, second = split_halves(bars)

    print(f"Baseline: mult={MULT} atr={ATR_PERIOD} breakeven={BREAKEVEN}xATR, trail/tp OFF (current production)\n")

    print("== TRAIL_PEAK_MULT sweep (chandelier stop from peak; TAKE_PROFIT OFF) ==")
    print(f"{'trail':>6} | {'FULL 60d':>28} | {'1st half (OOS-A)':>28} | {'2nd half (OOS-B)':>28}")
    print("-" * 100)
    for trail in (0.0, 0.5, 0.75, 1.0, 1.5, 2.0):
        _, _, _, sf = summ(run(bars, trail=trail))
        _, _, _, s1 = summ(run(first, trail=trail))
        _, _, _, s2 = summ(run(second, trail=trail))
        print(f"{trail:>6} | {sf:>28} | {s1:>28} | {s2:>28}")

    print("\n== TAKE_PROFIT_MULT sweep (fixed TP from entry; TRAIL OFF) ==")
    print(f"{'tp':>6} | {'FULL 60d':>28} | {'1st half (OOS-A)':>28} | {'2nd half (OOS-B)':>28}")
    print("-" * 100)
    for tp in (0.0, 1.0, 1.5, 2.0, 3.0):
        _, _, _, sf = summ(run(bars, tp=tp))
        _, _, _, s1 = summ(run(first, tp=tp))
        _, _, _, s2 = summ(run(second, tp=tp))
        print(f"{tp:>6} | {sf:>28} | {s1:>28} | {s2:>28}")

    print("\nA setting is trustworthy only if net is positive/improved on BOTH halves, not just the full period.\n")

    print("== Exit-reason breakdown, FULL 60d, baseline (trail=0, tp=0) ==")
    base = reason_breakdown(run(bars, trail=0.0, tp=0.0))
    for reason, (n, net) in sorted(base.items(), key=lambda kv: -kv[1][0]):
        print(f"  {reason:14s}  n={n:3d}  net=Rs{net:+8.0f}")

    # show the reason breakdown for the best-looking trail value too, once eyeballed above
    print("\n== Exit-reason breakdown, FULL 60d, trail=1.0 (candidate) ==")
    cand = reason_breakdown(run(bars, trail=1.0, tp=0.0))
    for reason, (n, net) in sorted(cand.items(), key=lambda kv: -kv[1][0]):
        print(f"  {reason:14s}  n={n:3d}  net=Rs{net:+8.0f}")


if __name__ == "__main__":
    main()
