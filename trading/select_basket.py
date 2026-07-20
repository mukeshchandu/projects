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
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategies.supertrend import SupertrendStrategy
_ORIG_SAVE = SupertrendStrategy.save_state
_ORIG_LOAD = SupertrendStrategy._load_state
SupertrendStrategy.save_state = lambda self: None    # disabled during scoring (re-enabled to warm)
SupertrendStrategy._load_state = lambda self: None

LOOKBACK = 5           # trading days for the ranking window
TOP_K = 15             # stocks observed/subscribed per session (more names = more flips)
EMA_PERIOD = 50        # must match the runner's dynamic-basket EMA filter
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/today_basket.json")

# Official NIFTY 100 (Nifty 50 + Next 50). Yahoo-unavailable names self-prune at download.
# This hardcoded list is the FALLBACK; the live universe is refreshed weekly (Fri ~15:50) by
# update_universe.py into data/nifty100.json and loaded below if present.
UNIVERSE_FALLBACK = (
    "ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV "
    "BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK "
    "HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDIGO INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT "
    "M&M MARUTI MAXHEALTH NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SHRIRAMFIN SBIN "
    "SUNPHARMA TCS TATACONSUM TMPV TATASTEEL TECHM TITAN TRENT ULTRACEMCO WIPRO "
    "ABB ADANIENSOL ADANIGREEN ADANIPOWER AMBUJACEM BAJAJHLDNG BANKBARODA BPCL BRITANNIA "
    "BOSCHLTD CANBK CGPOWER CHOLAFIN CUMMINSIND DIVISLAB DLF DMART GAIL GODREJCP HDFCAMC "
    "HAL HINDZINC HYUNDAI INDHOTEL IOC IRFC JINDALSTEL LODHA MAZDOCK MUTHOOTFIN "
    "PIDILITIND PFC PNB RECLTD MOTHERSON SHREECEM SIEMENS SOLARINDS TATACAP TMCV TATAPOWER "
    "TORNTPHARM TVSMOTOR UNIONBANK UNITDSPR VBL VEDL ZYDUSLIFE").split()

def _load_universe():
    """Weekly-refreshed Nifty 100 from data/nifty100.json (written by update_universe.py);
    fall back to the hardcoded list if the file is missing/empty/stale-unreadable."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/nifty100.json")
    try:
        with open(path) as fh:
            data = __import__("json").load(fh)
        syms = data.get("symbols") if isinstance(data, dict) else data
        if isinstance(syms, list) and len(syms) >= 50:
            print(f"[select_basket] universe from {path}: {len(syms)} symbols "
                  f"(updated {data.get('updated','?') if isinstance(data, dict) else '?'})")
            return [str(s).strip().upper() for s in syms if str(s).strip()]
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[select_basket] universe file unreadable ({e}) — using hardcoded fallback")
    return UNIVERSE_FALLBACK

UNIVERSE = _load_universe()

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
    from marketdata import Candle
    from config import IST
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
    bars_by_sym = {}
    for i in range(0, len(tickers), 30):
        chunk = tickers[i:i+30]
        df = yf.download(chunk, period="12d", interval="15m", group_by="ticker",
                         progress=False, auto_adjust=False, threads=True)
        for tk in chunk:
            sym = tk[:-3]
            try:
                sub = df[tk] if len(chunk) > 1 else df
                bars = _bars_for(sub)
                s = score_stock(bars)
                if s:
                    scores[sym] = s
                    bars_by_sym[sym] = bars
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
    return ranked, scores, bars_by_sym


def warmup(symbols, bars_by_sym):
    """Seed ATR/Supertrend/EMA state for the selected stocks from recent 15-min history,
    so the morning runner loads them warm and can signal from 9:15 (no cold start)."""
    from marketdata import Candle
    from config import IST
    SupertrendStrategy.save_state = _ORIG_SAVE     # re-enable persistence for the warm-up
    SupertrendStrategy._load_state = _ORIG_LOAD
    print("   warming selected stocks (ATR + EMA state):")
    for sym in symbols:
        bars = bars_by_sym.get(sym)
        if not bars:
            print(f"     {sym:12s} — no bars, skipped (will warm live)")
            continue
        strat = SupertrendStrategy(sym, qty=1, multiplier=1.5, ema_period=EMA_PERIOD)
        strat._reset_all()                          # discard any stale state; rebuild from history
        for dt, o, h, l, c in bars:
            strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c))
        strat.save_state()
        atr = f"{strat._atr:.4f}" if strat._atr else "warming"
        print(f"     {sym:12s} atr={atr}  trend={strat._trend}  candles={len(strat._candles)}  ema={'set' if strat._ema else 'none'}")


def main():
    dry = "--dry" in sys.argv
    try:
        ranked, scores, bars_by_sym = select()
    except Exception as e:
        print(f"[select_basket] selection FAILED ({e}) — runner will use its default basket")
        sys.exit(0)

    if len(ranked) < TOP_K:
        print(f"[select_basket] only {len(ranked)} scored (<{TOP_K}) — leaving default basket")
        sys.exit(0)

    top = ranked[:TOP_K]
    # Stamp the basket for the session it will be traded in:
    #   run PRE-OPEN (before 09:15 IST, e.g. the 08:00 morning cron) -> TODAY's session;
    #   run at/after open (e.g. an evening run)                      -> the NEXT session.
    # Skip weekends either way. Morning-run stamping uses yesterday-close history, which is
    # exactly what the 08:45 runner then loads warm.
    from config import IST, MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE
    now = datetime.now(IST)
    if "--today" in sys.argv:
        # Force TODAY's session — for an intentional mid-session re-warm + runner restart.
        session = now.date()
    else:
        before_open = (now.hour, now.minute) < (MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE)
        session = now.date() + (timedelta(0) if before_open else timedelta(days=1))
    while session.weekday() >= 5:      # Sat/Sun -> roll to Monday
        session += timedelta(days=1)
    session_str = session.strftime("%Y-%m-%d")
    payload = {"date": session_str, "generated": now.strftime("%Y-%m-%d %H:%M IST"),
               "lookback": LOOKBACK, "selector": "vol+lowflips",
               "stocks": [{"symbol": s, "mode": "MIS"} for s in top]}

    print(f"[select_basket] generated {now:%Y-%m-%d %H:%M} IST -> for session {session_str}  "
          f"top {TOP_K} by vol + low-flips ({LOOKBACK}d):")
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
    warmup(top, bars_by_sym)


if __name__ == "__main__":
    main()
