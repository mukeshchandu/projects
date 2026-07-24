#!/usr/bin/env python3
"""
entry_filter_backtest.py — The win rate is stuck ~20% regardless of exit/timeframe, so the
bottleneck is ENTRY quality. This tests several causal entry FILTERS (veto a Supertrend flip
at entry when the setup is weak) on the same strategy (15-min, EMA-50, trailtight exit),
across all recorded days, and compares net P&L / trades / win% to the unfiltered baseline.

A vetoed flip = no trade; the strategy is reset flat so it can take the next flip. Everything
is decided from data available at entry (ADX, ATR, EMA gap, time-of-day, flip momentum).

Usage:  python3 entry_filter_backtest.py [N_DAYS]     (default = all)
"""
from __future__ import annotations
import glob, os, sys
from datetime import datetime
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_chart as S
import strategies.supertrend as st_mod
import exit_backtest as EB                       # sets S._warm_sequences = 15m look-ahead-free warm
from regime_backtest import ADX
from config import IST
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

MULT, CAP, LEV = 1.5, 5000, 4

# ── entry filters: ctx -> allow?  (ctx has adx, atr, ema, close, side, st, mins) ──
def _tod(mins):  # minutes since midnight IST
    return mins
FILTERS = {
    "baseline (all flips)": lambda c: True,
    "adx>=15":              lambda c: c["adx"] is not None and c["adx"] >= 15,
    "adx>=20":              lambda c: c["adx"] is not None and c["adx"] >= 20,
    "adx>=25":              lambda c: c["adx"] is not None and c["adx"] >= 25,
    "atr% 0.2-1.5":         lambda c: c["atr"] and 0.2 <= 100 * c["atr"] / c["close"] <= 1.5,
    "time 0945-1430":       lambda c: 9 * 60 + 45 <= c["mins"] <= 14 * 60 + 30,
    "ema gap>=0.3ATR":      lambda c: c["atr"] and abs(c["close"] - c["ema"]) >= 0.3 * c["atr"],
    "flip momo>=0.25ATR":   lambda c: c["atr"] and (
                                 (c["close"] - c["st"]) if c["side"] == "BUY" else (c["st"] - c["close"])
                             ) >= 0.25 * c["atr"],
}


def run_day(sym, seq, allow):
    """Replay one day with trailtight exit; veto flips where allow(ctx) is False."""
    st_mod.BREAKEVEN_TRIGGER_MULT = 0.5   # trailtight (the adopted live exit)
    st_mod.TRAIL_PEAK_MULT        = 1.5
    st_mod.TAKE_PROFIT_MULT       = 0.0
    strat = SupertrendStrategy(sym, 1, multiplier=MULT, long_only=False, ema_period=50)
    adx = ADX(14)
    wb = CandleBuilder(900)
    for ft, px in S._warm_sequences(sym):
        c = wb.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c is not None:
            strat.on_candle(c); adx.update(c.high, c.low, c.close)
    strat.position = 0; strat._entry_price = strat._entry_atr = strat._peak = None
    strat._breakeven_armed = False

    stb = CandleBuilder(900); pos = None
    net = 0.0; ntr = nwin = veto = 0

    def book(side, ep, xp):
        nonlocal net, ntr, nwin
        qty = max(1, int(CAP * LEV / ep))
        gross = (xp - ep) * qty if side == "LONG" else (ep - xp) * qty
        n = gross - S._charges(ep * qty, xp * qty)
        net += n; ntr += 1; nwin += (1 if n > 0 else 0)

    for ft, px in seq:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if not S._market_hours(ts):
            continue
        if S._is_eod(ts):
            if pos: book(pos[0], pos[1], px); pos = None
            continue
        if pos:
            xs = strat.check_stops(px)
            if xs: book(pos[0], pos[1], xs["price"]); pos = None
        c = stb.update(Tick(ts=ts, symbol="x", ltp=px))
        if c is None:
            continue
        cur = adx.update(c.high, c.low, c.close)
        for sig in strat.on_candle(c):
            a = sig["action"]
            if a in ("BUY", "SELL") and pos is None:
                ctx = {"adx": cur, "atr": strat._atr, "ema": strat._ema, "close": c.close,
                       "side": a, "st": strat._supertrend, "mins": ts.hour * 60 + ts.minute}
                if allow(ctx):
                    pos = ("LONG" if a == "BUY" else "SHORT", sig["price"])
                else:
                    veto += 1
                    strat.position = 0; strat._entry_price = strat._entry_atr = None
                    strat._peak = None; strat._breakeven_armed = False
            elif a == "EXIT" and pos is not None:
                book(pos[0], pos[1], sig["price"]); pos = None
    if pos:
        book(pos[0], pos[1], seq[-1][1])
    return net, ntr, nwin, veto


def main():
    days = sorted(os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(EB.HERE, "data", "*", "ticks.jsonl")))
    if len(sys.argv) > 1:
        days = days[-int(sys.argv[1]):]
    syms = set()
    for d in days:
        syms |= set(S._load_ticks(os.path.join(EB.HERE, "data", d, "ticks.jsonl")))
    for s in syms:
        EB._hist(s)
    print(f"entry-filter backtest · {len(days)} days: {days[0]} → {days[-1]} · "
          f"15-min · trailtight exit\n")

    print(f"{'filter':22s} {'net Rs':>10s} {'trades':>7s} {'wins':>5s} {'win%':>6s} {'vetoed':>7s}")
    base_net = None
    for name, fn in FILTERS.items():
        net = tr = win = veto = 0
        for day in days:
            S.DATE = day
            today = S._load_ticks(os.path.join(EB.HERE, "data", day, "ticks.jsonl"))
            for sym, seq in today.items():
                if len(seq) < 100:
                    continue
                n, t, w, v = run_day(sym, seq, fn)
                net += n; tr += t; win += w; veto += v
        wr = (100.0 * win / tr) if tr else 0.0
        if base_net is None:
            base_net = net
        print(f"{name:22s} {net:+10.1f} {tr:7d} {win:5d} {wr:5.1f}% {veto:7d}")


if __name__ == "__main__":
    main()
