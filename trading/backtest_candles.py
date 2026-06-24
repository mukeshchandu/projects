#!/usr/bin/env python3
"""
backtest_candles.py  v3  — bugs fixed
  Fix 1: stop exit uses lower[i]/upper[i] directly, NOT st[i]
  Fix 2: entry at next bar OPEN, not signal bar close
  Fix 3: tiered NSE tick sizes
"""
import warnings, math
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import yfinance as yf
warnings.filterwarnings("ignore")

ATR_RANGE  = [7, 10, 14, 21]
MULT_RANGE = [1.0, 1.2, 1.5, 2.0, 3.0]
CAPITAL    = 10_000

INTERVALS = [
    ("2m",  "2m",  None,    59),
    ("10m", "5m",  "10min", 59),
    ("15m", "15m", None,    59),
]

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

# ── NSE tiered tick size ───────────────────────────────────────────────────
def get_tick(price):
    if   price <=    250: return 0.01
    elif price <=  1_000: return 0.05
    elif price <=  5_000: return 0.10
    elif price <= 10_000: return 0.50
    elif price <= 20_000: return 1.00
    else:                 return 5.00

# N_TICKS: extra ticks added per fill beyond LTP
# 1 tick = rounding to ask/bid, 1 tick = spread/market impact
# 2 ticks total each side = 4 ticks round trip
N_TICKS = 2

def buy_fill(price):
    t = get_tick(price)
    return (math.ceil(round(price / t, 8)) + N_TICKS) * t

def sell_fill(price):
    t = get_tick(price)
    return (math.floor(round(price / t, 8)) - N_TICKS) * t

def tick_cost_bps(price):
    return round(get_tick(price) / price * 10_000, 1)

# ── fetch ──────────────────────────────────────────────────────────────────
def fetch(yf_ivl, fetch_days, resample_to=None):
    end = datetime.now(); start = end - timedelta(days=fetch_days)
    data = {}
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
        df.index = (df.index.tz_localize("Asia/Kolkata")
                    if df.index.tz is None else df.index.tz_convert("Asia/Kolkata"))
        mask = (
            ((df.index.hour == 9)  & (df.index.minute >= 15)) |
            ((df.index.hour > 9)   & (df.index.hour  < 15))   |
            ((df.index.hour == 15) & (df.index.minute <= 30))
        )
        df = df[mask].dropna()
        if resample_to:
            df = df.resample(resample_to, label="left", closed="left").agg(
                {"open":"first","high":"max","low":"min",
                 "close":"last","volume":"sum"}).dropna()
        if len(df) > 20:
            data[sym] = df
    return data

# ── backtest ───────────────────────────────────────────────────────────────
def backtest_stock(df, atr_period, multiplier):
    c = df["close"].values.astype(float)
    o = df["open"].values.astype(float)   # needed for next-bar entry
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    n = len(c)
    if n < atr_period + 5: return None

    # ATR — Wilder smoothing
    pc = np.empty(n); pc[0]=c[0]; pc[1:]=c[:-1]
    tr = np.maximum(h-l, np.maximum(np.abs(h-pc), np.abs(l-pc)))
    atr = np.zeros(n); atr[0]=tr[0]; a = 1.0/atr_period
    for i in range(1,n): atr[i] = atr[i-1]*(1-a) + tr[i]*a

    # Supertrend bands
    hl2 = (h+l)/2; bu = hl2+multiplier*atr; bl = hl2-multiplier*atr
    upper = np.zeros(n); upper[0] = bu[0]
    lower = np.zeros(n); lower[0] = bl[0]
    for i in range(1,n):
        upper[i] = bu[i] if (bu[i]<upper[i-1] or c[i-1]>upper[i-1]) else upper[i-1]
        lower[i] = bl[i] if (bl[i]>lower[i-1] or c[i-1]<lower[i-1]) else lower[i-1]

    # Trend direction (causal — only uses past + current bar)
    trend = np.zeros(n, dtype=int); trend[0] = -1
    for i in range(1,n):
        if trend[i-1] == -1:   # was bearish
            trend[i] = 1  if c[i] > upper[i] else -1
        else:                  # was bullish
            trend[i] = -1 if c[i] < lower[i] else  1

    ts  = df.index
    pos = None
    pending = None   # direction queued for next bar open
    trades  = []
    avg_price = float(np.mean(c))

    for i in range(1, n):
        hour = ts[i].hour

        # ── 1. Execute pending entry at this bar's OPEN ──────────────────
        if pending is not None and pos is None and hour < 15:
            ep  = buy_fill(o[i]) if pending == "L" else sell_fill(o[i])
            qty = max(1, int(CAPITAL / ep))
            pos = {"side": pending, "ep": ep, "qty": qty}
            pending = None

        # ── 2. EOD forced exit ───────────────────────────────────────────
        if hour >= 15 and pos:
            xp  = sell_fill(c[i]) if pos["side"]=="L" else buy_fill(c[i])
            pnl = (xp-pos["ep"])*pos["qty"] if pos["side"]=="L" \
                  else (pos["ep"]-xp)*pos["qty"]
            trades.append(pnl); pos = None; pending = None
            continue

        # ── 3. Stop check — CORRECT bands, not st[] ─────────────────────
        # Bug fix: when trend flips, st[i] becomes the OPPOSITE band.
        # Always check long stop vs lower[i], short stop vs upper[i].
        if pos:
            if pos["side"] == "L" and c[i] < lower[i]:
                xp  = sell_fill(lower[i])          # exit at the stop level
                pnl = (xp - pos["ep"]) * pos["qty"]
                trades.append(pnl); pos = None
            elif pos["side"] == "S" and c[i] > upper[i]:
                xp  = buy_fill(upper[i])           # exit at the stop level
                pnl = (pos["ep"] - xp) * pos["qty"]
                trades.append(pnl); pos = None

        # ── 4. Signal → queue entry for NEXT bar open ───────────────────
        if pos is None and pending is None and i > 1:
            if trend[i] != trend[i-1] and trend[i-1] != 0 and hour < 15:
                pending = "L" if trend[i] == 1 else "S"

    if not trades: return None
    p    = np.array(trades)
    wins = int((p > 0).sum())
    sh   = float(p.mean()/p.std()*np.sqrt(252)) if len(p)>1 and p.std()>0 else 0
    cum  = np.cumsum(p)
    dd   = float(abs((cum - np.maximum.accumulate(cum)).min())) if len(cum)>1 else 0
    return dict(trades=len(trades), wins=wins,
                wr=round(100*wins/len(p),1),
                pnl=round(float(p.sum()),2),
                avg=round(float(p.mean()),2),
                sharpe=round(sh,2), max_dd=round(dd,2),
                days=(ts[-1]-ts[0]).days,
                tick_bps=round(tick_cost_bps(avg_price),1),
                avg_price=round(avg_price,2))

# ── param sweep ────────────────────────────────────────────────────────────
def best_params(stock_data):
    rows = []
    for atr in ATR_RANGE:
        for mult in MULT_RANGE:
            pnls=[]; sharpes=[]
            for sym,df in stock_data.items():
                r = backtest_stock(df, atr, mult)
                if r: pnls.append(r["pnl"]); sharpes.append(r["sharpe"])
            if pnls:
                rows.append(dict(atr=atr, mult=mult,
                                 avg_pnl=round(np.mean(pnls),2),
                                 avg_sharpe=round(np.mean(sharpes),2)))
    df_r = pd.DataFrame(rows).sort_values("avg_sharpe", ascending=False)
    return df_r.iloc[0] if not df_r.empty else None

# ── main ───────────────────────────────────────────────────────────────────
print("\nCapital = Rs 10,000 per stock (independent)")
print("P&L total = sum across all 20 stocks running in parallel")
print("Tick fills: BUY rounds UP, SELL rounds DOWN — NSE tiered tick\n")

all_results = {}; all_best_par = {}

for label, yf_ivl, resample_to, fetch_days in INTERVALS:
    print(f"\n{'='*65}")
    print(f"INTERVAL: {label}  |  {fetch_days} cal days")
    raw = fetch(yf_ivl, fetch_days, resample_to)
    print(f"  Stocks: {len(raw)}")
    print(f"  Param sweep...", end="", flush=True)
    bp = best_params(raw); all_best_par[label] = bp
    print(f"  Best: ATR={int(bp['atr'])} Mult={bp['mult']} "
          f"Sharpe={bp['avg_sharpe']} P&L=Rs{bp['avg_pnl']}")
    res = {}
    for sym, df in raw.items():
        r = backtest_stock(df, int(bp["atr"]), float(bp["mult"]))
        if r: res[sym] = r
    all_results[label] = res

for label, _, _, _ in INTERVALS:
    bp = all_best_par[label]; res = all_results[label]
    print(f"\n{'═'*76}")
    print(f"  {label} | ATR={int(bp['atr'])} Mult={bp['mult']} | "
          f"next-bar-open entry | tick fills")
    print(f"{'─'*76}")
    print(f"  {'STOCK':12s}  {'PRICE':>6}  {'TICK':>7}  {'TR':>4}  "
          f"{'WIN%':>5}  {'P&L':>10}  {'AVG/T':>7}  {'SHARPE':>7}")
    print(f"{'─'*76}")
    for sym, r in sorted(res.items(), key=lambda x: x[1]["pnl"], reverse=True):
        print(f"  {sym:12s}  {r['avg_price']:>6.1f}  {r['tick_bps']:>5.1f}bps"
              f"  {r['trades']:>4}  {r['wr']:>4.0f}%  "
              f"Rs{r['pnl']:>+8.2f}  Rs{r['avg']:>+6.2f}  {r['sharpe']:>7.2f}")
    print(f"{'─'*76}")
    tot = sum(r["pnl"] for r in res.values())
    awr = np.mean([r["wr"]     for r in res.values()])
    ash = np.mean([r["sharpe"] for r in res.values()])
    print(f"  {'TOTAL/AVG':12s}  {'':>6}  {'':>7}  {'':>4}  "
          f"{awr:>4.0f}%  Rs{tot:>+8.2f}  {'':>7}  {ash:>7.2f}")

print(f"\n{'═'*65}")
print(f"  SUMMARY")
print(f"{'─'*65}")
print(f"  {'INTERVAL':>8}  {'ATR':>4}  {'MULT':>5}  "
      f"{'AVG_WR':>7}  {'TOT_PNL':>10}  {'AVG_SH':>8}")
print(f"{'─'*65}")
for label, _, _, _ in INTERVALS:
    bp = all_best_par[label]; res = all_results[label]
    tot = sum(r["pnl"] for r in res.values())
    print(f"  {label:>8}  {int(bp['atr']):>4}  {bp['mult']:>5.1f}  "
          f"{np.mean([r['wr'] for r in res.values()]):>6.1f}%  "
          f"Rs{tot:>+8.2f}  "
          f"{np.mean([r['sharpe'] for r in res.values()]):>8.2f}")
print(f"{'═'*65}")
print("\nNote: total P&L assumes Rs 10k per stock independently.")
print("For Rs 10k shared capital, divide total by number of stocks traded.")
