#sweep_supertrend.py — grid-search atr_period x multiplier across all equities
from __future__ import annotations
import os
from datetime import datetime, timedelta
from config import IST
from backtest.engine import BacktestEngine
from strategies.supertrend import SupertrendStrategy

BACKTEST_END      = datetime.now(tz=IST)
BACKTEST_START    = BACKTEST_END - timedelta(days=59)
INITIAL_CAPITAL   = 15_000.0
MAX_TRADE_CAPITAL = 10_000.0
INTERVAL          = 15
SLIP              = float(os.environ.get("SLIP", "1"))

# Equities only — indices lose, no point sweeping them
INSTRUMENTS = [
    ("IDEA","IDEA.NS"), ("SUZLON","SUZLON.NS"), ("YESBANK","YESBANK.NS"),
    ("NHPC","NHPC.NS"), ("SAIL","SAIL.NS"), ("PNB","PNB.NS"),
    ("RPOWER","RPOWER.NS"), ("TATASTEEL","TATASTEEL.NS"), ("IDFCFIRSTB","IDFCFIRSTB.NS"),
]
ATR_PERIODS = [5, 7, 9, 14]
MULTIPLIERS = [1.5, 2.0, 2.5, 3.0]


def metric(r, *names, default=0.0):
    for n in names:
        if isinstance(r, dict):
            if n in r:
                return r[n]
        elif hasattr(r, n):
            return getattr(r, n)
    return default


def main() -> None:
    os.makedirs("logs", exist_ok=True)
    engine = BacktestEngine()

    print(f"=== SLIPPAGE = {SLIP:.0f} tick(s)/side ===")
    print("Fetching candles (once per instrument)...")
    candles_by = {}
    for name, yf in INSTRUMENTS:
        candles_by[name] = engine.fetch_candles("NSE", "0", BACKTEST_START, BACKTEST_END,
                                                 interval=INTERVAL, yf_ticker=yf)

    introspected = False
    agg = {}  # (atr,mult) -> (total_pnl, sum_sharpe, rows)

    for ap in ATR_PERIODS:
        for mult in MULTIPLIERS:
            total_pnl, sum_sharpe, rows = 0.0, 0.0, []
            for name, yf in INSTRUMENTS:
                candles = candles_by[name]
                if not candles:
                    continue
                strat = SupertrendStrategy(symbol=name, qty=1, atr_period=ap, multiplier=mult)
                r = engine.run(strategy=strat, candles=candles, initial_capital=INITIAL_CAPITAL,
                               tick_size=0.01, segment="equity_intraday", lot_size=1,
                               interval=INTERVAL, log_path=f"logs/sweep_{name}_{ap}_{mult}.log",
                               max_trade_capital=MAX_TRADE_CAPITAL, slippage_ticks=SLIP)
                if not introspected:
                    keys = list(r.__dict__.keys()) if hasattr(r, "__dict__") else (list(r.keys()) if isinstance(r, dict) else dir(r))
                    print(f"[result schema] {keys}\n")
                    introspected = True
                pnl = metric(r, "net_pnl", "pnl", "total_pnl", "total_net_pnl")
                shp = metric(r, "sharpe", "sharpe_ratio")
                trd = metric(r, "num_trades", "total_trades", "n_trades", "trades")
                if isinstance(trd, list):
                    trd = len(trd)
                wr = metric(r, "win_rate", "winrate", "win_pct")
                if wr and wr <= 1.0:
                    wr *= 100.0
                total_pnl  += pnl
                sum_sharpe += shp
                rows.append((name, pnl, shp, trd, wr))
            agg[(ap, mult)] = (total_pnl, sum_sharpe, rows)
            print(f"atr={ap:2d} mult={mult:.1f}   total_pnl=Rs{total_pnl:9.0f}   sum_sharpe={sum_sharpe:6.1f}")

    print("\n" + "=" * 60)
    print("TOP COMBOS BY TOTAL P&L (across 9 equities)")
    print("=" * 60)
    by_pnl = sorted(agg.items(), key=lambda x: x[1][0], reverse=True)
    for (ap, mult), (tp, ss, _) in by_pnl[:8]:
        print(f"  atr={ap:2d} mult={mult:.1f}   P&L=Rs{tp:9.0f}   SumSharpe={ss:6.1f}")

    print("\n" + "=" * 60)
    print("TOP COMBOS BY SUM SHARPE (risk-adjusted, more robust)")
    print("=" * 60)
    by_shp = sorted(agg.items(), key=lambda x: x[1][1], reverse=True)
    for (ap, mult), (tp, ss, _) in by_shp[:8]:
        print(f"  atr={ap:2d} mult={mult:.1f}   SumSharpe={ss:6.1f}   P&L=Rs{tp:9.0f}")

    best_key = by_shp[0][0]
    tp, ss, rows = agg[best_key]
    print("\n" + "=" * 60)
    print(f"BEST (by Sharpe): atr={best_key[0]} mult={best_key[1]} — per stock")
    print("=" * 60)
    for name, pnl, shp, trd, wr in sorted(rows, key=lambda x: -x[1]):
        print(f"  {name:12s} P&L=Rs{pnl:8.0f}  Sharpe={shp:5.1f}  trades={trd:3.0f}  win={wr:4.0f}%")


if __name__ == "__main__":
    main()
