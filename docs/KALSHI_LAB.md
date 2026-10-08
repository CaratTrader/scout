# Kalshi strategy lab: learning loop and go-live gate

Written 2026-10-07, **before** any results of the lab were seen. The gate below may only be tightened, never loosened, after this date.

## Goal

Grow a $50 Kalshi bankroll with a strategy that earns **at least +10% per trade on average** after fees. Decision date: Sunday 2026-10-11, 20:00 ET.

## Loop (runs every night; paper only)

1. **Collect.** `lab/kalshi/fetch.py` adds the day's settled markets and 1-minute candles for ~35 high-frequency series:
   - crypto/commodity 15-minute
   - hourly above/below
   - rain, gas, commodities
   - sports games, tennis

   It runs incrementally and is synced to the paper bot's idle window (Kalshi allows ~1 request/s per IP).
2. **Map.** `lab/kalshi/calib.py` measures where Kalshi prices are wrong: realised win rate vs taker price by series family, price band and time before close. For sports it uses time before the *scheduled* end, never before the actual close, which would leak the future.
3. **Test.** Strategy families (observation rules such as the weather R0/R2m and rain-recorded; favourite/longshot bands; model-vs-price rules) are backtested walk-forward:
   - parameters are chosen on the first 70% of the history;
   - results count only on the last 30% (validation), which is never used for choosing.
4. **Paper.** Strategies that pass validation run on live data with a simulated $50 bankroll each. Fills are at the quoted ask, sized to the book, and include the 0.07·p·(1−p) taker fee.
5. **Record.** Every family and variant ever examined is counted (K). The significance bar rises with K, so searching more cannot manufacture a winner.
6. **Report.** `data/kalshi_lab/report.md`: leaderboard, gate status per strategy, paper P&L.

## Go-live gate (all must hold)

| # | Test | Bar |
|---|---|---|
| 1 | Validation trades | n ≥ 40 |
| 2 | Mean return per $ on validation, after fees, taker at the ask | ≥ +10% |
| 3 | Significance, clustered by event, equal stakes | t ≥ z(0.05/K) (K = cells examined; e.g. K=50 → t ≥ 3.1) |
| 4 | Not one lucky trade | mean > 0 after removing the 3 best trades |
| 5 | Stable | both halves of the validation period positive |
| 6 | Capacity | median book size at the signal price ≥ $5 |
| 7 | Forward paper since freeze | zero wrong-side fills from data or code errors; mean not below backtest mean − 2 SE |

If nothing passes by Sunday the answer is **no-go**: paper trading continues and the loop keeps searching.

## Paper harness and plug-ins

`scout/kalshi_lab_paper.py` (launchd `com.tradeinc.kalshilab`) runs every registry entry with status paper, gate-pass or monitor as a plug-in. Interface: `scout/kalshi_lab_strategies/base.py`.

- **Registry entry.** `"module"` names the plug-in: a bare name is looked up in `scout.kalshi_lab_strategies`, then `lab.kalshi.strategies`; a dotted name is imported as is; `"module:Class"` picks one class. Entries without `"module"` fall back to their family (rain → `rain_n`, weather → `weather_mirror`). Optional `"poll_s"` sets the poll interval.
- **Plug-in.** Declares the Kalshi series it needs and its poll interval, optionally asks for outside data (cached fetchers: METAR, Coinbase candles, ESPN, Polymarket), and returns signals `{ticker, side, max_price, why}` from `decide(now, market_quotes, external_data)`. It never calls Kalshi itself.
- **Kalshi calls.** One open-markets call per series per poll, shared by every strategy due in that poll. An order book only when a signal fires. All calls fall in the temperature bot's idle window, at least 1.2 s apart, and at most 25 in any minute (`KLAB_MAX_CALLS_PER_MIN`). Poll intervals stretch when the planned open-market calls would use more than 60% of that cap.
- **Bankroll.** Sizing, halts, the unit-stake signal log and the ledgers are the harness's, the same for every strategy. A plug-in acts on one signal per market and side unless it keeps its own bookkeeping.

## Live profile, if a strategy passes

The live switch is the account holder's action, with their own Kalshi API key. The code never holds credentials.

- Bankroll $50. Stake = min(quarter-Kelly on the validation win rate shrunk 50% toward the price, $5).
- At most 3 open positions.
- Halt after 3 losses in a row (owner's rule). Halt after a $10 daily loss or a $15 cumulative loss; review before restarting.

## Pre-registered: weather out-of-sample test (written 2026-10-07 ~11:00 ET, before the data finished downloading)

**Data.** Kalshi archive (`/historical/...`) of KXHIGH{NY,CHI,MIA,LAX,AUS,DEN,PHIL}, climate days 2026-01-01..04-22, with IEM ASOS rows (hourly, specials and 5-minute) for those days. No weather rule has seen these days: Polymarket design data starts 2026-04-23, Kalshi design data 2026-07-19.

**Rules.** Exactly as the paper bot runs them on 2026-10-07, with no re-tuning:
- R0: dead bucket by a full degree on the METAR max.
- R2m: floor ≥ round(max(METAR max, unfiltered max, 5-minute max)) + 3F after the peak (15:00 local, 1F fall, 45 min), YES bid ≥ 0.15.
- One trade per market, 2-minute fill delay, taker at the quoted price, 0.07·p·(1−p) fee.
- Run: `KB_FROM=2026-01-01 KB_TO=2026-04-22 python -m lab.us.kalshi_backtest 2 0.80`, row "LIVE R0+R2m".

**Verdict.** Gate rows 1-5 above, with K as recorded in data/kalshi_lab/K.json on the day of the run. Afternoon climate-report data may be missing for these months; the rules then run on METAR alone. That is a known difference from the live bot, where the report can only make R2 more cautious.

**Amendment (2026-10-07 ~10:40 ET, still before any out-of-sample result was seen).**
- **Window.** Extended to climate days **2025-07-01..2026-04-22** (Kalshi archive, same 7 series, same rules) so the unseen sample can reach the n ≥ 40 bar. The verdict is the whole window. The 2025-H2 and 2026 segments are reported separately, and each must have positive return (gate row 5 applied per segment).
- **No more windows.** None will be added after the first result is seen.

## Result: weather out-of-sample test (run 2026-10-07 evening, read 2026-10-08)

**FAILED.** Kalshi archive 2025-07-01..2026-04-22: 2,072 station-days, LIVE R0+R2m.
- n=48, 67% win, **−17.6% per $**, t (by station-day) −1.80. K=296, so the bar was t ≥ 3.58.
- Without the 3 best trades: −23.3%. Halves: −17.9% and −17.4%.
- Segments: 2025-H2 n=22, −14.1%; 2026-01..04 n=26, −20.6%.

On all 15 months (2025-07..2026-10) the same rules make +2.5% per $ (t 0.2), or −1.5% without the single best trade. The +19% of Jul-Oct 2026 was in-sample luck plus overfitting.

The weather strategy is a **monitor only** from now on. Strategy search continues in round 1 of the parallel hunt (lab/kalshi/strategies/).

## Round 1 / 1b strategy hunt (2026-10-08): 20 families, ~10,900 variants, all cut

Families tested:
- crypto 15-minute and hourly, versus Coinbase
- index hourly, versus Yahoo
- commodities, FX and rates
- hourly temperature nowcast
- sports versus DraftKings, plus sports archive calibration
- tennis Elo and in-play
- cross-venue Polymarket
- macro releases (CPI nowcast, jobless claims)
- weather daily-high walk-forward, daily lows
- rain long history, rain maker
- ladder and cross-series arbitrage
- microstructure, passive maker
- learned recalibration
- earthquakes and entertainment

Findings:
- On professionally quoted series the Kalshi mid beats every free model.
- Venue moves coincide within about 6 s, and REST quotes are about 20 s stale.
- Takers filling 1-2 minutes late lose to spread plus fee.
- Makers are adversely selected. The one exception, KXRAIN NO in Jul-Sep 2026, has vanished as spreads tightened.
- Longshots are overpriced everywhere, but the favourite side pays only +1-5%.

Lab K is about 12,100 (bar t ≥ 4.46 for searched cells). Code: lab/kalshi/strategies/. Results: data/kalshi_lab/strategies/<family>/result.json.

Round 2 targets slow, retail-dominated markets: monthly accumulators, thin weather cities, mention markets, launch windows, spreads and totals, charts, and weather/commodity maker.

## Round 2 strategy hunt (2026-10-08/09): 9 families in slow retail markets, 3,325 variants, all cut

| Family | Result |
|---|---|
| Monthly rain accumulators | Model loses to the mid; locked strikes are already ≥ 0.97 |
| Thin-city weather maker | −9% |
| Launch window | Pre-registered out-of-sample: −28% |
| Sports spreads and totals | Track DraftKings |
| Chart favourites | +1.8% |
| Weather daily maker | +2.4% |
| Daily-commodity maker | Out-of-sample: −39% |

Infra audit: candles carry no look-ahead; REST /markets is about 20 s stale.

**Only lead:** maker NO at listing on mention markets. Validation +18.6%/$ (t 2.76), reproduced independently. The auditor showed part of it came from entry times anchored on the future close. The robust core is about +4% per fill, and +10% would need queue priority. It goes to a forward test in round 3.

Lab K is about 15,450 (bar t ≥ 4.51).
