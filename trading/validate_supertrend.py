#validate_supertrend.py — train/test out-of-sample validation
from __future__ import annotations
from datetime import datetime, timedelta
from config import IST
from backtest.engine import BacktestEngine
from strategies.supertrend import SupertrendStrategy

BACKTEST_END      = datetime.now(tz=IST)
BACKTEST_START    = BACKTEST_END - timedelta(days=59)
CUTOFF            = BACKTEST_START + timedelta(days=40)   # first 40 days train, last 19 test
INITIAL_CAPITAL   = 15_000.0
MAX_TRADE_CAPITAL = 10_000.0
INTERVAL          = 15
CHOSEN_ATR, CHOSEN_MULT = 14, 1.5
ATR_GRID  = [5, 7, 9, 14]
MULT_GRID = [1.5, 2.0, 2.5, 3.0]

INSTRUMENTS = [
    ("IDEA","IDEA.NS"), ("SUZLON","SUZLON.NS"), ("YESBANK","YESBANK.NS"),
    ("NHPC","NHPC.NS"), ("SAIL","SAIL.NS"), ("PNB","PNB.NS"),
    ("RPOWER","RPOWER.NS"), ("TATASTEEL","TATASTEEL.NS"), ("IDFCFIRSTB","IDFCFIRSTB.NS"),
]


def metrics(engine, name, candles, atr, mult):
    if not candles:
        return {"n": 0, "wins": 0, "pnl": 0.0, "sharpe": 0.0}
    s = SupertrendStrategy(symbol=name, qty=1, atr_period=atr, multiplier=mult)
    r = engine.run(strategy=s, candles=candles, initial_capital=INITIAL_CAPITAL,
                   tick_size=0.01, segment="equity_intraday", lot_size=1,
                   interval=INTERVAL, log_path=None, max_trade_capital=MAX_TRADE_CAPITAL)
    ts = list(getattr(r, "trades", []) or [])
    n = len(ts)
    wins = sum(1 for t in ts if getattr(t, "net_pnl", 0) > 0)
    pnl  = sum(getattr(t, "net_pnl", 0) for t in ts)
    return {"n": n, "wins": wins, "pnl": pnl, "sharpe": getattr(r, "sharpe", 0.0)}


def agg(rows):
    n    = sum(r["n"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    pnl  = sum(r["pnl"] for r in rows)
    return n, wins, pnl, (wins / n * 100 if n else 0), (pnl / n if n else 0)


def main():
    engine = BacktestEngine()
    print("Fetching + splitting...")
    data = {}
    for name, yf in INSTRUMENTS:
        c = engine.fetch_candles("NSE", "0", BACKTEST_START, BACKTEST_END,
                                 interval=INTERVAL, yf_ticker=yf)
        data[name] = ([x for x in c if x.start < CUTOFF],
                      [x for x in c if x.start >= CUTOFF])

    tr0, te0 = data[INSTRUMENTS[0][0]]
    print("\n" + "=" * 66)
    print("TRAIN/TEST VALIDATION — Supertrend (cost + slippage aware)")
    if tr0 and te0:
        print(f"  Train: {tr0[0].start.date()} -> {tr0[-1].start.date()}  "
              f"({len({x.start.date() for x in tr0})} trading days)")
        print(f"  Test : {te0[0].start.date()} -> {te0[-1].start.date()}  "
              f"({len({x.start.date() for x in te0})} trading days, UNSEEN)")
    print("=" * 66)

    # PHASE 1 — re-optimize on TRAIN only
    print("\nPHASE 1 — Re-optimize on TRAIN only (does it rediscover the config?)")
    best = None
    for atr in ATR_GRID:
        for mult in MULT_GRID:
            ssh = sum(metrics(engine, name, data[name][0], atr, mult)["sharpe"]
                      for name, _ in INSTRUMENTS)
            if best is None or ssh > best[2]:
                best = (atr, mult, ssh)
    print(f"  TRAIN-optimal (by SumSharpe): atr={best[0]}  mult={best[1]}")
    print(f"  Pre-chosen config           : atr={CHOSEN_ATR}  mult={CHOSEN_MULT}")
    if best[0] == CHOSEN_ATR and best[1] == CHOSEN_MULT:
        print("  -> MATCH (config is stable, not cherry-picked)")
    elif best[1] == CHOSEN_MULT:
        print("  -> SAME MULTIPLIER (atr differs — low impact, config stable)")
    else:
        print("  -> DIVERGED (train picks a different config — investigate)")

    configs = [("chosen", CHOSEN_ATR, CHOSEN_MULT)]
    if not (best[0] == CHOSEN_ATR and best[1] == CHOSEN_MULT):
        configs.append(("train-optimal", best[0], best[1]))

    # PHASE 2 — apply config to train vs unseen test
    for label, atr, mult in configs:
        print("\n" + "-" * 66)
        print(f"PHASE 2 — atr={atr} mult={mult} ({label}) :  TRAIN  vs  TEST (unseen)")
        print("-" * 66)
        print(f"  {'Stock':11s} {'trTrd':>5s} {'trWin':>5s} {'tr/t':>7s}   "
              f"{'teTrd':>5s} {'teWin':>5s} {'te/t':>7s}")
        tr_rows, te_rows = [], []
        for name, _ in INSTRUMENTS:
            trm = metrics(engine, name, data[name][0], atr, mult)
            tem = metrics(engine, name, data[name][1], atr, mult)
            tr_rows.append(trm); te_rows.append(tem)
            trw = trm["wins"] / trm["n"] * 100 if trm["n"] else 0
            tew = tem["wins"] / tem["n"] * 100 if tem["n"] else 0
            tre = trm["pnl"] / trm["n"] if trm["n"] else 0
            tee = tem["pnl"] / tem["n"] if tem["n"] else 0
            print(f"  {name:11s} {trm['n']:5d} {trw:4.0f}% {tre:+7.1f}   "
                  f"{tem['n']:5d} {tew:4.0f}% {tee:+7.1f}")
        tn, tw, tp, twr, tex = agg(tr_rows)
        en, ew, ep, ewr, eex = agg(te_rows)
        print("  " + "-" * 62)
        print(f"  {'AGGREGATE':11s} {tn:5d} {twr:4.0f}% {tex:+7.1f}   "
              f"{en:5d} {ewr:4.0f}% {eex:+7.1f}")
        print(f"\n  Train: {tn} trades, {twr:.0f}% win, Rs{tex:+.1f}/trade, total Rs{tp:+.0f}")
        print(f"  Test : {en} trades, {ewr:.0f}% win, Rs{eex:+.1f}/trade, total Rs{ep:+.0f}")
        ratio = (eex / tex * 100) if tex else 0
        print(f"\n  >> Test expectancy = {ratio:.0f}% of train  |  win-rate delta = {ewr-twr:+.0f}pp")
        if eex > 0 and ratio >= 40 and abs(ewr - twr) <= 12:
            print("  >> VERDICT: PASS — edge holds out-of-sample, overfit risk low.")
        elif eex > 0:
            print("  >> VERDICT: SOFT PASS — still profitable but degraded; watch in paper.")
        else:
            print("  >> VERDICT: FAIL — edge vanishes on unseen data, likely overfit.")


if __name__ == "__main__":
    main()
