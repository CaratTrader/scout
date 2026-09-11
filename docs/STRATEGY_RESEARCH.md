# Scout — strategy review, findings, and integration roadmap

_Written 2026-09-04 on the Mac mini host, from the live paper record (103–104 closed trades,
Aug 28 → Sep 4), the journal (4.5k events), the 30-day backtest, and primary-source research.
Companion to the v2 dashboard at http://localhost:8787._

---

## 1. What the bot actually does

Scout trades Polymarket's **"Bitcoin Up or Down" 5-minute markets**. Each window resolves
**Up** if Chainlink's **60-second BTC/USD TWAP at the window end is ≥ "the price at the
beginning of that range"**, otherwise Down (market description, verified live on Gamma; the
resolution source is `btc-usd-twap-60s-streams`).

The signal (`scout/crypto_lag.py`):

1. `open` = the 60 s TWAP stream value nearest the window start; `spot` = the **latest 60 s
   TWAP value** (fallback: Binance.us 1-minute kline open / REST ticker).
2. `chg = spot/open − 1`, scaled by a realized-vol estimate from the last 30 one-minute
   Binance closes (σ per window ≈ 0.08 % today) → `z`.
3. `p_up = Φ(z)` shrunk toward an **empirical calibration table** (z-bucket × seconds-left)
   fitted on 30 days of Binance klines, then blended **75 % model / 25 % CLOB mid**.
4. Buy a side when `p_side − ask − fee(ask) ≥ 0.03`, ask ∈ [0.25, 0.92], `fair − ask ≤ 0.20`,
   only **50–250 s before close**. Half-Kelly on 40 % of bankroll, stake $2–5, taker fills.
5. Hold to settlement. Deterministic vetoes: 2 crypto slots, same-asset, 900 s cooldown after
   a BTC loss, per-band "lessons" veto, session drawdown $12, profit lock, 2-loss streak.

Two other modules almost never fire: the 3 s **dip/complete-set tape** and the **TWAP lock**
endgame (0 fires so far).

## 2. The record, honestly

| Metric (paper, Aug 28 → Sep 4) | Value |
|---|---|
| Closed trades | 104 (44 W / 60 L, 42 %) |
| Realized P&L / fees | +$38.60 / $17.27 (5.8 % of gross wins) |
| Expectancy per trade | +$0.37, σ $6.28 → **t = 0.60** (90 % bootstrap CI −$0.55 … +$1.40) |
| Return per $ staked | +0.155 (t = 1.06) |
| Profit factor | 1.15 |
| Realized bankroll peak → now | $117.10 → $88.60 (**−$28.51, max drawdown $42.86**) |
| Brier, model vs market mid, taken trades | 0.260 vs 0.257 (coin flip = 0.250) |
| Brier, all 472 scored candidates (incl. vetoed, outcomes resolved) | 0.239 vs market 0.236 |
| Mean forecast vs hit rate | 49 % forecast → 42 % realized (**overconfident by ~7 pts**) |
| Days positive | 4 of 8; current streak 1 |
| Median / p95 signal age at submit | 1.7 s / 3.7 s |

Live-money gate (200 fills, t ≥ 2, 7 positive days): **0 of 3**. At the current expectancy
and dispersion, t ≥ 2 needs roughly `(2σ/mean)² ≈ (2×6.28/0.37)² ≈ 1,150 trades` — about 90
days at 13 trades/day. **The gate can only be reached by raising the edge, not by waiting.**

The MIGRATE.md snapshot ($109.68, "every day since 08-29 profitable") was a local peak; the
three days that followed lost $30.

## 3. Diagnosis — where the edge is, and where it leaks

### 3.1 The state variable is stale (the biggest finding)

Splitting the same 104 trades by the **price source the bot used** at entry:

| Source at signal | n | Win | P&L | Return per $ |
|---|---|---|---|---|
| Chainlink **60 s TWAP** as spot (primary path) | 75 | 33 % | **−$31.36** | −0.10 |
| Binance spot (fallback path, fresh raw price) | 29 | 66 % | **+$69.96** | +0.55 |

Two-proportion z ≈ 3.0 (p ≈ 0.003) — large for this sample, though it is one of several
slices, and fallback trades cluster right after bot restarts, so treat it as strong evidence,
not proof.

Mechanism: a 60 s TWAP trails the raw price by ~30 s of drift. When BTC moves 10 bps in the
last 40 s, raw shows −10 bps, the TWAP shows ~−4 bps, and the book already prices Down at
~70 %. The model, fed the TWAP, computes a small `z`, a `p_up` near 0.45, blends to ~0.40,
and finds a "+6 % edge" buying Up at 0.31. That phantom edge lives exactly in the
0.25–0.40 band the bot now trades almost exclusively. Trade 4199292 today is a clean example:
`chg = 0.000 %`, model 0.525, market 0.305, bought YES 0.31, lost.

The calibration table was fitted on **Binance klines (raw prices)** and is being applied to
a **TWAP** state variable — a unit mismatch that also explains the 7-point overconfidence.

**Implemented (default off):** `CRYPTO_SPOT_SOURCE=raw` in `.env` makes the signal use the
latest raw Chainlink tick (≤ 3 s old) as spot while keeping the TWAP-at-start as the open.
Covered by `tests/test_crypto.py::test_score_spot_source_switch_uses_raw_oracle_tick`.
Flip it, restart the bot (`launchctl kickstart -k gui/501/com.tradeinc.scout`), and the
dashboard's "price source" breakdown will show `chainlink_raw` as its own row for a clean A/B.

### 3.2 The strategy has narrowed itself to one band

The lessons DB has vetoed 0.40–0.60 (n = 8, 25 % win), 0.60–0.80 (n = 12) and 0.80–0.92
(n = 6) on small samples; `CRYPTO_MIN_ASK` blocks < 0.25. All realized profit sits in
0.25–0.40 (n = 74, 40 % win, +$73.70), every other band is negative. The "underdog" bet is
therefore the whole strategy — and per 3.1 its apparent edge partly comes from stale inputs.
The backtest prior for the same band is only +0.014 per $.

Counterfactual outcomes for vetoed markets (dashboard "Vetoes" panel, 271 markets resolved):

| Veto | Markets | Would-be win | Ret/$ | P&L at $5 |
|---|---|---|---|---|
| loss_cooldown (900 s after a BTC loss) | 79 | 51 % | +0.13 | **+$52** |
| lesson 0.40–0.60 | 77 | 52 % | 0.00 | +$2 |
| already_in | 35 | 34 % | −0.34 | −$59 |
| same_asset | 33 | 39 % | −0.11 | −$18 |
| lesson 0.60–0.80 / 0.80–0.92 | 21 / 9 | 71 % / 100 % | +0.03 / +0.17 | +$3 / +$7 |

The cooldown is the one rail with a real cost; the lesson vetoes on favorites are roughly
neutral so far; `already_in`/`same_asset` are doing their job.

### 3.3 Fees

Taker fee = 0.07 × p(1−p) per share (verified against 2026 fee docs). At a 0.32 ask that is
1.5 ¢/share ≈ 4.8 % of stake; $17.27 of fees against $38.60 net. **Makers pay 0.** The code
already has `maker_buy_price` and paper `orders` with `paper_maker_fillable`, unused on the
crypto path. A resting bid one tick under the ask inside the entry window would have saved
~45 % of net P&L if filled; the paper `orders` mechanism can measure the fill rate.

### 3.4 Timing inside the window

Return per $ by seconds-left at entry: 180–240 s +$65.7 (n = 42), 120–180 s −$44.7 (n = 31),
60–120 s +$4.5 (n = 18). Not significant yet, but consistent with 3.1: the later the entry,
the more the TWAP lag matters relative to the remaining time.

### 3.5 Volatility input

σ per window today: bot 0.080 % (30 × 1 m Binance.us closes), Coinbase realized 1 h 0.086 %,
DVOL-implied 0.117 %, code default 0.24 % (used only when the kline fetch fails — 3× too
high). The 30-sample estimate is noisy; an EWMA over 1-minute returns blended with implied
vol would be steadier.

## 4. Settlement mechanics — verified, and still open

- Verified: BTC 5-minute markets resolve on the **60 s** TWAP stream (market description
  names `btc-usd-twap-60s-streams`; the bot subscribes to the 60 s topic). Announcements
  said 30 s; a post-launch analysis reports the unannounced switch to 60 s. The 30 s topic
  still streams and settles nothing — never point the bot at it.
- Open: whether "price at the beginning" is the **TWAP value at the start instant** (bot's
  assumption, Genfinity's reading) or a **raw snapshot** (tradoxvps' reading). The two agree
  in most windows and differ when the last minute before the open trended. The dashboard's
  **Settlement audit** records both boundary values for every window and checks which rule
  reproduces Polymarket's resolution; after a day (~288 windows) it will be decisive.
- Fees: crypto taker rate 0.07 (was 0.072), makers 0, per 2026 fee guides.

## 5. What to integrate — ranked

| # | Integration | Why | Effort | Status |
|---|---|---|---|---|
| 1 | **Raw Chainlink tick as spot** (`CRYPTO_SPOT_SOURCE=raw`) | 3.1: +0.65/$ gap between sources | done, default off | A/B in paper |
| 2 | **Re-fit calibration on live oracle data** | table fitted on Binance klines, applied to Chainlink | 1 day | dataset now logging (`data/windows_audit.jsonl` + per-second oracle series) |
| 3 | **Settlement audit → pin the open rule** | model reference must match resolution | automatic | running |
| 4 | **Maker execution in the entry window** | fees are 45 % of net; makers pay 0 | 1 day, paper first | code hooks exist |
| 5 | **Replace the 900 s cooldown** with "skip the next window" or a z-threshold | counterfactual +$52 / 79 markets | 1 h | measure first |
| 6 | **Order-flow features**: Coinbase trade imbalance (CVD), OKX taker buy share, liquidations | short-horizon OFI predictability is the best-documented microstructure effect (SSRN 2026; Cont et al.) | logging now, backtest next | `data/features.jsonl` |
| 7 | **σ from EWMA + DVOL blend** | 3.5 | 2 h | indicators live |
| 8 | **Tighter sampling inside the entry window** | loop cadence 35 s → each window scored ~6× ; TTL 3 s | 0.5 day | measure "seconds since candidate first seen" |
| 9 | **TWAP-lock feasibility count** | 0 fires; per-second oracle log shows how often |required move| > 3σ with ask ≤ 0.92 | 2 h | data available |
| 10 | **Alerts** (ntfy/Telegram) on halt, DOWN, or −$10 day | ops | 1 h | dashboard has the state |
| 11 | Hourly markets (Binance candle settlement) and ETH/SOL 5 m | different model / more samples | later | audit pipeline reusable |

Things researched and **not** recommended: Binance.com futures data (geo-blocked from this
host, HTTP 451), Bybit (403), Fear & Greed as a signal (daily, no 5-minute information — shown
for context only), "settlement sniping" (economically dead after the TWAP change).

## 6. The v2 dashboard

`dashboard/server.py` (stdlib HTTP, 127.0.0.1:8787) + `dashboard/index.html`. Read-only
toward the bot. Runs the bot's own signal math on the same Chainlink/CLOB streams once per
second, so the live panel shows exactly what the bot would score — plus the raw-spot variant
side by side.

| Panel | Question it answers |
|---|---|
| KPI strip | equity, P&L today, win rate, profit factor, expectancy with t-stat and CI, drawdown from peak, fee drag, Brier vs market |
| Live window | countdown, phase, raw vs TWAP vs Coinbase, both `chg` definitions, z, model/market/blend p, both books with edge and filter verdicts, TWAP-lock status |
| Positions / rails | open positions with live mark, session baseline, floors, streak, cooldown |
| Market context | Coinbase/Kraken, OKX perp premium, funding, OI, long/short, taker buy share, liquidations, σ realized vs implied (DVOL), momentum, Fear & Greed — every poll appended to `data/features.jsonl` |
| Bankroll + drawdown, daily, per-trade, rolling 20 | performance shape, not just the total |
| Live-money gate | 200 fills / t ≥ 2 / 7 positive days with ETA |
| By entry price / hour / weekday / signal quality | where P&L comes from (seconds-left, disagreement, edge, z, age, source, side) |
| Calibration | reliability curve, model vs market, taken trades and all 472 candidates |
| Vetoes | counts and counterfactual P&L per reason from resolved outcomes (`data/resolutions.json`) |
| Settlement audit | which "open" rule reproduces resolutions (`data/windows_audit.jsonl`) |
| Trades, lessons, decision feed, health, Monte Carlo | audit trail, learning DB, journal, launchd/cadence/errors, bootstrap of the next 200 trades |

Every chart has a table view; colors follow a CVD-validated palette; the page never blanks
on a partial file read (last-good parse). Restart: `launchctl kickstart -k gui/501/com.tradeinc.dashboard`.

## 7. Sources

- Polymarket TWAP docs — https://docs.polymarket.com/market-data/chainlink-twap
- Polymarket Developers announcement (Aug 7 switch) — https://x.com/PolymarketDevs/status/2082813706996772881
- Chainlink TWAP streams settle 5/15-minute markets (Genfinity, 2026-08-12) — https://genfinity.io/2026/08/12/chainlink-twap-data-streams-polymarket-crypto-markets/
- Post-launch mechanics and the 60 s switch (tradoxvps) — https://tradoxvps.com/polymarket-twap-settlement/
- Crypto Briefing on the upgrade and liquidity rewards — https://cryptobriefing.com/polymarket-twap-upgrade-liquidity-rewards/
- Fee formula and 2026 rates — https://polyguana.com/learn/polymarket-fees , https://pineanalytics.substack.com/p/polymarket-fee-rollout
- Order-flow imbalance and short-horizon crypto returns (Vafin, SSRN 2026) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6938742
- Order-flow imbalance as an HFT signal (Markwick) — https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html
- Latency bot write-ups for these markets — https://github.com/learningworship/polymarket-latency-bot , https://www.quantvps.com/blog/how-latency-impacts-polymarket-trading-performance
- PMData Chainlink TWAP dataset — https://pmdata.dev/docs/datasets/chainlink-twap

---

## 8. Strategy lab — 31 strategies, one honest evaluation (added 2026-09-05)

### 8.1 Where edge can come from in this market, and where it cannot

A 5-minute Up/Down binary settled on a 60-second Chainlink TWAP is a very specific
instrument. Ranking the possible edges by how a professional would weigh them:

1. **Settlement structure (the real one).** Since Aug 7 the close is an average, so a
   lead with 60–90 s left can only be overturned by a sustained reversal, not a print.
   Anyone still pricing snapshot-era flip risk under-prices late favourites. This is
   explainable, measurable, and will decay as market makers adapt — exploit and monitor.
2. **Favourite–longshot bias.** Bettors overpay for longshots in every betting market
   studied (Snowberg & Wolfers; prediction-market literature). Our data agree: every
   underdog-buying variant loses, every favourite variant wins. Scout's de-facto book
   (25–40¢ underdogs) sits on the wrong side of this bias.
3. **Speed.** The raw oracle and Coinbase lead the TWAP by seconds and the book reprices
   in seconds. That race is won by co-located sub-second bots, not a 35-second loop on a
   Mac mini. Scout's "lag" edge is this race, which is why it evaporates under honest fills.
4. **Execution.** Passive bids in a market whose underlying moves get filled exactly when
   the world has turned against you (Glosten–Milgrom adverse selection). The replay puts a
   number on it: resting the lag signal at the bid returns −0.33 per $ over 4,391 fills.
5. **Order flow.** Book jumps are informed: fading them loses −0.17 per $ (t = −8);
   following them after the fact is already too late (−0.02). Flow features belong inside
   the probability model, not as a stand-alone trigger.
6. **Breadth.** Information ratio ≈ IC × √breadth (Grinold–Kahn). 288 windows a day per
   asset is enormous breadth: a small, real, independent edge compounds quickly, and many
   small bets beat few large ones. The lock strategies fire 15–35 times a day.
7. **Sizing under fat tails.** A 0.93 entry loses 100 % in the 1–3 % of windows that flip.
   Fractional Kelly on the *measured* edge, never on the claimed one; the arena's paper
   record is that measurement.
8. **Multiple testing.** The best of 31 null strategies shows t ≈ 2.4 by luck. The bar is
   t ≥ 3 with both halves positive, then the same bar again on out-of-sample paper fills.

### 8.2 Replay results — BTC 5m, Jul 29 → Sep 4 (37.5 days, 10,775 windows), fills at the next print, no slip cap

| Strategy | n | Win | Ret/$ | t | Avg fill | Pre-Aug 7 | Post-Aug 7 | Verdict |
|---|---|---|---|---|---|---|---|---|
| lock_k3 (≤90 s, lead ≥3σ of remaining avg) | 618 | 98.5 % | +0.096 | 7.9 | 0.93 | −0.014 | **+0.136** | pass |
| lock_k2 (≤90 s, lead ≥2σ) | 1,406 | 97.0 % | +0.064 | 7.7 | 0.93 | −0.011 | **+0.092** | pass |
| mom_z15_late (\|z\|≥1.5, last 130 s) | 1,308 | 97.2 % | +0.062 | 7.4 | 0.94 | +0.004 | **+0.083** | pass |
| mom_z1 (\|z\|≥1, ask ≤0.85) | 3,014 | 92.1 % | +0.023 | 3.5 | 0.91 | −0.017 | +0.035 | borderline |
| fav_lock_90 | 653 | 98.8 % | +0.003 | 0.6 | 0.98 | | | no edge after fees |
| mom_z1_maker | 433 | 59 % | +0.014 | 0.3 | 0.63 | | | unproven |
| lag_twap (Scout as deployed) | 9,044 | 61 % | −0.050 | −5.1 | 0.62 | −0.058 | −0.048 | negative |
| lag_raw / every lag variant | 2–9k | 61–78 % | −0.02 … −0.09 | −2 … −6 | | | | negative |
| fade_flat (underdog when flat) | 4,861 | 39 % | −0.073 | −3.7 | 0.40 | | | negative |
| fade_jump | 7,868 | 28 % | −0.171 | −8.1 | 0.30 | | | negative |
| lag_raw_maker | 4,391 | 31 % | −0.331 | −21 | 0.46 | | | toxic |

Sensitivity: the stale last-print fill turns every strategy positive (Scout +0.21/$, t = 19);
a 0.05 slip cap gives lock/momentum +0.5…+0.7/$ on a calmer subset. The no-cap numbers
above are the conservative ones. ETH/SOL 5m and BTC 15m histories are fetched by
`lab.data` and appear in `data/lab/backtest_leaderboard.json` and the dashboard once the
replay is re-run; the partial BTC 15m replay (957 windows) shows the lag model flat there
(+0.014/$, t = 0.5).

### 8.3 What is running now

`com.tradeinc.lab` runs all 31 strategies in paper on live BTC/ETH/SOL 5m and 15m books,
one $50 ledger each, and records every second of every window to `data/lab/ticks/` for
future replays with real book depth. The dashboard's Strategy lab panel ranks paper and
replay side by side; `lab/README.md` has the promotion rules. The two lock strategies and
late momentum are the candidates; Scout's lag family is the control that should keep
losing if the analysis is right.

### 8.4 Final replay leaderboard — all six datasets (BTC/ETH/SOL × 5m/15m, 19,384 windows, fills at the next print, no slip cap)

| Strategy | Markets | n | Win | Ret/$ | t | Max DD ($5 flat) | Pre-Aug 7 | Post-Aug 7 |
|---|---|---|---|---|---|---|---|---|
| lock_k2_alt | ETH+SOL 5m | 1,071 | 98.2 % | **+0.174** | **14.0** | $11 | — (data starts Aug 25) | +0.174 |
| lock_k2_15m | BTC 15m | 101 | 98.0 % | +0.411 | 8.3 | $5 | — | +0.411 |
| lock_k2_offhours | BTC 5m, not 11–17 UTC | 1,007 | 96.9 % | +0.089 | 8.3 | $35 | −0.016 | +0.129 |
| lock_k2_cheap | BTC 5m, ask ≤ 0.85 | 913 | 96.5 % | +0.099 | 8.2 | $23 | −0.003 | +0.132 |
| mom_z15_cheap | BTC 5m | 875 | 96.9 % | +0.096 | 8.1 | $26 | +0.017 | +0.120 |
| lock_k3 | BTC 5m | 618 | 98.5 % | +0.096 | 7.9 | $15 | −0.014 | +0.136 |
| lock_k2 | BTC 5m | 1,406 | 97.0 % | +0.064 | 7.7 | $42 | −0.011 | +0.092 |
| mom_z15_late | BTC 5m | 1,308 | 97.2 % | +0.062 | 7.4 | $32 | +0.004 | +0.083 |
| mom_z1 | BTC 5m | 3,014 | 92.1 % | +0.023 | 3.5 | $138 | −0.017 | +0.035 |
| fav_lock_90 · lag_raw_15m · mom_z1_maker | | | | ≈ 0 | < 1 | | | |
| every lag variant (BTC/ETH/SOL), x_btc_eth/sol, fade_flat, follow_jump | | 2–9k each | | −0.02 … −0.09 | −1.6 … −6.2 | | | |
| fade_jump · lag_raw_maker | | 7,868 · 4,391 | 28 % · 31 % | −0.171 · −0.331 | −8 · −21 | | | |

Per asset, the same lock rule: ETH +0.184/$ (n = 512, t = 9.6), SOL +0.165/$ (n = 559, t = 10.2),
BTC +0.064/$ (n = 1,406, t = 7.7). Thinner books mis-price late leads more. Eight variants clear
the pre-registered bar; all eight are the same idea (a late, multi-sigma lead under TWAP
settlement), so treat them as one strategy family with several dials, not eight independent
confirmations. The arena decides which dials survive real books.

---

## 9. Real-book verdicts and executive decisions (2026-09-06)

The arena ran 37.5 hours on live BTC/ETH/SOL 5m and 15m books (7,727 settled paper fills) and
recorded 533k one-second book snapshots over 1,737 windows. Replaying every strategy on those
recordings with fills at the displayed ask (size permitting) gives the first evaluation that
is not distorted by 1-minute history. Settlement audit: the **TWAP value at window start is
the opening reference — 455 of 455 resolutions**; a raw snapshot open would have mis-called 10 %.

| Strategy (real books, 1,181 windows) | n | Win | Ret/$ | t | Read |
|---|---|---|---|---|---|
| scout_clone (Scout's exact rules) | 76 | 37 % | +0.077 | 0.5 | noise; the cooldown is the only filter that helps (no-cooldown twin: −0.158) |
| lag_raw / lag_twap / every lag variant | 148–265 | 36–43 % | −0.05 … −0.17 | −0.6 … −2.0 | the model has no edge at taker prices |
| lock_twap_all (settlement-integral lock, 5 s persistence) | 90 | 84 % | −0.039 | −0.9 | the book already prices late leads at their true odds |
| lock_k2_alt, lock_k2, momentum variants | 3–31 | 67–81 % | −0.07 … −0.4 | | the 97 % replay win rates were an artefact of stale prints |
| mm_fair3_all (two-sided maker quotes) | 1,866 | 38 % | −0.100 | −2.9 | adverse selection; a 20 % maker rebate (≈0.3 ¢/share) cannot cover it |
| lag_raw_maker | 255 | 34 % | −0.148 | −1.8 | same |
| follow_jump / fade_jump | 257 | 64 % / 36 % | −0.047 / +0.005 | | nothing |

Scout itself is +$131.73 since the raw-spot switch (82 trades), but **all of it came from YES
underdogs (34 trades, +$146) while NO trades lost**, and its clone on the same days is +$30:
a favourable drift regime, not a repeatable edge. Overall record: 195 trades, t = 1.63.

**Decisions**

1. **No live money.** The gate is unmet (t 1.6, streak 2) and, more importantly, no
   strategy has a positive real-book t above 1. Connecting a wallet now would be paying
   fees to learn what paper already says. `LIVE` stays 0; going live is your manual act.
2. **Scout stays running in paper as is** (raw spot, band, cooldown): it is the control and
   the gate counter, and it costs nothing.
3. **Retire the price-model search on 5-minute taker entries.** Forty-nine variants, six
   markets, three evaluation methods: at 7 % taker fees the 5-minute book is efficient
   enough that a 35-second loop cannot beat it. Further parameter tuning is curve-fitting.
4. **Keep the arena and recorder running** — the tick set is the asset. Every new idea is
   tested there first (`lab.replay_ticks`), with the same bar: t ≥ 3, n ≥ 100, on real books.
5. **Where an edge could still exist**, in order: (a) hourly/4-hour markets, where fees per
   unit of information are lower and settlement is a candle, not a TWAP; (b) quoting that
   re-prices every second and cancels on adverse moves (true market making, needs
   infrastructure and capital, competes with professionals); (c) cross-market structural
   arbitrage (complete sets across venues) — rare and small. None of these is a weekend job.

Sources for the fee mechanics: [Maker Rebates Program](https://docs.polymarket.com/programs/maker-rebates), [Polymarket help: maker rebates](https://help.polymarket.com/en/articles/13364471-maker-rebates-program), [taker fees on 15-minute crypto markets](https://coinmarketcap.com/academy/article/polymarket-introduces-fees-on-15-minute-crypto-bets).

---

## 10. Live attempt, indicator study, and the one input that earned its place (2026-09-08)

**Live.** The user funded a wallet ($69.81) and started `com.tradeinc.scout.live` from this
Mac. It authenticated, read the balance and scanned correctly, but every order it tried was
rejected: `buy rejected: Trading restricted in your region` — Polymarket's CLOB geoblock
([policy](https://docs.polymarket.com/developers/CLOB/geoblock)) makes the US, UK, Canada,
Australia, most of the EU and others close-only: existing positions can be closed, no new
orders. Zero live trades; the job only produces rejections from this location.

**Indicator study** (`lab/features_study.py`; 943 BTC 5m windows over 92 h, indicator taken
at window start + 60 s, outcome = resolution):

| Indicator | AUC | Read |
|---|---|---|
| Coinbase − oracle basis (bp) | **0.597** | P(up) 0.37 → 0.62 across quintiles; the only real signal |
| funding premium, Fear & Greed | 0.53, 0.52 | noise-level |
| funding rate, OI change, long/short, taker buy share, liquidations, DVOL, realized σ, 15/60-min drift | 0.46–0.51 | nothing, as the literature says for positioning metrics |

The basis leads the oracle (1 bp of basis → +0.4–0.5 bp of oracle move over the next 15–60 s)
and it is only partly priced by the book. At the trade level on real books it separates every
taker family: lag_raw_fav +0.079/$ with the basis vs −0.086 against (n 283/171), mom_z1
+0.033 vs −0.208, lag_raw −0.016 vs −0.120, Scout's own paper trades +0.34 vs +0.03.
A 60-minute trend filter looked good on Scout's paper trades and bad on the arena's fills,
so it was rejected as not robust.

**Added:** a real-time Coinbase ticker feed in the arena (price, best bid/ask and sizes, ~10/s),
`State.basis_bps()` and `State.cb_imbalance()`, a `BasisFilter` and an `ObiFilter`, Coinbase-as-spot
lag variants, and nine forward-test strategies (`lag_cb`, `lag_cb_fav`, `lag_raw_basis`,
`lag_fav_basis`, `lag_fav_basis3`, `mom_z1_basis`, `scout_clone_basis`, `lag_fav_obi`,
`lag_fav_basis_all`). The tick recorder now stores Coinbase price and level-1 sizes so future
tick replays can test order-book-imbalance timing. Promotion bar unchanged: t ≥ 3 after 100
real-book fills. Sources: [OFI horizon evidence](https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html),
[order-imbalance nowcasting](https://pmc.ncbi.nlm.nih.gov/articles/PMC10040314/),
[open interest is positioning, not direction](https://bitsgap.com/blog/bitcoin-open-interest-explained).

## 11. Why live lost while paper won (2026-09-09)

Three live trades, three losses, versus a paper bot in profit. Not variance, not execution
(live fills landed at the decision prices, 1.3–1.9 s signal age). Two causes:

1. **Live did not inherit paper's lessons.** The lessons veto rebuilds its statistics from the
   *active* ledger. The live ledger had three fills, so it fell back to the 30-day backtest
   prior, which allows 0.40–0.60 asks. Paper's own 285-trade record says that band wins 25 %
   and returns −0.52 per $, so paper vetoes it. Two of the three live losses were 0.40–0.60
   entries paper would have refused. Fix: `LESSONS_SOURCE=data/ledger.json` (new env) makes
   the live bot learn from the paper ledger; set in the live plist.
2. **No Coinbase confirmation.** On real books, entries against the Coinbase–oracle basis
   lost ~0.1 per $ more than entries with it in every family. Fix: `CRYPTO_BASIS_MIN_BPS=0.15`
   (new env, off by default) starts a Coinbase ticker feed (`scout/coinbase.py`) and vetoes
   sides Coinbase does not already lead toward (`basis_disagree` / `basis_unknown` in the
   journal). Enabled for live; paper stays unchanged as the control.

Expectations stay modest: paper's edge is t ≈ 2 on 285 trades with a 40 % win rate, so runs of
three or four losses are routine even when everything works.

## 12. The late-window lock pocket — the one taker trade the real books still misprice (2026-09-10)

Five live trades, five losses, all underdogs at 0.25–0.46 from the lag model. Instead of tuning
that model again, the real-book tick recordings (`data/lab/ticks`, 5,622 windows across
BTC/ETH/SOL × 5m/15m, Sep 5–10) were cut the other way: *buy the favourite at the next-print
ask, one sample per window per cell, split by seconds left, fill price and the settlement-lock
z-score*. Fees at 0.07·p·(1−p) per share are included in every number.

**Market calibration (no signal).** With 60–120 s left the favourite is *over*-priced in every
price band (−1 to −6 ¢ per $ after fees); with < 60 s left it is fairly priced (≈ 0) except at
0.985+. No signal-free favourite trade works.

**The lock z adds the information the book lacks.** Inside the last 60 s, part of the 60 s
settlement average is already observed. `lock_signal` asks what the remaining seconds would
have to average to flip the outcome and expresses the distance in sigmas (z). Splitting the
< 60 s favourite trades by that z:

Without the z the favourite at 0.96–0.985 is a coin-toss after fees (weak-z cells: 96–99 %
win, −0.012 to +0.011 per $). With it, one window counted once per cell, fills at the next
print, all 5,643 recorded windows (outcomes complete after fixing the Gamma lookup — the
windows endpoint returns nothing for these slugs; the events endpoint resolves them):

| module \|z\| threshold, k < 60 s | fill 0.96–0.985            | fill 0.985–0.995           | fill 0.93–0.96            |
|----------------------------------|----------------------------|----------------------------|---------------------------|
| ≥ 2.5                            | 307 n, 4 losses, +0.011/$  | 646 n, 4 losses, +0.003/$  | 94 n, 5 losses, +0.003/$  |
| ≥ 3.0                            | 183 n, 3 losses, +0.008/$  | 380 n, 1 loss, +0.007/$    | 67 n, 4 losses, −0.003/$  |
| ≥ 3.3                            | 143 n, 1 loss, +0.018/$    | 309 n, 1 loss, +0.006/$    | 61 n, 4 losses, −0.009/$  |
| **≥ 3.5**                        | **133 n, 0 losses, +0.025/$** | 284 n, 0 losses, +0.009/$ | 51 n, 2 losses, +0.018/$ |
| ≥ 4.0                            | 107 n, 0 losses, +0.025/$  | 232 n, 0 losses, +0.009/$  | 38 n, 1 loss, +0.032/$    |

(The module `z` in `scout/twap_lock.py` carries the 1/√3 time-average shrink; module 3.3 ≈ a
plain 2-sigma requirement on the remaining average.)

Robustness of the 0.96–0.985 pocket at module |z| ≥ 3.3 (159 windows when the cut is the plain
z ≥ 2): ETH 56/0 losses, SOL 59/0, BTC 44/1; 5m 128/1, 15m 31/0; YES 76/0, NO 83/1; by day
21/0, 23/0, 67/0, 24/0, 15/1, 9/0. The single loss (BTC 5m, Sep 9, 15–20 s left) had |z|
between 3.3 and 3.5. **The profile uses 3.5.** Be clear about what that is: an in-sample choice
that removes exactly one loss and 7 % of the trades. Break-even loss rate at a 0.975 fill is
2.3 %; 1 loss in 143 is 0.7 %, so the pocket is profitable at either threshold — plan on a live
loss rate nearer 1 in 150 than zero, i.e. about +1.8 to +2.4 ¢ per $ after fees.

**Why the pocket exists.** At 0.97 the taker fee is 0.2 ¢ per share, so the hurdle is tiny.
Sellers at 0.97 with 30 s left are holders cashing out and NO buyers hoping for a reversal;
both price the residual 3 % on spot intuition. The settlement is a 60 s average, and once half
of it is observed a reversal needs an implausible sustained move, not a tick. The 0.93–0.96 band
*loses* with the same z: when the book disagrees with the arithmetic, the book tends to know
something (usually Coinbase already reversing — section 10). So the profile trades only where
the book *agrees*: 0.955–0.98.

**Liquidity.** At the favourite's ask in that cell: p10 $126, p50 $435 displayed. A $5 order
fills; so would $50.

**Economics at the current bankroll ($38).** The wide cut (z ≥ 2, k < 60) qualified 9–67
windows a day (median ~22) across the six markets; the profile's 8–45 s window at |z| ≥ 3.5
keeps about two thirds of those, ~15 a day. At $5 a trade and +2.4 % that is **$1.5–3 a day**,
4–8 % of bankroll daily — small in dollars, large as a rate. Raise `MAX_CRYPTO_STAKE` as cash
grows; the edge is per-dollar and the book supports $50 orders.

### What was built

* `scout/twap_lock.py`: every constant now has a `TWAP_LOCK_*` env override (`MIN_SECONDS`,
  `MAX_SECONDS`, `MIN_P`, `ASK_FLOOR`, `ASK_CAP`) plus `TWAP_LOCK_MIN_Z` (reject locks whose
  |z| is below it). Defaults unchanged → paper bot unchanged.
* `scout/crypto_lag.py`: `CRYPTO_LAG_MODEL=off` disables the lag/underdog model (the lock and
  the tape keep trading); lock sigma now comes from the oracle's own one-minute closes (the
  arena's definition, so live z matches the study) with the old Binance fallback;
  `CRYPTO_EVENT_CACHE_SECONDS` caches the Gamma event fetch.
* `scout/agent.py`: `CRYPTO_ONLY=1` skips the general market scan. The live cycle measured
  ~23 s of work on a 20 s sleep (43 s period); crypto-only with `LOOP_SECONDS=2` is what lets
  the loop act inside the last 45 s.
* `lab/strategies.py`: `lock_live_mirror` — the exact live rule in the paper arena (real
  books) as the forward control; watch it on the dashboard's Strategy lab panel.
* `lab/lock_dryrun.py`: read-only dry run printing what the live loop would buy.
* `docs/com.tradeinc.scout.live.lock.plist` + `live_switch_to_lock.sh`: the live profile.
  The switch is the user's action.
* Tests: `tests/test_lock_profile.py` (108 passing).

### The live profile

`CRYPTO_LAG_MODEL=off`, `LESSONS=0`, `CRYPTO_ONLY=1`, `LOOP_SECONDS=1`, `TAPE_SECONDS=1` (the
per-cycle tape scan blocks for this long; its 12 s default made the first live cycles ~20 s),
`QUIET_CYCLES=15`
(routine log/journal lines every 15th cycle; anything that trades always prints),
`CRYPTO_ASSETS=btc,eth,sol` + `CRYPTO_WINDOWS_MIN=5,15` (the `.env` paper filter is BTC 5m only,
which would leave ~1/5 of the opportunities), lock window 8–45 s, ask 0.955–0.98,
`TWAP_LOCK_MIN_Z=3.5`, `CRYPTO_MIN_EDGE=0.012` (with fair clamped at 0.995 this caps the fill at
~0.98), `CRYPTO_MIN_SECONDS_TO_EXPIRY=5`, TTL 6 s, stake $2–5, 3 slots (BTC/ETH mutually
exclusive by the correlation veto), 3-loss-streak halt only.

Dry run on the real market (`lab/lock_dryrun.py`, 2026-09-10 04:40 UTC): crypto-only cycle
median 0.47 s (p90 6 s on slow Gamma calls), so the 2 s loop sees every window's last 45 s
about 15 times.

### Next pockets, in order

1. 0.985–0.995 with |z| ≥ 3.5: 284 windows, 0 losses, +0.9 % per trade — needs
   `CRYPTO_MIN_EDGE≈0.004` and `TWAP_LOCK_ASK_CAP=0.99`; smaller edge, twice the opportunities.
2. Coinbase agreement as a second gate for the lock (untested here; the 0.93–0.96 losses
   suggest it would help if the band is ever widened).
3. Raise stakes with the bankroll; re-run this study weekly (`lab/replay_ticks.py` and the
   scratch study) as the recordings grow.

### 12b. Same evening: more markets, wider band, maker test, sizing (2026-09-10 23:00 UTC)

* **Seven assets, not three.** Polymarket lists TWAP-settled 5m/15m Up-or-Down markets for
  btc, eth, sol, xrp, doge, bnb and hype (and 4h windows for all seven); both Chainlink RTDS
  feeds carry all seven at ~1 update/s (verified). `scout` now parses and streams all seven
  (`SUPPORTED_ASSETS`, `CRYPTO_STREAM_ASSETS`); the arena records them; the live profile trades
  them. That is 2.3× the windows for the same rule. 4h windows need a slug/`is_short_updown_window`
  change and are not traded yet.
* **Real-book replay of the lock variants** (`lab/replay_ticks.py`, 5,977 windows, fills only when
  the displayed size covers $5):

  | variant | trades | win % | per $ | P&L at fixed $5 |
  |---|---|---|---|---|
  | lock_live_mirror (0.955–0.98, 8–45 s) | 93 | 100.0 | +0.024 | +$11.2 |
  | lock_wide (0.955–0.99, 8–55 s) | 331 | 99.7 | +0.012 | +$20.6 |
  | lock_maker1 (bid 1 tick under the ask, fee 0) | 44 filled / 3,101 unfilled | 97.7 | +0.010 | +$2.3 |
  | lock_maker2 (2 ticks under) | 24 / 3,223 | 95.8 | +0.003 | +$0.4 |
  | lock_twap_all (old wide lock, 0.55–0.93) | 644 | 86.3 | −0.015 | −$49.5 |

  The maker version is dead: it fills 1–2 % of the time and the fills that do happen are the
  adversely selected ones. The wide band nearly doubles total profit for one loss in 331, so the
  live profile moves to it (`TWAP_LOCK_ASK_CAP=0.99`, `TWAP_LOCK_MAX_SECONDS=55`,
  `CRYPTO_MIN_EDGE=0.004`); the 0.98/45 s cut stays in the arena as `lock_tight`.
* **Sizing.** `MAX_CRYPTO_STAKE` raised from 5 to 20 in the profile. The existing half-Kelly sizer
  (`crypto_clip`) then stakes ~27 % of cash on a 0.975 ask and ~16 % on a 0.99 ask — about $10 and
  $6 at $38 — and scales with the bankroll. At ~35 trades a day that is roughly $4–8 a day now,
  compounding while the record holds; a loss costs one stake. Set `MAX_CRYPTO_STAKE=5` again to go
  back to flat $5.
* Still pending on the live box: the user's re-run of `live_switch_to_lock.sh` (loads the 1 s tape,
  seven assets, wide band and sizing in one restart).

### 12c. First live cycles and the speed problem (2026-09-10 23:30–23:50 UTC)

The seven-coin profile's first verbose cycle saw 30 markets (28 with live books) and scored
2–6 lock signals per cycle. The first live order (YES at 0.99, $6.18 by the Kelly sizer) came
back from the venue with **"no orders found to match with FAK order"**: the offer was gone
before the order arrived. A direct probe of the books 35 s before a window end showed why —
on decided windows the favourite side usually has **no asks at all** (bids queue at 0.99,
nobody sells), and the 0.97–0.99 offers that do appear are lifted within seconds. The tick
recorder still saw in-band offers in 17 of 29 lock-seconds today, so the pocket is there, but
it is a race. Two fixes:

* `LOCK_HUNT_SECONDS=8` (`agent.hunt_locks`): after the normal scan, windows inside the lock
  band are watched every ~0.3 s with websocket books, and the existing candidate → veto →
  execute path fires the moment the favourite is offered inside the band. One attempt per
  market per second; the cycle is unchanged when no window is near its end.
* Websocket books with one empty side (ask streams as 0) are now treated as "offered at 0.999"
  on that side instead of forcing a REST refresh of every token; the REST fallback does the same.

Realistic consequence for the yield estimate: capture rate, not signal rate, is the binding
constraint. Judge the profile on live fills per day over the first week before sizing up.

### 12d. First live win, 4h windows, and the rest of Polymarket (2026-09-11 06:30 UTC)

* **First live lock fill:** 2026-09-11 03:59:32 UTC, BTC 15m, YES at 0.97, $6.18 (Kelly-sized),
  settled 1.00 → **+$0.18**. Six attempts in the first six hours of the seven-coin profile, one
  fill: the other five came back "no orders found to match" / "no resting asks" (12c). The
  sub-second hunt loop is built for exactly this and loads on the next restart.
* **4h windows added** (`SUPPORTED_WINDOWS_S`, slug tag `4h`, `CRYPTO_WINDOWS_MIN=5,15,240`;
  the Chainlink value buffer now keeps ~5.5 h so a 4h window's open is still available at its
  close). Same lock arithmetic in the last minute; 42 more windows a day across the seven coins,
  finer ticks (0.967-style asks seen) and, plausibly, fewer bots racing for the last offers.
  Arena and dry run record them from 06:18 UTC; the live profile trades them after restart.
* **The rest of Polymarket, scanned:** 1,500 most-active events, 757 multi-outcome (neg-risk)
  events; only 6 had outcome asks summing below $1 (0.94–0.995), all in thin or months-away
  markets (World Cup 2030 final location, Japan 10y yield end-2026, ...). Riskless, but a 1–5 %
  return on capital that would be locked past the deadline, on $38 of capital. Not a path to
  the goal; the tape already trades the crypto-window version of this when it appears.
* Honest arithmetic for "pay for the subscription by Sep 17": at $38 the lock yields cents per
  trade; even at 50 % capture of ~50 signals a day with compounding stakes that is on the order
  of $20–40 over six days. The strategy is right; the bankroll is what caps the dollars.
* **Latency-aware capture (scratch study, 6,679 windows):** 370 windows had a favourite offered
  inside the wide band while |z| ≥ 3.5 (win 368/370). The offer was still there 1 s later in
  74 % of them, 2 s later in 51 %, 3 s in 36 %, 5 s in 18 %. The old ~5 s cycle therefore
  captured ~1 in 6 (exactly the live record: 1 fill in 6 attempts); a 0.3 s watch with ~1 s order
  latency should capture roughly half to three quarters. That is the single biggest lever left.
