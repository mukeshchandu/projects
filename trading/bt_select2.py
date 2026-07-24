#!/usr/bin/env python3
"""
(a) Exhaustive selector exploration for daily stock selection.
Metrics: volatility (daily range%), |cumulative return|, efficiency ratio (trendiness),
ADX (trend strength), low-flip-count (chop avoider), recent strategy P&L — plus rank-sum
COMBINATIONS. Walk-forward, MIS + EMA-50, K=5, lookback N in {3,5,10}. Reuses cached bars.
"""
from __future__ import annotations
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fill_price, round_trip_cost, is_eod
from bt_regime import compute_adx, compute_ema
from bt_select import download_all

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None


def per_day_metrics(bars, mult=1.5, capital=5000):
    ema = compute_ema(bars)
    adx = compute_adx(bars)
    st = sys.modules["strategies.supertrend"]
    st.BREAKEVEN_TRIGGER_MULT = 1.0; st.TRAIL_PEAK_MULT = 0.0; st.TAKE_PROFIT_MULT = 0.0
    strat = SupertrendStrategy("X", qty=1, multiplier=mult, long_only=False)
    pos = None; prev_eod = False
    net_by_day = {}; ohlc = {}; adx_by_day = {}; close_by_day = {}; trend_by_day = {}

    def openp(side, px):
        nonlocal pos
        ef = fill_price("BUY" if side == "LONG" else "SELL", px)
        pos = {"side": side, "ef": ef, "qty": max(1, int(capital/ef))}

    def closep(px, d):
        nonlocal pos
        if pos is None:
            return
        xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
        q = pos["qty"]
        g = (xf-pos["ef"])*q if pos["side"] == "LONG" else (pos["ef"]-xf)*q
        net_by_day[d] = net_by_day.get(d, 0.0) + (g - round_trip_cost(pos["ef"]*q, xf*q, "MIS"))
        pos = None

    for i, (dt, o, h, l, c) in enumerate(bars):
        d = dt.date()
        v = ohlc.get(d)
        if v is None:
            ohlc[d] = [o, h, l, c]
        else:
            v[1] = max(v[1], h); v[2] = min(v[2], l); v[3] = c
        close_by_day[d] = c
        if adx[i] is not None:
            adx_by_day[d] = adx[i]
        if is_eod(dt):
            if pos is not None and not prev_eod:
                closep(c, d)
            prev_eod = True
            continue
        prev_eod = False
        for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
            a = sig["action"]
            if a in ("BUY", "SELL") and pos is None:
                if ema[i] is None or (c > ema[i] if a == "BUY" else c < ema[i]):
                    openp("LONG" if a == "BUY" else "SHORT", sig["price"])
            elif a == "EXIT" and pos is not None:
                closep(sig["price"], d)
        trend_by_day[d] = strat._trend
    if pos is not None:
        closep(bars[-1][4], bars[-1][0].date())
    rng = {d: (v[1]-v[2])/v[0]*100 for d, v in ohlc.items() if v[0]}
    return dict(net=net_by_day, rng=rng, adx=adx_by_day, close=close_by_day, trend=trend_by_day)


def main():
    print("Loading cached universe + per-day metrics...")
    bars = download_all()
    M = {s: per_day_metrics(b) for s, b in bars.items()}
    SYMS = list(M.keys())
    all_dates = sorted({d for s in SYMS for d in M[s]["rng"]})
    trade_days = all_dates[10:]
    print(f"  {len(SYMS)} stocks | {len(trade_days)} trade days\n")

    def mval(s, m, lb):
        d = M[s]
        if m == "vol":
            xs = [d["rng"][x] for x in lb if x in d["rng"]]
            return sum(xs)/len(xs) if xs else None
        if m == "adx":
            xs = [d["adx"][x] for x in lb if x in d["adx"]]
            return sum(xs)/len(xs) if xs else None
        if m == "pnl":
            return sum(d["net"].get(x, 0.0) for x in lb)
        cl = [d["close"][x] for x in lb if x in d["close"]]
        if m == "ret":
            return abs(cl[-1]/cl[0]-1)*100 if len(cl) >= 2 and cl[0] else None
        if m == "er":
            if len(cl) < 2:
                return None
            path = sum(abs(cl[i]-cl[i-1]) for i in range(1, len(cl)))
            return abs(cl[-1]-cl[0])/path if path else 0.0
        if m == "lowflips":
            tr = [d["trend"][x] for x in lb if x in d["trend"]]
            flips = sum(1 for i in range(1, len(tr)) if tr[i] != tr[i-1] and tr[i] and tr[i-1])
            return -flips
        return None

    def sel_net(specs, N, K):
        daily = []
        for D in trade_days:
            gi = all_dates.index(D)
            lb = all_dates[max(0, gi-N):gi]
            comp = {}
            for m, _ in specs:
                comp[m] = {s: mval(s, m, lb) for s in SYMS}
                comp[m] = {s: v for s, v in comp[m].items() if v is not None}
            valid = set(SYMS)
            for m, _ in specs:
                valid &= set(comp[m])
            if not valid:
                daily.append(0.0); continue
            rs = {s: 0 for s in valid}
            for m, hb in specs:
                order = sorted(valid, key=lambda s: comp[m][s], reverse=hb)
                for r, s in enumerate(order):
                    rs[s] += r
            picked = sorted(valid, key=lambda s: rs[s])[:K]
            daily.append(sum(M[s]["net"].get(D, 0.0) for s in picked))
        return daily

    def stat(daily):
        h = len(daily)//2
        return sum(daily), sum(daily[:h]), sum(daily[h:])

    base = stat([sum(M[s]["net"].get(D, 0.0) for s in SYMS) for D in trade_days])
    print(f"BASELINE (all {len(SYMS)}): Rs{base[0]:+.0f} (1st {base[1]:+.0f} / 2nd {base[2]:+.0f})\n")

    specs_named = [
        ("vol",          [("vol", True)]),
        ("ret_abs",      [("ret", True)]),
        ("efficiency",   [("er", True)]),
        ("adx",          [("adx", True)]),
        ("lowflips",     [("lowflips", True)]),
        ("pnl",          [("pnl", True)]),
        ("vol+adx",      [("vol", True), ("adx", True)]),
        ("vol+eff",      [("vol", True), ("er", True)]),
        ("adx+eff",      [("adx", True), ("er", True)]),
        ("vol+adx+eff",  [("vol", True), ("adx", True), ("er", True)]),
        ("vol+lowflips", [("vol", True), ("lowflips", True)]),
        ("ret+eff",      [("ret", True), ("er", True)]),
        ("vol+adx+lowflips", [("vol", True), ("adx", True), ("lowflips", True)]),
    ]
    print(f"{'selector':>18} | " + " | ".join(f"N={N:<2}(full/1st/2nd)" for N in (3, 5, 10)))
    print("-" * 84)
    robust = []
    for name, specs in specs_named:
        row = f"{name:>18} | "
        cells = []
        for N in (3, 5, 10):
            t, s1, s2 = stat(sel_net(specs, N, 5))
            flag = "*" if (s1 > 0 and s2 > 0) else " "
            cells.append(f"{t:>+5.0f}/{s1:>+4.0f}/{s2:>+4.0f}{flag}")
            if s1 > 0 and s2 > 0:
                robust.append((min(s1, s2), t, name, N))  # rank by worst-half (balance)
        print(row + " | ".join(cells))
    print("\n* = positive in BOTH halves (robust).  Cells: full/1st-half/2nd-half net (Rs, 1x).")

    robust.sort(reverse=True)
    print("\nMost ROBUST (ranked by weakest half, i.e. most balanced & positive):")
    for worst, t, name, N in robust[:6]:
        print(f"  {name:>18} N={N:<2}  full Rs{t:+.0f}  weakest-half Rs{worst:+.0f}")

    if robust:
        _, _, bn, bN = robust[0]
        specs = dict(specs_named)[bn]
        print(f"\nK-sweep at most-balanced ({bn}, N={bN}):")
        for K in (3, 5, 8, 12):
            t, s1, s2 = stat(sel_net(specs, bN, K))
            print(f"  K={K:>2}: Rs{t:+.0f} (1st {s1:+.0f} / 2nd {s2:+.0f})")


if __name__ == "__main__":
    main()
