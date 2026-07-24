#!/usr/bin/env python3
"""
quant_lab.py — brutal, from-scratch multi-strategy backtest lab (trust no old code).

Tests many strategy TYPES on Nifty-100 yfinance 15-min over ~60 days, each:
  • in its NATIVE form (its own entry+exit), and
  • grafted onto OUR exit (chandelier trail 1.5×ATR + breakeven 0.5×ATR + hard-SL 1.5×ATR),
  • optionally with our entry TIME filter (09:45–14:30),
sweeping each strategy's key variables. Honest: MIS costs + close±spread fills, intraday
(EOD square 15:00, no overnight), and a TRAIN/TEST split so only generalizing configs count.

Strategies output a per-bar DESIRED POSITION (+1/-1/0); the simulator trades it.

Run:  python3 quant_lab.py --fetch      (download+cache Nifty100 15m/60d, once)
      python3 quant_lab.py              (run the full sweep -> quant_results.txt)
"""
from __future__ import annotations
import itertools, os, pickle, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "nifty100_15m.pkl")
OUT   = os.path.join(HERE, "quant_results.txt")

NIFTY = ("ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV "
 "BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK "
 "HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDIGO INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT "
 "M&M MARUTI NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SHRIRAMFIN SBIN "
 "SUNPHARMA TCS TATACONSUM TATASTEEL TECHM TITAN TRENT ULTRACEMCO WIPRO "
 "ABB ADANIENSOL ADANIGREEN ADANIPOWER AMBUJACEM BANKBARODA BPCL BRITANNIA "
 "BOSCHLTD CANBK CGPOWER CHOLAFIN DIVISLAB DLF DMART GAIL GODREJCP HDFCAMC "
 "HAL HINDZINC INDHOTEL IOC IRFC JINDALSTEL LODHA MUTHOOTFIN "
 "PIDILITIND PFC PNB RECLTD MOTHERSON SHREECEM SIEMENS TATAPOWER "
 "TORNTPHARM TVSMOTOR UNIONBANK UNITDSPR VBL VEDL ZYDUSLIFE").split()

IST = "Asia/Kolkata"


# ── data ──────────────────────────────────────────────────────────────────
def fetch():
    import yfinance as yf
    out = {}
    for i, s in enumerate(NIFTY):
        try:
            df = yf.download(s + ".NS", period="60d", interval="15m", progress=False, auto_adjust=False)
            if df is None or len(df) < 200: continue
            if getattr(df.columns, "nlevels", 1) > 1: df.columns = df.columns.get_level_values(0)
            idx = df.index
            try: idx = idx.tz_convert(IST)
            except Exception: idx = idx.tz_localize("UTC").tz_convert(IST)
            df.index = idx
            df = df[["Open","High","Low","Close"]].dropna()
            df = df.between_time("09:15", "15:29")
            if len(df) > 200: out[s] = df
        except Exception as e:
            print(f"  {s}: {e}")
        if i % 20 == 0: print(f"  fetched {i}/{len(NIFTY)} ...")
    pickle.dump(out, open(CACHE, "wb"))
    print(f"cached {len(out)} stocks -> {CACHE}")


# ── indicators ──────────────────────────────────────────────────────────────
def wilder(s, n): return s.ewm(alpha=1/n, adjust=False).mean()
def atr(df, n=14):
    pc = df.Close.shift()
    tr = pd.concat([df.High-df.Low, (df.High-pc).abs(), (df.Low-pc).abs()], axis=1).max(axis=1)
    return wilder(tr, n)
def rsi(c, n=14):
    d = c.diff(); up = d.clip(lower=0); dn = -d.clip(upper=0)
    rs = wilder(up, n) / wilder(dn, n).replace(0, np.nan)
    return 100 - 100/(1+rs)
def supertrend_dir(df, n=10, mult=3.0):
    a = atr(df, n); hl2 = (df.High+df.Low)/2
    up = hl2 + mult*a; dn = hl2 - mult*a
    fu = up.copy(); fl = dn.copy(); dirn = pd.Series(1, index=df.index)
    c = df.Close.values; upv=up.values; dnv=dn.values; fuv=fu.values; flv=fl.values; d=np.ones(len(df),int)
    for i in range(1, len(df)):
        fuv[i] = upv[i] if (upv[i] < fuv[i-1] or c[i-1] > fuv[i-1]) else fuv[i-1]
        flv[i] = dnv[i] if (dnv[i] > flv[i-1] or c[i-1] < flv[i-1]) else flv[i-1]
        if d[i-1] == 1:  d[i] = -1 if c[i] < flv[i-1] else 1
        else:            d[i] =  1 if c[i] > fuv[i-1] else -1
    return pd.Series(d, index=df.index)


# ── strategies: each returns a per-bar desired position (+1/-1/0) ───────────
def st_supertrend(df, p): return supertrend_dir(df, p.get("atr",10), p.get("mult",3.0)).values
def st_ema_cross(df, p):
    f = df.Close.ewm(span=p.get("fast",9), adjust=False).mean()
    s = df.Close.ewm(span=p.get("slow",21), adjust=False).mean()
    return np.where(f > s, 1, -1)
def st_macd(df, p):
    m = df.Close.ewm(span=12,adjust=False).mean() - df.Close.ewm(span=26,adjust=False).mean()
    sig = m.ewm(span=9, adjust=False).mean()
    return np.where(m > sig, 1, -1)
def st_momentum(df, p):
    roc = df.Close.pct_change(p.get("n",20))
    th = p.get("th", 0.0)
    return np.where(roc > th, 1, np.where(roc < -th, -1, 0))
def st_donchian(df, p):
    n = p.get("n",20)
    hh = df.High.rolling(n).max().shift(); ll = df.Low.rolling(n).min().shift()
    pos = np.zeros(len(df), int); cur = 0
    c = df.Close.values; h=hh.values; l=ll.values
    for i in range(len(df)):
        if not np.isnan(h[i]) and c[i] > h[i]: cur = 1
        elif not np.isnan(l[i]) and c[i] < l[i]: cur = -1
        pos[i] = cur
    return pos
def st_bollinger_mr(df, p):
    n=p.get("n",20); k=p.get("k",2.0)
    ma=df.Close.rolling(n).mean(); sd=df.Close.rolling(n).std()
    up=ma+k*sd; lo=ma-k*sd; c=df.Close.values; mav=ma.values; upv=up.values; lov=lo.values
    pos=np.zeros(len(df),int); cur=0
    for i in range(len(df)):
        if cur==0:
            if not np.isnan(lov[i]) and c[i]<lov[i]: cur=1
            elif not np.isnan(upv[i]) and c[i]>upv[i]: cur=-1
        elif cur==1 and c[i]>=mav[i]: cur=0
        elif cur==-1 and c[i]<=mav[i]: cur=0
        pos[i]=cur
    return pos
def st_rsi_mr(df, p):
    n=p.get("n",14); lo=p.get("lo",30); hi=p.get("hi",70)
    r=rsi(df.Close,n).values; pos=np.zeros(len(df),int); cur=0
    for i in range(len(df)):
        if cur==0:
            if r[i]<lo: cur=1
            elif r[i]>hi: cur=-1
        elif cur==1 and r[i]>=50: cur=0
        elif cur==-1 and r[i]<=50: cur=0
        pos[i]=cur
    return pos
def st_vwap_mr(df, p):
    k=p.get("k",0.01)
    date=df.index.date
    tp=(df.High+df.Low+df.Close)/3
    vwap=tp.groupby(date).cummean() if hasattr(tp.groupby(date),"cummean") else tp.groupby(date).transform(lambda x: x.expanding().mean())
    c=df.Close.values; v=vwap.values; pos=np.zeros(len(df),int); cur=0
    for i in range(len(df)):
        if cur==0:
            if c[i] < v[i]*(1-k): cur=1
            elif c[i] > v[i]*(1+k): cur=-1
        elif cur==1 and c[i]>=v[i]: cur=0
        elif cur==-1 and c[i]<=v[i]: cur=0
        pos[i]=cur
    return pos
def st_orb(df, p):
    n=p.get("or_bars",2)  # opening range = first n 15-min bars
    date=df.index.date; pos=np.zeros(len(df),int)
    c=df.Close.values; h=df.High.values; l=df.Low.values
    i=0; N=len(df)
    import itertools as _it
    for d, grp in _it.groupby(range(N), key=lambda j: date[j]):
        idx=list(grp)
        if len(idx)<=n: continue
        orh=max(h[j] for j in idx[:n]); orl=min(l[j] for j in idx[:n])
        cur=0
        for j in idx[n:]:
            if c[j]>orh: cur=1
            elif c[j]<orl: cur=-1
            pos[j]=cur
    return pos

STRATS = {
 "supertrend":   (st_supertrend, [{"atr":10,"mult":3.0},{"atr":14,"mult":1.5},{"atr":10,"mult":2.0},{"atr":20,"mult":3.0}]),
 "ema_cross":    (st_ema_cross,  [{"fast":9,"slow":21},{"fast":5,"slow":20},{"fast":12,"slow":26},{"fast":20,"slow":50}]),
 "macd":         (st_macd,       [{}]),
 "momentum":     (st_momentum,   [{"n":10,"th":0.0},{"n":20,"th":0.0},{"n":20,"th":0.003},{"n":40,"th":0.005}]),
 "donchian":     (st_donchian,   [{"n":10},{"n":20},{"n":40},{"n":55}]),
 "bollinger_mr": (st_bollinger_mr,[{"n":20,"k":2.0},{"n":20,"k":2.5},{"n":10,"k":1.5},{"n":30,"k":2.0}]),
 "rsi_mr":       (st_rsi_mr,     [{"n":14,"lo":30,"hi":70},{"n":7,"lo":20,"hi":80},{"n":14,"lo":25,"hi":75}]),
 "vwap_mr":      (st_vwap_mr,    [{"k":0.005},{"k":0.01},{"k":0.02}]),
 "orb":          (st_orb,        [{"or_bars":1},{"or_bars":2},{"or_bars":4}]),
}


# ── simulator ────────────────────────────────────────────────────────────
def _charges(ev, xv):
    stt=0.00025*xv; stamp=0.00003*ev; exch=0.0000307*(ev+xv); sebi=0.000001*(ev+xv)
    return stt+stamp+exch+sebi+0.18*(exch+sebi)

def simulate(df, pos, exit_mode, spread_ticks=1, time_filter=False,
             cap=5000, lev=4, be=0.5, trail=1.5, hardsl=1.5):
    """exit_mode: 'native' (follow pos) or 'our' (pos triggers entry; chandelier/BE/hardSL/EOD exit)."""
    c=df.Close.values; h=df.High.values; l=df.Low.values
    a=atr(df,14).values
    ts=df.index; date=ts.date; mins=ts.hour*60+ts.minute
    def tick(px): return 0.01 if px<=250 else 0.05 if px<=1000 else 0.10 if px<=5000 else 0.50
    trades=[]
    side=0; ep=0.0; peak=0.0; earmed=False; entry_atr=0.0
    def book(sd, e, x):
        qty=max(1,int(cap*lev/e)); g=(x-e)*qty if sd==1 else (e-x)*qty
        trades.append(g - _charges(e*qty, x*qty))
    for i in range(len(df)):
        if a[i]!=a[i] or c[i]<=0: continue
        eod = mins[i] >= 15*60
        # manage existing position
        if side!=0:
            xprice=None
            if eod: xprice=c[i]
            elif exit_mode=="our":
                adverse = l[i] if side==1 else h[i]
                # hard SL
                if side==1 and adverse <= ep - hardsl*entry_atr: xprice=ep-hardsl*entry_atr
                elif side==-1 and adverse >= ep + hardsl*entry_atr: xprice=ep+hardsl*entry_atr
                else:
                    # breakeven arm + chandelier trail on peak
                    if side==1:
                        peak=max(peak,h[i])
                        if h[i]>=ep+be*entry_atr: earmed=True
                        if earmed and l[i]<=ep: xprice=ep
                        elif trail and l[i]<=peak-trail*entry_atr: xprice=peak-trail*entry_atr
                    else:
                        peak=min(peak,l[i])
                        if l[i]<=ep-be*entry_atr: earmed=True
                        if earmed and h[i]>=ep: xprice=ep
                        elif trail and h[i]>=peak+trail*entry_atr: xprice=peak+trail*entry_atr
            else:  # native: exit when pos leaves current side
                if pos[i]!=side: xprice=c[i]
            if xprice is not None:
                t=tick(xprice); xp=xprice-t*spread_ticks if side==1 else xprice+t*spread_ticks
                book(side, ep, xp); side=0
        # entries: only on a FRESH signal (a transition), never a continuing run — otherwise an
        # always-in strategy re-enters every bar right after our stop exits (churn).
        if side==0 and not eod and pos[i]!=0 and (i==0 or pos[i]!=pos[i-1]):
            if time_filter and not (9*60+45 <= mins[i] <= 14*60+30): continue
            want=pos[i]
            # for 'our' exit, enter on any nonzero signal; for native, same
            t=tick(c[i]); ep = c[i]+t*spread_ticks if want==1 else c[i]-t*spread_ticks
            side=want; peak=h[i] if want==1 else l[i]; earmed=False; entry_atr=a[i]
    net=round(sum(trades),0); n=len(trades); win=sum(1 for x in trades if x>0)
    return net, n, (100.0*win/n if n else 0.0)


def main():
    if "--fetch" in sys.argv or not os.path.exists(CACHE):
        print("fetching Nifty-100 15m/60d ..."); fetch()
        if "--fetch" in sys.argv: return
    data = pickle.load(open(CACHE, "rb"))
    stocks = list(data)
    print(f"loaded {len(stocks)} stocks")
    rows=[]
    for name,(fn,grid) in STRATS.items():
        # precompute signals per (stock, param) once
        for p in grid:
            sigs={s: fn(data[s], p) for s in stocks}
            for exit_mode in ("native","our"):
                for tf in (False, True):
                    tr_net=te_net=ntr=0; wracc=[]
                    for s in stocks:
                        df=data[s]; pos=sigs[s]; mid=len(df)//2
                        # train/test by bar index
                        for lbl,(a0,a1) in (("tr",(0,mid)),("te",(mid,len(df)))):
                            sub=df.iloc[a0:a1]; ps=pos[a0:a1]
                            if len(sub)<50: continue
                            net,n,wr=simulate(sub,ps,exit_mode,time_filter=tf)
                            if lbl=="tr": tr_net+=net
                            else: te_net+=net; ntr+=n; wracc.append((wr,n))
                    W=sum(w*n for w,n in wracc); Ntot=sum(n for _,n in wracc)
                    wr=W/Ntot if Ntot else 0
                    rows.append(dict(strat=name,params=str(p),exit=exit_mode,tf=tf,
                                     train=tr_net,test=te_net,ntr=ntr,wr=round(wr,1)))
        print(f"  done {name}")
    rows.sort(key=lambda r: r["test"], reverse=True)
    with open(OUT,"w") as fh:
        fh.write(f"# {len(rows)} configs | Nifty100 15m/60d | train/test | MIS costs + spread | our-exit=chandelier\n")
        fh.write(f"{'strat':13} {'exit':7} {'tf':3} {'train':>8} {'test':>8} {'trades':>7} {'win%':>6}  params\n")
        for r in rows:
            fh.write(f"{r['strat']:13} {r['exit']:7} {str(r['tf']):3} {r['train']:>8.0f} {r['test']:>8.0f} "
                     f"{r['ntr']:>7} {r['wr']:>6}  {r['params']}\n")
    print(f"\nwrote {OUT}")
    pos=[r for r in rows if r["train"]>0 and r["test"]>0]
    print(f"\nconfigs positive in BOTH train & test: {len(pos)}/{len(rows)}")
    print(f"{'strat':13} {'exit':7} {'tf':3} {'train':>8} {'test':>8} {'win%':>6}  params")
    for r in (pos[:20] if pos else rows[:20]):
        print(f"{r['strat']:13} {r['exit']:7} {str(r['tf']):3} {r['train']:>8.0f} {r['test']:>8.0f} {r['wr']:>6}  {r['params']}")


if __name__ == "__main__":
    main()
