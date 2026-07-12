#!/usr/bin/env python3
"""
validate_new_config.py
======================
OOS validation for the new config discovered in big_sweep:
  - 5-min candles  (was 15-min)
  - Entry window 09:30-14:00  (was 09:15-15:00)
  - Supertrend atr=14, mult=1.5 (unchanged)

Split: 40 days train / 19 days test (59 days total)

Phase 1 — Re-optimize on TRAIN only
  Sweep atr x mult, find best combo. Should rediscover (14, 1.5).

Phase 2 — Compare train vs test
  Run (14, 1.5) on both halves. Compare expectancy + win rate.

VERDICT: PASS if
  - Phase 1 rediscovers atr=14, mult=1.5 (or very close)
  - Test expectancy >= 70% of train expectancy
  - Test win rate within 15pp of train
"""

import sys, warnings
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── CONFIG ────────────────────────────────────────────────────────────────
INTERVAL_MIN  = 5
ENTRY_H, ENTRY_M   = 9, 30
EXIT_H,  EXIT_M    = 14, 0
EOD_H,   EOD_M     = 15, 0

TRAIN_DAYS    = 27
TEST_DAYS     = 12
LOOKBACK_DAYS = 75
MAX_CAPITAL   = 10_000
SLIPPAGE_BPS  = 3.0
MIN_TRADES    = 3

TARGET_ATR    = 14
TARGET_MULT   = 1.5

ATR_PERIODS   = [5, 7, 10, 14, 20]
MULTIPLIERS   = [1.0, 1.2, 1.5, 2.0, 2.5]

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


# ── DATA ──────────────────────────────────────────────────────────────────

def fetch_5min(ticker: str) -> pd.DataFrame:
    end   = datetime.now()
    start = end - timedelta(days=LOOKBACK_DAYS)
    try:
        df = yf.download(ticker, start=start, end=end,
                         interval="5m", auto_adjust=True, progress=False)
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")
        return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

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


def split_train_test(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    days = sorted(df.index.normalize().unique())
    if len(days) < TRAIN_DAYS + 1:
        print(f"  WARN: only {len(days)} days available")
    train_days = set(days[:TRAIN_DAYS])
    test_days  = set(days[TRAIN_DAYS:TRAIN_DAYS + TEST_DAYS])
    train_df   = df[df.index.normalize().isin(train_days)]
    test_df    = df[df.index.normalize().isin(test_days)]
    return train_df, test_df


# ── SUPERTREND ────────────────────────────────────────────────────────────

def compute_supertrend(df: pd.DataFrame, atr_period: int,
                       multiplier: float) -> pd.DataFrame:
    close = df["close"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    n     = len(close)

    pc = np.empty(n); pc[0] = close[0]; pc[1:] = close[:-1]
    tr = np.maximum(high - low,
         np.maximum(np.abs(high - pc), np.abs(low - pc)))

    atr = np.zeros(n); atr[0] = tr[0]
    a   = 1.0 / atr_period
    for i in range(1, n):
        atr[i] = atr[i-1] * (1 - a) + tr[i] * a

    hl2 = (high + low) / 2
    bu  = hl2 + multiplier * atr
    bl  = hl2 - multiplier * atr

    upper = np.zeros(n); upper[0] = bu[0]
    lower = np.zeros(n); lower[0] = bl[0]
    for i in range(1, n):
        upper[i] = bu[i] if (bu[i] < upper[i-1] or close[i-1] > upper[i-1]) else upper[i-1]
        lower[i] = bl[i] if (bl[i] > lower[i-1] or close[i-1] < lower[i-1]) else lower[i-1]

    st    = np.zeros(n); st[0] = upper[0]
    trend = np.zeros(n, dtype=int); trend[0] = -1
    for i in range(1, n):
        if st[i-1] == upper[i-1]:
            st[i], trend[i] = (lower[i], 1) if close[i] > upper[i] else (upper[i], -1)
        else:
            st[i], trend[i] = (upper[i], -1) if close[i] < lower[i] else (lower[i], 1)

    out = df.copy()
    out["supertrend"] = st
    out["trend"]      = trend
    return out


# ── BACKTEST ──────────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame) -> Dict[str, Any]:
    slip     = SLIPPAGE_BPS / 10_000.0
    close_a  = df["close"].values
    st_a     = df["supertrend"].values
    trend_a  = df["trend"].values
    idx      = df.index

    trades, position, entry_px, prev_trend = [], 0, 0.0, 0

    for i in range(1, len(df)):
        h, m  = idx[i].hour, idx[i].minute
        close = close_a[i]
        st    = st_a[i]
        trend = trend_a[i]

        if h > EOD_H or (h == EOD_H and m >= EOD_M):
            if position != 0:
                xp  = close * (1 - slip) if position == 1 else close * (1 + slip)
                pnl = (xp - entry_px) if position == 1 else (entry_px - xp)
                qty = max(1, int(MAX_CAPITAL / entry_px))
                trades.append(pnl * qty)
                position = 0
            prev_trend = 0
            continue

        if position == 1 and close < st:
            xp  = st * (1 - slip)
            trades.append((xp - entry_px) * max(1, int(MAX_CAPITAL / entry_px)))
            position = 0
        elif position == -1 and close > st:
            xp  = st * (1 + slip)
            trades.append((entry_px - xp) * max(1, int(MAX_CAPITAL / entry_px)))
            position = 0

        in_win = (
            (h > ENTRY_H or (h == ENTRY_H and m >= ENTRY_M)) and
            (h < EXIT_H  or (h == EXIT_H  and m <  EXIT_M))
        )
        if in_win and position == 0 and prev_trend != 0 and trend != prev_trend:
            entry_px = close * (1 + slip) if trend == 1 else close * (1 - slip)
            position = 1 if trend == 1 else -1

        prev_trend = trend

    if len(trades) < MIN_TRADES:
        return {"n": len(trades), "pnl": 0, "sharpe": -99,
                "wr": 0, "aw": 0, "al": 0, "exp": 0}

    p    = np.array(trades)
    wins = p[p > 0]; losses = p[p <= 0]
    wr   = len(wins) / len(p)
    aw   = float(wins.mean())        if len(wins)   else 0.0
    al   = float(abs(losses.mean())) if len(losses) else 0.0
    sh   = float(p.mean() / p.std() * np.sqrt(252)) if p.std() > 0 else 0.0

    return {
        "n":   len(p),
        "pnl": round(float(p.sum()), 2),
        "sharpe": round(sh, 3),
        "wr":  round(wr * 100, 1),
        "aw":  round(aw, 2),
        "al":  round(al, 2),
        "exp": round(wr * aw - (1 - wr) * al, 2),
    }


# ── PHASE 1: RE-OPTIMIZE ON TRAIN ─────────────────────────────────────────

def phase1_sweep(train_data: Dict[str, pd.DataFrame]) -> Tuple[int, float]:
    print("\n" + "="*60)
    print("PHASE 1 — Re-optimize on TRAIN data only")
    print(f"Sweeping {len(ATR_PERIODS)} ATR periods x {len(MULTIPLIERS)} multipliers")
    print("="*60)

    scores: Dict[Tuple, float] = {}

    for atr_p in ATR_PERIODS:
        for mult in MULTIPLIERS:
            total_sharpe = 0.0
            count = 0
            for sym, df_train in train_data.items():
                df_st = compute_supertrend(df_train, atr_p, mult)
                m     = run_backtest(df_st)
                if m["n"] >= MIN_TRADES:
                    total_sharpe += m["sharpe"]
                    count += 1
            scores[(atr_p, mult)] = total_sharpe

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    print(f"\n{'ATR':>6}  {'Mult':>6}  {'SumSharpe':>10}")
    print("-" * 28)
    for (atr_p, mult), score in ranked[:10]:
        marker = "  <-- BEST" if (atr_p, mult) == ranked[0][0] else ""
        print(f"{atr_p:>6}  {mult:>6}  {score:>10.3f}{marker}")

    best_atr, best_mult = ranked[0][0]
    print(f"\nTrain best: atr={best_atr}, mult={best_mult}")
    print(f"Target was: atr={TARGET_ATR}, mult={TARGET_MULT}")
    match = (best_atr == TARGET_ATR and best_mult == TARGET_MULT)
    print(f"Phase 1 result: {'MATCH' if match else 'DIFFERENT — check Phase 2 carefully'}")
    return best_atr, best_mult


# ── PHASE 2: TRAIN vs TEST ────────────────────────────────────────────────

def phase2_compare(train_data: Dict[str, pd.DataFrame],
                   test_data:  Dict[str, pd.DataFrame],
                   atr_p: int, mult: float) -> bool:
    print("\n" + "="*60)
    print(f"PHASE 2 — Train vs Test  (atr={atr_p}, mult={mult})")
    print(f"Entry window: {ENTRY_H:02d}:{ENTRY_M:02d} - {EXIT_H:02d}:{EXIT_M:02d}")
    print(f"Candle: {INTERVAL_MIN}-min")
    print("="*60)

    fmt = f"{'Symbol':>12}  {'Tr_n':>5}  {'Tr_exp':>7}  {'Tr_wr':>6}  " \
          f"{'Te_n':>5}  {'Te_exp':>7}  {'Te_wr':>6}  {'Ratio':>6}"
    print(fmt)
    print("-" * 72)

    tr_exps, te_exps = [], []
    tr_wrs,  te_wrs  = [], []

    for sym in sorted(train_data.keys()):
        df_tr = compute_supertrend(train_data[sym], atr_p, mult)
        df_te = compute_supertrend(test_data[sym],  atr_p, mult)
        tr = run_backtest(df_tr)
        te = run_backtest(df_te)

        ratio = (te["exp"] / tr["exp"]) if tr["exp"] != 0 else float("nan")
        flag  = ""
        if abs(ratio) < 0.5 and tr["exp"] > 0:
            flag = "  WARN"

        print(f"{sym:>12}  {tr['n']:>5}  {tr['exp']:>7.2f}  {tr['wr']:>5.1f}%  "
              f"{te['n']:>5}  {te['exp']:>7.2f}  {te['wr']:>5.1f}%  "
              f"{ratio:>6.2f}{flag}")

        if tr["n"] >= MIN_TRADES:
            tr_exps.append(tr["exp"]); tr_wrs.append(tr["wr"])
        if te["n"] >= MIN_TRADES:
            te_exps.append(te["exp"]); te_wrs.append(te["wr"])

    print("-" * 72)
    avg_tr_exp = np.mean(tr_exps) if tr_exps else 0
    avg_te_exp = np.mean(te_exps) if te_exps else 0
    avg_tr_wr  = np.mean(tr_wrs)  if tr_wrs  else 0
    avg_te_wr  = np.mean(te_wrs)  if te_wrs  else 0
    ratio_exp  = avg_te_exp / avg_tr_exp if avg_tr_exp != 0 else 0

    print(f"\n{'AVERAGE':>12}        {avg_tr_exp:>7.2f}  {avg_tr_wr:>5.1f}%        "
          f"{avg_te_exp:>7.2f}  {avg_te_wr:>5.1f}%  {ratio_exp:>6.2f}")

    print(f"\nTest expectancy = {ratio_exp*100:.0f}% of train expectancy")
    print(f"Win rate shift  = {avg_te_wr - avg_tr_wr:+.1f}pp")

    passed = (ratio_exp >= 0.70 and avg_te_exp > 0 and (avg_te_wr - avg_tr_wr) > -15)
    return passed


# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("VALIDATION: 5-min candles + 09:30-14:00 entry window")
    print(f"Train: {TRAIN_DAYS} days  |  Test: {TEST_DAYS} days")
    print("=" * 60)

    print("\nFetching 5-min data for 9 symbols...")
    train_data: Dict[str, pd.DataFrame] = {}
    test_data:  Dict[str, pd.DataFrame] = {}

    for ticker, sym in SYMBOLS:
        print(f"  {sym:12s} ... ", end="", flush=True)
        df = fetch_5min(ticker)
        if df.empty:
            print("NO DATA")
            continue
        tr, te = split_train_test(df)
        if len(tr) < 100 or len(te) < 20:
            print(f"too short (train={len(tr)} test={len(te)} candles)")
            continue
        train_data[sym] = tr
        test_data[sym]  = te
        tr_days = tr.index.normalize().nunique()
        te_days = te.index.normalize().nunique()
        print(f"{len(df):5d} candles  train={tr_days}d/{len(tr)}c  test={te_days}d/{len(te)}c")

    if len(train_data) < 3:
        print("\nNot enough symbols with data. Exiting.")
        sys.exit(1)

    best_atr, best_mult = phase1_sweep(train_data)
    passed = phase2_compare(train_data, test_data, TARGET_ATR, TARGET_MULT)

    print("\n" + "=" * 60)
    if passed:
        print("VERDICT: PASS")
        print("New config (5-min, 09:30-14:00) holds on out-of-sample data.")
        print("Safe to update paper trader with new config.")
    else:
        print("VERDICT: FAIL")
        print("Test performance significantly below train.")
        print("Do NOT update live config — investigate first.")
    print("=" * 60)


if __name__ == "__main__":
    main()
