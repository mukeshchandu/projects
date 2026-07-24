#!/usr/bin/env python3
"""
verify_pipeline.py — Offline end-to-end smoke test of the LIVE runner path (market closed).

Drives the real runner.TradingApp.handle_tick over a recorded day's ticks with the PaperBroker,
so it exercises: startup wiring, warm-up (indicators primed from history), the market-open gate
(<09:15 dropped), 15-min candle building, entry signals WITH the new filters, order placement,
tick-level stops/exits, and EOD square-off — without touching the live broker or network.

Recorded ticks are shifted to "today" so the runner's same-day gate accepts them.

Usage:  python3 verify_pipeline.py [YYYY-MM-DD tick-day]   (default 2026-07-17)
"""
import os, sys, json, logging, types
from datetime import datetime, date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# stub live-only deps not installed on the Mac, so `import runner` works offline
_dot = types.ModuleType("dotenv"); _dot.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _dot)
_ws = types.ModuleType("websocket")
_ws.WebSocketApp = object; _ws.enableTrace = lambda *a, **k: None
sys.modules.setdefault("websocket", _ws)
SRC_DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-07-17"

# runner opens data/<today>/ticks.jsonl + logs/*.csv at import — make sure the dirs exist
from config import IST
TODAY = datetime.now(tz=IST).date()
os.makedirs(f"data/{TODAY}", exist_ok=True)
os.makedirs("logs", exist_ok=True)

import runner as R
import sim_chart as S
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy
from paper import PaperBroker

# ── capture the runner's log so we can count what happened ──
events = []
class Cap(logging.Handler):
    def emit(self, rec): events.append(rec.getMessage())
R.log.setLevel(logging.INFO); R.log.addHandler(Cap())

# ── don't pollute real logs / state during the test ──
import tempfile
tmp = tempfile.mkdtemp(prefix="verify_")
R._tick_fh  = open(f"{tmp}/ticks.jsonl", "a")
R._csv_fh   = open(f"{tmp}/trades.csv", "a")
R._strat_fh = open(f"{tmp}/strategy.csv", "a")
R._save_runner_state = lambda: None
R.broker = PaperBroker()
R.MAX_POSITIONS = 2
R.CAPITAL_PER_TRADE = 5000

# ── tokens present in the recorded day ──
TOKENS = {"11536": ("TCS", 0.10), "10753": ("UNIONBANK", 0.01), "5097": ("ETERNAL", 0.05),
          "1594": ("INFY", 0.10), "4244": ("HDFCAMC", 0.10)}

# ── warm each strategy from history, exactly as select_basket would prime it ──
S.ST_INTERVAL = 900
S.DATE = SRC_DAY
SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

R.INSTRUMENTS = {}
R.MODES = {}
R._open_trades = {}
warmed = {}
for tok, (sym, ti) in TOKENS.items():
    strat = SupertrendStrategy(sym, 1, multiplier=1.5, long_only=False, ema_period=50)
    wb = CandleBuilder(900)
    nwarm = 0
    for ft, px in S._warm_sequences(sym):
        c = wb.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c is not None:
            strat.on_candle(c); nwarm += 1
    strat.position = 0; strat._entry_price = strat._entry_atr = strat._peak = None
    strat._breakeven_armed = False
    warmed[sym] = (nwarm, strat._atr, strat._ema, strat._trend)
    R.INSTRUMENTS[tok] = {"symbol": sym, "tsym": f"{sym}-EQ", "exchange": "NSE",
                          "token": tok, "ti": ti, "mode": "MIS",
                          "builder": CandleBuilder(900), "strategy": strat}
    R.MODES[sym] = "MIS"
    R._open_trades[sym] = None

print(f"WARM-UP (history < {SRC_DAY}):")
for sym, (n, atr, ema, tr) in warmed.items():
    ok = "OK " if (n >= 14 and atr and ema) else "!! "
    print(f"  {ok}{sym:10s} bars={n:3d}  atr={atr}  ema={'set' if ema else 'None'}  trend={tr}")

# ── replay: shift recorded ticks to today so the same-day gate accepts them ──
shift = int((TODAY - date.fromisoformat(SRC_DAY)).total_seconds())
app = R.TradingApp()
fed = preopen = candles0 = 0
first_ts = last_ts = None
try:
    with open(f"data/{SRC_DAY}/ticks.jsonl") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            m = json.loads(line)
            tk = m.get("tk")
            if tk not in TOKENS or not m.get("ft"):
                continue
            orig_ft = int(m["ft"]); m["ft"] = str(orig_ft + shift)
            t_ist = datetime.fromtimestamp(orig_ft, tz=IST)
            if (t_ist.hour, t_ist.minute) < (9, 15):
                preopen += 1
            if t_ist.date() == date.fromisoformat(SRC_DAY):
                first_ts = first_ts or t_ist; last_ts = t_ist
            app.handle_tick(m); fed += 1
    crash = None
except Exception as e:
    import traceback; crash = traceback.format_exc()

# ── report ──
def n(sub): return sum(1 for e in events if sub in e)
print(f"\nREPLAY  src={SRC_DAY} (shifted +{shift//86400}d to {TODAY})  ticks_fed={fed}")
print(f"  session span (IST): {first_ts.strftime('%H:%M') if first_ts else '?'} → {last_ts.strftime('%H:%M') if last_ts else '?'}")
print(f"  pre-open ticks (<09:15) present in data : {preopen}")
print(f"  candles closed (strategy-state logs)    : {n('CANDLE') or '(debug off)'}")
print(f"  ENTRY signals placed                    : {n('ENTRY #')}")
print(f"  EXIT / book events                      : {n('EXIT  ')}")
print(f"  ORDER placements (paper)                : {n('ORDER ')}")
print(f"  errors / rejects in log                 : {n('ERROR') + n('REJECT')}")
print("\n  entry/exit log lines:")
for e in events:
    if "ENTRY #" in e or e.strip().startswith("EXIT ") or "EOD" in e:
        print("   ", e[:110])
print("\nRESULT:", "CRASH\n" + crash if crash else "no exceptions — pipeline ran clean")
