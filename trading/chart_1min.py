#!/usr/bin/env python3
"""
1-minute candle viewer (chart_candidates style) with the 15-PHASE Supertrend ensemble.
Price = 1-min candles from today's ticks. Overlaid: the 15-min Supertrend line (bull/bear)
and the 15-phase ensemble flip markers (earliest of 15 one-minute-offset grids). The
strategies are WARMED UP from recent Yahoo 1-min history, so signals appear from the
market open (9:15) instead of only after the ~3.5h cold 15-min warmup (the 'nothing till
noon' problem). Buttons switch stocks.

Usage: python3 chart_1min.py [YYYY-MM-DD]  -> writes chart_1min_<date>.html
"""
from __future__ import annotations
import json, os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import IST
from marketdata import Candle, CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

ST_INTERVAL = 900   # supertrend timeframe (15-min)
DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz=IST).strftime("%Y-%m-%d")
TICK_FILE = os.path.join(os.path.dirname(__file__), f"data/{DATE}/ticks.jsonl")


class PhasedBuilder:
    def __init__(self, off_min):
        self.off = off_min * 60; self.cur = None
    def update(self, ft, px):
        b = ((ft - self.off) // ST_INTERVAL) * ST_INTERVAL + self.off
        if self.cur is None:
            self.cur = [b, px, px, px, px]; return None
        if b > self.cur[0]:
            fin = self.cur; self.cur = [b, px, px, px, px]; return fin
        self.cur[2] = max(self.cur[2], px); self.cur[3] = min(self.cur[3], px); self.cur[4] = px
        return None


def load_today():
    sym_of, rows = {}, {}
    for line in open(TICK_FILE):
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        tk = m.get("tk")
        if not tk:
            continue
        if m.get("ts") and tk not in sym_of:
            sym_of[tk] = m["ts"].replace("-EQ", "")
        lp, ft = m.get("lp") or m.get("c"), m.get("ft")
        if not lp or not ft:
            continue
        try:
            rows.setdefault(tk, []).append((int(ft), float(lp)))
        except (ValueError, TypeError):
            continue
    out = {}
    for tk, seq in rows.items():
        seq.sort(key=lambda x: x[0])
        out[sym_of.get(tk, tk)] = seq
    return out


def warmup_ticks(sym):
    """Prior-day 1-min closes from Yahoo, as (epoch, price), to warm the supertrends."""
    try:
        import yfinance as yf
        df = yf.download(sym + ".NS", period="5d", interval="1m", progress=False, auto_adjust=False)
        if df is None or len(df) == 0:
            return []
        if getattr(df.columns, "nlevels", 1) > 1:
            df.columns = df.columns.get_level_values(0)
        idx = df.index
        try:
            idx = idx.tz_convert("Asia/Kolkata")
        except Exception:
            idx = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
        out = []
        for i, ts in enumerate(idx):
            if ts.strftime("%Y-%m-%d") >= DATE:      # only PRIOR days for warmup
                continue
            c = float(df["Close"].iloc[i])
            if c == c:
                out.append((int(ts.timestamp()), c))
        return out
    except Exception as e:
        print(f"    ({sym} warmup fetch failed: {e})")
        return []


def analyse(sym, today_seq):
    warm = warmup_ticks(sym)
    # phase-0 supertrend (15-min) + 15 phase strategies, warmed then run on today
    b0 = CandleBuilder(ST_INTERVAL)
    s0 = SupertrendStrategy("p0", 1, multiplier=1.5)
    pb = [PhasedBuilder(k) for k in range(15)]
    ps = [SupertrendStrategy(f"p{k}", 1, multiplier=1.5) for k in range(15)]
    lastp = [0] * 15
    st_line, sig15 = [], []
    last0 = 0

    def feed(ft, px, live):
        nonlocal last0
        ts = datetime.fromtimestamp(ft, tz=IST)
        c = b0.update(Tick(ts=ts, symbol="x", ltp=px))
        if c is not None:
            s0.on_candle(c)
            if live and s0._supertrend is not None:
                st_line.append((c.start.isoformat(), round(s0._supertrend, 2), s0._trend))
            last0 = s0._trend
        for i in range(15):
            fin = pb[i].update(ft, px)
            if fin is None:
                continue
            cdt = datetime.fromtimestamp(fin[0], tz=IST)
            ps[i].on_candle(Candle(start=cdt, open=fin[1], high=fin[2], low=fin[3], close=fin[4]))
            nt = ps[i]._trend
            if live and nt != lastp[i] and nt and lastp[i]:
                sig15.append((ts.isoformat(), px, "BULL" if nt == 1 else "BEAR"))
            lastp[i] = nt

    for ft, px in warm:
        feed(ft, px, live=False)      # warm-up (not plotted)
    # 1-min display candles from today's ticks
    b1 = CandleBuilder(60)
    candles = []
    for ft, px in today_seq:
        c1 = b1.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c1 is not None:
            candles.append((c1.start.isoformat(), round(c1.open, 2), round(c1.high, 2),
                            round(c1.low, 2), round(c1.close, 2)))
        feed(ft, px, live=True)       # today (plotted)
    return dict(candles=candles, st=st_line, sig15=sig15, warmed=len(warm) > 0)


def main():
    if not os.path.exists(TICK_FILE):
        print(f"No tick file: {TICK_FILE}"); sys.exit(1)
    today = load_today()
    charts = {}
    for sym, seq in sorted(today.items()):
        if len(seq) < 100:
            continue
        charts[sym] = analyse(sym, seq)
        c = charts[sym]
        print(f"  {sym:12s} 1m-candles={len(c['candles'])}  warmed={c['warmed']}  "
              f"ST-pts={len(c['st'])}  15-phase flips={len(c['sig15'])}")

    out = os.path.join(os.path.dirname(__file__), f"chart_1min_{DATE}.html")
    open(out, "w").write(HTML.replace("/*DATA*/", json.dumps(charts)).replace("__DATE__", DATE))
    print(f"\nwrote {out}  — open in a browser; buttons switch stocks, drag to zoom")


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>1-min + 15-phase — __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>*{box-sizing:border-box}body{margin:0;padding:8px;background:#0d1117;color:#e6edf3;font-family:monospace;font-size:12px}
#bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px;align-items:center}
.btn{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 12px;cursor:pointer;font-family:monospace}
.btn:hover{border-color:#58a6ff;color:#58a6ff}.btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
#chart{width:100%;height:78vh}.note{color:#8b949e;margin:4px 0}</style></head><body>
<div class="note">__DATE__ — 1-min candles + 15-min Supertrend (green=bull/red=bear) + ◆ 15-phase ensemble flips. Warmed from history so signals start at the open. Drag=zoom, dblclick=reset.</div>
<div id="bar"></div><div id="chart"></div>
<script>
const D=/*DATA*/, bar=document.getElementById('bar'), el=document.getElementById('chart');
let cur=null;
function draw(sym){
  cur=sym;[...bar.children].forEach(b=>b.classList.toggle('active',b.textContent===sym));
  const d=D[sym];
  const cs=d.candles;
  const cndl={type:'candlestick',name:sym,x:cs.map(c=>c[0]),open:cs.map(c=>c[1]),high:cs.map(c=>c[2]),low:cs.map(c=>c[3]),close:cs.map(c=>c[4]),
    increasing:{line:{color:'#2ecc71'}},decreasing:{line:{color:'#e74c3c'}}};
  const bull={x:[],y:[]},bear={x:[],y:[]};
  d.st.forEach(s=>{(s[2]===1?bull:bear).x.push(s[0]);(s[2]===1?bull:bear).y.push(s[1]);});
  const stB={x:bull.x,y:bull.y,type:'scatter',mode:'markers',name:'ST bull',marker:{color:'#2ecc71',size:5}};
  const stR={x:bear.x,y:bear.y,type:'scatter',mode:'markers',name:'ST bear',marker:{color:'#e74c3c',size:5}};
  const f=(dir)=>({x:d.sig15.filter(s=>s[2]===dir).map(s=>s[0]),y:d.sig15.filter(s=>s[2]===dir).map(s=>s[1])});
  const gb=f('BULL'),gs=f('BEAR');
  const t=[cndl,stB,stR,
    {x:gb.x,y:gb.y,type:'scatter',mode:'markers',name:'15φ BULL',marker:{symbol:'diamond',color:'#2ecc71',size:9,line:{color:'#fff',width:1}}},
    {x:gs.x,y:gs.y,type:'scatter',mode:'markers',name:'15φ BEAR',marker:{symbol:'diamond',color:'#e74c3c',size:9,line:{color:'#fff',width:1}}}];
  Plotly.newPlot(el,t,{paper_bgcolor:'#0d1117',plot_bgcolor:'#0d1117',font:{color:'#e6edf3',family:'monospace'},
    margin:{t:8,r:8,b:30,l:52},xaxis:{gridcolor:'#161b22',rangeslider:{visible:false}},yaxis:{gridcolor:'#161b22'},
    legend:{orientation:'h'},dragmode:'zoom'},{responsive:true,scrollZoom:true});
}
Object.keys(D).forEach(s=>{const b=document.createElement('div');b.className='btn';b.textContent=s;b.onclick=()=>draw(s);bar.appendChild(b);});
if(Object.keys(D).length)draw(Object.keys(D)[0]);
</script></body></html>"""


if __name__ == "__main__":
    main()
