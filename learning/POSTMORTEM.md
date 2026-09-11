# Trade post-mortems — Scout BTC 5m

_Generated 2026-09-01 19:26 · 88 closed trades · 40W-48L · net $+59.68_

## Loss patterns

- **coin_flip_variance**: 38 losses, $-157.52 — expected variance in a +EV band; no action.
- **favorite_upset**: 7 losses, $-34.68 — monitor.
- **cheap_longshot_vs_momentum**: 3 losses, $-9.17 — FIXED: CRYPTO_MIN_ASK=0.25 now blocks this band, and the lessons veto refuses it from the backtest prior.

## Every trade

### August 28, 2:30AM-2:35AM ET — YES @ 0.22 → WON +$6.98

- BTC 79,827.14 → 79,833.73 (+0.8 bps); path by minute (bps vs open): 60s:+0.2, 120s:-1.9, 180s:-0.7, 240s:-1.2, 300s:+0.8
- Market implied P(YES wins) ≈ 22% at entry; claimed model edge +4.0%
- Worked as modeled: momentum persisted (2/5 minutes on our side).

### August 28, 9:45AM-9:50AM ET — YES @ 0.66 → WON +$1.65

- BTC 79,087.99 → 79,196.35 (+13.7 bps); path by minute (bps vs open): 60s:+6.1, 120s:+15.2, 180s:+15.7, 240s:+27.6, 300s:+13.7
- Market implied P(YES wins) ≈ 66% at entry; claimed model edge +3.8%
- Worked as modeled: momentum persisted (5/5 minutes on our side).

### August 28, 9:50AM-9:55AM ET — YES @ 0.45 → WON +$5.62

- BTC 79,196.35 → 79,419.41 (+28.2 bps); path by minute (bps vs open): 60s:+5.3, 120s:+4.3, 180s:+2.5, 240s:+16.2, 300s:+28.2
- Market implied P(YES wins) ≈ 45% at entry; claimed model edge +8.6%
- Worked as modeled: momentum persisted (5/5 minutes on our side).

### August 28, 9:55AM-10:00AM ET — NO @ 0.10 → LOST −$3.41

- BTC 79,419.41 → 79,330.34 (-11.2 bps); path by minute (bps vs open): 60s:+0.1, 120s:-2.6, 180s:+1.5, 240s:+15.0, 300s:-11.2
- Market implied P(NO wins) ≈ 90% at entry; claimed model edge +8.9%
- **Why it lost:** bought a 10% longshot against established momentum. The move never reversed (on our side 2/5 minutes). The book was right; the Gaussian tail model overestimates reversal odds — backtest: this price band wins 7.5% needing ~11%, −36%/$ staked.

### August 28, 10:15AM-10:20AM ET — NO @ 0.13 → LOST −$3.18

- BTC 78,626.0 → 78,884.44 (+32.9 bps); path by minute (bps vs open): 60s:-10.9, 120s:-6.9, 180s:+13.5, 240s:+22.0, 300s:+32.9
- Market implied P(NO wins) ≈ 87% at entry; claimed model edge +8.5%
- **Why it lost:** bought a 13% longshot against established momentum. The move never reversed (on our side 2/5 minutes). The book was right; the Gaussian tail model overestimates reversal odds — backtest: this price band wins 7.5% needing ~11%, −36%/$ staked.

### August 28, 12:15PM-12:20PM ET — YES @ 0.07 → LOST −$2.58
- no price data

### August 28, 1:10PM-1:15PM ET — NO @ 0.48 → LOST −$2.47
- no price data

### August 28, 1:30PM-1:35PM ET — YES @ 0.67 → WON +$1.38
- no price data

### August 28, 1:40PM-1:45PM ET — YES @ 0.34 → WON +$4.76
- no price data

### August 28, 1:50PM-1:55PM ET — YES @ 0.29 → LOST −$5.22
- no price data

### August 28, 2:10PM-2:15PM ET — YES @ 0.52 → LOST −$8.27
- no price data

### August 28, 2:55PM-3:00PM ET — NO @ 0.45 → LOST −$2.28
- no price data

### August 28, 3:35PM-3:40PM ET — NO @ 0.48 → LOST −$4.19
- no price data

### August 28, 4:20PM-4:25PM ET — NO @ 0.60 → LOST −$5.14
- no price data

### August 28, 4:40PM-4:45PM ET — YES @ 0.56 → WON +$2.59
- no price data

### August 28, 4:45PM-4:50PM ET — NO @ 0.69 → LOST −$4.18
- no price data

### August 28, 5:10PM-5:15PM ET — YES @ 0.27 → WON +$11.86
- no price data

### August 28, 5:15PM-5:20PM ET — NO @ 0.25 → WON +$8.16
- no price data

### August 28, 5:35PM-5:40PM ET — NO @ 0.34 → LOST −$3.20
- no price data

### August 29, 2:05PM-2:10PM ET — YES @ 0.36 → WON +$8.25
- no price data

### August 29, 2:10PM-2:15PM ET — YES @ 0.26 → LOST −$2.10
- no price data

### August 29, 2:30PM-2:35PM ET — YES @ 0.37 → LOST −$5.22
- no price data

### August 29, 3:20PM-3:25PM ET — NO @ 0.38 → WON +$3.18
- no price data

### August 29, 3:25PM-3:30PM ET — YES @ 0.27 → LOST −$2.10
- no price data

### August 29, 3:50PM-3:55PM ET — NO @ 0.66 → WON +$1.64
- no price data

### August 29, 4:00PM-4:05PM ET — YES @ 0.39 → WON +$5.66
- no price data

### August 29, 4:10PM-4:15PM ET — YES @ 0.28 → LOST −$2.16
- no price data

### August 29, 4:35PM-4:40PM ET — YES @ 0.71 → WON +$1.45
- no price data

### August 29, 4:40PM-4:45PM ET — NO @ 0.33 → WON +$9.92
- no price data

### August 29, 4:45PM-4:50PM ET — NO @ 0.35 → WON +$5.98
- no price data

### August 29, 4:50PM-4:55PM ET — YES @ 0.66 → WON +$2.46
- no price data

### August 29, 5:00PM-5:05PM ET — YES @ 0.28 → LOST −$5.25
- no price data

### August 29, 5:20PM-5:25PM ET — YES @ 0.25 → LOST −$5.26
- no price data

### August 29, 6:05PM-6:10PM ET — YES @ 0.26 → WON +$8.22
- no price data

### August 29, 6:10PM-6:15PM ET — YES @ 0.39 → LOST −$5.21
- no price data

### August 29, 6:30PM-6:35PM ET — NO @ 0.37 → LOST −$3.50
- no price data

### August 29, 7:10PM-7:15PM ET — NO @ 0.28 → WON +$11.55
- no price data

### August 29, 7:15PM-7:20PM ET — NO @ 0.35 → LOST −$3.46
- no price data

### August 29, 7:35PM-7:40PM ET — NO @ 0.30 → LOST −$2.73
- no price data

### August 29, 8:40PM-8:45PM ET — NO @ 0.30 → WON +$11.33
- no price data

### August 29, 8:50PM-8:55PM ET — YES @ 0.29 → LOST −$5.13
- no price data

### August 29, 9:10PM-9:15PM ET — YES @ 0.37 → LOST −$5.22
- no price data

### August 29, 9:55PM-10:00PM ET — YES @ 0.27 → WON +$7.00
- no price data

### August 29, 10:00PM-10:05PM ET — YES @ 0.27 → WON +$13.26
- no price data

### August 30, 9:00AM-9:05AM ET — YES @ 0.29 → LOST −$5.25
- no price data

### August 30, 9:35AM-9:40AM ET — NO @ 0.63 → WON +$2.81
- no price data

### August 30, 9:45AM-9:50AM ET — YES @ 0.79 → WON +$1.26
- no price data

### August 30, 9:50AM-9:55AM ET — YES @ 0.69 → LOST −$5.11
- no price data

### August 30, 10:15AM-10:20AM ET — YES @ 0.27 → LOST −$2.14
- no price data

### August 30, 10:50AM-10:55AM ET — YES @ 0.91 → WON +$0.46
- no price data

### August 30, 11:15AM-11:20AM ET — YES @ 0.28 → WON +$6.66
- no price data

### August 30, 11:30AM-11:35AM ET — NO @ 0.37 → LOST −$3.62
- no price data

### August 30, 11:55AM-12:00PM ET — NO @ 0.75 → LOST −$5.09
- no price data

### August 30, 12:35PM-12:40PM ET — NO @ 0.34 → WON +$9.47
- no price data

### August 30, 12:40PM-12:45PM ET — YES @ 0.89 → LOST −$5.04
- no price data

### August 30, 1:25PM-1:30PM ET — NO @ 0.79 → LOST −$5.07
- no price data

### August 30, 3:15PM-3:20PM ET — NO @ 0.85 → WON +$0.83
- no price data

### August 30, 3:25PM-3:30PM ET — NO @ 0.31 → LOST −$2.98
- no price data

### August 30, 3:45PM-3:50PM ET — NO @ 0.25 → LOST −$4.90
- no price data

### August 30, 4:25PM-4:30PM ET — YES @ 0.39 → LOST −$5.21
- no price data

### August 30, 4:45PM-4:50PM ET — NO @ 0.36 → LOST −$5.22
- no price data

### August 30, 6:35PM-6:40PM ET — YES @ 0.25 → WON +$14.74
- no price data

### August 30, 6:50PM-6:55PM ET — YES @ 0.30 → WON +$11.42
- no price data

### August 30, 6:55PM-7:00PM ET — YES @ 0.86 → WON +$0.77
- no price data

### August 30, 7:00PM-7:05PM ET — NO @ 0.85 → LOST −$5.05
- no price data

### August 30, 7:40PM-7:45PM ET — YES @ 0.35 → LOST −$3.95
- no price data

### August 30, 8:25PM-8:30PM ET — YES @ 0.25 → LOST −$5.26
- no price data

### August 30, 9:10PM-9:15PM ET — NO @ 0.85 → WON +$0.83
- no price data

### August 30, 9:25PM-9:30PM ET — NO @ 0.35 → LOST −$3.93
- no price data

### August 30, 9:55PM-10:00PM ET — NO @ 0.39 → WON +$3.21
- no price data

### August 30, 10:30PM-10:35PM ET — NO @ 0.36 → LOST −$3.02
- no price data

### August 30, 11:15PM-11:20PM ET — NO @ 0.26 → WON +$13.97
- no price data

### August 30, 11:25PM-11:30PM ET — NO @ 0.26 → LOST −$3.32
- no price data

### August 30, 11:50PM-11:55PM ET — YES @ 0.27 → LOST −$2.39
- no price data

### August 31, 12:50AM-12:55AM ET — YES @ 0.34 → WON +$7.05
- no price data

### August 31, 1:25AM-1:30AM ET — NO @ 0.25 → WON +$7.40
- no price data

### August 31, 1:35AM-1:40AM ET — YES @ 0.25 → LOST −$5.26
- no price data

### August 31, 1:55AM-2:00AM ET — YES @ 0.27 → WON +$5.81
- no price data

### August 31, 10:00AM-10:05AM ET — NO @ 0.37 → LOST −$4.48
- no price data

### August 31, 10:25AM-10:30AM ET — NO @ 0.32 → LOST −$2.67
- no price data

### August 31, 10:05PM-10:10PM ET — YES @ 0.26 → LOST −$5.26
- no price data

### August 31, 10:25PM-10:30PM ET — YES @ 0.39 → LOST −$5.21
- no price data

### September 1, 9:10AM-9:15AM ET — NO @ 0.36 → LOST −$5.22
- no price data

### September 1, 9:40AM-9:45AM ET — YES @ 0.37 → WON +$3.32
- no price data

### September 1, 9:55AM-10:00AM ET — YES @ 0.39 → LOST −$5.21
- no price data

### September 1, 10:15AM-10:20AM ET — NO @ 0.26 → WON +$13.97
- no price data

### September 1, 10:20AM-10:25AM ET — YES @ 0.32 → WON +$9.45
- no price data

### September 1, 5:00PM-5:05PM ET — NO @ 0.25 → WON +$14.74
- no price data
