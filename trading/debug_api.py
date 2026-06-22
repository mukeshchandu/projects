#debug_api.py
import json
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv
from auth import get_session
from config import IST

HEADERS   = {"Content-Type": "application/x-www-form-urlencoded"}
REST_BASE = "https://piconnect.flattrade.in/PiConnectAPI"

def call(endpoint, payload, jkey):
    body = "jData=" + json.dumps(payload) + "&jKey=" + jkey
    r = requests.post(f"{REST_BASE}/{endpoint}", data=body, headers=HEADERS, timeout=15)
    try:    return r.status_code, r.json()
    except: return r.status_code, r.text

def show(status, data):
    print(f"  HTTP: {status}")
    if isinstance(data, list):
        print(f"  Type: list  len={len(data)}")
        for row in data[:3]: print(f"    {row}")
    elif isinstance(data, dict):
        print(f"  stat={data.get('stat')}  emsg={data.get('emsg','')}")
        for k, v in data.items(): print(f"    {k}: {str(v)[:120]}")
    else:
        print(f"  Raw: {str(data)[:400]}")

load_dotenv()
user_id, token = get_session()
print(f"Auth OK — user={user_id}\n")

base = {"uid": user_id, "actid": user_id}
now  = datetime.now(tz=IST)
et_e = str(int(now.timestamp()))
st_e = str(int((now - timedelta(days=7)).timestamp()))
print(f"Range: last 7 days  st={st_e}  et={et_e}\n")

print("── GetIndexList ─────────────────────────────────────────────")
show(*call("GetIndexList", {**base, "exch": "NSE"}, token))

print("\n── GetQuotes NSE|26000 ──────────────────────────────────────")
show(*call("GetQuotes", {**base, "exch": "NSE", "token": "26000"}, token))

print("\n── TPSeries NSE|26000 last 7 days intrv=15 ─────────────────")
show(*call("TPSeries", {**base, "exch": "NSE", "token": "26000", "st": st_e, "et": et_e, "intrv": "15"}, token))

print("\n── SearchScrip RELIANCE ─────────────────────────────────────")
sc, d = call("SearchScrip", {**base, "exch": "NSE", "stext": "RELIANCE"}, token)
show(sc, d)
rel_token = None
if isinstance(d, dict) and d.get("values"):
    for v in d["values"]:
        if "RELIANCE-EQ" in v.get("tsym",""):
            rel_token = v["token"]
            print(f"\n  RELIANCE token={rel_token}")
            break

if rel_token:
    print(f"\n── TPSeries NSE|{rel_token} (RELIANCE) last 7 days ────────────")
    show(*call("TPSeries", {**base, "exch": "NSE", "token": rel_token, "st": st_e, "et": et_e, "intrv": "15"}, token))
