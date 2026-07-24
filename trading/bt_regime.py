#!/usr/bin/env python3
"""
Regime-filter test: gate the MIS strategy so it only trades when genuinely trending.
Filters: ADX threshold (trend strength) and EMA-50 alignment (trade with the trend).
MIS-only, mult 1.5, long+short, Rs5000 notional. OOS split-half, both universes.
"""
from __future__ import annotations
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fill_price, round_trip_cost, is_eod
from bt_mis import load_universe, CURRENT8, BROAD


def compute_adx(bars, period=14):
    n = len(bars)
    adx = [None] * n
    if n <= period + 1:
        return adx
    tr = [0.0] * n; pdm = [0.0] * n; mdm = [0.0] * n
    for i in range(1, n):
        h, l, pc, ph, pl = bars[i][2], bars[i][3], bars[i-1][4], bars[i-1][2], bars[i-1][3]
        up, dn = h - ph, pl - l
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = sum(tr[1:period+1]); sp = sum(pdm[1:period+1]); sm = sum(mdm[1:period+1])
    dxs = []
    for i in range(period+1, n):
        atr = atr - atr/period + tr[i]
        sp = sp - sp/period + pdm[i]
        sm = sm - sm/period + mdm[i]
        pdi = 100*sp/atr if atr else 0
        mdi = 100*sm/atr if atr else 0
        dxs.append((i, 100*abs(pdi-mdi)/(pdi+mdi) if (pdi+mdi) else 0))
    if len(dxs) >= period:
        av = sum(d for _, d in dxs[:period]) / period
        adx[dxs[period-1][0]] = av
        for k in range(period, len(dxs)):
            i, dx = dxs[k]
            av = (av*(period-1) + dx) / period
            adx[i] = av
    return adx


def compute_ema(bars, period=50):
    n = len(bars); ema = [None]*n
    if not n:
        return ema
    k = 2/(period+1); e = bars[0][4]
    for i in range(n):
        e = bars[i][4] if i == 0 else bars[i][4]*k + e*(1-k)
        ema[i] = e
    return ema


def run(bars_by_sym, *, mult=1.5, adx_thr=None, ema_align=False, capital=5000):
    trades = []
    for sym, bars in bars_by_sym.items():
        if not bars:
            continue
        adx = compute_adx(bars) if adx_thr is not None else None
        ema = compute_ema(bars) if ema_align else None
        strat = SupertrendStrategy(sym, qty=1, multiplier=mult, long_only=False)
        pos = None; prev_eod = False

        def openp(side, px):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital/ef))}

        def closep(px):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            g = (xf-pos["ef"])*q if pos["side"] == "LONG" else (pos["ef"]-xf)*q
            trades.append({"sym": sym, "net": g - round_trip_cost(pos["ef"]*q, xf*q, "MIS")})
            pos = None

        for i, (dt, o, h, l, c) in enumerate(bars):
            if is_eod(dt):
                if pos is not None and not prev_eod:
                    closep(c)
                prev_eod = True
                continue
            prev_eod = False
            for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    ok = True
                    if adx_thr is not None:
                        ok = ok and (adx[i] is not None and adx[i] >= adx_thr)
                    if ema_align and ema[i] is not None:
                        ok = ok and (c > ema[i] if a == "BUY" else c < ema[i])
                    if ok:
                        openp("LONG" if a == "BUY" else "SHORT", sig["price"])
                elif a == "EXIT" and pos is not None:
                    closep(sig["price"])
        if pos is not None:
            closep(bars[-1][4])
    return trades


def netv(tr):
    return (len(tr), sum(t["net"] for t in tr) if tr else 0.0)


def split(bars):
    f, s = {}, {}
    for sym, b in bars.items():
        m = len(b)//2; f[sym], s[sym] = b[:m], b[m:]
    return f, s


def report(tag, bars):
    ok = [s for s in bars if bars[s]]
    print(f"\n{'='*72}\nUNIVERSE: {tag}  ({len(ok)} stocks)  — MIS-only, m1.5, long+short")
    first, second = split(bars)
    print(f"  {'filter':>26} | {'FULL 60d':>16} | {'1st half':>12} | {'2nd half':>12} | robust")
    print("  " + "-"*76)
    configs = [
        ("none (baseline)", dict()),
        ("ADX>=20", dict(adx_thr=20)),
        ("ADX>=25", dict(adx_thr=25)),
        ("EMA-50 align", dict(ema_align=True)),
        ("ADX>=20 + EMA-50", dict(adx_thr=20, ema_align=True)),
        ("ADX>=25 + EMA-50", dict(adx_thr=25, ema_align=True)),
    ]
    for name, kw in configs:
        nf, netf = netv(run(bars, **kw))
        _, net1 = netv(run(first, **kw))
        _, net2 = netv(run(second, **kw))
        rob = "OK both+" if (net1 > 0 and net2 > 0) else ""
        print(f"  {name:>26} | Rs{netf:>+8.0f} ({nf:3d}) | Rs{net1:>+7.0f} | Rs{net2:>+7.0f} | {rob}")


def main():
    print("Loading cached universes...")
    report("CURRENT 8", load_universe("cur8", CURRENT8))
    report("BROAD (15 liquid NSE)", load_universe("broad", BROAD))
    print("\nRobust = net positive in BOTH halves (survives out-of-sample).")


if __name__ == "__main__":
    main()
