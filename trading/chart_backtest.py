#!/usr/bin/env python3
import json, math, sys, warnings; warnings.filterwarnings("ignore")
from datetime import time as dtime
import pandas as pd, yfinance as yf, pytz

IST=pytz.timezone("Asia/Kolkata"); SYM="IRFC"; DAYS=3; TICK=0.05; CAPITAL=1500.0

def supertrend(df,p=14,m=1.5):
    hl2=(df["High"]+df["Low"])/2
    tr=pd.concat([df["High"]-df["Low"],(df["High"]-df["Close"].shift()).abs(),(df["Low"]-df["Close"].shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/p,min_periods=p,adjust=False).mean()
    bub=(hl2+m*atr).values; blb=(hl2-m*atr).values; close=df["Close"].values; n=len(df)
    fub=[float("nan")]*n; flb=[float("nan")]*n; st=[float("nan")]*n; trd=[0]*n
    fi=next((i for i in range(n) if bub[i]==bub[i]),None)
    if fi is None: df=df.copy();df["st"]=float("nan");df["trend"]=0;return df
    fub[fi]=bub[fi];flb[fi]=blb[fi];trd[fi]=1;st[fi]=flb[fi]
    for i in range(fi+1,n):
        pc=close[i-1]
        fub[i]=bub[i] if bub[i]<fub[i-1] or pc>fub[i-1] else fub[i-1]
        flb[i]=blb[i] if blb[i]>flb[i-1] or pc<flb[i-1] else flb[i-1]
        c=close[i]
        if c>st[i-1]: trd[i]=1;st[i]=flb[i]
        elif c<st[i-1]: trd[i]=-1;st[i]=fub[i]
        else: trd[i]=trd[i-1];st[i]=flb[i] if trd[i]==1 else fub[i]
    df=df.copy();df["st"]=st;df["trend"]=trd;return df

def fp(p,s): return round((math.ceil(p/TICK)+1)*TICK,2) if s=="BUY" else round((math.floor(p/TICK)-1)*TICK,2)

raw=yf.download(f"{SYM}.NS",period="30d",interval="15m",progress=False,auto_adjust=True)
if isinstance(raw.columns,pd.MultiIndex): raw.columns=raw.columns.get_level_values(0)
if raw.index.tz is None: raw.index=raw.index.tz_localize("UTC")
raw.index=raw.index.tz_convert(IST); raw=raw.between_time("09:15","15:30"); raw=supertrend(raw)
all_d=sorted(raw.index.normalize().unique()); sim_d=all_d[-DAYS:]
pre=raw[raw.index.normalize()<sim_d[0]]; df=raw[raw.index.normalize().isin(sim_d)].copy()
init_trd=int(pre["trend"].iloc[-1]) if len(pre)>0 else 1

# Simulate
open_trade=None; prev=init_trd; entries=[]; exits=[]; total_pnl=0
for ts,row in df.iterrows():
    c=float(row["Close"]); t=int(row["trend"]); last=prev; prev=t
    if last==0: continue
    sig="BUY" if last==-1 and t==1 else "SELL" if last==1 and t==-1 else None
    if not sig: continue
    if open_trade and sig=="SELL":
        ex=fp(c,"SELL"); pnl=(ex-open_trade["entry"])*open_trade["qty"]; total_pnl+=pnl
        exits.append({"ts":ts.isoformat(),"price":ex,"pnl":round(pnl,2),"open":False})
        open_trade=None
    if sig=="BUY" and not open_trade:
        en=fp(c,"BUY"); qty=int(CAPITAL*0.8/en)
        if qty>0: open_trade={"entry":en,"qty":qty,"ts":ts.isoformat()}; entries.append({"ts":ts.isoformat(),"price":en,"qty":qty})
if open_trade:
    ex=fp(float(df["Close"].iloc[-1]),"SELL"); pnl=(ex-open_trade["entry"])*open_trade["qty"]; total_pnl+=pnl
    exits.append({"ts":df.index[-1].isoformat(),"price":ex,"pnl":round(pnl,2),"open":True})

print(f"\nTrades: {len(exits)}  |  Total P&L: {'+'if total_pnl>=0 else ''}Rs{total_pnl:.2f}\n")
for e,x in zip(entries,exits):
    print(f"  BUY  {e['ts'][:16]}  Rs{e['price']:.2f}  qty={e['qty']}  →  EXIT {x['ts'][:16]}  Rs{x['price']:.2f}  {'+'if x['pnl']>=0 else ''}Rs{x['pnl']:.2f}  {'(OPEN)' if x['open'] else ''}")

# Build HTML
times=[ts.isoformat() for ts in df.index]
D={"sym":SYM,"times":times,"opens":df["Open"].tolist(),"highs":df["High"].tolist(),
   "lows":df["Low"].tolist(),"closes":df["Close"].tolist(),"st":df["st"].tolist(),
   "trends":df["trend"].tolist(),"entries":entries,"exits":exits,"pnl":round(total_pnl,2),"capital":CAPITAL}

html=f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{SYM} Backtest</title>
<script src="https://cdn.plot.ly/plotly-2.26.0.min.js"></script>
<style>body{{background:#0d1117;color:#e6edf3;font-family:monospace;margin:0;padding:10px;}}
#hdr{{display:flex;gap:16px;align-items:center;margin-bottom:6px;font-size:12px;color:#8b949e;}}
#pnl{{font-size:14px;font-weight:bold;margin-left:auto;}}</style></head><body>
<div id="hdr">
  <span><b style="color:#58a6ff">{SYM}</b> · Supertrend ATR=14 Mult=1.5 · 15-min CNC · Last {DAYS} days</span>
  <span>▲ green=BUY &nbsp; ▼ red=SELL/close &nbsp; scroll=zoom drag=pan</span>
  <span id="pnl"></span>
</div>
<div id="chart"></div>
<script>
const D={json.dumps(D)};
document.getElementById('pnl').innerHTML='Total P&L: <span style="color:'+(D.pnl>=0?'#2ecc71':'#e74c3c')+'">'+(D.pnl>=0?'+':'')+'Rs'+D.pnl.toFixed(2)+'</span>';
const stUx=[],stUy=[],stDx=[],stDy=[];
D.trends.forEach((t,i)=>{{
  if(t===1){{stUx.push(D.times[i]);stUy.push(D.st[i]);stDx.push(D.times[i]);stDy.push(null);}}
  else if(t===-1){{stDx.push(D.times[i]);stDy.push(D.st[i]);stUx.push(D.times[i]);stUy.push(null);}}
  else{{stUx.push(D.times[i]);stUy.push(null);stDx.push(D.times[i]);stDy.push(null);}}
}});
const traces=[
  {{type:'candlestick',name:'Price',x:D.times,open:D.opens,high:D.highs,low:D.lows,close:D.closes,
    increasing:{{line:{{color:'#26a641'}},fillcolor:'#26a641'}},decreasing:{{line:{{color:'#e74c3c'}},fillcolor:'#e74c3c'}},xaxis:'x',yaxis:'y'}},
  {{type:'scatter',name:'ST Bull',x:stUx,y:stUy,mode:'lines',line:{{color:'#2ecc71',width:2.5}},connectgaps:false,xaxis:'x',yaxis:'y'}},
  {{type:'scatter',name:'ST Bear',x:stDx,y:stDy,mode:'lines',line:{{color:'#e74c3c',width:2.5}},connectgaps:false,xaxis:'x',yaxis:'y'}},
  {{type:'scatter',name:'BUY',x:D.entries.map(e=>e.ts),y:D.entries.map(e=>e.price),mode:'markers',
    marker:{{symbol:'triangle-up',size:18,color:'#00ff88',line:{{color:'#0d1117',width:1.5}}}},
    text:D.entries.map(e=>`BUY Rs${{e.price.toFixed(2)}} qty=${{e.qty}}`),
    hovertemplate:'%{{text}}<extra></extra>',xaxis:'x',yaxis:'y'}},
  {{type:'scatter',name:'EXIT',x:D.exits.map(e=>e.ts),y:D.exits.map(e=>e.price),mode:'markers',
    marker:{{symbol:'triangle-down',size:18,color:D.exits.map(e=>e.open?'#f39c12':(e.pnl>=0?'#ff4444':'#ff8800')),line:{{color:'#0d1117',width:1.5}}}},
    text:D.exits.map(e=>`EXIT Rs${{e.price.toFixed(2)}} P&L ${{e.pnl>=0?'+':''}}Rs${{e.pnl.toFixed(2)}}${{e.open?' (OPEN)':''}}`),
    hovertemplate:'%{{text}}<extra></extra>',xaxis:'x',yaxis:'y'}},
  {{type:'scatter',name:'Trend',x:D.times,y:D.trends,mode:'lines',line:{{color:'#f7b731',width:2,shape:'hv'}},
    fill:'tozeroy',fillcolor:'rgba(247,183,49,0.10)',xaxis:'x',yaxis:'y2'}}
];
const shapes=[];
D.entries.forEach(e=>shapes.push({{type:'line',x0:e.ts,x1:e.ts,y0:0,y1:1,yref:'paper',line:{{color:'rgba(0,255,136,0.3)',width:1,dash:'dot'}}}}));
D.exits.forEach(e=>shapes.push({{type:'line',x0:e.ts,x1:e.ts,y0:0,y1:1,yref:'paper',line:{{color:'rgba(255,68,68,0.3)',width:1,dash:'dot'}}}}));
Plotly.newPlot('chart',traces,{{
  paper_bgcolor:'#0d1117',plot_bgcolor:'#161b22',font:{{color:'#e6edf3',family:'monospace',size:11}},
  grid:{{rows:2,columns:1,pattern:'independent',roworder:'top to bottom'}},
  xaxis:{{type:'category',rangeslider:{{visible:false}},showgrid:true,gridcolor:'#21262d',tickangle:-55,nticks:20,domain:[0,1],anchor:'y',showspikes:true,spikecolor:'#555',spikethickness:1}},
  yaxis:{{title:'Price (Rs)',showgrid:true,gridcolor:'#21262d',domain:[0.30,1],anchor:'x',showspikes:true,spikecolor:'#555',spikethickness:1}},
  yaxis2:{{title:'Trend',showgrid:true,gridcolor:'#21262d',zeroline:true,zerolinecolor:'#333',tickvals:[-1,0,1],ticktext:['-1 Bear','0','+1 Bull'],domain:[0,0.26],anchor:'x',range:[-1.6,1.6]}},
  legend:{{bgcolor:'#161b22',bordercolor:'#30363d',borderwidth:1,orientation:'h',x:0,y:1.01}},
  hovermode:'x unified',hoverlabel:{{bgcolor:'#21262d',bordercolor:'#30363d',font:{{family:'monospace',size:11}}}},
  margin:{{t:10,b:55,l:65,r:10}},height:720,shapes
}},{{responsive:true,scrollZoom:true,displaylogo:false}});
</script></body></html>"""

out="/home/ec2-user/projects/trading/chart.html"
with open(out,"w") as f: f.write(html)
print(f"\nSaved: {out}")
print("Copy to Mac:  scp ec2-user@<your-ip>:/home/ec2-user/projects/trading/chart.html ~/Desktop/chart.html")
