#!/usr/bin/env python3
"""
backtest_stocks.py — Supertrend (atr=14, mult=1.5, 15-min) on 30 stocks, 30 days
Run: python backtest_stocks.py
"""
import warnings, sys
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")

# ── Stocks to test ────────────────────────────────────────────────────────
STOCKS = [
    # More PSU Power/Renewable
    ("TATAPOWER.NS",   "TATAPOWER"),
    ("ADANIPOWER.NS",  "ADANIPOWER"),
    ("ADANIGREEN.NS",  "ADANIGREEN"),
    ("JSWENERGY.NS",   "JSWENERGY"),
    ("INOXWIND.NS",    "INOXWIND"),
    ("CESC.NS",        "CESC"),
    # More Metals
    ("HINDALCO.NS",    "HINDALCO"),
    ("NMDC.NS",        "NMDC"),
    ("VEDL.NS",        "VEDL"),
    ("HINDCOPPER.NS",  "HINDCOPPER"),
    ("JSWSTEEL.NS",    "JSWSTEEL"),
    ("MOIL.NS",        "MOIL"),
    # More PSU Banks
    ("BANKINDIA.NS",   "BANKINDIA"),
    ("MAHABANK.NS",    "MAHABANK"),
    ("IOB.NS",         "IOB"),
    ("CENTRALBANK.NS", "CENTRALBANK"),
    ("UCOBANK.NS",     "UCOBANK"),
    # Current basket
    ("IDEA.NS",        "IDEA"),
    ("SUZLON.NS",      "SUZLON"),
    ("YESBANK.NS",     "YESBANK"),
    ("NHPC.NS",        "NHPC"),
    ("SAIL.NS",        "SAIL"),
    ("PNB.NS",         "PNB"),
    ("RPOWER.NS",      "RPOWER"),
    ("TATASTEEL.NS",   "TATASTEEL"),
    ("IDFCFIRSTB.NS",  "IDFCFIRSTB"),
    # PSU banks
    ("BANKBARODA.NS",  "BANKBARODA"),
    ("CANARABANK.NS",  "CANARABANK"),
    ("UNIONBANK.NS",   "UNIONBANK"),
    # PSU energy / infra
    ("NTPC.NS",        "NTPC"),
    ("RECLTD.NS",      "RECLTD"),
    ("PFC.NS",         "PFC"),
    ("COALINDIA.NS",   "COALINDIA"),
    ("POWERGRID.NS",   "POWERGRID"),
    # Mid-cap / others
    ("ZOMATO.NS",      "ZOMATO"),
    ("ADANIPORTS.NS",  "ADANIPORTS"),
    ("GMRINFRA.NS",    "GMRINFRA"),
    ("IDEA.NS",        "IDEA2"),     # duplicate removed below
    ("TRIDENT.NS",     "TRIDENT"),
    ("HFCL.NS",        "HFCL"),
    ("NATIONALUM.NS",  "NATIONALUM"),
    ("SJVN.NS",        "SJVN"),
    ("GOLDBEES.NS",    "GOLD"),
    ("SILVERBEES.NS",  "SILVER"),
]

# Remove duplicate
STOCKS = list({s[1]: s for s in STOCKS}.values())

ATR_PERIOD   = 14
MULTIPLIER   = 1.5
INTERVAL     = "15m"
LOOKBACK     = 30
MAX_CAPITAL  = 10_000
SLIPPAGE_BPS = 3.0
MIN_TRADES   = 3

# ── Fetch ─────────────────────────────────────────────────────────────────
def fetch(ticker):
    end   = datetime.now()
    start = end - timedelta(days=LOOKBACK)
    try:
        df = yf.download(ticker, start=start, end=end,
                         interval=INTERVAL, auto_adjust=True, progress=False)
    except:
        return pd.DataFrame()
    if df.empty: return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    if df.index.tz is None:
        df.index = df.index.tz_localize("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")
    mask = (
        ((df.index.hour == 9)  & (df.index.minute >= 15)) |
        ((df.index.hour > 9)   & (df.index.hour < 15))    |
        ((df.index.hour == 15) & (df.index.minute <= 30))
    )
    return df[mask].dropna()

# ── Supertrend ────────────────────────────────────────────────────────────
def supertrend(df):
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    n = len(c)
    pc = np.empty(n); pc[0] = c[0]; pc[1:] = c[:-1]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(n); atr[0] = tr[0]
    a = 1.0 / ATR_PERIOD
    for i in range(1, n):
        atr[i] = atr[i-1]*(1-a) + tr[i]*a
    hl2 = (h+l)/2
    bu = hl2 + MULTIPLIER*atr
    bl = hl2 - MULTIPLIER*atr
    upper = np.zeros(n); upper[0] = bu[0]
    lower = np.zeros(n); lower[0] = bl[0]
    for i in range(1,n):
        upper[i] = bu[i] if (bu[i]<upper[i-1] or c[i-1]>upper[i-1]) else upper[i-1]
        lower[i] = bl[i] if (bl[i]>lower[i-1] or c[i-1]<lower[i-1]) else lower[i-1]
    st = np.zeros(n); st[0] = upper[0]
    trend = np.zeros(n, dtype=int); trend[0] = -1
    for i in range(1,n):
        if st[i-1]==upper[i-1]:
            st[i],trend[i] = (lower[i],1) if c[i]>upper[i] else (upper[i],-1)
        else:
            st[i],trend[i] = (upper[i],-1) if c[i]<lower[i] else (lower[i],1)
    out = df.copy()
    out["st"] = st; out["trend"] = trend
    return out

# ── Backtest ──────────────────────────────────────────────────────────────
def backtest(df):
    slip = SLIPPAGE_BPS/10_000
    c    = df["close"].values
    st   = df["st"].values
    tr   = df["trend"].values
    idx  = df.index
    trades, pos, epx, ptrd = [], 0, 0.0, 0
    for i in range(1, len(df)):
        h, m  = idx[i].hour, idx[i].minute
        close = c[i]
        # EOD exit
        if h > 15 or (h == 15 and m >= 0):
            if pos != 0:
                xp  = close*(1-slip) if pos==1 else close*(1+slip)
                pnl = ((xp-epx) if pos==1 else (epx-xp)) * max(1, int(MAX_CAPITAL/epx))
                trades.append(pnl); pos = 0
            ptrd = 0; continue
        # Trail SL
        if pos==1 and close<st[i]:
            xp  = st[i]*(1-slip)
            trades.append((xp-epx)*max(1,int(MAX_CAPITAL/epx))); pos=0
        elif pos==-1 and close>st[i]:
            xp  = st[i]*(1+slip)
            trades.append((epx-xp)*max(1,int(MAX_CAPITAL/epx))); pos=0
        # Entry
        if pos==0 and ptrd!=0 and tr[i]!=ptrd:
            if tr[i]==1:  epx=close*(1+slip); pos=1
            else:          epx=close*(1-slip); pos=-1
        ptrd = tr[i]
    return trades

# ── Run all ───────────────────────────────────────────────────────────────
print(f"Supertrend atr={ATR_PERIOD} mult={MULTIPLIER} | 15-min | {LOOKBACK} days\n")
print(f"Fetching {len(STOCKS)} stocks...")

results = []
for ticker, sym in STOCKS:
    df = fetch(ticker)
    if df.empty: print(f"  {sym:14s}  NO DATA"); continue
    df  = supertrend(df)
    pnls = backtest(df)
    if len(pnls) < MIN_TRADES:
        print(f"  {sym:14s}  {len(pnls)} trades — too few"); continue
    p   = np.array(pnls)
    w   = p[p>0]; l = p[p<=0]
    sh  = float(p.mean()/p.std()*np.sqrt(252)) if p.std()>0 else 0
    wr  = len(w)/len(p)*100
    exp = float(len(w)/len(p)*w.mean() - len(l)/len(p)*abs(l.mean())) if len(w) and len(l) else (w.mean() if len(w) else l.mean())
    dd  = float(abs((np.cumsum(p) - np.maximum.accumulate(np.cumsum(p))).min()))
    results.append({"sym":sym,"n":len(p),"pnl":round(float(p.sum()),2),
                    "sharpe":round(sh,2),"wr":round(wr,1),
                    "exp":round(exp,2),"dd":round(dd,2)})
    print(f"  {sym:14s}  {len(p):3d} trades  Rs{p.sum():+8.2f}  Sharpe {sh:.2f}")

# ── Table ─────────────────────────────────────────────────────────────────
results.sort(key=lambda x: x["sharpe"], reverse=True)
print(f"\n{'─'*75}")
print(f"{'SYMBOL':>14}  {'TRADES':>6}  {'WIN%':>5}  {'EXPECTANCY':>10}  {'P&L':>10}  {'SHARPE':>7}  {'MAX DD':>8}")
print(f"{'─'*75}")
for r in results:
    print(f"{r['sym']:>14}  {r['n']:>6}  {r['wr']:>4.0f}%  "
          f"Rs{r['exp']:>9.2f}  Rs{r['pnl']:>+9.2f}  {r['sharpe']:>7.2f}  Rs{r['dd']:>7.2f}")
print(f"{'─'*75}")
print(f"\nTop 5 to consider adding to basket:")
for r in results[:5]:
    already = "✓ already in basket" if r["sym"] in [
        "IDEA","SUZLON","YESBANK","NHPC","SAIL","PNB","RPOWER","TATASTEEL","IDFCFIRSTB"
    ] else "← candidate"
    print(f"  {r['sym']:14s}  Sharpe {r['sharpe']:.2f}  Rs{r['pnl']:+.2f}  {already}")
