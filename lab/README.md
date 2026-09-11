# Strategy lab

One strategy interface, two evaluations: a **replay** over cached history and a **paper
arena** on the live streams. The exact same `Strategy` objects run in both, so what you
backtest is what you paper-trade.

```
lab/strategies.py   State + Strategy + the catalog (31 variants, 10 families)
lab/replay.py       honest replay over lab/cache + backtest/cache  ->  lab/results/*.json, data/lab/backtest_leaderboard.json
lab/arena.py        paper arena on live books (launchd: com.tradeinc.lab)  ->  data/lab/ledgers.json, leaderboard.json, ticks/
lab/data.py         history fetcher: Gamma outcomes + CLOB 1-minute price history + Coinbase klines
tests/test_lab.py   decisions on synthetic states, fill model, sizing, ledgers
```

## Run

```bash
.venv/bin/python -m lab.data --assets btc,eth,sol --windows 5,15 --days 10 --btc5-extend   # refresh history
.venv/bin/python -m lab.replay --max-slip 1.0                                              # all assets, honest fills
.venv/bin/python -m lab.replay --assets btc --windows 5 --since 2026-08-07 --tag twap      # TWAP regime only
launchctl kickstart -k gui/501/com.tradeinc.lab                                            # restart the arena
tail -f data/lab/arena.log
```

The dashboard's **Strategy lab** panel (http://localhost:8787) shows paper and replay side by side.

## Fill model (the part that decides everything)

History is CLOB last-trade prices at 1-minute fidelity. Pricing a fill at the *last* print
is fantasy: the print is up to a minute stale and the fresh price is what triggered the
signal, so every strategy looks like a money machine (the first run showed t ≈ 18 for
Scout's model). The replay therefore fills **taker orders at the first print after the
decision** (`--exec next`, default) with a `--max-slip` cap (default 0.05; use 1.0 for the
worst case), and **maker orders only when a later print trades through the limit** with
zero fee. Outcomes are real Gamma resolutions. Stats are on flat $5 stakes so strategies
compare on signal quality; each strategy also compounds its own sizing rule.

The arena fills taker orders at the live best ask when the displayed size covers the order,
and maker orders when the ask trades down to the limit; both settle on Gamma's resolution
90 s after the window (oracle proxy after 12 failed lookups).

## Selection protocol (pre-registered, do not move the goalposts)

With 31 variants the best null strategy is expected to show t ≈ 2.4 by luck, so:

1. **Replay stage** — ret/$ ≥ +0.03 after fees, t ≥ 3, n ≥ 100, both halves of the period
   positive. Also look at pre/post Aug 7: an edge that only exists after the TWAP switch is
   structural (good) but young (watch for decay).
2. **Paper stage** — same bar on the arena's own fills after ≥ 100 settled trades, plus a fill
   realism check (share of taker attempts skipped for size, maker unfilled rate).
3. **Promotion** — the survivor runs as its own production-style paper bot for 200 fills and
   must pass the existing live-money gate (200 fills, t ≥ 2, 7 positive days). Going live is
   the user's manual decision; the lab never sets LIVE.
4. **Retirement** — rolling 7-day ret/$ below zero, or t < 0 over the last 100 trades → demote.

## Adding a strategy

Subclass `Strategy`, implement `decide(state) -> Order | None`, set `family`, `assets`,
`windows`, `entry` (seconds-left band) and `sizing`, add it to `catalog()`, and add a test in
`tests/test_lab.py` that pins its behaviour on a synthetic `State`. The arena picks it up on
restart with a fresh $50 ledger; the replay includes it on the next run.

## Tick recorder (data/lab/ticks/)

One JSON array per second per active window, gzipped at the daily rollover:

```
[t, asset, mins, epoch, raw_oracle, twap60, open_ref, yes_ask, yes_bid, no_ask, no_bid, yes_ask_size, sigma]
```

`raw_oracle` is the latest Chainlink tick, `twap60` the 60 s TWAP stream value, `open_ref`
the reference the arena scored against (TWAP at window start, Binance 1-minute open as a
fallback), asks/bids from the CLOB websocket (REST fallback when the stream is quiet), and
`sigma` the realized full-window volatility used by the models. With outcomes from
`data/lab/backtest_leaderboard.json`'s sources (Gamma) this becomes a 1-second replay set
with real book depth — the dataset the 1-minute history cannot provide.

## Tick replay (the real-book test)

```bash
.venv/bin/python -m lab.replay_ticks                  # every strategy on data/lab/ticks, real books
.venv/bin/python -m lab.replay_ticks --only mm_fair3  # one strategy
```

Fills at the recorded best ask of that second when the displayed size covers $5; maker
orders fill when a later ask trades down to the limit; outcomes from `lab/cache` or a Gamma
lookup (cached in `lab/cache/outcomes.json`). Writes `data/lab/tick_leaderboard.json`, which
the dashboard shows as the "Ticks" columns. History accessors on `State` never return points
after the state's own timestamp, so strategies cannot peek.

## Indicator study

```bash
.venv/bin/python -m lab.features_study
```

Joins `data/features.jsonl` (15-second external indicators) with BTC 5m outcomes and with the
bots' fills: per-indicator AUC and quintile hit rates, plus alignment tests for trades. Tick
rows written after 2026-09-08 carry three extra columns: Coinbase price, bid size, ask size.

## 2026-09-10 — live mirror and dry run

* `lock_live_mirror` (catalog): the exact rule proposed for the live job — settlement-lock
  |z| >= 3.5 inside the last 8–45 s, favourite asked 0.955–0.98, all assets, 5m and 15m. It is
  the forward paper control for `docs/STRATEGY_RESEARCH.md` section 12; judge it on the arena
  leaderboard like any other strategy.
* `python -m lab.lock_dryrun --minutes 30`: runs the live loop's own collect → books → score →
  size path with the lock profile environment and prints `LOCK` / `WOULD BUY` lines. Read-only.
* `LockTwap` gained `min_z` and `entry` parameters.
