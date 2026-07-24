#!/usr/bin/env python3
"""
Long-only vs reversal for the LIVE dynamic-MIS config.
Same selection (vol + low-flips, Nifty 100, top-5, N=5 lookback) and the same real
SupertrendStrategy (EMA-50 filter, mult 1.5), run two ways:
  - reversal  (long_only=False): shorts on bear flips  [current production]
  - long-only (long_only=True):  flat during downtrends [the proposed change]
Walk-forward, OOS split-half, MIS costs, Rs5000/trade. Uses cached Nifty-100 bars.
"""
from __future__ import annotations
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fill_price, round_trip_cost, is_eod
from bt_select import download_all

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

LOOKBACK, TOP_K, EMA = 5, 5, 50


def daily_net(bars, long_only, capital=5000):
    st_mod.BREAKEVEN_TRIGGER_MULT = 1.0; st_mod.TRAIL_PEAK_MULT = 0.0; st_mod.TAKE_PROFIT_MULT = 0.0
    strat = SupertrendStrategy("X", 1, multiplier=1.5, long_only=long_only, ema_period=EMA)
    pos = None; prev_eod = False
    net_by_day = {}; trend_by_day = {}

    def close(px, d):
        nonlocal pos
        if pos is None:
            return
        xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
        q = pos["qty"]
        g = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
        net_by_day[d] = net_by_day.get(d, 0.0) + (g - round_trip_cost(pos["ef"]*q, xf*q, "MIS"))
        pos = None

    def openp(side, px):
        nonlocal pos
        ef = fill_price("BUY" if side == "LONG" else "SELL", px)
        pos = {"side": side, "ef": ef, "qty": max(1, int(capital/ef))}

    for dt, o, h, l, c in bars:
        d = dt.date()
        if is_eod(dt):
            if pos is not None and not prev_eod:
                close(c, d)
            prev_eod = True
            continue
        prev_eod = False
        for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
            a = sig["action"]
            if a in ("BUY", "SELL") and pos is None:
                openp("LONG" if a == "BUY" else "SHORT", sig["price"])
            elif a == "EXIT" and pos is not None:
                close(sig["price"], d)
        trend_by_day[d] = strat._trend
    if pos is not None:
        close(bars[-1][4], bars[-1][0].date())
    return net_by_day, trend_by_day


def day_range(bars):
    ohlc = {}
    for dt, o, h, l, c in bars:
        d = dt.date(); v = ohlc.get(d)
        if v is None:
            ohlc[d] = [o, h, l]
        else:
            v[1] = max(v[1], h); v[2] = min(v[2], l)
    return {d: (v[1]-v[2])/v[0]*100 for d, v in ohlc.items() if v[0]}


def main():
    print("Loading cached Nifty-100 bars...")
    bars = download_all()
    NET_REV, NET_LO, RNG, TREND = {}, {}, {}, {}
    for sym, b in bars.items():
        if not b:
            continue
        NET_REV[sym], TREND[sym] = daily_net(b, long_only=False)
        NET_LO[sym], _ = daily_net(b, long_only=True)
        RNG[sym] = day_range(b)
    syms = list(NET_REV)
    all_dates = sorted({d for s in syms for d in RNG.get(s, {})})
    trade_days = all_dates[10:]
    print(f"  {len(syms)} stocks | {len(trade_days)} trade days\n")

    def score(sym, lb):
        rs = [RNG[sym][d] for d in lb if d in RNG.get(sym, {})]
        tr = [TREND[sym][d] for d in lb if d in TREND.get(sym, {})]
        if not rs or len(tr) < 2:
            return None
        vol = sum(rs)/len(rs)
        flips = sum(1 for i in range(1, len(tr)) if tr[i] != tr[i-1] and tr[i] and tr[i-1])
        return vol, flips

    def selective(net_map):
        daily = []
        for D in trade_days:
            gi = all_dates.index(D)
            lb = all_dates[max(0, gi-LOOKBACK):gi]
            sc = {s: score(s, lb) for s in syms}
            sc = {s: v for s, v in sc.items() if v is not None}
            if not sc:
                daily.append(0.0); continue
            by_vol  = sorted(sc, key=lambda s: sc[s][0], reverse=True)
            by_flip = sorted(sc, key=lambda s: sc[s][1])
            rank = {s: 0 for s in sc}
            for r, s in enumerate(by_vol):  rank[s] += r
            for r, s in enumerate(by_flip): rank[s] += r
            picked = sorted(sc, key=lambda s: rank[s])[:TOP_K]
            daily.append(sum(net_map.get(s, {}).get(D, 0.0) for s in picked))
        return daily

    def stat(daily):
        h = len(daily)//2
        return sum(daily), sum(daily[:h]), sum(daily[h:])

    rev = stat(selective(NET_REV))
    lo  = stat(selective(NET_LO))
    print("Dynamic vol+lowflips selection (top 5), MIS + EMA-50, Rs5000/trade:\n")
    print(f"  {'mode':>22} | {'FULL 60d':>12} | {'1st half':>10} | {'2nd half':>10} | robust")
    print("  " + "-" * 70)
    for name, s in [("REVERSAL (production)", rev), ("LONG-ONLY (proposed)", lo)]:
        rob = "OK both+" if (s[1] > 0 and s[2] > 0) else ""
        print(f"  {name:>22} | Rs{s[0]:>+9.0f} | Rs{s[1]:>+7.0f} | Rs{s[2]:>+7.0f} | {rob}")


if __name__ == "__main__":
    main()
