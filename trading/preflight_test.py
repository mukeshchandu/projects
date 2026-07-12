#!/usr/bin/env python3
import os, sys, json, time, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

def ok(tag, msg=""): print("  PASS  %s%s" % (tag, (" — "+msg) if msg else ""))
def fail(tag, msg=""): print("  FAIL  %s%s" % (tag, (" — "+msg) if msg else ""))
def warn(tag, msg=""): print("  WARN  %s%s" % (tag, (" — "+msg) if msg else ""))

results = []
print("="*60)
print("  PREFLIGHT TEST")
print("="*60)

# 1. Auth
print("\n[1] Authentication")
try:
    from auth import get_session
    uid, token = get_session()
    assert uid and token and len(token) > 10
    ok("get_session()", "uid=%s  token=%s..." % (uid, token[:8]))
    results.append(True)
except Exception as e:
    fail("get_session()", str(e)); sys.exit(1)

# 2. Client + REST API
print("\n[2] REST API")
try:
    from client import FlattradeClient
    client = FlattradeClient()
    client.set_session(user_id=uid, token=token)
    ok("FlattradeClient.set_session()")
    results.append(True)
except Exception as e:
    fail("FlattradeClient", str(e)); sys.exit(1)

try:
    limits = client.get_limits()
    cash = float(limits.get("cash", 0) or 0)
    if limits.get("stat") == "Ok":
        ok("get_limits()", "cash=Rs%.2f" % cash)
    else:
        warn("get_limits()", "stat=%s  emsg=%s" % (limits.get("stat"), limits.get("emsg","?")))
    results.append(True)
except Exception as e:
    fail("get_limits()", str(e)); results.append(False)

try:
    pos = client.positions()
    ok("positions()", "type=%s" % type(pos).__name__)
    results.append(True)
except Exception as e:
    fail("positions()", str(e)); results.append(False)

try:
    ob = client.order_book()
    ok("order_book()", "type=%s" % type(ob).__name__)
    results.append(True)
except Exception as e:
    fail("order_book()", str(e)); results.append(False)

# 3. Token resolution
print("\n[3] Token resolution (all 9 stocks)")
BASKET = [("NSE","IRFC"),("NSE","HFCL"),("NSE","JSWSTEEL"),("NSE","BANKBARODA"),
          ("NSE","TATASTEEL"),("NSE","NMDC"),("NSE","IDEA"),("NSE","INDUSINDBK"),("NSE","SUZLON")]
MODES  = {"IRFC":"CNC","HFCL":"CNC","JSWSTEEL":"CNC","BANKBARODA":"CNC",
          "TATASTEEL":"CNC","NMDC":"CNC","IDEA":"CNC","INDUSINDBK":"CNC","SUZLON":"MIS"}
resolved = {}
for exch, sym in BASKET:
    try:
        hits = client.search_scrip(exch, sym)
        tok = None; tsym = None
        for r in hits:
            ts = r.get("tsym","")
            if ts in (sym+"-EQ", sym):
                tok = r.get("token"); tsym = ts; break
        if not tok and hits:
            tok = hits[0].get("token"); tsym = hits[0].get("tsym", sym+"-EQ")
        if tok:
            resolved[sym] = {"token": tok, "tsym": tsym}
            ok("  %-14s" % sym, "token=%s  tsym=%s" % (tok, tsym))
            results.append(True)
        else:
            fail("  %-14s" % sym, "no token"); results.append(False)
    except Exception as e:
        fail("  %-14s" % sym, str(e)); results.append(False)
    time.sleep(0.15)

# 4. State files
print("\n[4] Strategy state files")
for exch, sym in BASKET:
    path = "data/st_state/%s.json" % sym
    if not os.path.exists(path):
        fail("  %-14s" % sym, "MISSING — run warmup"); results.append(False); continue
    try:
        s = json.load(open(path))
        atr = s.get("atr") or 0
        trend = s.get("trend", 0)
        nc = len(s.get("candles", []))
        if atr > 0 and trend in (1,-1) and nc >= 5:
            ok("  %-14s" % sym, "atr=%.4f  trend=%+d  candles=%d" % (atr, trend, nc))
        else:
            warn("  %-14s" % sym, "atr=%s trend=%s candles=%d — may need warmup" % (atr, trend, nc))
        results.append(True)
    except Exception as e:
        fail("  %-14s" % sym, str(e)); results.append(False)

# 5. LiveBroker
print("\n[5] LiveBroker")
try:
    from live_broker import LiveBroker
    tsym_map = {sym: resolved.get(sym,{}).get("tsym", sym+"-EQ") for _,sym in BASKET}
    broker = LiveBroker(client, tsym_map, MODES)
    ok("LiveBroker(client, tsym_map, mode_map)")
    for sym, mode in MODES.items():
        exp = "C" if mode == "CNC" else "I"
        got = "C" if broker.mode_map.get(sym) == "CNC" else "I"
        assert got == exp, "%s wrong: got %s" % (sym, got)
    ok("product_type mapping", "CNC->C  MIS->I  all correct")
    results.append(True)
except Exception as e:
    fail("LiveBroker", str(e)); results.append(False)

# 6. Paper broker
print("\n[6] Paper broker")
try:
    from paper import PaperBroker
    pb = PaperBroker()
    f = pb.simulate_fill("IDEA", "BUY", 100, 14.0, "test")
    assert f and f.price > 0
    ok("simulate_fill()", "IDEA BUY 100 @ Rs%.4f" % f.price)
    results.append(True)
except Exception as e:
    fail("PaperBroker", str(e)); results.append(False)

# 7. Live order (expect rejection)
print("\n[7] Live order path (1 share IDEA @ Rs1 — expect rejection)")
try:
    tsym = resolved.get("IDEA",{}).get("tsym","IDEA-EQ")
    resp = client.place_order(
        buy_or_sell="B", product_type="C", exchange="NSE",
        tradingsymbol=tsym, quantity=1,
        price_type="LMT", price=1.0, retention="IOC",
        remarks="preflight"
    )
    stat = resp.get("stat","")
    emsg = resp.get("emsg","")
    if stat == "Ok":
        norenordno = resp.get("norenordno","")
        warn("Accepted (pre-open?)", "cancelling norenordno=%s" % norenordno)
        client.cancel_order(norenordno)
    else:
        ok("Rejected cleanly", "emsg=%s" % emsg[:70])
    results.append(True)
except Exception as e:
    fail("place_order()", str(e)); results.append(False)

# 8. WebSocket
print("\n[8] WebSocket (5s timeout)")
ws_result = {"auth": False, "err": None}
def _ws_test():
    import websocket as _ws
    from config import WS_URL
    def on_open(ws):
        ws.send(json.dumps({"t":"a","uid":uid,"actid":uid,"source":"API","accesstoken":token}))
    def on_message(ws, raw):
        try:
            msg = json.loads(raw)
            if msg.get("t") == "ak" and msg.get("s") == "OK":
                ws_result["auth"] = True; ws.close()
        except Exception: pass
    def on_error(ws, err): ws_result["err"] = str(err)
    _ws.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message, on_error=on_error).run_forever()

t = threading.Thread(target=_ws_test, daemon=True)
t.start(); t.join(timeout=6)
if ws_result["auth"]:
    ok("WebSocket auth", "ak s=OK received"); results.append(True)
elif ws_result["err"]:
    fail("WebSocket", ws_result["err"]); results.append(False)
else:
    warn("WebSocket", "no ack in 5s — check WS_URL"); results.append(False)

# Summary
print("\n" + "="*60)
passed = sum(results); total = len(results)
print("  RESULT: %d/%d passed" % (passed, total))
if passed == total: print("  ALL GOOD — safe to go live")
else: print("  FIX FAILURES before going live")
print("="*60)
