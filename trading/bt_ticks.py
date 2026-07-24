#!/usr/bin/env python3
"""
Faithful tick-replay backtest — runs captured ticks through the REAL production
strategy (SupertrendStrategy.on_candle + check_stops + CandleBuilder), replicates
the runner's EOD handling (MIS squared at 15:00, CNC held overnight), and applies a
realistic Flattrade cost model. Mac-only tool; not part of the live system.

Usage: python3 bt_ticks.py
"""
from __future__ import annotations
import glob, json, math, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST, EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Tick, CandleBuilder
from paper import _get_tick
from strategies.supertrend import SupertrendStrategy

# Disable file persistence for the backtest — one strategy object per symbol is
# reused across all days in memory (so ATR warmup + overnight CNC carry naturally).
SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

TOKENS = {
    "21951": ("HFCL", "CNC"), "4668": ("BANKBARODA", "CNC"),
    "15332": ("NMDC", "CNC"), "628": ("CESC", "CNC"),
    "3812": ("ZEEL", "CNC"), "341": ("BALRAMCHIN", "CNC"),
    "11872": ("GRANULES", "MIS"), "12018": ("SUZLON", "MIS"),
}


def fill_price(side: str, px: float) -> float:
    t = _get_tick(px)
    if side == "BUY":
        return round((math.ceil(round(px / t, 8)) + 1) * t, 4)
    return round((math.floor(round(px / t, 8)) - 1) * t, 4)


def round_trip_cost(entry_val: float, exit_val: float, mode: str) -> float:
    """Flattrade zero brokerage; statutory + DP charges only (INR)."""
    if mode == "CNC":
        stt = 0.001 * entry_val + 0.001 * exit_val
        stamp = 0.00015 * entry_val
        dp = 20.0 * 1.18   # Flattrade: Rs20 per delivery-sell debit + 18% GST
    else:
        stt = 0.00025 * exit_val
        stamp = 0.00003 * entry_val
        dp = 0.0
    exch = 0.0000307 * (entry_val + exit_val)   # NSE 0.00307%
    sebi = 0.000001 * (entry_val + exit_val)
    gst = 0.18 * (exch + sebi)
    return stt + stamp + dp + exch + sebi + gst


def is_eod(ts) -> bool:
    return ts.hour > EOD_EXIT_HOUR or (ts.hour == EOD_EXIT_HOUR and ts.minute >= EOD_EXIT_MINUTE)


def load_ticks():
    by_sym = {sym: [] for sym, _ in TOKENS.values()}
    for f in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "data/2026-*/ticks.jsonl"))):
        for line in open(f):
            try:
                m = json.loads(line)
            except Exception:
                continue
            tok = m.get("tk")
            if tok not in TOKENS:
                continue
            lp = m.get("lp") or m.get("c")
            ft = m.get("ft")
            if not lp or not ft:
                continue
            bid, ask = m.get("bp1"), m.get("sp1")
            try:
                by_sym[TOKENS[tok][0]].append((int(ft), float(lp),
                    float(bid) if bid else 0.0, float(ask) if ask else 0.0))
            except (ValueError, TypeError):
                continue
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x[0])
    return by_sym


def run_config(ticks_by_sym, *, breakeven, trail_peak, take_profit,
               interval, capital, force_long_only=None, realistic=False):
    st_mod.BREAKEVEN_TRIGGER_MULT = breakeven
    st_mod.TRAIL_PEAK_MULT = trail_peak
    st_mod.TAKE_PROFIT_MULT = take_profit

    trades = []
    for tok, (sym, mode) in TOKENS.items():
        seq = ticks_by_sym.get(sym, [])
        if not seq:
            continue
        long_only = (mode == "CNC") if force_long_only is None else force_long_only
        strat = SupertrendStrategy(sym, qty=1, long_only=long_only)
        builder = CandleBuilder(interval_seconds=interval)
        pos = None
        prev_eod = False
        cur = {"bid": 0.0, "ask": 0.0}

        def rfill(side, px):
            # realistic: cross the spread (BUY pays ask, SELL hits bid); else lp ± 1 tick
            if realistic and cur["bid"] > 0 and cur["ask"] > 0:
                return cur["ask"] if side == "BUY" else cur["bid"]
            return fill_price(side, px)

        def open_pos(side, px, ts):
            nonlocal pos
            ef = rfill("BUY" if side == "LONG" else "SELL", px)
            lev = 4 if mode == "MIS" else 1
            qty = max(1, int(capital * lev / ef))
            pos = {"side": side, "entry_fill": ef, "qty": qty, "entry_time": ts}

        def close_pos(px, ts, reason):
            nonlocal pos
            if pos is None:
                return
            xf = rfill("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            gross = (xf - pos["entry_fill"]) * q if pos["side"] == "LONG" else (pos["entry_fill"] - xf) * q
            cost = round_trip_cost(pos["entry_fill"] * q, xf * q, mode)
            trades.append({"sym": sym, "mode": mode, "side": pos["side"],
                           "gross": gross, "cost": cost, "net": gross - cost, "reason": reason})
            pos = None

        for ft, price, bid, ask in seq:
            ts = datetime.fromtimestamp(ft, tz=IST)
            if bid > 0: cur["bid"] = bid
            if ask > 0: cur["ask"] = ask
            if is_eod(ts):
                # Replicate runner EOD: square MIS once at 15:00; hold CNC overnight.
                if pos is not None and mode == "MIS" and not prev_eod:
                    close_pos(price, ts, "EOD")
                prev_eod = True
                continue
            prev_eod = False
            if pos is not None:
                xs = strat.check_stops(price)
                if xs:
                    close_pos(xs["price"], ts, xs["reason"].split("|")[0].strip())
            candle = builder.update(Tick(ts=ts, symbol=sym, ltp=price))
            if candle is None:
                continue
            for sig in strat.on_candle(candle):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    open_pos("LONG" if a == "BUY" else "SHORT", sig["price"], candle.start)
                elif a == "EXIT" and pos is not None:
                    close_pos(sig["price"], candle.start, sig["reason"].split("|")[0].strip())
        if pos is not None:
            close_pos(seq[-1][1], datetime.fromtimestamp(seq[-1][0], tz=IST), "END")
    return trades


def summarize(trades):
    n = len(trades)
    if n == 0:
        return dict(n=0)
    wins = [t for t in trades if t["net"] > 0]
    return dict(n=n, win=len(wins) / n * 100,
                gross=sum(t["gross"] for t in trades),
                cost=sum(t["cost"] for t in trades),
                net=sum(t["net"] for t in trades),
                avg=sum(t["net"] for t in trades) / n)


def fmt(s):
    if s.get("n", 0) == 0:
        return "no trades"
    return (f"trades={s['n']:3d}  win={s['win']:4.0f}%  gross=Rs{s['gross']:+8.1f}  "
            f"costs=Rs{s['cost']:7.1f}  NET=Rs{s['net']:+8.1f}  avg=Rs{s['avg']:+6.1f}/trade")


def buy_hold(tk, capital):
    """Benchmark: what if you just bought each stock at first price and held to last?"""
    total = 0.0
    for tok, (sym, mode) in TOKENS.items():
        seq = tk.get(sym)
        if not seq:
            continue
        first, last = seq[0][1], seq[-1][1]
        qty = max(1, int(capital / first))
        total += (last - first) * qty
    return total


def main():
    print("Loading ticks...")
    tk = load_ticks()
    total = sum(len(v) for v in tk.values())
    print(f"  {total:,} ticks | 8 symbols | 2026-06-22 .. 2026-07-10 (14 sessions)\n")

    CAP = 5000
    print(f"Per-stock capital Rs{CAP} (MIS notional x4, CNC x1). "
          f"Costs: zero brokerage + STT/exch/stamp/GST + CNC DP ~Rs18.88/sell.\n")

    configs = [
        ("A. PRODUCTION (15m, long-only CNC, breakeven 1xATR ON)",
         dict(breakeven=1.0, trail_peak=0.0, take_profit=0.0, interval=900, capital=CAP)),
        ("B. Breakeven OFF",
         dict(breakeven=0.0, trail_peak=0.0, take_profit=0.0, interval=900, capital=CAP)),
        ("C. Long+SHORT everywhere",
         dict(breakeven=1.0, trail_peak=0.0, take_profit=0.0, interval=900, capital=CAP, force_long_only=False)),
        ("D. Peak-trail 3xATR ON",
         dict(breakeven=1.0, trail_peak=3.0, take_profit=0.0, interval=900, capital=CAP)),
        ("E. Fixed take-profit 3xATR ON",
         dict(breakeven=1.0, trail_peak=0.0, take_profit=3.0, interval=900, capital=CAP)),
        ("F. 5-min candles",
         dict(breakeven=1.0, trail_peak=0.0, take_profit=0.0, interval=300, capital=CAP)),
        ("G. Breakeven 2xATR (looser)",
         dict(breakeven=2.0, trail_peak=0.0, take_profit=0.0, interval=900, capital=CAP)),
    ]
    prod_trades = None
    for label, kw in configs:
        tr = run_config(tk, **kw)
        if label.startswith("A."):
            prod_trades = tr
        print(f"{label}\n    {fmt(summarize(tr))}\n")

    print(f"BENCHMARK  buy & hold all 8 (Rs{CAP} each): Rs{buy_hold(tk, CAP):+.1f}\n")

    # (a) OPTIMISTIC (lp±1tick) vs REALISTIC (cross real bid/ask) fills
    print("=" * 70)
    print("FILL MODEL — optimistic (lp±1tick) vs REALISTIC (cross bid/ask):")
    for label, kw in [("A. PRODUCTION", dict(breakeven=1.0, trail_peak=0.0, take_profit=0.0, interval=900, capital=CAP)),
                      ("C. Long+SHORT", dict(breakeven=1.0, trail_peak=0.0, take_profit=0.0, interval=900, capital=CAP, force_long_only=False))]:
        opt = summarize(run_config(tk, realistic=False, **kw))
        rea = summarize(run_config(tk, realistic=True, **kw))
        print(f"  {label}")
        print(f"    optimistic: {fmt(opt)}")
        print(f"    realistic : {fmt(rea)}")
        if opt.get('n') and rea.get('n'):
            print(f"    => realistic fills cost Rs{opt['net']-rea['net']:.0f} more (the spread you actually pay)\n")

    print("=" * 70)
    print("Per-symbol breakdown — PRODUCTION config:")
    bysym = {}
    for t in prod_trades:
        d = bysym.setdefault(t["sym"], {"mode": t["mode"], "n": 0, "net": 0.0, "gross": 0.0})
        d["n"] += 1; d["net"] += t["net"]; d["gross"] += t["gross"]
    for sym, d in sorted(bysym.items(), key=lambda x: x[1]["net"]):
        print(f"  {sym:12s} {d['mode']}  trades={d['n']:3d}  gross=Rs{d['gross']:+8.1f}  net=Rs{d['net']:+8.1f}")

    # exit-reason distribution (production)
    print("\nExit reasons — PRODUCTION config:")
    rc = {}
    for t in prod_trades:
        rc[t["reason"]] = rc.get(t["reason"], 0) + 1
    for r, c in sorted(rc.items(), key=lambda x: -x[1]):
        print(f"  {r:14s} {c}")


if __name__ == "__main__":
    main()
