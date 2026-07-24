"""
cross_asset.py
--------------
OOS test: do cross-asset daily moves predict Nifty-50 next-day direction?
Walk-forward split: train on earliest ~70%, test on latest ~30% of dates.
"""

import warnings
warnings.filterwarnings("ignore")

import os
import sys
import logging
import datetime
import numpy as np
import pandas as pd
import yfinance as yf
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score

# ── logging ──────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "cross_asset.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
log = logging.getLogger(__name__)

# ── data fetch ────────────────────────────────────────────────────────────────
TICKERS = {
    "NSEI":     "^NSEI",
    "USDINR":   "USDINR=X",
    "GOLD":     "GC=F",
    "SP500":    "^GSPC",
    "NASDAQ":   "^IXIC",
    "CRUDE":    "CL=F",
    "VIX":      "^INDIAVIX",
}

def fetch_close(name: str, ticker: str, period: str = "2y") -> pd.Series:
    """Download adjusted/Close, return a named Series or None if empty."""
    try:
        df = yf.download(ticker, period=period, auto_adjust=False, progress=False)
        if df is None or df.empty:
            log.warning(f"  {name} ({ticker}): empty — skipping")
            return None
        # Handle MultiIndex columns (yfinance >=0.2 sometimes returns them)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        col = "Adj Close" if "Adj Close" in df.columns else "Close"
        s = df[col].dropna()
        s.name = name
        s.index = pd.to_datetime(s.index).normalize()
        log.info(f"  {name} ({ticker}): {len(s)} rows, {s.index[0].date()} – {s.index[-1].date()}")
        return s
    except Exception as e:
        log.warning(f"  {name} ({ticker}): fetch error ({e}) — skipping")
        return None


log.info("=" * 70)
log.info(f"cross_asset.py  run at {datetime.datetime.now().isoformat(timespec='seconds')}")
log.info("Fetching data …")

series = {}
for name, ticker in TICKERS.items():
    s = fetch_close(name, ticker)
    if s is not None:
        series[name] = s

if "NSEI" not in series:
    log.error("Nifty (^NSEI) fetch failed — cannot proceed.")
    sys.exit(1)

# ── build aligned price frame ─────────────────────────────────────────────────
# Outer-join on Nifty dates; ffill gaps in FX/commodities (never > a few days)
nsei_idx = series["NSEI"].index
price_df = pd.DataFrame(index=nsei_idx)
for name, s in series.items():
    price_df[name] = s.reindex(nsei_idx).ffill()

price_df.dropna(subset=["NSEI"], inplace=True)

# ── compute returns ───────────────────────────────────────────────────────────
ret = price_df.pct_change()   # simple daily close-to-close return

# ── build feature matrix (all lagged one day — known before day D) ────────────
# We build features on index=D but only use values from D-1 (or earlier).
# ret.shift(1) at day D = D-1 return  → safe, no leakage.

feats = pd.DataFrame(index=ret.index)

# DE-LEAK: US / commodities / FX sessions close AFTER India's 15:30 close (US ~01:30 IST
# next day, FX daily ~end-of-day UTC). Their D-1 bar is NOT known when we'd position for the
# India close[D-1]->close[D] target. Lag them by 2 so only genuinely-prior values are used.
for col in ["USDINR", "GOLD", "SP500", "NASDAQ", "CRUDE"]:
    if col in ret.columns:
        feats[f"{col}_ret1"] = ret[col].shift(2)

if "VIX" in ret.columns:
    feats["VIX_ret1"] = ret["VIX"].shift(1)

# Nifty own lags
feats["NSEI_ret1"]  = ret["NSEI"].shift(1)
feats["NSEI_ret2"]  = ret["NSEI"].shift(2)
feats["NSEI_ret5"]  = ret["NSEI"].rolling(5).sum().shift(1)

# Nifty prior-day range (high-low) / close as a vol proxy
if "NSEI" in price_df.columns:
    try:
        nsei_raw = yf.download("^NSEI", period="2y", auto_adjust=False, progress=False)
        if isinstance(nsei_raw.columns, pd.MultiIndex):
            nsei_raw.columns = nsei_raw.columns.get_level_values(0)
        nsei_raw.index = pd.to_datetime(nsei_raw.index).normalize()
        if not nsei_raw.empty and "High" in nsei_raw.columns:
            day_range = (nsei_raw["High"] - nsei_raw["Low"]) / nsei_raw["Close"]
            day_range = day_range.reindex(feats.index).ffill()
            feats["NSEI_range1"] = day_range.shift(1)
    except Exception:
        pass

# ── target: next-day Nifty direction ─────────────────────────────────────────
# ret["NSEI"] at row D = Nifty close-to-close return for day D
# label = 1 if positive, 0 otherwise
target = (ret["NSEI"] > 0).astype(int)
target.name = "label"

# ── align, drop NaN rows ──────────────────────────────────────────────────────
data = feats.copy()
data["label"] = target
data.dropna(inplace=True)

log.info(f"\nClean dataset: {len(data)} rows, {data.index[0].date()} – {data.index[-1].date()}")
log.info(f"Features: {list(feats.columns)}")

# ── walk-forward split (70/30, time-ordered) ──────────────────────────────────
n = len(data)
split = int(n * 0.70)
train = data.iloc[:split]
test  = data.iloc[split:]

log.info(f"\nSplit → train: {len(train)} rows ({train.index[0].date()} – {train.index[-1].date()})")
log.info(f"         test:  {len(test)} rows ({test.index[0].date()} – {test.index[-1].date()})")

feat_cols = [c for c in data.columns if c != "label"]
X_train, y_train = train[feat_cols].values, train["label"].values
X_test,  y_test  = test[feat_cols].values,  test["label"].values

# Standardize with TRAIN stats only
scaler = StandardScaler().fit(X_train)
X_train_s = scaler.transform(X_train)
X_test_s  = scaler.transform(X_test)

# ── models ────────────────────────────────────────────────────────────────────
models = {
    "LogisticRegression":          LogisticRegression(max_iter=1000, random_state=42),
    "HistGradientBoosting":        HistGradientBoostingClassifier(max_iter=200, random_state=42),
}

results = {}
for mname, model in models.items():
    if mname == "HistGradientBoosting":
        model.fit(X_train, y_train)        # HGBC handles NaN; use raw (already clean)
        preds = model.predict(X_test)
    else:
        model.fit(X_train_s, y_train)
        preds = model.predict(X_test_s)
    acc = accuracy_score(y_test, preds)
    results[mname] = {"acc": acc, "preds": preds}

# ── PnL simulation ────────────────────────────────────────────────────────────
COST_BPS = 10 / 10_000   # 10 bps round-trip per trade (buy+sell)

nsei_test_ret = ret["NSEI"].reindex(test.index).fillna(0).values

def simulate_strategy(preds, returns, cost_bps):
    """
    preds: 1=long, 0=short next day.
    returns: actual Nifty return for that day.
    cost deducted whenever signal changes (round-trip).
    """
    pos = np.where(preds == 1, 1.0, -1.0)
    daily_pnl = pos * returns
    # cost on signal flip
    changes = np.concatenate([[1], np.diff(pos) != 0]).astype(float)
    daily_pnl -= changes * cost_bps
    cum = np.cumprod(1 + daily_pnl) - 1
    return daily_pnl, cum

bh_cum = np.cumprod(1 + nsei_test_ret) - 1
bh_total = bh_cum[-1]

# ── report ────────────────────────────────────────────────────────────────────
log.info("\n" + "=" * 70)
log.info("OUT-OF-SAMPLE RESULTS")
log.info("=" * 70)

# Baseline: always-long (buy and hold)
always_long_preds = np.ones(len(y_test), dtype=int)
baseline_acc = accuracy_score(y_test, always_long_preds)
up_frac = y_test.mean()
log.info(f"\nBaseline (always-long)  accuracy = {baseline_acc:.3f}  (up-days fraction = {up_frac:.3f})")

for mname, res in results.items():
    acc = res["acc"]
    preds = res["preds"]
    daily_pnl, cum = simulate_strategy(preds, nsei_test_ret, COST_BPS)
    strat_total = cum[-1]
    num_trades = int((np.diff(np.where(preds == 1, 1.0, -1.0)) != 0).sum()) + 1
    log.info(f"\n{'─'*60}")
    log.info(f"Model: {mname}")
    log.info(f"  OOS accuracy:           {acc:.3f}  (vs baseline {baseline_acc:.3f})")
    log.info(f"  Strategy cumulative ret:{strat_total*100:+.1f}%")
    log.info(f"  Buy-and-hold cum ret:   {bh_total*100:+.1f}%")
    log.info(f"  Outperformance:         {(strat_total - bh_total)*100:+.1f}%")
    log.info(f"  Trades (signal changes):{num_trades}")
    coin_flip = "YES (barely)" if 0.50 < acc < 0.53 else ("YES" if acc > 0.53 else "NO")
    log.info(f"  Better than coin flip?  {coin_flip}")
    beat_bh   = "YES" if strat_total > bh_total else "NO"
    log.info(f"  Beats buy-and-hold?     {beat_bh}")
    results[mname]["strat_total"] = strat_total
    results[mname]["beat_bh"] = beat_bh

# ── honest verdict ────────────────────────────────────────────────────────────
log.info("\n" + "=" * 70)
log.info("HONEST VERDICT")
log.info("=" * 70)
best_acc = max(r["acc"] for r in results.values())
best_beat = any(r["beat_bh"] == "YES" for r in results.values())

if best_acc < 0.52:
    verdict = (
        "OOS accuracy is near or below 50%: cross-asset features "
        "do NOT reliably predict Nifty next-day direction. "
        "Result is consistent with a near-efficient daily market."
    )
elif best_acc < 0.55:
    verdict = (
        f"OOS accuracy is slightly above 50% ({best_acc:.3f}) — marginal signal, "
        "but after 10 bps cost, strategy " +
        ("beats buy-and-hold on this window." if best_beat else "does NOT beat buy-and-hold after costs.")
        + " Edge, if any, is small and fragile."
    )
else:
    verdict = (
        f"OOS accuracy {best_acc:.3f} is noteworthy. "
        "Validate on longer OOS windows before trusting."
    )

log.info(verdict)
log.info("=" * 70)
log.info(f"Log appended to: {LOG_FILE}")
