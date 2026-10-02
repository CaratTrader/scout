# Polymarket US research (started 2026-09-22)

Goal: find something with a real, repeatable edge that a US-resident account can trade legally on Polymarket US
(polymarket.us, CFTC-regulated, dollar-settled), using the same honesty bar as the lab: real books, catchable
fills, t >= 3 and n >= 100 with both halves positive before anything goes live.

## 1. Venue facts (verified against the live API and docs on 2026-09-22)
* Public data: `https://gateway.polymarket.us/v1` (markets, events, series, book, bbo, settlement, price-history,
  20 req/s per IP). Trading: `https://api.polymarket.us` with Ed25519 keys created at polymarket.us/developer
  after KYC in the iOS app; official `polymarket-us` Python SDK; order types LIMIT / MARKET, TIF GTC / GTD / IOC /
  FOK / DAY, post-only flag; tick 0.001, min 1 contract on sports.
* Fees (effective 2026-09-17): taker 0.0695 * C * p * (1-p); **maker rebate -0.0125 * C * p * (1-p)** (makers are paid).
* Price history: `longPrice` = YES best ask, `shortPrice` = NO best ask (= 1 - YES bid); 1-minute points via
  `fixedInterval=INTERVAL_LIVE` (15 min before start to end) or custom ranges <= 24 h. This gives minute-level
  bid/ask for every market, which is what makes an honest backtest possible.
* Universe since launch (crawl of 363,371 markets by weekly start-date windows): sports 354k (player props 206k,
  spreads 42k, totals 41k, moneylines 40k, futures 18k), politics 5,015, **climate 2,543**, culture 1,110,
  macro 187, finance 92, crypto 13 (long-dated ladders only; no 5-minute crypto).
* Same games as the global venue: US slug = prefix + global slug (`aec-mlb-tb-tex-2026-09-04` <-> `mlb-tb-tex-2026-09-04`),
  so the deep global book (public data, readable from the US) can serve as a reference price.
* Market Integrity policy prohibits spoofing, wash trades, fictitious trades, self-dealing, front-running, MNPI,
  manipulation and disruptive practices; it says nothing about bots, latency or cross-venue arbitrage. Rulebook
  chapter 7 still to be read before any live use.

## 2. Studies
### 2a. Cross-venue lead-lag on sports moneylines (global -> US)
Method: `lab/us/xvenue_batch.py`. For each resolved US moneyline, align the US 1-minute bid/ask with the global
1-minute price for the same game (winner identity fixes the outcome mapping). Taker rule: buy on US when its price
is >= theta below the global price for two consecutive minutes, fill at the later ask (conservative), exit at
convergence or hold to settlement. Maker rule: rest at global fair +/- 1c, filled only when the US display price
crosses the quote. Results: see section 3 (filled in when the 2,036-game run finishes).

### 2b. Daily-high temperature ladders (climate category)
Markets: `tc-temp-<sfo|lax|mdw|nyc|mia>high-<date>-<bucket>`, buckets `lt68f` (<=67), `gte68lt69f` (68-69),
..., `gte76f` (>=76); 553 city-days from 2026-04-22, five stations (KSFO, KLAX, KMDW, KNYC, KMIA), resolved on
the NWS daily climatological report. Median ~6k shares traded per market. Same instrument family as the
`scout/weather_lock.py` paper bot (observed-max lock), but the US book is far less efficient: on 2026-09-18 SFO the
winning 68-69 bucket was still offered at 0.55 two hours after the day's high was in, and a bucket above the high
was bid at 0.97.
Method: `lab/us/temp_backtest.py` on METAR history (Iowa State ASOS archive, `lab/us/iem_fetch.py`) and the
ladders' minute prices (`lab/us/temp_fetch.py`). Rules R0 (certain), R1 (winner lock after the peak), R2 (fade
buckets above the observed high). Results: section 3.

## 3. Results
### 3a. Temperature ladders — results (409 city-days with prices and METAR, 2026-04-23 → 09-18; fills 2 min after the decision unless noted)

| rule | 2-min delay | 15-min delay |
|---|---|---|
| R0 certain (dead bucket NO / top bucket YES) | n=130, win 99%, avg price 0.81, +17.4c/share, +21% per $ staked, t=9.4 | n=117, win 99%, avg price 0.80, +18.5c/share, +23% per $ staked, t=9.2 |
| R1x after the 00Z maximum (YES on its bucket, NO on the rest) | n=115, win 91%, avg price 0.58, +32.4c/share, +56% per $ staked, t=13.1 | n=114, win 91%, avg price 0.58, +32.3c/share, +56% per $ staked, t=13.3 |
| R2 fade buckets ≥ max+3 after the peak | n=39, win 92%, avg price 0.61, +30.0c/share, +49% per $ staked, t=5.9 | n=36, win 89%, avg price 0.66, +21.7c/share, +33% per $ staked, t=3.7 |

The one R0 loss is KNYC 2026-08-27, a day the Central Park sensor reported spurious highs (flagged `$`, heavy rain) that the official report ignored; the hourly spike and the 6-hour group are now both filtered (corroboration within 2.5F; group ≤ hourly max + 3F).

**Decay check (the lab bar is both halves positive):**
* R0 first half (2026-04-23 → 2026-05-11): n=65, win 100%, avg price 0.74, +24.7c/share, +33% per $ staked, t=8.5
* R0 second half (2026-05-11 → 2026-09-18): n=65, win 98%, avg price 0.88, +10.2c/share, +12% per $ staked, t=5.3
* R1x first half (2026-04-23 → 2026-05-02): n=57, win 96%, avg price 0.54, +40.7c/share, +75% per $ staked, t=14.7
* R1x second half (2026-05-03 → 2026-09-18): n=58, win 86%, avg price 0.61, +24.1c/share, +40% per $ staked, t=6.4
* R2 first half (2026-04-23 → 2026-06-11): n=19, win 89%, avg price 0.62, +26.3c/share, +42% per $ staked, t=4.1
* R2 second half (2026-06-11 → 2026-09-18): n=20, win 95%, avg price 0.60, +33.6c/share, +56% per $ staked, t=4.3

**Combined engine (one trade per bucket per day, earliest rule wins): opportunities per calendar day across the five cities and the $/day at $200 of contracts per trade**

| month | city-days | trades/day | win | avg price | edge per $ | $/day at $200/trade |
|---|---|---|---|---|---|---|
| 2026-04 | 15 | 14.67 | 100% | 0.54 | +81% | $2374 |
| 2026-05 | 155 | 2.65 | 91% | 0.76 | +19% | $99 |
| 2026-06 | 90 | 2.89 | 94% | 0.79 | +19% | $108 |
| 2026-07 | 85 | 1.47 | 100% | 0.68 | +46% | $137 |
| 2026-08 | 54 | 1.48 | 88% | 0.80 | +9% | $26 |
| 2026-09 | 40 | 1.00 | 100% | 0.68 | +45% | $90 |

June–September combined: 101 trades over 54 calendar days = 1.88 trades/day, win 95%, edge +25% per $ → about **$94/day at $200 per trade, $236/day at $500** (books showed 500–9,000 shares per level on 2026-09-22, so these sizes are realistic). April was the market's launch period and is not repeatable.

Caveats: opportunities fell from ~10/day in April to ~1–2/day since June as the market matured; August had only 4 R1x trades (2 lost); fills assumed at the displayed price up to the displayed size; taker fee included, no maker rebate; residual risks are a late-evening warm-up after the 00Z report (~7% of R1x trades) and station data faults.

### 3b. Cross-venue sports lead-lag — results (1,236 matched games: cbb 215, mlb 193, nfl 182, wnba 151, nhl 150, nba 142, wta 86, atp 60, cfb 56)

Taker rule, in-game only, fills at the US ask one minute after a two-minute-persistent gap, fee included, hold to settlement / exit at convergence:

| games (global volume) | theta 3c: n, hold, exit | theta 5c: n, hold, exit |
|---|---|---|
| liquid global market (≥ $100k) | 1,283 trades, +0.8c/share (t 0.7), −0.4c (t −0.9) | 579 trades, +5.7c (t 3.2), +3.1c (t 3.8) |
| thin global market (< $100k) | 318 trades, +12.3c (t 6.7), +10.0c (t 9.4) | 177 trades, +21.7c (t 9.3), +17.3c (t 10.5) |
| NFL (liquid) | 195, −0.5c, +0.9c | 92, +6.0c (t 1.4), +4.7c (t 2.9) |
| MLB (liquid) | 246, −3.7c, −3.1c | 103, +0.3c, −2.0c |
| WNBA (liquid) | 444, −1.8c, −4.3c | 181, −0.2c, −3.3c |
| NBA (liquid) | 68, +4.3c (t 0.9), +5.3c (t 1.8) | 51, +5.7c (t 1.0), +6.0c (t 1.6) |
| college basketball (thin) | 241, +15.1c (t 7.6), +12.1c (t 9.7) | 149, +23.9c (t 10.2), +18.5c (t 10.6) |
| NHL (liquid) | 93, +10.3c (t 3.3), +13.0c (t 6.8) | 49, +16.2c (t 3.0), +21.2c (t 6.7) |

Pre-game signals were discarded: before the first trade the US history shows placeholder 0.50/0.50 prices, which produced fictitious +30–50c "edges".

**Verdict: not tradable as tested.** On the liquid leagues, where a fill is plausible, the edge is zero to a few cents and not robust across leagues (MLB and WNBA negative). The large "edges" sit in thin college basketball and NHL markets whose displayed US prices are frequently crossed or have zero spread in-game, which a real two-sided book cannot show:

| league | global liquidity | games | in-game minutes | crossed | zero spread | ≤2c | ≤10c | >10c |
|---|---|---|---|---|---|---|---|---|
| nfl | liquid | 178 | 32707 | 1% | 7% | 48% | 39% | 5% |
| mlb | liquid | 183 | 31913 | 0% | 9% | 85% | 5% | 0% |
| cbb | thin | 186 | 27795 | 4% | 12% | 33% | 47% | 4% |
| nhl | liquid | 131 | 25861 | 2% | 21% | 32% | 44% | 2% |
| nba | liquid | 117 | 18934 | 1% | 7% | 52% | 38% | 2% |
| wnba | liquid | 139 | 18770 | 0% | 14% | 76% | 10% | 0% |
| cfb | liquid | 77 | 16138 | 2% | 14% | 36% | 45% | 3% |
| cfb | thin | 59 | 8637 | 0% | 47% | 43% | 2% | 8% |
| atp | liquid | 30 | 7457 | 0% | 33% | 32% | 18% | 17% |
| wta | liquid | 51 | 7456 | 1% | 8% | 66% | 24% | 1% |
| cbb | liquid | 29 | 4335 | 2% | 13% | 43% | 39% | 3% |
| nhl | thin | 23 | 4306 | 0% | 12% | 35% | 53% | 0% |
| wta | thin | 35 | 4036 | 3% | 10% | 52% | 24% | 11% |
| nba | thin | 27 | 4019 | 1% | 11% | 72% | 17% | 0% |
| atp | thin | 30 | 3972 | 1% | 11% | 58% | 23% | 7% |
| mlb | thin | 10 | 1640 | 1% | 22% | 51% | 20% | 6% |
| wnba | thin | 12 | 1522 | 0% | 23% | 71% | 6% | 0% |
| nfl | thin | 4 | 621 | 0% | 24% | 73% | 4% | 0% |

Those minutes are display artefacts (last trade or one-sided books), not offers, so the thin-market numbers are not evidence. The maker variant crashed in this run; the September sample had it at −2 to −4c per share (adverse selection). A real test needs a live recorder of both venues' books at sub-second resolution, which is a separate project. Nothing here justifies capital today.

## 4. What is running now
* `com.tradeinc.ustemp` (paper): `scout/us_temp_paper.py` polls the five METARs and the five open ladders every 60 s, applies R0 / R1x / R2 with the data-quality rules above, requires a quote to survive two polls before a paper fill (size-capped), settles from the venue. Ledger `data/ledger_us_temp.json`, log `data/us_temp.log`.
* The bar before real money: two weeks of paper fills with the catchable rule, win rate ≥ 90% and realised edge ≥ +20% per $ on ≥ 30 fills, then a KYC'd Polymarket US account, API key, and a small live stake ($50–100 per trade).
* (2026-10-01) For the Kalshi paper job the go-live bar at the end of section 6a replaces this one.

## 5. Data sources for the temperature trader (2026-09-23)
| source | what | latency / resolution | use |
|---|---|---|---|
| aviationweather.gov METAR API | hourly + special reports, T-group tenths, 6-hour max groups at 00/06/12/18Z | ~5 min; hourly | primary observation; 00Z group = true afternoon max (98% exact) |
| Iowa State ASOS archive (`asos.py`) | same METARs, raw text | minutes | fallback when AWC returns 502/504 |
| **NWS Daily Climate Report (CLI), intraday issuance** via api.weather.gov `products?type=CLI&location=<MIA|NYC|MDW|SFO|LAX>` | "VALID TODAY AS OF 0400 PM … TODAY MAXIMUM 93 2:57 PM": the resolution product's own max-so-far, from 1-minute data | Miami ~16:25 EDT, NYC ~16:35 EDT, Chicago ~16:35 CDT; SF ~17:25 PDT and LA ~18:35 PDT (after their 00Z) | trusted observation (raises the max like a 6-hour group); candidate early R1x trigger 2–3.5 h before 00Z in the three eastern/central cities (`USTEMP_CLI_TRIGGER`, backtest `lab/us/cli_backtest.py` on the IEM AFOS archive) — rejected 2026-10-01, the backtest had a look-ahead (5a); trigger off |
| IEM `obhistory` 5-minute MADIS reports | whole-°C 5-minute observations | near real time | not used yet; would tighten the running max between hourly reports (1.8°F resolution) |
| Forecasts (NWS hourly, HRRR) | prediction | n/a | deliberately not used: the edge is observational; a forecast model would be a separate, riskier strategy |

### 5a. Intraday climate report as an early trigger — result (2026-09-23; corrected 2026-10-01)

> **Correction (2026-10-01): the first version of this section had a look-ahead. Its result is withdrawn.**
> It acted 2 min after the report's *as-of* time ("VALID TODAY AS OF 0400 PM"), as if the report were public then.
> Reports are issued a median 33 min after their as-of time across the nine offices that issue one (p10 23 min,
> p90 43, never sooner than 18); 25–37 min in Miami, NYC and Chicago. Issuance is the IEM AFOS entered time in the
> archive file name (`data/lab/us/cli_intraday/<PIL>_<date>_<HHMM>Z.txt`, UTC); the WMO header in the text agrees on
> 4,383 of 4,389 files and is never earlier. Withdrawn: 44 trades, 91% win, avg price 0.56, +33.7c per share, +60%
> per $, t 5.5; the claim that this was the 00Z rule's edge 2–3.5 hours earlier; and the decision based on it
> (`USTEMP_CLI_TRIGGER=1` from 2026-09-23). The trigger is off again. The original code on the same data reproduces the
> withdrawn numbers exactly, so the error was in the method (the timing, plus 6 fills on placeholder quotes), not in
> the data.

Re-run with the report usable at issuance + 7 min and the fill 2 min after that
(`python -m lab.us.cli_backtest 2 7`; peak gate evaluated at that minute, the max lifted to the report's and dated at
its as-of time when the report beats METAR, as the bot does; 0.50/0.50 quotes skipped as the empty-book placeholder):

| city | report issued (local, median) | report max == final high | same, when METAR says the peak has passed at issuance + 7 min (fall ≥ 2F, ≥ 60 min since max) |
|---|---|---|---|
| Miami | 16:25 (p10–p90 16:21–16:36) | 147/154 = 95% | 66/68 = 97% |
| NYC | 16:37 (16:32–16:48) | 128/155 = 83% | 52/54 = 96% |
| Chicago | 16:35 (16:33–16:43) | 124/155 = 80% | 24/27 = 89% |

| R1x triggered by the report + peak gate, Polymarket US | n | win | avg price | per share | per $ | t (share) | t ($ stakes) |
|---|---|---|---|---|---|---|---|
| first version (as-of + 2 min, look-ahead) | 44 | 91% | 0.56 | +33.7c | +60% | 5.5 | 3.2 |
| honest, Miami + NYC + Chicago | 9 | 100% | 0.63 | +35.9c | +57% | 3.5 | 1.5 |
| honest, with LA (SF none) | 13 | 100% | 0.66 | +32.9c | +50% | 4.4 | 1.5 |

The honest fills fall on 4 days, the last on 2026-06-11 (LA 2026-07-18); one NO bought at 0.04 (NYC 2026-04-29) is 72% of
the dollar profit. Of the original 44, 6 were fills against the 0.50/0.50 placeholder of launch day 2026-04-23 and all
the others were placed at as-of + 2 min, 20–35 min before the report was issued; once it is out the book has usually
repriced.
Sensitivity: usable at issuance + 0 → 10 trades, + 15 → 8; the old METAR-only peak gate → 14 trades, 79% win, +25c.
Verdict: no usable early trigger (about one fill a month, none since mid-July); keep `USTEMP_CLI_TRIGGER=0`. SF and LA
reports come after their 00Z.

## 6. Kalshi (2026-09-27): the same edge on a venue with six times the markets

> **Correction (2026-10-01): the Kalshi results first published here on 2026-09-27 had a look-ahead.**
> `lab/us/kalshi_backtest.py` and `lab/us/cli_backtest.py` treated the NWS afternoon climate report as known 5 min
> after its as-of time (`t >= rep["asof_min"] + 5`). Reports are issued a median 33 min after the as-of time (p10 23,
> p90 43, never sooner than 18), and by then Kalshi has usually repriced the winning bucket. With the report usable at
> issuance + 7 min, the report-triggered rule (R1c) loses money. These headlines are **withdrawn**:
> * Kalshi R0 + R1c + R2: 134 trades, +21% per $, 1.8 trades/day, about $74/day at $200 per trade;
> * Kalshi "core" NYC + DC + Chicago: 30 trades, 93% win, +76% per $ (about $61/day at $200);
> * Polymarket US report trigger (section 5a): 44 trades, 91% win, +34c per share, t 5.5.
>
> What survives is R0 + R2: about 0.9 trades a day across 17 cities and +12.5c per contract, but t is only 1.4 on
> equal-$ stakes, one lucky trade carries two thirds of the profit, and without it the backtest makes about $2/day at
> $25 per trade. Section 6a has the corrected numbers and the new go-live bar. The heading's "same edge" is the
> 2026-09-27 claim; it is not established.

* Daily high-temperature ladders for ~30 US cities (NYC, Chicago, Miami, LA, SF, Boston, DC, Philadelphia, Denver,
  Austin, Houston, Dallas, Phoenix, Seattle, Atlanta, Las Vegas, San Diego, Minneapolis, OKC, Newark, Trenton, ...),
  plus daily lows and international cities. Series tickers like `KXHIGHNY`; one ladder per day: `T62` (less: max ≤ 61),
  `B62.5 … B68.5` (between, 2°F inclusive buckets), `T69` (greater: max ≥ 70). Markets open 14:00Z the day before,
  close 05:00Z after the day, settle a few hours later.
* Resolution: "maximum temperature recorded at <city> (CLIxxx) … according to The Weather Company" — matched the NWS
  daily climate high on 304 of 305 backtested days, so the same METAR / 6-hour-group / intraday-report engine applies.
* Public API (`api.elections.kalshi.com/trade-api/v2`): series list, markets with top-of-book, order books with size,
  1-minute candlesticks with yes bid/ask OHLC (dense: ~750 candles a day on the active bucket, 1–2c spreads).
  Rate limit about 1 request/s (429 above). Trading needs an account and RSA-signed requests; paper needs nothing.
* Fees: taker 0.07 · C · p · (1−p) (used in the backtest); maker 0 on most series.
* First live look (Sep 27, midday): the crowd's buckets sit at 0.5–0.7 while the bucket holding the current
  observed max is often unbid, i.e. the market trades the forecast. On Sep 26 the NYC winner (≤61) traded at
  0.76–0.79 at 16:00 while the Polymarket US equivalent was 0.93.
* Paper trader `scout/kalshi_temp_paper.py` (launchd `com.tradeinc.kalshitemp`, six cities to start); dashboard
  `/ustemp?venue=kalshi`. Polymarket paper jobs retired the same day.

### 6a. Kalshi backtest — honest results (first published 2026-09-27; rewritten 2026-10-01 after the look-ahead fix)
**Setup.** `python -m lab.us.kalshi_backtest 2 0.80`: 17 cities, 1,075 station-days on 68 calendar days
(2026-07-19 → 09-26; the earlier "1,056 station-days, Jul 14–Sep 26" came from older data), Kalshi 1-minute bid/ask
candles, fill at the quote 2 min after the decision, 7% taker fee, YES cap 0.80, one position per market (the first
rule in time wins). Decisions every 15 min from 12:00 to 23:15 local, plus each report's usable minute. The afternoon
report counts only from issuance + 7 min (file-name UTC time converted to the station's clock, next-UTC-date
issuances included), and only "VALID TODAY AS OF" reports with as-of ≥ 12:00 from NYC, Miami, Chicago, DC,
Philadelphia, Boston, Atlanta, Dallas and Minneapolis. When the report is above the METAR max it lifts the max,
dated at its as-of time, as the bot does. An independent replay that shares only the METAR loader reproduced every
trade (same tickers, sides, prices and times) and the sensitivity runs below.

Definitions: "per contract" is the average P&L of one contract. "Equal $" means every trade stakes the same dollars
(25 / price contracts), so a trade's return is P&L / price; this is how the bot sizes. Trades/day are across all 17
cities (n / 68). t uses the sample standard deviation, as in the independent check; the script prints the population
version, which is 0.01–0.06 higher here (R0, n = 4: 7.5 and 7.1).

**Live configuration** (paper job `com.tradeinc.kalshitemp`, revised 2026-09-27):
* R0, dead buckets on the METAR-only max: buy NO when the bucket's top + 1F ≤ the observed max (NO ask 0.02–0.97),
  and YES on the open top bucket once the max is inside it.
* R2, fade buckets far above the max after the peak: bucket floor ≥ round(max) + 3, at or after 15:00 local, the
  temperature ≥ 1F below the max, ≥ 45 min since the max, YES bid ≥ 0.15.
* Report trigger off (`USTEMP_CLI_TRIGGER=0`). The report is still used as a trusted observation that lifts the max
  for R2 once it is available (`USTEMP_CLI_OBS=1`; offices nyc, mia, mdw, dca, phl, bos, atl, dfw, msp; as-of ≥ 12:00).
* $25 per trade, YES cap 0.80, one position per market.

| rule | n | win | avg price | P&L / contract | t (contract) | per $, equal contracts | per $, equal $ stakes | t (equal $) | trades/day | $/day at $25 | total at $25 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R0 dead buckets (METAR only) | 4 | 100% | 0.93 | +6.5c | 6.46 | +7.0% | +7.1% | 6.12 | 0.06 | +$0.10 | +$7 |
| R2 fade ≥ max+3 after the peak | 59 | 80% | 0.66 | +12.9c | 3.02 | +19.6% | +29.0% | 1.37 | 0.87 | +$6.30 | +$428 |
| **live: R0 + R2** | **63** | **81%** | 0.67 | **+12.5c** | 3.13 | +18.5% | **+27.6%** | **1.39** | **0.93** | **+$6.40** | +$435 |
| live without its largest winner | 62 | 81% | 0.68 | +11.2c | 2.91 | +16.4% | +9.7% | 1.14 | 0.91 | +$2.20 | +$150 |
| R1c report + peak gate, honest timing (off) | 29 | 41% | 0.58 | −17.7c | −2.19 | −30.5% | −46.3% | −3.50 | 0.43 | −$4.94 | −$336 |
| R1x after the 00Z max only (off) | 39 | 74% | 0.71 | +2.0c | 0.35 | +2.8% | −0.2% | −0.01 | 0.57 | −$0.03 | −$2 |
| R0 + R1c + R2 (report trigger on) | 91 | 68% | 0.64 | +2.8c | 0.69 | +4.3% | +4.1% | 0.28 | 1.34 | +$1.37 | +$93 |

By the bar at the top of this document (t ≥ 3, n ≥ 100, both halves positive) the live configuration does not
qualify: n = 63 and t 1.39 on equal-$ stakes. Every one of the 63 live trades bought NO.

**Concentration in one trade.**
* The largest winner, KXHIGHTSEA-26SEP02-B68.5 (Seattle, 68–69F), an R2 NO bought at 0.08, made +$285.89 at $25:
  66% of the $435. Without it: +11.2c per contract, +9.7% per equal $, t 1.14, $2.20/day.
* Why it won (this corrects the first write-up, which said the temperature rose after 15:00; the independent check
  found otherwise and the raw METARs confirm it): KSEA reported 69F at 11:53, and the 5-minute data show 19–21C for
  the whole hour, so it was a real reading. Rain then held it at 59–60F after 15:00. The spike filter (`robust_max`)
  rejected the 69F because its hourly neighbours were 65 and 62, and the 16:53 METAR's 6-hour max group (10211 = 70F)
  was dropped as an artefact. So R2 saw a max of 65 and faded the 68–69 bucket, while the market correctly treated 69
  as already observed (YES 0.92). The fade won only because the official high was 70, not 69. The peak had passed;
  the max the rule used was wrong, and the profit was luck.
* The spike filter cuts both ways. With no filter, 4 of the 59 R2 trades would not have been taken: Seattle 09-02
  (+$285.89), Denver 08-22 B94.5 (+$12.28), Denver 08-22 B92.5 (−$26.09) and Austin 09-06 B96.5 (−$26.66); apart from
  Seattle they net −$40.47. Two of the six fades bought below NO 0.20 (Seattle and Austin) were these filter
  disagreements. For R0 the filter is needed: without it R0 takes one more trade, the KNYC 08-27 sensor spike, which
  loses its $25 stake at NO 0.04.

**Stability.**
* Halves: first 31 trades (07-20 → 08-20) +11.4c per contract, +17% per equal $, t 1.39 (equal $); last 32
  (08-21 → 09-26) +13.6c, +38% per equal $ (Seattle included), t 1.01. Both positive, neither significant.
* By month:

| month | n | win | P&L / contract | per $, equal contracts | per $, equal $ stakes | t (equal $) | total at $25 |
|---|---|---|---|---|---|---|---|
| July | 11 | 82% | +10.7c | +15% | +9% | 0.48 | +$24 |
| August | 29 | 83% | +10.2c | +14% | +15% | 1.15 | +$109 |
| September | 23 | 78% | +16.2c | +27% | +53% | 1.03 | +$303 (Seattle $286) |

* By city no one has more than 8 trades (n, per $ on equal contracts, total at $25): Atlanta 8 (+15%, +$32), Denver 8
  (+17%, +$21), Phoenix 7 (+49%, +$62), Dallas 6 (+16%, +$13), Seattle 6 (+45%, +$317), Boston 5 (−14%, −$39), Austin 4
  (+44%, +$27), Philadelphia 3 (+$14), Chicago 3 (+$18), Las Vegas 3 (−33%, −$39), DC 2 (+$14), Minneapolis 2 (+$10),
  LA 2 (−$22), SF 2 (−$4), NYC 1 (+$4), Miami 1 (+$6).
* R2 by NO entry price (the first write-up left out the 0.40–0.60 row and the 0.20–0.40 P&L):

| NO price | n | win | P&L / contract | total at $25 |
|---|---|---|---|---|
| below 0.20 | 6 | 17% | +8.2c | +$153 (Seattle +$286; the other five −$133) |
| 0.20–0.40 | 5 | 60% | +23.2c | +$81 |
| 0.40–0.60 | 2 | 50% | −1.2c | −$5 |
| 0.60–0.80 | 25 | 88% | +14.6c | +$129 |
| 0.80–1.00 | 21 | 95% | +11.1c | +$70 |

  R0 + R2 with R2 limited to NO ≥ 0.60: 50 trades, 92% win, +12.5c, t 3.23 per contract and 3.13 on equal $,
  $3.03/day. This cut was chosen after looking at the data, so it is something to check in paper, not a result.

**Realistic money and capacity.**
* Backtest at $25 per trade: $6.40/day, of which $4.20 is the Seattle trade; $2.20/day without it. Largest
  drawdown at $25: $62.
* The backtest fills the full $25 at the displayed price. The paper job's 12 signal snapshots so far (4 markets,
  2026-09-28 → 09-30) showed a median of about $14 on the book at or better than the signal price (range $0.46–$50).
  A $25 order would have been cut by displayed size on 7 of the 12, a $200 order on all 12; Denver 09-30 filled 12 of
  32 contracts.
* Plan on about $2/day at $25 per trade, less when books are thin. Capacity above about $25 per trade is unproven;
  the bot should log book depth on every signal before anyone raises the stake.

**Why the report trigger (R1c) collapsed.** The look-ahead run on the same data had R1c at 82 trades, 78% win,
+14.0c. The 53 trades that disappear with honest timing won 98% (+31.1c). Measured against the strict report the bot
uses, 52 of them were filled before the report was issued, a median 22 min before (the rebaseline gave 50 and 17 min,
measured against the lenient earliest report). Two minutes after the report is usable, the median ask of the winning
bucket is 1.00 and the other buckets' bids are 0. The 29 trades left are the same tickers with the same outcomes as in
the old run (23 of them at the same time, price and side): the cases where the book still disagreed with the report,
and they lose. YES side 11 trades, 27% win, −21.1c; NO side 18 trades, 50% win, −15.6c. By month: July 7 trades +1%
per $, August 18 trades 28% win −52%, September 4 trades +2%. By station: Atlanta 15 (−32%), Boston 5 (−23%),
Philadelphia 4 (no wins), NYC 3 (−33%), DC 2 (+67%); none in Miami, Chicago, Dallas or Minneapolis. Every variant is
negative: allowing R1c after 00Z, as the bot would with the trigger on, 33 trades, 48.5% win, −13.6c, t −1.9; the
lenient parse (adds the Minneapolis and Dallas 4 PM reports) 34 trades, 50% win, −13.2c; the old METAR-only peak gate
with honest timing 35 trades, 49% win, −12.8c.

Look-ahead run, for reference only (withdrawn; same data, delay 2, cap 0.80, t from the script):

| rule | n | win | P&L / contract | per $, equal contracts | t (contract) | t (equal $) | $/day at $25 |
|---|---|---|---|---|---|---|---|
| R1c | 82 | 78% | +14.0c | +22.2% | 2.99 | 2.85 | +$28.12 |
| R0 + R2 | 61 | 80% | +12.2c | +18.1% | 2.98 | 1.36 | +$6.18 |
| R0 + R1c + R2 | 141 | 79% | +13.1c | +20.4% | 4.05 | 3.11 | +$34.13 |

The 134-trade table first published in this section came from older data and code; the 141 trades above are the
same look-ahead on the current data.

**The report hardly matters for R2 under honest timing.** It lifted the max at entry on 1 of the 59 R2 trades: 52 of
the 59 fire in the 15:00 hour, and the report is usable at a median 16:32–16:44 in Miami, NYC, Chicago and Atlanta,
17:32–17:41 in DC, Boston and Philadelphia, 19:35–19:48 in Dallas and Minneapolis. R0 + R2 with no report at all: 64
trades, $443; usable at issuance + 15 min: the same; issuance + 0, the lenient parse, or reports from all 17 offices:
unchanged (63 trades, $435). Fill delay 1 min instead of 2: 63 trades, +12.2c, $6.39/day.

**Report offices.** Minneapolis and Dallas label their 4 PM report "VALID AS OF 0400 PM" (no TODAY), so the bot's
parser only takes their 7 PM report (usable ~19:45); this changes nothing in the live backtest result. Denver does
issue a "VALID TODAY AS OF 0400 PM" report (usable ~16:40) and Austin a 5 PM one; Phoenix, Las Vegas, Seattle, San
Diego, SF and LA issue theirs after 00Z. The earlier note that Denver, Austin and Phoenix have no afternoon report was
wrong: the old "earliest report of the day" logic picked Denver's 06:00 issuance (max since midnight, "VALID AS OF
0600 AM"), which hid the 4 PM one.

**Assumptions and limits.**
* Issuance is known only to the minute (file name and WMO header have no seconds); usable at issuance + 0 gives the
  same live result. Where the WMO header is up to 3 h later than the file-name time, the later time is used (2
  afternoon files).
* METAR availability lag is not modelled: a reading counts from its valid minute, while the bot sees hourly readings
  a few minutes later.
* The report archive for NYC, Miami and Chicago ends 2026-09-23, so the report cannot lift the max there on 09-24 →
  09-26.
* Fills at the displayed top-of-book price with no size limit; Kalshi's fee rounding to the cent is not modelled.
* Kalshi's settlement equalled the NWS climate high on 1,088 of 1,089 station-days.

**Go-live bar (2026-10-01; replaces section 4's two-week, ≥ 30 fills, ≥ 90% win bar for this job).** No real money
until the paper job's R0 + R2 fills, sized at equal dollars, show both:
1. return per $ > 0 with t ≥ 2 on equal-$ stakes, over roughly 60–100+ fills; and
2. zero wrong-side fills caused by data or code errors (a fill contradicted by the observations available at the
   time: a bad sensor value, a parse error such as negative strikes, a stale or misread report, a wrong bucket).

The backtest itself does not meet point 1 (t 1.39 with the Seattle trade, 1.14 without). How many fills it takes
depends on the true edge: with the backtest's spread of returns, t reaches 2 after about 20 fills if paper behaves
like the NO ≥ 0.60 subset, about 130 like the full backtest, and about 190 like the backtest without Seattle. Do not
stop before about 60 fills even if t passes 2 sooner. At about 0.9 fills a day, fewer with downtime and thin books,
60–100 fills take roughly 2–4 months.

Paper so far (2026-10-01): 3 settled fills, all won, +$7.48 on $59 staked (Atlanta R2 and Phoenix R0 on 09-28,
Denver R2 on 09-30, the last cut to 12 contracts by displayed size). Three fills prove nothing.

Paper job configuration (2026-09-27, revised; checked against the installed plist 2026-10-01): R0 + R2, report trigger off
(`USTEMP_CLI_TRIGGER=0`), the report kept as a trusted observation for R2 (`USTEMP_CLI_OBS=1`), YES cap 0.80, $25
per trade, all 17 cities in paper. Expected pace about one trade a day across 17 cities; the edge per trade rests on
about 60 backtest trades, one of which carries two thirds of the dollar profit.
