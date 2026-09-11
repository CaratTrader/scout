# Live: MetaMask → USDC → CLOB

## Short-window safety

The live crypto path validates signal age, quote age, window time, and price dislocation
immediately before signing. Do not raise `CRYPTO_SIGNAL_TTL_SECONDS` to hide latency;
missed trades are safer than executing an obsolete probability. Grok runs on a background
worker and is intentionally outside the 5m/15m execution path.

Complete-set legs use FOK orders. If the second leg fails, Scout immediately attempts to
flatten the first and pauses entries until collateral can be reconciled. This is a
best-effort safeguard because the venue does not provide atomic cross-token matching.

Use `python -m scout performance` after a session. Newly enriched trades report signal
latency and Brier calibration in addition to realized, fee-inclusive P&L.

Do this on **your machine**. Do **not** paste a private key into chat.

## 1. Wallet

1. New MetaMask account (throwaway size). Save the seed offline.
2. Open https://polymarket.com → **Connect MetaMask**.
3. Profile menu: copy the **account wallet** address (the one Polymarket shows, often *not* identical to the MetaMask address). That is `POLYMARKET_FUNDER`.
4. MetaMask: copy the **account** address. That is the signer.

## 2. Deposit

On polymarket.com use **Deposit**. Send **USDC**. Polymarket wraps it to **pUSD** (that is what the CLOB spends). Start **$50**. Also keep a little **POL** in MetaMask if the site asks you to sign an on-chain approve.

Site deposit is the easy path. Do not send random tokens to a random Polygon address.

You want **≥ 10 pUSD** showing in the Polymarket UI before the bot can trade.

## 3. Export the signer key

MetaMask → Account details → Show private key. That is `POLYMARKET_PRIVATE_KEY`.

## 4. Put it in `.env` (this folder)

```
LIVE=0
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER=0x...
POLYMARKET_SIGNATURE_TYPE=3
STARTING_BANKROLL=50
```

Signature type (one number):

| How you logged in | Type |
| --- | --- |
| MetaMask / Rabby on polymarket.com **after May 2026** (Deposit Wallet) | **3** |
| MetaMask / Rabby **older** Safe wallet | **2** |
| Email / Magic / Google on polymarket.com | **1** |
| Raw EOA, no Polymarket smart wallet | **0** |

If you just connected MetaMask on a new account, use **3**. If the first live order errors on signature, try **2**.

Keep `LIVE=0` until the next message.

## 5. Check (no trade yet)

```bash
cd /home/msd/ai-workspace/bots
.venv/bin/python -m scout doctor
```

Doctor should show funder + key masked, LIVE still off.

## 6. Next message to me

Say: **keys are in .env, trade**.

Do not paste the key. I will set `LIVE=1` and run one cycle.
