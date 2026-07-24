# Supertrend Intraday Algo-Trading System

A live, autonomous intraday trading system for the Indian equity market. It runs a
**Supertrend + EMA-50** strategy on **15-minute candles** over a **dynamically-selected daily
basket of NSE stocks**, executes through the **Flattrade** broker API, and runs hands-free on
**AWS EC2** via cron on weekdays (09:15–15:00 IST).

> **Honest status — read this first.** The execution path is heavily hardened for safety, but
> out-of-sample backtesting (documented in [§10](#10-research-findings--the-honest-verdict)) found
> **no robust, cost-surviving edge**. Trend-following TA has no generalizing signal on this
> universe; the only *real* signal found (order-book microstructure) sits **below the spread** and
> loses money as a taker; a cross-asset "edge" turned out to be a look-ahead leak. **Treat this as
> a learning / validation system and run it at tiny capital (~₹1,500 wallet).** It is not expected
> to be profitable.

---

## Table of contents
1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [The strategy in detail](#3-the-strategy-in-detail)
4. [Order execution](#4-order-execution)
5. [Position sizing, leverage, MIS vs CNC](#5-position-sizing-leverage-mis-vs-cnc)
6. [Daily stock selection & warm-up](#6-daily-stock-selection--warm-up)
7. [Deploy workflow](#7-deploy-workflow)
8. [Cron schedule](#8-cron-schedule)
9. [Data, state & logs](#9-data-state--logs)
10. [Research findings — the honest verdict](#10-research-findings--the-honest-verdict)
11. [Cost model](#11-cost-model)
12. [File map](#12-file-map)
13. [Setup & running](#13-setup--running)
14. [Security](#14-security)
15. [Known issues / pending work](#15-known-issues--pending-work)

---

## 1. What it does

Every weekday, autonomously:

1. **~08:00 IST** — `select_basket.py` ranks the **Nifty-100** universe and picks the **top 15**
   names (by volatility + trend-cleanliness), stamps them as today's basket, and warms up each
   stock's indicators from historical 15-min bars.
2. **08:45 IST** — `start_trading.sh` regenerates the Flattrade session token and launches
   `runner.py` under a `flock` singleton watchdog.
3. **09:15–15:00 IST** — `runner.py` streams live ticks over WebSocket, builds 15-min candles,
   computes Supertrend + EMA-50 per stock, and trades trend flips with real-time tick-level stops.
4. **15:00 IST** — all open positions are squared off (intraday, no overnight).
5. **15:05 IST** — `stop_trading.sh` drops a STOP flag; the watchdog exits cleanly.

No manual intervention is needed beyond funding the wallet before 08:45.

---

## 2. Architecture

```
                    ┌─────────────────────────────────────────────┐
   Flattrade REST ──┤ auth.py / generate_token.py  → session token │
                    └─────────────────────────────────────────────┘
                                      │
   Flattrade WS ───► client.py ───► runner.py (main loop)
   (live ticks)      (REST+WS)        │
                                      ├─ marketdata.py   Tick / Candle / CandleBuilder
                                      ├─ strategies/supertrend.py   signal + tick-level stops
                                      ├─ live_broker.py  real orders (pricing / retry / margin)
                                      │    └─ paper.py    PaperBroker (dry-run twin)
                                      └─ writes → data/runner_state.json (position ledger)
                                                   logs/{runner,trades,strategy}_<date>.*

   select_basket.py ─► data/today_basket.json     (which 15 stocks + warm state)
   update_universe.py ─► data/nifty100.json        (Friday refresh of the universe)
```

**Key design principles**

- **Git is the single source of truth.** Edits happen on the Mac working copy, are pushed to
  GitHub, and EC2 is *pull-only* — never edit EC2 files by hand.
- **Confirm-before-book.** A position is only recorded in the ledger after the exchange confirms
  the fill — never optimistically. This prevents phantom positions.
- **Disk-backed ledger.** `data/runner_state.json` survives a restart, so a mid-session crash
  doesn't lose track of open positions.
- **Real-time stops.** Stops are evaluated on *every tick*, not just at candle close, so exits are
  as tight as the feed allows.

---

## 3. The strategy in detail

### 3.1 ATR (Average True Range, Wilder, period 14)
True range per candle = `max(high−low, |high−prevClose|, |low−prevClose|)`, smoothed with Wilder's
EMA (`α = 1/period`). ATR is the volatility unit for *everything* — band width, stops, and entry
filters are all expressed in multiples of ATR.

### 3.2 Supertrend (ATR period 14, multiplier 1.5)
```
hl2        = (high + low) / 2
upperBand  = hl2 + multiplier × ATR
lowerBand  = hl2 + multiplier × ATR      (mirrored below)
```
Bands are made "sticky" (the classic Supertrend recursion) so they only tighten toward price.
The **trend** flips:
- to **bullish** when close crosses **above** the final upper band,
- to **bearish** when close crosses **below** the final lower band.

A flip is the **entry trigger**. `multiplier = 1.5` and `ATR = 14` are the live settings.

### 3.3 EMA-50 trend filter (entry-only)
A 50-period EMA on close acts as a regime gate:
- **Longs** only allowed when `close > EMA-50`.
- **Shorts** only allowed when `close < EMA-50`.

The EMA is used **for entries only** — it never forces an exit. (This was verified explicitly:
`check_stops` does not reference the EMA.)

### 3.4 Entry filters (stack on top of the flip)
An entry fires only if **all** of these hold (constants in `strategies/supertrend.py`):

| Filter | Rule | Why |
|---|---|---|
| **Trend flip** | Supertrend just flipped in the desired direction | the core signal |
| **EMA-50** | long above / short below EMA-50 | trade with the trend |
| **Time window** | candle **starts** in `[09:30, 14:15]` (≈ 09:45–14:30 exec) | skip the noisy open & late day — the single most robust filter found |
| **EMA gap** | `|close − EMA-50| ≥ 0.3 × ATR` | require the move to be genuinely extended, not hugging the mean |

Because a 15-min bar's signal is acted on at its **close**, the window is checked on the bar's
**start** minute.

### 3.5 Exit rules ("trailtight" — evaluated every tick)
In priority order (constants in `strategies/supertrend.py`):

1. **Hard stop-loss** — exit if price moves `1.5 × ATR` against entry. (`HARD_SL_MULT = 1.5`)
2. **Breakeven lock** — once `+0.5 × ATR` in profit, move the stop to entry.
   (`BREAKEVEN_TRIGGER_MULT = 0.5`)
3. **Chandelier peak-trail** — track the peak favorable price; exit if it retraces
   `1.5 × ATR` from that peak. (`TRAIL_PEAK_MULT = 1.5`) — this is the dominant profit-locking lever.
4. **Fixed take-profit** — disabled (`TAKE_PROFIT_MULT = 0.0`); caps winners, hurt in backtests.
5. **EOD square-off** — everything is closed at **15:00 IST**. No overnight risk.

The exit tuning ("trailtight" = breakeven 0.5 + trail 1.5, no TP) beat the older exit
(breakeven 1.0, no trail) in both halves of a 19-day / 131-trade tick backtest. It is a *better
exit*, not a durable edge — win rate stays ~18–33% regardless, so the bottleneck is entry quality.

---

## 4. Order execution

The execution layer (`live_broker.py` + the order management in `runner.py`) is where most of the
hardening lives. The guiding rule: **never price off LTP — always the fresh best bid/ask.**

- **Order type:** `LMT + IOC` (immediate-or-cancel limit). The limit price uses the exchange's
  real tick size (`ti` from `SearchScrip`, not a hardcoded guess — sub-₹250 names were being
  rejected by a wrong tick assumption).
- **Pricing:** off the **fresh** best bid (sell) / best ask (buy) from the latest tick, *never* the
  last-traded price.
- **Entry retry-on-cancel:** if an entry IOC is cancelled (code 16388), it is re-placed on the
  **next fresh tick's** quote, up to `MAX_ENTRY_RETRIES = 3`. The retry schedule: attempts **1 & 2
  sit at the fresh quote** (0 ticks — no giving up a tick prematurely); attempts **3+ cross 1
  tick** to get filled. Always the fresh quote, never a stale one. If all retries fail, the entry
  is abandoned cleanly (ledger cleared, strategy reset, margin released). Exits use the same
  fresh-quote-then-cross schedule.
- **Confirm-before-book:** a fill is only written to the ledger when the WS order update confirms
  it. In paper mode the fill is simulated immediately.
- **Market-open gate:** ticks before 09:15 IST (and stale snapshots) are dropped — a pre-open
  candle once triggered a rejected order and a ~9,000-attempt retry loop; this gate closes that.
- **Phantom clearing:** a rejected or IOC-cancelled order clears any provisional position, for
  **both** buy and sell/short sides (an earlier bug only handled the buy side).
- **Exits are never capital-gated:** `simulate_fill(is_exit=True)` never blocks a close on a
  capital check and never reduces the exit quantity — a position must always be closeable.
- **EOD orphan reconcile:** at end of day, if the ledger says flat but the real exchange
  `net_position ≠ 0`, the residual is squared off. This catches never-filled entries that would
  otherwise linger (Flattrade would auto-square them ~15:20, out of our control).

---

## 5. Position sizing, leverage, MIS vs CNC

| Setting | Value | Notes |
|---|---|---|
| Own capital per stock | **₹5,000** | `MAX_CAPITAL_PER_STOCK` (pre-leverage) |
| Max concurrent positions | **2** | `MAX_POSITIONS` (env-overridable) |
| Wallet deploy cap | **≤ 85%** | `WALLET_DEPLOY_FRAC` — never commit the whole wallet |
| MIS leverage | **×4** | intraday product, **no DP charge** |
| CNC leverage | **×1** | delivery product, **₹23.60 DP charge per sell** |

- **MIS = long + short**; **CNC = long-only**.
- The basket observes **15** names but trades at most **2** concurrently — it's capital-limited, so
  more names just means more flip opportunities to pick the best 2 from.
- **MIS ≫ CNC for this strategy** because the flat ₹23.60 DP charge on every CNC delivery sell
  destroys the thin per-trade P&L. See [§11](#11-cost-model).

---

## 6. Daily stock selection & warm-up

`select_basket.py`:
- **Universe:** the Nifty-100, loaded from `data/nifty100.json` (with a hardcoded fallback).
- **Ranking:** each stock scored by **10-day volatility (avg daily range %)** + **low flip-count**
  (rank-sum) — i.e. names that are *both* moving *and* cleanly trending, not choppy. Plain
  volatility alone failed on large-caps (they chop); vol + low-flips is the robust winner.
- **Output:** top **15** written to `data/today_basket.json`, all as MIS.
- **Session stamping:** if run **before** 09:15 IST it stamps **today's** session (and warms from
  the prior close); otherwise it stamps the **next** session (skipping weekends). A `--today` flag
  forces today's session regardless of clock.
- **Warm-up:** indicators (ATR / EMA / trend) are primed from **15-min history** (a 60-day window,
  which covers every needed day — yfinance's 1-min feed only goes back 7 days and under-warmed
  older days).

`update_universe.py` refreshes `data/nifty100.json` from the NSE Nifty-100 list every **Friday
~15:50 IST**. It fetches via a cookie-primed NSE JSON API with a CSV fallback and **never
overwrites on a failed/short fetch** (atomic write). *(NSE's endpoint often 503s from EC2; the
fallback handles it.)*

---

## 7. Deploy workflow

**Never edit EC2 files by hand.** The loop is:

1. **Edit** in the Mac working copy: `~/Downloads/projects-main/projects/trading/`.
2. **Test before pushing:** `python3 -m py_compile <files>` + `python3 -m pyflakes <files>` +
   a stub run where possible (`verify_pipeline.py`, `test_entry_retry.py`).
3. **Commit + push from the Mac.** The Mac's default git identity is a *work* account with no
   access to this personal repo, so pushes use a **Personal Access Token for `mukeshchandu`**
   embedded in the local `.git/config` remote URL. If a push 403s, the PAT was rotated — reset it:
   ```bash
   git remote set-url origin https://mukeshchandu:<PAT>@github.com/mukeshchandu/projects.git
   ```
   **Never commit the PAT or `.env`.**
4. **Deploy on EC2:** `git -C /home/ec2-user/projects pull`.
5. **A running runner does NOT hot-reload.** A mid-session pull has no effect until the runner
   restarts (the 08:45 cron restarts it daily; for an intraday deploy, stop + start).

### Commit conventions
- **Short** one-line commit messages.
- Add an entry to **`CHANGELOG.md`** (newest on top). The header notes all entries are AI-written.
- **Do NOT add the AI as author or co-author.**

---

## 8. Cron schedule

EC2, weekdays, times in UTC (→ IST):

```
30 2  * * 1-5   select_basket.py   (08:00 IST)  → pick + warm today's 15-stock basket
15 3  * * 1-5   start_trading.sh   (08:45 IST)  → token + flock singleton watchdog + runner
35 9  * * 1-5   stop_trading.sh    (15:05 IST)  → STOP flag halts the watchdog
15 10 * * 1-5   replay_ticks.py    (15:45 IST)  → legacy ATR warmup (select_basket now warms too)
20 10 * * 5     update_universe.py (15:50 IST, Fri) → refresh Nifty-100 universe
```

Selection was **moved to the morning (08:00)** from the old evening (18:30) run so it stamps
*today's* session pre-open and warms from yesterday's close for the 08:45 runner.

---

## 9. Data, state & logs

Under `trading/`:

| Path | What | Committed? |
|---|---|---|
| `data/YYYY-MM-DD/ticks.jsonl` | raw WS ticks per day (`lp`, `bp1`/`sp1` bid/ask sizes, `ft` epoch) | **yes** — irreplaceable recorded data, used by backtests |
| `data/nifty100_15m.pkl` | cached Nifty-100 15-min bars (backtest input) | yes |
| `data/nifty100.json` | current Nifty-100 universe list | yes |
| `data/st_state/{SYMBOL}.json` | per-stock ATR/EMA/trend warm state | gitignored |
| `data/runner_state.json` | position ledger (survives restart) | gitignored |
| `data/today_basket.json` | today's 15 selected stocks | gitignored |
| `logs/runner_<date>.log` | full runtime log | gitignored |
| `logs/trades_<date>.csv` | completed trades + P&L (gross / cost / net) | gitignored (`git add -f` to share) |
| `logs/strategy_<date>.csv` | **per-candle state** (ohlc, atr, supertrend, trend, bands, ema, position, signal, reason) — the ground truth | gitignored |

> `ticks.jsonl`'s `ft` field is a **standard Unix epoch** — convert with `tz=IST`. Only stale
> snapshot + pre-open ticks were ever bad; the code drops those.

---

## 10. Research findings — the honest verdict

Extensive, cost-accurate, out-of-sample backtesting was done to answer one question: *is there a
retail-capturable edge here?* The answer is **no**. Details, so nobody re-derives them:

- **No robust TA edge.** `quant_lab.py` tested **9 strategy types** (supertrend, ema_cross, macd,
  momentum, donchian, bollinger_mr, rsi_mr, vwap_mr, orb) × parameters × native-vs-our-exit ×
  time-filter on Nifty-100 15-min / 60-day data with a train/test split. Trend strategies are
  31–48% win-rate and OOS-negative; mean-reversion has better win rates but overfits. The only
  both-halves-positive config (VWAP-MR k=2%) was a knife-edge fluke carried by 1–2 stocks.
- **Timeframe: keep 15-min.** Faster candles die from whipsaw/overtrading (1-min = −6,647 over 496
  trades). The robust band is 11–15 min; picking a specific minute (e.g. 12) is overfitting —
  train/test proved the argmax moves between halves.
- **Order-book imbalance (OBI) is the only *real* signal found**, and it's not tradeable.
  `obi_lab.py` on our L1 tick book: OBI predicts the forward mid-move (IC ≈ 0.03–0.045, ~52% hit,
  strong OBI → ±0.4 bps). **But** the half-spread (~1.4 bps) dwarfs the 0.4 bps edge, so a taker
  loses ~4.5 bps/trade. Only harvestable as a *maker* (can't backtest fills from L1; adverse
  selection + latency + no rebates = retail loses).
- **ML confirms the sub-spread wall.** `ml_micro.py` (sklearn `HistGradientBoostingClassifier` +
  `LogisticRegression`, walk-forward by date, 13 microstructure features, 30-s forward-mid target)
  extracts *slightly more* real signal than raw OBI (IC 0.037 vs 0.014, top-decile +1.17 bps) — but
  taker P&L is still **−4.1 bps/trade**. Signal ≪ ~3.5 bps cost+spread. RL/DL were skipped: 22 days
  of ticks / 60 days of bars is far too little data.
- **Cross-asset "edge" was a leak.** `cross_asset.py` first showed 59% next-day-direction accuracy
  — but it used prior-day US/FX/commodity closes that land ~01:30 IST, *inside* the India
  close-to-close target window (look-ahead leakage). De-leaked (externals lagged 2 days): **52.4%,
  loses to buy-and-hold.** A coin flip.
- **Selection is the main lever**, and buy-and-hold beat the strategy in an up-market window.

**Method notes that made these trustworthy:** the faithful engine `bt_engine.py` fills at **best
bid/ask** on tick data and **close ± spread** on yfinance (the old `bt_`/`sim_chart` scripts filled
at the mid/signal price — too optimistic). All ML used walk-forward-by-date splits (no random
shuffle, which leaks in time series), train-only standardization, and realistic MIS costs. Every
parameter claim was required to be **positive in both train and test halves** before being trusted.

---

## 11. Cost model (model this in any backtest)

Flattrade charges **zero brokerage**. Real costs:

| Charge | MIS (intraday) | CNC (delivery) |
|---|---|---|
| STT | 0.025% on sell | 0.1% both legs |
| Exchange txn | 0.00307% | 0.00307% |
| SEBI | ₹10/crore | ₹10/crore |
| Stamp duty | 0.003% on buy | 0.015% on buy |
| GST | 18% on (brokerage + txn + SEBI) | same |
| **DP charge** | **none** | **₹20 + GST = ₹23.60 flat per delivery sell** |

The **DP charge is the dominant cost killer** for small frequent trades — it alone flips this
strategy from positive (MIS) to negative (CNC). **DDPI** (₹175 one-time) enables auto CNC-sell via
API but does **not** reduce the DP charge.

---

## 12. File map

### Live system
| File | Role |
|---|---|
| `runner.py` | main loop: ticks → candles → entries/exits, order management, EOD, logging |
| `strategies/supertrend.py` | the strategy: Supertrend + EMA-50 + entry filters + tick-level stops |
| `strategies/base.py` | strategy base class |
| `live_broker.py` | real Flattrade orders — pricing (best bid/ask), IOC retry, margin |
| `paper.py` | `PaperBroker` / `PaperFill` — dry-run twin of the broker |
| `client.py` | Flattrade REST + WebSocket client |
| `auth.py`, `generate_token.py` | session-token generation |
| `config.py` | IST timezone, market-open (09:15) & EOD (15:00) times |
| `marketdata.py` | `Tick`, `Candle`, `CandleBuilder` |

### Live ops
| File | Role |
|---|---|
| `select_basket.py` | daily 15-stock selection + warm-up |
| `update_universe.py` | Friday Nifty-100 universe refresh |
| `replay_ticks.py` | legacy ATR warmup |
| `start_trading.sh` / `stop_trading.sh` | `flock` singleton launcher + STOP flag |

### Research & backtest lab (Mac-only tooling, committed for preservation)
| File | Role |
|---|---|
| `bt_engine.py` | **faithful** backtest engine — fills at best bid/ask (ticks) or close±spread (bars); reuses the real strategy |
| `quant_lab.py` | 9 strategy types × params × exits, OOS train/test |
| `obi_lab.py` | order-book-imbalance predictive-power test |
| `ml_micro.py` | microstructure ML (HistGBM + LogReg), walk-forward by date, leakage-audited |
| `cross_asset.py` | daily Nifty direction from US/FX/gold/crude (de-leaked) |
| `bt_ticks.py`, `bt_yahoo.py`, `bt_sweep.py`, `bt_sweep_all.py`, `bt_robust.py`, `bt_mis.py`, `bt_regime.py`, `bt_select*.py`, `bt_phase.py`, `bt_longonly.py`, `bt_trail_sweep.py`, `bt_hardsl_analysis.py` | parameter/robustness sweeps |
| `tf_backtest.py`, `entry_filter_backtest.py`, `exit_backtest.py`, `regime_backtest.py` | timeframe / entry-filter / exit / regime studies |
| `sim_chart.py`, `chart_*.py` | HTML chart generators (candles + Supertrend belt + EMA + trend panel + would-have trades) |
| `verify_pipeline.py`, `test_entry_retry.py` | offline correctness tests |
| `bt_sweep_results.txt`, `quant_results.txt` | saved sweep outputs |
| `NAIM_TRADE_HANDOFF.md` | session-handoff doc (start-of-session context) |

---

## 13. Setup & running

### Dependencies
```bash
python3 -m venv venv && source venv/bin/activate
pip install requests websocket-client python-dotenv numpy pandas yfinance
```
(`yfinance` is needed by `select_basket.py` and the backtests.)

### Secrets — `trading/.env` (gitignored, EC2 only)
```
FLATTRADE_USER_ID=...
FLATTRADE_API_KEY=...
FLATTRADE_API_SECRET=...
PASSWORD=...
TOTP_SECRET=...
FLATTRADE_SESSION_TOKEN=...     # regenerated each morning by generate_token.py
MAX_POSITIONS=2                 # optional tunable
```
`FLATTRADE_SESSION_TOKEN` is the only credential used at runtime. **Never log, print, or commit any
credential.**

### Run
```bash
# pick & warm today's basket (before 09:15 IST for today's session)
python3 select_basket.py --today

# start live trading (token + singleton watchdog + runner)
./start_trading.sh

# stop
./stop_trading.sh
```
On EC2 this is all driven by cron ([§8](#8-cron-schedule)) — just fund the wallet before 08:45.

### Backtest
```bash
python3 quant_lab.py --fetch    # download+cache Nifty-100 15m/60d (once)
python3 quant_lab.py            # run the sweep
python3 obi_lab.py              # OBI test on recorded tick data
python3 ml_micro.py             # microstructure ML
```

---

## 14. Security

- **`.env`** holds live broker credentials — gitignored, never committed, present on EC2 only.
  `.env` edits are the *one* thing done directly on EC2 (via a targeted `sed`, not code editing).
- **The GitHub PAT** lives only in the Mac clone's `.git/config` remote URL. Never echo it into
  files or logs; sanitize any printed remote (`sed -E 's#//[^@]*@#//***@#g'`). Rotate it if the
  machine is decommissioned.
- Never price orders off LTP; never expose credentials in logs.

---

## 15. Known issues / pending work

1. **Oversell (real bug):** the partial-fill handler can re-order a "remaining" quantity while the
   original order is still filling → oversell → accidental short (seen live on UNIONBANK). Fix:
   don't chase a partial remainder until the original order is terminal, and cap every exit at the
   actual exchange net position.
2. **Chart-from-log:** rewrite the chart tools to read `logs/strategy_<date>.csv` (real logged
   state) rather than recomputing indicators.
3. **Verify NSE fetch on EC2:** `update_universe.py`'s NSE endpoint often 503s from EC2 (fallback
   handles it, but the live path is unverified).
4. **`om` fill-qty field:** the WS order-update filled-quantity field is assumed to be
   `fillshares`/`flqty` — verify on the first live partial fill.

---

*This system was built and iterated collaboratively; all code and this README were written by
Claude (AI assistant) to the owner's specifications. See `CHANGELOG.md` for the change history.*
