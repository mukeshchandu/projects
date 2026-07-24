#!/usr/bin/env python3
"""
bt_sweep_all.py — parameter sweep on the faithful engine (bt_engine.run_bars) over ~60 days
of yfinance 15-min data, across a basket of liquid names, with a TRAIN/TEST split so we keep
only configs that generalize. Sweeps ATR period × multiplier × EMA period × exit rule.

Writes bt_sweep_results.txt (full ranking) and prints the top configs.
Usage: python3 bt_sweep_all.py
"""
from __future__ import annotations
import itertools, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_engine as E

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "bt_sweep_results.txt")

# ~25 liquid NSE names spanning sectors (yfinance-friendly)
STOCKS = ("RELIANCE TCS INFY HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK LT ITC "
          "HINDUNILVR BHARTIARTL MARUTI TATASTEEL JSWSTEEL HCLTECH TECHM SUNPHARMA "
          "TITAN ADANIENT ADANIPORTS POWERGRID NTPC COALINDIA ETERNAL").split()

WARM_BARS = 60          # bars used to prime ATR/EMA before scoring
GRID = dict(
    atr_period = [7, 10, 14, 21],
    multiplier = [1.0, 1.5, 2.0, 2.5, 3.0],
    ema_period = [0, 20, 50, 100],
    exit       = [("be0.5+tr1.5", 0.5, 1.5, 0.0),   # current live
                  ("be1.0+noTr",  1.0, 0.0, 0.0),   # old exit
                  ("be1.0+tr2.0", 1.0, 2.0, 0.0),
                  ("tp3",         1.0, 0.0, 3.0)],
)


def fetch_15m(sym):
    import yfinance as yf
    df = yf.download(sym + ".NS", period="60d", interval="15m", progress=False, auto_adjust=False)
    if df is None or len(df) == 0:
        return []
    if getattr(df.columns, "nlevels", 1) > 1:
        df.columns = df.columns.get_level_values(0)
    idx = df.index
    try: idx = idx.tz_convert("Asia/Kolkata")
    except Exception: idx = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
    out = []
    for i, ts in enumerate(idx):
        o, h, l, c = (float(df[k].iloc[i]) for k in ("Open", "High", "Low", "Close"))
        if c == c:
            out.append((ts.to_pydatetime(), o, h, l, c))
    return out


def main():
    print(f"fetching 15m/60d for {len(STOCKS)} stocks ...")
    data = {}
    for s in STOCKS:
        bars = fetch_15m(s)
        if len(bars) > WARM_BARS + 100:
            data[s] = bars
    print(f"  usable: {len(data)} stocks")
    if not data:
        print("no data"); return

    # split point (by index) per stock: warm | train | test
    combos = list(itertools.product(GRID["atr_period"], GRID["multiplier"],
                                    GRID["ema_period"], GRID["exit"]))
    print(f"running {len(combos)} configs x {len(data)} stocks ...")
    rows = []
    for k, (atr, mult, ema, (exname, be, tr, tp)) in enumerate(combos):
        cfg = dict(interval=900, atr_period=atr, multiplier=mult, ema_period=ema,
                   be_mult=be, trail_mult=tr, tp_mult=tp,
                   entry_start_min=9*60+30, entry_end_min=14*60+15, ema_gap_atr=0.3)
        tr_net = te_net = ntr = nwin = 0
        for s, bars in data.items():
            warm = bars[:WARM_BARS]
            body = bars[WARM_BARS:]
            mid  = len(body) // 2
            for label, seg in (("train", body[:mid]), ("test", body[mid:])):
                if len(seg) < 30: continue
                r = E.run_bars(s, seg, cfg, warm if label == "train" else bars[:WARM_BARS+mid])
                if label == "train": tr_net += r["net"]
                else:                te_net += r["net"]; ntr += r["ntr"]; nwin += r["nwin"]
        wr = (100.0 * nwin / ntr) if ntr else 0.0
        rows.append(dict(atr=atr, mult=mult, ema=ema, exit=exname,
                         train=round(tr_net,0), test=round(te_net,0),
                         ntr=ntr, wr=round(wr,1)))
        if k % 20 == 0:
            print(f"  {k}/{len(combos)} done")

    # rank: generalizing configs = positive on BOTH halves, by test net
    rows.sort(key=lambda r: r["test"], reverse=True)
    both_pos = [r for r in rows if r["train"] > 0 and r["test"] > 0]
    with open(OUT, "w") as fh:
        fh.write(f"# sweep: {len(combos)} configs x {len(data)} stocks, 15m/60d yfinance, train/test split\n")
        fh.write(f"{'atr':>4} {'mult':>5} {'ema':>4} {'exit':>12} {'train':>8} {'test':>8} {'trades':>7} {'win%':>6}\n")
        for r in rows:
            fh.write(f"{r['atr']:>4} {r['mult']:>5} {r['ema']:>4} {r['exit']:>12} "
                     f"{r['train']:>8.0f} {r['test']:>8.0f} {r['ntr']:>7} {r['wr']:>6}\n")
    print(f"\nwrote {OUT}")
    print(f"\nconfigs positive in BOTH train & test: {len(both_pos)}/{len(rows)}")
    print(f"{'atr':>4} {'mult':>5} {'ema':>4} {'exit':>12} {'train':>8} {'test':>8} {'win%':>6}")
    for r in both_pos[:15]:
        print(f"{r['atr']:>4} {r['mult']:>5} {r['ema']:>4} {r['exit']:>12} "
              f"{r['train']:>8.0f} {r['test']:>8.0f} {r['wr']:>6}")
    print("\ncurrent live config row (atr14 mult1.5 ema50 be0.5+tr1.5):")
    for r in rows:
        if r["atr"]==14 and r["mult"]==1.5 and r["ema"]==50 and r["exit"]=="be0.5+tr1.5":
            print(f"  train {r['train']:.0f}  test {r['test']:.0f}  win% {r['wr']}  (rank {rows.index(r)+1}/{len(rows)})")


if __name__ == "__main__":
    main()
