# 5 minutes to a running loop

Paper first. Live is a second pass. You do not need Polymarket money to start.

## Minute 0–2: machine

```bash
cd /home/msd/ai-workspace/bots
./setup.sh
```

That creates `.venv`, installs deps, copies `.env`.

## Minute 2–4: brain

1. Open https://console.x.ai
2. Add **$20–50** credit (this pays Grok 4.6 + search, not bets)
3. Create an API key
4. Put it in `.env`:

```
XAI_API_KEY=xai-...
```

5. Check:

```bash
.venv/bin/python -m scout doctor
```

All green except LIVE (LIVE should stay off).

## Minute 4–5: run

```bash
.venv/bin/python -m scout loop
```

Leave it. Paper ledger is `data/ledger.json`. Ctrl-C stops it.

Without the xAI key the loop still scans, but it almost never bets. Completeness gaps of 2%+ are rare.

---

# Later: anonymous live ($50 USDC)

Do this only after paper has produced fills you would have taken.

Fast anonymous path (EOA, no Magic email):

1. New wallet in Rabby/MetaMask. Write the seed on paper. This is throwaway size.
2. From an exchange, **withdraw USDC on Polygon** to that address. Amount: **$50**. Also send **~$2 POL** for gas.
3. Open https://polymarket.com → Connect that wallet → deposit if the site uses a proxy. Copy the **funder / deposit** address it shows.
4. `.env`:

```
LIVE=0
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_FUNDER=0x...          # proxy if the site gave you one, else the same address
POLYMARKET_SIGNATURE_TYPE=0      # 0 = EOA. use 1 only if you logged in with email/Magic
```

5. One shot, then loop:

```bash
# keep LIVE=0 until this command
# then:
# LIVE=1 in .env
.venv/bin/python -m scout doctor
.venv/bin/python -m scout once --live
.venv/bin/python -m scout loop --live
```

If doctor says LIVE keys missing, stop. Do not guess signature_type. Email login = `1`. Raw wallet = `0`.

Full wallet notes: `MONEY.md`.
