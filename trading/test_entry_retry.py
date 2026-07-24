#!/usr/bin/env python3
"""Unit test: entry retry-on-cancel. Mocks a live broker that cancels orders, and checks the
runner re-arms, re-places on a fresh tick with a +1-tick cross, fills, and abandons after cap."""
import os, sys, types
from datetime import datetime, timezone
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from config import IST
os.makedirs(f"data/{datetime.now(tz=IST).date()}", exist_ok=True); os.makedirs("logs", exist_ok=True)
_d = types.ModuleType("dotenv"); _d.load_dotenv = lambda *a, **k: None; sys.modules.setdefault("dotenv", _d)
_w = types.ModuleType("websocket"); _w.WebSocketApp = object; _w.enableTrace = lambda *a, **k: None
sys.modules.setdefault("websocket", _w)

import runner as R
from paper import PaperFill

class MockLive:
    """Mimics LiveBroker: returns a fill with an ordno and registers it in .pending."""
    def __init__(self): self.pending = {}; self._committed = 0.0; self.orders = []; self.n = 0
    def simulate_fill(self, symbol, side, qty, price, reason="", quote=None, cross_ticks=0, is_exit=False):
        self.n += 1; ordno = f"ORD{self.n}"
        px = round(price + cross_ticks * 0.05 * (1 if side == "BUY" else -1), 2)
        self.orders.append({"ordno": ordno, "side": side, "cross": cross_ticks, "px": px})
        self.pending[ordno] = {"symbol": symbol, "side": side, "qty": qty, "est": px, "mode": "MIS"}
        return PaperFill(datetime.now(timezone.utc), symbol, side, qty, px, reason, ordno)

class DummyStrat:
    def __init__(self): self.position = 1; self._entry_price = 100.0; self._entry_atr = 1.0

def cancel(app, ordno): app.handle_order({"reporttype": "Canceled", "norenordno": ordno, "tsym": "TCS-EQ"})
def fill(app, ordno, px): app.handle_order({"reporttype": "Fill", "norenordno": ordno, "avgprc": str(px), "tsym": "TCS-EQ"})

def setup():
    R.broker = MockLive(); R.MODES = {"TCS": "MIS"}; R.MAX_POSITIONS = 2; R.CAPITAL_PER_TRADE = 5000
    R.INSTRUMENTS = {"1": {"symbol": "TCS", "strategy": DummyStrat()}}
    R._open_trades = {"TCS": {"side": "LONG", "entry": 100.0, "qty": 8, "ts_str": "11:30",
                              "filled": False, "entry_armed": False, "entry_tries": 0,
                              "trade_no": 1, "reason": "flip UP"}}
    R._save_runner_state = lambda: None
    return R.TradingApp(), R.broker

fails = []
def chk(cond, msg): print(("  ok  " if cond else " FAIL ") + msg); (fails.append(msg) if not cond else None)

print("A) 1st+2nd attempts at fresh touch (cross 0); 3rd+ cross 1 tick; then fill")
app, bk = setup()
app._do_entry("TCS", 100.00, "flip UP", "11:30")                       # ORD1 @ touch (cross 0)
chk(bk.orders[-1]["cross"] == 0, "attempt 1 crosses 0 ticks (at the ask)")
cancel(app, "ORD1")
t = R._open_trades["TCS"]
chk(t["entry_armed"] and t["entry_tries"] == 1, "1st cancel -> re-armed, tries=1")
app._do_entry("TCS", 100.20, "retry", "11:30")                          # ORD2: 2nd attempt, STILL at touch
chk(bk.orders[-1]["cross"] == 0, "attempt 2 (1st retry) still crosses 0 ticks")
chk(bk.orders[-1]["px"] == 100.20, "attempt 2 priced off FRESH quote 100.20 (no tick given up), not stale 100.00")
cancel(app, "ORD2")
chk(t["entry_tries"] == 2, "2nd cancel -> tries=2")
app._do_entry("TCS", 100.30, "retry", "11:30")                          # ORD3: 3rd attempt, now cross 1
chk(bk.orders[-1]["cross"] == 1, "attempt 3 (2nd retry) crosses 1 tick")
chk(bk.orders[-1]["px"] == 100.35, "attempt 3 = fresh 100.30 + 1 tick = 100.35")
fill(app, "ORD3", 100.35)
chk(t["filled"] and not t["entry_armed"], "fill confirmed -> filled=True")

print("B) repeated cancels -> abandon after cap, ledger + strategy reset")
app, bk = setup()
app._do_entry("TCS", 100.0, "flip UP", "11:30")                         # ORD1
for i in range(R.MAX_ENTRY_RETRIES):                                    # ORD1 cancel + retries
    cancel(app, f"ORD{i+1}")
    if R._open_trades["TCS"]:
        app._do_entry("TCS", 100.0 + 0.1*(i+1), "retry", "11:30")
n_before = bk.n
cancel(app, f"ORD{n_before}")                                           # the (cap+1)-th cancel
chk(R._open_trades["TCS"] is None, "abandoned after cap -> ledger cleared")
chk(R.INSTRUMENTS["1"]["strategy"].position == 0, "strategy reset flat on abandon")
chk(bk.n == R.MAX_ENTRY_RETRIES + 1, f"placed {R.MAX_ENTRY_RETRIES+1} orders total (1 + {R.MAX_ENTRY_RETRIES} retries)")

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
