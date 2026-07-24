# HANDOVER — Algo-Trading Project (read this first)

*Self-contained handover for a fresh Claude session on a **new machine**. It carries the full
state of the live Supertrend algo-trading system + the research lab + the new options data-logging
and backtest work, so you can continue without re-deriving anything. Written 2026-07-25.*

---

## 0. FIRST TASKS ON A NEW MACHINE
1. **Read the code and understand the whole strategy** before changing anything — the math (ATR,
   Supertrend bands, EMA-50 filter, flips), entry filters, every exit rule (hard SL / breakeven /
   chandelier trail / EOD), sizing/leverage, daily selection, warm-up, and **order handling**
   (pricing off best bid/ask, IOC retries, confirm-before-book, EOD reconcile). Confirm your
   understanding matches `runner.py`, `live_broker.py`, `strategies/supertrend.py`,
   `select_basket.py`. The full reference is [`README.md`](README.md).
2. **Read §A below (HOW TO WORK) before touching anything** — it is the most important section;
   most mistakes come from breaking these rules.

---

## A. HOW TO WORK — operating rules (behave exactly like this)

### The golden interaction model — you edit the Mac, AWS is the user's hands
- You (Claude) run commands **only on the user's local Mac clone**. You do **NOT** have SSH or
  console access to EC2 — never assume you can run anything on AWS.
- Anything that must happen on AWS, you hand to the user as **copy-paste command blocks**; the user
  runs them on EC2 and pastes the output back. **Wait for that output** before concluding it worked
  — don't declare an AWS step successful you didn't see succeed.
- **Git is the single source of truth:** Mac (edit) → GitHub (push) → AWS (`git pull`). AWS is
  **pull-only**. Never edit AWS files by hand and never tell the user to hand-edit AWS code. The one
  exception is `trading/.env`, which is edited directly on EC2 via a targeted command and never
  committed.

### The edit loop — do this for every code change
1. **Understand first.** Read the code. If the user is still forming their intent, **do NOT change
   code yet** — the user's explicit rule: *"don't do anything in the code until I understand."*
   Explain what's there and what you'd change, get the go-ahead, then act.
2. **Edit in the Mac clone only** (`projects/trading/`).
3. **Test before pushing — always:**
   - `python3 -m py_compile <files>` + `python3 -m pyflakes <files>`.
   - **Import smoke-test with heavy deps stubbed.** The Mac may lack `websocket`/`dotenv`/`requests`/
     `pyotp`, and importing `runner` has file side-effects — so stub the missing modules in
     `sys.modules` first, then import. Pattern:
     ```python
     import sys, types
     for m in ['dotenv','requests','websocket','pyotp']:
         sys.modules[m] = types.ModuleType(m)
     sys.modules['dotenv'].load_dotenv = lambda *a, **k: None
     sys.modules['websocket'].WebSocketApp = object
     import config, marketdata, paper, live_broker, client, auth, options_logger
     from strategies.supertrend import SupertrendStrategy
     print("imports OK")
     ```
   - Run `test_entry_retry.py` when touching entry/exit/order logic.
4. **Commit/push only when the user explicitly asks** (see §7). Then hand the user the AWS
   deploy + verify commands.

### How to give AWS commands
- One command per fenced ```bash block, no leading `$`, and **sanitize** any URL/remote that could
  contain the PAT: pipe through `sed -E 's#//[^@]*@#//***@#g'`.
- Always say **what the user should expect** in the output so they can tell success from failure.

### NEVER (these cause real damage)
- **Never auto-commit or push** without an explicit request.
- **Never add the AI as git author/co-author.** Commit as
  `git -c user.name='mukeshchandu' commit -m "..."` and note in the body that Claude wrote it.
- **Never commit, print, or log** `trading/.env` or the PAT. Sanitize every remote/URL you echo.
- **Never price orders off LTP** — always the fresh best bid/ask.
- **Never rewrite git history or force-push** — both repos are shared with AWS.
- **Never re-add heavy/research files** (`*.html`, `*.pkl`, tick data, backtest scripts) to the
  **live** repo — those belong in `trading-lab`.
- **Never let `options_logger.py` place orders** — it only records, and stays a separate process
  from `runner.py`.
- Don't expect a running `runner.py` to hot-reload — changes apply only on the next restart.

### Posture
Be honest about the edge (there isn't a retail-capturable one — §9); OOS-validate any strategy
change before recommending it; keep capital tiny; treat live as validation. For chart HTML
deliverables, just hand over the file path — the user opens it locally.

---

## 1. The two repositories

| Repo | What | AWS uses it? |
|---|---|---|
| **`github.com/mukeshchandu/projects`** (code in `trading/`) | **LIVE system** — lean, production runtime + ops + docs only | **YES** — AWS pulls this |
| **`github.com/mukeshchandu/trading-lab`** (private) | **RESEARCH lab** — all backtests, charts, 22 days of recorded tick data, the options backtest toolkit, and a self-contained copy of the live modules so research runs standalone | no |

The live repo was deliberately slimmed (research/charts/tick-data/pkl removed → they live in
`trading-lab`) so the AWS working tree stays clean. Don't re-add heavy/research files to the live
repo; `.gitignore` there now blocks `*.html`, `*.pkl`, `data/*/ticks.jsonl`, `data/options/`,
`*_results.*`.

---

## 2. New-machine setup

```bash
# 1) GitHub auth as the PERSONAL account (the old PAT/keyring died with the old Mac)
gh auth login          # choose github.com, HTTPS, account: mukeshchandu
#    OR create a new classic PAT (repo scope) at github.com/settings/tokens

# 2) clone both repos
gh repo clone mukeshchandu/projects
gh repo clone mukeshchandu/trading-lab

# 3) live repo venv + deps
cd projects/trading
python3 -m venv venv && source venv/bin/activate
pip install requests websocket-client python-dotenv numpy pandas yfinance pyotp playwright
#   (playwright is only for generate_token.py; run `playwright install chromium` if used)

# 4) create trading/.env (NEVER commit it) — see §5 for keys
```

**Pushing from the new Mac:** if `gh auth login` set up the git credential helper, `git push`
just works as `mukeshchandu`. If not, embed a fresh PAT in the remote:
`git remote set-url origin https://mukeshchandu:<PAT>@github.com/mukeshchandu/projects.git`
(and the same for `trading-lab`). **Never commit the PAT.** When printing a remote, sanitize:
`git remote -v | sed -E 's#//[^@]*@#//***@#g'`.

---

## 3. Deploy workflow (never edit AWS files by hand)

1. Edit in the Mac clone `projects/trading/`.
2. Test: `python -m py_compile <files>` + `python -m pyflakes <files>` (+ `test_entry_retry.py`).
3. Commit + push from the Mac (short message, CHANGELOG entry, **no AI co-author** — see §7).
4. Deploy on AWS: `git -C /home/ec2-user/projects pull`.
5. A running `runner.py` does NOT hot-reload — a mid-session pull only takes effect on restart
   (the 08:45 IST cron restarts it daily; for intraday deploy, stop+start).

---

## 4. AWS / EC2

- User `ec2-user`; project at `/home/ec2-user/projects/` (code in `.../trading/`), venv at
  `.../trading/venv/`. **Verify the current public IP** (changes on stop/start unless an Elastic IP
  is attached). SSH example (confirm IP/PEM): `ssh -i ~/Downloads/trading.pem ec2-user@<IP>`.
- The user runs all EC2 commands manually and pastes output back; the Claude session runs commands
  on the Mac only.
- **Cron (weekdays, UTC → IST):**
  ```
  30 2  * * 1-5   select_basket.py        (08:00 IST)  pick + warm today's 15-stock basket
  15 3  * * 1-5   start_trading.sh        (08:45 IST)  token + flock watchdog + runner
  35 9  * * 1-5   stop_trading.sh         (15:05 IST)  STOP flag halts watchdog
  15 10 * * 1-5   replay_ticks.py         (15:45 IST)  legacy ATR warmup
  20 10 * * 5     update_universe.py      (15:50 IST, Fri) refresh Nifty-100 universe
  ```
  Hands-free on weekdays; just fund the wallet before 08:45.

---

## 5. Secrets — `trading/.env` (gitignored; EC2 + local only, NEVER commit/log/print)
Keys: `FLATTRADE_USER_ID`, `FLATTRADE_API_KEY`, `FLATTRADE_API_SECRET`, `PASSWORD`, `TOTP_SECRET`,
`FLATTRADE_SESSION_TOKEN` (regenerated each morning by `generate_token.py`), optional
`MAX_POSITIONS` (currently 2), `EMA_FILTER`. Only `FLATTRADE_SESSION_TOKEN` is used at runtime.

---

## 6. Options data logging (NEW — 2026-07-24/25) & the backtest toolkit

**Why:** we want to research option strategies but have no historical option data. So the live
runner now also RECORDS NSE index-option chains, and the lab has a full options backtest toolkit.

**Single WebSocket per token (important):** Flattrade allows only ONE active WS per session token
(empirically verified — a 2nd connection disconnects the 1st). So options logging must NOT be a
separate process; it piggybacks on the runner's existing WS.

**Live repo (`projects/trading/`) — options logging is INSIDE `runner.py`:**
- At startup `runner.py` calls `_setup_options_logging()`: resolves the NIFTY/BANKNIFTY chains
  (nearest expiry, ATM ± N strikes) by reusing `options_logger._resolve_chain` (which uses
  `client.get_option_chain` → `GetOptionChain`), writes `data/options/<date>/<UNDER>_manifest.json`,
  and opens the jsonl files.
- In `on_open` it subscribes the option tokens as **full DEPTH (`d`)** and spot as touchline (`t`)
  on the SAME connection (equity is subscribed FIRST so a WS cap can't starve trading).
- Option & index ticks are routed out at the TOP of `handle_tick` (before the tk/tf filter and all
  trading logic) and written RAW (+ local `rt`) to `data/options/<date>/<UNDER>.jsonl`. The trading
  path only ever sees equity tokens → **order handling is unchanged**.
- **Flags:** `LOG_OPTIONS` (default on; `0` disables), `OPTION_FEED` (default `d`; `t` for lighter
  touchline). Setup is best-effort — any failure logs a warning and trading continues.
- `options_logger.py` remains as (a) the importable resolver module used by the runner and (b) a
  manual/`--dry-run` tool. **Do NOT run it standalone while `runner.py` is live** (2nd WS). There is
  intentionally **no options cron** and `start_options_logger.sh` was removed.
- **Verify on AWS:** `python options_logger.py --dry-run` should resolve ~42 instruments per
  underlying and write the manifest (this is the one piece needing live-API confirmation). Then in a
  live session, check `data/options/<date>/NIFTY.jsonl` grows and the runner log shows
  `OPTIONS LOGGING ON`. If resolution ever returns 0, drop a manual list at
  `data/options/instruments_NIFTY.json` (format in the logger header).
- Logged data is gitignored in the live repo. To move a day into the lab for backtesting:
  `git -C /home/ec2-user/projects add -f trading/data/options/<date> && git commit && git push`,
  then copy under `trading-lab/data/options/<date>/`.

**Lab repo (`trading-lab/options/`):** reads exactly the logger's schema.
- `greeks.py` (Black-Scholes price/greeks/IV, no scipy), `chain.py` (loads a day, no look-ahead),
  `bt_engine.py` (faithful engine: BUY→ask / SELL→bid fills, F&O cost model, 15:15 square-off,
  no look-ahead), `strategies.py` (9 templates: straddles/strangles long+short, `atm_directional`
  grafting the Supertrend flip onto ATM CE/PE, verticals, iron condor/fly), `sample_data.py`
  (synthetic day generator), `run_demo.py` (end-to-end demo).
- **Runnable NOW** on synthetic data: `cd trading-lab/options && python3 run_demo.py`. Verified:
  greeks self-test passes; 9/9 strategies run clean.
- ⚠️ **Reviewer TODOs** (flagged, not yet verified against reality):
  1. **Cost-model rates** in `bt_engine.py` (STT 0.10% sell, exch txn 0.03503%, SEBI, stamp 0.003%
     buy, GST 18%) are post-Oct-2024 estimates — **verify against a live contract note**.
  2. `atm_directional`/vertical Supertrend defaults were loosened (5-min, period 7, mult 2.0) just so
     the synthetic demo exercises all code paths — **retune for the real regime**.
  3. All demo results are synthetic → prove plumbing, not edge.

---

## 7. Conventions (agreed — do not violate)
- **NEVER auto-commit.** Commit/push only when the user explicitly asks.
- **Short** one-line commit messages; add a **`CHANGELOG.md`** entry (newest on top).
- **Do NOT add the AI as author or co-author.** (Note in the body that Claude wrote it.)
- **Never price orders off LTP** — always the fresh best bid/ask.
- Keep capital tiny; treat live as validation. OOS-validate (both-halves-positive) any change.
- HTML chart deliverables: just hand over the file path; the user opens it locally.

---

## 8. Current live config (as deployed)
15-stock morning-selected basket (Nifty-100 ranked by volatility + low-flip-count); Supertrend
ATR-14 / mult-1.5 + EMA-50 entry filter; entry filters = time window [09:30–14:15 start] +
EMA-gap ≥ 0.3×ATR; "trailtight" exit (hard SL 1.5×ATR / breakeven 0.5×ATR / chandelier 1.5×ATR /
EOD 15:00); entry retry-on-cancel (attempts 1–2 at fresh quote, 3+ cross 1 tick, max 3);
per-stock cap ₹5,000, `MAX_POSITIONS=2`, deploy ≤85% of wallet; MIS = long+short (no DP charge),
CNC = long-only. Wallet is tiny (~₹1,500).

---

## 9. The honest verdict (don't re-derive)
Exhaustive OOS testing found **no retail-capturable edge**: TA (9 strategy types) has no
generalizing signal; order-book/ML signal is real but **sits under the spread** (taker loses
~4 bps/trade, maker-only); the cross-asset daily "edge" was a look-ahead leak (52% after de-leaking,
loses to buy-and-hold). MIS ≫ CNC (the ₹23.60 flat DP charge per CNC sell is the dominant cost
killer). The live system is hardened/safe but **should not be expected to profit** — it's a
validation/learning system. Full detail in `trading-lab/README.md`.

---

## 10. Pending work
1. **Verify `options_logger.py` instrument resolution on the live API** (§6) — the main open item.
2. **Options cost-model rates + strategy retune** (§6 reviewer TODOs).
3. **Oversell fix (real bug):** the partial-fill handler can re-order a "remaining" qty while the
   original is still filling → oversell → accidental short (seen live on UNIONBANK). Fix: don't
   chase a partial remainder until the original order is terminal, and cap every exit at the actual
   exchange net position.
4. `update_universe.py` NSE fetch is unverified on EC2 (503s common; CSV fallback handles it).

*End of handover. Start by reading `README.md` + the code, then continue with whatever the user asks.*
