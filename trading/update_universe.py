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

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
BROWSER = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept-Encoding": "gzip, deflate, br"}
# Legacy CSV archives (often 503 now) — kept as a last resort.
CSV_SOURCES = [
    "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
]


def _from_nse_api():
    """NSE's live JSON API needs a browser session with cookies first (prime the homepage,
    then hit the equity-stockIndices endpoint with a Referer)."""
    import requests
    s = requests.Session()
    s.headers.update(BROWSER)
    try:
        s.get("https://www.nseindia.com/", timeout=20)
        s.get("https://www.nseindia.com/market-data/live-equity-market", timeout=20)
        r = s.get("https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20100",
                  headers={"Accept": "application/json",
                           "Referer": "https://www.nseindia.com/market-data/live-equity-market"},
                  timeout=20)
        if r.status_code != 200:
            print(f"  NSE api -> HTTP {r.status_code}"); return None
        data = r.json().get("data", [])
        syms = sorted({d["symbol"].strip().upper() for d in data
                       if d.get("symbol") and d["symbol"].upper() != "NIFTY 100"})
        print(f"  NSE api -> {len(syms)} symbols")
        return syms if len(syms) >= MIN_EXPECTED else None
    except Exception as e:
        print(f"  NSE api -> {e}"); return None


def _from_csv():
    import csv, io, requests
    for url in CSV_SOURCES:
        try:
            r = requests.get(url, headers={**BROWSER, "Accept": "text/csv,*/*"}, timeout=20)
            if r.status_code != 200 or not r.text:
                print(f"  {url} -> HTTP {r.status_code}"); continue
            rows = list(csv.DictReader(io.StringIO(r.text)))
            col = next((c for c in (rows[0].keys() if rows else []) if c.strip().lower() == "symbol"), None)
            if not col:
                print(f"  {url} -> no 'Symbol' column"); continue
            syms = sorted({row[col].strip().upper() for row in rows if row.get(col, "").strip()})
            print(f"  {url} -> {len(syms)} symbols")
            if len(syms) >= MIN_EXPECTED:
                return syms
        except Exception as e:
            print(f"  {url} -> {e}")
    return None


def _fetch_symbols():
    return _from_nse_api() or _from_csv()


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
