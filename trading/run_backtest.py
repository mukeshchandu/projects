#run_backtest.py
from __future__ import annotations
import os
from datetime import datetime, timedelta
from config import IST, NIFTY_TOKEN, BANKNIFTY_TOKEN
from backtest.engine import BacktestEngine
from backtest.report import print_report, print_summary

from strategies.orb                import ORBStrategy
from strategies.vwap_reversion     import VWAPReversionStrategy
from strategies.ema_crossover      import EMACrossoverStrategy
from strategies.gap_fill           import GapFillStrategy
from strategies.rsi_reversion      import RSIReversionStrategy
from strategies.supertrend         import SupertrendStrategy
from strategies.prev_day_breakout  import PrevDayBreakoutStrategy
from strategies.vwap_rsi           import VWAPRSIStrategy
from strategies.bollinger_reversion import BollingerReversionStrategy
from strategies.opening_momentum   import OpeningMomentumStrategy
from strategies.vwap_breakout      import VWAPBreakoutStrategy
from strategies.trend_pullback     import TrendPullbackStrategy
from strategies.donchian           import DonchianBreakoutStrategy
from strategies.keltner            import KeltnerBreakoutStrategy
from strategies.adx_trend          import ADXTrendStrategy

BACKTEST_END      = datetime.now(tz=IST)
BACKTEST_START    = BACKTEST_END - timedelta(days=59)
INITIAL_CAPITAL   = 15_000.0
MAX_TRADE_CAPITAL = 10_000.0
QTY = 1

# token "0" = placeholder (yfinance doesn't need it; only live WS needs real token)
INSTRUMENTS = [
    # ── Indices (keep for reference, expect losses) ──────────────────
    ("NSE", NIFTY_TOKEN,     "NIFTY",       1, 0.05, "fno_options",    "^NSEI"),
    ("NSE", BANKNIFTY_TOKEN, "BANKNIFTY",   1, 0.05, "fno_options",    "^NSEBANK"),
    # ── Original equities ────────────────────────────────────────────
    ("NSE", "14366",         "IDEA",        1, 0.01, "equity_intraday", "IDEA.NS"),
    ("NSE", "0",             "SUZLON",      1, 0.01, "equity_intraday", "SUZLON.NS"),
    ("NSE", "0",             "YESBANK",     1, 0.01, "equity_intraday", "YESBANK.NS"),
    # ── New equities ─────────────────────────────────────────────────
    ("NSE", "0",             "NHPC",        1, 0.01, "equity_intraday", "NHPC.NS"),
    ("NSE", "0",             "SAIL",        1, 0.01, "equity_intraday", "SAIL.NS"),
    ("NSE", "0",             "PNB",         1, 0.01, "equity_intraday", "PNB.NS"),
    ("NSE", "0",             "RPOWER",      1, 0.01, "equity_intraday", "RPOWER.NS"),
    ("NSE", "0",             "TATASTEEL",   1, 0.01, "equity_intraday", "TATASTEEL.NS"),
    ("NSE", "0",             "IDFCFIRSTB",  1, 0.01, "equity_intraday", "IDFCFIRSTB.NS"),
]


def build_strategies(name: str):
    return [
        (ORBStrategy(symbol=name, qty=QTY),                15),
        (VWAPReversionStrategy(symbol=name, qty=QTY),       5),
        (EMACrossoverStrategy(symbol=name, qty=QTY),        5),
        (GapFillStrategy(symbol=name, qty=QTY),            15),
        (RSIReversionStrategy(symbol=name, qty=QTY),       15),
        (SupertrendStrategy(symbol=name, qty=QTY),         15),
        (PrevDayBreakoutStrategy(symbol=name, qty=QTY),    15),
        (VWAPRSIStrategy(symbol=name, qty=QTY),             5),
        (BollingerReversionStrategy(symbol=name, qty=QTY),  5),
        (OpeningMomentumStrategy(symbol=name, qty=QTY),     5),
        (VWAPBreakoutStrategy(symbol=name, qty=QTY),       15),
        (TrendPullbackStrategy(symbol=name, qty=QTY),       5),
        (DonchianBreakoutStrategy(symbol=name, qty=QTY),   15),
        (KeltnerBreakoutStrategy(symbol=name, qty=QTY),    15),
        (ADXTrendStrategy(symbol=name, qty=QTY),           15),
    ]


def main() -> None:
    os.makedirs("logs", exist_ok=True)
    engine  = BacktestEngine()
    results = []

    for exchange, tok, name, lot_size, tick_size, segment, yf_ticker in INSTRUMENTS:
        print(f"\n{'━'*62}")
        print(f"  {name}  [{yf_ticker}]")
        print(f"  {BACKTEST_START.date()} → {BACKTEST_END.date()}")
        print(f"{'━'*62}")
        candle_cache: dict = {}

        for strategy, interval in build_strategies(name):
            if interval not in candle_cache:
                candle_cache[interval] = engine.fetch_candles(
                    exchange, tok, BACKTEST_START, BACKTEST_END,
                    interval=interval, yf_ticker=yf_ticker,
                )
            candles = candle_cache[interval]
            if not candles:
                print(f"  No data for {name} {interval}-min — skip")
                continue

            sname    = strategy.__class__.__name__
            log_path = f"logs/{name}_{sname}_{interval}m.log"
            print(f"  {sname} ({interval}-min) ...", end=" ", flush=True)

            result = engine.run(
                strategy=strategy,
                candles=candles,
                initial_capital=INITIAL_CAPITAL,
                tick_size=tick_size,
                segment=segment,
                lot_size=lot_size,
                interval=interval,
                log_path=log_path,
                max_trade_capital=MAX_TRADE_CAPITAL,
            )
            print_report(result)
            results.append(result)

    print_summary(results)


if __name__ == "__main__":
    main()
