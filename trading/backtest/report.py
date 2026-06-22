#backtest/report.py
from __future__ import annotations

from typing import List

from backtest.engine import BacktestResult


def _pnl(v: float) -> str:
    return f"+₹{v:,.2f}" if v >= 0 else f"-₹{abs(v):,.2f}"


def print_report(result: BacktestResult) -> None:
    W = 60
    print(f"\n{'═' * W}")
    print(f"  {result.strategy_name}  |  {result.symbol}  |  {result.interval}-min")
    print(f"  {result.start_date} → {result.end_date}  |  segment: {result.segment}")
    print(f"{'─' * W}")
    print(f"  {'Total Trades':<26} {result.total_trades:>6}")
    print(f"  {'Win Rate':<26} {result.win_rate:>5.1f}%  ({result.winning_trades}W / {result.total_trades - result.winning_trades}L)")
    print(f"  {'Total Net P&L':<26} {_pnl(result.total_pnl):>12}")
    print(f"  {'Avg Win':<26} {_pnl(result.avg_win):>12}")
    print(f"  {'Avg Loss':<26} {_pnl(result.avg_loss):>12}")
    print(f"  {'Reward : Risk':<26} {result.reward_risk:>5.2f}")
    print(f"  {'Max Drawdown':<26} {result.max_drawdown:>5.1f}%")
    print(f"  {'Sharpe Ratio':<26} {result.sharpe:>5.2f}")
    print(f"  {'Tick Size':<26} {result.tick_size}")
    print(f"  {'Lot Size':<26} {result.lot_size}")

    if result.best_trade:
        bt = result.best_trade
        print(f"{'─' * W}")
        print(f"  Best  : {bt.entry_time.strftime('%d %b %Y')}  {bt.side:<5}  {_pnl(bt.net_pnl)}  | {bt.entry_reason[:35]}")
    if result.worst_trade:
        wt = result.worst_trade
        print(f"  Worst : {wt.entry_time.strftime('%d %b %Y')}  {wt.side:<5}  {_pnl(wt.net_pnl)}  | {wt.entry_reason[:35]}")

    print(f"{'═' * W}")


def print_summary(results: List[BacktestResult]) -> None:
    ranked = sorted(results, key=lambda r: r.total_pnl, reverse=True)
    W = 75
    print(f"\n{'═' * W}")
    print(f"  {'STRATEGY RANKING':^71}")
    print(f"{'─' * W}")
    print(f"  {'#':<3} {'Strategy':<26} {'Symbol':<12} {'Trades':>6} {'Win%':>6} {'P&L':>12} {'DD%':>6} {'Sharpe':>7}")
    print(f"{'─' * W}")
    for i, r in enumerate(ranked, 1):
        print(f"  {i:<3} {r.strategy_name:<26} {r.symbol:<12} "
              f"{r.total_trades:>6} {r.win_rate:>5.1f}% "
              f"{_pnl(r.total_pnl):>12} {r.max_drawdown:>5.1f}% {r.sharpe:>7.2f}")
    print(f"{'═' * W}")

    winner = ranked[0] if ranked else None
    if winner and winner.total_pnl > 0:
        print(f"\n  Recommended for live: {winner.strategy_name} on {winner.symbol}")
        print(f"  Run: python runner.py  (already set to {winner.strategy_name})")
    print()