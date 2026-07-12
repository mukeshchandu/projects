#!/usr/bin/env python3
"""
big_sweep.py — Full Supertrend parameter exploration

Sweeps:
  4 candle intervals  : 5, 10, 15, 30 min
  5 ATR periods       : 5, 7, 10, 14, 20
  5 multipliers       : 1.0, 1.2, 1.5, 2.0, 2.5
  4 entry windows     : full-day / skip-first-candle / no-late / both
  4 hard stop levels  : none, 1%, 2%, 3%
  9 symbols

Total: 4x5x5x4x4x9 = 14,400 backtests

Outputs:
  sweep_results.csv       — all results
  stdout                  — top 30 + best per symbol + breakdown tables
"""

import itertools, os, sys, time, warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── SWEEP PARAMS ─────────────────────────────────────────────────────────
CANDLE_INTERVALS = [5, 10, 15, 30]          # minutes
ATR_PERIODS      = [5, 7, 10, 14, 20]
MULTIPLIERS      = [1.0, 1.2, 1.5, 2.0, 2.5]

# (entry_h, entry_m, last_entry_h, last_entry_m)
ENTRY_WINDOWS = [
    (9, 15, 15, 0),    # full day
    (9, 30, 15, 0),    # skip opening candle
    (9, 15, 14, 0),    # no entries after 2 PM
    (9, 30, 14, 0),    # skip open + no late entries
]

HARD_SL_PCTS  = [None, 1.0, 2.0, 3.0]      # % from entry, None = no hard SL

SYMBOLS = [
    ("IDEA.NS",        "IDEA"),
    ("SUZLON.NS",      "SUZLON"),
    ("YESBANK.NS",     "YESBANK"),
    ("NHPC.NS",        "NHPC"),
    ("SAIL.NS",        "SAIL"),
    ("PNB.NS",         "PNB"),
    ("RPOWER.NS",      "RPOWER"),
    ("TATASTEEL.NS",   "TATASTEEL"),
    ("IDFCFIRSTB.NS",  "IDFCFIRSTB"),
]

MAX_CAPITAL   = 10_000
SLIPPAGE_BPS  = 3.0
EOD_HOUR      = 15
EOD_MINUTE    = 0
LOOKBACK_DAYS = 59
OUTPUT_CSV    = "sweep_results.csv"
MIN_TRADES    = 5


# ── DATA FETCH ────────────────────────────────────────────────────────────

def fetch_ohlcv(ticker: str, interval_min: int) -> pd.DataFrame:
    end   = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)

    fetch_iv = "5m" if interval_min == 10 else f"{interval_min}m"
    try:
        df = yf.download(ticker, start=start, end=end,
                         interval=fetch_iv, auto_adjust=True, progress=False)
    except Exception as e:
        print(f"  ERROR {ticker} {fetch_iv}: {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]

    # Normalise to IST
    if df.index.tz is None:
        df.index = df.index.tz_localize("Asia/Kolkata")
    else:
        df.index = df.index.tz_convert("Asia/Kolkata")

    # Resample 5m -> 10m if needed
    if interval_min == 10:
        df = df.resample("10min", origin="start").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}
        ).dropna()

    # Keep only market hours 9:15 - 15:30
    mask = (
        ((df.index.hour == 9)  & (df.index.minute >= 15)) |
        ((df.index.hour > 9)   & (df.index.hour < 15))    |
        ((df.index.hour == 15) & (df.index.minute <= 30))
    )
    return df[mask].dropna()


# ── SUPERTREND ────────────────────────────────────────────────────────────

def compute_supertrend(df: pd.DataFrame, atr_period: int,
                       multiplier: float) -> pd.DataFrame:
    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    n     = len(close)

    # True Range
    pc   = np.empty(n); pc[0] = close[0]; pc[1:] = close[:-1]
    tr   = np.maximum(high - low,
           np.maximum(np.abs(high - pc), np.abs(low - pc)))

    # Wilder ATR
    atr  = np.zeros(n)
    atr[0] = tr[0]
    a    = 1.0 / atr_period
    for i in range(1, n):
        atr[i] = atr[i-1] * (1 - a) + tr[i] * a

    hl2   = (high + low) / 2
    bu    = hl2 + multiplier * atr
    bl    = hl2 - multiplier * atr

    upper = np.zeros(n); upper[0] = bu[0]
    lower = np.zeros(n); lower[0] = bl[0]
    for i in range(1, n):
        upper[i] = bu[i] if (bu[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1]
        lower[i] = bl[i] if (bl[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1]

    st    = np.zeros(n); st[0] = upper[0]
    trend = np.zeros(n, dtype=int); trend[0] = -1
    for i in range(1, n):
        if st[i-1] == upper[i-1]:
            if close[i] > upper[i]:
                st[i] = lower[i]; trend[i] = 1
            else:
                st[i] = upper[i]; trend[i] = -1
        else:
            if close[i] < lower[i]:
                st[i] = upper[i]; trend[i] = -1
            else:
                st[i] = lower[i]; trend[i] = 1

    out = df.copy()
    out["supertrend"] = st
    out["trend"]      = trend
    return out


# ── BACKTEST ──────────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame,
                 entry_window: Tuple,
                 hard_sl_pct: Optional[float]) -> Dict[str, Any]:
    entry_h, entry_m, exit_h, exit_m = entry_window
    slip = SLIPPAGE_BPS / 10_000.0

    close_arr = df["close"].values
    st_arr    = df["supertrend"].values
    trend_arr = df["trend"].values
    idx       = df.index

    trades      = []
    position    = 0
    entry_px    = 0.0
    prev_trend  = 0

    for i in range(1, len(df)):
        h, m  = idx[i].hour, idx[i].minute
        close = close_arr[i]
        st    = st_arr[i]
        trend = trend_arr[i]

        # ── EOD force exit ────────────────────────────────────────────────
        if h > EOD_HOUR or (h == EOD_HOUR and m >= EOD_MINUTE):
            if position != 0:
                xp  = close * (1 - slip) if position == 1 else close * (1 + slip)
                pnl = (xp - entry_px) if position == 1 else (entry_px - xp)
                qty = max(1, int(MAX_CAPITAL / entry_px))
                trades.append({"pnl": pnl * qty, "exit": "EOD"})
                position = 0
            prev_trend = 0
            continue

        # ── Hard stop loss ────────────────────────────────────────────────
        if position != 0 and hard_sl_pct is not None:
            if position == 1 and close <= entry_px * (1 - hard_sl_pct / 100):
                xp  = close * (1 - slip)
                pnl = (xp - entry_px) * max(1, int(MAX_CAPITAL / entry_px))
                trades.append({"pnl": pnl, "exit": "SL"})
                position = 0
            elif position == -1 and close >= entry_px * (1 + hard_sl_pct / 100):
                xp  = close * (1 + slip)
                pnl = (entry_px - xp) * max(1, int(MAX_CAPITAL / entry_px))
                trades.append({"pnl": pnl, "exit": "SL"})
                position = 0

        # ── Trailing stop (Supertrend band) ───────────────────────────────
        if position == 1 and close < st:
            xp  = st * (1 - slip)
            pnl = (xp - entry_px) * max(1, int(MAX_CAPITAL / entry_px))
            trades.append({"pnl": pnl, "exit": "TRAIL"})
            position = 0
        elif position == -1 and close > st:
            xp  = st * (1 + slip)
            pnl = (entry_px - xp) * max(1, int(MAX_CAPITAL / entry_px))
            trades.append({"pnl": pnl, "exit": "TRAIL"})
            position = 0

        # ── Entry on trend flip ───────────────────────────────────────────
        in_win = (
            (h > entry_h or (h == entry_h and m >= entry_m)) and
            (h < exit_h  or (h == exit_h  and m <  exit_m))
        )
        if in_win and position == 0 and prev_trend != 0 and trend != prev_trend:
            if trend == 1:
                entry_px = close * (1 + slip)
                position = 1
            else:
                entry_px = close * (1 - slip)
                position = -1

        prev_trend = trend

    # ── Metrics ───────────────────────────────────────────────────────────
    if len(trades) < MIN_TRADES:
        return {"n_trades": len(trades), "total_pnl": 0, "sharpe": -99,
                "win_rate": 0, "avg_win": 0, "avg_loss": 0,
                "expectancy": 0, "max_drawdown": 0}

    pnls   = np.array([t["pnl"] for t in trades])
    wins   = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr     = len(wins) / len(pnls)
    aw     = float(wins.mean())   if len(wins)   else 0
    al     = float(abs(losses.mean())) if len(losses) else 0

    cum    = np.cumsum(pnls)
    dd     = cum - np.maximum.accumulate(cum)
    sharpe = float(pnls.mean() / pnls.std() * np.sqrt(252)) if pnls.std() > 0 else 0

    return {
        "n_trades":    len(trades),
        "total_pnl":   round(float(pnls.sum()), 2),
        "sharpe":      round(sharpe, 3),
        "win_rate":    round(wr * 100, 1),
        "avg_win":     round(aw, 2),
        "avg_loss":    round(al, 2),
        "expectancy":  round(wr * aw - (1 - wr) * al, 2),
        "max_drawdown": round(float(abs(dd.min())), 2),
    }


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    total = (len(CANDLE_INTERVALS) * len(ATR_PERIODS) * len(MULTIPLIERS) *
             len(ENTRY_WINDOWS) * len(HARD_SL_PCTS) * len(SYMBOLS))
    print("=" * 70)
    print("SUPERTREND BIG SWEEP")
    print(f"  Intervals   : {CANDLE_INTERVALS} min")
    print(f"  ATR periods : {ATR_PERIODS}")
    print(f"  Multipliers : {MULTIPLIERS}")
    print(f"  Entry windows: {len(ENTRY_WINDOWS)}  |  Hard SL levels: {len(HARD_SL_PCTS)}")
    print(f"  Symbols     : {len(SYMBOLS)}  |  Total runs: {total:,}")
    print("=" * 70)

    # ── Fetch raw data ────────────────────────────────────────────────────
    print("\n[1/3] Fetching market data...")
    raw: Dict[Tuple, pd.DataFrame] = {}
    for ticker, sym in SYMBOLS:
        for iv in CANDLE_INTERVALS:
            print(f"  {sym:12s} {iv:2d}-min ... ", end="", flush=True)
            df = fetch_ohlcv(ticker, iv)
            if df.empty:
                print("NO DATA")
            else:
                raw[(ticker, iv)] = df
                print(f"{len(df):5d} candles  "
                      f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')}")

    # ── Compute Supertrend for each (symbol, interval, atr, mult) ─────────
    print("\n[2/3] Computing Supertrend bands...")
    st_cache: Dict[Tuple, pd.DataFrame] = {}
    n_st = len(SYMBOLS) * len(CANDLE_INTERVALS) * len(ATR_PERIODS) * len(MULTIPLIERS)
    done = 0
    for ticker, sym in SYMBOLS:
        for iv in CANDLE_INTERVALS:
            df_raw = raw.get((ticker, iv))
            if df_raw is None:
                continue
            for atr_p in ATR_PERIODS:
                for mult in MULTIPLIERS:
                    st_cache[(ticker, iv, atr_p, mult)] = compute_supertrend(df_raw, atr_p, mult)
                    done += 1
                    sys.stdout.write(f"\r  {done}/{n_st}")
                    sys.stdout.flush()
    print()

    # ── Run all backtests ─────────────────────────────────────────────────
    print(f"\n[3/3] Running {total:,} backtests...")
    results = []
    done = 0
    t0 = time.time()

    for ticker, sym in SYMBOLS:
        for iv in CANDLE_INTERVALS:
            for atr_p in ATR_PERIODS:
                for mult in MULTIPLIERS:
                    df_st = st_cache.get((ticker, iv, atr_p, mult))
                    if df_st is None:
                        continue
                    for ew in ENTRY_WINDOWS:
                        ew_label = f"{ew[0]:02d}:{ew[1]:02d}-{ew[2]:02d}:{ew[3]:02d}"
                        for sl in HARD_SL_PCTS:
                            m = run_backtest(df_st, ew, sl)
                            results.append({
                                "symbol":    sym,
                                "interval":  iv,
                                "atr":       atr_p,
                                "mult":      mult,
                                "window":    ew_label,
                                "hard_sl":   f"{sl}%" if sl else "none",
                                **m,
                            })
                            done += 1
                            if done % 1000 == 0:
                                eta = (time.time()-t0)/done*(total-done)
                                sys.stdout.write(f"\r  {done:,}/{total:,}  ETA {eta:.0f}s  ")
                                sys.stdout.flush()

    print(f"\r  {done:,} done in {time.time()-t0:.1f}s                ")

    # ── Results ───────────────────────────────────────────────────────────
    df_r = pd.DataFrame(results)
    df_r = df_r[df_r["n_trades"] >= MIN_TRADES].sort_values("sharpe", ascending=False)
    df_r.to_csv(OUTPUT_CSV, index=False)
    print(f"\nSaved {len(df_r):,} valid results to {OUTPUT_CSV}")

    cols = ["symbol","interval","atr","mult","window","hard_sl",
            "n_trades","total_pnl","sharpe","win_rate","expectancy","max_drawdown"]

    print("\n" + "="*110)
    print("TOP 30 BY SHARPE")
    print("="*110)
    print(df_r[cols].head(30).to_string(index=False))

    print("\n" + "="*90)
    print("BEST CONFIG PER SYMBOL")
    print("="*90)
    best = df_r.groupby("symbol").first().reset_index()
    print(best[cols].to_string(index=False))

    print("\n" + "="*50)
    print("SHARPE BY CANDLE INTERVAL (avg across all combos)")
    print("="*50)
    print(df_r.groupby("interval")["sharpe"].agg(["mean","max","count"]).round(3).to_string())

    print("\n" + "="*50)
    print("SHARPE BY MULTIPLIER")
    print("="*50)
    print(df_r.groupby("mult")["sharpe"].agg(["mean","max","count"]).round(3).to_string())

    print("\n" + "="*50)
    print("SHARPE BY HARD STOP LOSS")
    print("="*50)
    print(df_r.groupby("hard_sl")["sharpe"].agg(["mean","max","count"]).round(3).to_string())

    print("\n" + "="*50)
    print("SHARPE BY ENTRY WINDOW")
    print("="*50)
    print(df_r.groupby("window")["sharpe"].agg(["mean","max","count"]).round(3).to_string())

    print("\nDone.")


if __name__ == "__main__":
    main()
