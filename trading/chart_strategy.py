#!/usr/bin/env python3
"""
Strategy-behaviour chart: for each stock, overlay on 1-min price —
  - the SUPERTREND belt (green = bull / red = bear) — the bull/bear indicator
  - the EMA-50 filter line (long only allowed above it, short only below)
  - entry ▲/▼ and exit ✕ markers with hover = time, price, reason
so you can see exactly WHEN and WHY each order triggered (flip vs EMA gate vs stop).
Single grid, MIS, warmed from history. Buttons switch stocks; zoom.

Usage: python3 chart_strategy.py [YYYY-MM-DD]  -> writes chart_strategy_<date>.html
"""
from __future__ import annotations
import json, os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy
from chart_dual import load_today, warmup_ticks, is_eod, charges, DATE

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None
CAP = 5000


def analyse(sym, seq):
    warm = warmup_ticks(sym)
    # keep only today's market-hours ticks (9:15–15:30) — drop prior-day/pre-open snapshot
    # ticks that would otherwise leak into the belt with junk timestamps
    tgt = datetime.strptime(DATE, "%Y-%m-%d").date()
    def _mkt(ft):
        t = datetime.fromtimestamp(ft, tz=IST)
        return t.date() == tgt and (9, 15) <= (t.hour, t.minute) <= (15, 30)
    seq = [(ft, px) for ft, px in seq if _mkt(ft)]
    st_mod.BREAKEVEN_TRIGGER_MULT = 1.0; st_mod.TRAIL_PEAK_MULT = 0.0; st_mod.TAKE_PROFIT_MULT = 0.0
    strat = SupertrendStrategy(sym, 1, multiplier=1.5, long_only=False, ema_period=50)
    b = CandleBuilder(900)
    for ft, px in warm:
        c = b.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c is not None:
            strat.on_candle(c)
    b.current = None   # discard the open warm-up candle so it can't leak into today's belt
    strat.position = 0; strat._entry_price = None; strat._entry_atr = None
    strat._peak = None; strat._breakeven_armed = False

    st_line, ema_line, trades = [], [], []
    pos = None; prev_eod = False

    def rec(side, ep, et, xp, xt, reason):
        qty = max(1, int(CAP * 4 / ep))
        gross = (xp - ep) * qty if side == "LONG" else (ep - xp) * qty
        trades.append({"side": side, "et": et, "ep": round(ep, 2), "xt": xt, "xp": round(xp, 2),
                       "pnl": round(gross - charges(ep * qty, xp * qty), 1), "reason": reason})

    for ft, px in seq:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if is_eod(ts):
            if pos and not prev_eod:
                rec(pos[0], pos[1], pos[2], px, ts.isoformat(), "EOD"); pos = None
            prev_eod = True
            continue
        prev_eod = False
        if pos:
            xs = strat.check_stops(px)
            if xs:
                rec(pos[0], pos[1], pos[2], xs["price"], ts.isoformat(), xs["reason"].split("|")[0].strip()); pos = None
        c = b.update(Tick(ts=ts, symbol="x", ltp=px))
        if c is not None:
            for sig in strat.on_candle(c):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    pos = ("LONG" if a == "BUY" else "SHORT", sig["price"], c.start.isoformat())
                elif a == "EXIT" and pos:
                    rec(pos[0], pos[1], pos[2], sig["price"], c.start.isoformat(), sig["reason"].split("|")[0].strip()); pos = None
            if strat._supertrend is not None:
                st_line.append((c.start.isoformat(), round(strat._supertrend, 2), strat._trend,
                                round(strat._upper, 2) if strat._upper else None,
                                round(strat._lower, 2) if strat._lower else None))
            if strat._ema is not None:
                ema_line.append((c.start.isoformat(), round(strat._ema, 2)))
    if pos:
        rec(pos[0], pos[1], pos[2], seq[-1][1], datetime.fromtimestamp(seq[-1][0], tz=IST).isoformat(), "END")

    b1 = CandleBuilder(60); candles = []
    for ft, px in seq:
        c1 = b1.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c1 is not None:
            candles.append((c1.start.isoformat(), round(c1.open, 2), round(c1.high, 2), round(c1.low, 2), round(c1.close, 2)))
    return dict(candles=candles, st=st_line, ema=ema_line, trades=trades)


def main():
    today = load_today()
    charts = {}
    for sym, seq in sorted(today.items()):
        if len(seq) < 100:
            continue
        charts[sym] = analyse(sym, seq)
        print(f"  {sym:12s} candles={len(charts[sym]['candles'])} st-pts={len(charts[sym]['st'])} "
              f"ema-pts={len(charts[sym]['ema'])} trades={len(charts[sym]['trades'])}")
    out = os.path.join(os.path.dirname(__file__), f"chart_strategy_{DATE}.html")
    open(out, "w").write(HTML.replace("/*DATA*/", json.dumps(charts))
                             .replace("__DATE__", DATE).replace("__X0__", DATE + "T09:15:00+05:30")
                             .replace("__X1__", DATE + "T15:30:00+05:30"))
    print(f"\nwrote {out}  — open in a browser; buttons switch stocks; drag to zoom")


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Strategy behaviour — __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>*{box-sizing:border-box}body{margin:0;padding:8px;background:#0d1117;color:#e6edf3;font-family:monospace;font-size:12px}
#bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.btn{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 12px;cursor:pointer;font-family:monospace}
.btn:hover{border-color:#58a6ff;color:#58a6ff}.btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
#chart{width:100%;height:86vh}.note{color:#8b949e;margin:4px 0}</style></head><body>
<div class="note">__DATE__ 9:15–15:30 · 1-min price · grey BAND = Supertrend belt (upper/lower ATR band we maintain) · bold green/red = the ACTIVE trailing line (green=bull/red=bear, flips = trend change) · blue dotted = EMA-50 filter · ▲long/▼short entry, ✕ exit (hover=time+price+reason) · drag=zoom</div>
<div id="bar"></div><div id="chart"></div>
<script>
const D=/*DATA*/, bar=document.getElementById('bar'), el=document.getElementById('chart');
function seg(st,tr){const x=[],y=[];st.forEach(s=>{if(s[2]===tr){x.push(s[0]);y.push(s[1]);}else{x.push(s[0]);y.push(null);}});return {x,y};}
function draw(sym){
  [...bar.children].forEach(b=>b.classList.toggle('active',b.textContent===sym));
  const d=D[sym];
  const cs=d.candles;
  const cndl={type:'candlestick',x:cs.map(c=>c[0]),open:cs.map(c=>c[1]),high:cs.map(c=>c[2]),low:cs.map(c=>c[3]),close:cs.map(c=>c[4]),
    name:'price',increasing:{line:{color:'#3fb950'}},decreasing:{line:{color:'#f85149'}},showlegend:false};
  // Supertrend BELT: the ATR band (upper/lower) we maintain, drawn as a filled channel
  const bx=d.st.map(s=>s[0]);
  const upper={x:bx,y:d.st.map(s=>s[3]),type:'scatter',mode:'lines',name:'ST band (upper)',
    line:{color:'#30363d',width:1,shape:'hv'},showlegend:false};
  const lower={x:bx,y:d.st.map(s=>s[4]),type:'scatter',mode:'lines',name:'ST belt',
    line:{color:'#30363d',width:1,shape:'hv'},fill:'tonexty',fillcolor:'rgba(88,110,117,0.15)'};
  const bull=seg(d.st,1), bear=seg(d.st,-1);
  const stB={x:bull.x,y:bull.y,type:'scatter',mode:'lines',name:'ST bull (support)',line:{color:'#2ecc71',width:3,shape:'hv'},connectgaps:false};
  const stR={x:bear.x,y:bear.y,type:'scatter',mode:'lines',name:'ST bear (resistance)',line:{color:'#e74c3c',width:3,shape:'hv'},connectgaps:false};
  const ema={x:d.ema.map(e=>e[0]),y:d.ema.map(e=>e[1]),type:'scatter',mode:'lines',name:'EMA-50 filter',line:{color:'#58a6ff',width:1.5,dash:'dot'}};
  const eL=d.trades.filter(t=>t.side==='LONG'), eS=d.trades.filter(t=>t.side==='SHORT');
  const txt=t=>'entry '+t.ep+' @'+t.et.slice(11,16)+'  ->  exit '+t.xp+' @'+t.xt.slice(11,16)+'  P&L Rs'+t.pnl+'  ['+t.reason+']';
  const t=[upper,lower,cndl,stB,stR,ema,
    {x:eL.map(t=>t.et),y:eL.map(t=>t.ep),type:'scatter',mode:'markers',name:'long entry',marker:{symbol:'triangle-up',size:13,color:'#2ecc71',line:{color:'#fff',width:1}},text:eL.map(txt),hoverinfo:'text'},
    {x:eS.map(t=>t.et),y:eS.map(t=>t.ep),type:'scatter',mode:'markers',name:'short entry',marker:{symbol:'triangle-down',size:13,color:'#e74c3c',line:{color:'#fff',width:1}},text:eS.map(txt),hoverinfo:'text'},
    {x:d.trades.map(t=>t.xt),y:d.trades.map(t=>t.xp),type:'scatter',mode:'markers',name:'exit',marker:{symbol:'x',size:11,color:'#e6edf3'},text:d.trades.map(txt),hoverinfo:'text'}];
  Plotly.newPlot(el,t,{paper_bgcolor:'#0d1117',plot_bgcolor:'#0d1117',font:{color:'#e6edf3',family:'monospace'},
    margin:{t:10,r:8,b:30,l:56},dragmode:'zoom',hovermode:'closest',
    xaxis:{range:['__X0__','__X1__'],gridcolor:'#161b22',rangeslider:{visible:false}},yaxis:{gridcolor:'#161b22'},
    legend:{orientation:'h'}},{responsive:true,scrollZoom:true});
}
Object.keys(D).forEach(s=>{const b=document.createElement('div');b.className='btn';b.textContent=s;b.onclick=()=>draw(s);bar.appendChild(b);});
if(Object.keys(D).length)draw(Object.keys(D)[0]);
</script></body></html>"""


if __name__ == "__main__":
    main()
