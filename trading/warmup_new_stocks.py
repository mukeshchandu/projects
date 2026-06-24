#!/usr/bin/env python3
"""
Fetch 59 days 15m data for new stocks, run Supertrend, save state
State format matches exactly what supertrend.py _load_state expects:
  ts=unix_timestamp, o/h/l/c single-letter keys
"""
import json, warnings
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import yfinance as yf
warnings.filterwarnings("ignore")

ATR_PERIOD = 14
MULTIPLIER = 1.5
FETCH_DAYS = 59
STATE_DIR  = Path("data/st_state")
STATE_DIR.mkdir(parents=True, exist_ok=True)

NEW_STOCKS = [
    ("HFCL.NS",       "HFCL"),
    ("VEDL.NS",       "VEDL"),
    ("COALINDIA.NS",  "COALINDIA"),
    ("NATIONALUM.NS", "NATIONALUM"),
    ("BANKBARODA.NS", "BANKBARODA"),
    ("UNIONBANK.NS",  "UNIONBANK"),
    ("NMDC.NS",       "NMDC"),
    ("NTPC.NS",       "NTPC"),
    ("ADANIPORTS.NS", "ADANIPORTS"),
]

def fetch(ticker):
    end = datetime.now(); start = end - timedelta(days=FETCH_DAYS)
    df = yf.download(ticker, start=start, end=end,
                     interval="15m", auto_adjust=True, progress=False)
    if df.empty: return None
    if isinstance(df.columns[0], tuple):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df.index = (df.index.tz_localize("Asia/Kolkata")
                if df.index.tz is None else df.index.tz_convert("Asia/Kolkata"))
    mask = (
        ((df.index.hour == 9)  & (df.index.minute >= 15)) |
        ((df.index.hour > 9)   & (df.index.hour  < 15))   |
        ((df.index.hour == 15) & (df.index.minute <= 30))
    )
    return df[mask].dropna()

def compute_and_save(sym, df):
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

    # Last ATR_PERIOD candles — exact format matching state files
    last = df.tail(ATR_PERIOD)
    candles = [
        {
            "ts": int(idx.timestamp()),
            "o":  round(float(row["open"]),  2),
            "h":  round(float(row["high"]),  2),
            "l":  round(float(row["low"]),   2),
            "c":  round(float(row["close"]), 2),
        }
        for idx, row in last.iterrows()
    ]

    state = {
        "atr":        float(atr[-1]),
        "upper":      float(upper[-1]),
        "lower":      float(lower[-1]),
        "supertrend": float(st[-1]),
        "trend":      int(trend[-1]),
        "candles":    candles,
    }

    path = STATE_DIR / f"{sym}.json"
    with open(path, "w") as f:
        json.dump(state, f)
    return state

print(f"\nWarming up {len(NEW_STOCKS)} new stocks  "
      f"(ATR={ATR_PERIOD}, Mult={MULTIPLIER}, {FETCH_DAYS} days)\n")
print(f"{'SYMBOL':14s}  {'CANDLES':>7}  {'ATR':>8}  {'ST':>10}  TREND")
print("─" * 58)

for ticker, sym in NEW_STOCKS:
    path = STATE_DIR / f"{sym}.json"
    if path.exists():
        print(f"{sym:14s}  already has state — skipped"); continue
    df = fetch(ticker)
    if df is None or len(df) < ATR_PERIOD + 5:
        print(f"{sym:14s}  FAILED — no data from yfinance"); continue
    state = compute_and_save(sym, df)
    trend = "UP" if state["trend"]==1 else "DOWN"
    print(f"{sym:14s}  {len(df):>7}  {state['atr']:>8.4f}  "
          f"{state['supertrend']:>10.4f}  {trend}")

print(f"\nAll state files:")
for p in sorted(STATE_DIR.glob("*.json")):
    print(f"  {p.name}")
