#!/usr/bin/env python3
"""
backtest_5d.py  —  Mirrors live system exactly.
Supertrend ATR=14 Mult=1.5 | 15-min | Capital Rs1500 | MAX_POSITIONS=1
Pre-simulates prior 25 days to carry open positions into the 5-day window.
"""
import math, sys, warnings
warnings.filterwarnings("ignore")
from datetime import time as dtime
try:
    import pandas as pd
    import yfinance as yf
    import pytz
except ImportError as e:
    sys.exit(f"pip install yfinance pandas pytz   (missing: {e})")
pd.options.mode.chained_assignment = None
IST = pytz.timezone("Asia/Kolkata")

BASKET = ["HFCL","BANKBARODA","NMDC","CESC","ZEEL","BALRAMCHIN","GRANULES","SUZLON"]
MODES  = {"HFCL":"CNC","BANKBARODA":"CNC","NMDC":"CNC",
          "CESC":"CNC","ZEEL":"CNC",
          "BALRAMCHIN":"MIS","GRANULES":"MIS","SUZLON":"MIS"}
BIDX     = {s: i for i, s in enumerate(BASKET)}
CAPITAL  = 1500.0
MAX_POS  = 2
TICK     = 0.05
SIM_DAYS = 5
EOD_T    = dtime(15, 0)

def supertrend(df, p=14, m=1.5):
    hl2 = (df["High"] + df["Low"]) / 2
    tr  = pd.concat([df["High"] - df["Low"],
                     (df["High"] - df["Close"].shift()).abs(),
                     (df["Low"]  - df["Close"].shift()).abs()], axis=1).max(axis=1)
    atr  = tr.ewm(alpha=1/p, min_periods=p, adjust=False).mean()
    bub  = (hl2 + m * atr).values
    blb  = (hl2 - m * atr).values
    close = df["Close"].values
    n    = len(df)

    # Use plain lists — avoids NaN propagation that breaks pandas Series comparisons
    fub = [float("nan")] * n
    flb = [float("nan")] * n
    st  = [float("nan")] * n
    trd = [0] * n

    # Find first row where ATR is valid
    first_i = next((i for i in range(n) if not (bub[i] != bub[i])), None)  # nan check
    if first_i is None:
        df = df.copy(); df["st"] = float("nan"); df["trend"] = 0; return df

    fub[first_i] = bub[first_i]
    flb[first_i] = blb[first_i]
    trd[first_i] = 1
    st[first_i]  = flb[first_i]   # start bullish

    for i in range(first_i + 1, n):
        pc = close[i - 1]
        fub[i] = bub[i] if bub[i] < fub[i-1] or pc > fub[i-1] else fub[i-1]
        flb[i] = blb[i] if blb[i] > flb[i-1] or pc < flb[i-1] else flb[i-1]
        c = close[i]
        if   c > st[i-1]: trd[i] =  1; st[i] = flb[i]
        elif c < st[i-1]: trd[i] = -1; st[i] = fub[i]
        else:
            trd[i] = trd[i-1]
            st[i]  = flb[i] if trd[i] == 1 else fub[i]

    df = df.copy()
    df["st"]    = st
    df["trend"] = trd
    return df

def fp(price, side):
    if side == "BUY": return round((math.ceil(price / TICK) + 1) * TICK, 2)
    return                   round((math.floor(price / TICK) - 1) * TICK, 2)

def sim(candles, open_trades, prev_trend, record=True):
    """open_trades is a list of active position dicts (max MAX_POS)."""
    trades = []; missed = []
    for ts, sym, row in candles:
        close = float(row["Close"]); trend = int(row["trend"])
        last  = prev_trend.get(sym); prev_trend[sym] = trend
        held  = next((t for t in open_trades if t["sym"] == sym), None)
        # MIS EOD exit
        if held and held["mode"] == "MIS" and ts.time() >= EOD_T:
            if record:
                ex = fp(close, "SELL")
                trades.append({**held, "exit": ex, "exit_ts": ts,
                                "pnl": (ex - held["entry"]) * held["qty"],
                                "reason": "EOD"})
            open_trades.remove(held); continue
        if last is None or last == 0: continue
        if   last == -1 and trend ==  1: sig = "BUY"
        elif last ==  1 and trend == -1: sig = "SELL"
        else: continue
        # Exit held position for this symbol on opposing signal
        if held:
            if record:
                ex = fp(close, "SELL")
                trades.append({**held, "exit": ex, "exit_ts": ts,
                                "pnl": (ex - held["entry"]) * held["qty"],
                                "reason": "SIGNAL"})
            open_trades.remove(held); held = None
        # Enter on BUY if slot available
        if sig == "BUY":
            if len(open_trades) < MAX_POS:
                mode = MODES[sym]
                alloc = CAPITAL * 4 / MAX_POS   # mirrors runner: cash*4/MAX_POSITIONS
                en = fp(close, "BUY"); qty = int(alloc / en)
                if qty > 0:
                    open_trades.append({"sym": sym, "side": "LONG", "entry": en,
                                        "qty": qty, "mode": mode, "entry_ts": ts})
            elif record:
                holders = ",".join(t["sym"] for t in open_trades)
                missed.append((ts.strftime("%m/%d %H:%M"), sym, holders))
    return open_trades, prev_trend, trades, missed

# ── Download ──────────────────────────────────────────────────────────────────
print(f"\nDownloading 15-min data (30d, simulating last {SIM_DAYS} trading days) ...\n")
dfs = {}; dfs_pre = {}
for sym in BASKET:
    raw = yf.download(f"{sym}.NS", period="30d", interval="15m",
                      progress=False, auto_adjust=True)
    if raw.empty: print(f"  SKIP {sym}: no data"); continue
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    if raw.index.tz is None: raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(IST)
    raw = raw.between_time("09:15", "15:30")
    if len(raw) < 50: print(f"  SKIP {sym}: only {len(raw)} candles"); continue
    raw = supertrend(raw)
    all_d = sorted(raw.index.normalize().unique())
    sim_d = all_d[-SIM_DAYS:]
    dfs_pre[sym] = raw[raw.index.normalize() < sim_d[0]]
    dfs[sym]     = raw[raw.index.normalize().isin(sim_d)]
    # count actual trend flips in simulation window (sanity check)
    t_vals = dfs[sym]["trend"].values
    flips  = sum(1 for i in range(1, len(t_vals)) if t_vals[i] != t_vals[i-1] and t_vals[i] != 0)
    pre_last = int(dfs_pre[sym]["trend"].iloc[-1]) if len(dfs_pre[sym]) > 0 else 0
    print(f"  {sym:<14} {len(dfs[sym]):3d} candles  "
          f"{sim_d[0].date()} → {sim_d[-1].date()}  "
          f"entering={'UP' if pre_last==1 else 'DOWN' if pre_last==-1 else '?'}  "
          f"flips_in_window={flips}")
if not dfs: sys.exit("No data downloaded.")
print()

# ── Pre-simulate prior period to find open position at window start ────────────
print("Pre-simulating prior days to find carried-over position ...")
pre_candles = sorted(
    [(ts, s, r) for s, df in dfs_pre.items() for ts, r in df.iterrows()],
    key=lambda x: (x[0], BIDX.get(x[1], 99))
)
open_trades, prev_trend, _, _ = sim(pre_candles, [], {}, record=False)

if open_trades:
    for ot in open_trades:
        age = (list(dfs[ot["sym"]].index)[0] - ot["entry_ts"]).days
        print(f"  Carrying over: {ot['sym']} LONG  "
              f"entry=Rs{ot['entry']:.2f}  qty={ot['qty']}  "
              f"mode={ot['mode']}  "
              f"opened={ot['entry_ts'].strftime('%m/%d %H:%M')} (~{age}d ago)")
    print()
else:
    print("  No open positions — starting window flat.\n")

# ── Actual 5-day simulation ───────────────────────────────────────────────────
sim_candles = sorted(
    [(ts, s, r) for s, df in dfs.items() for ts, r in df.iterrows()],
    key=lambda x: (x[0], BIDX.get(x[1], 99))
)
open_trades, _, trades, missed = sim(sim_candles, open_trades, prev_trend, record=True)

# Close any open positions at window end (unrealized P&L)
for t in open_trades:
    lc = float(dfs[t["sym"]]["Close"].iloc[-1]); ex = fp(lc, "SELL")
    trades.append({**t, "exit": ex, "exit_ts": None,
                    "pnl": (ex - t["entry"]) * t["qty"], "reason": "WINDOW_END"})

# ── Report ────────────────────────────────────────────────────────────────────
W = 115
print("=" * W)
print(f"  BACKTEST  |  Rs{CAPITAL:.0f} capital  |  MAX_POSITIONS={MAX_POS}  |  "
      f"Last {SIM_DAYS} trading days  |  Supertrend ATR=14 Mult=1.5 15m")
print("=" * W)

if not trades:
    print("\n  No trades fired — market was in continuous trend with no flips.\n" + "=" * W)
    sys.exit(0)

print(f"\n{'#':>3}  {'Symbol':>12}  {'Mode':>4}  {'Entry':>9}  {'Exit':>9}  "
      f"{'Qty':>5}  {'Invested':>10}  {'P&L':>10}  {'Reason':>11}  Entry → Exit")
print("-" * W)
total_pnl = 0
for i, t in enumerate(trades, 1):
    ets = t["entry_ts"].strftime("%m/%d %H:%M")
    xts = t["exit_ts"].strftime("%m/%d %H:%M") if t["exit_ts"] else "OPEN_END "
    pnl = t["pnl"]; total_pnl += pnl; inv = t["entry"] * t["qty"]
    sgn = "+" if pnl >= 0 else ""
    print(f"{i:>3}  {t['sym']:>12}  {t['mode']:>4}  "
          f"Rs{t['entry']:>7.2f}  Rs{t['exit']:>7.2f}  {t['qty']:>5}  "
          f"Rs{inv:>9.2f}  {sgn}Rs{pnl:>8.2f}  {t['reason']:>11}  {ets} → {xts}")
print("-" * W)
Wc = sum(1 for t in trades if t["pnl"] > 0); Lc = len(trades) - Wc
sgn = "+" if total_pnl >= 0 else ""
print(f"\n  Trades   : {len(trades)}  ({Wc} wins / {Lc} losses)")
print(f"  P&L      : {sgn}Rs{total_pnl:.2f}  ({sgn}{total_pnl/CAPITAL*100:.1f}%  on Rs{CAPITAL:.0f})")
print(f"  Equity   : Rs{CAPITAL + total_pnl:.2f}")

sym_pnl = {}
for t in trades: sym_pnl.setdefault(t["sym"], []).append(t["pnl"])
print(f"\n  Per-stock:")
for sym in BASKET:
    if sym not in sym_pnl: continue
    sp = sum(sym_pnl[sym]); ssgn = "+" if sp >= 0 else ""
    print(f"    {sym:<14}  {len(sym_pnl[sym])} trade(s)  {ssgn}Rs{sp:.2f}  mode={MODES[sym]}")

if missed:
    print(f"\n  Missed BUY signals (slot taken): {len(missed)}")
    for m_ts, m_sym, holder in missed[:10]:
        print(f"    {m_ts}  {m_sym:>12}  blocked by {holder}")
print("\n" + "=" * W)

