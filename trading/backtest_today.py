import json, os, sys
from datetime import datetime
from config import IST
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

TOKEN_MAP = {
    "14366": "IDEA",
    "12018": "SUZLON",
    "11915": "YESBANK",
    "17400": "NHPC",
    "2963":  "SAIL",
    "10666": "PNB",
    "15259": "RPOWER",
    "3499":  "TATASTEEL",
    "11184": "IDFCFIRSTB",
}

today     = datetime.now(tz=IST).strftime("%Y-%m-%d")
tick_file = f"data/{today}/ticks.jsonl"

if not os.path.exists(tick_file):
    print(f"No tick file: {tick_file}"); sys.exit(1)

ticks = []
for line in open(tick_file):
    try: msg = json.loads(line.strip())
    except: continue
    if msg.get("t") not in ("tk","tf"): continue
    token = msg.get("tk")
    if token not in TOKEN_MAP: continue
    lp = msg.get("lp") or msg.get("c")
    ft = msg.get("ft")
    if not lp or not ft: continue
    ticks.append((int(ft), token, float(lp), float(msg.get("v",0) or 0)))

ticks.sort(key=lambda x: x[0])
print(f"Loaded {len(ticks):,} ticks  ({today})\n")

builders   = {sym: CandleBuilder(900) for sym in TOKEN_MAP.values()}
strategies = {sym: SupertrendStrategy(sym, qty=1) for sym in TOKEN_MAP.values()}

MAX_CAPITAL = 10_000
slip        = 3.0 / 10_000

trades   = []
open_pos = {sym: None for sym in TOKEN_MAP.values()}
total_pnl = 0.0

for ft_int, token, price, vol in ticks:
    sym    = TOKEN_MAP[token]
    ts     = datetime.fromtimestamp(ft_int, tz=IST)
    candle = builders[sym].update(Tick(ts=ts, symbol=token, ltp=price, volume=vol, raw={}))
    if candle is None: continue

    for sig in strategies[sym].on_candle(candle):
        action = sig["action"]
        px     = sig["price"]
        t_str  = candle.start.strftime("%H:%M")

        if action == "BUY" and open_pos[sym] is None:
            fill = px * (1 + slip)
            qty  = max(1, int(MAX_CAPITAL / fill))
            open_pos[sym] = {"side":"LONG","entry":fill,"qty":qty,"time":t_str}
            print(f"  ENTRY  {sym:12s}  LONG   Rs{fill:.2f}  qty={qty}  @{t_str}  [{sig.get('reason','')}]")

        elif action == "SELL" and open_pos[sym] is None:
            fill = px * (1 - slip)
            qty  = max(1, int(MAX_CAPITAL / fill))
            open_pos[sym] = {"side":"SHORT","entry":fill,"qty":qty,"time":t_str}
            print(f"  ENTRY  {sym:12s}  SHORT  Rs{fill:.2f}  qty={qty}  @{t_str}  [{sig.get('reason','')}]")

        elif action == "EXIT" and open_pos[sym] is not None:
            pos  = open_pos[sym]
            fill = px*(1-slip) if pos["side"]=="LONG" else px*(1+slip)
            pnl  = ((fill-pos["entry"]) if pos["side"]=="LONG" else (pos["entry"]-fill)) * pos["qty"]
            total_pnl += pnl
            open_pos[sym] = None
            mark = "WIN " if pnl >= 0 else "LOSS"
            print(f"  EXIT   {sym:12s}  {mark}  Rs{pnl:+.2f}  "
                  f"entry=Rs{pos['entry']:.2f}@{pos['time']}  exit=Rs{fill:.2f}@{t_str}  "
                  f"running=Rs{total_pnl:+.2f}")
            trades.append({"sym":sym,"pnl":pnl})

# ── Per-stock summary ─────────────────────────────────────────────────────
print(f"\n{'─'*65}")
print(f"{'SYMBOL':>12}  {'TRADES':>6}  {'WINS':>5}  {'WIN%':>5}  {'P&L':>10}  STATUS")
print(f"{'─'*65}")
for sym in sorted(TOKEN_MAP.values()):
    sym_trades = [t for t in trades if t["sym"] == sym]
    still_open = open_pos[sym]
    status     = f"OPEN {open_pos[sym]['side']}" if still_open else "flat"
    if not sym_trades:
        print(f"{sym:>12}  {'0':>6}  {'─':>5}  {'─':>5}  {'─':>10}  {status}")
        continue
    w   = [t["pnl"] for t in sym_trades if t["pnl"] > 0]
    pnl = sum(t["pnl"] for t in sym_trades)
    wr  = 100 * len(w) / len(sym_trades)
    print(f"{sym:>12}  {len(sym_trades):>6}  {len(w):>5}  {wr:>4.0f}%  Rs{pnl:>+9.2f}  {status}")

n_wins = len([t for t in trades if t["pnl"] > 0])
print(f"{'─'*65}")
print(f"{'TOTAL':>12}  {len(trades):>6}  {n_wins:>5}  "
      f"{100*n_wins/max(1,len(trades)):>4.0f}%  Rs{total_pnl:>+9.2f}")
print(f"{'─'*65}")
