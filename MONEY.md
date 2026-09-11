# Where money actually goes

Two bills. Neither goes into this repo. 5-minute paper setup is `QUICKSTART.md` — you only pay xAI for that.

## 1. Grok 4.6 — the brain

- Create a key at https://console.x.ai
- Put credits on that xAI account
- Put the key in `.env` as `XAI_API_KEY`
- This pays for scoring, web search, and X search
- Without this, the loop only looks for completeness arb (rare) and sits

Start with $20–$50 of xAI credit. A 10-minute Grok pass over 8 markets with search is the expensive part.

## 2. Polymarket — the bets

- Account: https://polymarket.com
- Asset: **USDC on Polygon**
- Destination: your Polymarket **funder / proxy wallet**, shown in the site deposit flow — not a random EOA unless you trade as an EOA
- Size to start: **$50 USDC**. Same as the viral post. Not $5,273.

Then in `.env`:

- `POLYMARKET_FUNDER` = that deposit address
- `POLYMARKET_PRIVATE_KEY` = the key that signs for that account (export from the same wallet you used to deposit)
- `POLYMARKET_SIGNATURE_TYPE` = `1` for a normal polymarket.com email/Magic login, `0` if you deposited from a plain EOA

Do not send ETH as the stake. Gas is a little POL/MATIC. The bet is USDC.

## What not to fund

- This git repo
- Anyone’s “managed wallet”
- SuperGrok Bot subscription unless you also want the cloud teammate in `grok-bot/`
- More than you can lose in one evening

## When to switch LIVE on

1. `python3 -m scout loop` in paper until you have seen real fills you agree with
2. Deposit $50 USDC
3. `LIVE=1` in `.env`
4. `python3 -m scout once --live` once
5. Only then `python3 -m scout loop --live`

This agent will not win because it is an agent. Completeness arb is the only locked edge. Grok 4.6 directional bets can lose the whole $50.
