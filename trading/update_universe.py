#!/usr/bin/env python3
"""
update_universe.py — Refresh the tradable universe (NIFTY 100 constituents) weekly.

Fetches the official NIFTY 100 constituent list from NSE and writes it to data/nifty100.json,
which select_basket.py loads (falling back to its hardcoded list if the file is missing).

SAFETY: if the fetch fails or returns fewer than 50 symbols, the existing file is LEFT
UNTOUCHED — a bad/blocked fetch can never wipe the universe.

Run weekly after market close, e.g. Friday 15:50 IST. Cron (EC2, UTC):
    20 10 * * 5  cd /home/ec2-user/projects/trading && venv/bin/python update_universe.py >> logs/universe.log 2>&1
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "data", "nifty100.json")
MIN_EXPECTED = 50   # never overwrite with a suspiciously short list

# NSE publishes constituents as CSV; primary + mirror. Column of interest: "Symbol".
SOURCES = [
    "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "https://www1.nseindia.com/content/indices/ind_nifty100list.csv",
]
HEADERS = {  # NSE rejects the default UA
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/csv,application/csv,*/*",
}


def _fetch_symbols():
    import csv, io, requests
    for url in SOURCES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200 or not r.text:
                print(f"  {url} -> HTTP {r.status_code}")
                continue
            rows = list(csv.DictReader(io.StringIO(r.text)))
            col = next((c for c in (rows[0].keys() if rows else []) if c.strip().lower() == "symbol"), None)
            if not col:
                print(f"  {url} -> no 'Symbol' column ({list(rows[0].keys()) if rows else 'empty'})")
                continue
            syms = [row[col].strip().upper() for row in rows if row.get(col, "").strip()]
            syms = sorted(set(syms))
            if len(syms) >= MIN_EXPECTED:
                print(f"  {url} -> {len(syms)} symbols")
                return syms
            print(f"  {url} -> only {len(syms)} symbols (<{MIN_EXPECTED}), skipping")
        except Exception as e:
            print(f"  {url} -> {e}")
    return None


def main():
    print(f"[update_universe] {datetime.now(tz=IST):%Y-%m-%d %H:%M IST} — fetching NIFTY 100")
    syms = _fetch_symbols()
    if not syms:
        print("[update_universe] FETCH FAILED — leaving existing universe untouched")
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    payload = {"updated": datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M IST"),
               "count": len(syms), "symbols": syms}
    # write atomically so a crash mid-write can't corrupt the file
    tmp = OUT + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=0)
    os.replace(tmp, OUT)
    print(f"[update_universe] wrote {OUT}  ({len(syms)} symbols)")


if __name__ == "__main__":
    main()
