#!/usr/bin/env python3
"""
sweep_portfolio.py
Runs portfolio backtest for MAX_POSITIONS in [1,2]
across lookback windows [1,5,10,15,30,45,50] days.
Fetches data ONCE then slices — fast.
"""
import warnings
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────
TOTAL_CAPITAL  = 10_000
MAX_POS_LIST   = [1, 2]
LOOKBACK_LIST  = [1, 5, 10, 15, 30, 45, 50]
FETCH_DAYS     = 52          # fetch once, slice later
ATR_PERIOD     = 14
MULTIPLIER     = 1.5
SLIPPAGE_BPS   = 3.0

STOCKS = [
    ("RPOWER.NS",     "RPOWER"),    ("NHPC.NS",       "NHPC"),
    ("HFCL.NS",       "HFCL"),      ("TATASTEEL.NS",  "TATASTEEL"),
    ("NTPC.NS",       "NTPC"),      ("NATIONALUM.NS", "NATIONALUM"),
    ("YESBANK.NS",    "YESBANK"),   ("SUZLON.NS",     "SUZLON"),
    ("PNB.NS",        "PNB"),       ("COALINDIA.NS",  "COALINDIA"),
    ("ADANIPORTS.NS", "ADANIPORTS"),("UNIONBANK.NS",  "UNIONBANK"),
    ("BANKBARODA.NS", "BANKBARODA"),("TRIDENT.NS",    "TRIDENT"),
    ("SAIL.NS",       "SAIL"),      ("IDEA.NS",       "IDEA"),
    ("TATAPOWER.NS",  "TATAPOWER"), ("HINDALCO.NS",   "HINDALCO"),
    ("NMDC.NS",       "NMDC"),      ("VEDL.NS",       "VEDL"),
    ("BANKINDIA.NS",  "BANKINDIA"), ("MAHABANK.NS",   "MAHABANK"),
    ("IOB.NS",        "IOB"),       ("GOLDBEES.NS",   "GOLD"),
    ("SILVERBEES.NS", "SILVER"),
]

# ── FETCH ─────────────────────────────────────────────────────────────────
def fetch_all():
    end   = datetime.now()
    start = end - timedelta(days=FETCH_DAYS)
    result = {}
    print(f"Fetching {len(STOCKS)} stocks ({FETCH_DAYS} days of 15-min)...")
    for ticker, sym in STOCKS:
        try:
            df = yf.download(ticker, start=start, end=end,
                             interval="15m", auto_adjust=True, progress=False)
        except:
            continue
        if df.empty: continue
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
        df = df[mask].dropna()
        if not df.empty:
            result[sym] = df
        print(f"  {sym:14s}  {len(df):4d} candles")
    return result

# ── SUPERTREND ────────────────────────────────────────────────────────────
def add_supertrend(df):
    c=df["close"].values.astype(float)
    h=df["high"].values.astype(float)
    l=df["low"].values.astype(float)
    n=len(c)
    pc=np.empty(n); pc[0]=c[0]; pc[1:]=c[:-1]
    tr=np.maximum(h-l,np.maximum(np.abs(h-pc),np.abs(l-pc)))
    atr=np.zeros(n); atr[0]=tr[0]; a=1.0/ATR_PERIOD
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
    out=df.copy(); out["st"]=st; out["trend"]=trend
    return out

# ── SIMULATE ──────────────────────────────────────────────────────────────
def simulate(stock_dfs, max_pos):
    slip         = SLIPPAGE_BPS / 10_000
    cap_per_slot = TOTAL_CAPITAL / max_pos

    # Build event stream
    events = []
    for sym, df in stock_dfs.items():
        c_arr=df["close"].values; s_arr=df["st"].values; t_arr=df["trend"].values
        for i in range(len(df)):
            events.append((df.index[i], sym, c_arr[i], s_arr[i], t_arr[i]))
    events.sort(key=lambda x: x[0])

    active     = {}
    prev_trend = {sym: 0 for sym in stock_dfs}
    trades     = []
    skipped    = 0
    total_pnl  = 0.0

    for ts, sym, close, st, trend in events:
        h, m = ts.hour, ts.minute

        if h > 15 or (h == 15 and m >= 0):
            if sym in active:
                pos = active.pop(sym)
                xp  = close*(1-slip) if pos["side"]=="LONG" else close*(1+slip)
                pnl = ((xp-pos["entry"]) if pos["side"]=="LONG"
                       else (pos["entry"]-xp)) * pos["qty"]
                total_pnl += pnl
                trades.append(pnl)
            prev_trend[sym] = 0
            continue

        if sym in active:
            pos = active[sym]
            hit = (pos["side"]=="LONG" and close<st) or (pos["side"]=="SHORT" and close>st)
            if hit:
                xp  = st*(1-slip) if pos["side"]=="LONG" else st*(1+slip)
                pnl = ((xp-pos["entry"]) if pos["side"]=="LONG"
                       else (pos["entry"]-xp)) * pos["qty"]
                total_pnl += pnl
                trades.append(pnl)
                active.pop(sym)

        pt = prev_trend[sym]
        if sym not in active and pt != 0 and trend != pt:
            if len(active) < max_pos:
                ep  = close*(1+slip) if trend==1 else close*(1-slip)
                qty = max(1, int(cap_per_slot / ep))
                active[sym] = {"side":"LONG" if trend==1 else "SHORT",
                               "entry":ep, "qty":qty}
            else:
                skipped += 1

        prev_trend[sym] = trend

    p    = np.array(trades) if trades else np.array([0])
    wins = len(p[p>0])
    wr   = 100*wins/len(p) if len(p) else 0
    sh   = float(p.mean()/p.std()*np.sqrt(252)) if len(p)>1 and p.std()>0 else 0
    dd   = float(abs((np.cumsum(p)-np.maximum.accumulate(np.cumsum(p))).min())) if len(p)>1 else 0
    return {
        "trades":   len(trades),
        "pnl":      round(total_pnl, 2),
        "wr":       round(wr, 1),
        "sharpe":   round(sh, 2),
        "skipped":  skipped,
        "max_dd":   round(dd, 2),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────
all_data = fetch_all()

# Pre-compute Supertrend on full dataset
print("\nComputing Supertrend...")
st_data = {sym: add_supertrend(df) for sym, df in all_data.items()}

# Run all combinations
print("\nRunning combinations...\n")
results = {}
cutoff = datetime.now(tz=list(st_data.values())[0].index.tz)

for days in LOOKBACK_LIST:
    start_cut = cutoff - timedelta(days=days)
    # Slice each stock to last N days
    sliced = {}
    for sym, df in st_data.items():
        sub = df[df.index >= start_cut]
        if len(sub) > 10:
            sliced[sym] = sub
    for max_pos in MAX_POS_LIST:
        r = simulate(sliced, max_pos)
        results[(days, max_pos)] = r

# ── Print table ───────────────────────────────────────────────────────────
print(f"Capital=Rs{TOTAL_CAPITAL:,}  |  Stocks={len(st_data)}")
print(f"\n{'─'*85}")
print(f"{'DAYS':>6}  {'MAX_POS':>7}  {'TRADES':>7}  {'WIN%':>5}  "
      f"{'SKIPPED':>8}  {'P&L':>10}  {'RET%':>6}  {'SHARPE':>7}  {'MAX DD':>8}")
print(f"{'─'*85}")

for days in LOOKBACK_LIST:
    for max_pos in MAX_POS_LIST:
        r = results[(days, max_pos)]
        ret = 100 * r["pnl"] / TOTAL_CAPITAL
        print(f"{days:>6}  {max_pos:>7}  {r['trades']:>7}  {r['wr']:>4.0f}%  "
              f"{r['skipped']:>8}  Rs{r['pnl']:>+8.2f}  {ret:>+5.1f}%  "
              f"{r['sharpe']:>7.2f}  Rs{r['max_dd']:>7.2f}")
    print()

print(f"{'─'*85}")
print("\nBest by P&L:")
best = max(results.items(), key=lambda x: x[1]["pnl"])
print(f"  {best[0][0]} days, MAX_POSITIONS={best[0][1]}  →  "
      f"Rs{best[1]['pnl']:+.2f}  ({100*best[1]['pnl']/TOTAL_CAPITAL:.1f}%)")
print("Best by Sharpe:")
best_sh = max(results.items(), key=lambda x: x[1]["sharpe"])
print(f"  {best_sh[0][0]} days, MAX_POSITIONS={best_sh[0][1]}  →  "
      f"Sharpe {best_sh[1]['sharpe']:.2f}  Rs{best_sh[1]['pnl']:+.2f}")
