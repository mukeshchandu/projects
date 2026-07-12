#!/usr/bin/env python3
"""Backtest Supertrend(14,1.5) 15-min on 100 NSE stocks, rank by PnL."""
import math, time
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

ATR_PERIOD = 14
MULT       = 1.5
CAPITAL    = 10_000
N_TICKS    = 1

UNIVERSE = [
    "IDEA","SUZLON","YESBANK","RPOWER","JPPOWER","SPICEJET","NHPC","SJVN",
    "NBCC","RVNL","IRFC","HUDCO","SAIL","NMDC","MOIL","NATIONALUM","HINDCOPPER",
    "PNB","BANKBARODA","UNIONBANK","CANBK","INDIANB","BANKINDIA","IOB",
    "IDFCFIRSTB","BANDHANBNK","RBLBANK","HFCL","GMRINFRA",
    "TATASTEEL","JSWSTEEL","HINDALCO","VEDL","COALINDIA",
    "NTPC","TATAPOWER","ADANIGREEN","JSWENERGY","ADANIPORTS","ADANITRANS",
    "BPCL","HINDPETRO","IOC","MRPL","OIL","GAIL","ONGC","BHEL",
    "POWERGRID","RECLTD","PFC","SBIN","FEDERALBNK",
    "TATAMOTORS","ASHOKLEY","MOTHERSON","TVSMOTOR",
    "TATACHEM","HINDZINC","IRCTC","ZOMATO",
    "CHOLAFIN","MANAPPURAM","MUTHOOTFIN",
    "ICICIBANK","AXISBANK","INDUSINDBK","BAJFINANCE","BAJAJFINSV",
    "HCLTECH","WIPRO","TECHM","INFY","MPHASIS","COFORGE","LTIM",
    "SUNPHARMA","CIPLA","DRREDDY","LT","SIEMENS","ABB","HAVELLS",
    "BHARTIARTL","DELHIVERY","NYKAA","PAYTM","INDIGO",
    "DIXON","VOLTAS","WHIRLPOOL","HEROMOTOCO","RELIANCE","HDFCBANK","TCS",
    "PERSISTENT","TATAELXSI","UPL","PIDILITIND","BERGEPAINT","UNITDSPR",
]

def get_tick(p):
    if p<=250: return 0.01
    if p<=1000: return 0.05
    if p<=5000: return 0.10
    if p<=10000: return 0.50
    if p<=20000: return 1.00
    return 5.00

def buy_fill(p):
    t=get_tick(p); return round((math.ceil(round(p/t,8))+N_TICKS)*t,4)
def sell_fill(p):
    t=get_tick(p); return round((math.floor(round(p/t,8))-N_TICKS)*t,4)

def supertrend(h,l,c):
    n=len(c)
    tr=[max(h[i]-l[i], abs(h[i]-c[i-1]) if i else h[i]-l[i],
            abs(l[i]-c[i-1]) if i else h[i]-l[i]) for i in range(n)]
    atr=[0.0]*n
    if n>=ATR_PERIOD:
        atr[ATR_PERIOD-1]=sum(tr[:ATR_PERIOD])/ATR_PERIOD
        for i in range(ATR_PERIOD,n):
            atr[i]=(atr[i-1]*(ATR_PERIOD-1)+tr[i])/ATR_PERIOD
    ub=[((h[i]+l[i])/2)+MULT*atr[i] for i in range(n)]
    lb=[((h[i]+l[i])/2)-MULT*atr[i] for i in range(n)]
    st=[0.0]*n; tr2=[0]*n
    for i in range(1,n):
        if not atr[i]: continue
        ub[i]=min(ub[i],ub[i-1]) if c[i-1]<=ub[i-1] else ub[i]
        lb[i]=max(lb[i],lb[i-1]) if c[i-1]>=lb[i-1] else lb[i]
        st[i]=(ub[i] if c[i]<=ub[i] else lb[i]) if st[i-1]==ub[i-1] else (lb[i] if c[i]>=lb[i] else ub[i])
        tr2[i]=1 if c[i]>st[i] else -1
    return ub,lb,st,tr2

def run(sym):
    try:
        yfn = sym.replace("&","")+".NS"
        df = yf.download(yfn, start=datetime.now()-timedelta(days=58),
                         end=datetime.now(), interval="15m",
                         progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"): df.columns = df.columns.droplevel(1)
        if df is None or len(df)<60: return None
        df=df.dropna()
        o,h,l,c=df['Open'].values,df['High'].values,df['Low'].values,df['Close'].values
        ub,lb,st,tr=supertrend(h,l,c)
        pos=None; pending=None; trades=[]
        for i in range(1,len(c)):
            dt=df.index[i]
            # timestamps are UTC — convert to IST (+5:30)
            utc_min = (dt.hour if hasattr(dt,'hour') else 10)*60 + (dt.minute if hasattr(dt,'minute') else 0)
            ist_min = (utc_min + 330) % 1440
            hr = ist_min // 60
            mn = ist_min % 60
            if hr<9 or (hr==9 and mn<15): continue
            if hr>=15:
                if pos:
                    xp=sell_fill(c[i]) if pos['s']=='L' else buy_fill(c[i])
                    trades.append((xp-pos['e'] if pos['s']=='L' else pos['e']-xp)*pos['q'])
                    pos=None
                pending=None; continue
            if pending and not pos:
                ep=buy_fill(o[i]) if pending=='L' else sell_fill(o[i])
                pos={'s':pending,'e':ep,'q':max(1,int(CAPITAL/ep))}
                pending=None; continue
            if pos:
                if pos['s']=='L' and c[i]<lb[i]:
                    trades.append((sell_fill(lb[i])-pos['e'])*pos['q']); pos=None
                elif pos['s']=='S' and c[i]>ub[i]:
                    trades.append((pos['e']-buy_fill(ub[i]))*pos['q']); pos=None
                if pos and tr[i]!=tr[i-1] and tr[i]:
                    if pos['s']=='L' and tr[i]==-1:
                        trades.append((sell_fill(c[i])-pos['e'])*pos['q']); pos=None; pending='S'
                    elif pos['s']=='S' and tr[i]==1:
                        trades.append((pos['e']-buy_fill(c[i]))*pos['q']); pos=None; pending='L'
            else:
                if tr[i]==1 and tr[i-1]!=1: pending='L'
                elif tr[i]==-1 and tr[i-1]!=-1: pending='S'
        if not trades: return None
        total=sum(trades); wins=sum(1 for p in trades if p>0)
        cum=pk=dd=0
        for p in trades:
            cum+=p; pk=max(pk,cum); dd=max(dd,pk-cum)
        return {'symbol':sym,'trades':len(trades),'wins':wins,
                'win_rate':round(wins/len(trades),3),
                'total_pnl':round(total,2),
                'pnl_per_trade':round(total/len(trades),2),
                'max_drawdown':round(-dd,2)}
    except: return None

if __name__=="__main__":
    results=[]
    for i,sym in enumerate(UNIVERSE):
        r=run(sym)
        tag=f"Rs{r['total_pnl']:+.0f} {r['trades']}t" if r else "SKIP"
        print(f"[{i+1:3d}/{len(UNIVERSE)}] {sym:<16}{tag}")
        if r: results.append(r)
        time.sleep(0.25)
    if results:
        df=pd.DataFrame(results).sort_values('total_pnl',ascending=False)
        df.to_csv("backtest_universe_results.csv",index=False)
        print(f"\n{'='*55}")
        print(df[['symbol','trades','win_rate','total_pnl','pnl_per_trade']].to_string(index=False))
        print(f"\nSaved: backtest_universe_results.csv")
        print(f"\nTOP 20:")
        for r in df.head(20).itertuples():
            print(f"  {r.symbol:<16} Rs{r.total_pnl:+7.0f}  win={r.win_rate:.0%}  {r.trades}t")
