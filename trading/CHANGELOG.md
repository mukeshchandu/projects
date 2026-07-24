# Changelog

_All entries authored by Claude (AI assistant). Most recent on top._

## 2026-07-25 — Options logging moved INTO runner (single WS per token)
- Confirmed empirically that Flattrade allows only ONE active WebSocket per session token (a 2nd
  connection disconnects the 1st). So a standalone options-logger process would kick the live
  trader offline. **Options logging is now woven into `runner.py`** on the trading WS: at startup
  it resolves the NIFTY/BANKNIFTY chains (reusing `options_logger`'s resolver) and subscribes them
  on the same connection — **options as full DEPTH feed (`d`), equity unchanged as touchline (`t`)**.
  Option & index ticks are routed out at the very top of `handle_tick` (before the tk/tf filter and
  all trading logic) and written raw (+ local `rt`) to `data/options/<date>/<UNDER>.jsonl`; the
  trading path only ever sees equity tokens, so order handling is byte-for-byte unchanged.
- Env flags: `LOG_OPTIONS` (default on; `0` to disable), `OPTION_FEED` (default `d`). Setup is
  best-effort — any resolution/subscribe failure logs a warning and trading continues unaffected.
  Equity is subscribed FIRST so a WS subscription cap can never starve trading.
- **Removed `start_options_logger.sh`** (a 2nd process would open a 2nd WS — do NOT cron it).
  `options_logger.py` stays as an importable module (its resolver is reused) and a manual/dry-run
  tool, with a header warning never to run it standalone while `runner.py` is live.
- Written by Claude (AI assistant).

## 2026-07-25 — Fix options_logger instrument resolution (GetOptionChain)
- The initial `SearchScrip('NFO', underlying)` approach returned only 25 relevance-capped rows
  (lowest strikes, nowhere near ATM) → resolved 0 instruments. Rewrote resolution: discover the
  nearest expiry from SearchScrip `exd` fields, build the tsym expiry-token (e.g. `28JUL26`), then
  enumerate the ATM±N window via **`GetOptionChain`** (new `client.get_option_chain`; strprc must be
  an integer string). Seeds at the window bottom + ATM to sweep the ascending chain; dedups by
  token; reads lot size from the API `ls` field. Falls back to per-strike EXACT SearchScrip, then
  to a manual list. Verified on the live API (chain returns stat=Ok). Written by Claude.

## 2026-07-25 — HANDOVER.md for a fresh machine/session
- Added `HANDOVER.md`: self-contained onboarding for a new laptop — both repos, new-machine setup
  (fresh gh/PAT auth), deploy workflow, AWS/cron, secrets, the new options logging + backtest
  toolkit, conventions, current live config, findings, and pending work. Written by Claude.

## 2026-07-24 — Options data logging + repo slimmed for clean AWS deploy
- **New `options_logger.py`** — standalone daemon (NEVER trades) that records NSE index-option
  chain ticks for future backtesting. Resolves nearest-expiry strikes around ATM for NIFTY /
  BANKNIFTY via SearchScrip (defensive tsym parse; manual-list override), subscribes the NFO
  option tokens + spot over one WS, and appends raw ticks (+ local `rt`) to
  `data/options/<date>/<UNDERLYING>.jsonl` with a `_manifest.json`. Self-exits at 15:30 IST.
  `--dry-run` resolves + writes the manifest without connecting.
- **New `start_options_logger.sh`** — flock-singleton launcher/watchdog (cron alongside the runner).
- **Repo slimmed:** all research/backtest scripts, HTML charts, the 15m pkl cache, and the
  committed tick data were REMOVED from this repo (they live on in the separate `trading-lab`
  repo). This repo is now just the live runtime + ops + docs, so the AWS working tree stays clean.
- **`.gitignore`** now excludes runtime-generated data & artifacts (`data/*/ticks.jsonl`,
  `data/options/`, `*.pkl`, `*.html`, `*_results.*`). To share a logged day for backtesting,
  force it in: `git add -f data/options/<date>`.
- Written by Claude (AI assistant).

## 2026-07-24 — Preserve research/backtest lab before machine decommission
- Committed the full Mac-only research toolkit (previously kept uncommitted by convention) so it
  survives the machine wipe: `bt_engine.py` (faithful engine — fills at best bid/ask on ticks,
  close±spread on yfinance), `quant_lab.py` (9 strategy types × params, OOS train/test),
  `obi_lab.py` (order-book-imbalance signal), `ml_micro.py` + `cross_asset.py` (walk-forward ML,
  leakage-audited), the `bt_*`/`chart_*`/`sim_chart`/`*_backtest`/`tf_backtest` sweeps,
  `verify_pipeline.py`, `test_entry_retry.py`, `NAIM_TRADE_HANDOFF.md`, results txts, and the
  `nifty100_15m.pkl` data cache.
- Standing finding across all of it: NO retail-capturable edge — trend/MR TA has no OOS signal;
  microstructure/ML signal is real but sits under the spread (taker loses); the cross-asset daily
  "edge" was a look-ahead leak (52% after de-leaking, loses to buy-and-hold). Live system is
  hardened/safe but should not be expected to profit.
- This entry and the committed research code were written by Claude, the AI assistant.

## 2026-07-20 — Entry retry-on-cancel + 15-stock basket
- `runner.py`: entries now RETRY on exchange cancel (IOC 16388) instead of being dropped —
  `_do_entry` mirrors `_do_exit`. Schedule (entries AND exits): attempts 1 & 2 sit at the
  FRESH bid/ask (0 ticks), attempts 3+ cross a fixed 1 tick; always the fresh quote, never
  LTP. Capped at MAX_ENTRY_RETRIES=3, then abandon (ledger + strategy reset, margin released).
  Entries are now confirm-before-book (real only on fill) and stops are gated on `filled`.
- `select_basket.py`: TOP_K 8 → 15 (more names → more flip opportunities). Added `--today`
  flag to force stamping TODAY's session for an intentional mid-session re-warm + restart.

## 2026-07-18 — Morning selection/warm-up
select_basket stamping now keys off market open (09:15) not a magic hour: a PRE-OPEN run
(the new 08:00 IST cron) stamps TODAY's session; an at/after-open run stamps NEXT session.
Move the select_basket cron from 18:30 IST to 08:00 IST so selection + warm-up happen the
morning of the session (warms from yesterday's close, which the 08:45 runner loads).

## 2026-07-18 — Entry filters + universe refresh + bigger basket
- `supertrend.py`: entry FILTERS (OOS-validated, `entry_filter_backtest.py`) — no entries on
  bars starting before 09:30 or after 14:15 (≈09:45–14:30 execution; skips open noise + late
  entries), and require |close−EMA| ≥ 0.3×ATR (price extended in trend). Cut junk flips; with
  the trailtight exit the 19-day backtest went -486 → +262, TEST win% 28→34, better in both
  OOS halves. (ADX≥15 also helped a little but was left out — marginal + heavy state.)
- `select_basket.py`: TOP_K 5 → **8** stocks observed/day; universe now loads from
  data/nifty100.json (weekly refresh) with the hardcoded Nifty-100 as fallback.
- `update_universe.py` (new): fetches NIFTY 100 constituents from NSE → data/nifty100.json;
  never overwrites on a failed/short fetch. Cron (EC2, UTC) `20 10 * * 5` = Fri 15:50 IST.

## 2026-07-18 — Tighter profit-locking exit (trailtight)
Changed the exit defaults in `strategies/supertrend.py`: breakeven trigger 1.0→**0.5**×ATR
and chandelier peak-trail 0.0→**1.5**×ATR (take-profit still off). Basis: a 19-day / 131-trade
tick backtest (`exit_backtest.py`). The chandelier trail is the dominant lever (cuts big losers
fast + locks winners near the peak); the tighter breakeven is complementary. Out-of-sample
(second-half days it wasn't tuned on) it made +Rs1285 vs +Rs356 for the old exit. A causal
ADX-at-entry regime switch (`regime_backtest.py`) did NOT beat flat trailtight OOS, so it was
NOT added. Note: still period-dependent — improves the exit, not a durable edge.

## 2026-07-18 — Execution-safety fixes + sim chart
Fixes for the 2026-07-17 failure cascade (a rejected pre-open short became a phantom that
tried to "cover" ~9k times, then a real orphaned long was left for Flattrade to auto-square):
- **Market-open gate**: `runner.py` now ignores pre-open (<09:15 IST) and stale snapshot
  ticks, so the strategy only sees regular-session bars (no more 09:00 pre-open orders).
- **Phantom clear on any rejected entry**: the reject handler cleared only rejected BUY
  (long) entries; a rejected SELL (short) entry left a phantom. Now clears either side.
- **Exits never capital-gated**: `live_broker.simulate_fill(is_exit=True)` skips the cash
  check / qty-reduction for closes/covers (was the cause of the retry loop); margin is now
  committed/released by entry-vs-exit intent instead of BUY-vs-SELL.
- **EOD orphan reconcile**: at EOD, symbols we think are flat are checked against the
  exchange net position and squared by us, not left for the ~15:20 auto-square.
- **sim_chart.py** (new): replays a day's ticks through the fixed strategy and renders a
  clean per-stock chart (candles + supertrend belt + trend line + EMA-50 + bull/bear regime
  panel + would-be trades), warmed from prior-day data.

## 2026-07-15 — Real-time strategy-state log
Each closed 15-min candle now writes the live strategy state to `logs/strategy_<date>.csv`
(time, OHLC, atr, supertrend, trend, upper, lower, ema, position, signal, reason). This is
the ground truth of what the strategy saw and decided, so behaviour can be reviewed/charted
from logged values instead of recomputing (which risked warm-up leaks / replay drift).
