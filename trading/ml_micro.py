#!/usr/bin/env python3
"""
ml_micro.py — Can a machine-learning model on order-book MICROSTRUCTURE features
predict short-term price direction on Indian equity tick data OUT-OF-SAMPLE,
and does any edge beat the bid/ask spread?

ANTI-LEAKAGE DESIGN (read this comment carefully — it is the entire point):
  • Features at tick t use ONLY data available at or before t.
    - EWMAs are computed forward with pandas .ewm() on the past.
    - Past returns use backward-looking windows only (shift before diff).
    - realized-vol uses a rolling std of past returns.
  • Train/Test split is purely by CALENDAR DATE in chronological order:
      TRAIN = earliest ~70% of days  (contiguous block)
      TEST  = latest  ~30% of days   (contiguous block, never seen during training)
    There is NO random shuffle, NO sklearn train_test_split, NO k-fold across dates.
    This is mandatory for time series to avoid look-ahead bias.
  • Feature standardization uses TRAIN mean/std only, then applied to TEST.
  • No tick data from a future timestamp ever appears in a feature for tick t.
    The TARGET (forward H-second return) uses future data, but only as the label —
    never as a feature.

Usage:
  python3 ml_micro.py           # full 22 days
  python3 ml_micro.py --smoke   # 3 days only (quick end-to-end check)
"""

import warnings
warnings.filterwarnings("ignore")

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
H = 30                    # forward-return horizon in seconds
LABEL_THR_BPS = 0.2       # |fwd bps| threshold to keep a sample (drop near-zero)
MARKET_OPEN_H = 9
MARKET_OPEN_M = 15
MARKET_CLOSE_H = 15
MARKET_CLOSE_M = 30
IST_OFFSET_S = 5 * 3600 + 30 * 60    # UTC+5:30 in seconds (no pytz needed)
MIN_TICKS_PER_CHUNK = 50             # skip stock-days with too few ticks
ROUND_TRIP_BPS = 3.5      # assumed cost for taker (MIS: STT + exch/SEBI/GST)
CONF_LONG_THR = 0.55      # predicted-prob threshold to go long (taker)
CONF_SHORT_THR = 0.45     # predicted-prob threshold to go short (taker)
TRAIN_FRAC = 0.70         # ~70% of calendar days go to train
SMOKE_MAX_DAYS = 3        # number of days used in --smoke mode

# EWMA span parameters (in ticks, not time — ticks are irregular)
EWMA_FAST_SPAN = 5
EWMA_SLOW_SPAN = 20
VOL_WINDOW = 20           # rolling window for realised vol (past returns)

# ---------------------------------------------------------------------------
# Data loading (adapted from obi_lab.load_fullbook)
# ---------------------------------------------------------------------------

def ist_epoch(ft_int):
    """Return seconds in IST day (hours*3600 + ...) from a unix epoch int."""
    local_s = ft_int + IST_OFFSET_S
    return local_s % 86400  # seconds since midnight IST


def load_fullbook_df(path):
    """
    Load one ticks.jsonl file and return a dict:
        {symbol: pd.DataFrame}
    Each DataFrame has columns:
        ft, mid, obi, bid, ask, lp, bq1, sq1
    Sorted by ft. Only 'full-book' ticks (valid L1 + spread<=1%) are kept.
    Market hours 09:15–15:30 IST are enforced here.
    """
    OPEN_S  = MARKET_OPEN_H  * 3600 + MARKET_OPEN_M  * 60
    CLOSE_S = MARKET_CLOSE_H * 3600 + MARKET_CLOSE_M * 60

    sym_of = {}   # tk -> symbol
    rows = {}     # tk -> list of tuples

    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue

        tk = m.get("tk")
        if not tk or not m.get("ft"):
            continue

        # Build symbol map lazily from 'ts' field (not always present)
        if m.get("ts") and tk not in sym_of:
            sym_of[tk] = m["ts"].replace("-EQ", "")

        # Require full book fields
        try:
            bp = float(m["bp1"])
            sp = float(m["sp1"])
            bq = float(m["bq1"])
            sq = float(m["sq1"])
            lp = float(m["lp"])
        except (KeyError, TypeError, ValueError):
            continue

        if bp <= 0 or sp <= 0 or sp < bp or (bq + sq) <= 0:
            continue

        mid = (bp + sp) / 2.0
        # Drop stale/garbage quotes where spread > 1% of mid
        if (sp - bp) / mid > 0.01:
            continue

        # Market-hours filter (IST)
        ft_int = int(m["ft"])
        day_s = ist_epoch(ft_int)
        if day_s < OPEN_S or day_s > CLOSE_S:
            continue

        obi = (bq - sq) / (bq + sq)
        rows.setdefault(tk, []).append((ft_int, mid, obi, bp, sp, lp, bq, sq))

    out = {}
    for tk, v in rows.items():
        sym = sym_of.get(tk, tk)
        v.sort(key=lambda x: x[0])
        df = pd.DataFrame(v, columns=["ft", "mid", "obi", "bid", "ask", "lp", "bq1", "sq1"])
        out[sym] = df

    return out


# ---------------------------------------------------------------------------
# Feature engineering for one (symbol, day) chunk
# NO future data ever enters features — see inline comments.
# ---------------------------------------------------------------------------

def build_features(df):
    """
    Input: DataFrame with columns ft, mid, obi, bid, ask, lp, bq1, sq1
    Returns: DataFrame of features (same index), target columns fwd_bps, label

    All features at row i use ONLY rows 0..i (past and current).
    Target at row i uses the first tick at ft >= ft[i] + H  (future — label only).
    """
    n = len(df)
    ft  = df["ft"].values.astype(np.float64)
    mid = df["mid"].values
    obi = df["obi"].values
    bid = df["bid"].values
    ask = df["ask"].values
    lp  = df["lp"].values
    bq1 = df["bq1"].values
    sq1 = df["sq1"].values

    # ---- FEATURES ----

    # 1. OBI at tick t (current, causal)
    feat_obi = obi.copy()

    # 2. OBI EWMA (fast/slow) — pandas ewm uses past values only (adjust=False)
    s = pd.Series(obi)
    feat_obi_fast = s.ewm(span=EWMA_FAST_SPAN, adjust=False).mean().values
    feat_obi_slow = s.ewm(span=EWMA_SLOW_SPAN, adjust=False).mean().values

    # 3. Spread in bps at tick t
    spread_bps = (ask - bid) / mid * 1e4
    feat_spread_bps = spread_bps

    # 4. Past mid-returns (look-backward ONLY) over ~5s, ~15s, ~30s
    #    For each tick i, find the latest tick with ft <= ft[i] - window_s,
    #    then compute (mid[i] - mid[j]) / mid[j] * 1e4 bps.
    #    We vectorize with searchsorted on the sorted ft array.
    def past_ret(window_s):
        past_ft = ft - window_s
        # side='left': index of first ft >= past_ft, so ft[j-1] <= past_ft
        j = np.searchsorted(ft, past_ft, side="left")
        j = np.clip(j, 0, n - 1)
        # where j==0, we're looking before the start — set to current (ret=0)
        ret = np.where(j > 0, (mid - mid[j]) / mid[j] * 1e4, 0.0)
        return ret

    feat_ret5   = past_ret(5)
    feat_ret15  = past_ret(15)
    feat_ret30  = past_ret(30)

    # 5. Realized volatility = rolling std of past tick-to-tick mid-returns
    tick_rets = pd.Series(mid).pct_change().fillna(0)
    feat_rvol = tick_rets.rolling(VOL_WINDOW, min_periods=2).std().fillna(0).values * 1e4

    # 6. Trade sign: +1 if last trade was at/above mid (lift), -1 if below (hit)
    feat_trade_sign = np.where(lp >= mid, 1.0, -1.0)

    # 7. Log quantities — log1p for numerical stability
    feat_log_bq  = np.log1p(bq1)
    feat_log_sq  = np.log1p(sq1)
    feat_log_tot = np.log1p(bq1 + sq1)

    # 8. Time-of-day: minutes since 09:15 IST
    #    ft is unix epoch (seconds).  IST = UTC+5:30.
    day_s = (ft + IST_OFFSET_S) % 86400
    open_s = MARKET_OPEN_H * 3600 + MARKET_OPEN_M * 60
    feat_tod_min = (day_s - open_s) / 60.0

    # ---- TARGET ----
    # Forward mid-return over H seconds (FUTURE data — used ONLY as label).
    # For each tick i find the first tick j with ft[j] >= ft[i] + H.
    future_ft = ft + H
    j = np.searchsorted(ft, future_ft, side="left")
    valid = j < n
    fwd_bps = np.full(n, np.nan)
    fwd_bps[valid] = (mid[j[valid]] - mid[valid]) / mid[valid] * 1e4

    # Build feature matrix
    # NOTE: bid, ask, mid are NOT model features — they are carried through
    # for the taker PnL simulation only.
    features = pd.DataFrame({
        "obi":         feat_obi,
        "obi_fast":    feat_obi_fast,
        "obi_slow":    feat_obi_slow,
        "spread_bps":  feat_spread_bps,
        "ret5s":       feat_ret5,
        "ret15s":      feat_ret15,
        "ret30s":      feat_ret30,
        "rvol":        feat_rvol,
        "trade_sign":  feat_trade_sign,
        "log_bq":      feat_log_bq,
        "log_sq":      feat_log_sq,
        "log_tot":     feat_log_tot,
        "tod_min":     feat_tod_min,
        "fwd_bps":     fwd_bps,
        # Pass-through columns (not features, used only for simulation/diagnostics)
        "bid":         bid,
        "ask":         ask,
        "mid":         mid,
    })

    return features


# ---------------------------------------------------------------------------
# Load and assemble one day's feature rows
# ---------------------------------------------------------------------------

def process_day(path):
    """Return DataFrame of features for all stock-days in one file."""
    book = load_fullbook_df(path)
    all_chunks = []
    for sym, df in book.items():
        if len(df) < MIN_TICKS_PER_CHUNK:
            continue
        chunk = build_features(df)
        chunk["symbol"] = sym
        all_chunks.append(chunk)
    if not all_chunks:
        return pd.DataFrame()
    return pd.concat(all_chunks, ignore_index=True)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

FEATURE_COLS = [
    "obi", "obi_fast", "obi_slow", "spread_bps",
    "ret5s", "ret15s", "ret30s",
    "rvol", "trade_sign",
    "log_bq", "log_sq", "log_tot",
    "tod_min",
]


def main():
    parser = argparse.ArgumentParser(description="ML microstructure edge test")
    parser.add_argument("--smoke", action="store_true",
                        help=f"Run on only {SMOKE_MAX_DAYS} days for a quick end-to-end check")
    args = parser.parse_args()

    # -----------------------------------------------------------------
    # 1. Discover day-dirs (sorted chronologically — this ordering is
    #    critical for the walk-forward train/test split)
    # -----------------------------------------------------------------
    base = Path("data")
    day_dirs = sorted(glob.glob(str(base / "*/ticks.jsonl")))
    if not day_dirs:
        sys.exit("ERROR: no data/YYYY-MM-DD/ticks.jsonl files found. "
                 "Run from the trading project root.")

    if args.smoke:
        day_dirs = day_dirs[:SMOKE_MAX_DAYS]
        print(f"[SMOKE MODE] Using first {len(day_dirs)} days only.")

    # -----------------------------------------------------------------
    # 2. WALK-FORWARD SPLIT: earliest ~70% = train, latest ~30% = test.
    #
    #    WHY: shuffling or cross-validating across dates would allow
    #    the model to see future market regimes while training on past
    #    ones.  We enforce strict temporal ordering: the model is always
    #    trained on the past and evaluated on the future.
    # -----------------------------------------------------------------
    n_total = len(day_dirs)
    n_train = max(1, int(math.floor(n_total * TRAIN_FRAC)))
    n_test  = n_total - n_train

    train_paths = day_dirs[:n_train]
    test_paths  = day_dirs[n_train:]

    print(f"Days total: {n_total}  |  Train: {n_train}  |  Test: {n_test}")
    print(f"Train: {os.path.dirname(train_paths[0]).split('/')[-1]} "
          f"→ {os.path.dirname(train_paths[-1]).split('/')[-1]}")
    if test_paths:
        print(f"Test : {os.path.dirname(test_paths[0]).split('/')[-1]} "
              f"→ {os.path.dirname(test_paths[-1]).split('/')[-1]}")
    print()

    # -----------------------------------------------------------------
    # 3. Load all training data
    # -----------------------------------------------------------------
    print("Loading training days...")
    train_frames = []
    for p in train_paths:
        day_label = p.split("/")[-2]
        df_day = process_day(p)
        if not df_day.empty:
            df_day["date"] = day_label
            train_frames.append(df_day)
            print(f"  {day_label}: {len(df_day):>7} rows (before filtering)")

    df_train_raw = pd.concat(train_frames, ignore_index=True) if train_frames else pd.DataFrame()

    # Drop rows with NaN fwd_bps (last ticks of each day) and near-zero moves
    df_train_raw = df_train_raw.dropna(subset=["fwd_bps"])
    df_train_raw = df_train_raw[np.abs(df_train_raw["fwd_bps"]) > LABEL_THR_BPS].copy()
    df_train_raw["label"] = (df_train_raw["fwd_bps"] > 0).astype(int)

    # Also drop any rows where FEATURE_COLS have NaN (shouldn't happen, but safe)
    df_train_raw = df_train_raw.dropna(subset=FEATURE_COLS).reset_index(drop=True)

    print(f"\nTrain samples after filtering: {len(df_train_raw)}")

    # -----------------------------------------------------------------
    # 4. Load all test data
    # -----------------------------------------------------------------
    print("\nLoading test days...")
    test_frames = []
    for p in test_paths:
        day_label = p.split("/")[-2]
        df_day = process_day(p)
        if not df_day.empty:
            df_day["date"] = day_label
            test_frames.append(df_day)
            print(f"  {day_label}: {len(df_day):>7} rows (before filtering)")

    if not test_frames:
        print("WARNING: No test days loaded (possibly in smoke mode with too few days).")
        # In smoke mode with only 2 days, test might be empty; handle gracefully
        _save_and_exit("No test data available (too few days for smoke mode split).")
        return

    df_test_raw = pd.concat(test_frames, ignore_index=True)
    df_test_raw = df_test_raw.dropna(subset=["fwd_bps"])
    df_test_raw = df_test_raw[np.abs(df_test_raw["fwd_bps"]) > LABEL_THR_BPS].copy()
    df_test_raw["label"] = (df_test_raw["fwd_bps"] > 0).astype(int)
    df_test_raw = df_test_raw.dropna(subset=FEATURE_COLS).reset_index(drop=True)

    print(f"\nTest samples after filtering: {len(df_test_raw)}")

    # -----------------------------------------------------------------
    # 5. Standardize with TRAIN statistics ONLY.
    #
    #    WHY: using test mean/std would leak distributional information
    #    about the test period into feature normalization, giving a
    #    spuriously well-calibrated model.
    # -----------------------------------------------------------------
    X_train = df_train_raw[FEATURE_COLS].values.astype(np.float64)
    y_train = df_train_raw["label"].values
    fwd_train = df_train_raw["fwd_bps"].values

    X_test = df_test_raw[FEATURE_COLS].values.astype(np.float64)
    y_test = df_test_raw["label"].values
    fwd_test = df_test_raw["fwd_bps"].values

    # Compute mean and std on TRAIN, apply to both TRAIN and TEST
    train_mean = X_train.mean(axis=0)
    train_std  = X_train.std(axis=0)
    train_std[train_std == 0] = 1.0   # avoid div-by-zero for constant features

    X_train_s = (X_train - train_mean) / train_std
    X_test_s  = (X_test  - train_mean) / train_std

    # -----------------------------------------------------------------
    # 6. Train models
    # -----------------------------------------------------------------
    print("\nTraining models...")

    lr = LogisticRegression(max_iter=500, C=0.1, solver="lbfgs", random_state=42)
    lr.fit(X_train_s, y_train)

    hgb = HistGradientBoostingClassifier(
        max_iter=200,
        max_depth=4,
        learning_rate=0.05,
        min_samples_leaf=50,
        random_state=42,
    )
    hgb.fit(X_train_s, y_train)

    # -----------------------------------------------------------------
    # 7. Evaluate on TEST set (OOS only)
    # -----------------------------------------------------------------
    lr_prob  = lr.predict_proba(X_test_s)[:, 1]
    hgb_prob = hgb.predict_proba(X_test_s)[:, 1]

    # OBI sign predictor (baseline from obi_lab.py — ~52% hit / IC~0.03 raw)
    # Using the obi column directly (column index 0 in FEATURE_COLS)
    obi_col = df_test_raw["obi"].values
    obi_pred_label = (obi_col > 0).astype(int)

    def evaluate(prob, name, y_true, fwd_bps_arr, obi_col_arr,
                 bid_arr=None, ask_arr=None, mid_arr=None, ft_arr=None):
        """Compute all evaluation metrics for a model on test data."""
        pred_label = (prob > 0.5).astype(int)
        hit = accuracy_score(y_true, pred_label) * 100

        # IC = Pearson corr of (prob - 0.5) with actual fwd_bps
        centered = prob - 0.5
        ic, ic_p = stats.pearsonr(centered, fwd_bps_arr)

        # Top-decile / bottom-decile mean fwd_bps
        n = len(prob)
        top_idx = np.argsort(prob)[-n // 10:]
        bot_idx = np.argsort(prob)[:n // 10]
        top_fwd = fwd_bps_arr[top_idx].mean()
        bot_fwd = fwd_bps_arr[bot_idx].mean()

        # Cost-aware taker PnL
        long_mask  = prob > CONF_LONG_THR
        short_mask = prob < CONF_SHORT_THR
        confident  = long_mask | short_mask

        taker_pnl = []
        for i in np.where(confident)[0]:
            if bid_arr is None or ask_arr is None:
                break
            if long_mask[i]:
                # Buy at ask, hope to exit at mid+H; fwd_bps = (mid_future - mid_now)/mid_now*1e4
                # Entry at ask: extra cost = (ask - mid) / mid * 1e4 bps
                half_sp = (ask_arr[i] - bid_arr[i]) / 2.0 / mid_arr[i] * 1e4
                net = fwd_bps_arr[i] - half_sp - ROUND_TRIP_BPS
            else:
                # Sell at bid, forward return is negative (we want fwd < 0)
                half_sp = (ask_arr[i] - bid_arr[i]) / 2.0 / mid_arr[i] * 1e4
                net = -fwd_bps_arr[i] - half_sp - ROUND_TRIP_BPS
            taker_pnl.append(net)

        taker_pnl = np.array(taker_pnl) if taker_pnl else np.array([])
        n_trades  = len(taker_pnl)
        win_pct   = (taker_pnl > 0).mean() * 100 if n_trades else float("nan")
        avg_bps   = taker_pnl.mean() if n_trades else float("nan")
        total_bps = taker_pnl.sum() if n_trades else float("nan")

        return dict(
            name=name,
            hit=hit, ic=ic, ic_p=ic_p,
            top_fwd=top_fwd, bot_fwd=bot_fwd,
            n_trades=n_trades, win_pct=win_pct,
            avg_bps=avg_bps, total_bps=total_bps,
        )

    # We need original bid/ask/mid for taker sim (pre-standardized)
    bid_test = df_test_raw["bid"].values
    ask_test = df_test_raw["ask"].values
    mid_test = df_test_raw["mid"].values

    results = []
    for prob, name in [(lr_prob, "LogReg"), (hgb_prob, "HistGBM")]:
        r = evaluate(prob, name, y_test, fwd_test, obi_col,
                     bid_arr=bid_test, ask_arr=ask_test, mid_arr=mid_test)
        results.append(r)

    # Baseline: OBI sign (treated as prob = 1 if obi>0, 0 if obi<=0 — binary)
    obi_prob = np.where(obi_col > 0, 1.0, 0.0)   # hard signal, for comparison
    r_obi = evaluate(obi_prob, "OBI-sign(baseline)",
                     y_test, fwd_test, obi_col,
                     bid_arr=bid_test, ask_arr=ask_test, mid_arr=mid_test)
    results.append(r_obi)

    # -----------------------------------------------------------------
    # 8. Render summary
    # -----------------------------------------------------------------
    lines = []
    lines.append("=" * 72)
    lines.append("ML MICROSTRUCTURE EDGE TEST — OUT-OF-SAMPLE RESULTS")
    lines.append("=" * 72)
    lines.append(f"Horizon H          : {H}s forward mid-return")
    lines.append(f"Label threshold    : |fwd| > {LABEL_THR_BPS} bps")
    lines.append(f"Round-trip cost    : ~{ROUND_TRIP_BPS} bps (taker MIS)")
    lines.append(f"Confidence thr     : long > {CONF_LONG_THR}, short < {CONF_SHORT_THR}")
    lines.append(f"Train samples      : {len(X_train):,}")
    lines.append(f"Test  samples      : {len(X_test):,}")
    lines.append("")

    hdr = (f"{'Model':<24} {'HitRate%':>8} {'IC':>7} {'IC_p':>8} "
           f"{'TopDecil':>9} {'BotDecil':>9} {'Trades':>7} "
           f"{'Win%':>6} {'AvgBps':>8} {'TotBps':>9}")
    lines.append(hdr)
    lines.append("-" * len(hdr))

    for r in results:
        lines.append(
            f"{r['name']:<24} {r['hit']:>7.2f}% {r['ic']:>7.3f} {r['ic_p']:>8.4f} "
            f"{r['top_fwd']:>+9.2f} {r['bot_fwd']:>+9.2f} {r['n_trades']:>7} "
            f"{r['win_pct']:>5.1f}% {r['avg_bps']:>+8.2f} {r['total_bps']:>+9.0f}"
        )

    lines.append("")
    lines.append("VERDICT:")

    for r in results:
        if r["name"] == "OBI-sign(baseline)":
            continue
        beats_obi_hit = r["hit"] > r_obi["hit"]
        beats_obi_ic  = abs(r["ic"]) > abs(r_obi["ic"])
        pnl_pos       = r["avg_bps"] > 0 if not math.isnan(r["avg_bps"]) else False
        beats_spread  = pnl_pos

        verdict_parts = []
        if beats_obi_hit:
            verdict_parts.append(f"hits {r['hit']:.1f}% (beats OBI baseline {r_obi['hit']:.1f}%)")
        else:
            verdict_parts.append(f"hits {r['hit']:.1f}% (does NOT beat OBI baseline {r_obi['hit']:.1f}%)")

        if beats_obi_ic:
            verdict_parts.append(f"IC={r['ic']:+.3f} (beats OBI IC={r_obi['ic']:+.3f})")
        else:
            verdict_parts.append(f"IC={r['ic']:+.3f} (does NOT beat OBI IC={r_obi['ic']:+.3f})")

        if pnl_pos:
            verdict_parts.append(f"TAKER PnL positive: avg {r['avg_bps']:+.2f} bps/trade "
                                  f"(EDGE SURVIVES SPREAD)")
        else:
            verdict_parts.append(f"TAKER PnL negative: avg {r['avg_bps']:+.2f} bps/trade "
                                  f"(edge DOES NOT survive {ROUND_TRIP_BPS} bps cost)")

        lines.append(f"  {r['name']}: " + "; ".join(verdict_parts) + ".")

    lines.append("")
    lines.append("NOTE: If OOS hit ~50-52% and taker PnL < 0, the ML model adds no tradeable")
    lines.append("edge beyond raw OBI.  Be sceptical — microstructure is a competitive signal.")
    lines.append("=" * 72)

    summary = "\n".join(lines)
    print(summary)

    # -----------------------------------------------------------------
    # 9. Append to logs/ml_micro.log
    # -----------------------------------------------------------------
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "ml_micro.log"

    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode_tag = "[SMOKE]" if args.smoke else "[FULL]"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'#'*72}\n")
        f.write(f"# Run at {ts_str}  {mode_tag}\n")
        f.write(f"{'#'*72}\n")
        f.write(summary + "\n")

    print(f"\nResults appended to {log_path}")


def _save_and_exit(msg):
    print(f"\nWARNING: {msg}")
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    with open(log_dir / "ml_micro.log", "a") as f:
        f.write(f"\nRun aborted: {msg}\n")


if __name__ == "__main__":
    main()
