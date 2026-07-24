#!/usr/bin/env python3
"""
obi_lab.py — does ORDER-BOOK IMBALANCE (OBI) predict the next price move, on our real
level-1 tick book? This is the one strategy class we can test on real data (yfinance has no
book). Decisive question first: PREDICTIVE POWER. If OBI doesn't forecast the forward mid
move, no execution scheme (taker or maker) can help.

OBI = (bid_qty - ask_qty) / (bid_qty + ask_qty)   in [-1, +1]  (+ = buy pressure)
For every full-book tick we measure the forward MID return over H seconds and relate it to OBI.

Reports (pooled across all stocks/days):
  • IC  = corr(OBI, forward mid return)
  • hit rate = P(sign(OBI)==sign(fwd move)) for strong OBI
  • forward move (bps) by OBI bucket  vs  the typical half-spread (the taker hurdle)
  • a TAKER P&L sim (enter at ask/bid on strong OBI, exit H s later) net of spread+costs
Run: python3 obi_lab.py
"""
import glob, json, os
import numpy as np

HORIZONS = [15, 30, 60]      # seconds to look forward
THR = 0.5                    # |OBI| threshold for "strong"


def load_fullbook(path):
    """{symbol: (ft[], mid[], obi[], bid[], ask[])} for ticks with full L1 book."""
    sym_of, rows = {}, {}
    for line in open(path):
        line=line.strip()
        if not line: continue
        try: m=json.loads(line)
        except Exception: continue
        tk=m.get("tk")
        if not tk or not m.get("ft"): continue
        if m.get("ts") and tk not in sym_of: sym_of[tk]=m["ts"].replace("-EQ","")
        try:
            bp=float(m["bp1"]); sp=float(m["sp1"]); bq=float(m["bq1"]); sq=float(m["sq1"])
        except (KeyError, TypeError, ValueError):
            continue
        if bp<=0 or sp<=0 or sp<bp or (bq+sq)<=0: continue
        # drop absurd/stale quotes: spread > 1% of mid (garbage touchline snapshots)
        mid=(bp+sp)/2
        if (sp-bp)/mid > 0.01: continue
        obi=(bq-sq)/(bq+sq)
        rows.setdefault(tk,[]).append((int(m["ft"]), mid, obi, bp, sp))
    out={}
    for tk,v in rows.items():
        v.sort(key=lambda x:x[0])
        a=np.array(v, float)
        out[sym_of.get(tk,tk)] = a   # cols: ft, mid, obi, bid, ask
    return out


def main():
    days=sorted(glob.glob("data/*/ticks.jsonl"))
    # accumulators per horizon
    OBI=[]; FWD={h:[] for h in HORIZONS}; SPREAD=[]
    # taker sim accumulators per horizon
    taker={h:{"pnl":0.0,"n":0,"win":0} for h in HORIZONS}
    nfb=0
    for p in days:
        book=load_fullbook(p)
        for sym,a in book.items():
            if len(a)<50: continue
            ft=a[:,0]; mid=a[:,1]; obi=a[:,2]; bid=a[:,3]; ask=a[:,4]
            nfb+=len(a)
            half_sp_bps = (ask-bid)/2/mid*1e4
            for h in HORIZONS:
                j = np.searchsorted(ft, ft+h, side="left")
                valid = j < len(ft)
                fwd = np.full(len(ft), np.nan)
                fwd[valid] = (mid[j[valid]]-mid[valid])/mid[valid]*1e4   # bps
                ok = ~np.isnan(fwd)
                if h==HORIZONS[0]:
                    OBI.append(obi[ok]); SPREAD.append(half_sp_bps[ok])
                FWD[h].append(fwd[ok] if h==HORIZONS[0] else fwd[ok])
                # store OBI aligned per horizon
                FWD.setdefault(("obi",h),[]).append(obi[ok])
                # TAKER sim: strong OBI -> cross the spread, exit at mid h sec later
                strong = ok & (np.abs(obi) >= THR)
                idx = np.where(strong)[0]
                for k in idx:
                    side = 1 if obi[k]>0 else -1
                    entry = ask[k] if side==1 else bid[k]      # pay the spread on entry
                    exitp = mid[j[k]]                          # exit at mid (optimistic for taker)
                    ret = (exitp-entry)/entry*side
                    # round-trip cost ~ MIS: STT .025% sell + exch/sebi/gst ~ .01%; approx 3.5 bps
                    pnl = (ret - 0.00035)
                    taker[h]["pnl"]+=pnl; taker[h]["n"]+=1; taker[h]["win"]+= (pnl>0)

    print(f"full-book ticks used: {nfb}\n")
    allobi=np.concatenate(OBI); allsp=np.concatenate(SPREAD)
    print(f"typical HALF-spread (taker hurdle): median {np.median(allsp):.1f} bps  mean {allsp.mean():.1f} bps\n")
    print(f"{'H(s)':>5} {'IC':>7} {'hit%(|OBI|>0.5)':>16} {'fwd bps OBI>+.5':>16} {'fwd bps OBI<-.5':>16}")
    for h in HORIZONS:
        obia=np.concatenate(FWD[("obi",h)]); fwda=np.concatenate(FWD[h])
        ic=np.corrcoef(obia,fwda)[0,1]
        strong=np.abs(obia)>=THR
        hit=np.mean(np.sign(obia[strong])==np.sign(fwda[strong]))*100 if strong.sum() else 0
        up=fwda[obia>=THR].mean() if (obia>=THR).sum() else float('nan')
        dn=fwda[obia<=-THR].mean() if (obia<=-THR).sum() else float('nan')
        print(f"{h:>5} {ic:>7.3f} {hit:>15.1f}% {up:>16.2f} {dn:>16.2f}")
    print(f"\nTAKER sim (cross spread on |OBI|>{THR}, exit at mid; net ~3.5bps cost):")
    print(f"{'H(s)':>5} {'trades':>8} {'win%':>6} {'avg bps/trade':>14} {'total %':>9}")
    for h in HORIZONS:
        t=taker[h]; n=t["n"]
        if n:
            print(f"{h:>5} {n:>8} {100*t['win']/n:>5.1f}% {t['pnl']/n*1e4:>13.2f} {t['pnl']*100:>8.1f}%")
    print("\nread: OBI has edge only if fwd-bps for strong OBI clearly exceeds the half-spread,")
    print("and IC is meaningfully >0. Taker total>0 => tradeable as taker; else only maker (hard).")


if __name__ == "__main__":
    main()
