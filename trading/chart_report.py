#!/usr/bin/env python3
"""
Build a self-contained HTML report (Plotly, no server needed) for the last N fully-
instrumented live trading days: 1-min candles rebuilt from raw ticks, the actual
production 15-min Supertrend series (from logs/strategy_<date>.csv, real live state),
a freshly-recomputed 1-min Supertrend for comparison, live trade markers/table, and a
15-min-vs-1-min trend-flip (0/1) comparison chart per symbol.

Never touches data/st_state/*.json (the LIVE strategy state) — SupertrendStrategy's
disk persistence is monkey-patched off before any instance is created, exactly like
the existing bt_*.py backtest scripts do.

Usage: python3 chart_report.py [DATE ...]   (defaults to every date with a strategy_<date>.csv)
       writes chart_report.html next to this script.
"""
from __future__ import annotations
import csv, json, os, sys, warnings
from collections import OrderedDict, defaultdict
from datetime import datetime
warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config import IST                              # noqa: E402
from marketdata import Candle                        # noqa: E402
from strategies.supertrend import SupertrendStrategy  # noqa: E402

# ── Safety: never let this script write/read live strategy state ───────────
SupertrendStrategy.save_state = lambda self: None
SupertrendStrategy._load_state = lambda self: None

ATR_PERIOD, MULT = 14, 1.5
MARKET_OPEN, MARKET_CLOSE = (9, 15), (15, 0)   # (hour, minute) — clip everything outside this
OUT_HTML = os.path.join(HERE, "chart_report.html")


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _in_market_hours(dt):
    hm = (dt.hour, dt.minute)
    return MARKET_OPEN <= hm < MARKET_CLOSE


def _in_market_hours_str(time_str):
    """time_str like '09:00' or '09:00:00' (no date)."""
    h, m = int(time_str[:2]), int(time_str[3:5])
    return MARKET_OPEN <= (h, m) < MARKET_CLOSE


def load_ticks(date):
    """token/symbol -> list of (epoch_secs, price), from data/<date>/ticks.jsonl.

    Only the occasional full-snapshot 'tk' messages carry a 'ts' (tradingsymbol) field;
    the far more frequent 'tf' (fast/touchline) updates carry only the token ('tk' key,
    confusingly the same key name as the message type). So symbol names have to be
    resolved via a token->symbol map built from whichever messages do have 'ts', and then
    applied to every message (both types) by token — otherwise almost all ticks are
    silently dropped and candles come out nearly empty.
    """
    path = os.path.join(HERE, "data", date, "ticks.jsonl")
    by_symbol = defaultdict(list)
    if not os.path.exists(path):
        return by_symbol
    raw = []
    token_to_symbol = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("t") not in ("tk", "tf"):
                continue
            token = msg.get("tk")
            if not token:
                continue
            ts_field = msg.get("ts", "")
            if ts_field.endswith("-EQ"):
                token_to_symbol[token] = ts_field[:-3]
            raw.append(msg)
    excluded = defaultdict(list)   # symbol -> [(dt_ist, price), ...] dropped for being outside market hours
    for msg in raw:
        symbol = token_to_symbol.get(msg.get("tk"))
        if not symbol:
            continue
        ft = msg.get("ft")
        lp = msg.get("lp") or msg.get("c")
        if not ft or not lp:
            continue
        try:
            ft_i, px = int(ft), float(lp)
        except (TypeError, ValueError):
            continue
        dt = datetime.fromtimestamp(ft_i, tz=IST)
        if not _in_market_hours(dt):
            excluded[symbol].append((_fmt(dt), px))
            continue
        by_symbol[symbol].append((ft_i, px))
    return by_symbol, excluded


def build_1m_candles(ticks):
    """[(epoch, price), ...] (in file order == time order) -> [(dt_ist, o,h,l,c), ...]."""
    buckets = OrderedDict()
    for ft, px in ticks:
        b = (ft // 60) * 60
        if b not in buckets:
            buckets[b] = [px, px, px, px]
        else:
            c = buckets[b]
            c[1] = max(c[1], px); c[2] = min(c[2], px); c[3] = px
    out = []
    for b in sorted(buckets):
        o, h, l, c = buckets[b]
        out.append((datetime.fromtimestamp(b, tz=IST), o, h, l, c))
    return out


def run_supertrend(symbol, candles_1m):
    """Feed 1-min candles through the real strategy class; return per-candle series."""
    strat = SupertrendStrategy(symbol, qty=1, atr_period=ATR_PERIOD, multiplier=MULT)
    strat._reset_all()
    series = []
    for dt, o, h, l, c in candles_1m:
        strat.on_candle(Candle(start=dt, open=o, high=h, low=l, close=c))
        series.append({"t": _fmt(dt), "o": o, "h": h, "l": l, "c": c,
                       "trend": strat._trend, "st": strat._supertrend})
    return series


def load_strategy_csv(date):
    """logs/strategy_<date>.csv -> {symbol: [ {t,o,h,l,c,atr,trend,st,ema,signal,reason}, ... ]}."""
    path = os.path.join(HERE, "logs", f"strategy_{date}.csv")
    out = defaultdict(list)
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for row in csv.DictReader(f):
            hhmm = row["time"][-5:]
            if not _in_market_hours_str(hhmm):
                continue
            try:
                out[row["symbol"]].append({
                    "t": row["time"] + ":00",
                    "o": float(row["open"]) if row["open"] else None,
                    "h": float(row["high"]) if row["high"] else None,
                    "l": float(row["low"]) if row["low"] else None,
                    "c": float(row["close"]) if row["close"] else None,
                    "trend": int(row["trend"]) if row["trend"] else 0,
                    "signal": row["signal"], "reason": row["reason"],
                })
            except ValueError:
                continue
    return out


def load_trades_csv(date):
    """runner.py stamps entry_time (and any on-candle-close exit) as the 15-min candle's
    START time, not when it actually closed and the order fired — so every logged time is
    ~15 min earlier than the real decision/fill moment. 'decision_time' below is that
    correction (start + 15m), shown alongside the raw logged time so the gap is visible
    rather than silently papered over. A raw entry_time outside market hours entirely
    (e.g. 06:xx) means the entry was driven by the pre-market stale-snapshot bug, not a
    real flip — flagged as phantom_entry rather than time-corrected."""
    path = os.path.join(HERE, "logs", f"trades_{date}.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                entry_hhmm = row["entry_time"]
                phantom = not _in_market_hours_str(entry_hhmm)
                out.append({
                    "symbol": row["symbol"], "side": row["side"],
                    "entry_time": f"{date} {entry_hhmm}:00",
                    "entry_price": float(row["entry_price"]),
                    "exit_time": f"{date} {row['exit_time']}:00",
                    "exit_price": float(row["exit_price"]),
                    "qty": int(row["qty"]), "gross_pnl": float(row["gross_pnl"]),
                    "cost": float(row["cost"]), "net_pnl": float(row["net_pnl"]),
                    "phantom_entry": phantom,
                })
            except (KeyError, ValueError):
                continue
    return out


def basket_for(date):
    path = os.path.join(HERE, "logs", f"runner_{date}.log")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        for line in f:
            idx = line.find("DYNAMIC BASKET")
            if idx == -1:
                continue
            rest = line[idx:]
            paren_close = rest.find(")")
            after_paren = rest[paren_close + 1:]           # ": HCLTECH,LODHA,... | EMA filter=50"
            syms_part = after_paren.split(":", 1)[1] if ":" in after_paren else after_paren
            syms_part = syms_part.split("|")[0].strip()
            return [s.strip() for s in syms_part.split(",") if s.strip()]
    return []


def build_day(date):
    print(f"  {date} ...")
    basket = basket_for(date)
    strat15 = load_strategy_csv(date)
    trades = load_trades_csv(date)
    if not basket:
        basket = sorted(strat15.keys())
    ticks, excluded = load_ticks(date)

    symbols = {}
    for sym in basket:
        c1m = build_1m_candles(ticks.get(sym, []))
        series_1m = run_supertrend(sym, c1m)
        exc = excluded.get(sym, [])
        symbols[sym] = {
            "candles_1m": series_1m,
            "series_15m": strat15.get(sym, []),
            "trades": [t for t in trades if t["symbol"] == sym],
            "excluded_premarket": exc[:5],   # sample, just to show what was dropped and why
            "excluded_count": len(exc),
        }
    net = sum(t["net_pnl"] for t in trades)
    xrange = [f"{date} {MARKET_OPEN[0]:02d}:{MARKET_OPEN[1]:02d}:00",
              f"{date} {MARKET_CLOSE[0]:02d}:{MARKET_CLOSE[1]:02d}:00"]
    return {"date": date, "basket": basket, "trades": trades, "net_pnl": net,
            "symbols": symbols, "xrange": xrange}


def find_dates():
    dates = []
    for fn in sorted(os.listdir(os.path.join(HERE, "logs"))):
        if fn.startswith("strategy_") and fn.endswith(".csv"):
            dates.append(fn[len("strategy_"):-len(".csv")])
    return dates


HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Live Trading Report</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 0; padding: 0 24px 60px;
         background: #0f1117; color: #e6e6e6; }
  h1 { padding-top: 20px; }
  h2 { border-bottom: 2px solid #333; padding-bottom: 6px; margin-top: 50px; }
  h3 { color: #7fd1ff; margin-top: 36px; }
  .sub { color: #9aa0aa; font-size: 13px; }
  .pnl-pos { color: #3ecf6e; font-weight: 600; }
  .pnl-neg { color: #ff5c5c; font-weight: 600; }
  table { border-collapse: collapse; width: 100PCT; margin: 10px 0 20px; font-size: 13px; }
  th, td { border: 1px solid #333; padding: 5px 9px; text-align: right; }
  th { background: #1b1f2a; text-align: center; }
  td:first-child, td:nth-child(2) { text-align: left; }
  .toc { background: #1b1f2a; padding: 14px 18px; border-radius: 8px; margin: 16px 0; }
  .toc a { color: #7fd1ff; margin-right: 16px; text-decoration: none; }
  .chart { margin: 6px 0 4px; }
  .caption { color: #9aa0aa; font-size: 12px; margin: 0 0 14px; }
  .warmup-note { color: #d9a441; font-size: 12px; }
</style>
</head>
<body>
<h1>Live Trading Report — dynamic basket, last __NDAYS__ days</h1>
<div class="toc"><b>Days:</b> __TOC__</div>
<p class="sub">1-min candles rebuilt from raw tick data (data/&lt;date&gt;/ticks.jsonl). 15-min series is the
actual production strategy log (logs/strategy_&lt;date&gt;.csv, real live state, already warmed pre-market by
select_basket.py). 1-min series is freshly recomputed with the same Supertrend code (atr=14, mult=1.5) purely
for comparison — it has no historical warm-up, so its trend line is flat/undefined
(<span class="warmup-note">amber warm-up band</span>) for its first ~14 one-minute bars each morning.
All charts are clipped to 09:15–15:00 IST: every symbol/day gets one stale full-snapshot tick from the broker
at ~06:08 IST (before the runner even connects) that is otherwise indistinguishable from a real price to
runner.py's own stale-tick filter (it only checks date, not market hours) — excluded ticks are counted per
symbol below. Entry markers show both the raw logged time (candle START — a real labeling bug in runner.py's
ts_str) and the diamond +15m mark, which is when that candle actually closed and the order really fired.</p>
__DAYS__
<script>
const REPORT = __DATA_JSON__;

function traceCandles(series, name) {
  return { type: "candlestick", name: name,
    x: series.map(d => d.t), open: series.map(d => d.o), high: series.map(d => d.h),
    low: series.map(d => d.l), close: series.map(d => d.c),
    increasing: {line: {color: "#3ecf6e"}}, decreasing: {line: {color: "#ff5c5c"}} };
}

function plus15(tstr) {
  // Pure string/integer arithmetic on purpose — these timestamps are naive IST wall-clock
  // labels with no zone info. Routing them through a JS Date (which assumes local time) and
  // back out via toISOString() (which is always UTC) silently shifts them by the browser's
  // UTC offset — that bug produced a bogus ~5h30m-earlier "decision time" the first time
  // this was written this way.
  const [datePart, timePart] = tstr.split(" ");
  let [h, m, s] = timePart.split(":").map(Number);
  m += 15;
  if (m >= 60) { m -= 60; h += 1; }
  const pad = n => String(n).padStart(2, "0");
  return `${datePart} ${pad(h)}:${pad(m)}:${pad(s)}`;
}

function tradeMarkers(trades) {
  const traces = [];
  if (!trades.length) return traces;
  const real = trades.filter(t => !t.phantom_entry);
  traces.push({ type: "scatter", mode: "markers", name: "entry (as logged, candle-START)",
    x: trades.map(t => t.entry_time), y: trades.map(t => t.entry_price),
    marker: { symbol: trades.map(t => t.side === "LONG" ? "triangle-up" : "triangle-down"),
              size: 13, color: trades.map(t => t.side === "LONG" ? "#3ecf6e" : "#ff9d3f"),
              line: {color: "#fff", width: 1} },
    text: trades.map(t => `ENTRY ${t.side} @ ${t.entry_price} — logged at ${t.entry_time.slice(11)}`
                          + (t.phantom_entry ? " ⚠ outside market hours — see notes" : "")),
    hoverinfo: "text+x" });
  if (real.length) {
    traces.push({ type: "scatter", mode: "markers", name: "≈ real decision time (candle CLOSE, +15m)",
      x: real.map(t => plus15(t.entry_time)), y: real.map(t => t.entry_price),
      marker: { symbol: "diamond", size: 10, color: "#7fd1ff", line: {color: "#fff", width: 1} },
      text: real.map(t => `flip actually closed here, ~15m after the logged entry_time`),
      hoverinfo: "text+x" });
    traces.push(...real.map(t => ({ type: "scatter", mode: "lines", showlegend: false,
      x: [t.entry_time, plus15(t.entry_time)], y: [t.entry_price, t.entry_price],
      line: { color: "#7fd1ff", width: 1, dash: "dash" }, hoverinfo: "skip" })));
  }
  traces.push({ type: "scatter", mode: "markers", name: "exit",
    x: trades.map(t => t.exit_time), y: trades.map(t => t.exit_price),
    marker: { symbol: "circle-open", size: 12, line: {width: 3},
              color: trades.map(t => t.net_pnl >= 0 ? "#3ecf6e" : "#ff5c5c") },
    text: trades.map(t => `EXIT net Rs${t.net_pnl.toFixed(2)}`), hoverinfo: "text+x" });
  trades.forEach(t => traces.push({ type: "scatter", mode: "lines", showlegend: false,
    x: [t.entry_time, t.exit_time], y: [t.entry_price, t.exit_price],
    line: { color: t.net_pnl >= 0 ? "#3ecf6e" : "#ff5c5c", width: 1, dash: "dot" }, hoverinfo: "skip" }));
  return traces;
}

function warmupShape(series1m) {
  const idx = series1m.findIndex(d => d.trend !== 0);
  if (idx <= 0) return [];
  return [{ type: "rect", xref: "x", yref: "paper", x0: series1m[0].t, x1: series1m[idx].t,
            y0: 0, y1: 1, fillcolor: "#d9a441", opacity: 0.12, line: {width: 0} }];
}

function renderPriceChart(divId, series1m, trades, xrange) {
  const data = [traceCandles(series1m, "1m"), ...tradeMarkers(trades)];
  const phantom = trades.filter(t => t.phantom_entry);
  const annotations = phantom.map(t => ({
    x: xrange[0], y: 1, xref: "x", yref: "paper", showarrow: false, xanchor: "left", yanchor: "top",
    text: `⚠ ${t.symbol} entry logged at ${t.entry_time.slice(11)} — outside market hours, driven by the`
        + ` pre-market stale-snapshot bug, not shown on this axis. Fill price Rs${t.entry_price} was real.`,
    font: {color: "#ff9d3f", size: 11}, align: "left", bgcolor: "#1b1f2a", bordercolor: "#ff9d3f" }));
  Plotly.newPlot(divId, data, {
    template: "plotly_dark", height: 420, margin: {t: 10, b: 30, l: 55, r: 10},
    paper_bgcolor: "#0f1117", plot_bgcolor: "#0f1117",
    xaxis: { rangeslider: {visible: false}, type: "date", range: xrange }, yaxis: { title: "price" },
    shapes: warmupShape(series1m), annotations: annotations, legend: {orientation: "h", y: 1.1} },
    {responsive: true});
}

function renderFlipCompare(divId, series15, series1m, symbol, xrange) {
  Plotly.newPlot(divId, [
    { type: "scatter", mode: "lines", line: {shape: "hv", color: "#7fd1ff", width: 2},
      name: "15-min trend (production)", x: series15.map(d => d.t), y: series15.map(d => d.trend),
      xaxis: "x", yaxis: "y" },
    { type: "scatter", mode: "lines", line: {shape: "hv", color: "#ffbf47", width: 2},
      name: "1-min trend (recomputed)", x: series1m.map(d => d.t), y: series1m.map(d => d.trend),
      xaxis: "x2", yaxis: "y2" },
  ], {
    template: "plotly_dark", height: 340, margin: {t: 30, b: 30, l: 55, r: 10},
    paper_bgcolor: "#0f1117", plot_bgcolor: "#0f1117",
    grid: {rows: 2, columns: 1, pattern: "independent"},
    xaxis:  { type: "date", title: "", range: xrange },
    xaxis2: { type: "date", title: "time of day", range: xrange },
    yaxis:  { title: symbol + " 15m", tickvals: [-1, 0, 1], ticktext: ["BEAR", "-", "BULL"], range: [-1.3, 1.3] },
    yaxis2: { title: symbol + " 1m",  tickvals: [-1, 0, 1], ticktext: ["BEAR", "-", "BULL"], range: [-1.3, 1.3] },
    legend: {orientation: "h", y: 1.15},
  }, {responsive: true});
}

REPORT.forEach(day => {
  Object.entries(day.symbols).forEach(([sym, d]) => {
    renderPriceChart(`chart_${day.date}_${sym}_a`, d.candles_1m, d.trades, day.xrange);
    renderPriceChart(`chart_${day.date}_${sym}_b`, d.candles_1m, d.trades, day.xrange);
    renderFlipCompare(`flip_${day.date}_${sym}`, d.series_15m, d.candles_1m, sym, day.xrange);
  });
});
</script>
</body></html>
"""

DAY_TEMPLATE = """
<h2 id="day-{date}">{date} — basket: {basket} — day net: <span class="{pnlcls}">Rs{net:+.2f}</span></h2>
{trades_table}
{symbol_blocks}
"""

SYMBOL_BLOCK = """
<h3>{symbol}</h3>
<p class="caption">Chart clipped to market hours (09:15–15:00 IST) — {excluded_note}
1-min candles + live trade markers (triangle = entry as logged, diamond = ≈ real decision time
+15m, circle = exit, dotted line links entry/exit). Amber band = 1-min strategy still warming up
(fewer than 14 one-minute bars seen).</p>
<div class="chart" id="chart_{date}_{symbol}_a"></div>
<p class="caption">Duplicate of the chart above (independent pan/zoom copy):</p>
<div class="chart" id="chart_{date}_{symbol}_b"></div>
<p class="caption">Trend flip (0/1-style): production 15-min strategy (top) vs a freshly recomputed 1-min
Supertrend on the same day/symbol (bottom) — same flip logic, different candle size.</p>
<div class="chart" id="flip_{date}_{symbol}"></div>
"""


def render_trades_table(trades):
    if not trades:
        return "<p class='sub'>No trades closed this day.</p>"
    rows = []
    for t in trades:
        cls = "pnl-pos" if t["net_pnl"] >= 0 else "pnl-neg"
        entry_label = t["entry_time"][11:]
        if t.get("phantom_entry"):
            entry_label += " ⚠"
        rows.append(
            f"<tr><td>{t['symbol']}</td><td>{t['side']}</td>"
            f"<td>{entry_label}</td><td>{t['entry_price']:.2f}</td>"
            f"<td>{t['exit_time'][11:]}</td><td>{t['exit_price']:.2f}</td>"
            f"<td>{t['qty']}</td><td>{t['gross_pnl']:+.2f}</td><td>{t['cost']:.2f}</td>"
            f"<td class='{cls}'>{t['net_pnl']:+.2f}</td></tr>")
    note = ("<p class='warmup-note'>⚠ = entry logged outside market hours — a phantom flip caused by "
            "the pre-market stale-snapshot bug, not a real Supertrend signal.</p>"
            if any(t.get("phantom_entry") for t in trades) else "")
    return ("<table><tr><th>Symbol</th><th>Side</th><th>Entry</th><th>Entry Px</th>"
            "<th>Exit</th><th>Exit Px</th><th>Qty</th><th>Gross</th><th>Cost</th><th>Net</th></tr>"
            + "".join(rows) + "</table>" + note)


def main():
    dates = sys.argv[1:] or find_dates()
    if not dates:
        print("No logs/strategy_<date>.csv files found — nothing to report.")
        return
    print(f"Building report for: {', '.join(dates)}")
    days = [build_day(d) for d in dates]

    toc = " | ".join(f'<a href="#day-{d["date"]}">{d["date"]}</a>' for d in days)
    day_html = []
    for day in days:
        blocks = []
        for sym, d in day["symbols"].items():
            n = d["excluded_count"]
            note = (f"excluded {n} pre/post-market tick(s) (incl. the ~06:08 stale broker snapshot)."
                    if n else "no pre/post-market ticks seen for this symbol.")
            blocks.append(SYMBOL_BLOCK.format(symbol=sym, date=day["date"], excluded_note=note))
        symbol_blocks = "".join(blocks)
        day_html.append(DAY_TEMPLATE.format(
            date=day["date"], basket=", ".join(day["basket"]),
            net=day["net_pnl"], pnlcls="pnl-pos" if day["net_pnl"] >= 0 else "pnl-neg",
            trades_table=render_trades_table(day["trades"]),
            symbol_blocks=symbol_blocks,
        ))

    html = (HTML_TEMPLATE
            .replace("__NDAYS__", str(len(days)))
            .replace("__TOC__", toc)
            .replace("__DAYS__", "".join(day_html))
            .replace("__DATA_JSON__", json.dumps(days))
            .replace("100PCT", "100%"))
    with open(OUT_HTML, "w") as f:
        f.write(html)
    print(f"\nWrote {OUT_HTML}  ({os.path.getsize(OUT_HTML)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
