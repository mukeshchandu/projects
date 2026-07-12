#!/usr/bin/env python3
"""
portfolio_backtest.py
=====================
Portfolio-level Supertrend backtest with shared capital.

TOTAL_CAPITAL = Rs10,000 split equally across MAX_POSITIONS active slots.
All stocks watched simultaneously. First trend flip takes a slot.
When all slots full, new signals are skipped until a slot frees up.

Change MAX_POSITIONS to 1, 2, 3, 4 to see the tradeoff.
"""

import warnings, sys
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 10_000
MAX_POSITIONS = 4

ATR_PERIOD   = 14
MULTIPLIER   = 1.5
LOOKBACK     = 30
SLIPPAGE_BPS = 3.0

# Best performers from previous run
STOCKS = [
    ("RPOWER.NS",      "RPOWER"),
    ("NHPC.NS",        "NHPC"),
    ("HFCL.NS",        "HFCL"),
    ("TATASTEEL.NS",   "TATASTEEL"),
    ("NTPC.NS",        "NTPC"),
    ("NATIONALUM.NS",  "NATIONALUM"),
    ("YESBANK.NS",     "YESBANK"),
    ("SUZLON.NS",      "SUZLON"),
    ("PNB.NS",         "PNB"),
    ("COALINDIA.NS",   "COALINDIA"),
    ("ADANIPORTS.NS",  "ADANIPORTS"),
    ("UNIONBANK.NS",   "UNIONBANK"),
    ("BANKBARODA.NS",  "BANKBARODA"),
    ("TRIDENT.NS",     "TRIDENT"),
    ("SAIL.NS",        "SAIL"),
    ("IDEA.NS",        "IDEA"),
    ("TATAPOWER.NS",   "TATAPOWER"),
    ("ADANIPOWER.NS",  "ADANIPOWER"),
    ("JSWENERGY.NS",   "JSWENERGY"),
    ("HINDALCO.NS",    "HINDALCO"),
    ("NMDC.NS",        "NMDC"),
    ("VEDL.NS",        "VEDL"),
    ("BANKINDIA.NS",   "BANKINDIA"),
    ("MAHABANK.NS",    "MAHABANK"),
    ("IOB.NS",         "IOB"),
    ("GOLDBEES.NS",    "GOLD"),
    ("SILVERBEES.NS",  "SILVER"),
]

# ── FETCH ─────────────────────────────────────────────────────────────────
def fetch(ticker):
    end   = datetime.now()
    start = end - timedelta(days=LOOKBACK)
    try:
        df = yf.download(ticker, start=start, end=end,
                         interval="15m", auto_adjust=True, progress=False)
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

# ── SUPERTREND ────────────────────────────────────────────────────────────
def add_supertrend(df):
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    n = len(c)
    pc = np.empty(n); pc[0]=c[0]; pc[1:]=c[:-1]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(n); atr[0]=tr[0]; a=1.0/ATR_PERIOD
    for i in range(1,n): atr[i]=atr[i-1]*(1-a)+tr[i]*a
    hl2=(h+l)/2; bu=hl2+MULTIPLIER*atr; bl=hl2-MULTIPLIER*atr
    upper=np.zeros(n); upper[0]=bu[0]
    lower=np.zeros(n); lower[0]=bl[0]
    for i in range(1,n):
        upper[i]=bu[i] if (bu[i]<upper[i-1] or c[i-1]>upper[i-1]) else upper[i-1]
        lower[i]=bl[i] if (bl[i]>lower[i-1] or c[i-1]<lower[i-1]) else lower[i-1]
    st=np.zeros(n); st[0]=upper[0]
    trend=np.zeros(n,dtype=int); trend[0]=-1
    for i in range(1,n):
        if st[i-1]==upper[i-1]:
            st[i],trend[i]=(lower[i],1) if c[i]>upper[i] else (upper[i],-1)
        else:
            st[i],trend[i]=(upper[i],-1) if c[i]<lower[i] else (lower[i],1)
    df=df.copy(); df["st"]=st; df["trend"]=trend
    return df

# ── MAIN ──────────────────────────────────────────────────────────────────
print(f"Portfolio Backtest | Capital=Rs{TOTAL_CAPITAL:,} | MAX_POSITIONS={MAX_POSITIONS}")
print(f"Capital per slot = Rs{TOTAL_CAPITAL//MAX_POSITIONS:,} | Stocks={len(STOCKS)} | {LOOKBACK} days\n")
print("Fetching data...")

stock_dfs = {}
for ticker, sym in STOCKS:
    df = fetch(ticker)
    if df.empty: print(f"  {sym:14s}  NO DATA"); continue
    df = add_supertrend(df)
    stock_dfs[sym] = df
    print(f"  {sym:14s}  {len(df):4d} candles")

# Build chronological event stream: (timestamp, sym, close, st, trend)
print(f"\nBuilding event stream from {len(stock_dfs)} stocks...")
events = []
for sym, df in stock_dfs.items():
    c_arr = df["close"].values
    s_arr = df["st"].values
    t_arr = df["trend"].values
    for i in range(len(df)):
        events.append((df.index[i], sym, c_arr[i], s_arr[i], t_arr[i]))
events.sort(key=lambda x: x[0])
print(f"Total events: {len(events):,}\n")

# ── Simulate ──────────────────────────────────────────────────────────────
slip         = SLIPPAGE_BPS / 10_000
cap_per_slot = TOTAL_CAPITAL / MAX_POSITIONS

active    = {}   # sym → {"side","entry","qty","ts"}
prev_trend = {sym: 0 for sym in stock_dfs}
trades    = []
skipped   = 0
total_pnl = 0.0

for ts, sym, close, st, trend in events:
    h, m = ts.hour, ts.minute

    # ── EOD force-exit ────────────────────────────────────────────────────
    if h > 15 or (h == 15 and m >= 0):
        if sym in active:
            pos = active.pop(sym)
            xp  = close*(1-slip) if pos["side"]=="LONG" else close*(1+slip)
            pnl = ((xp-pos["entry"]) if pos["side"]=="LONG"
                   else (pos["entry"]-xp)) * pos["qty"]
            total_pnl += pnl
            trades.append({"sym":sym,"pnl":pnl,"exit":"EOD",
                           "entry_ts":pos["ts"],"exit_ts":ts})
        prev_trend[sym] = 0
        continue

    # ── Trail SL exit ─────────────────────────────────────────────────────
    if sym in active:
        pos = active[sym]
        hit = (pos["side"]=="LONG" and close < st) or \
              (pos["side"]=="SHORT" and close > st)
        if hit:
            xp  = st*(1-slip) if pos["side"]=="LONG" else st*(1+slip)
            pnl = ((xp-pos["entry"]) if pos["side"]=="LONG"
                   else (pos["entry"]-xp)) * pos["qty"]
            total_pnl += pnl
            active.pop(sym)
            trades.append({"sym":sym,"pnl":pnl,"exit":"TRAIL",
                           "entry_ts":pos["ts"],"exit_ts":ts})

    # ── Entry signal ──────────────────────────────────────────────────────
    pt = prev_trend[sym]
    if sym not in active and pt != 0 and trend != pt:
        if len(active) < MAX_POSITIONS:
            ep  = close*(1+slip) if trend==1 else close*(1-slip)
            qty = max(1, int(cap_per_slot / ep))
            active[sym] = {"side":"LONG" if trend==1 else "SHORT",
                           "entry":ep, "qty":qty, "ts":ts}
            side = "LONG" if trend==1 else "SHORT"
            print(f"  ENTER [{ts.strftime('%m-%d %H:%M')}]  {sym:12s}  {side:5s}  "
                  f"Rs{ep:.2f}  qty={qty}  slots={len(active)}/{MAX_POSITIONS}")
        else:
            skipped += 1

    prev_trend[sym] = trend

# ── Per-symbol breakdown ──────────────────────────────────────────────────
print(f"\n{'─'*65}")
print(f"{'SYMBOL':>14}  {'TRADES':>6}  {'WINS':>5}  {'WIN%':>5}  {'P&L':>10}")
print(f"{'─'*65}")
syms_traded = sorted(set(t["sym"] for t in trades))
for sym in syms_traded:
    st = [t for t in trades if t["sym"]==sym]
    w  = [t["pnl"] for t in st if t["pnl"]>0]
    pnl= sum(t["pnl"] for t in st)
    wr = 100*len(w)/len(st)
    print(f"{sym:>14}  {len(st):>6}  {len(w):>5}  {wr:>4.0f}%  Rs{pnl:>+9.2f}")

n_wins = len([t for t in trades if t["pnl"]>0])
print(f"{'─'*65}")
print(f"{'TOTAL':>14}  {len(trades):>6}  {n_wins:>5}  "
      f"{100*n_wins/max(1,len(trades)):>4.0f}%  Rs{total_pnl:>+9.2f}")
print(f"{'─'*65}")
print(f"\nSignals skipped (slots full) : {skipped}")
print(f"Max simultaneous positions   : {MAX_POSITIONS}")
print(f"Capital per slot             : Rs{cap_per_slot:,.0f}")
print(f"Final P&L on Rs{TOTAL_CAPITAL:,} capital   : Rs{total_pnl:+.2f}  "
      f"({100*total_pnl/TOTAL_CAPITAL:.1f}%)")
