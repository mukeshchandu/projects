#backtest/engine.py
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import IST
from marketdata import Candle
from strategies.base import BaseStrategy
from backtest.charges import calc_charges


def _round_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)


@dataclass
class Trade:
    entry_time:          datetime
    symbol:              str
    side:                str
    qty:                 int
    entry_price:         float
    entry_candle_ohlcv:  str
    entry_reason:        str = ""
    exit_time:           Optional[datetime] = None
    exit_price:          Optional[float]    = None
    exit_reason:         str = ""
    gross_pnl:           float = 0.0
    charges:             float = 0.0
    net_pnl:             float = 0.0

    def close(self, exit_time, exit_price, reason, tick_size, segment):
        self.exit_time  = exit_time
        self.exit_price = _round_tick(exit_price, tick_size)
        self.exit_reason = reason
        if self.side == "LONG":
            self.gross_pnl = (self.exit_price - self.entry_price) * self.qty
        else:
            self.gross_pnl = (self.entry_price - self.exit_price) * self.qty
        buy_val  = self.entry_price * self.qty if self.side == "LONG" else self.exit_price * self.qty
        sell_val = self.exit_price  * self.qty if self.side == "LONG" else self.entry_price * self.qty
        self.charges = calc_charges(segment, buy_val, sell_val)
        self.net_pnl = round(self.gross_pnl - self.charges, 4)


@dataclass
class BacktestResult:
    strategy_name:   str
    symbol:          str
    interval:        int
    segment:         str
    tick_size:       float
    lot_size:        int
    start_date:      str
    end_date:        str
    initial_capital: float
    trades:     List[Trade] = field(default_factory=list)
    log_lines:  List[str]   = field(default_factory=list)

    @property
    def total_pnl(self):      return sum(t.net_pnl for t in self.trades)
    @property
    def total_trades(self):   return len(self.trades)
    @property
    def winning_trades(self): return sum(1 for t in self.trades if t.net_pnl > 0)
    @property
    def win_rate(self):       return self.winning_trades / self.total_trades * 100 if self.total_trades else 0.0
    @property
    def avg_win(self):
        wins = [t.net_pnl for t in self.trades if t.net_pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0
    @property
    def avg_loss(self):
        losses = [t.net_pnl for t in self.trades if t.net_pnl <= 0]
        return sum(losses) / len(losses) if losses else 0.0
    @property
    def reward_risk(self): return abs(self.avg_win / self.avg_loss) if self.avg_loss != 0 else 0.0
    @property
    def max_drawdown(self):
        cap  = self.initial_capital
        peak = cap
        mdd  = 0.0
        for t in self.trades:
            cap += t.net_pnl
            peak = max(peak, cap)
            mdd  = max(mdd, (peak - cap) / peak * 100)
        return mdd
    @property
    def sharpe(self):
        if len(self.trades) < 2: return 0.0
        import statistics
        rets = [t.net_pnl / self.initial_capital for t in self.trades]
        std  = statistics.stdev(rets)
        return (statistics.mean(rets) / std * (252 ** 0.5)) if std > 0 else 0.0
    @property
    def best_trade(self):  return max(self.trades, key=lambda t: t.net_pnl) if self.trades else None
    @property
    def worst_trade(self): return min(self.trades, key=lambda t: t.net_pnl) if self.trades else None


class BacktestEngine:
    CHUNK_DAYS = 30

    def __init__(self, client=None):
        self.client = client  # optional — only needed for TPSeries fallback

    # ── Data fetching ─────────────────────────────────────────────────

    def fetch_candles(
        self,
        exchange: str,
        token: str,
        start_dt: datetime,
        end_dt: datetime,
        interval: int = 5,
        yf_ticker: str = "",
    ) -> List[Candle]:
        if yf_ticker:
            return self._fetch_yfinance(yf_ticker, start_dt, end_dt, interval)
        if self.client:
            return self._fetch_tpseries(exchange, token, start_dt, end_dt, interval)
        raise RuntimeError("Provide either yf_ticker or a Flattrade client with TPSeries access.")

    def _fetch_yfinance(
        self,
        ticker: str,
        start_dt: datetime,
        end_dt: datetime,
        interval: int,
    ) -> List[Candle]:
        try:
            import yfinance as yf
        except ImportError:
            raise RuntimeError("Run: pip install yfinance")

        interval_map = {1:"1m", 3:"5m", 5:"5m", 10:"15m", 15:"15m", 30:"30m", 60:"60m", 120:"60m"}
        yf_interval  = interval_map.get(interval, "15m")

        print(f"  yfinance {ticker}  {yf_interval}  {start_dt.date()} → {end_dt.date()} ...")

        raw = yf.download(
            ticker,
            start=start_dt,
            end=end_dt,
            interval=yf_interval,
            progress=False,
            auto_adjust=True,
        )

        if raw is None or raw.empty:
            print(f"  [yfinance] No data returned for {ticker}")
            return []

        # Flatten multi-level columns (yfinance ≥0.2.x wraps in (Price, Ticker))
        if hasattr(raw.columns, "levels"):
            raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]

        col = {c.lower(): c for c in raw.columns}

        candles: List[Candle] = []
        for ts, row in raw.iterrows():
            try:
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=IST)
                else:
                    dt = dt.astimezone(IST)
                c = Candle(
                    start=dt,
                    open=float(row[col["open"]]),
                    high=float(row[col["high"]]),
                    low=float(row[col["low"]]),
                    close=float(row[col["close"]]),
                    volume=float(row.get(col.get("volume", "Volume"), 0) or 0),
                )
                if c.close > 0:
                    candles.append(c)
            except Exception:
                continue

        print(f"  Got {len(candles)} candles")
        return candles

    def _fetch_tpseries(
        self,
        exchange: str,
        token: str,
        start_dt: datetime,
        end_dt: datetime,
        interval: int,
    ) -> List[Candle]:
        if not self.client:
            return []
        all_candles: List[Candle] = []
        chunk_start = start_dt
        chunk_num   = 0
        print(f"  TPSeries {exchange}|{token} {interval}-min {start_dt.date()} → {end_dt.date()} ...")
        while chunk_start < end_dt:
            chunk_end = min(chunk_start + timedelta(days=self.CHUNK_DAYS), end_dt)
            chunk_num += 1
            rows = self.client.get_time_price_series(exchange, token, chunk_start, chunk_end, interval)
            parsed = [c for r in rows for c in [self._parse_tpseries_row(r)] if c]
            all_candles.extend(parsed)
            print(f"    chunk {chunk_num}: {chunk_start.date()} → {chunk_end.date()}  rows={len(rows)}  parsed={len(parsed)}")
            chunk_start = chunk_end
            time.sleep(0.3)
        seen:   set         = set()
        unique: List[Candle] = []
        for c in sorted(all_candles, key=lambda x: x.start):
            key = c.start.isoformat()
            if key not in seen:
                seen.add(key)
                unique.append(c)
        print(f"  Total unique candles: {len(unique)}")
        return unique

    def _parse_tpseries_row(self, row: Dict[str, Any]) -> Optional[Candle]:
        try:
            ts  = datetime.strptime(row["time"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=IST)
            o   = float(row.get("into", 0) or 0)
            h   = float(row.get("inth", 0) or 0)
            l   = float(row.get("intl", 0) or 0)
            c   = float(row.get("intc", 0) or 0)
            vol = float(row.get("intv", 0) or 0)
            return Candle(start=ts, open=o, high=h, low=l, close=c, volume=vol) if c > 0 else None
        except Exception:
            return None

    # ── Backtest runner ───────────────────────────────────────────────

    def run(
        self,
        strategy: BaseStrategy,
        candles: List[Candle],
        initial_capital: float = 15_000.0,
        tick_size: float = 0.05,
        segment: str = "fno_options",
        lot_size: int = 1,
        interval: int = 5,
        log_path: Optional[str] = None,
        max_trade_capital: float = 0.0,
        slippage_ticks: float = 1.0,
    ) -> BacktestResult:
        strategy.reset()
        result = BacktestResult(
            strategy_name=strategy.__class__.__name__,
            symbol=strategy.symbol,
            interval=interval,
            segment=segment,
            tick_size=tick_size,
            lot_size=lot_size,
            start_date=candles[0].start.date().isoformat() if candles else "",
            end_date=candles[-1].start.date().isoformat()  if candles else "",
            initial_capital=initial_capital,
        )
        open_trade: Optional[Trade] = None
        log: List[str] = []

        for candle in candles:
            signals = strategy.on_candle(candle)
            for sig in signals:
                action = sig["action"]
                price  = _round_tick(sig["price"], tick_size)
                qty    = sig["qty"] * lot_size
                reason = sig.get("reason", "")
                ohlcv  = (f"O={candle.open:.2f} H={candle.high:.2f} "
                          f"L={candle.low:.2f} C={candle.close:.2f} V={candle.volume:.0f}")
                ts_str = candle.start.strftime("%Y-%m-%d %H:%M IST")

                if action == "BUY" and open_trade is None:
                    qty = (max(1, int(max_trade_capital / price))
                           if max_trade_capital > 0 else sig["qty"] * lot_size)
                    price = _round_tick(price + slippage_ticks * tick_size, tick_size)
                    open_trade = Trade(candle.start, sig["symbol"], "LONG", qty, price, ohlcv, reason)
                    log.append(f"\n[{ts_str}]  ENTRY LONG  @ {price:.2f}")
                    log.append(f"  Candle   : {ohlcv}")
                    log.append(f"  Reason   : {reason}")
                    log.append(f"  Qty      : {qty}  (lot_size={lot_size})")

                elif action == "SELL" and open_trade is None:
                    qty = (max(1, int(max_trade_capital / price))
                           if max_trade_capital > 0 else sig["qty"] * lot_size)
                    price = _round_tick(price - slippage_ticks * tick_size, tick_size)
                    open_trade = Trade(candle.start, sig["symbol"], "SHORT", qty, price, ohlcv, reason)
                    log.append(f"\n[{ts_str}]  ENTRY SHORT @ {price:.2f}")
                    log.append(f"  Candle   : {ohlcv}")
                    log.append(f"  Reason   : {reason}")
                    log.append(f"  Qty      : {qty}  (lot_size={lot_size})")

                elif action == "EXIT" and open_trade is not None:
                    if open_trade.side == "LONG":
                        price = _round_tick(price - slippage_ticks * tick_size, tick_size)
                    else:
                        price = _round_tick(price + slippage_ticks * tick_size, tick_size)
                    open_trade.close(candle.start, price, reason, tick_size, segment)
                    result.trades.append(open_trade)
                    pnl_str = (f"+₹{open_trade.net_pnl:.2f}" if open_trade.net_pnl >= 0
                               else f"-₹{abs(open_trade.net_pnl):.2f}")
                    log.append(f"[{ts_str}]  EXIT {open_trade.side}")
                    log.append(f"  Candle   : {ohlcv}")
                    log.append(f"  Exit @   : {open_trade.exit_price:.2f}  ({reason})")
                    log.append(f"  Entry @  : {open_trade.entry_price:.2f}  ({open_trade.entry_reason})")
                    log.append(f"  Gross    : ₹{open_trade.gross_pnl:.2f}")
                    log.append(f"  Charges  : ₹{open_trade.charges:.2f}  ({segment})")
                    log.append(f"  Net P&L  : {pnl_str}")
                    log.append("  " + "─" * 52)
                    open_trade = None

        if open_trade is not None and candles:
            last = candles[-1]
            open_trade.close(last.start, last.close, "backtest end", tick_size, segment)
            result.trades.append(open_trade)
            log.append(f"[BACKTEST END] force-close @ {last.close:.2f}  net={open_trade.net_pnl:.2f}")

        result.log_lines = log
        if log_path:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w") as f:
                f.write("\n".join(log))
        return result
