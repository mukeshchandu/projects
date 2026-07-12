#!/usr/bin/env python3
"""
CNC backtest — Supertrend(14,1.5) 15-min, overnight holds allowed.
Charges: MIS rates if same-day exit, CNC rates if overnight.
No leverage (1x capital only).
"""
import math, time
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

ATR_PERIOD = 14
MULT       = 1.5
CAPITAL    = 10_000   # 1x — no leverage for CNC
N_TICKS    = 1

UNIVERSE = [
    "IDEA","SUZLON","YESBANK","NHPC","SAIL","PNB","RPOWER","TATASTEEL",
    "IDFCFIRSTB","HFCL","VEDL","COALINDIA","NATIONALUM","BANKBARODA",
    "UNIONBANK","NMDC","NTPC","ADANIPORTS",
    "ASHOKLEY","COFORGE","ADANIGREEN","BHEL","MPHASIS","INDUSINDBK",
]

def get_tick(p):
    if p<=250: return 0.01
    if p<=1000: return 0.05
    if p<=5000: return 0.10
    if p<=10000: return 0.50
    if p<=20000: return 1.00
    return 5.00

def buy_fill(p):
    t=get_tick(p); return round((math.ceil(round(p/t,8))+N_TICKS)*t,4)
def sell_fill(p):
    t=get_tick(p); return round((math.floor(round(p/t,8))-N_TICKS)*t,4)

def calc_charges(buy_p, sell_p, qty, overnight):
    """Flattrade charges: delivery=free brokerage, MIS=Rs20/order."""
    brokerage  = 0  # Flattrade: zero brokerage for both MIS and CNC
    stt        = sell_p * qty * (0.001 if overnight else 0.00025)
    if overnight: stt += buy_p * qty * 0.001   # STT on buy side for delivery
    txn        = (buy_p + sell_p) * qty * 0.0000345
    sebi       = (buy_p + sell_p) * qty * 0.000001
    stamp      = buy_p * qty * (0.00015 if overnight else 0.00003)
    gst        = (brokerage + txn + sebi) * 0.18
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)

def supertrend(h, l, c):
    n = len(c)
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]) if i else h[i]-l[i],
              abs(l[i]-c[i-1]) if i else h[i]-l[i]) for i in range(n)]
    atr = [0.0]*n
    if n >= ATR_PERIOD:
        atr[ATR_PERIOD-1] = sum(tr[:ATR_PERIOD]) / ATR_PERIOD
        for i in range(ATR_PERIOD, n):
            atr[i] = (atr[i-1]*(ATR_PERIOD-1) + tr[i]) / ATR_PERIOD
    ub = [((h[i]+l[i])/2) + MULT*atr[i] for i in range(n)]
    lb = [((h[i]+l[i])/2) - MULT*atr[i] for i in range(n)]
    st = [0.0]*n; tr2 = [0]*n
    for i in range(1, n):
        if not atr[i]: continue
        ub[i] = min(ub[i], ub[i-1]) if c[i-1] <= ub[i-1] else ub[i]
        lb[i] = max(lb[i], lb[i-1]) if c[i-1] >= lb[i-1] else lb[i]
        st[i] = (ub[i] if c[i] <= ub[i] else lb[i]) if st[i-1]==ub[i-1] else (lb[i] if c[i]>=lb[i] else ub[i])
        tr2[i] = 1 if c[i] > st[i] else -1
    return ub, lb, st, tr2

def run(sym):
    try:
        df = yf.download(sym+".NS", start=datetime.now()-timedelta(days=58),
                         end=datetime.now(), interval="15m",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 20: return None
        if hasattr(df.columns, "levels"): df.columns = df.columns.droplevel(1)
        df = df.dropna()

        o, h, l, c = df["Open"].values, df["High"].values, df["Low"].values, df["Close"].values
        ub, lb, st, tr = supertrend(h, l, c)

        pos = None; pending = None; trades = []

        for i in range(1, len(c)):
            dt  = df.index[i]
            utc_min = (dt.hour if hasattr(dt,"hour") else 10)*60 + (dt.minute if hasattr(dt,"minute") else 0)
            ist_min = (utc_min + 330) % 1440
            hr  = ist_min // 60
            mn  = ist_min % 60

            # Skip pre-market — but NO forced EOD exit (CNC holds overnight)
            if hr < 9 or (hr == 9 and mn < 15): continue
            # After 3:30 PM — skip (market closed, just carry position)
            if hr > 15 or (hr == 15 and mn >= 30): continue

            if pending and not pos:
                ep = buy_fill(o[i]) if pending=="L" else sell_fill(o[i])
                qty = max(1, int(CAPITAL / ep))
                pos = {"s": pending, "e": ep, "q": qty,
                       "date": dt.date() if hasattr(dt,"date") else None}
                pending = None
                continue

            if pos:
                # Stop check
                if pos["s"]=="L" and c[i] < lb[i]:
                    xp = sell_fill(lb[i])
                    overnight = pos["date"] and dt.date() != pos["date"] if hasattr(dt,"date") else False
                    charges = calc_charges(pos["e"], xp, pos["q"], overnight)
                    pnl = (xp - pos["e"]) * pos["q"] - charges
                    trades.append({"pnl": pnl, "overnight": overnight, "reason": "stop"})
                    pos = None
                elif pos["s"]=="S" and c[i] > ub[i]:
                    xp = buy_fill(ub[i])
                    overnight = pos["date"] and dt.date() != pos["date"] if hasattr(dt,"date") else False
                    charges = calc_charges(xp, pos["e"], pos["q"], overnight)
                    pnl = (pos["e"] - xp) * pos["q"] - charges
                    trades.append({"pnl": pnl, "overnight": overnight, "reason": "stop"})
                    pos = None

                # Trend flip
                if pos and tr[i] != tr[i-1] and tr[i]:
                    if pos["s"]=="L" and tr[i]==-1:
                        xp = sell_fill(c[i])
                        overnight = pos["date"] and dt.date() != pos["date"] if hasattr(dt,"date") else False
                        charges = calc_charges(pos["e"], xp, pos["q"], overnight)
                        pnl = (xp - pos["e"]) * pos["q"] - charges
                        trades.append({"pnl": pnl, "overnight": overnight, "reason": "flip"})
                        pos = None; pending = "S"
                    elif pos["s"]=="S" and tr[i]==1:
                        xp = buy_fill(c[i])
                        overnight = pos["date"] and dt.date() != pos["date"] if hasattr(dt,"date") else False
                        charges = calc_charges(xp, pos["e"], pos["q"], overnight)
                        pnl = (pos["e"] - xp) * pos["q"] - charges
                        trades.append({"pnl": pnl, "overnight": overnight, "reason": "flip"})
                        pos = None; pending = "L"
            else:
                if tr[i]==1 and tr[i-1]!=1: pending = "L"
                elif tr[i]==-1 and tr[i-1]!=-1: pending = "S"

        if not trades: return None
        pnls  = [t["pnl"] for t in trades]
        total = sum(pnls)
        wins  = sum(1 for p in pnls if p > 0)
        overnight_count = sum(1 for t in trades if t["overnight"])
        cum=pk=dd=0
        for p in pnls:
            cum+=p; pk=max(pk,cum); dd=max(dd,pk-cum)
        return {"symbol": sym, "trades": len(trades), "wins": wins,
                "win_rate": round(wins/len(trades), 3),
                "total_pnl": round(total, 2),
                "pnl_per_trade": round(total/len(trades), 2),
                "overnight_trades": overnight_count,
                "max_drawdown": round(-dd, 2)}
    except Exception as e:
        return None

if __name__ == "__main__":
    results = []
    for i, sym in enumerate(UNIVERSE):
        r = run(sym)
        tag = f"Rs{r['total_pnl']:+.0f} {r['trades']}t ({r['overnight_trades']} overnight)" if r else "SKIP"
        print(f"[{i+1:2d}/{len(UNIVERSE)}] {sym:<16}{tag}")
        if r: results.append(r)
        time.sleep(0.25)

    if results:
        df = pd.DataFrame(results).sort_values("total_pnl", ascending=False)
        df.to_csv("backtest_cnc_results.csv", index=False)
        print(f"\n{'='*60}")
        print(df[["symbol","trades","win_rate","total_pnl","pnl_per_trade","overnight_trades"]].to_string(index=False))
        print(f"\nSaved: backtest_cnc_results.csv")
