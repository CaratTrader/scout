---
name: size-and-ledger
description: Record paper Polymarket fills, mark the book, and refuse live orders unless ARM LIVE was typed today. Use when logging a paper trade, checking bankroll, Kelly sizing, or /size-and-ledger.
---

# Size and ledger

Ledger file: `/workspace/scout/ledger.json`. Create it on first use:

```json
{
  "mode": "paper",
  "starting_bankroll": 50,
  "cash": 50,
  "halted": false,
  "positions": [],
  "fills": []
}
```

## Paper fill

Only fill from a candidate in the latest scan. Subtract stake from cash, append the fill, append the position. Never size above 6% of cash. Never open a 9th position.

## Mark

Unrealized value = sum(shares * current outcome price). Equity = cash + unrealized. Report both.

## Live

If the user has not typed `ARM LIVE` in this conversation on today's date, refuse any browser click that submits an order. Offer to record paper instead.
