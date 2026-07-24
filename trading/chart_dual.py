#!/usr/bin/env python3
"""
Two stacked 1-min candle charts (shared x / linked zoom), full session 9:15-15:30, showing
HOW EACH STRATEGY WOULD HAVE TRADED today (simulated on today's ticks, warmed from history):
  TOP    = single 15-min grid strategy (production: EMA-50 filter, stops, MIS long+short)
  BOTTOM = 15-phase ensemble (earliest of 15 offset grids, always-in reversal)
Each would-be trade: ▲/▼ entry (green long / red short), ✕ exit, and a line entry->exit
coloured green (win) / red (loss). Hover shows P&L. Buttons switch stocks.

Usage: python3 chart_dual.py [YYYY-MM-DD]  -> writes chart_dual_<date>.html
"""
from __future__ import annotations
import json, os, sys, warnings
from datetime import datetime
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST
from marketdata import Candle, CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

ST_INTERVAL = 900
CAP = 5000
DATE = sys.argv[1] if len(sys.argv) > 1 else datetime.now(tz=IST).strftime("%Y-%m-%d")
TICK_FILE = os.path.join(os.path.dirname(__file__), f"data/{DATE}/ticks.jsonl")


def charges(entry_val, exit_val):   # MIS round-trip (no DP)
    stt = 0.00025 * exit_val
    stamp = 0.00003 * entry_val
    exch = 0.0000307 * (entry_val + exit_val)
    sebi = 0.000001 * (entry_val + exit_val)
    return stt + stamp + exch + sebi + 0.18 * (exch + sebi)


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


def is_eod(ts):
    return ts.hour > 15 or (ts.hour == 15 and ts.minute >= 0)


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
            if ts.strftime("%Y-%m-%d") >= DATE:
                continue
            c = float(df["Close"].iloc[i])
            if c == c:
                out.append((int(ts.timestamp()), c))
        return out
    except Exception:
        return []


def _record(trades, side, ep, et, xp, xt, reason):
    qty = max(1, int(CAP * 4 / ep))
    gross = (xp - ep) * qty if side == "LONG" else (ep - xp) * qty
    net = gross - charges(ep * qty, xp * qty)
    trades.append({"side": side, "et": et, "ep": round(ep, 2), "xt": xt, "xp": round(xp, 2),
                   "pnl": round(net, 1), "reason": reason})


def sim_single(warm, today):
    """Production single-grid strategy: EMA-50, stops, MIS long+short."""
    st_mod.BREAKEVEN_TRIGGER_MULT = 1.0; st_mod.TRAIL_PEAK_MULT = 0.0; st_mod.TAKE_PROFIT_MULT = 0.0
    strat = SupertrendStrategy("s", 1, multiplier=1.5, long_only=False, ema_period=50)
    b = CandleBuilder(ST_INTERVAL)
    for ft, px in warm:                      # warm indicators only
        c = b.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c is not None:
            strat.on_candle(c)
    strat.position = 0; strat._entry_price = None; strat._entry_atr = None
    strat._peak = None; strat._breakeven_armed = False
    trades = []; pos = None; prev_eod = False
    for ft, px in today:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if is_eod(ts):
            if pos and not prev_eod:
                _record(trades, pos[0], pos[1], pos[2], px, ts.isoformat(), "EOD"); pos = None
            prev_eod = True
            continue
        prev_eod = False
        if pos:
            xs = strat.check_stops(px)
            if xs:
                _record(trades, pos[0], pos[1], pos[2], xs["price"], ts.isoformat(),
                        xs["reason"].split("|")[0].strip()); pos = None
        c = b.update(Tick(ts=ts, symbol="x", ltp=px))
        if c is not None:
            for sig in strat.on_candle(c):
                a = sig["action"]
                if a in ("BUY", "SELL") and pos is None:
                    pos = ("LONG" if a == "BUY" else "SHORT", sig["price"], c.start.isoformat())
                elif a == "EXIT" and pos:
                    _record(trades, pos[0], pos[1], pos[2], sig["price"], c.start.isoformat(),
                            sig["reason"].split("|")[0].strip()); pos = None
    if pos:
        _record(trades, pos[0], pos[1], pos[2], today[-1][1],
                datetime.fromtimestamp(today[-1][0], tz=IST).isoformat(), "END")
    return trades


def sim_phase(warm, today):
    """15-phase ensemble, always-in reversal on the earliest phase flip."""
    st_mod.BREAKEVEN_TRIGGER_MULT = 0.0; st_mod.TRAIL_PEAK_MULT = 0.0; st_mod.TAKE_PROFIT_MULT = 0.0
    pb = [PhasedBuilder(k) for k in range(15)]
    ps = [SupertrendStrategy(f"p{k}", 1, multiplier=1.5) for k in range(15)]
    last = [0] * 15
    for ft, px in warm:
        for i in range(15):
            fin = pb[i].update(ft, px)
            if fin is not None:
                ps[i].on_candle(Candle(start=datetime.fromtimestamp(fin[0], tz=IST),
                                       open=fin[1], high=fin[2], low=fin[3], close=fin[4]))
                last[i] = ps[i]._trend
    trades = []; pos = None; prev_eod = False
    for ft, px in today:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if is_eod(ts):
            if pos and not prev_eod:
                _record(trades, pos[0], pos[1], pos[2], px, ts.isoformat(), "EOD"); pos = None
            prev_eod = True
            continue
        prev_eod = False
        for i in range(15):
            fin = pb[i].update(ft, px)
            if fin is None:
                continue
            ps[i].on_candle(Candle(start=datetime.fromtimestamp(fin[0], tz=IST),
                                   open=fin[1], high=fin[2], low=fin[3], close=fin[4]))
            nt = ps[i]._trend
            if nt != last[i] and nt and last[i]:
                want = "LONG" if nt == 1 else "SHORT"
                if pos is None:
                    pos = (want, px, ts.isoformat())
                elif pos[0] != want:
                    _record(trades, pos[0], pos[1], pos[2], px, ts.isoformat(), "flip")
                    pos = (want, px, ts.isoformat())
            last[i] = nt
    if pos:
        _record(trades, pos[0], pos[1], pos[2], today[-1][1],
                datetime.fromtimestamp(today[-1][0], tz=IST).isoformat(), "END")
    return trades


def one_min(today):
    b = CandleBuilder(60); out = []
    for ft, px in today:
        c = b.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c is not None:
            out.append((c.start.isoformat(), round(c.open, 2), round(c.high, 2), round(c.low, 2), round(c.close, 2)))
    return out


def main():
    if not os.path.exists(TICK_FILE):
        print(f"No tick file: {TICK_FILE}"); sys.exit(1)
    charts = {}
    for sym, seq in sorted(load_today().items()):
        if len(seq) < 100:
            continue
        warm = warmup_ticks(sym)
        t1, t15 = sim_single(warm, seq), sim_phase(warm, seq)
        charts[sym] = {"candles": one_min(seq), "t1": t1, "t15": t15, "warmed": len(warm) > 0}
        p1 = round(sum(t["pnl"] for t in t1), 1); p15 = round(sum(t["pnl"] for t in t15), 1)
        print(f"  {sym:12s} warmed={len(warm)>0}  single: {len(t1)} trades NET Rs{p1:+.0f}   "
              f"15-phase: {len(t15)} trades NET Rs{p15:+.0f}")
    out = os.path.join(os.path.dirname(__file__), f"chart_dual_{DATE}.html")
    open(out, "w").write(HTML.replace("/*DATA*/", json.dumps(charts))
                             .replace("__DATE__", DATE).replace("__X0__", DATE + "T09:15:00+05:30")
                             .replace("__X1__", DATE + "T15:30:00+05:30"))
    print(f"\nwrote {out}  — WOULD-HAVE-TRADED view; buttons switch stocks; zoom syncs both")


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Would-have-traded — __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>*{box-sizing:border-box}body{margin:0;padding:8px;background:#0d1117;color:#e6edf3;font-family:monospace;font-size:12px}
#bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.btn{background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;padding:5px 12px;cursor:pointer;font-family:monospace}
.btn:hover{border-color:#58a6ff;color:#58a6ff}.btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
#chart{width:100%;height:88vh}.note{color:#8b949e;margin:4px 0}</style></head><body>
<div class="note">__DATE__ 9:15–15:30 · would-have-traded on today's ticks · TOP = single 15-min grid (live: EMA-50+stops) · BOTTOM = 15-phase ensemble · ▲long/▼short entry, ✕ exit, line green=win/red=loss · zoom syncs</div>
<div id="bar"></div><div id="chart"></div>
<script>
const D=/*DATA*/, bar=document.getElementById('bar'), el=document.getElementById('chart');
function cndl(cs,yaxis){return {type:'candlestick',x:cs.map(c=>c[0]),open:cs.map(c=>c[1]),high:cs.map(c=>c[2]),low:cs.map(c=>c[3]),close:cs.map(c=>c[4]),
  yaxis:yaxis,xaxis:'x',name:'price',showlegend:false,increasing:{line:{color:'#3a5'}},decreasing:{line:{color:'#a44'}}};}
function trades(list,yaxis,showleg){
  const t=[];
  // connecting lines (win green / loss red)
  const wx=[],wy=[],lx=[],ly=[];
  list.forEach(tr=>{const a=(tr.pnl>=0)?[wx,wy]:[lx,ly];a[0].push(tr.et,tr.xt,null);a[1].push(tr.ep,tr.xp,null);});
  t.push({x:wx,y:wy,type:'scatter',mode:'lines',yaxis,xaxis:'x',line:{color:'#2ecc71',width:1.5},name:'win',showlegend:showleg});
  t.push({x:lx,y:ly,type:'scatter',mode:'lines',yaxis,xaxis:'x',line:{color:'#e74c3c',width:1.5},name:'loss',showlegend:showleg});
  const el_=(sel)=>list.filter(sel);
  const eL=el_(t=>t.side==='LONG'),eS=el_(t=>t.side==='SHORT');
  t.push({x:eL.map(t=>t.et),y:eL.map(t=>t.ep),type:'scatter',mode:'markers',yaxis,xaxis:'x',name:'long entry',showlegend:showleg,
    marker:{symbol:'triangle-up',size:12,color:'#2ecc71',line:{color:'#fff',width:1}},text:eL.map(t=>'entry '+t.ep),hoverinfo:'text+x'});
  t.push({x:eS.map(t=>t.et),y:eS.map(t=>t.ep),type:'scatter',mode:'markers',yaxis,xaxis:'x',name:'short entry',showlegend:showleg,
    marker:{symbol:'triangle-down',size:12,color:'#e74c3c',line:{color:'#fff',width:1}},text:eS.map(t=>'entry '+t.ep),hoverinfo:'text+x'});
  t.push({x:list.map(t=>t.xt),y:list.map(t=>t.xp),type:'scatter',mode:'markers',yaxis,xaxis:'x',name:'exit',showlegend:showleg,
    marker:{symbol:'x',size:11,color:'#e6edf3'},text:list.map(t=>'exit '+t.xp+'  ('+t.reason+')  P&L Rs'+t.pnl),hoverinfo:'text+x'});
  return t;
}
function draw(sym){
  [...bar.children].forEach(b=>b.classList.toggle('active',b.textContent===sym));
  const d=D[sym];
  const p1=d.t1.reduce((s,t)=>s+t.pnl,0).toFixed(0), p15=d.t15.reduce((s,t)=>s+t.pnl,0).toFixed(0);
  const t=[cndl(d.candles,'y'),cndl(d.candles,'y2'),...trades(d.t1,'y',true),...trades(d.t15,'y2',false)];
  Plotly.newPlot(el,t,{paper_bgcolor:'#0d1117',plot_bgcolor:'#0d1117',font:{color:'#e6edf3',family:'monospace'},
    margin:{t:24,r:8,b:30,l:56},dragmode:'zoom',hovermode:'closest',
    xaxis:{domain:[0,1],range:['__X0__','__X1__'],gridcolor:'#161b22',rangeslider:{visible:false},anchor:'y2'},
    yaxis:{domain:[0.53,1.0],gridcolor:'#161b22'},yaxis2:{domain:[0,0.47],gridcolor:'#161b22',anchor:'x'},
    annotations:[{text:'SINGLE 15-min grid — '+d.t1.length+' trades, NET Rs'+p1,x:0,xref:'paper',y:1.0,yref:'paper',showarrow:false,font:{color:'#8b949e'},xanchor:'left'},
                 {text:'15-PHASE ensemble — '+d.t15.length+' trades, NET Rs'+p15,x:0,xref:'paper',y:0.47,yref:'paper',showarrow:false,font:{color:'#8b949e'},xanchor:'left'}]
  },{responsive:true,scrollZoom:true});
}
Object.keys(D).forEach(s=>{const b=document.createElement('div');b.className='btn';b.textContent=s;b.onclick=()=>draw(s);bar.appendChild(b);});
if(Object.keys(D).length)draw(Object.keys(D)[0]);
</script></body></html>"""


if __name__ == "__main__":
    main()
