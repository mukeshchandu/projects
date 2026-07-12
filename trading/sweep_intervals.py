#!/usr/bin/env python3
"""
Fixed LOOKBACK=30d, TOTAL_CAPITAL=10000
Sweeps candle sizes [1,2,5,10,15,30 min] x MAX_POSITIONS [1,2]
Note: 1m data capped at 7 days by yfinance
"""
import warnings
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")

TOTAL_CAPITAL = 10_000
MAX_POS_LIST  = [1, 2]
LOOKBACK_DAYS = 30
ATR_PERIOD    = 14
MULTIPLIER    = 1.5
SLIPPAGE_BPS  = 3.0

# (label, yf_interval, resample_to, fetch_days)
INTERVALS = [
    ("1m",  "1m",  None,   8),   # yfinance caps 1m at 7 days
    ("2m",  "2m",  None,  35),
    ("5m",  "5m",  None,  35),
    ("10m", "5m",  "10min",35),  # download 5m, resample to 10m
    ("15m", "15m", None,  35),
    ("30m", "30m", None,  35),
]

STOCKS = [
    ("RPOWER.NS","RPOWER"),       ("NHPC.NS","NHPC"),
    ("HFCL.NS","HFCL"),           ("TATASTEEL.NS","TATASTEEL"),
    ("NTPC.NS","NTPC"),           ("NATIONALUM.NS","NATIONALUM"),
    ("YESBANK.NS","YESBANK"),     ("SUZLON.NS","SUZLON"),
    ("PNB.NS","PNB"),             ("COALINDIA.NS","COALINDIA"),
    ("ADANIPORTS.NS","ADANIPORTS"),("UNIONBANK.NS","UNIONBANK"),
    ("BANKBARODA.NS","BANKBARODA"),("TRIDENT.NS","TRIDENT"),
    ("SAIL.NS","SAIL"),           ("IDEA.NS","IDEA"),
    ("TATAPOWER.NS","TATAPOWER"), ("HINDALCO.NS","HINDALCO"),
    ("NMDC.NS","NMDC"),           ("VEDL.NS","VEDL"),
]

def market_hours(df):
    mask = (
        ((df.index.hour == 9)  & (df.index.minute >= 15)) |
        ((df.index.hour > 9)   & (df.index.hour < 15))    |
        ((df.index.hour == 15) & (df.index.minute <= 30))
    )
    return df[mask]

def fetch_stocks(yf_ivl, fetch_days, resample_to=None):
    end   = datetime.now()
    start = end - timedelta(days=fetch_days)
    data  = {}
    for ticker, sym in STOCKS:
        try:
            df = yf.download(ticker, start=start, end=end,
                             interval=yf_ivl, auto_adjust=True, progress=False)
        except Exception:
            continue
        if df.empty: continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        df.index = df.index.tz_localize("Asia/Kolkata") if df.index.tz is None \
                   else df.index.tz_convert("Asia/Kolkata")
        df = market_hours(df).dropna()
        if resample_to:
            df = df.resample(resample_to, label="left", closed="left").agg(
                {"open":"first","high":"max","low":"min",
                 "close":"last","volume":"sum"}).dropna()
        if len(df) > 5:
            data[sym] = df
    return data

def add_supertrend(df):
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    n = len(c)
    pc = np.empty(n); pc[0]=c[0]; pc[1:]=c[:-1]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(n); atr[0]=tr[0]; a=1.0/ATR_PERIOD
    for i in range(1,n): atr[i]=atr[i-1]*(1-a)+tr[i]*a
    hl2=( h+l)/2; bu=hl2+MULTIPLIER*atr; bl=hl2-MULTIPLIER*atr
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

def simulate(stock_dfs, max_pos, lookback_days):
    slip         = SLIPPAGE_BPS / 10_000
    cap_per_slot = TOTAL_CAPITAL / max_pos
    tz           = list(stock_dfs.values())[0].index.tz
    start_cut    = datetime.now(tz=tz) - timedelta(days=lookback_days)
    events = []
    for sym, df in stock_dfs.items():
        sub = df[df.index >= start_cut]
        if len(sub) < 2: continue
        c=sub["close"].values; s=sub["st"].values; t=sub["trend"].values
        for i in range(len(sub)):
            events.append((sub.index[i], sym, c[i], s[i], t[i]))
    if not events:
        return dict(trades=0,pnl=0,wr=0,sharpe=0,skipped=0,max_dd=0,candles=0)
    events.sort(key=lambda x: x[0])
    active={}; prev_trend={sym:0 for sym in stock_dfs}
    trades=[]; skipped=0
    for ts, sym, close, st, trend in events:
        if ts.hour >= 15:
            if sym in active:
                pos=active.pop(sym)
                xp=close*(1-slip) if pos["side"]=="LONG" else close*(1+slip)
                pnl=((xp-pos["entry"]) if pos["side"]=="LONG" else (pos["entry"]-xp))*pos["qty"]
                trades.append(pnl)
            prev_trend[sym]=0; continue
        if sym in active:
            pos=active[sym]
            if (pos["side"]=="LONG" and close<st) or (pos["side"]=="SHORT" and close>st):
                xp=st*(1-slip) if pos["side"]=="LONG" else st*(1+slip)
                pnl=((xp-pos["entry"]) if pos["side"]=="LONG" else (pos["entry"]-xp))*pos["qty"]
                trades.append(pnl); active.pop(sym)
        pt=prev_trend.get(sym,0)
        if sym not in active and pt!=0 and trend!=pt:
            if len(active)<max_pos:
                ep=close*(1+slip) if trend==1 else close*(1-slip)
                qty=max(1,int(cap_per_slot/ep))
                active[sym]={"side":"LONG" if trend==1 else "SHORT","entry":ep,"qty":qty}
            else: skipped+=1
        prev_trend[sym]=trend
    p=np.array(trades) if trades else np.array([0.0])
    wins=int((p>0).sum()); wr=100.0*wins/len(p) if len(p) else 0
    sh=float(p.mean()/p.std()*np.sqrt(252)) if len(p)>1 and p.std()>0 else 0
    cum=np.cumsum(p)
    dd=float(abs((cum-np.maximum.accumulate(cum)).min())) if len(cum)>1 else 0
    return dict(trades=len(trades),pnl=round(float(p.sum()),2),wr=round(wr,1),
                sharpe=round(sh,2),skipped=skipped,max_dd=round(dd,2),candles=len(events))

# ── MAIN ──────────────────────────────────────────────────────────────────
print(f"\nCapital=Rs{TOTAL_CAPITAL:,}  Lookback={LOOKBACK_DAYS}d  "
      f"ATR={ATR_PERIOD}  Mult={MULTIPLIER}  Stocks={len(STOCKS)}\n")

results={}
for label, yf_ivl, resample_to, fetch_days in INTERVALS:
    print(f"[{label:>3}] Fetching...", end="", flush=True)
    raw=fetch_stocks(yf_ivl, fetch_days, resample_to)
    st_data={}
    for sym,df in raw.items():
        try: st_data[sym]=add_supertrend(df)
        except: pass
    actual=7 if label=="1m" else LOOKBACK_DAYS
    print(f" {len(st_data)} stocks")
    for max_pos in MAX_POS_LIST:
        results[(label,max_pos)]=simulate(st_data, max_pos, actual)

print(f"\n{'═'*86}")
print(f"  CANDLE  MAXPOS  CANDLES  TRADES  WIN%  SKIPPED      P&L      RET%    MAX_DD")
print(f"{'─'*86}")
for label,_,_,_ in INTERVALS:
    for max_pos in MAX_POS_LIST:
        r=results[(label,max_pos)]; ret=100.0*r["pnl"]/TOTAL_CAPITAL
        note="*" if label=="1m" else " "
        print(f"  {label:>4}{note}  {max_pos:>4}   {r['candles']:>7}  {r['trades']:>6}  "
              f"{r['wr']:>4.0f}%  {r['skipped']:>7}  Rs{r['pnl']:>+8.2f}  "
              f"{ret:>+5.1f}%  Rs{r['max_dd']:>7.2f}")
    print()
print(f"{'═'*86}")
print("  * 1m: yfinance caps at 7 days regardless of LOOKBACK")

bp=max(results.items(),key=lambda x:x[1]["pnl"])
bs=max(results.items(),key=lambda x:x[1]["sharpe"])
print(f"\nBest P&L   : {bp[0][0]} candles, MAX={bp[0][1]}  "
      f"→ Rs{bp[1]['pnl']:+.2f} ({100*bp[1]['pnl']/TOTAL_CAPITAL:.1f}%)")
print(f"Best Sharpe: {bs[0][0]} candles, MAX={bs[0][1]}  "
      f"→ Sharpe {bs[1]['sharpe']:.2f}  Rs{bs[1]['pnl']:+.2f}")
