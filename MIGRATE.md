# MIGRATE.md — Scout bot handoff to the Mac Mini

You (Claude) are taking over a running project. Read this whole file before touching anything.

## What this is

"Scout" — a Polymarket **paper-trading** bot for BTC 5-minute Up/Down markets. It watches
Binance spot + the raw Chainlink oracle (the settlement feed) against Polymarket CLOB books,
buys mispriced sides mid-window, and settles at window end. Everything here was built and
tuned on evidence from a 30-day backtest (8,639 windows) plus its own live paper record.

**Status at handoff (2026-09-01):** paper bankroll **$109.68 from a $50 start in 5 days**,
88 closed trades, 45.5% win rate, +16.4% ROI on staked capital, every day since 08-29
profitable. This machine (Mac Mini) is becoming the 24/7 production host because the old
host was a lid-closing laptop.

## Hard rules (do not break)

1. **LIVE stays 0.** Never set `LIVE=1`, never touch `POLYMARKET_PRIVATE_KEY`, never execute
   real-money trades. Going live is the USER's own manual action, and only after the gate below.
2. The live-money gate the user agreed to: **~200 closed fills AND t-stat ≥ 2 AND 7+
   consecutive positive days.** When asked "should we go live", evaluate against this, honestly.
3. Paper data in `data/` is the track record — never reset or delete it casually.

## Architecture (all paths relative to repo root)

| Piece | What it does |
|---|---|
| `scout/agent.py` | main loop: universe scan → score → veto → size → execute → exits. `run_loop` auto-resets paper halts after a 30-min cooldown (`_paper_halt_wait`); campaign-target halt still stops it. |
| `scout/crypto_lag.py` | the signal: spot vs window open, vol-scaled z, `p_up_from_move(calibrated=True)` blends an empirical table (`scout/calibration.json`). Entry filters: `CRYPTO_ASSETS`/`CRYPTO_WINDOWS_MIN`/`CRYPTO_MIN_ASK` env. |
| `scout/streams.py` | websocket caches: raw Chainlink ticks + Binance spot (Polymarket RTDS) + CLOB books w/ dynamic resubscribe. REST fallback everywhere. Cut median signal age 4449→1767ms. |
| `scout/twap_lock.py` | endgame module: integrates observed oracle over the settlement TWAP's final 60s, buys near-certain sides at 30–90s left. 0 fires so far (strict by design). |
| `scout/lessons.py` | learning DB: every closed trade → price-band pattern stats (`learning/lessons.json`). Vetoes bands with proven negative record. Live overrides the backtest prior only when a one-sided binomial test says p<0.05 (or n≥30). |
| `scout/risk.py` | deterministic veto chain (max positions, correlated assets, loss cooldown, lesson veto, stake caps). |
| `backtest/` | 30-day replay harness + cache (klines, windows) + `calibrate.py` (refit the calibration table; walk-forward validated). |
| `learning/postmortem.py` | forensic per-trade report → `learning/POSTMORTEM.md`. |
| `dashboard/server.py` | stdlib HTTP server on **:8787**, serves `dashboard/index.html` + `/api/state` (auto-refresh UI: equity curve, trades, risk rails, lessons table). |
| `tests/` | 77 tests, all must pass. |

## Tuning history (why the numbers are what they are)

- `CRYPTO_MIN_ASK=0.25` — backtest: asks 0.05–0.15 won 7.5% (needing ~11%), −36%/$ staked; live 0-for-3. Longshots are poison; three layers now block them (floor, lessons veto, calibration).
- `MAX_CRYPTO_STAKE=5` — one $8 max-stake loss ate 2/3 of the $12 session-drawdown rail; $5 lets the rail absorb ~2.5 losses.
- Calibration (`scout/calibration.json`) — Gaussian tails gave longshots ~2x their true odds (z −2..−1.4 @60s: modeled 4.5%, measured 2.1%, n=566). Refit after any new backtest: `python backtest/calibrate.py`.
- The money band is **0.25–0.40 asks**: n=60 live, 43% win, +0.41/$ staked. Favorites (0.60–0.92) underperform live vs backtest (stale-quote optimism) and are currently lesson-vetoed on significant evidence.
- `CAMPAIGN_TARGET_USD=500` — hitting target halts for a human decision (raised from 100 after it was hit on 09-01).
- Python must run UTF-8 (`-X utf8`): a `→` in a rare code path once crash-looped the bot for 11 hours on cp1252 Windows. Keep the flag on any host.

## Mac Mini setup — do this in order

1. Python 3.11+ (`python3 --version`; if old, `brew install python@3.12`).
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. Create `.env` (repo root; gitignored). Copy `.env.example`, then apply these known-good values:
   ```
   STARTING_BANKROLL=50
   LOOP_SECONDS=20
   CRYPTO_ASSETS=btc
   CRYPTO_WINDOWS_MIN=5
   CRYPTO_MIN_ASK=0.25
   LESSONS=1
   MAX_CRYPTO_POSITIONS=2
   MIN_CRYPTO_STAKE=2
   MAX_CRYPTO_STAKE=5
   CRYPTO_KELLY_MULT=0.50
   CRYPTO_MIN_EDGE=0.03
   CRYPTO_BANKROLL_FRAC=0.40
   MAX_SESSION_DRAWDOWN=12
   PROFIT_LOCK_FRACTION=0.50
   CAMPAIGN_TARGET_USD=500
   TAKER_FEE_RATE=0.07
   LIVE=0
   RELAYER_API_KEY=            # ask the user to paste this (never commit it)
   RELAYER_API_KEY_ADDRESS=0x...       # the address shown next to your relayer key
   POLYMARKET_SIGNATURE_TYPE=3
   ```
   `XAI_API_KEY` stays empty — the bot runs `--no-grok`.
4. Verify: `.venv/bin/python -m pytest tests -q` (77 pass) then `.venv/bin/python -m scout once --no-grok`.
5. **24/7 service** — create two launchd user agents in `~/Library/LaunchAgents/`:
   - `com.tradeinc.scout.plist`: runs `.venv/bin/python -X utf8 -u -m scout loop --no-grok`
     with `WorkingDirectory` = repo root, `KeepAlive` true (this replaces the Windows
     supervisor.ps1 — launchd IS the supervisor), stdout/err to `data/night.log` / `data/night.err.log`.
   - `com.tradeinc.dashboard.plist`: runs `.venv/bin/python dashboard/server.py`, KeepAlive true.
   `launchctl load` both, verify with `launchctl list | grep tradeinc`.
6. Sleep: a Mac Mini has no lid, but tell the USER to set System Settings → Energy →
   "Prevent automatic sleeping when the display is off" ON (you must not change system
   settings yourself). `caffeinate` in the plist is a fallback.
7. Verify end to end: dashboard at http://localhost:8787 shows RUNNING; `data/night.log`
   ticks; a paper POST appears within an hour or two.
8. Windows leftovers to ignore/delete: `supervisor.ps1`, `Start Dashboard.bat` (the old
   host's wrappers). The old laptop's bot must be STOPPED once this host trades —
   two bots would double-count the same ledger history divergently. The user handles that
   from the old machine's chat.

## Daily operation

- Status: `python -m scout status` or the dashboard.
- Performance/latency/Brier: `python -m scout performance`.
- Forensics + learning refresh: `python learning/postmortem.py`.
- The bot halts itself on: $12 session drawdown, profit-lock floor, 2-loss streak
  (all auto-reset after 30 min in paper), campaign target $500 (stays halted for the user).
