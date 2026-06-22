#runner.py — paper trading IDEA with RSI Reversion
"""
Paper trade RSI Reversion on IDEA stock (15-min candles).
Run: python runner.py   (requires session token in .env)

What this does:
  - Subscribes to IDEA live ticks via Flattrade WebSocket
  - Builds 15-min candles and feeds them to RSIReversionStrategy
  - Logs every paper entry/exit with P&L to stdout
  - Records ALL ticks to data/YYYY-MM-DD/ticks.jsonl for future backtesting
  - Force-exits at EOD_EXIT_HOUR:EOD_EXIT_MINUTE (3:00 PM IST)

No real orders are placed. Run this for 2-4 weeks, then
backtest on the collected tick data with friend's PaperApi approach.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv("/home/ec2-user/projects/trading/.env")

from auth import get_session
from client import FlattradeClient
from marketdata import CandleBuilder, Tick
from strategies.rsi_reversion import RSIReversionStrategy
from config import IST, EOD_EXIT_HOUR, EOD_EXIT_MINUTE

# ── Instrument ────────────────────────────────────────────────────────
EXCHANGE    = "NSE"
TOKEN       = "14366"           # IDEA-EQ token
SYMBOL      = "IDEA"
INTERVAL_S  = 900               # 15-min = 900 seconds
MAX_CAPITAL = 10_000            # max ₹ deployed per paper trade

# ── Tick log (builds future backtest dataset) ─────────────────────────
_today     = datetime.now(tz=IST).strftime("%Y-%m-%d")
_tick_dir  = f"data/{_today}"
os.makedirs(_tick_dir, exist_ok=True)
_tick_path = f"{_tick_dir}/ticks.jsonl"
_tick_fh   = open(_tick_path, "a")

# ── Live state ────────────────────────────────────────────────────────
_strategy   = RSIReversionStrategy(symbol=SYMBOL, qty=1)
_builder    = CandleBuilder(interval_seconds=INTERVAL_S)
_paper_pnl  = 0.0
_open_trade = None    # {"side": str, "entry": float, "qty": int, "ts_str": str}
_trade_no   = 0


class TradingApp:
    def handle_tick(self, msg: dict) -> None:
        global _paper_pnl, _open_trade, _trade_no

        if msg.get("t") not in ("tk", "tf"):
            return

        # Record every tick — future backtests use this JSONL file
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

        # EOD: force-exit any open paper trade before market closes
        if ts.hour > EOD_EXIT_HOUR or (ts.hour == EOD_EXIT_HOUR and ts.minute >= EOD_EXIT_MINUTE):
            if _open_trade:
                t   = _open_trade
                pnl = ((price - t["entry"]) if t["side"] == "LONG" else (t["entry"] - price)) * t["qty"]
                _paper_pnl += pnl
                _open_trade = None
                print(f"\n[EOD] Force-exit #{_trade_no}  ₹{pnl:+.2f}  "
                      f"total=₹{_paper_pnl:+.2f}")
            return

        # Build candle from tick (volume field is cumulative-day from WS, tracked for VWAP later)
        vol  = float(msg.get("v", 0) or 0)
        tick = Tick(ts=ts, symbol=TOKEN, ltp=price, volume=vol, raw=msg)
        candle = _builder.update(tick)
        if candle is None:
            return

        ts_str = candle.start.strftime("%H:%M IST")
        print(f"\n[{ts_str}]  O={candle.open:.2f}  H={candle.high:.2f}  "
              f"L={candle.low:.2f}  C={candle.close:.2f}")

        for sig in _strategy.on_candle(candle):
            action = sig["action"]
            px     = sig["price"]

            if action == "BUY" and _open_trade is None:
                qty         = max(1, int(MAX_CAPITAL / px))
                _trade_no  += 1
                _open_trade = {"side": "LONG",  "entry": px, "qty": qty, "ts_str": ts_str}
                print(f"  [#{_trade_no}] LONG  @ ₹{px:.2f}  qty={qty}")
                print(f"  Reason: {sig.get('reason', '')}")

            elif action == "SELL" and _open_trade is None:
                qty         = max(1, int(MAX_CAPITAL / px))
                _trade_no  += 1
                _open_trade = {"side": "SHORT", "entry": px, "qty": qty, "ts_str": ts_str}
                print(f"  [#{_trade_no}] SHORT @ ₹{px:.2f}  qty={qty}")
                print(f"  Reason: {sig.get('reason', '')}")

            elif action == "EXIT" and _open_trade is not None:
                t   = _open_trade
                pnl = ((px - t["entry"]) if t["side"] == "LONG" else (t["entry"] - px)) * t["qty"]
                _paper_pnl += pnl
                _open_trade = None
                marker      = "✓" if pnl >= 0 else "✗"
                print(f"  [#{_trade_no}] EXIT {marker}  ₹{pnl:+.2f}  "
                      f"entry=₹{t['entry']:.2f}@{t['ts_str']}  exit=₹{px:.2f}  "
                      f"total=₹{_paper_pnl:+.2f}")
                print(f"  Reason: {sig.get('reason', '')}")

    def handle_order(self, msg: dict) -> None:
        print(f"  [ORDER] {msg.get('reporttype')} {msg.get('tsym')} {msg.get('norenordno', '')}")


def main() -> None:
    uid, token = get_session()
    client = FlattradeClient()
    client.set_session(user_id=uid, token=token)
    app = TradingApp()

    def on_open(c: FlattradeClient) -> None:
        c.subscribe(f"{EXCHANGE}|{TOKEN}", feed_type="t")
        print(f"[WS] Subscribed {EXCHANGE}|{TOKEN} ({SYMBOL}) touchline")
        print(f"[WS] Ticks → {_tick_path}")

    def on_close() -> None:
        print(f"\n[WS] Closed.  trades={_trade_no}  paper_pnl=₹{_paper_pnl:+.2f}")
        _tick_fh.close()

    def _on_sigint(sig, frame):
        print(f"\n[CTRL-C]  trades={_trade_no}  paper_pnl=₹{_paper_pnl:+.2f}")
        _tick_fh.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_sigint)

    print(f"[RUNNER] {SYMBOL}  RSIReversionStrategy  15-min  paper mode")
    print(f"[RUNNER] Capital cap ₹{MAX_CAPITAL:,}  |  EOD {EOD_EXIT_HOUR}:{EOD_EXIT_MINUTE:02d} IST")

    client.start_websocket(
        on_tick=app.handle_tick,
        on_order=app.handle_order,
        on_open=on_open,
        on_close=on_close,
        on_error=lambda e: print(f"[WS ERROR] {e}"),
    )


if __name__ == "__main__":
    main()
