#!/usr/bin/env python3
"""
Extended backtest on ~60 days of 15-min Yahoo Finance bars for the 8-stock basket.
Runs the REAL SupertrendStrategy.on_candle, replicates runner EOD (MIS squared 15:00,
CNC held overnight), applies the Flattrade cost model. Exits are evaluated at bar close
(incl. breakeven) — intrabar stop precision is not modeled, so stop results are mildly
optimistic; this test is about ENTRY-SIGNAL EDGE across trending + choppy regimes.

Usage: python3 bt_yahoo.py
"""
from __future__ import annotations
import math, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
import strategies.supertrend as st_mod
from config import IST, EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import Candle
from paper import _get_tick
from strategies.supertrend import SupertrendStrategy

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

SYMS = {  # symbol -> (yahoo_ticker, mode)
    "HFCL": ("HFCL.NS", "CNC"), "BANKBARODA": ("BANKBARODA.NS", "CNC"),
    "NMDC": ("NMDC.NS", "CNC"), "CESC": ("CESC.NS", "CNC"),
    "ZEEL": ("ZEEL.NS", "CNC"), "BALRAMCHIN": ("BALRAMCHIN.NS", "CNC"),
    "GRANULES": ("GRANULES.NS", "MIS"), "SUZLON": ("SUZLON.NS", "MIS"),
}


def fill_price(side, px):
    t = _get_tick(px)
    if side == "BUY":
        return round((math.ceil(round(px / t, 8)) + 1) * t, 4)
    return round((math.floor(round(px / t, 8)) - 1) * t, 4)


def round_trip_cost(entry_val, exit_val, mode):
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
    return stt + stamp + dp + exch + sebi + 0.18 * (exch + sebi)


def is_eod(dt):
    return dt.hour > EOD_EXIT_HOUR or (dt.hour == EOD_EXIT_HOUR and dt.minute >= EOD_EXIT_MINUTE)


def fetch(ticker):
    df = yf.download(ticker, period="60d", interval="15m", progress=False, auto_adjust=False)
    if df is None or len(df) == 0:
        return []
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    idx = df.index
    try:
        idx = idx.tz_convert("Asia/Kolkata")
    except Exception:
        idx = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
    bars = []
    for i, ts in enumerate(idx):
        o, h, l, c = float(df["Open"].iloc[i]), float(df["High"].iloc[i]), float(df["Low"].iloc[i]), float(df["Close"].iloc[i])
        if any(map(lambda x: x != x, [o, h, l, c])):  # skip NaN
            continue
        bars.append((ts.to_pydatetime(), o, h, l, c))
    return bars


def run(symbol_bars, *, breakeven, force_long_only=None, capital=5000):
    st_mod.BREAKEVEN_TRIGGER_MULT = breakeven
    st_mod.TRAIL_PEAK_MULT = 0.0
    st_mod.TAKE_PROFIT_MULT = 0.0
    trades = []
    for sym, (ticker, mode) in SYMS.items():
        bars = symbol_bars.get(sym, [])
        if not bars:
            continue
        long_only = (mode == "CNC") if force_long_only is None else force_long_only
        strat = SupertrendStrategy(sym, qty=1, long_only=long_only)
        pos = None
        prev_eod = False

        def open_pos(side, px, ts):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            lev = 4 if mode == "MIS" else 1
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital * lev / ef)), "ts": ts}

        def close_pos(px, ts, reason):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            gross = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            cost = round_trip_cost(pos["ef"] * q, xf * q, mode)
            trades.append({"sym": sym, "mode": mode, "ts": ts, "gross": gross,
                           "cost": cost, "net": gross - cost, "reason": reason})
            pos = None

        for dt, o, h, l, c in bars:
            if is_eod(dt):
                if pos is not None and mode == "MIS" and not prev_eod:
                    close_pos(c, dt, "EOD")
                prev_eod = True
                continue
            prev_eod = False
            candle = Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)
            for sig in strat.on_candle(candle):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    open_pos("LONG" if a == "BUY" else "SHORT", sig["price"], dt)
                elif a == "EXIT" and pos is not None:
                    close_pos(sig["price"], dt, sig["reason"].split("|")[0].strip())
        if pos is not None:
            close_pos(bars[-1][4], bars[-1][0], "END")
    return trades


def summ(trades):
    n = len(trades)
    if n == 0:
        return "no trades"
    net = sum(t["net"] for t in trades)
    gross = sum(t["gross"] for t in trades)
    cost = sum(t["cost"] for t in trades)
    win = sum(1 for t in trades if t["net"] > 0) / n * 100
    return f"trades={n:3d}  win={win:4.0f}%  gross=Rs{gross:+9.1f}  costs=Rs{cost:8.1f}  NET=Rs{net:+9.1f}"


def main():
    print("Downloading 60d x 15m bars for 8 symbols from Yahoo...\n")
    sb, bh = {}, 0.0
    for sym, (ticker, mode) in SYMS.items():
        bars = fetch(ticker)
        sb[sym] = bars
        if bars:
            first, last = bars[0][4], bars[-1][4]
            bh += (last - first) * max(1, int(5000 / first))
            span = f"{bars[0][0].date()} .. {bars[-1][0].date()}"
            print(f"  {sym:12s} {len(bars):5d} bars  {span}  ({first:.1f} -> {last:.1f})")
        else:
            print(f"  {sym:12s} NO DATA")
    print()

    print("A. PRODUCTION (long-only CNC, breakeven 1xATR)")
    prod = run(sb, breakeven=1.0)
    print("   ", summ(prod))
    print("B. Breakeven OFF")
    print("   ", summ(run(sb, breakeven=0.0)))
    print("C. Long+SHORT everywhere")
    print("   ", summ(run(sb, breakeven=1.0, force_long_only=False)))
    print(f"\nBENCHMARK  buy & hold all 8 (Rs5000 each): Rs{bh:+.1f}\n")

    # weekly net (regime dependence) for production
    print("Weekly NET — PRODUCTION (does it depend on regime?):")
    wk = {}
    for t in prod:
        y, w, _ = t["ts"].isocalendar()
        k = f"{y}-W{w:02d}"
        wk[k] = wk.get(k, 0.0) + t["net"]
    for k in sorted(wk):
        bar = "#" * min(40, int(abs(wk[k]) / 50))
        print(f"  {k}  Rs{wk[k]:+8.1f}  {bar}")

    print("\nPer-symbol NET — PRODUCTION:")
    bs = {}
    for t in prod:
        d = bs.setdefault(t["sym"], {"mode": t["mode"], "n": 0, "net": 0.0})
        d["n"] += 1; d["net"] += t["net"]
    for sym, d in sorted(bs.items(), key=lambda x: x[1]["net"]):
        print(f"  {sym:12s} {d['mode']}  trades={d['n']:3d}  net=Rs{d['net']:+8.1f}")


if __name__ == "__main__":
    main()
