#!/usr/bin/env python3
"""
options_logger.py — record NSE index-option chain data for future backtesting.

WHY: we want to research option strategies but have no historical option data. This
daemon subscribes to a window of strikes around ATM for one or more underlyings and
appends every market-data tick to disk, in the SAME shape as the equity ticks.jsonl,
so the options backtest toolkit (see the research repo `trading-lab/options/`) can
replay it later.

DESIGN — deliberately STANDALONE (separate process from runner.py). It NEVER places
orders and shares no state with the live trader, so it cannot affect live trading.

OUTPUT per run (under trading/):
  data/options/<YYYY-MM-DD>/<UNDERLYING>.jsonl          raw ticks (+ local "rt" epoch)
  data/options/<YYYY-MM-DD>/<UNDERLYING>_manifest.json  token -> {tsym,strike,opt,expiry,lot}

Run:
  python3 options_logger.py                 # log until EOD (15:30 IST)
  python3 options_logger.py --dry-run       # resolve + write manifest, print, exit (no WS)
  python3 options_logger.py --underlyings NIFTY

Instrument resolution is best-effort via SearchScrip + defensive tsym parsing. If it
resolves 0 instruments for an underlying, drop a manual list at
  data/options/instruments_<UNDERLYING>.json   ->  [{"exch":"NFO","token":"...","tsym":"...",
       "strike":24000,"opt":"CE","expiry":"2026-07-31","lot_size":75}, ...]
and it will use that instead. VERIFY resolution on the first live run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

from auth import get_session
from client import FlattradeClient
from config import IST, NIFTY_TOKEN, BANKNIFTY_TOKEN

# ── config ────────────────────────────────────────────────────────────────
# strike step + how many strikes each side of ATM to log, per underlying.
UNDERLYINGS: Dict[str, dict] = {
    "NIFTY":     {"spot_token": NIFTY_TOKEN,     "spot_exch": "NSE", "step": 50,  "n_each_side": 10, "lot_size": 75},
    "BANKNIFTY": {"spot_token": BANKNIFTY_TOKEN, "spot_exch": "NSE", "step": 100, "n_each_side": 10, "lot_size": 35},
}
FEED_TYPE   = "d"       # 'd' = depth (L1/L5 book + lp + oi + v); 't' = touchline (lighter)
NFO_EXCH    = "NFO"
EOD_HOUR, EOD_MIN = 15, 30   # stop logging at 15:30 IST (a bit past equity EOD for late option prints)
FLUSH_EVERY = 200            # flush files every N ticks

_MONTHS = {m: i for i, m in enumerate(
    ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"], start=1)}


def _log(msg: str) -> None:
    print(f"{datetime.now(IST):%H:%M:%S}  {msg}", flush=True)


# ── instrument resolution ───────────────────────────────────────────────────
def _parse_option_tsym(tsym: str) -> Optional[Tuple[str, date, str, float]]:
    """
    Best-effort parse of a Flattrade NFO option trading symbol into
    (underlying, expiry_date, opt_type[CE/PE], strike).
    Handles the common forms, e.g.:
        NIFTY31JUL25C24000     -> ('NIFTY', 2025-07-31, 'CE', 24000.0)
        BANKNIFTY26JUN25P51000 -> ('BANKNIFTY', 2025-06-26, 'PE', 51000.0)
        NIFTY31JUL25CE24000 / ...24000CE  (defensive variants)
    Returns None if it doesn't look like a dated option.
    """
    s = tsym.upper().strip()
    m = re.match(r"^([A-Z]+?)(\d{1,2})([A-Z]{3})(\d{2})(.*)$", s)
    if not m:
        return None
    sym, dd, mon, yy, rest = m.groups()
    if mon not in _MONTHS:
        return None
    try:
        expiry = date(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None
    # rest is like "C24000" / "P24000" / "CE24000" / "24000CE"
    mo = re.match(r"^(CE|PE|C|P)(\d+(?:\.\d+)?)$", rest)
    if not mo:
        mo2 = re.match(r"^(\d+(?:\.\d+)?)(CE|PE|C|P)$", rest)
        if not mo2:
            return None
        strike_s, ot = mo2.groups()
    else:
        ot, strike_s = mo.groups()
    opt = "CE" if ot in ("C", "CE") else "PE"
    try:
        strike = float(strike_s)
    except ValueError:
        return None
    return sym, expiry, opt, strike


def _spot_ltp(client: FlattradeClient, exch: str, token: str) -> Optional[float]:
    try:
        q = client.get_quotes(exch, token)
        if isinstance(q, dict) and q.get("stat") == "Ok":
            for k in ("lp", "c"):
                if q.get(k):
                    return float(q[k])
    except Exception as e:
        _log(f"  spot quote error ({exch}|{token}): {e}")
    return None


def _expiry_token(d: date) -> str:
    """date(2026,7,28) -> '28JUL26' (the token used inside NFO option trading symbols)."""
    return f"{d.day:02d}{d.strftime('%b').upper()}{d.year % 100:02d}"


def _nearest_expiry(client: FlattradeClient, name: str) -> Optional[date]:
    """Nearest expiry (>= today) for `name`, discovered from SearchScrip `exd` fields
    (format 'DD-MON-YYYY'). Uses option rows if present, else any matching-symbol row."""
    rows = client.search_scrip(NFO_EXCH, name)
    today = datetime.now(IST).date()
    exps = []
    for r in rows:
        if r.get("symname") != name:
            continue
        exd = r.get("exd")
        if not exd:
            continue
        try:
            exps.append(datetime.strptime(exd, "%d-%b-%Y").date())
        except (ValueError, TypeError):
            continue
    future = sorted(e for e in exps if e >= today)
    if future:
        return future[0]
    return sorted(exps)[0] if exps else None


def _mk_inst(v: dict, expiry: date, lot_default: int) -> Optional[dict]:
    """Normalize one GetOptionChain / SearchScrip row into our instrument dict."""
    if v.get("optt") not in ("CE", "PE"):
        return None
    tok = v.get("token")
    if not tok:
        return None
    try:
        strike = float(v["strprc"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        ls = int(float(v.get("ls"))) if v.get("ls") else lot_default
    except (TypeError, ValueError):
        ls = lot_default
    return {"exch": NFO_EXCH, "token": str(tok), "tsym": v.get("tsym"),
            "strike": strike, "opt": v["optt"], "expiry": expiry.isoformat(), "lot_size": ls}


def _chain_via_optionchain(client, name, exptoken, expiry, atm, step, n, lot) -> List[dict]:
    """Enumerate the ATM +/- n window using GetOptionChain. It returns strikes ascending
    from the seed strike, so seeding at the window BOTTOM (and again at ATM) sweeps the
    whole window in a couple of calls. Dedup by token."""
    lo, hi = atm - n * step, atm + n * step
    cnt = 2 * n + 2
    insts: Dict[str, dict] = {}
    for opt_char in ("C", "P"):
        for center in (lo, atm):
            tsym = f"{name}{exptoken}{opt_char}{int(center)}"
            try:
                res = client.get_option_chain(NFO_EXCH, tsym, center, cnt)
            except Exception as e:
                _log(f"    {name}: GetOptionChain({tsym}) error: {e}")
                continue
            if not isinstance(res, dict) or res.get("stat") != "Ok":
                continue
            for v in res.get("values", []):
                inst = _mk_inst(v, expiry, lot)
                if inst and lo <= inst["strike"] <= hi:
                    insts[inst["token"]] = inst
    return list(insts.values())


def _chain_via_search(client, name, exptoken, expiry, atm, step, n, lot) -> List[dict]:
    """Fallback: resolve each strike x {CE,PE} in the window by an EXACT SearchScrip on the
    constructed trading symbol (proven to return exactly that contract). ~2*(2n+1) calls."""
    lo, hi = atm - n * step, atm + n * step
    insts: Dict[str, dict] = {}
    strike = lo
    while strike <= hi:
        for opt_char in ("C", "P"):
            tsym = f"{name}{exptoken}{opt_char}{int(strike)}"
            try:
                rows = client.search_scrip(NFO_EXCH, tsym)
            except Exception:
                rows = []
            for r in rows:
                if r.get("tsym") != tsym:
                    continue
                # SearchScrip rows carry optt; synthesize strprc from the strike we asked for
                r = dict(r)
                r.setdefault("strprc", strike)
                inst = _mk_inst(r, expiry, lot)
                if inst:
                    insts[inst["token"]] = inst
        strike += step
    return list(insts.values())


def _resolve_chain(client: FlattradeClient, name: str, cfg: dict) -> Tuple[dict, List[Tuple[str, str]]]:
    """Return (manifest, [(exch, token), ...]) for `name`, nearest expiry, ATM +/- n strikes."""
    manual_path = f"data/options/instruments_{name}.json"
    spot = _spot_ltp(client, cfg["spot_exch"], cfg["spot_token"])
    step = cfg["step"]
    atm = round(spot / step) * step if spot else None

    # 0) manual override
    if os.path.exists(manual_path):
        insts = json.load(open(manual_path))
        _log(f"  {name}: using MANUAL instrument list ({len(insts)}) from {manual_path}")
        manifest = _manifest(name, cfg, spot, atm, insts)
        return manifest, [(i.get("exch", NFO_EXCH), str(i["token"])) for i in insts]

    if spot is None:
        _log(f"  {name}: could not read spot LTP — skipping (drop a manual list to override)")
        return {}, []

    # 1) discover the nearest expiry, build the token used inside option tsyms
    expiry = _nearest_expiry(client, name)
    if not expiry:
        _log(f"  {name}: could not discover an expiry via SearchScrip — provide {manual_path}")
        return {}, []
    exptoken = _expiry_token(expiry)
    n, lot = cfg["n_each_side"], cfg["lot_size"]

    # 2) enumerate the strike window: GetOptionChain, with per-strike search as fallback
    insts = _chain_via_optionchain(client, name, exptoken, expiry, atm, step, n, lot)
    if not insts:
        _log(f"  {name}: GetOptionChain returned nothing — falling back to per-strike search")
        insts = _chain_via_search(client, name, exptoken, expiry, atm, step, n, lot)

    if not insts:
        _log(f"  {name}: resolved 0 instruments (expiry={expiry}, token={exptoken}). "
             f"Provide {manual_path} to override.")
        return {}, []

    insts.sort(key=lambda p: (p["strike"], p["opt"]))
    lo, hi = atm - n * step, atm + n * step
    _log(f"  {name}: spot={spot:.1f} atm={atm} expiry={expiry} token={exptoken} "
         f"strikes[{lo:.0f}..{hi:.0f}] -> {len(insts)} option instruments")

    manifest = _manifest(name, cfg, spot, atm, insts)
    keys = [(i["exch"], i["token"]) for i in insts]
    keys.append((cfg["spot_exch"], str(cfg["spot_token"])))   # log the spot too
    return manifest, keys


def _manifest(name: str, cfg: dict, spot: Optional[float], atm, insts: List[dict]) -> dict:
    lot = insts[0]["lot_size"] if insts else cfg["lot_size"]
    expiry = insts[0]["expiry"] if insts else None
    return {
        "underlying": name,
        "spot_token": str(cfg["spot_token"]),
        "date": datetime.now(IST).date().isoformat(),
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "expiry": expiry,
        "atm_at_start": atm,
        "spot_at_start": spot,
        "step": cfg["step"],
        "lot_size": lot,
        "spot": {"token": str(cfg["spot_token"]), "exch": cfg["spot_exch"]},
        "instruments": {i["token"]: {k: i[k] for k in ("tsym", "exch", "strike", "opt", "expiry", "lot_size")}
                        for i in insts},
    }


# ── logger state ────────────────────────────────────────────────────────────
class OptionsLogger:
    def __init__(self, manifests: Dict[str, dict]) -> None:
        self._fh: Dict[str, Any] = {}          # underlying -> file handle
        self._tok2under: Dict[str, str] = {}   # token -> underlying (for routing)
        self._n = 0
        self._shutting_down = False
        for name, mani in manifests.items():
            self._tok2under[mani["spot_token"]] = name
            for tok in mani["instruments"]:
                self._tok2under[tok] = name

    def open_files(self, day: str) -> None:
        base = f"data/options/{day}"
        os.makedirs(base, exist_ok=True)
        for name in {v for v in self._tok2under.values()}:
            self._fh[name] = open(f"{base}/{name}.jsonl", "a")

    def on_tick(self, msg: dict) -> None:
        tok = msg.get("tk")
        if not tok:
            return
        name = self._tok2under.get(str(tok))
        if not name:
            return
        msg["rt"] = round(time.time(), 3)   # local receive epoch
        fh = self._fh.get(name)
        if fh:
            fh.write(json.dumps(msg, separators=(",", ":")) + "\n")
            self._n += 1
            if self._n % FLUSH_EVERY == 0:
                fh.flush()

    def flush_all(self) -> None:
        for fh in self._fh.values():
            try:
                fh.flush()
            except ValueError:
                pass

    def close(self) -> None:
        self._shutting_down = True
        self.flush_all()
        for fh in self._fh.values():
            try:
                fh.close()
            except ValueError:
                pass


def _write_manifests(day: str, manifests: Dict[str, dict]) -> None:
    base = f"data/options/{day}"
    os.makedirs(base, exist_ok=True)
    for name, mani in manifests.items():
        with open(f"{base}/{name}_manifest.json", "w") as f:
            json.dump(mani, f, indent=2)
    _log(f"manifests -> {base}/<UNDERLYING>_manifest.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlyings", default=",".join(UNDERLYINGS),
                    help="comma list, e.g. NIFTY,BANKNIFTY")
    ap.add_argument("--dry-run", action="store_true", help="resolve + write manifest, then exit")
    args = ap.parse_args()

    names = [n.strip().upper() for n in args.underlyings.split(",") if n.strip()]
    day = datetime.now(IST).date().isoformat()

    uid, token = get_session()
    client = FlattradeClient()
    client.set_session(uid, token)

    _log(f"resolving chains for {names} ...")
    manifests: Dict[str, dict] = {}
    all_keys: List[Tuple[str, str]] = []
    for name in names:
        cfg = UNDERLYINGS.get(name)
        if not cfg:
            _log(f"  {name}: no config — skipping")
            continue
        mani, keys = _resolve_chain(client, name, cfg)
        if not keys:
            continue
        manifests[name] = mani
        all_keys.extend(keys)

    if not manifests:
        _log("no instruments resolved for any underlying — exiting (see manual-list note in header)")
        sys.exit(1)

    _write_manifests(day, manifests)
    total_opts = sum(len(m["instruments"]) for m in manifests.values())
    _log(f"resolved {total_opts} option instruments across {len(manifests)} underlyings "
         f"({len(all_keys)} feed keys incl. spot)")

    if args.dry_run:
        _log("--dry-run: manifest written, not connecting WS. Done.")
        return

    logger = OptionsLogger(manifests)
    logger.open_files(day)
    scrip_keys = "#".join(f"{exch}|{tok}" for exch, tok in all_keys)

    def on_open(c: FlattradeClient) -> None:
        c.subscribe(scrip_keys, feed_type=FEED_TYPE)
        _log(f"WS CONNECTED  subscribed={len(all_keys)} keys  feed='{FEED_TYPE}'  -> data/options/{day}/")

    def on_close(code=None, msg=None) -> None:
        _log(f"WS DISCONNECTED code={code} reason={msg} ticks={logger._n}")
        logger.flush_all()

    def on_error(e) -> None:
        if not logger._shutting_down:
            _log(f"WS ERROR {e}")

    def _shutdown(sig, frame):
        _log(f"signal {sig} — flushing {logger._n} ticks and exiting")
        logger.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # watchdog thread: stop cleanly at EOD
    import threading

    def _eod_watch():
        while not logger._shutting_down:
            now = datetime.now(IST)
            if (now.hour, now.minute) >= (EOD_HOUR, EOD_MIN):
                _log(f"EOD {EOD_HOUR:02d}:{EOD_MIN:02d} reached — {logger._n} ticks logged, shutting down")
                logger.close()
                if client.ws:
                    try:
                        client.ws.close()
                    except Exception:
                        pass
                os._exit(0)
            time.sleep(15)

    threading.Thread(target=_eod_watch, daemon=True).start()

    client.start_websocket(on_tick=logger.on_tick, on_open=on_open,
                           on_close=on_close, on_error=on_error)


if __name__ == "__main__":
    main()
