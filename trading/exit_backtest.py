#!/usr/bin/env python3
"""
exit_backtest.py — Compare exit rules across the last N recorded tick-days, on the SAME
entries, to answer: does a profit-locking trailing exit beat the current one?

For each tick-day we:
  · warm ATR/EMA/trend ONLY from 15-min history dated strictly BEFORE that day (no
    look-ahead), fetched once per symbol from Yahoo (deterministic, covers ~1 month),
  · replay that day's real ticks through the fixed strategy under each exit mode,
  · the entries are identical across modes for a given day — only the EXIT differs — so the
    P&L delta is purely the exit rule.

Usage:  python3 exit_backtest.py [N_DAYS]      (default 7)
"""
from __future__ import annotations
import glob, os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sim_chart as S
from config import IST

N_DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
HERE   = os.path.dirname(os.path.abspath(__file__))
MODES  = ["current", "trail", "trailtight", "trailwide", "tp"]

# ── 15-min OHLC history per symbol (fetched once), for look-ahead-free warm-up ──
_hist_cache: dict = {}

def _hist(symbol: str) -> list:
    """[(ist_date_str, ft, o, h, l, c)] of 15-min bars, once per symbol."""
    if symbol in _hist_cache:
        return _hist_cache[symbol]
    bars = []
    try:
        import yfinance as yf
        df = yf.download(symbol + ".NS", period="1mo", interval="15m",
                         progress=False, auto_adjust=False)
        if df is not None and len(df):
            if getattr(df.columns, "nlevels", 1) > 1:
                df.columns = df.columns.get_level_values(0)
            idx = df.index
            try:
                idx = idx.tz_convert("Asia/Kolkata")
            except Exception:
                idx = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
            for i, ts in enumerate(idx):
                o, h, l, c = (float(df[k].iloc[i]) for k in ("Open", "High", "Low", "Close"))
                if c == c:   # not NaN
                    bars.append((ts.strftime("%Y-%m-%d"), int(ts.timestamp()), o, h, l, c))
    except Exception:
        pass
    _hist_cache[symbol] = bars
    return bars


WARM_BARS = 80   # last N 15-min bars before the day — ample for ATR-14 + EMA-50 convergence

def _warm_before(symbol: str) -> list:
    """Warm-up ticks from the last WARM_BARS 15-min bars dated strictly before the current
    S.DATE. Each bar is emitted as O,H,L,C sub-ticks so CandleBuilder(900) rebuilds the bar."""
    prior = [(ft, o, h, l, c) for d, ft, o, h, l, c in _hist(symbol) if d < S.DATE]
    out = []
    for ft, o, h, l, c in prior[-WARM_BARS:]:
        base = (ft // 900) * 900      # align to the 15-min bucket start
        out.append((base + 1, o)); out.append((base + 2, h))
        out.append((base + 3, l)); out.append((base + 4, c))
    return out

# route the sim's warm-up through our deterministic, date-aware source
S._warm_sequences = _warm_before


def main():
    days = sorted(os.path.basename(os.path.dirname(p))
                  for p in glob.glob(os.path.join(HERE, "data", "*", "ticks.jsonl")))
    days = days[-N_DAYS:]
    print(f"exit backtest · {len(days)} days: {days[0]} → {days[-1]} · EMA-50 filter · MIS\n")

    agg = {m: {"net": 0.0, "tr": 0, "win": 0} for m in MODES}
    per_day = {}   # day -> {mode: net}

    for day in days:
        S.DATE = day
        tf = os.path.join(HERE, "data", day, "ticks.jsonl")
        today = S._load_ticks(tf)
        syms = sorted(s for s, seq in today.items() if len(seq) >= 100)
        per_day[day] = {}
        for m in MODES:
            dnet, dtr, dwin = 0.0, 0, 0
            for sym in syms:
                r = S.simulate(sym, today[sym], exit_mode=m)
                dnet += r["net"]; dtr += len(r["trades"])
                dwin += sum(1 for t in r["trades"] if t["pnl"] > 0)
            agg[m]["net"] += dnet; agg[m]["tr"] += dtr; agg[m]["win"] += dwin
            per_day[day][m] = dnet
        base = per_day[day]["current"]
        print(f"{day}  ({len(syms)} stk)  " +
              "  ".join(f"{m}:{per_day[day][m]:+7.0f}" for m in MODES))

    print("\n" + "=" * 68)
    print(f"{'mode':11s} {'net Rs':>10s} {'trades':>7s} {'wins':>5s} {'win%':>6s}  vs current")
    base = agg["current"]["net"]
    for m in MODES:
        a = agg[m]
        wr = (100.0 * a["win"] / a["tr"]) if a["tr"] else 0.0
        delta = a["net"] - base
        print(f"{m:11s} {a['net']:+10.1f} {a['tr']:7d} {a['win']:5d} {wr:5.1f}%  {delta:+8.1f}")
    print("=" * 68)
    best = max(MODES, key=lambda m: agg[m]["net"])
    print(f"best FLAT mode: {best}  ({agg[best]['net']:+.1f})   "
          f"| trail vs current: {agg['trail']['net'] - base:+.1f}")

    # ── ORACLE ceiling: perfect per-day mode selection (HINDSIGHT — not tradable) ──
    oracle = sum(max(per_day[d].values()) for d in per_day)
    oracle_2way = sum(max(per_day[d]["current"], per_day[d]["trailtight"]) for d in per_day)
    print("\n── ceiling if we picked the right exit each day (HINDSIGHT, look-ahead) ──")
    print(f"oracle (best of all 5 modes / day) : {oracle:+.1f}")
    print(f"oracle (current-vs-trailtight only): {oracle_2way:+.1f}")
    print(f"...vs best flat mode {best}: {oracle - agg[best]['net']:+.1f} extra is the regime prize")
    print("per-day winner:", {d: max(per_day[d], key=per_day[d].get) for d in per_day})


if __name__ == "__main__":
    main()
