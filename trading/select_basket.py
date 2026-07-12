#!/usr/bin/env python3
"""
Pre-market daily stock selection (live).
Ranks the NIFTY 100 universe by a combination of VOLATILITY (10-day avg daily range %)
and TREND CLEANLINESS (few recent Supertrend flips) — the config that was robust in both
out-of-sample halves on the official Nifty 100. Picks top-K, writes data/today_basket.json
(all MIS) for the runner. Non-fatal on failure -> runner falls back to its fixed basket.

Usage: python3 select_basket.py         (writes data/today_basket.json)
       python3 select_basket.py --dry    (print only)
"""
from __future__ import annotations
import json, os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOOKBACK = 5           # trading days for the ranking window
TOP_K = 5
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/today_basket.json")

# Official NIFTY 100 (Nifty 50 + Next 50). Yahoo-unavailable names self-prune at download.
UNIVERSE = (
    "ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV "
    "BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK "
    "HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDIGO INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT "
    "M&M MARUTI MAXHEALTH NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SHRIRAMFIN SBIN "
    "SUNPHARMA TCS TATACONSUM TMPV TATASTEEL TECHM TITAN TRENT ULTRACEMCO WIPRO "
    "ABB ADANIENSOL ADANIGREEN ADANIPOWER AMBUJACEM BAJAJHLDNG BANKBARODA BPCL BRITANNIA "
    "BOSCHLTD CANBK CGPOWER CHOLAFIN CUMMINSIND DIVISLAB DLF DMART GAIL GODREJCP HDFCAMC "
    "HAL HINDZINC HYUNDAI INDHOTEL IOC IRFC JINDALSTEL LODHA LTIM MAZDOCK MUTHOOTFIN "
    "PIDILITIND PFC PNB RECLTD MOTHERSON SHREECEM SIEMENS SOLARINDS TATACAP TMCV TATAPOWER "
    "TORNTPHARM TVSMOTOR UNIONBANK UNITDSPR VBL VEDL ZYDUSLIFE").split()

# Stocks the account cannot trade as MIS -> excluded from selection.
MIS_BLOCKLIST = {"BALRAMCHIN"}


def _bars_for(sub):
    idx = sub.index
    try:
        idx = idx.tz_convert("Asia/Kolkata")
    except Exception:
        idx = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
    out = []
    for j, ts in enumerate(idx):
        o, h, l, c = (float(sub["Open"].iloc[j]), float(sub["High"].iloc[j]),
                      float(sub["Low"].iloc[j]), float(sub["Close"].iloc[j]))
        if o != o or c != c:
            continue
        out.append((ts.to_pydatetime(), o, h, l, c))
    return out


def score_stock(bars):
    """Return (avg_range_pct_over_lookback, supertrend_flip_count_over_lookback) or None."""
    from strategies.supertrend import SupertrendStrategy
    from marketdata import Candle
    from config import IST
    SupertrendStrategy.save_state = lambda self: None
    SupertrendStrategy._load_state = lambda self: None
    if len(bars) < 60:
        return None
    # daily range%
    day = {}
    for dt, o, h, l, c in bars:
        d = dt.date()
        v = day.get(d)
        if v is None:
            day[d] = [o, h, l]
        else:
            v[1] = max(v[1], h); v[2] = min(v[2], l)
    dates = sorted(day)
    lb = dates[-LOOKBACK:]
    if len(lb) < LOOKBACK:
        return None
    rng = sum((day[d][1]-day[d][2])/day[d][0]*100 for d in lb if day[d][0]) / len(lb)
    # supertrend flips over lookback
    strat = SupertrendStrategy("X", qty=1, multiplier=1.5)
    trend_seq = []
    for dt, o, h, l, c in bars:
        strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c))
        if dt.date() in lb:
            trend_seq.append(strat._trend)
    flips = sum(1 for i in range(1, len(trend_seq))
                if trend_seq[i] != trend_seq[i-1] and trend_seq[i] and trend_seq[i-1])
    return (rng, flips)


def select():
    import yfinance as yf
    tickers = [s + ".NS" for s in UNIVERSE if s not in MIS_BLOCKLIST]
    scores = {}
    for i in range(0, len(tickers), 30):
        chunk = tickers[i:i+30]
        df = yf.download(chunk, period="12d", interval="15m", group_by="ticker",
                         progress=False, auto_adjust=False, threads=True)
        for tk in chunk:
            sym = tk[:-3]
            try:
                sub = df[tk] if len(chunk) > 1 else df
                s = score_stock(_bars_for(sub))
                if s:
                    scores[sym] = s
            except Exception:
                continue
    # rank-sum: high range% (better) + low flips (better)
    syms = list(scores)
    by_rng = sorted(syms, key=lambda s: scores[s][0], reverse=True)
    by_flip = sorted(syms, key=lambda s: scores[s][1])
    rank = {s: 0 for s in syms}
    for r, s in enumerate(by_rng):
        rank[s] += r
    for r, s in enumerate(by_flip):
        rank[s] += r
    ranked = sorted(syms, key=lambda s: rank[s])
    return ranked, scores


def main():
    dry = "--dry" in sys.argv
    try:
        ranked, scores = select()
    except Exception as e:
        print(f"[select_basket] selection FAILED ({e}) — runner will use its default basket")
        sys.exit(0)

    if len(ranked) < TOP_K:
        print(f"[select_basket] only {len(ranked)} scored (<{TOP_K}) — leaving default basket")
        sys.exit(0)

    top = ranked[:TOP_K]
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    payload = {"date": today, "lookback": LOOKBACK, "selector": "vol+lowflips",
               "stocks": [{"symbol": s, "mode": "MIS"} for s in top]}

    print(f"[select_basket] {today}  top {TOP_K} by vol + low-flips ({LOOKBACK}d):")
    for s in top:
        rng, fl = scores[s]
        print(f"    {s:12s} range={rng:4.2f}%  flips={fl}")
    print("   next:", ", ".join(ranked[TOP_K:TOP_K+6]))

    if dry:
        print("   [dry run — not written]")
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(payload, open(OUT, "w"), indent=2)
    print(f"   wrote {OUT}")


if __name__ == "__main__":
    main()
