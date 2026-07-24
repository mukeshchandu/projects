#!/usr/bin/env python3
"""
Out-of-sample robustness: split the 60d window into first-half vs second-half and
check whether a parameter choice holds up on data it wasn't picked on. Guards against
curve-fitting the multiplier. Caches Yahoo bars to disk for reliable repeat runs.
"""
from __future__ import annotations
import json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fetch, fill_price, round_trip_cost, is_eod, SYMS

CACHE = os.path.join(os.path.dirname(__file__), "bars_60d_cache.json")


def load_bars():
    if os.path.exists(CACHE):
        raw = json.load(open(CACHE))
        # revive datetimes
        from datetime import datetime
        out = {}
        for sym, rows in raw.items():
            out[sym] = [(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
        return out
    out = {}
    for sym, (t, m) in SYMS.items():
        for attempt in range(3):
            bars = fetch(t)
            if bars:
                out[sym] = bars
                break
        else:
            out[sym] = []
    json.dump({sym: [[b[0].isoformat(), b[1], b[2], b[3], b[4]] for b in bars]
               for sym, bars in out.items()}, open(CACHE, "w"))
    return out


def run(bars_by_sym, *, mult, atr_period, breakeven=1.0, capital=5000):
    st_mod.BREAKEVEN_TRIGGER_MULT = breakeven
    st_mod.TRAIL_PEAK_MULT = 0.0
    st_mod.TAKE_PROFIT_MULT = 0.0
    net = gross = 0.0
    n = 0
    for sym, (ticker, mode) in SYMS.items():
        bars = bars_by_sym.get(sym, [])
        if not bars:
            continue
        strat = SupertrendStrategy(sym, qty=1, atr_period=atr_period, multiplier=mult,
                                   long_only=(mode == "CNC"))
        pos = None
        prev_eod = False
        for dt, o, h, l, c in bars:
            if is_eod(dt):
                if pos is not None and mode == "MIS" and not prev_eod:
                    xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", c)
                    q = pos["qty"]
                    g = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
                    net += g - round_trip_cost(pos["ef"] * q, xf * q, mode); gross += g; n += 1; pos = None
                prev_eod = True
                continue
            prev_eod = False
            for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    ef = fill_price("BUY" if a == "BUY" else "SELL", sig["price"])
                    lev = 4 if mode == "MIS" else 1
                    pos = {"side": "LONG" if a == "BUY" else "SHORT", "ef": ef,
                           "qty": max(1, int(capital * lev / ef))}
                elif a == "EXIT" and pos is not None:
                    xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", sig["price"])
                    q = pos["qty"]
                    g = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
                    net += g - round_trip_cost(pos["ef"] * q, xf * q, mode); gross += g; n += 1; pos = None
        if pos is not None:
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", bars[-1][4])
            q = pos["qty"]
            g = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            net += g - round_trip_cost(pos["ef"] * q, xf * q, mode); gross += g; n += 1
    return n, gross, net


def split_halves(bars_by_sym):
    first, second = {}, {}
    for sym, bars in bars_by_sym.items():
        mid = len(bars) // 2
        first[sym] = bars[:mid]
        second[sym] = bars[mid:]
    return first, second


def main():
    bars = load_bars()
    ok = [s for s in bars if bars[s]]
    print(f"Symbols with data: {len(ok)}  ({', '.join(ok)})\n")
    first, second = split_halves(bars)

    print(f"{'params':>14} | {'FULL 60d':>18} | {'1st half (OOS-A)':>20} | {'2nd half (OOS-B)':>20}")
    print("-" * 82)
    for mult, atr in [(1.5, 14), (2.5, 14), (3.0, 10), (3.0, 14), (3.0, 21), (4.0, 21)]:
        nf, gf, netf = run(bars, mult=mult, atr_period=atr)
        n1, g1, net1 = run(first, mult=mult, atr_period=atr)
        n2, g2, net2 = run(second, mult=mult, atr_period=atr)
        tag = f"m{mult} a{atr}"
        print(f"{tag:>14} | net Rs{netf:>+8.0f} ({nf:3d}) | net Rs{net1:>+8.0f} ({n1:3d}) | net Rs{net2:>+8.0f} ({n2:3d})")
    print("\nA setting is trustworthy only if BOTH halves are positive (not just the full-period fit).")


if __name__ == "__main__":
    main()
