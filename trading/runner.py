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
from config import IST, EOD_EXIT_HOUR, EOD_EXIT_MINUTE

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
_eod_done     = False
_shutting_down = False
_runner_state_path = "data/runner_state.json"

# ── Tick file (raw) ──────────────────────────────────────────────────────
_tick_path = f"data/{_today}/ticks.jsonl"
_tick_fh   = open(_tick_path, "a")

# ── Trade CSV ────────────────────────────────────────────────────────────
_csv_path = f"logs/trades_{_today}.csv"
_csv_fh   = open(_csv_path, "a")
if os.path.getsize(_csv_path) == 0:
    _csv_fh.write("trade_no,symbol,side,entry_time,entry_price,exit_time,exit_price,qty,pnl\n")
    _csv_fh.flush()

_entry_meta: Dict[int, dict] = {}   # trade_no → {entry_time}


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
        for r in results:
            ts = r.get("tsym", "")
            if ts in (f"{sym}-EQ", sym):
                token = r.get("token")
                tsym  = ts
                break
        if not token and results:
            token = results[0].get("token")
            tsym  = results[0].get("tsym", f"{sym}-EQ")
        if not tsym:
            tsym = f"{sym}-EQ"
        if not token:
            log.warning("No token for %s — skipping", sym)
            continue
        INSTRUMENTS[token] = {
            "symbol":   sym,
            "exchange": exch,
            "tsym":     tsym,
            "mode":     MODES.get(sym, "MIS"),
            "strategy": SupertrendStrategy(symbol=sym, qty=1,
                                           long_only=(MODES.get(sym, "MIS") == "CNC"),
                                           ema_period=EMA_PERIOD),
            "builder":  CandleBuilder(interval_seconds=INTERVAL_S),
        }
        _open_trades[sym] = None
        log.info("Resolved  %-15s  token=%s", sym, token)


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


def _flush_and_close() -> None:
    _tick_fh.flush()
    _tick_fh.close()
    _csv_fh.flush()
    _csv_fh.close()


class TradingApp:
    def _do_exit(self, symbol: str, px: float, reason: str, ts_str: str) -> bool:
        """Close the open position for `symbol` at ~px. Shared by candle-close and
        tick-level (real-time) stop paths. Returns True if a fill happened."""
        global _total_pnl
        t = _open_trades.get(symbol)
        if not t:
            return False
        side = "SELL" if t["side"] == "LONG" else "BUY"
        fill = broker.simulate_fill(symbol, side, t["qty"], px, reason)
        if fill is None:
            return False
        pnl = ((fill.price - t["entry"]) if t["side"] == "LONG"
               else (t["entry"] - fill.price)) * t["qty"]
        _total_pnl += pnl
        _open_trades[symbol] = None
        result = "WIN " if pnl >= 0 else "LOSS"
        log.info("EXIT  #%-3d  %-12s  %s  Rs%+.2f  entry=Rs%.2f@%s  exit=Rs%.2f  total=Rs%+.2f  [%s]",
                 _trade_no, symbol, result, pnl, t["entry"], t["ts_str"], fill.price, _total_pnl, reason)
        meta = _entry_meta.get(_trade_no, {})
        _csv_fh.write(f"{_trade_no},{symbol},{t['side']},"
                      f"{meta.get('entry_time','')},"
                      f"{t['entry']:.2f},{ts_str},"
                      f"{fill.price:.2f},{t['qty']},{pnl:.2f}\n")
        _csv_fh.flush()
        _save_runner_state()
        return True

    def handle_tick(self, msg: dict) -> None:
        global _trade_no, _total_pnl, _eod_done

        if msg.get("t") not in ("tk", "tf"):
            return

        token = msg.get("tk")
        if not token or token not in INSTRUMENTS:
            return

        inst   = INSTRUMENTS[token]
        symbol = inst["symbol"]

        _tick_fh.write(json.dumps(msg) + "\n")
        _tick_fh.flush()

        lp = msg.get("lp") or msg.get("c")
        ft = msg.get("ft")
        if not lp or not ft:
            return
        try:
            price = float(lp)
            ts    = datetime.fromtimestamp(int(ft), tz=IST)
        except (ValueError, OSError):
            return

        # Ignore stale snapshot ticks (e.g. Friday's close arriving on a Monday connect):
        # they must not build candles or trip the EOD branch.
        if ts.date() != datetime.now(tz=IST).date():
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
                    exit_side = "SELL" if t["side"] == "LONG" else "BUY"
                    fill = broker.simulate_fill(symbol, exit_side, t["qty"], price, "EOD")
                    if fill is None:
                        _open_trades[symbol] = None
                        _save_runner_state()
                        log.error("EOD ORDER REJECTED  %s", symbol)
                        return
                    pnl = ((fill.price - t["entry"]) if t["side"] == "LONG" else (t["entry"] - fill.price)) * t["qty"]
                    _total_pnl += pnl
                    _open_trades[symbol] = None
                    log.info("EOD EXIT  %-12s  Rs%+.2f  total=Rs%+.2f", symbol, pnl, _total_pnl)
                    meta = _entry_meta.get(_trade_no, {})
                    _csv_fh.write(f"{_trade_no},{symbol},{t['side']},"
                                  f"{meta.get('entry_time','')},"
                                  f"{t['entry']:.2f},{ts.strftime('%H:%M')},"
                                  f"{fill.price:.2f},{t['qty']},{pnl:.2f}\n")
                    _csv_fh.flush()
            mis_open = any(_open_trades.get(s) for s in MODES if MODES[s] == "MIS")
            if not _eod_done and not mis_open:
                _eod_done = True
                _eod_summary()
            _save_runner_state()
            return

        # ── Real-time stop management: evaluate stops on EVERY tick, not just candle close.
        # Entry/trend logic still runs only on closed candles (below).
        t = _open_trades.get(symbol)
        if t is not None:
            exit_sig = inst["strategy"].check_stops(price)
            if exit_sig:
                self._do_exit(symbol, exit_sig["price"], exit_sig.get("reason", ""),
                              datetime.now(tz=IST).strftime("%H:%M"))

        vol    = float(msg.get("v", 0) or 0)
        tick   = Tick(ts=ts, symbol=token, ltp=price, volume=vol, raw=msg)
        candle = inst["builder"].update(tick)
        if candle is None:
            return

        ts_str = candle.start.strftime("%H:%M")
        log.debug("CANDLE  %-12s  O=%.2f  H=%.2f  L=%.2f  C=%.2f",
                  symbol, candle.open, candle.high, candle.low, candle.close)

        for sig in inst["strategy"].on_candle(candle):
            action = sig["action"]
            px     = sig["price"]
            reason = sig.get("reason", "")

            if action == "BUY" and _open_trades[symbol] is None:
                if sum(1 for v in _open_trades.values() if v) >= MAX_POSITIONS:
                    continue
                lev  = 4 if inst.get("mode", "MIS") == "MIS" else 1
                qty  = max(1, int(CAPITAL_PER_TRADE * lev / px))
                fill = broker.simulate_fill(symbol, "BUY", qty, px, reason)
                if fill is None:
                    continue
                _trade_no += 1
                _open_trades[symbol] = {"side": "LONG", "entry": fill.price, "qty": qty, "ts_str": ts_str}
                _entry_meta[_trade_no] = {"entry_time": ts_str}
                log.info("ENTRY #%-3d  %-12s  LONG   Rs%.2f  qty=%d  [%s]",
                         _trade_no, symbol, fill.price, qty, reason)

            elif action == "SELL" and _open_trades[symbol] is None:
                if sum(1 for v in _open_trades.values() if v) >= MAX_POSITIONS:
                    continue
                lev  = 4 if inst.get("mode", "MIS") == "MIS" else 1
                qty  = max(1, int(CAPITAL_PER_TRADE * lev / px))
                fill = broker.simulate_fill(symbol, "SELL", qty, px, reason)
                if fill is None:
                    continue
                _trade_no += 1
                _open_trades[symbol] = {"side": "SHORT", "entry": fill.price, "qty": qty, "ts_str": ts_str}
                _entry_meta[_trade_no] = {"entry_time": ts_str}
                log.info("ENTRY #%-3d  %-12s  SHORT  Rs%.2f  qty=%d  [%s]",
                         _trade_no, symbol, fill.price, qty, reason)

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
                slip   = (actual - p["est"]) if p["side"] == "BUY" else (p["est"] - actual)
                log.info("FILL CONFIRMED  %-12s  %s  actual=Rs%.4f  est=Rs%.4f  slippage=Rs%+.4f",
                         p["symbol"], p["side"], actual, p["est"], slip)
                # Update open trade entry price to actual fill
                t = _open_trades.get(p["symbol"])
                if t:
                    t["entry"] = actual
            else:
                log.info("FILL  %s  norenordno=%s  avgprc=%s", tsym, norenordno, msg.get("avgprc"))
        elif rtype.lower() == "rejected":
            log.error("ORDER REJECTED  %s  norenordno=%s  reason=%s",
                      tsym, norenordno, msg.get("rejreason", "unknown"))
            # If a BUY entry we optimistically recorded is rejected async, undo the phantom.
            if hasattr(broker, "pending"):
                p = broker.pending.pop(norenordno, None)
                if p and p["side"].upper() == "BUY":
                    sym = p["symbol"]
                    if _open_trades.get(sym):
                        _open_trades[sym] = None
                        log.warning("PHANTOM CLEARED  %s — entry rejected, ledger reset", sym)
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

    global broker, MAX_CAPITAL_PER_STOCK
    if os.getenv("LIVE_MODE") == "1":
        tsym_map = {inst["symbol"]: inst["tsym"] for inst in INSTRUMENTS.values()}
        mode_map = {inst["symbol"]: inst.get("mode","MIS") for inst in INSTRUMENTS.values()}
        broker   = LiveBroker(client, tsym_map, mode_map)
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

    scrip_keys = "#".join(f"{inst['exchange']}|{tok}" for tok, inst in INSTRUMENTS.items())

    threading.Thread(target=_heartbeat, daemon=True).start()

    app = TradingApp()

    def on_open(c: FlattradeClient) -> None:
        c.subscribe(scrip_keys, feed_type="t")
        c.subscribe_orders()
        log.info("WS CONNECTED  subscribed=%d instruments", len(INSTRUMENTS))
        log.info("Ticks -> %s", _tick_path)

    def on_close(code=None, msg=None) -> None:
        log.warning("WS DISCONNECTED  code=%s  reason=%s  trades=%d  pnl=Rs%+.2f",
                    code, msg, _trade_no, _total_pnl)
        if _shutting_down:
            return
        try:
            _tick_fh.flush()
            _csv_fh.flush()
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
