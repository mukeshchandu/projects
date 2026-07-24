from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from dotenv import load_dotenv
load_dotenv("/home/ec2-user/projects/trading/.env")

from auth import get_session
from client import FlattradeClient
from marketdata import CandleBuilder, Tick
from paper import PaperBroker
from live_broker import LiveBroker
from strategies.supertrend import SupertrendStrategy
from config import (IST, EOD_EXIT_HOUR, EOD_EXIT_MINUTE,
                    MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE)

# ── Logging ─────────────────────────────────────────────────────────────
_today = datetime.now(tz=IST).strftime("%Y-%m-%d")
os.makedirs("logs", exist_ok=True)
os.makedirs(f"data/{_today}", exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(f"logs/runner_{_today}.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Basket ───────────────────────────────────────────────────────────────
BASKET = [
    ("NSE", "HFCL"),
    ("NSE", "BANKBARODA"),
    ("NSE", "NMDC"),
    ("NSE", "CESC"),
    ("NSE", "ZEEL"),
    ("NSE", "BALRAMCHIN"),
    ("NSE", "GRANULES"),
    ("NSE", "SUZLON"),
]
# MIS = intraday (EOD exit 3PM) | CNC = delivery (hold overnight)
MODES = {
    "HFCL":       "CNC",
    "BANKBARODA": "CNC",
    "NMDC":       "CNC",
    "CESC":       "CNC",
    "ZEEL":       "CNC",
    "BALRAMCHIN": "CNC",
    "GRANULES":   "MIS",
    "SUZLON":     "MIS",
}
MAX_CAPITAL_PER_STOCK = 5_000          # own capital deployed per stock (pre-leverage)
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS", "2"))
MAX_ENTRY_RETRIES  = 3   # re-place a cancelled ENTRY at most N times (fresh quote, +1 tick)
CAPITAL_PER_TRADE  = MAX_CAPITAL_PER_STOCK
WALLET_DEPLOY_FRAC = 0.85              # never commit more than 85% of wallet cash
EMA_PERIOD = int(os.getenv("EMA_FILTER", "0")) or None   # EMA trend filter (None = off; dynamic basket sets 50)
INTERVAL_S            = 900

# ── State ────────────────────────────────────────────────────────────────
INSTRUMENTS:  Dict[str, dict]          = {}
broker        = PaperBroker()
_open_trades: Dict[str, Optional[dict]] = {}
_trade_no     = 0
_total_pnl    = 0.0
_last_tick:   Dict[str, datetime]      = {}
_quote:       Dict[str, tuple]         = {}   # symbol -> (best_bid, best_ask) from the feed
_last_price:  Dict[str, float]         = {}   # last seen ltp per symbol (carry across quote-only ticks)
_eod_done     = False
_eod_orphan_done: set                  = set()  # symbols whose orphan net was squared at EOD
_shutting_down = False
_runner_state_path = "data/runner_state.json"

# ── Tick file (raw) ──────────────────────────────────────────────────────
_tick_path = f"data/{_today}/ticks.jsonl"
_tick_fh   = open(_tick_path, "a")

# ── Trade CSV ────────────────────────────────────────────────────────────
_csv_path = f"logs/trades_{_today}.csv"
_csv_fh   = open(_csv_path, "a")
if os.path.getsize(_csv_path) == 0:
    _csv_fh.write("trade_no,symbol,side,entry_time,entry_price,exit_time,exit_price,qty,gross_pnl,cost,net_pnl\n")
    _csv_fh.flush()

_entry_meta: Dict[int, dict] = {}   # trade_no → {entry_time}

# ── Strategy-state log (per closed candle: the belt/EMA/trend the strategy actually saw) ──
_strat_path = f"logs/strategy_{_today}.csv"
_strat_fh   = open(_strat_path, "a")
if os.path.getsize(_strat_path) == 0:
    _strat_fh.write("time,symbol,open,high,low,close,atr,supertrend,trend,upper,lower,ema,position,signal,reason\n")
    _strat_fh.flush()


# ── Options chain logging (piggybacks on the SAME WebSocket — Flattrade allows only ONE
# active WS per session token, so we cannot run a second logger process. Option/spot ticks
# are routed out at the very TOP of handle_tick and never touch trading logic.) ──
import options_logger as _OL   # reuse its chain resolver + UNDERLYINGS config

LOG_OPTIONS = os.getenv("LOG_OPTIONS", "1").strip().lower() not in ("0", "false", "no", "off", "")
OPTION_FEED = os.getenv("OPTION_FEED", "d")   # 'd' = full depth book (raw, nothing dropped); 't' = touchline
_opt_under_of: Dict[str, str] = {}   # token -> underlying (routes a tick to the right file)
_opt_fh: Dict[str, object]    = {}   # underlying -> open jsonl file handle
_opt_nfo_keys: list           = []   # ["NFO|63927", ...] subscribed with OPTION_FEED (depth)
_opt_spot_keys: list          = []   # ["NSE|26000", ...] subscribed with 't' (index has no depth)
_opt_tick_n = 0


def _setup_options_logging(client) -> None:
    """Resolve option chains for the configured underlyings and open per-underlying jsonl
    files. BEST-EFFORT: any failure logs a warning and leaves trading completely unaffected
    (options logging simply stays off for the day)."""
    if not LOG_OPTIONS:
        log.info("Options logging DISABLED (LOG_OPTIONS=0)")
        return
    try:
        base = f"data/options/{_today}"
        os.makedirs(base, exist_ok=True)
        manifests = {}
        for name, cfg in _OL.UNDERLYINGS.items():
            try:
                mani, keys = _OL._resolve_chain(client, name, cfg)
            except Exception as e:
                log.warning("options: resolve %s failed (%s) — skipping", name, e)
                continue
            if not keys:
                continue
            manifests[name] = mani
            _opt_fh[name] = open(f"{base}/{name}.jsonl", "a")
            for exch, tok in keys:
                _opt_under_of[str(tok)] = name
                (_opt_nfo_keys if exch == "NFO" else _opt_spot_keys).append(f"{exch}|{tok}")
        for name, mani in manifests.items():
            with open(f"{base}/{name}_manifest.json", "w") as f:
                json.dump(mani, f, indent=2)
        if _opt_under_of:
            log.info("OPTIONS LOGGING ON: %d option instruments, %d underlyings, feed='%s' -> %s",
                     len(_opt_nfo_keys), len(manifests), OPTION_FEED, base)
        else:
            log.warning("OPTIONS LOGGING: resolved 0 instruments — off for today")
    except Exception as e:
        log.warning("options logging setup failed (%s) — continuing WITHOUT it (trading unaffected)", e)
        _opt_under_of.clear(); _opt_fh.clear(); _opt_nfo_keys.clear(); _opt_spot_keys.clear()


def _log_option_tick(name: str, msg: dict) -> None:
    """Append one raw option/spot tick (full message + local receive time) to its jsonl."""
    global _opt_tick_n
    fh = _opt_fh.get(name)
    if fh is None:
        return
    msg["rt"] = round(time.time(), 3)
    try:
        fh.write(json.dumps(msg, separators=(",", ":")) + "\n")
        _opt_tick_n += 1
        if _opt_tick_n % 200 == 0:
            fh.flush()
    except (ValueError, OSError):
        pass


def _flush_options() -> None:
    for fh in _opt_fh.values():
        try:
            fh.flush()
        except (ValueError, OSError):
            pass


def _log_strat_state(candle, symbol, strat, sig) -> None:
    """Record the real-time strategy state at each closed candle so behaviour can be
    reviewed/charted from the actual live values — no recomputation needed."""
    def f(v):
        return f"{v:.2f}" if isinstance(v, (int, float)) else ""
    try:
        _strat_fh.write(",".join([
            candle.start.strftime("%Y-%m-%d %H:%M"), symbol,
            f(candle.open), f(candle.high), f(candle.low), f(candle.close),
            f(getattr(strat, "_atr", None)), f(getattr(strat, "_supertrend", None)),
            str(getattr(strat, "_trend", "")),
            f(getattr(strat, "_upper", None)), f(getattr(strat, "_lower", None)),
            f(getattr(strat, "_ema", None)), str(getattr(strat, "position", "")),
            (sig.get("action", "") if sig else ""),
            (sig.get("reason", "").replace(",", " ") if sig else ""),
        ]) + "\n")
        _strat_fh.flush()
    except Exception as e:
        log.warning("strat-state log failed for %s: %s", symbol, e)


def _load_today_basket():
    """If data/today_basket.json exists and is for today, use it (dynamic selection);
    otherwise return None and the built-in fixed basket is used."""
    path = "data/today_basket.json"
    if not os.path.exists(path):
        return None
    try:
        s = json.load(open(path))
    except Exception as e:
        log.warning("today_basket load failed: %s — using default basket", e)
        return None
    if s.get("date") != _today:
        log.warning("today_basket is stale (date=%s, today=%s) — using default basket",
                    s.get("date"), _today)
        return None
    stocks = s.get("stocks") or []
    if not stocks:
        return None
    basket = [("NSE", x["symbol"]) for x in stocks]
    modes  = {x["symbol"]: x.get("mode", "MIS") for x in stocks}
    return basket, modes


def resolve_tokens(client: FlattradeClient) -> None:
    for exch, sym in BASKET:
        results = client.search_scrip(exch, sym)
        token = None
        tsym  = None
        tick  = None
        for r in results:
            ts = r.get("tsym", "")
            if ts in (f"{sym}-EQ", sym):
                token = r.get("token")
                tsym  = ts
                tick  = r.get("ti")
                break
        if not token and results:
            token = results[0].get("token")
            tsym  = results[0].get("tsym", f"{sym}-EQ")
            tick  = results[0].get("ti")
        if not tsym:
            tsym = f"{sym}-EQ"
        if not token:
            log.warning("No token for %s — skipping", sym)
            continue
        try:
            tick_f = float(tick) if tick else None
        except (TypeError, ValueError):
            tick_f = None
        INSTRUMENTS[token] = {
            "symbol":   sym,
            "exchange": exch,
            "tsym":     tsym,
            "mode":     MODES.get(sym, "MIS"),
            "ti":       tick_f,
            "strategy": SupertrendStrategy(symbol=sym, qty=1,
                                           long_only=(MODES.get(sym, "MIS") == "CNC"),
                                           ema_period=EMA_PERIOD),
            "builder":  CandleBuilder(interval_seconds=INTERVAL_S),
        }
        _open_trades[sym] = None
        log.info("Resolved  %-15s  token=%s  tick=%s", sym, token, tick_f)


def _sync_positions_from_exchange(client: FlattradeClient) -> None:
    """On restart: read open positions from exchange and restore _open_trades."""
    try:
        result = client.positions()
        if not isinstance(result, list):
            return
        for p in result:
            tsym   = p.get("tsym", "")
            sym    = tsym.replace("-EQ", "")
            netqty = int(p.get("netqty", 0) or 0)
            if sym not in _open_trades or netqty == 0:
                continue
            if _open_trades.get(sym):
                continue   # already restored from saved ledger — don't overwrite
            side = "LONG" if netqty > 0 else "SHORT"
            avg  = float(p.get("netavgprc", 0) or 0)
            qty  = abs(netqty)
            _open_trades[sym] = {"side": side, "entry": avg, "qty": qty, "ts_str": "resumed"}
            log.warning("POSITION RESUMED  %-12s  %s  qty=%d  entry=Rs%.2f",
                        sym, side, qty, avg)
    except Exception as e:
        log.warning("Position sync failed: %s — starting with clean slate", e)


def _heartbeat() -> None:
    """Log a warning if any symbol goes silent for >10 min during market hours."""
    while True:
        time.sleep(60)
        now = datetime.now(tz=IST)
        in_market = (
            (now.hour > 9 or (now.hour == 9 and now.minute >= 15)) and
            (now.hour < 15 or (now.hour == 15 and now.minute < 30))
        )
        if not in_market:
            continue
        for inst in INSTRUMENTS.values():
            sym  = inst["symbol"]
            last = _last_tick.get(sym)
            if last is None:
                continue
            gap_min = (now - last).total_seconds() / 60
            if gap_min > 10:
                log.warning("TICK GAP  %s — silent for %.0f min  (last tick %s)",
                            sym, gap_min, last.strftime("%H:%M:%S"))


def _eod_summary() -> None:
    log.info("─" * 60)
    log.info("EOD SUMMARY  date=%s  trades=%d  total_pnl=Rs%.2f",
             _today, _trade_no, _total_pnl)
    still_open = {s: t for s, t in _open_trades.items() if t}
    if still_open:
        for sym, t in still_open.items():
            if MODES.get(sym, "MIS") == "CNC":
                log.info("CNC OVERNIGHT  %s  %s  entry=Rs%.2f  qty=%d",
                         sym, t["side"], t["entry"], t["qty"])
            else:
                log.warning("STILL OPEN  %s  %s  entry=Rs%.2f  qty=%d",
                            sym, t["side"], t["entry"], t["qty"])
    else:
        log.info("All positions flat at EOD")
    log.info("Ticks  -> %s", _tick_path)
    log.info("Trades -> %s", _csv_path)
    log.info("─" * 60)


def _save_runner_state() -> None:
    """Persist the position ledger so a restart resumes exactly where we left off."""
    try:
        with open(_runner_state_path, "w") as f:
            json.dump({
                "date":        _today,
                "trade_no":    _trade_no,
                "total_pnl":   _total_pnl,
                "open_trades": _open_trades,
            }, f)
    except Exception as e:
        log.warning("runner state save failed: %s", e)


def _load_runner_state() -> None:
    """Restore _open_trades across restarts. MIS positions from a prior day are dropped
    (they were EOD-squared at 15:00); CNC positions carry overnight."""
    global _trade_no, _total_pnl
    if not os.path.exists(_runner_state_path):
        return
    try:
        s = json.load(open(_runner_state_path))
    except Exception as e:
        log.warning("runner state load failed: %s", e)
        return
    saved_date = s.get("date")
    _trade_no  = int(s.get("trade_no", 0) or 0)
    _total_pnl = float(s.get("total_pnl", 0.0) or 0.0)
    for sym, t in (s.get("open_trades") or {}).items():
        if not t or sym not in _open_trades:
            continue
        if MODES.get(sym, "MIS") == "MIS" and saved_date != _today:
            log.info("Dropping stale MIS position %s (saved %s)", sym, saved_date)
            continue
        _open_trades[sym] = t
        log.warning("STATE RESTORED  %-12s  %s  entry=Rs%.2f  qty=%d  (saved %s)",
                    sym, t.get("side"), float(t.get("entry", 0) or 0),
                    int(t.get("qty", 0) or 0), saved_date)


def _align_strategy_from_trades() -> None:
    """Make each strategy's internal position match the runner ledger (the source of
    truth for whether a position exists) so hard/trailing SL run on resumed positions."""
    for inst in INSTRUMENTS.values():
        sym   = inst["symbol"]
        strat = inst["strategy"]
        t     = _open_trades.get(sym)
        if t:
            strat.position = 1 if t["side"] == "LONG" else -1
            if getattr(strat, "_entry_price", None) is None:
                strat._entry_price = float(t.get("entry", 0) or 0)
            log.info("ALIGN  %-12s  strat.position=%d  entry=Rs%.2f",
                     sym, strat.position, strat._entry_price or 0.0)
        elif strat.position != 0:
            # ledger says flat but strategy state disagreed — trust the ledger
            strat.position     = 0
            strat._entry_price = None
            strat._entry_atr   = None


def _charges(entry_val: float, exit_val: float, mode: str) -> float:
    """Estimated Flattrade round-trip charges (INR): STT + exchange + SEBI + stamp + GST,
    plus the flat DP charge on CNC (delivery) sells. Brokerage is nil on Flattrade."""
    if mode == "CNC":
        stt = 0.001 * entry_val + 0.001 * exit_val
        stamp = 0.00015 * entry_val
        dp = 20.0 * 1.18
    else:
        stt = 0.00025 * exit_val
        stamp = 0.00003 * entry_val
        dp = 0.0
    exch = 0.0000307 * (entry_val + exit_val)
    sebi = 0.000001 * (entry_val + exit_val)
    return stt + stamp + dp + exch + sebi + 0.18 * (exch + sebi)


def _flush_and_close() -> None:
    _tick_fh.flush()
    _tick_fh.close()
    _csv_fh.flush()
    _csv_fh.close()
    _strat_fh.flush()
    _strat_fh.close()
    for fh in _opt_fh.values():
        try:
            fh.flush(); fh.close()
        except (ValueError, OSError):
            pass


class TradingApp:
    def _book_exit(self, symbol, t, xp, qty, reason, exit_ts):
        """Finalise a closed trade: realise NET PnL, log, CSV, clear the ledger. Called only
        once the exit has actually FILLED (live: on confirmation; paper: immediately)."""
        global _total_pnl
        mode = MODES.get(symbol, "MIS")
        gross = ((xp - t["entry"]) if t["side"] == "LONG" else (t["entry"] - xp)) * qty
        cost  = _charges(t["entry"] * qty, xp * qty, mode)
        net   = gross - cost
        _total_pnl += net
        _open_trades[symbol] = None
        result = "WIN " if net >= 0 else "LOSS"
        log.info("EXIT  %-12s  %s  net=Rs%+.2f (gross=Rs%+.2f cost=Rs%.2f)  entry=Rs%.2f  "
                 "exit=Rs%.4f  qty=%d  total=Rs%+.2f  [%s]",
                 symbol, result, net, gross, cost, t["entry"], xp, qty, _total_pnl, reason)
        _csv_fh.write(f"{t.get('trade_no','')},{symbol},{t['side']},{t.get('ts_str','')},"
                      f"{t['entry']:.2f},{exit_ts},{xp:.2f},{qty},{gross:.2f},{cost:.2f},{net:.2f}\n")
        _csv_fh.flush()
        _save_runner_state()

    def _do_exit(self, symbol: str, px: float, reason: str, ts_str: str) -> bool:
        """Place an AGGRESSIVE exit order. LIVE: the trade is booked only when the fill
        confirmation arrives (handle_order); if the order cancels it is RETRIED — an open
        position is never abandoned. PAPER: the returned fill is final and booked now.
        The 'exiting' flag prevents firing duplicate orders while one is in flight."""
        t = _open_trades.get(symbol)
        if not t or t.get("exiting"):
            return False
        side = "SELL" if t["side"] == "LONG" else "BUY"
        # First TWO attempts sit AT the fresh bid/ask (0 ticks) — re-quote before giving up a
        # tick; only from the 2nd retry on do we cross a fixed 1 tick to force the fill.
        cross = 1 if t.get("exit_tries", 0) >= 2 else 0
        fill = broker.simulate_fill(symbol, side, t["qty"], px, reason,
                                    quote=_quote.get(symbol), cross_ticks=cross, is_exit=True)
        if fill is None:
            t["exit_armed"] = True          # no quote / placement failed -> retry next tick
            return False
        if hasattr(broker, "pending") and getattr(fill, "ordno", ""):
            t["exiting"] = True
            t["exit_armed"] = False
            broker.pending[fill.ordno].update({
                "kind": "exit", "symbol": symbol, "entry": t["entry"], "side": t["side"],
                "mode": MODES.get(symbol, "MIS"), "qty": t["qty"],
                "trade_no": t.get("trade_no", ""), "entry_time": t.get("ts_str", ""),
                "exit_ts": ts_str, "reason": reason})
            log.info("EXIT ORDER  %-12s  %s  qty=%d  [%s] — awaiting fill", symbol, side, t["qty"], reason)
            return True
        self._book_exit(symbol, t, fill.price, t["qty"], reason, ts_str)
        return True

    def _do_entry(self, symbol: str, price: float, reason: str, ts_str: str) -> bool:
        """Place/RE-place an ENTRY order. The first TWO attempts (initial + 1st retry) sit AT
        the FRESH bid/ask (0 ticks) — a first cancel is usually just the quote moving, so we
        re-quote without giving up a tick. Only from the 2nd retry on do we cross a fixed 1
        tick to force the fill. Always priced off the fresh quote, never the stale one, never
        LTP. LIVE: real only once the fill confirms; PAPER: filled immediately."""
        t = _open_trades.get(symbol)
        if not t or t.get("filled") or t.get("exiting"):
            return False
        side  = "BUY" if t["side"] == "LONG" else "SELL"
        cross = 1 if t.get("entry_tries", 0) >= 2 else 0
        fill  = broker.simulate_fill(symbol, side, t["qty"], price, reason,
                                     quote=_quote.get(symbol), cross_ticks=cross, is_exit=False)
        if fill is None:
            t["entry_armed"] = True          # no live quote yet -> retry on the next fresh tick
            return False
        if hasattr(broker, "pending") and getattr(fill, "ordno", ""):
            t["entry_armed"] = False
            t["entry"] = fill.price
            broker.pending[fill.ordno].update({
                "kind": "entry", "symbol": symbol, "side": side,
                "mode": MODES.get(symbol, "MIS"), "qty": t["qty"], "reason": reason})
            log.info("ENTRY ORDER  %-12s  %s  qty=%d  Rs%.2f  [%s] — awaiting fill",
                     symbol, side, t["qty"], fill.price, reason)
            return True
        # PAPER: immediate fill
        t["entry"] = fill.price; t["filled"] = True; t["entry_armed"] = False
        log.info("ENTRY #%-3d  %-12s  %-5s  Rs%.2f  qty=%d  [%s]",
                 t.get("trade_no", 0), symbol, t["side"], fill.price, t["qty"], reason)
        return True

    def handle_tick(self, msg: dict) -> None:
        global _trade_no, _eod_done

        # ── Options/spot chain logging (piggyback) ──────────────────────────────
        # Route option & index ticks OUT here — before the tk/tf filter and before any
        # trading logic — and log the FULL raw message (depth 'dk'/'df' included). Trading
        # below only ever sees equity tokens (those in INSTRUMENTS). This cannot affect
        # order handling in any way.
        if _opt_under_of:
            u = _opt_under_of.get(str(msg.get("tk")))
            if u:
                _log_option_tick(u, msg)
                return

        if msg.get("t") not in ("tk", "tf"):
            return

        token = msg.get("tk")
        if not token or token not in INSTRUMENTS:
            return

        inst   = INSTRUMENTS[token]
        symbol = inst["symbol"]

        _tick_fh.write(json.dumps(msg) + "\n")
        _tick_fh.flush()

        # Track best bid/ask (top of book) so orders are priced to cross the spread, not off
        # last-price. bid (bp1) and ask (sp1) arrive INDEPENDENTLY and either can be blank/0 in
        # a given 'tf' update — update each side only when it's a valid positive number and
        # carry the last good value forward.
        q = list(_quote.get(symbol, (0.0, 0.0)))
        for i, v in ((0, msg.get("bp1")), (1, msg.get("sp1"))):
            try:
                if v and float(v) > 0:
                    q[i] = float(v)
            except (TypeError, ValueError):
                pass
        if q[0] or q[1]:
            _quote[symbol] = (q[0], q[1])

        ft = msg.get("ft")
        if not ft:
            return
        lp = msg.get("lp") or msg.get("c")
        has_lp = bool(lp)
        if has_lp:
            try:
                price = float(lp)
            except (ValueError, OSError):
                return
            _last_price[symbol] = price
        else:
            # quote-only tick (bid/ask changed, price unchanged): use last known price so
            # retries/stops can still act on the fresh quote instead of waiting for a trade.
            price = _last_price.get(symbol)
            if price is None:
                return
        try:
            ts = datetime.fromtimestamp(int(ft), tz=IST)
        except (ValueError, OSError):
            return

        # Ignore stale snapshot ticks (e.g. Friday's close arriving on a Monday connect):
        # they must not build candles or trip the EOD branch.
        if ts.date() != datetime.now(tz=IST).date():
            return

        # ── Market-open gate ──────────────────────────────────────────────────
        # Never build candles or trade on pre-open ticks (09:00–09:15) or stale
        # snapshot ticks whose feed-time (ft) predates the open. The strategy must
        # only ever see real, regular-session bars. (Bug: without this, the 08:45
        # start built a phantom pre-open candle and fired an order at 09:00 that the
        # exchange rejected — seeding a phantom position.)
        if (ts.hour, ts.minute) < (MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE):
            return

        _last_tick[symbol] = datetime.now(tz=IST)

        # EOD
        if ts.hour > EOD_EXIT_HOUR or (ts.hour == EOD_EXIT_HOUR and ts.minute >= EOD_EXIT_MINUTE):
            t = _open_trades.get(symbol)
            if t:
                mode = inst.get("mode", "MIS")
                if mode == "CNC":
                    log.info("EOD  %-12s  [CNC] holding overnight  entry=Rs%.2f  qty=%d",
                             symbol, t["entry"], t["qty"])
                else:
                    # Verify the REAL position first — if the exchange is flat (never-filled
                    # entry, or already squared), clear the phantom and place NO order.
                    net = broker.net_position(symbol) if hasattr(broker, "net_position") else None
                    if net == 0:
                        log.warning("EOD %s — exchange net=0 (phantom/already flat); clearing, NO order", symbol)
                        _open_trades[symbol] = None
                        _save_runner_state()
                        return
                    # Aggressive EOD close via the confirm-before-book path; retries on cancel.
                    if not t.get("exiting"):
                        self._do_exit(symbol, price, "EOD", ts.strftime("%H:%M"))
            elif inst.get("mode", "MIS") == "MIS" and symbol not in _eod_orphan_done:
                # We think we're FLAT here — but reconcile against the exchange. An untracked
                # net position (e.g. a phantom 'cover' that actually opened a real position)
                # must be squared by US at EOD, not left for Flattrade's ~15:20 auto-square.
                net = broker.net_position(symbol) if hasattr(broker, "net_position") else None
                if net:   # non-zero and not None
                    _eod_orphan_done.add(symbol)
                    oside = "SELL" if net > 0 else "BUY"
                    log.warning("EOD ORPHAN  %s — exchange net=%d but ledger flat; squaring %s %d",
                                symbol, net, oside, abs(net))
                    broker.simulate_fill(symbol, oside, abs(net), price, "EOD orphan",
                                         quote=_quote.get(symbol), is_exit=True)
            mis_open = any(_open_trades.get(s) for s in MODES if MODES[s] == "MIS")
            if not _eod_done and not mis_open:
                _eod_done = True
                _eod_summary()
            _save_runner_state()
            return

        # ── Real-time stop management: evaluate stops on EVERY tick, not just candle close.
        # Entry/trend logic still runs only on closed candles (below).
        t = _open_trades.get(symbol)
        if t is not None and not t.get("exiting"):
            if not t.get("filled"):
                # entry not yet confirmed on the exchange: if a prior attempt was cancelled,
                # re-place on THIS fresh tick (fresh bid/ask, +1 tick). No stop checks until
                # we actually hold the position.
                if t.get("entry_armed"):
                    self._do_entry(symbol, price, t.get("reason", "entry retry"), t.get("ts_str", ""))
            elif t.get("exit_armed"):
                # a prior exit cancelled -> retry on EVERY tick (incl. quote-only) with the
                # freshest bid/ask, so we're not waiting for the next trade to get out
                self._do_exit(symbol, price, "retry", datetime.now(tz=IST).strftime("%H:%M"))
            elif has_lp:
                # a real stop check only matters when the price actually moved (lp present)
                exit_sig = inst["strategy"].check_stops(price)
                if exit_sig:
                    self._do_exit(symbol, exit_sig["price"], exit_sig.get("reason", ""),
                                  datetime.now(tz=IST).strftime("%H:%M"))

        if not has_lp:
            return   # quote-only tick: no new candle to build

        vol    = float(msg.get("v", 0) or 0)
        tick   = Tick(ts=ts, symbol=token, ltp=price, volume=vol, raw=msg)
        candle = inst["builder"].update(tick)
        if candle is None:
            return

        ts_str = candle.start.strftime("%H:%M")
        log.debug("CANDLE  %-12s  O=%.2f  H=%.2f  L=%.2f  C=%.2f",
                  symbol, candle.open, candle.high, candle.low, candle.close)

        strat = inst["strategy"]
        sigs  = strat.on_candle(candle)
        _log_strat_state(candle, symbol, strat, sigs[0] if sigs else None)
        for sig in sigs:
            action = sig["action"]
            px     = sig["price"]
            reason = sig.get("reason", "")

            if action in ("BUY", "SELL") and _open_trades[symbol] is None:
                if sum(1 for v in _open_trades.values() if v) >= MAX_POSITIONS:
                    continue
                lev  = 4 if inst.get("mode", "MIS") == "MIS" else 1
                qty  = max(1, int(CAPITAL_PER_TRADE * lev / px))
                _trade_no += 1
                _open_trades[symbol] = {"side": "LONG" if action == "BUY" else "SHORT",
                                        "entry": px, "qty": qty, "ts_str": ts_str,
                                        "filled": False, "entry_armed": False, "entry_tries": 0,
                                        "trade_no": _trade_no, "reason": reason}
                _entry_meta[_trade_no] = {"entry_time": ts_str}
                # place at the touch now; a cancel re-arms a fresh-quote +1-tick retry
                self._do_entry(symbol, px, reason, ts_str)

            elif action == "EXIT" and _open_trades[symbol] is not None:
                self._do_exit(symbol, px, reason, ts_str)

        _save_runner_state()

    def handle_order(self, msg: dict) -> None:
        rtype      = msg.get("reporttype", "")
        norenordno = msg.get("norenordno", "")
        tsym       = msg.get("tsym", "")

        if rtype.lower() in ("fill", "complete") and hasattr(broker, "pending"):
            p = broker.pending.pop(norenordno, None)
            if p:
                actual = float(msg.get("avgprc") or msg.get("flprc") or p["est"])
                # actual filled qty (Flattrade: fillshares/flqty); fall back to ordered qty
                try:
                    filled = int(float(msg.get("fillshares") or msg.get("flqty") or p["qty"]))
                except (TypeError, ValueError):
                    filled = p["qty"]
                filled = max(1, min(filled, p["qty"]))

                if p.get("kind") == "exit":
                    # Exit CONFIRMED filled -> now (and only now) book the trade at the real
                    # average fill price + actually-filled qty.
                    sym = p["symbol"]
                    t = _open_trades.get(sym)
                    if not t:
                        log.info("EXIT FILL %s but ledger already flat (avgprc=%.4f)", sym, actual)
                    elif filled < t.get("qty", filled):
                        booked = dict(t)
                        self._book_exit(sym, booked, actual, filled, p.get("reason", "") + " (partial)", p.get("exit_ts", ""))
                        t["qty"] = t["qty"] - filled
                        t["exiting"] = False; t["exit_armed"] = True
                        _open_trades[sym] = t
                        log.warning("PARTIAL EXIT %s filled %d, %d remain — retrying", sym, filled, t["qty"])
                    else:
                        self._book_exit(sym, t, actual, filled, p.get("reason", ""), p.get("exit_ts", ""))
                else:
                    slip = (actual - p["est"]) if p["side"] == "BUY" else (p["est"] - actual)
                    t = _open_trades.get(p["symbol"])
                    if t:
                        t["entry"] = actual
                        t["filled"] = True   # confirmed by the exchange -> real position
                        t["entry_armed"] = False
                        if filled < p["qty"]:
                            t["qty"] = filled
                            log.warning("PARTIAL ENTRY  %s  filled=%d/%d — position qty reduced to %d",
                                        p["symbol"], filled, p["qty"], filled)
                    log.info("FILL CONFIRMED  %-12s  %s  actual=Rs%.4f  est=Rs%.4f  slippage=Rs%+.4f",
                             p["symbol"], p["side"], actual, p["est"], slip)
            else:
                log.info("FILL  %s  norenordno=%s  avgprc=%s", tsym, norenordno, msg.get("avgprc"))
        elif rtype.lower() in ("rejected", "canceled", "cancelled"):
            log.warning("ORDER %s  %s  norenordno=%s  reason=%s",
                        rtype.upper(), tsym, norenordno, msg.get("rejreason", "unknown"))
            # A BUY entry we optimistically recorded but that was rejected OR an IOC that
            # cancelled without filling => undo the phantom. (A partial fill already popped
            # `pending` in the fill branch, so a later cancel finds nothing and is a no-op.)
            if hasattr(broker, "pending"):
                p = broker.pending.pop(norenordno, None)
                if p and p.get("kind") == "exit":
                    # An exit that didn't fill: DO NOT abandon the position or book it —
                    # re-arm so the next tick retries the exit (aggressively). Still holding.
                    sym = p["symbol"]
                    t = _open_trades.get(sym)
                    if t:
                        t["exiting"] = False
                        t["exit_armed"] = True
                        t["exit_tries"] = t.get("exit_tries", 0) + 1   # crosses 1 tick from the 2nd retry on
                        log.warning("EXIT UNFILLED  %s — %s; RETRY #%d (still holding, not booked)",
                                    sym, rtype, t["exit_tries"])
                        _save_runner_state()
                elif p:
                    # An ENTRY (BUY=long OR SELL=short) that cancelled/rejected without filling.
                    # RETRY it: re-arm so the next fresh tick re-places at the current bid/ask
                    # +1 tick (never the stale quote). Give up only after MAX_ENTRY_RETRIES,
                    # then clear the phantom (reset ledger + strategy + release margin).
                    sym = p["symbol"]
                    t = _open_trades.get(sym)
                    if t and not t.get("filled"):
                        if t.get("entry_tries", 0) < MAX_ENTRY_RETRIES:
                            t["entry_armed"] = True
                            t["entry_tries"] = t.get("entry_tries", 0) + 1
                            log.warning("ENTRY UNFILLED  %s — %s; RETRY #%d (fresh quote%s)",
                                        sym, rtype, t["entry_tries"],
                                        ", +1 tick" if t["entry_tries"] >= 2 else "")
                            _save_runner_state()
                        else:
                            _open_trades[sym] = None
                            log.warning("ENTRY ABANDONED  %s — %d cancels, no fill; ledger reset",
                                        sym, t.get("entry_tries", 0))
                            for inst in INSTRUMENTS.values():
                                if inst["symbol"] == sym:
                                    st = inst["strategy"]
                                    st.position = 0
                                    st._entry_price = None
                                    st._entry_atr = None
                                    break
                            if hasattr(broker, "_committed"):
                                margin = (p["qty"] * p["est"] / 4.0) if p.get("mode") == "MIS" else (p["qty"] * p["est"])
                                broker._committed = max(0.0, broker._committed - margin)
                            _save_runner_state()
        else:
            log.info("ORDER UPDATE  %-8s  %s  norenordno=%s", rtype, tsym, norenordno)


def main() -> None:
    global BASKET, MODES, EMA_PERIOD
    uid, token = get_session()
    client = FlattradeClient()
    client.set_session(user_id=uid, token=token)

    log.info("=" * 60)
    log.info("STARTUP  date=%s  pid=%d", _today, os.getpid())
    mode_label = "LIVE" if os.getenv("LIVE_MODE") == "1" else "PAPER"
    log.info("Strategy: Supertrend atr=14 mult=1.5 | 15-min | %s mode", mode_label)

    dyn = _load_today_basket()
    if dyn:
        BASKET, MODES = dyn
        EMA_PERIOD = EMA_PERIOD or 50   # match the backtested dynamic-MIS config
        log.info("DYNAMIC BASKET (%d): %s | EMA filter=%s",
                 len(BASKET), ",".join(s for _, s in BASKET), EMA_PERIOD)
    else:
        log.info("Using default fixed basket (%d stocks)", len(BASKET))

    log.info("Resolving NSE tokens...")
    resolve_tokens(client)
    _load_runner_state()
    _sync_positions_from_exchange(client)
    _align_strategy_from_trades()

    global broker
    if os.getenv("LIVE_MODE") == "1":
        tsym_map = {inst["symbol"]: inst["tsym"] for inst in INSTRUMENTS.values()}
        mode_map = {inst["symbol"]: inst.get("mode","MIS") for inst in INSTRUMENTS.values()}
        ti_map   = {inst["symbol"]: inst.get("ti") for inst in INSTRUMENTS.values()}
        broker   = LiveBroker(client, tsym_map, mode_map, ti_map)
        global CAPITAL_PER_TRADE
        try:
            limits = client.get_limits()
            cash = float(limits.get("cash", 0) or 0)
            deploy = WALLET_DEPLOY_FRAC * cash
            CAPITAL_PER_TRADE = max(1, min(MAX_CAPITAL_PER_STOCK, int(deploy / MAX_POSITIONS)))
            log.info("*** LIVE MODE  cash=Rs%.0f  deploy(85%%)=Rs%.0f  K=%d  cap/stock=Rs%d "
                     "(MIS notional 4x, CNC 1x) ***",
                     cash, deploy, MAX_POSITIONS, CAPITAL_PER_TRADE)
        except Exception as e:
            log.warning("Could not fetch limits (%s) — using Rs%d/trade", e, CAPITAL_PER_TRADE)

    if not INSTRUMENTS:
        log.error("No instruments resolved — exiting")
        sys.exit(1)

    _setup_options_logging(client)   # best-effort; never blocks trading

    scrip_keys = "#".join(f"{inst['exchange']}|{tok}" for tok, inst in INSTRUMENTS.items())

    threading.Thread(target=_heartbeat, daemon=True).start()

    app = TradingApp()

    def on_open(c: FlattradeClient) -> None:
        # Equity FIRST (so a WS subscription cap can never starve trading), then options.
        c.subscribe(scrip_keys, feed_type="t")
        c.subscribe_orders()
        if _opt_nfo_keys:
            c.subscribe("#".join(_opt_nfo_keys), feed_type=OPTION_FEED)
        if _opt_spot_keys:
            c.subscribe("#".join(_opt_spot_keys), feed_type="t")
        log.info("WS CONNECTED  subscribed=%d equity + %d option + %d spot",
                 len(INSTRUMENTS), len(_opt_nfo_keys), len(_opt_spot_keys))
        log.info("Ticks -> %s", _tick_path)

    def on_close(code=None, msg=None) -> None:
        log.warning("WS DISCONNECTED  code=%s  reason=%s  trades=%d  pnl=Rs%+.2f",
                    code, msg, _trade_no, _total_pnl)
        if _shutting_down:
            return
        try:
            _tick_fh.flush()
            _csv_fh.flush()
            _flush_options()
        except ValueError:
            pass   # files already closed during shutdown

    def on_error(e) -> None:
        if _shutting_down:
            return
        log.error("WS ERROR  %s", e)

    def _shutdown(sig, frame):
        global _shutting_down
        _shutting_down = True
        signame = "SIGINT" if sig == signal.SIGINT else "SIGTERM"
        log.info("%s received — saving state and shutting down", signame)
        _save_runner_state()
        _eod_summary()
        _flush_and_close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("%d stocks | Rs%d/trade | K=%d | EOD %02d:%02d IST",
             len(INSTRUMENTS), CAPITAL_PER_TRADE, MAX_POSITIONS, EOD_EXIT_HOUR, EOD_EXIT_MINUTE)
    log.info("=" * 60)

    client.start_websocket(
        on_tick=app.handle_tick,
        on_order=app.handle_order,
        on_open=on_open,
        on_close=on_close,
        on_error=on_error,
    )


if __name__ == "__main__":
    main()
