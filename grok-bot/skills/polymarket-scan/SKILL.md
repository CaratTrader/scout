---
name: polymarket-scan
description: Scan liquid Polymarket binaries, flag completeness gaps and Grok fair-value gaps of 8%+, and write a sized paper report. Use when scanning Polymarket, hunting mispricing, running the 10-minute scout loop, or the user says /polymarket-scan.
---

# Polymarket scan

Run this as a read-only pass. Do not click Buy/Sell.

## Inputs

- Public Polymarket data only (site or `https://gamma-api.polymarket.com/markets/keyset?closed=false&limit=100`).
- Paginate with `next_cursor` until 400 markets or the cursor ends.
- Skip closed, non-orderbook, and restricted-if-untradeable rows.

## Filters (keep in this file only)

Keep a market only if all of these hold:

- `enableOrderBook` is true and `active` is true
- 24h volume ≥ 2000
- liquidity ≥ 5000
- spread ≤ 0.04
- both outcome asks are in (0, 1)

## Signals

1. Completeness: `yes_ask + no_ask ≤ 0.92` (edge ≥ 8%). Side is BOTH.
2. Fair-value: estimate P(yes) from current public sources (X, news, official pages). Flag YES if `p_yes - yes_ask ≥ 0.08`, NO if `(1 - p_yes) - no_ask ≥ 0.08`. Drop the row if you cannot cite a source.

## Size

- Paper bankroll starts at $50 unless the ledger says otherwise.
- Half Kelly, then cap at 6% of current paper cash.
- Max 8 open paper positions.
- Halt if cash < $5 and no positions remain.

## Output

Write `/workspace/scout/last_scan.md` and paste a copy in the conversation:

```
time_utc:
scanned:
flagged:
paper_cash:
halted:

# candidates
- edge: 0.00
  kind: completeness|grok
  side: YES|NO|BOTH
  stake_usd: 0.00
  question:
  url:
  thesis:
  source:
```

If nothing flags: `no trade. wait for the next scan.`

## Approval

Stop after the report. A paper fill is a later message. A live order is forbidden unless the user typed `ARM LIVE` today.
