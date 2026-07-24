#!/usr/bin/env python3
"""
sim_chart.py — Simulate the CURRENT (fixed) strategy code on a day's recorded ticks and
render one clean, zoomable chart per stock showing EXACTLY how the code would trade:

  TOP panel   · 1-min candles
              · Supertrend BELT  — the ATR band we maintain (grey channel)
              · Supertrend LINE  — the active trailing stop, green (bull) / red (bear);
                                    every colour flip is a trend change
              · EMA-50 filter    — blue dotted (longs only above it, shorts only below)
              · ▲ long entry / ▼ short entry / ✕ exit  (hover = time, price, reason, P&L)
  BOTTOM panel· BULL / BEAR regime  — stepped +1 (green) / -1 (red): the "0/1" trend signal

Reflects the fixed code: a MARKET-OPEN GATE drops pre-open (<09:15) and stale snapshot
ticks, so the strategy only ever sees real regular-session bars (mirrors runner.py).

Indicators are WARMED from the previous local tick-days (no network) so ATR/EMA/trend are
already primed at the open — you see real signals from 09:15, not a blank first half-day.

Usage:  python3 sim_chart.py [YYYY-MM-DD]     ->  writes sim_chart_<date>.html
"""
from __future__ import annotations
import glob, json, os, sys, warnings
from datetime import datetime, time
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import strategies.supertrend as st_mod
from config import IST, MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE, EOD_EXIT_HOUR, EOD_EXIT_MINUTE
from marketdata import CandleBuilder, Tick
from strategies.supertrend import SupertrendStrategy

# don't touch live state files during a simulation
SupertrendStrategy.save_state  = lambda self: None
SupertrendStrategy._load_state = lambda self: None

ST_INTERVAL = 900          # 15-min supertrend grid
CAP         = 5000         # own capital per trade (for P&L display)
LEV         = 4            # MIS leverage
MULT        = 1.5
HERE        = os.path.dirname(os.path.abspath(__file__))

# Exit presets: (breakeven_mult, peak_trail_mult, take_profit_mult) in ATRs.
#   current   — live behavior: lock stop to ENTRY at +1 ATR, else trail the (wide) Supertrend.
#               Gives back the whole run if price round-trips past +1 ATR.
#   trail     — breakeven floor + CHANDELIER trail: once profitable, exit k=2 ATR below the
#               peak price since entry -> ratchets up, locks in most of a run.
#   trailtight— tighter chandelier (k=1.5) — captures more but stops out sooner.
#   trailwide — looser chandelier (k=3) — gives the trade more room.
#   tp        — breakeven floor + fixed take-profit at +3 ATR (caps winners).
EXIT_PRESETS = {
    "current":    (1.0, 0.0, 0.0),
    "trail":      (1.0, 2.0, 0.0),
    "trailtight": (0.5, 1.5, 0.0),
    "trailwide":  (1.0, 3.0, 0.0),
    "tp":         (1.0, 0.0, 3.0),
}

# CLI: python3 sim_chart.py [YYYY-MM-DD] [noema] [current|trail|trailtight|trailwide|tp]
_args      = list(sys.argv[1:])
USE_EMA    = "noema" not in _args
EMA_PERIOD = 50 if USE_EMA else None
EXIT_MODE  = next((a for a in _args if a in EXIT_PRESETS), "current")
_flags     = set(EXIT_PRESETS) | {"noema"}
_dates     = [a for a in _args if a not in _flags]
DATE      = _dates[0] if _dates else datetime.now(tz=IST).strftime("%Y-%m-%d")
OPEN_T    = time(MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE)   # 09:15
CLOSE_T   = time(15, 30)


def _market_hours(ts: datetime) -> bool:
    """True only for regular-session ticks (09:15–15:30 IST) — the fixed market-open gate."""
    return OPEN_T <= ts.time() <= CLOSE_T


def _is_eod(ts: datetime) -> bool:
    return (ts.hour, ts.minute) >= (EOD_EXIT_HOUR, EOD_EXIT_MINUTE)


def _charges(entry_val: float, exit_val: float) -> float:   # MIS round-trip (no DP charge)
    stt   = 0.00025 * exit_val
    stamp = 0.00003 * entry_val
    exch  = 0.0000307 * (entry_val + exit_val)
    sebi  = 0.000001  * (entry_val + exit_val)
    return stt + stamp + exch + sebi + 0.18 * (exch + sebi)


def _load_ticks(path: str) -> dict:
    """Return {symbol: [(ft, price), ...]} sorted by time, real trade ticks only (lp present)."""
    sym_of, rows = {}, {}
    with open(path) as fh:
        for line in fh:
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
            lp, ft = m.get("lp"), m.get("ft")
            if not lp or not ft:              # need a real traded price + feed-time
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


def _warm_yfinance(symbol: str) -> list:
    """Prior-day 1-min closes from Yahoo (dates strictly before the target day), as (ft, px)
    warm-up ticks — same source the live select_basket uses. Returns [] on any failure."""
    try:
        import yfinance as yf
        df = yf.download(symbol + ".NS", period="7d", interval="1m",
                         progress=False, auto_adjust=False)
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
            if ts.strftime("%Y-%m-%d") >= DATE:      # warm from PRIOR sessions only
                continue
            c = float(df["Close"].iloc[i])
            if c == c:                               # not NaN
                out.append((int(ts.timestamp()), c))
        return out
    except Exception:
        return []


def _warm_local(symbol: str, n_days: int = 3) -> list:
    """Fallback: market-hours ticks from up to n_days of PRIOR local tick files."""
    days = sorted(glob.glob(os.path.join(HERE, "data", "*", "ticks.jsonl")))
    picked = [(os.path.basename(os.path.dirname(p)), p) for p in days
              if os.path.basename(os.path.dirname(p)) < DATE]
    warm = []
    for _, path in picked[-n_days:]:
        for ft, px in _load_ticks(path).get(symbol, []):
            if _market_hours(datetime.fromtimestamp(ft, tz=IST)):
                warm.append((ft, px))
    warm.sort(key=lambda x: x[0])
    return warm


def _warm_sequences(symbol: str) -> list:
    """Warm ATR/EMA/trend before the target day: Yahoo prior-day 1-min first (consistent for
    every symbol regardless of past baskets), local prior ticks as a fallback."""
    return _warm_yfinance(symbol) or _warm_local(symbol)


def simulate(symbol: str, day_seq: list, exit_mode: str = None) -> dict:
    """Run the real SupertrendStrategy over warm-up + target-day ticks and capture what it
    would do: supertrend/EMA/trend series + entries/exits (identical logic to the runner)."""
    be, trail, tp = EXIT_PRESETS[exit_mode or EXIT_MODE]
    st_mod.BREAKEVEN_TRIGGER_MULT = be
    st_mod.TRAIL_PEAK_MULT        = trail
    st_mod.TAKE_PROFIT_MULT       = tp
    strat = SupertrendStrategy(symbol, 1, multiplier=MULT, long_only=False, ema_period=EMA_PERIOD)

    # ── warm indicators from prior days (no trades recorded) ──
    wb = CandleBuilder(ST_INTERVAL)
    for ft, px in _warm_sequences(symbol):
        c = wb.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=px))
        if c is not None:
            strat.on_candle(c)
    # start the target day flat (the runner squares off every EOD)
    strat.position = 0
    strat._entry_price = strat._entry_atr = strat._peak = None
    strat._breakeven_armed = False

    st_line, ema_line, regime, trades = [], [], [], []
    pos = None          # (side, entry_px, entry_iso)
    warmed_at = None

    def book(side, ep, et, xp, xt, reason):
        qty   = max(1, int(CAP * LEV / ep))
        gross = (xp - ep) * qty if side == "LONG" else (ep - xp) * qty
        net   = gross - _charges(ep * qty, xp * qty)
        trades.append({"side": side, "et": et, "ep": round(ep, 2), "xt": xt,
                       "xp": round(xp, 2), "qty": qty, "pnl": round(net, 1),
                       "reason": reason})

    stb = CandleBuilder(ST_INTERVAL)
    for ft, px in day_seq:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if not _market_hours(ts):          # MARKET-OPEN GATE (the fix): ignore pre-open/stale
            continue

        if _is_eod(ts):
            if pos:
                book(pos[0], pos[1], pos[2], px, ts.isoformat(), "EOD")
                pos = None
            continue

        # tick-level stop management (runs on every tick, exactly like the runner)
        if pos:
            xs = strat.check_stops(px)
            if xs:
                book(pos[0], pos[1], pos[2], xs["price"], ts.isoformat(),
                     xs["reason"].split("|")[0].strip())
                pos = None

        c = stb.update(Tick(ts=ts, symbol="x", ltp=px))
        if c is None:
            continue

        for sig in strat.on_candle(c):
            a = sig["action"]
            if a in ("BUY", "SELL") and pos is None:
                pos = ("LONG" if a == "BUY" else "SHORT", sig["price"], c.start.isoformat())
            elif a == "EXIT" and pos is not None:
                book(pos[0], pos[1], pos[2], sig["price"], c.start.isoformat(),
                     sig["reason"].split("|")[0].strip())
                pos = None

        if strat._supertrend is not None:
            if warmed_at is None:
                warmed_at = c.start.isoformat()
            st_line.append((c.start.isoformat(), round(strat._supertrend, 2), strat._trend,
                            round(strat._upper, 2) if strat._upper else None,
                            round(strat._lower, 2) if strat._lower else None))
            regime.append((c.start.isoformat(), strat._trend))
        if strat._ema is not None:
            ema_line.append((c.start.isoformat(), round(strat._ema, 2)))

    if pos:   # never hit EOD in the data — close at last price
        last = day_seq[-1]
        book(pos[0], pos[1], pos[2], last[1],
             datetime.fromtimestamp(last[0], tz=IST).isoformat(), "END")

    # 1-min candles for the price display (gated to market hours)
    mb, candles = CandleBuilder(60), []
    for ft, px in day_seq:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if not _market_hours(ts):
            continue
        c = mb.update(Tick(ts=ts, symbol="x", ltp=px))
        if c is not None:
            candles.append((c.start.isoformat(), round(c.open, 2), round(c.high, 2),
                            round(c.low, 2), round(c.close, 2)))

    net = round(sum(t["pnl"] for t in trades), 1)
    return dict(candles=candles, st=st_line, ema=ema_line, regime=regime,
                trades=trades, net=net, warmed_at=warmed_at)


def main():
    tick_file = os.path.join(HERE, "data", DATE, "ticks.jsonl")
    if not os.path.exists(tick_file):
        sys.exit(f"no tick file: {tick_file}")
    today = _load_ticks(tick_file)
    charts = {}
    for sym, seq in sorted(today.items()):
        if len(seq) < 100:
            continue
        charts[sym] = simulate(sym, seq)
        c = charts[sym]
        print(f"  {sym:12s} 1m-candles={len(c['candles']):4d}  st-pts={len(c['st']):3d}  "
              f"trades={len(c['trades'])}  net=Rs{c['net']:+.1f}")
    suffix = ("" if USE_EMA else "_noema") + ("" if EXIT_MODE == "current" else f"_{EXIT_MODE}")
    filt   = "EMA-50 filter" if USE_EMA else "NO EMA filter (pure reversal)"
    be, tr, tp = EXIT_PRESETS[EXIT_MODE]
    exitdesc = {"current": "exit: breakeven+ST-trail",
                "trail": "exit: breakeven + chandelier 2ATR",
                "trailtight": "exit: breakeven .5 + chandelier 1.5ATR",
                "trailwide": "exit: breakeven + chandelier 3ATR",
                "tp": "exit: breakeven + TP 3ATR"}[EXIT_MODE]
    filt = filt + " · " + exitdesc
    out = os.path.join(HERE, f"sim_chart_{DATE}{suffix}.html")
    x0 = DATE + "T09:15:00+05:30"
    x1 = DATE + "T15:30:00+05:30"
    with open(out, "w") as fh:
        fh.write(HTML.replace("/*DATA*/", json.dumps(charts))
                     .replace("__DATE__", DATE).replace("__FILTER__", filt)
                     .replace("__X0__", x0).replace("__X1__", x1))
    print(f"\nwrote {out}\n  open in a browser · buttons switch stocks · drag to zoom")


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sim — __DATE__</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*{box-sizing:border-box}
body{margin:0;padding:10px 12px;background:#0d1117;color:#e6edf3;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
h1{font-size:14px;margin:0 0 8px;font-weight:600;color:#e6edf3}
h1 span{color:#8b949e;font-weight:400}
#bar{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.btn{background:#161b22;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 14px;cursor:pointer;font-family:inherit;font-size:12px;transition:.12s}
.btn:hover{border-color:#58a6ff;color:#58a6ff}
.btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
.btn .pnl{font-size:11px;margin-left:6px;opacity:.85}
#meta{color:#8b949e;margin:2px 0 6px;line-height:1.5}
#wrap{display:flex;gap:10px}
#chart{flex:1;height:82vh;min-width:0}
#side{width:270px;flex:none;background:#0f141a;border:1px solid #21262d;border-radius:8px;padding:8px 10px;overflow:auto;height:82vh}
#side h2{font-size:12px;margin:0 0 6px;color:#c9d1d9}
table{width:100%;border-collapse:collapse;font-size:11px}
th,td{text-align:left;padding:3px 4px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500}
.long{color:#3fb950}.short{color:#f85149}
.win{color:#3fb950}.loss{color:#f85149}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;vertical-align:middle}
.lg{color:#8b949e;margin-top:10px;line-height:1.7;font-size:11px}
</style></head><body>
<h1>What the fixed code would trade — <span>__DATE__ · 09:15–15:30 IST · MIS long+short · __FILTER__ · warmed from prior days</span></h1>
<div id="bar"></div>
<div id="meta"></div>
<div id="wrap"><div id="chart"></div>
<div id="side"><h2>Would-be trades</h2><div id="tbl"></div>
<div class="lg">
<span class="dot" style="background:#2ecc71"></span>Supertrend bull (support)<br>
<span class="dot" style="background:#e74c3c"></span>Supertrend bear (resistance)<br>
<span class="dot" style="background:rgba(88,110,117,.5)"></span>ATR belt (band we maintain)<br>
<span class="dot" style="background:#58a6ff"></span>EMA-50 trend filter<br>
<span class="dot" style="background:#3fb950"></span>▲ long entry &nbsp; <span class="dot" style="background:#f85149"></span>▼ short entry &nbsp; ✕ exit<br>
Bottom panel = bull(+1)/bear(-1) regime
</div></div></div>
<script>
const D=/*DATA*/, bar=document.getElementById('bar'), el=document.getElementById('chart'),
      tbl=document.getElementById('tbl'), meta=document.getElementById('meta');

function seg(st,tr){const x=[],y=[];st.forEach(s=>{if(s[2]===tr){x.push(s[0]);y.push(s[1]);}else{x.push(s[0]);y.push(null);}});return {x,y};}

function draw(sym){
  [...bar.children].forEach(b=>b.classList.toggle('active',b.dataset.s===sym));
  const d=D[sym], cs=d.candles;

  const cndl={type:'candlestick',xaxis:'x',yaxis:'y',x:cs.map(c=>c[0]),open:cs.map(c=>c[1]),high:cs.map(c=>c[2]),low:cs.map(c=>c[3]),close:cs.map(c=>c[4]),
    name:'price',increasing:{line:{color:'#3fb950'}},decreasing:{line:{color:'#f85149'}},showlegend:false,hoverinfo:'x+y'};

  const bx=d.st.map(s=>s[0]);
  const upper={xaxis:'x',yaxis:'y',x:bx,y:d.st.map(s=>s[3]),type:'scatter',mode:'lines',name:'ATR band',line:{color:'#30363d',width:1,shape:'hv'},showlegend:false,hoverinfo:'skip'};
  const lower={xaxis:'x',yaxis:'y',x:bx,y:d.st.map(s=>s[4]),type:'scatter',mode:'lines',name:'ATR belt',line:{color:'#30363d',width:1,shape:'hv'},fill:'tonexty',fillcolor:'rgba(88,110,117,0.13)',showlegend:false,hoverinfo:'skip'};
  const bull=seg(d.st,1), bear=seg(d.st,-1);
  const stB={xaxis:'x',yaxis:'y',x:bull.x,y:bull.y,type:'scatter',mode:'lines',name:'ST bull',line:{color:'#2ecc71',width:2.6,shape:'hv'},connectgaps:false,hoverinfo:'skip'};
  const stR={xaxis:'x',yaxis:'y',x:bear.x,y:bear.y,type:'scatter',mode:'lines',name:'ST bear',line:{color:'#e74c3c',width:2.6,shape:'hv'},connectgaps:false,hoverinfo:'skip'};
  const ema={xaxis:'x',yaxis:'y',x:d.ema.map(e=>e[0]),y:d.ema.map(e=>e[1]),type:'scatter',mode:'lines',name:'EMA-50',line:{color:'#58a6ff',width:1.4,dash:'dot'},hoverinfo:'skip'};

  const eL=d.trades.filter(t=>t.side==='LONG'), eS=d.trades.filter(t=>t.side==='SHORT');
  const txt=t=>t.side+'  entry '+t.ep+' @'+t.et.slice(11,16)+'  →  exit '+t.xp+' @'+t.xt.slice(11,16)+'  ['+t.reason+']  P&L Rs'+t.pnl;
  const mk=(arr,sym_,col)=>({xaxis:'x',yaxis:'y',x:arr.map(t=>t.et),y:arr.map(t=>t.ep),type:'scatter',mode:'markers',
    marker:{symbol:sym_,size:13,color:col,line:{color:'#fff',width:1}},text:arr.map(txt),hoverinfo:'text',showlegend:false});
  const exit={xaxis:'x',yaxis:'y',x:d.trades.map(t=>t.xt),y:d.trades.map(t=>t.xp),type:'scatter',mode:'markers',
    marker:{symbol:'x',size:11,color:'#e6edf3'},text:d.trades.map(txt),hoverinfo:'text',showlegend:false};

  // bottom regime panel: +1 bull (green) / -1 bear (red), stepped, filled to zero
  const rg=d.regime;
  const rb={xaxis:'x',yaxis:'y2',x:rg.map(r=>r[0]),y:rg.map(r=>r[1]===1?1:null),type:'scatter',mode:'lines',
    line:{color:'#2ecc71',width:1.5,shape:'hv'},fill:'tozeroy',fillcolor:'rgba(46,204,113,0.28)',connectgaps:false,name:'bull',showlegend:false,hoverinfo:'skip'};
  const rr={xaxis:'x',yaxis:'y2',x:rg.map(r=>r[0]),y:rg.map(r=>r[1]===-1?-1:null),type:'scatter',mode:'lines',
    line:{color:'#e74c3c',width:1.5,shape:'hv'},fill:'tozeroy',fillcolor:'rgba(231,76,60,0.26)',connectgaps:false,name:'bear',showlegend:false,hoverinfo:'skip'};

  const traces=[upper,lower,cndl,stB,stR,ema,mk(eL,'triangle-up','#3fb950'),mk(eS,'triangle-down','#f85149'),exit,rb,rr];
  const layout={paper_bgcolor:'#0d1117',plot_bgcolor:'#0d1117',font:{color:'#c9d1d9',family:'ui-monospace,monospace',size:11},
    margin:{t:8,r:10,b:26,l:56},dragmode:'zoom',hovermode:'closest',showlegend:false,
    xaxis:{domain:[0,1],anchor:'y2',range:['__X0__','__X1__'],gridcolor:'#161b22',rangeslider:{visible:false},type:'date'},
    yaxis:{domain:[0.26,1],gridcolor:'#161b22',title:{text:'price',font:{size:10}}},
    yaxis2:{domain:[0,0.18],range:[-1.4,1.4],tickvals:[-1,0,1],ticktext:['bear','·','bull'],gridcolor:'#161b22',zeroline:true,zerolinecolor:'#30363d'},
    shapes:[{type:'line',xref:'paper',x0:0,x1:1,yref:'y2',y0:0,y1:0,line:{color:'#30363d',width:1}}]};
  Plotly.react(el,traces,layout,{responsive:true,scrollZoom:true,displayModeBar:true,
    modeBarButtonsToRemove:['select2d','lasso2d','autoScale2d']});

  // side table
  let rows=d.trades.map((t,i)=>`<tr><td>${i+1}</td><td class="${t.side==='LONG'?'long':'short'}">${t.side==='LONG'?'▲L':'▼S'}</td>`
    +`<td>${t.et.slice(11,16)}</td><td>${t.ep}</td><td>${t.xp}</td><td>${t.reason}</td>`
    +`<td class="${t.pnl>=0?'win':'loss'}">${t.pnl>=0?'+':''}${t.pnl}</td></tr>`).join('');
  if(!rows) rows=`<tr><td colspan="7" style="color:#8b949e">no trades — strategy stayed flat</td></tr>`;
  tbl.innerHTML=`<table><tr><th>#</th><th></th><th>in</th><th>entry</th><th>exit</th><th>why exit</th><th>P&L</th></tr>${rows}</table>`;
  meta.innerHTML=`<b>${sym}</b> · ${d.trades.length} trade(s) · net <b class="${d.net>=0?'win':'loss'}">${d.net>=0?'+':''}Rs${d.net}</b> (MIS, 4× notional, incl. costs)`
    +` · indicators warmed & live from <b>${d.warmed_at?d.warmed_at.slice(11,16):'—'}</b>`;
}

const netOf=s=>D[s].net;
Object.keys(D).sort((a,b)=>netOf(b)-netOf(a)).forEach(s=>{
  const b=document.createElement('div');b.className='btn';b.dataset.s=s;
  const n=D[s].net;
  b.innerHTML=`${s}<span class="pnl ${n>=0?'win':'loss'}">${n>=0?'+':''}${n}</span>`;
  b.onclick=()=>draw(s);bar.appendChild(b);
});
if(Object.keys(D).length)draw(Object.keys(D).sort((a,b)=>netOf(b)-netOf(a))[0]);
</script></body></html>"""


if __name__ == "__main__":
    main()
