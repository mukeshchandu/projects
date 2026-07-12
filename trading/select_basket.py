#!/usr/bin/env python3
"""
Pre-market daily stock selection (live).
Ranks a liquid NSE universe by 10-day average daily range % (volatility) and writes the
top-K to data/today_basket.json for the runner to trade that day (all MIS). Uses Yahoo
daily data (one batched call). If it fails or returns too few names, it writes nothing —
the runner then falls back to its built-in basket. Safe to run pre-market from cron.

Usage: python3 select_basket.py            (writes data/today_basket.json)
       python3 select_basket.py --dry       (print only, no write)
"""
from __future__ import annotations
import json, os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

LOOKBACK = 10          # sessions
TOP_K = 5
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/today_basket.json")

# Liquid NSE universe to pick from (MIS-tradeable). Trim/extend as you like.
UNIVERSE = ("RELIANCE TATAMOTORS ICICIBANK SBIN INFY TATASTEEL ADANIENT IDEA PNB IOC "
            "HINDALCO AXISBANK WIPRO ONGC COALINDIA HFCL NMDC CESC GRANULES SUZLON "
            "SAIL VEDL JINDALSTEL RECLTD PFC IRFC BEL HAL TATAPOWER GMRINFRA "
            "ASHOKLEY TVSMOTOR MOTHERSON BANKBARODA CANBK IDFCFIRSTB FEDERALBNK "
            "AUROPHARMA LUPIN BIOCON UPL DEEPAKNTR ADANIPORTS DLF ZEEL YESBANK").split()

# Stocks the account cannot trade as MIS -> exclude from the MIS universe.
MIS_BLOCKLIST = {"BALRAMCHIN"}

IST_TZ = "Asia/Kolkata"


def select():
    import yfinance as yf
    tickers = [s + ".NS" for s in UNIVERSE if s not in MIS_BLOCKLIST]
    df = yf.download(tickers, period="20d", interval="1d", group_by="ticker",
                     progress=False, auto_adjust=False, threads=True)
    scores = {}
    for tk in tickers:
        sym = tk[:-3]
        try:
            sub = df[tk] if len(tickers) > 1 else df
            o = sub["Open"].dropna().tolist()
            h = sub["High"].dropna().tolist()
            l = sub["Low"].dropna().tolist()
            n = min(len(o), len(h), len(l))
            if n < LOOKBACK:
                continue
            ranges = [(h[i] - l[i]) / o[i] * 100 for i in range(n - LOOKBACK, n) if o[i]]
            if ranges:
                scores[sym] = sum(ranges) / len(ranges)
        except Exception:
            continue
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked


def main():
    dry = "--dry" in sys.argv
    try:
        ranked = select()
    except Exception as e:
        print(f"[select_basket] selection FAILED ({e}) — runner will use its default basket")
        sys.exit(0)   # non-fatal: no file written, runner falls back

    if len(ranked) < TOP_K:
        print(f"[select_basket] only {len(ranked)} scored (<{TOP_K}) — leaving default basket")
        sys.exit(0)

    top = ranked[:TOP_K]
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    payload = {"date": today, "lookback": LOOKBACK,
               "stocks": [{"symbol": s, "mode": "MIS"} for s, _ in top]}

    print(f"[select_basket] {today}  top {TOP_K} by {LOOKBACK}d avg range%:")
    for s, sc in top:
        print(f"    {s:12s} {sc:5.2f}%")
    print("   next best:", ", ".join(f"{s}({sc:.1f})" for s, sc in ranked[TOP_K:TOP_K+5]))

    if dry:
        print("   [dry run — not written]")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(payload, open(OUT, "w"), indent=2)
    print(f"   wrote {OUT}")


if __name__ == "__main__":
    main()
