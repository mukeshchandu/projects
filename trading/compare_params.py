#!/usr/bin/env python3
"""
Compare ATR=14 Mult=1.5 (live config) vs ATR=7 Mult=1.5 (sweep winner)
15m candles, 59 days, N_TICKS=2 slippage, next-bar-open entry
"""
import warnings, math
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")

CAPITAL  = 10_000
FETCH_DAYS = 59

STOCKS = [
    ("RPOWER.NS","RPOWER"),        ("NHPC.NS","NHPC"),
    ("HFCL.NS","HFCL"),            ("TATASTEEL.NS","TATASTEEL"),
    ("NTPC.NS","NTPC"),            ("NATIONALUM.NS","NATIONALUM"),
    ("YESBANK.NS","YESBANK"),      ("SUZLON.NS","SUZLON"),
    ("PNB.NS","PNB"),              ("COALINDIA.NS","COALINDIA"),
    ("ADANIPORTS.NS","ADANIPORTS"),("UNIONBANK.NS","UNIONBANK"),
    ("BANKBARODA.NS","BANKBARODA"),("TRIDENT.NS","TRIDENT"),
    ("SAIL.NS","SAIL"),            ("IDEA.NS","IDEA"),
    ("TATAPOWER.NS","TATAPOWER"),  ("HINDALCO.NS","HINDALCO"),
    ("NMDC.NS","NMDC"),            ("VEDL.NS","VEDL"),
]

CONFIGS = [
    ("ATR=7  Mult=1.5  (sweep best)", 7,  1.5),
    ("ATR=14 Mult=1.5  (live config)",14,  1.5),
    ("ATR=14 Mult=1.0  (live ATR, tight band)", 14, 1.0),
    ("ATR=7  Mult=1.0  (sweep ATR, tight band)",  7, 1.0),
]

def get_tick(price):
    if   price <=    250: return 0.01
    elif price <=  1_000: return 0.05
    elif price <=  5_000: return 0.10
    elif price <= 10_000: return 0.50
    elif price <= 20_000: return 1.00
    else:                 return 5.00

N_TICKS = 2

def buy_fill(price):
    t = get_tick(price)
    return (math.ceil(round(price / t, 8)) + N_TICKS) * t

def sell_fill(price):
    t = get_tick(price)
    return (math.floor(round(price / t, 8)) - N_TICKS) * t

def fetch_15m():
    end = datetime.now(); start = end - timedelta(days=FETCH_DAYS)
    data = {}
    print("Fetching 15m data...")
    for ticker, sym in STOCKS:
        try:
            df = yf.download(ticker, start=start, end=end,
                             interval="15m", auto_adjust=True, progress=False)
        except Exception:
            continue
        if df.empty: continue
        if isinstance(df.columns, pd.MultiIndex):
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
        df = df[mask].dropna()
        if len(df) > 20:
            data[sym] = df
    print(f"  {len(data)} stocks loaded\n")
    return data

def backtest(df, atr_period, multiplier):
    c=df["close"].values.astype(float); o=df["open"].values.astype(float)
    h=df["high"].values.astype(float);  l=df["low"].values.astype(float)
    n=len(c)
    if n < atr_period + 5: return None
    pc=np.empty(n); pc[0]=c[0]; pc[1:]=c[:-1]
    tr=np.maximum(h-l,np.maximum(np.abs(h-pc),np.abs(l-pc)))
    atr=np.zeros(n); atr[0]=tr[0]; a=1.0/atr_period
    for i in range(1,n): atr[i]=atr[i-1]*(1-a)+tr[i]*a
    hl2=(h+l)/2; bu=hl2+multiplier*atr; bl=hl2-multiplier*atr
    upper=np.zeros(n); upper[0]=bu[0]
    lower=np.zeros(n); lower[0]=bl[0]
    for i in range(1,n):
        upper[i]=bu[i] if (bu[i]<upper[i-1] or c[i-1]>upper[i-1]) else upper[i-1]
        lower[i]=bl[i] if (bl[i]>lower[i-1] or c[i-1]<lower[i-1]) else lower[i-1]
    trend=np.zeros(n,dtype=int); trend[0]=-1
    for i in range(1,n):
        if trend[i-1]==-1: trend[i]=1  if c[i]>upper[i] else -1
        else:              trend[i]=-1 if c[i]<lower[i] else  1
    ts=df.index; pos=None; pending=None; trades=[]
    for i in range(1,n):
        hour=ts[i].hour
        if pending is not None and pos is None and hour<15:
            ep=buy_fill(o[i]) if pending=="L" else sell_fill(o[i])
            qty=max(1,int(CAPITAL/ep))
            pos={"side":pending,"ep":ep,"qty":qty}; pending=None
        if hour>=15 and pos:
            xp=sell_fill(c[i]) if pos["side"]=="L" else buy_fill(c[i])
            pnl=(xp-pos["ep"])*pos["qty"] if pos["side"]=="L" else (pos["ep"]-xp)*pos["qty"]
            trades.append(pnl); pos=None; pending=None; continue
        if pos:
            if pos["side"]=="L" and c[i]<lower[i]:
                xp=sell_fill(lower[i])
                trades.append((xp-pos["ep"])*pos["qty"]); pos=None
            elif pos["side"]=="S" and c[i]>upper[i]:
                xp=buy_fill(upper[i])
                trades.append((pos["ep"]-xp)*pos["qty"]); pos=None
        if pos is None and pending is None and i>1:
            if trend[i]!=trend[i-1] and trend[i-1]!=0 and hour<15:
                pending="L" if trend[i]==1 else "S"
    if not trades: return None
    p=np.array(trades); wins=int((p>0).sum())
    sh=float(p.mean()/p.std()*np.sqrt(252)) if len(p)>1 and p.std()>0 else 0
    cum=np.cumsum(p)
    dd=float(abs((cum-np.maximum.accumulate(cum)).min())) if len(cum)>1 else 0
    return dict(trades=len(trades),wr=round(100*wins/len(p),1),
                pnl=round(float(p.sum()),2),avg=round(float(p.mean()),2),
                sharpe=round(sh,2),max_dd=round(dd,2))

# ── run ────────────────────────────────────────────────────────────────────
raw = fetch_15m()
all_res = {}
for label, atr, mult in CONFIGS:
    res = {}
    for sym, df in raw.items():
        r = backtest(df, atr, mult)
        if r: res[sym] = r
    all_res[label] = res

# ── per-stock side-by-side ─────────────────────────────────────────────────
syms = sorted(raw.keys())
c1, c2 = CONFIGS[0], CONFIGS[1]  # sweep best vs live

print(f"\n{'═'*82}")
print(f"  SIDE BY SIDE: per stock, 15m, 59 days, N_TICKS=2")
print(f"{'─'*82}")
print(f"  {'STOCK':12s}  │  {'ATR=7  Mult=1.5':^22s}  │  {'ATR=14 Mult=1.5 (LIVE)':^22s}  │  DIFF")
print(f"  {'':12s}  │  {'TR  WIN%  P&L':^22s}  │  {'TR  WIN%  P&L':^22s}  │")
print(f"{'─'*82}")

total_diff = 0
for sym in sorted(raw.keys(), key=lambda s: all_res[c1[0]].get(s,{}).get("pnl",0), reverse=True):
    r1 = all_res[c1[0]].get(sym)
    r2 = all_res[c2[0]].get(sym)
    if not r1 or not r2: continue
    diff = r1["pnl"] - r2["pnl"]
    total_diff += diff
    arrow = "▲" if diff > 0 else "▼"
    print(f"  {sym:12s}  │  {r1['trades']:>3} {r1['wr']:>4.0f}%  Rs{r1['pnl']:>+7.0f}  │"
          f"  {r2['trades']:>3} {r2['wr']:>4.0f}%  Rs{r2['pnl']:>+7.0f}  │"
          f"  {arrow} Rs{abs(diff):>+6.0f}")

print(f"{'─'*82}")
tot1 = sum(r["pnl"] for r in all_res[c1[0]].values())
tot2 = sum(r["pnl"] for r in all_res[c2[0]].values())
wr1  = np.mean([r["wr"]     for r in all_res[c1[0]].values()])
wr2  = np.mean([r["wr"]     for r in all_res[c2[0]].values()])
sh1  = np.mean([r["sharpe"] for r in all_res[c1[0]].values()])
sh2  = np.mean([r["sharpe"] for r in all_res[c2[0]].values()])
print(f"  {'TOTAL':12s}  │  {'':>3} {wr1:>4.0f}%  Rs{tot1:>+7.0f}  │"
      f"  {'':>3} {wr2:>4.0f}%  Rs{tot2:>+7.0f}  │"
      f"  {'▲' if total_diff>0 else '▼'} Rs{abs(total_diff):>+6.0f}")

# ── full summary all 4 configs ─────────────────────────────────────────────
print(f"\n{'═'*72}")
print(f"  ALL 4 CONFIGS — 15m, 59 days, N_TICKS=2")
print(f"{'─'*72}")
print(f"  {'CONFIG':40s}  {'WR':>5}  {'TOT_PNL':>10}  {'SHARPE':>7}  {'TRADES':>7}")
print(f"{'─'*72}")
for label, atr, mult in CONFIGS:
    res = all_res[label]
    tot = sum(r["pnl"]    for r in res.values())
    awr = np.mean([r["wr"]     for r in res.values()])
    ash = np.mean([r["sharpe"] for r in res.values()])
    atr_avg = np.mean([r["trades"] for r in res.values()])
    marker = " ◄ LIVE" if atr==14 and mult==1.5 else ""
    print(f"  {label:40s}  {awr:>4.0f}%  Rs{tot:>+8.0f}  {ash:>7.2f}  {atr_avg:>7.0f}{marker}")
print(f"{'═'*72}")
