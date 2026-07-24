#!/usr/bin/env python3
"""
Build a standalone, zoomable HTML chart of today's tick data for the tradable stocks,
overlaying the Supertrend line (bull/bear coloured) and the flip signals of BOTH:
  - single 15-min grid (phase 0)
  - 15-phase ensemble (earliest of 15 minute-offset grids)
So you can see, on real intraday price, where each strategy turns bull/bear.

Usage: python3 chart_today.py [YYYY-MM-DD]   -> writes chart_<date>.html
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import IST
from marketdata import Candle, CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

INTERVAL = 900
DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz=IST).strftime("%Y-%m-%d")
TICK_FILE = os.path.join(os.path.dirname(__file__), f"data/{DATE}/ticks.jsonl")


class PhasedBuilder:
    def __init__(self, off_min):
        self.off = off_min * 60; self.cur = None
    def update(self, ft, px):
        b = ((ft - self.off) // INTERVAL) * INTERVAL + self.off
        if self.cur is None:
            self.cur = [b, px, px, px, px]; return None
        if b > self.cur[0]:
            fin = self.cur; self.cur = [b, px, px, px, px]; return fin
        self.cur[2] = max(self.cur[2], px); self.cur[3] = min(self.cur[3], px); self.cur[4] = px
        return None


def load():
    """token->symbol, and per-symbol ordered [(ft, lp)]."""
    sym_of = {}
    rows = {}
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
        ts = m.get("ts")
        if ts and tk not in sym_of:
            sym_of[tk] = ts.replace("-EQ", "")
        lp = m.get("lp") or m.get("c")
        ft = m.get("ft")
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


def analyse(seq):
    """Return price line (downsampled), phase-0 supertrend line+flips, 15-phase flips."""
    # phase-0 candles + strategy
    b0 = CandleBuilder(INTERVAL)
    s0 = SupertrendStrategy("P0", 1, multiplier=1.5)
    st_line = []      # (iso, st, trend)
    sig1 = []         # (iso, price, dir)
    last0 = 0
    # 15 phase strategies
    pb = [PhasedBuilder(k) for k in range(15)]
    ps = [SupertrendStrategy(f"p{k}", 1, multiplier=1.5) for k in range(15)]
    lastp = [0] * 15
    sig15 = []
    price = []
    last_keep = 0
    for ft, lp in seq:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if ft - last_keep >= 20:          # downsample price line to ~1 pt / 20s
            price.append((ts.isoformat(), lp)); last_keep = ft
        # phase 0
        c = b0.update(Tick(ts=ts, symbol="x", ltp=lp))
        if c is not None:
            s0.on_candle(c)
            if s0._supertrend is not None:
                st_line.append((c.start.isoformat(), round(s0._supertrend, 2), s0._trend))
            if s0._trend != last0 and s0._trend != 0 and last0 != 0:
                sig1.append((c.start.isoformat(), c.close, "BULL" if s0._trend == 1 else "BEAR"))
            last0 = s0._trend
        # 15 phases
        for i in range(15):
            fin = pb[i].update(ft, lp)
            if fin is None:
                continue
            cdt = datetime.fromtimestamp(fin[0], tz=IST)
            ps[i].on_candle(Candle(start=cdt, open=fin[1], high=fin[2], low=fin[3], close=fin[4]))
            nt = ps[i]._trend
            if nt != lastp[i] and nt != 0 and lastp[i] != 0:
                sig15.append((ts.isoformat(), lp, "BULL" if nt == 1 else "BEAR"))
            lastp[i] = nt
    return price, st_line, sig1, sig15


def main():
    if not os.path.exists(TICK_FILE):
        print(f"No tick file: {TICK_FILE}"); sys.exit(1)
    data = load()
    charts = {}
    for sym, seq in sorted(data.items()):
        if len(seq) < 100:
            continue
        price, st_line, sig1, sig15 = analyse(seq)
        charts[sym] = dict(price=price, st=st_line, sig1=sig1, sig15=sig15)
        print(f"  {sym:12s} pts={len(price)}  st={len(st_line)}  single-grid flips={len(sig1)}  15-phase flips={len(sig15)}")

    out = os.path.join(os.path.dirname(__file__), f"chart_{DATE}.html")
    with open(out, "w") as f:
        f.write(HTML.replace("/*DATA*/", json.dumps(charts)).replace("__DATE__", DATE))
    print(f"\nwrote {out}  — open it in a browser (zoom/pan/hover enabled)")


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Supertrend — __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>body{font-family:system-ui,Arial;margin:12px;background:#0f1115;color:#ddd}
h2{margin:18px 0 4px}.c{width:100%;height:460px}.legend{font-size:13px;color:#aaa;margin:4px 0 10px}</style>
</head><body>
<h1>Supertrend signals — __DATE__ (real ticks)</h1>
<div class="legend">Price = grey line. Supertrend line: <b style="color:#26a69a">green=bull</b> / <b style="color:#ef5350">red=bear</b>.
Markers: ▲/▼ = <b>single 15-min grid</b> flips &nbsp; ◆ = <b>15-phase ensemble</b> flips (earliest of 15 offset grids). Drag to zoom, double-click to reset.</div>
<div id="charts"></div>
<script>
const D = /*DATA*/;
const root = document.getElementById('charts');
function mk(sym, d){
  const h=document.createElement('h2'); h.textContent=sym; root.appendChild(h);
  const div=document.createElement('div'); div.className='c'; root.appendChild(div);
  const px={x:d.price.map(p=>p[0]),y:d.price.map(p=>p[1]),type:'scatter',mode:'lines',
            name:'price',line:{color:'#888',width:1}};
  const bull={x:[],y:[]},bear={x:[],y:[]};
  d.st.forEach(s=>{ (s[2]===1?bull:bear).x.push(s[0]); (s[2]===1?bull:bear).y.push(s[1]); });
  const stB={x:bull.x,y:bull.y,type:'scatter',mode:'markers',name:'ST bull',marker:{color:'#26a69a',size:4}};
  const stR={x:bear.x,y:bear.y,type:'scatter',mode:'markers',name:'ST bear',marker:{color:'#ef5350',size:4}};
  const f=(arr,dir)=>({x:arr.filter(s=>s[2]===dir).map(s=>s[0]),y:arr.filter(s=>s[2]===dir).map(s=>s[1])});
  const g1b=f(d.sig1,'BULL'),g1s=f(d.sig1,'BEAR'),g5b=f(d.sig15,'BULL'),g5s=f(d.sig15,'BEAR');
  const t=[px,stB,stR,
    {x:g1b.x,y:g1b.y,type:'scatter',mode:'markers',name:'1-grid BULL',marker:{symbol:'triangle-up',color:'#26a69a',size:13,line:{color:'#fff',width:1}}},
    {x:g1s.x,y:g1s.y,type:'scatter',mode:'markers',name:'1-grid BEAR',marker:{symbol:'triangle-down',color:'#ef5350',size:13,line:{color:'#fff',width:1}}},
    {x:g5b.x,y:g5b.y,type:'scatter',mode:'markers',name:'15-phase BULL',marker:{symbol:'diamond',color:'#26a69a',size:8,opacity:.55}},
    {x:g5s.x,y:g5s.y,type:'scatter',mode:'markers',name:'15-phase BEAR',marker:{symbol:'diamond',color:'#ef5350',size:8,opacity:.55}}];
  Plotly.newPlot(div,t,{paper_bgcolor:'#0f1115',plot_bgcolor:'#0f1115',font:{color:'#ddd'},
    margin:{t:10,r:10,b:30,l:50},xaxis:{gridcolor:'#222'},yaxis:{gridcolor:'#222'},
    legend:{orientation:'h'},dragmode:'zoom'},{responsive:true,scrollZoom:true});
}
Object.keys(D).forEach(s=>mk(s,D[s]));
</script></body></html>"""


if __name__ == "__main__":
    main()
