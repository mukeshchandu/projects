#!/usr/bin/env python3
"""
Phase-ensemble backtest: run N phase-shifted 15-min Supertrends per stock (offsets
0..N-1 min). ONE position per stock, always-in-market reversal: enter on the earliest
phase's trend-FLIP event, reverse when ANY phase flips the opposite way. Compares a
single grid (1 phase) vs the 15-phase ensemble on captured tick data (minute resolution).
MIS, Rs5000/trade (1x), full Flattrade cost model. Mac-only research tool.
"""
from __future__ import annotations
import os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fill_price, round_trip_cost, is_eod
from bt_ticks import load_ticks, TOKENS

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

INTERVAL = 900


class PhasedBuilder:
    """15-min OHLC builder whose bucket boundaries are shifted by `offset_min` minutes."""
    def __init__(self, offset_min):
        self.off = offset_min * 60
        self.cur = None

    def update(self, ft, price):
        b = ((ft - self.off) // INTERVAL) * INTERVAL + self.off
        if self.cur is None:
            self.cur = [b, price, price, price, price]
            return None
        if b > self.cur[0]:
            fin = self.cur
            self.cur = [b, price, price, price, price]
            return fin
        self.cur[2] = max(self.cur[2], price)
        self.cur[3] = min(self.cur[3], price)
        self.cur[4] = price
        return None


def run(ticks_by_sym, n_phases, reverse=True, capital=5000):
    trades = []
    for tok, (sym, mode) in TOKENS.items():
        seq = ticks_by_sym.get(sym, [])
        if not seq:
            continue
        builders = [PhasedBuilder(k) for k in range(n_phases)]
        strats   = [SupertrendStrategy(f"{sym}_{k}", 1, multiplier=1.5) for k in range(n_phases)]
        last_tr  = [0] * n_phases
        pos = None
        prev_eod = False

        def close(px):
            nonlocal pos
            if pos is None:
                return
            xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
            q = pos["qty"]
            g = (xf - pos["ef"]) * q if pos["side"] == "LONG" else (pos["ef"] - xf) * q
            trades.append({"sym": sym, "net": g - round_trip_cost(pos["ef"]*q, xf*q, "MIS")})
            pos = None

        def openp(side, px):
            nonlocal pos
            ef = fill_price("BUY" if side == "LONG" else "SELL", px)
            pos = {"side": side, "ef": ef, "qty": max(1, int(capital / ef))}

        for ft, price in seq:
            ts = datetime.fromtimestamp(ft, tz=IST)
            if is_eod(ts):
                if pos is not None and not prev_eod:
                    close(price)          # MIS squares at 15:00
                prev_eod = True
                continue
            prev_eod = False
            for i, b in enumerate(builders):
                fin = b.update(ft, price)
                if fin is None:
                    continue
                cdt = datetime.fromtimestamp(fin[0], tz=IST)
                strats[i].on_candle(Candle(start=cdt, open=fin[1], high=fin[2], low=fin[3], close=fin[4]))
                nt = strats[i]._trend
                if nt != last_tr[i] and nt != 0 and last_tr[i] != 0:
                    # genuine flip event on phase i
                    if reverse:
                        # always-in-market: bull->long, bear->short
                        if nt == 1 and (pos is None or pos["side"] != "LONG"):
                            close(price); openp("LONG", price)
                        elif nt == -1 and (pos is None or pos["side"] != "SHORT"):
                            close(price); openp("SHORT", price)
                    else:
                        # long-only: enter long on bull, exit to FLAT on bear (never short)
                        if nt == 1 and pos is None:
                            openp("LONG", price)
                        elif nt == -1 and pos is not None:
                            close(price)
                last_tr[i] = nt
        if pos is not None:
            close(seq[-1][1])
    return trades


def summ(trades):
    n = len(trades)
    if n == 0:
        return "no trades"
    net = sum(t["net"] for t in trades)
    win = sum(1 for t in trades if t["net"] > 0) / n * 100
    return f"trades={n:4d}  win={win:4.0f}%  NET=Rs{net:+9.1f}  avg=Rs{net/n:+6.1f}/trade"


def main():
    print("Loading tick data (minute resolution)...")
    tk = load_ticks()
    print(f"  {sum(len(v) for v in tk.values()):,} ticks | 8 stocks | 14 sessions\n")
    print("MIS, Rs5000/trade. REVERSAL (always-in: long<->short) vs LONG-ONLY (flat between):\n")
    print(f"  {'phases':>7} | {'REVERSAL (always-in)':>34} | {'LONG-ONLY (flat between)':>34}")
    print("  " + "-" * 80)
    for n in (1, 3, 15):
        rev = summ(run(tk, n, reverse=True))
        lo  = summ(run(tk, n, reverse=False))
        print(f"  {n:>7} | {rev:>34} | {lo:>34}")
    print("\nLong-only should ~halve trades (no short legs) and sit out downtrends.")


if __name__ == "__main__":
    main()
