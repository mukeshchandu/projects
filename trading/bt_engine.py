#!/usr/bin/env python3
"""
bt_engine.py — the ONE faithful backtest engine ("paper"), rewritten clean so it replicates
LIVE exactly. Everything (sweeps, our-data, yfinance) runs through this.

Fidelity principles:
  • Uses the REAL SupertrendStrategy — entries, exits, ATR, supertrend, EMA filter, entry
    filters (time window, EMA-gap) are the live code, not a reimplementation.
  • Mirrors the runner's flow: market-open gate (<09:15 dropped) → tick-level stop checks →
    signal ONLY on a closed candle → EOD square at 15:00.
  • Order timing = live: the entry/exit is priced at the quote present WHEN the candle closes
    (first tick past the boundary) or when the stop fires — never a future bar.
  • FILLS AT BEST BID/ASK, never LTP:
        BUY  fills at best ASK (+cross ticks),  SELL fills at best BID (−cross ticks).
    On our tick data we use the real bp1/sp1 (carried forward like the runner's _quote).
    On yfinance (no book) we synthesize ask/bid = close ± spread_ticks·tick.
  • Costs: real MIS charges (STT/exch/SEBI/stamp/GST), no DP (intraday).

Two entry points:
  run_ticks(symbol, ticks, cfg, warm_ticks)  — tick-level (our recorded data) = gold standard
  run_bars (symbol, bars,  cfg, warm_bars)   — bar-level  (yfinance OHLC), stops via bar H/L

A `cfg` is a dict; see DEFAULT_CFG. This module is import-only (no __main__ side effects);
drivers (bt_sweep_all.py) call run_* many times.
"""
from __future__ import annotations
import json, math
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# NSE tiered tick size (same table paper.py uses)
def tick_size(price: float) -> float:
    if   price <=   250: return 0.01
    elif price <= 1_000: return 0.05
    elif price <= 5_000: return 0.10
    elif price <=10_000: return 0.50
    elif price <=20_000: return 1.00
    else:                return 5.00

DEFAULT_CFG = dict(
    interval=900,          # candle seconds
    atr_period=14,
    multiplier=1.5,
    ema_period=50,         # None/0 disables the trend filter
    be_mult=0.5,           # breakeven trigger ×ATR (0 disables)
    trail_mult=1.5,        # chandelier peak-trail ×ATR (0 disables)
    tp_mult=0.0,           # take-profit ×ATR (0 disables)
    entry_start_min=9*60+30,   # no entry on bars starting before this (IST minutes)
    entry_end_min=14*60+15,    # no entry on bars starting after this
    ema_gap_atr=0.3,       # require |close-EMA| >= this×ATR (0 disables)
    long_only=False,
    cap=5000, lev=4,       # own capital ×leverage per trade (P&L scale)
    yf_spread_ticks=1,     # yfinance fill: close ± this×tick (half-spread each side)
)

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)
EOD = (15, 0)


def _charges(entry_val, exit_val):   # MIS round-trip, no DP
    stt   = 0.00025 * exit_val
    stamp = 0.00003 * entry_val
    exch  = 0.0000307 * (entry_val + exit_val)
    sebi  = 0.000001  * (entry_val + exit_val)
    return stt + stamp + exch + sebi + 0.18 * (exch + sebi)


def _mk_strategy(symbol, cfg):
    """Build the REAL strategy with cfg params and push cfg's exit/entry-filter knobs into the
    strategy module globals (that's where they live)."""
    import strategies.supertrend as st
    st.BREAKEVEN_TRIGGER_MULT = cfg["be_mult"]
    st.TRAIL_PEAK_MULT        = cfg["trail_mult"]
    st.TAKE_PROFIT_MULT       = cfg["tp_mult"]
    st.ENTRY_START_MIN        = cfg["entry_start_min"]
    st.ENTRY_END_MIN          = cfg["entry_end_min"]
    st.ENTRY_EMA_GAP_ATR      = cfg["ema_gap_atr"]
    st.SupertrendStrategy.save_state  = lambda self: None   # never touch live state files
    st.SupertrendStrategy._load_state = lambda self: None
    return st.SupertrendStrategy(symbol, 1, atr_period=cfg["atr_period"],
                                 multiplier=cfg["multiplier"], long_only=cfg["long_only"],
                                 ema_period=(cfg["ema_period"] or None))


def _in_session(ts):   return MARKET_OPEN <= (ts.hour, ts.minute) <= MARKET_CLOSE
def _is_eod(ts):       return (ts.hour, ts.minute) >= EOD


def _round_tick(px, t, up):
    n = px / t
    return (math.ceil(round(n, 8)) if up else math.floor(round(n, 8))) * t


# ══════════════════════════════ TICK-LEVEL (our data) ══════════════════════════════
def run_ticks(symbol, ticks, cfg=None, warm_ticks=None):
    """ticks / warm_ticks: list of (ft:int, lp:float|None, bid:float|None, ask:float|None).
    Returns dict(trades=[...], net, ntr, nwin)."""
    from marketdata import CandleBuilder, Tick
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    strat = _mk_strategy(symbol, cfg)

    # warm indicators (feed lp as ticks into a candle builder at cfg interval)
    wb = CandleBuilder(cfg["interval"])
    for ft, lp, _b, _a in (warm_ticks or []):
        if lp is None: continue
        c = wb.update(Tick(ts=datetime.fromtimestamp(ft, tz=IST), symbol="x", ltp=lp))
        if c is not None: strat.on_candle(c)
    strat.position = 0; strat._entry_price = strat._entry_atr = strat._peak = None
    strat._breakeven_armed = False

    b = CandleBuilder(cfg["interval"])
    bid = ask = None; last_lp = None
    pos = None   # (side, entry_px, entry_iso)
    trades = []

    def fill_px(side, ref, cross):
        t = tick_size(ref)
        if side == "BUY":
            base = ask if ask else (last_lp + t if last_lp else ref)
            return round(_round_tick(base, t, True) + cross * t, 4)
        else:
            base = bid if bid else (last_lp - t if last_lp else ref)
            return round(_round_tick(base, t, False) - cross * t, 4)

    def book(side, ep, et, xp, xt, reason):
        qty = max(1, int(cfg["cap"] * cfg["lev"] / ep))
        gross = (xp - ep) * qty if side == "LONG" else (ep - xp) * qty
        net = gross - _charges(ep * qty, xp * qty)
        trades.append(dict(side=side, et=et, ep=round(ep, 2), xt=xt, xp=round(xp, 2),
                           qty=qty, pnl=round(net, 1), reason=reason))

    for ft, lp, b_, a_ in ticks:
        ts = datetime.fromtimestamp(ft, tz=IST)
        if b_: bid = b_
        if a_: ask = a_
        if lp is not None: last_lp = lp
        if not _in_session(ts):
            continue
        price = lp if lp is not None else last_lp
        if price is None:
            continue

        if _is_eod(ts):
            if pos:
                xp = fill_px("SELL" if pos[0] == "LONG" else "BUY", price, 0)
                book(pos[0], pos[1], pos[2], xp, ts.isoformat(), "EOD"); pos = None
            continue

        # tick-level stop management (exit fills at the touch)
        if pos:
            xs = strat.check_stops(price)
            if xs:
                xp = fill_px("SELL" if pos[0] == "LONG" else "BUY", price, 0)
                book(pos[0], pos[1], pos[2], xp, ts.isoformat(),
                     xs["reason"].split("|")[0].strip()); pos = None

        if lp is None:
            continue   # quote-only tick: no candle to build
        c = b.update(Tick(ts=ts, symbol="x", ltp=lp))
        if c is None:
            continue
        for sig in strat.on_candle(c):
            act = sig["action"]
            if act in ("BUY", "SELL") and pos is None:
                ep = fill_px(act, c.close, 0)
                pos = ("LONG" if act == "BUY" else "SHORT", ep, c.start.isoformat())
            elif act == "EXIT" and pos is not None:
                xp = fill_px("SELL" if pos[0] == "LONG" else "BUY", c.close, 0)
                book(pos[0], pos[1], pos[2], xp, c.start.isoformat(),
                     sig["reason"].split("|")[0].strip()); pos = None
    if pos:
        xp = fill_px("SELL" if pos[0] == "LONG" else "BUY", last_lp, 0)
        book(pos[0], pos[1], pos[2], xp, "END", "END")

    net = round(sum(t["pnl"] for t in trades), 1)
    nwin = sum(1 for t in trades if t["pnl"] > 0)
    return dict(trades=trades, net=net, ntr=len(trades), nwin=nwin)


# ══════════════════════════════ BAR-LEVEL (yfinance) ══════════════════════════════
def run_bars(symbol, bars, cfg=None, warm_bars=None):
    """bars / warm_bars: list of (ts:datetime, o,h,l,c). No order book → fill at close ± spread;
    intra-bar stops checked against the bar's High/Low (worst-case ordering: stop before target).
    Only same-interval bars (yfinance already at cfg interval)."""
    from marketdata import Candle
    cfg = {**DEFAULT_CFG, **(cfg or {})}
    strat = _mk_strategy(symbol, cfg)
    for ts, o, h, l, c in (warm_bars or []):
        strat.on_candle(Candle(start=ts.astimezone(IST), open=o, high=h, low=l, close=c))
    strat.position = 0; strat._entry_price = strat._entry_atr = strat._peak = None
    strat._breakeven_armed = False

    trades = []; pos = None
    sp = cfg["yf_spread_ticks"]

    def fill(side, ref):
        t = tick_size(ref)
        return round(ref + sp * t, 4) if side == "BUY" else round(ref - sp * t, 4)

    def book(side, ep, et, xp, xt, reason):
        qty = max(1, int(cfg["cap"] * cfg["lev"] / ep))
        gross = (xp - ep) * qty if side == "LONG" else (ep - xp) * qty
        net = gross - _charges(ep * qty, xp * qty)
        trades.append(dict(side=side, et=et, ep=round(ep, 2), xt=xt, xp=round(xp, 2),
                           qty=qty, pnl=round(net, 1), reason=reason))

    for ts, o, h, l, c in bars:
        ts = ts.astimezone(IST)
        if not _in_session(ts):
            continue
        if _is_eod(ts):
            if pos:
                book(pos[0], pos[1], pos[2], fill("SELL" if pos[0]=="LONG" else "BUY", o),
                     ts.isoformat(), "EOD"); pos = None
            continue
        # intra-bar stop check using the adverse extreme (High for shorts, Low for longs)
        if pos:
            probe = l if pos[0] == "LONG" else h
            xs = strat.check_stops(probe)
            if xs:
                book(pos[0], pos[1], pos[2], fill("SELL" if pos[0]=="LONG" else "BUY", xs["price"]),
                     ts.isoformat(), xs["reason"].split("|")[0].strip()); pos = None
        for sig in strat.on_candle(Candle(start=ts, open=o, high=h, low=l, close=c)):
            act = sig["action"]
            if act in ("BUY", "SELL") and pos is None:
                pos = ("LONG" if act=="BUY" else "SHORT", fill(act, c), ts.isoformat())
            elif act == "EXIT" and pos is not None:
                book(pos[0], pos[1], pos[2], fill("SELL" if pos[0]=="LONG" else "BUY", c),
                     ts.isoformat(), sig["reason"].split("|")[0].strip()); pos = None
    if pos and bars:
        book(pos[0], pos[1], pos[2], fill("SELL" if pos[0]=="LONG" else "BUY", bars[-1][4]), "END", "END")

    net = round(sum(t["pnl"] for t in trades), 1)
    nwin = sum(1 for t in trades if t["pnl"] > 0)
    return dict(trades=trades, net=net, ntr=len(trades), nwin=nwin)


# ── loaders ───────────────────────────────────────────────────────────────
def load_tickfile(path):
    """{symbol: [(ft, lp, bid, ask), ...]} raw per-tick (None where absent), time-sorted."""
    sym_of, rows = {}, {}
    for line in open(path):
        line = line.strip()
        if not line: continue
        try: m = json.loads(line)
        except Exception: continue
        tk = m.get("tk")
        if not tk or not m.get("ft"): continue
        if m.get("ts") and tk not in sym_of: sym_of[tk] = m["ts"].replace("-EQ", "")
        def f(k):
            v = m.get(k)
            try: return float(v) if v not in (None, "", "0", 0) else None
            except (TypeError, ValueError): return None
        rows.setdefault(tk, []).append((int(m["ft"]), f("lp"), f("bp1"), f("sp1")))
    return {sym_of.get(tk, tk): sorted(v, key=lambda x: x[0]) for tk, v in rows.items()}
