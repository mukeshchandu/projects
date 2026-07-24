# Naim Trade — Session Handoff
*Paste this at the start of the new session. It carries the full state of the live Supertrend algo-trading system so you can continue without re-deriving anything.*

---

## 0. FIRST TASK FOR THE NEW SESSION
Before doing anything else, **read the code and explain the entire strategy inch-by-inch** — the exact math (ATR, Supertrend bands, EMA-50 filter, trend flips), the entry rules, every exit rule (hard SL, breakeven, trailing supertrend, EOD), position sizing/leverage, the daily stock selection, warm-up, and **how orders are actually placed and managed** (pricing off best bid/ask, IOC retries, confirm-before-book, partial fills, EOD reconcile). Confirm your understanding matches the code in `runner.py`, `live_broker.py`, `strategies/supertrend.py`, and `select_basket.py` before proposing changes.

---

## 1. What this is
A live intraday algo-trading system on **Flattrade** (Indian broker) running on **AWS EC2**. Strategy = **Supertrend** on **15-min candles** with an **EMA-50 filter**, trading a **dynamically-selected daily basket** of 5 NSE stocks (from the Nifty 100), mostly **MIS (intraday)**. Real-time tick-level stops. It trades autonomously 9:15 AM–3:00 PM IST on weekdays via cron.

**Honest status:** extensively hardened for execution safety, but backtests (out-of-sample) show **no robust, cost-surviving edge** — treat it as a validation/learning system at **tiny capital** (wallet is ~Rs1,500). The edge, if any, is thin and selection-dependent.

---

## 2. Where the code is

| | Path |
|---|---|
| **GitHub repo** | `https://github.com/mukeshchandu/projects.git` (code in the `trading/` subdirectory) |
| **Mac working copy** | `/Users/chandu/Downloads/projects-main/projects/` (repo root); code in `.../projects/trading/` |
| **EC2 (live)** | `/home/ec2-user/projects/` (repo root); code in `/home/ec2-user/projects/trading/` |

**Golden rule: never edit files on EC2 by hand.** All edits happen in the Mac copy → commit → push → EC2 pulls. Git is the single source of truth; EC2 is pull-only.

---

## 3. Deploy workflow (how we code & push)

1. **Edit** files in the Mac copy (`~/Downloads/projects-main/projects/trading/`).
2. **Test before pushing** (always): `python3 -m py_compile <files>` + `python3 -m pyflakes <files>` + a stub test where possible (import `runner` with fake `websocket`/`dotenv` modules — see existing test patterns in the chat history).
3. **Commit + push from the Mac.** The Mac's default git identity is a *work* account with **no access** to this personal repo, so pushing uses a **Personal Access Token for `mukeshchandu`** embedded in the local `.git/config` remote URL. If push ever 403s, the PAT was rotated → set a new one on the `origin` URL (`git remote set-url origin https://mukeshchandu:<PAT>@github.com/mukeshchandu/projects.git`). **Never commit the PAT or `.env`.**
4. **Deploy on EC2:** `git -C /home/ec2-user/projects pull`. That is the *only* thing that changes EC2 files.
5. **A running runner does NOT hot-reload** — a pull mid-session has no effect until the runner restarts. The 8:45 AM cron restarts it daily; for an intraday deploy you must stop+start (or wait for tomorrow).

### Commit conventions (agreed)
- **Short commit messages** (one line).
- Add an entry to **`trading/CHANGELOG.md`** (newest on top) describing the change. The file header notes all entries are AI-written.
- **Do NOT add the AI as author or co-author.**

---

## 4. AWS / EC2 details
- **User:** `ec2-user` · internal host seen as `ip-172-31-41-40`.
- **Public IP / PEM / SSH:** from the original handout it was `ssh -i ~/Downloads/trading.pem ec2-user@13.63.28.2` — **VERIFY the current public IP** (it can change across stop/start unless an Elastic IP is attached; the user SSHes in regularly and knows the live IP).
- **Project dir on EC2:** `/home/ec2-user/projects/trading/` · uses a **venv** at `.../trading/venv/`.
- **Running commands:** the user runs all EC2 commands manually (SSH or AWS browser console) and pastes output back. This (Claude) session runs commands on the **Mac** only.
- **Dependencies on EC2 venv:** `requests`, `websocket-client`, `python-dotenv`, `numpy`, `pandas`, `yfinance` (yfinance needed by `select_basket.py`; install with `source venv/bin/activate && pip install yfinance`).

---

## 5. Environment variables (secrets)
- File: **`/home/ec2-user/projects/trading/.env`** (on EC2 only; **gitignored — never commit**).
- Keys: `FLATTRADE_USER_ID`, `FLATTRADE_API_KEY`, `FLATTRADE_API_SECRET`, `PASSWORD`, `TOTP_SECRET`, `FLATTRADE_SESSION_TOKEN`, and tunables like `MAX_POSITIONS` (currently `2`), optional `EMA_FILTER`.
- `FLATTRADE_SESSION_TOKEN` is the only credential used at runtime; regenerated each morning by `generate_token.py` (run inside `start_trading.sh`).
- **Never log, print, or commit any credential.** `.env` changes are the one thing done directly on EC2 (via a targeted `sed`, not manual editing of code).

---

## 6. Where the data & logs are (under `trading/`)

| Path | What | Committed? |
|---|---|---|
| `data/YYYY-MM-DD/ticks.jsonl` | raw WS ticks per day (has `lp`, `bp1`/`sp1` bid/ask, `ft`) | yes (used for backtests/charts) |
| `data/st_state/{SYMBOL}.json` | per-stock ATR/EMA/trend warm state | gitignored |
| `data/runner_state.json` | position ledger (survives restart) | gitignored |
| `data/today_basket.json` | today's selected 5 stocks (written by `select_basket.py`) | gitignored |
| `logs/runner_<date>.log` | full runtime log | gitignored |
| `logs/trades_<date>.csv` | completed trades + P&L | gitignored (force-add to share) |
| `logs/strategy_<date>.csv` | **per-candle strategy state** (ohlc, atr, supertrend, trend, upper, lower, ema, position, signal, reason) — the real-time ground truth | gitignored |
| `logs/select_<date>.log`, `logs/token_<date>.log` | selection & token logs | gitignored |

To share logs/ticks for analysis: `git add -f trading/logs/<file>` (or ticks are already committed), commit, push; then this session pulls on the Mac.

---

## 7. Cron schedule (EC2, weekdays, times in UTC → IST)
```
0 13  * * 1-5   select_basket.py   (18:30 IST)  → picks NEXT session's basket + warms it
15 3  * * 1-5   start_trading.sh   (08:45 IST)  → token + singleton watchdog + runner
35 9  * * 1-5   stop_trading.sh    (15:05 IST)  → STOP flag halts watchdog
15 10 * * 1-5   replay_ticks.py    (15:45 IST)  → ATR warmup (legacy; select_basket now warms too)
```
Hands-free on weekdays; just fund the wallet before 08:45.

---

## 8. Current live config
- Per-stock cap **Rs5,000** own capital; deploy **≤85%** of wallet; **MAX_POSITIONS=2**.
- **MIS notional ×4** (leverage), **CNC ×1** (no leverage).
- **Long-only for CNC**, **long+short for MIS**.
- **Breakeven lock ON** (+1×ATR → stop to entry). Peak-trail & fixed-TP OFF.
- **EMA-50 filter ON** for the dynamic basket.
- **Selection:** Nifty 100 ranked by **volatility (10-day avg daily range%) + low-flip-count** (rank-sum), **top 5**, all MIS. Evening run stamps the *next* session (skips weekends).
- **Order pricing:** off **fresh best bid/ask, never LTP**; first attempt AT the quote, retries +1 tick, retry on every tick incl. quote-only, **confirm-before-book**, EOD verifies real exchange net position.

---

## 9. File map (`trading/`)
**Live system:** `runner.py` (main loop, ticks, candles, entries/exits, EOD, logging), `live_broker.py` (real Flattrade orders — pricing/retry/capital), `paper.py` (PaperBroker + PaperFill), `client.py` (Flattrade REST+WS), `auth.py` (session token), `config.py` (IST, EOD times), `marketdata.py` (Candle/CandleBuilder/Tick), `strategies/supertrend.py` (the strategy), `strategies/base.py`.
**Live ops:** `select_basket.py` (daily selection + warm-up), `replay_ticks.py` (legacy warmup), `start_trading.sh` / `stop_trading.sh` (flock singleton + STOP flag), `generate_token.py`.
**Backtests (Mac-only, uncommitted unless noted):** `bt_ticks.py` (faithful tick replay), `bt_yahoo.py`, `bt_sweep.py`, `bt_robust.py` (OOS split), `bt_mis.py`, `bt_regime.py`, `bt_select.py` / `bt_select2.py` (daily-selection walk-forward), `bt_phase.py`, `bt_longonly.py`.
**Charts (Mac-only):** `chart_dual.py` (would-have trades, dual panel), `chart_strategy.py` (belt+EMA+trend+trades overlay), `chart_1min.py`, `chart_today.py`.
**`CHANGELOG.md`** — commit descriptions, newest on top.

---

## 10. Key findings & caveats (don't re-derive)
- **No robust edge:** OOS split-half caught overfitting; profitable-looking params didn't generalize; net is thin-to-negative after real costs.
- **The DP charge is the dominant cost killer:** Flattrade **Rs20 + GST = Rs23.60 flat per CNC delivery sell** — brutal on small frequent trades. **MIS has NO DP charge → MIS ≫ CNC** for this strategy.
- **Selection is the main lever** (trade only genuinely moving+trending names). Buy-and-hold beat it in an up-market.
- **Costs to model in any backtest:** zero brokerage (Flattrade), STT (0.1% both legs CNC / 0.025% sell MIS), exch 0.00307%, SEBI, stamp (0.015% CNC / 0.003% MIS buy), GST 18% on (brokerage+txn+SEBI), + Rs23.60 DP on CNC sells.
- **Backtest tick data only carries bid/ask ~16–34% of the time** and only ~15 sessions exist → can't build a reliable bid/ask fill model from it; the `lp±1tick` price model is a fair (slightly conservative) proxy.
- **DDPI (Rs175 one-time)** enables auto CNC-sell via API but does NOT reduce the DP charge.

---

## 11. Pending work
1. **Oversell fix (real bot bug):** the partial-fill handler can re-order a "remaining" qty while the original order is still filling → oversell → accidental short (happened live on UNIONBANK). Fix = don't chase a partial remainder until the original order is terminal, AND cap every exit at the **actual exchange net position**.
2. **Chart-from-log:** rewrite charts to read `logs/strategy_<date>.csv` (real logged state) instead of recomputing.
3. Optional tuning: multiplier / EMA period / timeframe sweeps (always OOS-validate before trusting).

---

## 12. How to work in this project (style)
- Edit on Mac → **test (compile + pyflakes + stub)** → short commit + CHANGELOG entry (no AI co-author) → push → `git pull` on EC2.
- **Never price orders off LTP** — always best bid/ask.
- Keep capital tiny; treat live as validation.
- Be honest about the edge; OOS-validate any strategy change before recommending it live.

*End of handoff. Start by reading the code and explaining the full strategy inch-by-inch (math + order handling), then proceed with the pending oversell fix if the user asks.*
