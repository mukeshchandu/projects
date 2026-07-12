#!/usr/bin/env python3
"""
stock_selector.py
Phase 1: 58-day backtest (MIS + CNC) on full universe → pick best strategy per stock
Phase 2: 5-day recent validation → confirm still profitable
Output: ranked table to decide which stocks to add live
"""
import math, time
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

ATR_PERIOD = 14
MULT       = 1.5
CAPITAL    = 10_000
N_TICKS    = 1

UNIVERSE = [
    "IDEA","SUZLON","YESBANK","NHPC","SAIL","PNB","RPOWER","TATASTEEL",
    "IDFCFIRSTB","HFCL","VEDL","COALINDIA","NATIONALUM","BANKBARODA",
    "UNIONBANK","NMDC","NTPC","ADANIPORTS","ASHOKLEY","COFORGE",
    "ADANIGREEN","BHEL","MPHASIS","INDUSINDBK","SBIN","ICICIBANK",
    "AXISBANK","FEDERALBNK","BANDHANBNK","RBLBANK","CANBK","INDIANB",
    "BANKINDIA","IRFC","RECLTD","PFC","HUDCO","MANAPPURAM","CHOLAFIN",
    "MUTHOOTFIN","BAJFINANCE","BAJAJFINSV","ONGC","BPCL","HINDPETRO",
    "IOC","MRPL","OIL","GAIL","TATAPOWER","ADANITRANS","JSWENERGY",
    "SJVN","TATAMOTORS","TVSMOTOR","MOTHERSON","HINDALCO","JSWSTEEL",
    "TATACHEM","HINDZINC","IRCTC","ZOMATO","INFY","WIPRO","HCLTECH",
    "TECHM","MPHASIS","LTIM","SUNPHARMA","CIPLA","DRREDDY","LT",
    "SIEMENS","ABB","HAVELLS","BHARTIARTL","DELHIVERY","NYKAA",
    "DIXON","VOLTAS","HEROMOTOCO","RELIANCE","HDFCBANK","TCS",
    "PERSISTENT","TATAELXSI","NMDC","POWERGRID","BHEL","NTPC",
    "SILVERBEES","GOLDBEES","NIFTYBEES",
]
UNIVERSE = list(dict.fromkeys(UNIVERSE))  # deduplicate

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
    tr=[max(h[i]-l[i],abs(h[i]-c[i-1]) if i else h[i]-l[i],
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

def backtest(df, start_idx=0, eod_exit=True):
    o,h,l,c = df['Open'].values,df['High'].values,df['Low'].values,df['Close'].values
    ub,lb,st,tr = supertrend(h,l,c)
    pos=None; pending=None; trades=[]
    for i in range(max(1,start_idx), len(c)):
        dt=df.index[i]
        utc_min=(dt.hour if hasattr(dt,'hour') else 10)*60+(dt.minute if hasattr(dt,'minute') else 0)
        ist_min=(utc_min+330)%1440; hr=ist_min//60; mn=ist_min%60
        if hr<9 or (hr==9 and mn<15): continue
        if eod_exit and hr>=15:
            if pos:
                xp=sell_fill(c[i]) if pos['s']=='L' else buy_fill(c[i])
                trades.append((xp-pos['e'] if pos['s']=='L' else pos['e']-xp)*pos['q'])
                pos=None
            pending=None; continue
        if not eod_exit and (hr>15 or (hr==15 and mn>=30)): continue
        if pending and not pos:
            ep=buy_fill(o[i]) if pending=='L' else sell_fill(o[i])
            pos={'s':pending,'e':ep,'q':max(1,int(CAPITAL/ep))}; pending=None; continue
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
    return {'pnl':round(total,2),'wins':wins,'trades':len(trades),
            'win_rate':round(wins/len(trades),3)}

def run(sym):
    try:
        df=yf.download(sym+'.NS',start=datetime.now()-timedelta(days=58),
                       end=datetime.now(),interval='15m',progress=False,auto_adjust=True)
        if df is None or len(df)<30: return None
        if hasattr(df.columns,'levels'): df.columns=df.columns.droplevel(1)
        df=df.dropna().reset_index()

        # Get last 5 trading days cutoff index
        dates=df['Datetime'].apply(lambda x: x.date() if hasattr(x,'date') else x)
        unique_dates=sorted(dates.unique())
        cutoff_date=unique_dates[-5] if len(unique_dates)>=5 else unique_dates[0]
        cutoff_idx=dates[dates>=cutoff_date].index[0]

        df=df.set_index('Datetime')

        # 58-day backtest: MIS vs CNC
        mis_58=backtest(df, start_idx=0, eod_exit=True)
        cnc_58=backtest(df, start_idx=0, eod_exit=False)
        if not mis_58 and not cnc_58: return None

        mis_pnl=mis_58['pnl'] if mis_58 else -999999
        cnc_pnl=cnc_58['pnl'] if cnc_58 else -999999
        best_strategy='CNC' if cnc_pnl>mis_pnl else 'MIS'
        best_58=cnc_58 if best_strategy=='CNC' else mis_58

        # 5-day validation with best strategy
        val=backtest(df, start_idx=cutoff_idx, eod_exit=(best_strategy=='MIS'))

        return {
            'symbol':    sym,
            'strategy':  best_strategy,
            'pnl_58d':   best_58['pnl'],
            'wr_58d':    best_58['win_rate'],
            't_58d':     best_58['trades'],
            'mis_58d':   mis_pnl if mis_58 else 0,
            'cnc_58d':   cnc_pnl if cnc_58 else 0,
            'pnl_5d':    val['pnl'] if val else 0,
            'wr_5d':     val['win_rate'] if val else 0,
            't_5d':      val['trades'] if val else 0,
            'pass':      (best_58['pnl']>0) and (val and val['pnl']>0),
        }
    except: return None

if __name__=='__main__':
    results=[]
    total=len(UNIVERSE)
    for i,sym in enumerate(UNIVERSE):
        r=run(sym)
        if r:
            tag='PASS' if r['pass'] else 'fail'
            print(f"[{i+1:3d}/{total}] {sym:<16} {r['strategy']} 58d=Rs{r['pnl_58d']:+.0f} 5d=Rs{r['pnl_5d']:+.0f}  {tag}")
        else:
            print(f"[{i+1:3d}/{total}] {sym:<16} SKIP")
        if r: results.append(r)
        time.sleep(0.2)

    df=pd.DataFrame(results)
    df=df.sort_values('pnl_58d',ascending=False)
    df.to_csv('stock_selection_results.csv',index=False)

    print('\n' + '='*75)
    print(f"{'Symbol':<14} {'Strat':>5} {'58d_PnL':>9} {'58d_WR':>7} {'5d_PnL':>8} {'5d_WR':>7} {'Status':>6}")
    print('-'*75)
    for r in df.itertuples():
        status='✓ ADD' if r.pass_ else '✗ skip'
        print('%-14s %5s %9.0f %7.0f%% %8.0f %7.0f%% %6s' % (
            r.symbol, r.strategy, r.pnl_58d, r.wr_58d*100,
            r.pnl_5d, r.wr_5d*100, status))

    passed=df[df['pass']==True]
    print(f'\n{"="*75}')
    print(f'READY TO ADD ({len(passed)} stocks):')
    for r in passed.itertuples():
        print(f'  {r.symbol:<16} {r.strategy}  58d=Rs{r.pnl_58d:+.0f}  5d=Rs{r.pnl_5d:+.0f}')
    print(f'\nSaved: stock_selection_results.csv')
