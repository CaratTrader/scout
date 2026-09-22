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

