#!/usr/bin/env python3
"""
fetch_option_history.py — pull HISTORICAL option candles from Flattrade's TPSeries.

Flattrade's TPSeries serves OHLCV + OI candles for CURRENTLY-LISTED NFO option
contracts back to their listing (weeklies ~2 weeks, monthlies more; the general
lookback ceiling is ~90 days, as seen on the index). This lets us build a real
historical options dataset NOW instead of only accumulating logged depth.

WHAT YOU GET per candle: time, ssboe (epoch), into/inth/intl/intc (OHLC),
intvwap, intv (interval volume), v (cum volume), oi, intoi. NO bid/ask — so a
backtest off this fills at close ± a modelled spread (not the true book). The
runner's live depth logging (data/options/) remains the higher-fidelity source.

IMPORTANT LIMITATION: TPSeries only serves contracts whose token is still live.
EXPIRED contracts' tokens are not returned by SearchScrip/GetOptionChain, so you
can't fetch a contract's history after it expires. Practical fix: run this daily
(or at least the day before each expiry) to capture every contract's full life
before its token dies — combined with the live logger, history accumulates going
forward. (Fetching already-expired series would need a historical symbol master.)

Output (under trading/, gitignored — share via `git add -f` like tick data):
  data/options_hist/<UNDER>_<expiry>/<tsym>.json   {meta:{...}, candles:[...]}
  data/options_hist/<UNDER>_<expiry>/_spot.json     the index candles
  data/options_hist/<UNDER>_<expiry>/_manifest.json

Run:
  python3 fetch_option_history.py                       # 60d of 5-min, NIFTY+BANKNIFTY
  python3 fetch_option_history.py --interval 1 --days 30
  python3 fetch_option_history.py --underlyings NIFTY --interval 15
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta

from auth import get_session
from client import FlattradeClient
from config import IST
import options_logger as OL

HIST_DIR = "data/options_hist"
SLEEP_BETWEEN_CALLS = 0.30   # gentle on rate limits


def _log(m: str) -> None:
    print(f"{datetime.now(IST):%H:%M:%S}  {m}", flush=True)


def _fetch_series(client, exch, token, start_dt, end_dt, interval):
    try:
        rows = client.get_time_price_series(exch, token, start_dt, end_dt, interval=interval)
        return rows if isinstance(rows, list) else []
    except Exception as e:
        _log(f"    TPSeries error {exch}|{token}: {e}")
        return []


def fetch_underlying(client, name, cfg, start_dt, end_dt, interval) -> dict:
    mani, keys = OL._resolve_chain(client, name, cfg)
    insts = mani.get("instruments", {})
    if not insts:
        _log(f"  {name}: no instruments resolved — skipping")
        return {}
    expiry = mani.get("expiry") or "unknown"
    out_dir = os.path.join(HIST_DIR, f"{name}_{expiry}")
    os.makedirs(out_dir, exist_ok=True)

    _log(f"  {name}: {len(insts)} contracts, expiry {expiry}, "
         f"{interval}m candles {start_dt:%Y-%m-%d}..{end_dt:%Y-%m-%d}")

    saved, total_candles = 0, 0
    for tok, m in insts.items():
        rows = _fetch_series(client, m.get("exch", "NFO"), tok, start_dt, end_dt, interval)
        if rows:
            with open(os.path.join(out_dir, f"{m['tsym']}.json"), "w") as f:
                json.dump({"meta": {"token": tok, **m}, "candles": rows}, f)
            saved += 1
            total_candles += len(rows)
        time.sleep(SLEEP_BETWEEN_CALLS)

    # spot index history too
    spot_tok = str(cfg["spot_token"])
    spot_rows = _fetch_series(client, cfg["spot_exch"], spot_tok, start_dt, end_dt, interval)
    if spot_rows:
        with open(os.path.join(out_dir, "_spot.json"), "w") as f:
            json.dump({"meta": {"token": spot_tok, "exch": cfg["spot_exch"],
                                "underlying": name}, "candles": spot_rows}, f)

    manifest = {
        "underlying": name, "expiry": expiry, "interval_min": interval,
        "fetched_at": datetime.now(IST).isoformat(timespec="seconds"),
        "range": [start_dt.isoformat(), end_dt.isoformat()],
        "spot": {"token": spot_tok, "exch": cfg["spot_exch"], "candles": len(spot_rows)},
        "instruments": {m["tsym"]: {"token": tok, **m} for tok, m in insts.items()},
    }
    with open(os.path.join(out_dir, "_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    _log(f"  {name}: saved {saved}/{len(insts)} contracts ({total_candles} candles) "
         f"+ spot ({len(spot_rows)}) -> {out_dir}")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlyings", default=",".join(OL.UNDERLYINGS))
    ap.add_argument("--interval", type=int, default=5, help="candle minutes: 1/3/5/15/30/60")
    ap.add_argument("--days", type=int, default=60, help="lookback days (TPSeries caps ~90)")
    args = ap.parse_args()

    names = [n.strip().upper() for n in args.underlyings.split(",") if n.strip()]
    end_dt = datetime.now(IST)
    start_dt = end_dt - timedelta(days=args.days)

    uid, token = get_session()
    client = FlattradeClient()
    client.set_session(uid, token)

    os.makedirs(HIST_DIR, exist_ok=True)
    _log(f"fetching {args.interval}m option history, {args.days}d lookback, for {names}")
    for name in names:
        cfg = OL.UNDERLYINGS.get(name)
        if not cfg:
            _log(f"  {name}: no config — skipping")
            continue
        fetch_underlying(client, name, cfg, start_dt, end_dt, args.interval)
    _log("done.")


if __name__ == "__main__":
    main()
