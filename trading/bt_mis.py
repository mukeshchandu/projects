#!/usr/bin/env python3
"""
MIS-only backtest (intraday: zero DP charge, no overnight hold, long+short allowed)
across a BROADER NSE universe, with out-of-sample split-half validation and a small
multiplier sweep. Sized at flat Rs5,000 notional (1x) to measure raw signal edge;
4x MIS leverage would scale P&L (and risk) ~4x.
"""
from __future__ import annotations
import json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fetch, fill_price, round_trip_cost, is_eod

# Universes to test
CURRENT8 = ["HFCL", "BANKBARODA", "NMDC", "CESC", "ZEEL", "BALRAMCHIN", "GRANULES", "SUZLON"]
BROAD = ["RELIANCE", "TATAMOTORS", "ICICIBANK", "SBIN", "INFY", "TATASTEEL", "ADANIENT",
         "IDEA", "PNB", "IOC", "HINDALCO", "AXISBANK", "ITC", "WIPRO", "ONGC", "COALINDIA"]


def cache_path(tag):
    return os.path.join(os.path.dirname(__file__), f"bars_{tag}.json")


def load_universe(tag, tickers):
    cp = cache_path(tag)
    if os.path.exists(cp):
        from datetime import datetime
        raw = json.load(open(cp))
        return {s: [(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
                for s, rows in raw.items()}
    out = {}
    for sym in tickers:
        bars = []
        for _ in range(3):
            bars = fetch(sym + ".NS")
            if bars:
                break
        out[sym] = bars
    json.dump({s: [[b[0].isoformat(), b[1], b[2], b[3], b[4]] for b in bars]
               for s, bars in out.items()}, open(cp, "w"))
    return out


def run_mis(bars_by_sym, *, mult, atr_period=14, long_only=False, breakeven=1.0, capital=5000):
    """All symbols traded as MIS: intraday, EOD square, no DP charge."""
    st_mod.BREAKEVEN_TRIGGER_MULT = breakeven
    st_mod.TRAIL_PEAK_MULT = 0.0
    st_mod.TAKE_PROFIT_MULT = 0.0
    trades = []
    for sym, bars in bars_by_sym.items():
        if not bars:
            continue
        strat = SupertrendStrategy(sym, qty=1, atr_period=atr_period, multiplier=mult, long_only=long_only)
        pos = None
        prev_eod = False

        def openp(side, px):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital / ef)), "sym": sym}

        def closep(px):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            g = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            c = round_trip_cost(pos["ef"] * q, xf * q, "MIS")
            trades.append({"sym": sym, "gross": g, "cost": c, "net": g - c})
            pos = None

        for dt, o, h, l, c in bars:
            if is_eod(dt):
                if pos is not None and not prev_eod:
                    closep(c)   # MIS squares at EOD
                prev_eod = True
                continue
            prev_eod = False
            for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    openp("LONG" if a == "BUY" else "SHORT", sig["price"])
                elif a == "EXIT" and pos is not None:
                    closep(sig["price"])
        if pos is not None:
            closep(bars[-1][4])
    return trades


def net(trades):
    if not trades:
        return (0, 0, 0, 0)
    return (len(trades), sum(t["gross"] for t in trades),
            sum(t["cost"] for t in trades), sum(t["net"] for t in trades))


def split(bars):
    f, s = {}, {}
    for sym, b in bars.items():
        m = len(b) // 2
        f[sym], s[sym] = b[:m], b[m:]
    return f, s


def report(tag, bars):
    ok = [s for s in bars if bars[s]]
    print(f"\n{'='*70}\nUNIVERSE: {tag}  ({len(ok)} stocks with data)")
    print(f"  {', '.join(ok)}")
    first, second = split(bars)
    print(f"\n  MIS-only (intraday, no DP), long+SHORT:")
    print(f"  {'params':>10} | {'FULL 60d':>16} | {'1st half':>14} | {'2nd half':>14}")
    print("  " + "-" * 62)
    for mult in (1.5, 2.5, 3.0):
        nf, gf, cf, netf = net(run_mis(bars, mult=mult))
        _, _, _, net1 = net(run_mis(first, mult=mult))
        _, _, _, net2 = net(run_mis(second, mult=mult))
        rob = "OK both+" if (net1 > 0 and net2 > 0) else "not robust"
        print(f"  {'m'+str(mult):>10} | Rs{netf:>+8.0f} ({nf:3d}) | Rs{net1:>+7.0f} | Rs{net2:>+7.0f}  {rob}")

    # long-only variant at mult 2.5
    nf, gf, cf, netf = net(run_mis(bars, mult=2.5, long_only=True))
    print(f"\n  MIS long-ONLY, m2.5: FULL Rs{netf:+.0f} ({nf} trades, gross Rs{gf:+.0f}, costs Rs{cf:.0f})")

    # per-stock at mult 2.5 long+short
    tr = run_mis(bars, mult=2.5)
    bs = {}
    for t in tr:
        d = bs.setdefault(t["sym"], 0.0)
        bs[t["sym"]] = d + t["net"]
    print(f"\n  Per-stock NET (m2.5, long+short):")
    for sym, v in sorted(bs.items(), key=lambda x: x[1]):
        print(f"    {sym:12s} Rs{v:+8.0f}")
    winners = sum(1 for v in bs.values() if v > 0)
    print(f"  -> {winners}/{len(bs)} stocks net-positive")


def main():
    print("Loading universes (cached after first run)...")
    report("CURRENT 8", load_universe("cur8", CURRENT8))
    report("BROAD (16 liquid NSE)", load_universe("broad", BROAD))


if __name__ == "__main__":
    main()
