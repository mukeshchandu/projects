#!/usr/bin/env python3
"""
regime_backtest.py — Test a CAUSAL regime switch for the exit rule, and validate it
out-of-sample.

Idea (from the exit backtest): 'trailtight' wins on trending days, 'current' wins on choppy
days. So at each ENTRY we measure trend strength with ADX(14) — computed only from candles
seen so far (causal) — and pick the exit management for that trade:
    ADX_at_entry >= threshold  -> TREND -> trailtight (be 0.5, chandelier 1.5 ATR)
    ADX_at_entry <  threshold  -> CHOP  -> current    (be 1.0, wide Supertrend trail)

We sweep the threshold, then split the days into TRAIN (first half) / TEST (second half),
pick the best threshold on TRAIN only, and report its TEST P&L vs the flat baselines — so we
know whether the regime rule generalizes or just memorizes.

Usage:  python3 regime_backtest.py
"""
from __future__ import annotations
import glob, os, sys
from datetime import datetime
import warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_chart as S
import strategies.supertrend as st_mod
import exit_backtest as EB                     # sets S._warm_sequences = EB._warm_before
from config import IST
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

MULT, CAP, LEV = 1.5, 5000, 4
CURRENT    = (1.0, 0.0, 0.0)
TRAILTIGHT = (0.5, 1.5, 0.0)
THRESHOLDS = [0, 12, 16, 20, 24, 28, 999]      # 0 = always trailtight; 999 = always current


class ADX:
    """Causal Wilder ADX(14) fed one candle at a time. Returns current ADX or None until primed."""
    def __init__(self, p: int = 14):
        self.p = p; self.prev = None; self.n = 0
        self.tr = self.pdm = self.mdm = 0.0
        self.adx = None; self.dx_sum = 0.0; self.dx_n = 0

    def update(self, h, l, c):
        if self.prev is None:
            self.prev = (h, l, c); return None
        ph, pl, pc = self.prev; self.prev = (h, l, c)
        up, dn = h - ph, pl - l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        tr  = max(h - l, abs(h - pc), abs(l - pc))
        if self.n < self.p:
            self.tr += tr; self.pdm += pdm; self.mdm += mdm; self.n += 1
            return None
        self.tr  = self.tr  - self.tr  / self.p + tr
        self.pdm = self.pdm - self.pdm / self.p + pdm
        self.mdm = self.mdm - self.mdm / self.p + mdm
        if self.tr == 0:
            return self.adx
        pdi = 100 * self.pdm / self.tr; mdi = 100 * self.mdm / self.tr
        denom = pdi + mdi
        dx = 100 * abs(pdi - mdi) / denom if denom else 0.0
        if self.adx is None:
            self.dx_sum += dx; self.dx_n += 1
            if self.dx_n == self.p:
                self.adx = self.dx_sum / self.p
        else:
            self.adx = (self.adx * (self.p - 1) + dx) / self.p
        return self.adx


def _set_exit(mode):
    be, tr, tp = TRAILTIGHT if mode == "trend" else CURRENT
    st_mod.BREAKEVEN_TRIGGER_MULT = be
    st_mod.TRAIL_PEAK_MULT        = tr
    st_mod.TAKE_PROFIT_MULT       = tp


def run_day(sym, seq, thr):
    """Replay one day; switch exit rule per-trade by ADX-at-entry. thr=0 -> always trailtight,
    thr=999 -> always current. Returns (net, trades, wins, adx_at_entries)."""
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
    net = 0.0; ntr = nwin = 0; adx_entries = []

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
                _set_exit("trend" if (cur is not None and cur >= thr) else "chop")
                adx_entries.append(cur if cur is not None else -1)
                pos = ("LONG" if a == "BUY" else "SHORT", sig["price"])
            elif a == "EXIT" and pos is not None:
                book(pos[0], pos[1], sig["price"]); pos = None
    if pos:
        book(pos[0], pos[1], seq[-1][1])
    return net, ntr, nwin, adx_entries


def total_for(days, thr):
    net = tr = win = 0; adxs = []
    for day in days:
        S.DATE = day
        today = S._load_ticks(os.path.join(EB.HERE, "data", day, "ticks.jsonl"))
        for sym, seq in today.items():
            if len(seq) < 100:
                continue
            n, t, w, ae = run_day(sym, seq, thr)
            net += n; tr += t; win += w; adxs += ae
    return net, tr, win, adxs


def main():
    days = sorted(os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(EB.HERE, "data", "*", "ticks.jsonl")))
    # prime history cache once per symbol
    syms = set()
    for day in days:
        syms |= set(S._load_ticks(os.path.join(EB.HERE, "data", day, "ticks.jsonl")))
    for s in syms:
        EB._hist(s)

    mid = len(days) // 2
    train, test = days[:mid], days[mid:]
    print(f"{len(days)} days · TRAIN {train[0]}→{train[-1]} ({len(train)}) · "
          f"TEST {test[0]}→{test[-1]} ({len(test)})\n")

    print(f"{'ADX thr':>8s} {'TRAIN net':>10s} {'TEST net':>10s}   (thr 0=always trailtight, 999=always current)")
    rows = {}
    for thr in THRESHOLDS:
        tn = total_for(train, thr)[0]
        te = total_for(test, thr)[0]
        rows[thr] = (tn, te)
        print(f"{thr:>8d} {tn:+10.1f} {te:+10.1f}")

    # pick threshold on TRAIN only, report its TEST result vs flat baselines
    best_thr = max([t for t in THRESHOLDS if t not in (0, 999)], key=lambda t: rows[t][0])
    print("\n── out-of-sample verdict ──")
    print(f"threshold chosen on TRAIN: ADX>={best_thr}")
    print(f"  TEST regime(thr={best_thr}) : {rows[best_thr][1]:+.1f}")
    print(f"  TEST flat trailtight       : {rows[0][1]:+.1f}")
    print(f"  TEST flat current          : {rows[999][1]:+.1f}")
    edge = rows[best_thr][1] - rows[0][1]
    print(f"  regime vs flat trailtight on TEST: {edge:+.1f}  "
          f"({'REGIME HELPS' if edge > 0 else 'no benefit — flat trailtight as good/better'})")


if __name__ == "__main__":
    main()
