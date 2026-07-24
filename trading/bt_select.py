#!/usr/bin/env python3
"""
Dynamic daily stock-selection backtest (walk-forward).
Each trading day, rank a ~100-stock universe over the PAST N days and trade only the
top-K that day (MIS, m1.5, long+short, EMA-50 filter). Selection uses only past data
=> inherently out-of-sample. MIS squares daily, so each day's P&L is independent, which
makes the config sweep exact and cheap (precompute per-day net once, then just pick).

Sweeps lookback N in {1,2,3,5,10} x 3 selection metrics x K. ~50 trade days.
"""
from __future__ import annotations
import json, os, sys, time, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
from config import IST
from marketdata import Candle
from strategies.supertrend import SupertrendStrategy
from bt_yahoo import fill_price, round_trip_cost, is_eod
from bt_regime import compute_adx, compute_ema

SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

# Official NIFTY 100 (Nifty 50 + Next 50). Yahoo-unavailable names self-prune at download.
UNIVERSE = (
    "ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV "
    "BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK "
    "HDFCLIFE HINDALCO HINDUNILVR ICICIBANK INDIGO INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT "
    "M&M MARUTI MAXHEALTH NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SHRIRAMFIN SBIN "
    "SUNPHARMA TCS TATACONSUM TMPV TATASTEEL TECHM TITAN TRENT ULTRACEMCO WIPRO "
    "ABB ADANIENSOL ADANIGREEN ADANIPOWER AMBUJACEM BAJAJHLDNG BANKBARODA BPCL BRITANNIA "
    "BOSCHLTD CANBK CGPOWER CHOLAFIN CUMMINSIND DIVISLAB DLF DMART GAIL GODREJCP HDFCAMC "
    "HAL HINDZINC HYUNDAI INDHOTEL IOC IRFC JINDALSTEL LODHA MAZDOCK MUTHOOTFIN "
    "PIDILITIND PFC PNB RECLTD MOTHERSON SHREECEM SIEMENS SOLARINDS TATACAP TMCV TATAPOWER "
    "TORNTPHARM TVSMOTOR UNIONBANK UNITDSPR VBL VEDL ZYDUSLIFE").split()

CACHE = os.path.join(os.path.dirname(__file__), "bars_universe.json")


def download_all():
    if os.path.exists(CACHE):
        from datetime import datetime
        raw = json.load(open(CACHE))
        return {s: [(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
                for s, rows in raw.items() if rows}
    out = {}
    tickers = [s + ".NS" for s in UNIVERSE]
    for i in range(0, len(tickers), 25):
        chunk = tickers[i:i+25]
        try:
            df = yf.download(chunk, period="60d", interval="15m", group_by="ticker",
                             progress=False, auto_adjust=False, threads=True)
        except Exception as e:
            print("  chunk fail", e); continue
        for tk in chunk:
            sym = tk[:-3]
            try:
                sub = df[tk] if len(chunk) > 1 else df
                idx = sub.index
                try:
                    idx = idx.tz_convert("Asia/Kolkata")
                except Exception:
                    idx = idx.tz_localize("UTC").tz_convert("Asia/Kolkata")
                rows = []
                for j, ts in enumerate(idx):
                    o, h, l, c = (float(sub["Open"].iloc[j]), float(sub["High"].iloc[j]),
                                  float(sub["Low"].iloc[j]), float(sub["Close"].iloc[j]))
                    if o != o or c != c:
                        continue
                    rows.append((ts.to_pydatetime(), o, h, l, c))
                if rows:
                    out[sym] = rows
            except Exception:
                continue
        time.sleep(1)
    json.dump({s: [[b[0].isoformat(), b[1], b[2], b[3], b[4]] for b in rows]
               for s, rows in out.items()}, open(CACHE, "w"))
    return out


def per_day_metrics(bars, mult=1.5, ema_align=True, capital=5000):
    """Return dicts keyed by date: strat net, range%, adx — for one symbol."""
    ema = compute_ema(bars) if ema_align else None
    adx = compute_adx(bars)
    st_mod = sys.modules["strategies.supertrend"]
    st_mod.BREAKEVEN_TRIGGER_MULT = 1.0; st_mod.TRAIL_PEAK_MULT = 0.0; st_mod.TAKE_PROFIT_MULT = 0.0
    strat = SupertrendStrategy("X", qty=1, multiplier=mult, long_only=False)
    pos = None; prev_eod = False
    net_by_day = {}; day_ohlc = {}; adx_by_day = {}

    def openp(side, px):
        nonlocal pos
        ef = fill_price("BUY" if side == "LONG" else "SELL", px)
        pos = {"side": side, "ef": ef, "qty": max(1, int(capital/ef))}

    def closep(px, d):
        nonlocal pos
        if pos is None:
            return
        xf = fill_price("SELL" if pos["side"] == "LONG" else "BUY", px)
        q = pos["qty"]
        g = (xf-pos["ef"])*q if pos["side"] == "LONG" else (pos["ef"]-xf)*q
        net_by_day[d] = net_by_day.get(d, 0.0) + (g - round_trip_cost(pos["ef"]*q, xf*q, "MIS"))
        pos = None

    for i, (dt, o, h, l, c) in enumerate(bars):
        d = dt.date()
        ov = day_ohlc.get(d)
        if ov is None:
            day_ohlc[d] = [o, h, l, c]
        else:
            ov[1] = max(ov[1], h); ov[2] = min(ov[2], l); ov[3] = c
        if adx[i] is not None:
            adx_by_day[d] = adx[i]
        if is_eod(dt):
            if pos is not None and not prev_eod:
                closep(c, d)
            prev_eod = True
            continue
        prev_eod = False
        for sig in strat.on_candle(Candle(start=dt.astimezone(IST), open=o, high=h, low=l, close=c)):
            a = sig["action"]
            if a in ("BUY", "SELL") and pos is None:
                ok = True
                if ema is not None and ema[i] is not None:
                    ok = c > ema[i] if a == "BUY" else c < ema[i]
                if ok:
                    openp("LONG" if a == "BUY" else "SHORT", sig["price"])
            elif a == "EXIT" and pos is not None:
                closep(sig["price"], d)
    if pos is not None:
        closep(bars[-1][4], bars[-1][0].date())
    rng = {d: (v[1]-v[2])/v[0]*100 for d, v in day_ohlc.items() if v[0]}
    return net_by_day, rng, adx_by_day


def main():
    print("Downloading ~100-stock universe (cached after first run)...")
    bars = download_all()
    print(f"  {len(bars)} stocks with data\n  computing per-day metrics...")

    NET, RNG, ADX = {}, {}, {}
    for sym, b in bars.items():
        NET[sym], RNG[sym], ADX[sym] = per_day_metrics(b)

    # global trading-date axis
    all_dates = sorted({d for sym in NET for d in NET[sym]} |
                       {d for sym in RNG for d in RNG[sym]})
    trade_days = all_dates[10:]              # reserve first 10 for lookback
    print(f"  {len(all_dates)} session-days total | trading days 11..{len(all_dates)} = {len(trade_days)} days\n")

    def score(sym, metric, lb_dates):
        vals = []
        for d in lb_dates:
            if metric == "vol":
                if d in RNG.get(sym, {}): vals.append(RNG[sym][d])
            elif metric == "adx":
                if d in ADX.get(sym, {}): vals.append(ADX[sym][d])
            elif metric == "pnl":
                vals.append(NET.get(sym, {}).get(d, 0.0))
        if not vals:
            return None
        return sum(vals) if metric == "pnl" else sum(vals)/len(vals)

    def selective_net(metric, N, K):
        daily = []
        for di, D in enumerate(trade_days):
            gi = all_dates.index(D)
            lb = all_dates[max(0, gi-N):gi]
            ranked = [(score(s, metric, lb), s) for s in NET]
            ranked = [(sc, s) for sc, s in ranked if sc is not None]
            ranked.sort(reverse=True)
            picked = [s for _, s in ranked[:K]]
            daily.append(sum(NET.get(s, {}).get(D, 0.0) for s in picked))
        return daily

    def stats(daily):
        h = len(daily)//2
        return sum(daily), sum(daily[:h]), sum(daily[h:])

    # baseline: trade the whole universe every day
    base = [sum(NET.get(s, {}).get(D, 0.0) for s in NET) for D in trade_days]
    bt, b1, b2 = stats(base)
    print(f"BASELINE (trade ALL ~{len(NET)} every day): NET Rs{bt:+.0f}  (1st {b1:+.0f} / 2nd {b2:+.0f})\n")

    print("SELECTIVE — top K=5 each day, by metric x lookback N:")
    print(f"  {'metric':>8} {'N':>3} | {'FULL':>10} | {'1st half':>9} | {'2nd half':>9} | robust")
    print("  " + "-"*58)
    best = None
    for metric in ("vol", "adx", "pnl"):
        for N in (1, 2, 3, 5, 10):
            t, s1, s2 = stats(selective_net(metric, N, 5))
            rob = "OK both+" if (s1 > 0 and s2 > 0) else ""
            print(f"  {metric:>8} {N:>3} | Rs{t:>+7.0f} | Rs{s1:>+6.0f} | Rs{s2:>+6.0f} | {rob}")
            if best is None or t > best[0]:
                best = (t, metric, N)
        print()

    print(f"BEST: metric={best[1]} N={best[2]} -> Rs{best[0]:+.0f}")
    print(f"\nK-sweep at best (metric={best[1]}, N={best[2]}):")
    for K in (3, 5, 10, 20):
        t, s1, s2 = stats(selective_net(best[1], best[2], K))
        print(f"  K={K:>2}: NET Rs{t:+.0f}  (1st {s1:+.0f} / 2nd {s2:+.0f})")
    print("\n(Sized Rs5000/trade, 1x; MIS 4x leverage scales ~4x. Selection uses PAST data only = walk-forward OOS.)")


if __name__ == "__main__":
    main()
