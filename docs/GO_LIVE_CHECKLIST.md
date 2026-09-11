# Going live — the checklist (your hands, not the bot's)

Claude will not enter keys, set `LIVE=1`, or place orders. Everything below is done by you,
on this Mac, in a terminal. Do not paste keys into any chat.

## 0. Read this first

As of 2026-09-06 no strategy has a real-book edge: Scout is t = 1.6 on 195 paper trades and
its clone on recorded live books is +0.08 per $ with t = 0.5. The realistic expectation for a
live run is roughly zero minus fees, with wide swings. If you go live anyway, treat the money
as tuition: an amount you can lose entirely without caring.

## 1. Money and wallet (see LIVE.md for the wallet detail)

- New wallet in MetaMask or Rabby, seed on paper, connect it at polymarket.com, use the site's
  **Deposit** flow with **USDC on Polygon**. Start with **$50**. Keep ~$2 of POL for gas.
- Copy the account/funder address the site shows.

## 2. Keys into `.env` — typed by you

Edit `~/Tradeinc/.env` and fill these (the relayer address is already there):

```
POLYMARKET_PRIVATE_KEY=0x...          # exported from the same wallet you deposited from
POLYMARKET_FUNDER=0x...               # the address polymarket.com shows for your account
POLYMARKET_SIGNATURE_TYPE=3           # 3 new Deposit Wallet · 2 older Safe · 1 email/Magic · 0 raw EOA
RELAYER_API_KEY=...                   # your relayer key
LIVE=0                                # leave it: the --live flag is what trades; this only picks the dashboard's default view
```

`chmod 600 ~/Tradeinc/.env` afterwards.

## 3. Minimum-risk caps — already set for you

These live in `docs/com.tradeinc.scout.live.plist` and `live_once.sh`, so the paper bot keeps
its own settings. For reference:

```
MIN_CRYPTO_STAKE=1
MAX_CRYPTO_STAKE=2                    # $2 per trade: ~4% of a $50 bankroll
MAX_CRYPTO_POSITIONS=1
MAX_POSITIONS=1
CRYPTO_MIN_EDGE=0.05                  # trade less, only the stronger signals
CRYPTO_MAX_SLIP=0.01                  # do not chase a moving book
CRYPTO_SIGNAL_TTL_SECONDS=2
MAX_SESSION_DRAWDOWN=6                # halts after ~3 losing $2 trades in a session
HALT_BANKROLL=40                      # halts for good below $40 cash: worst case ≈ −$10
CAMPAIGN_TARGET_USD=100               # halts for your review at +$50
```

In live mode a halt **stays halted** (no 30-minute auto-reset); you restart deliberately.
"Less risk, more reward" in a binary market only has two dials: smaller stakes and fewer,
stronger signals. A cheaper entry is not less risk; it is a lower win rate.

## 4. Verify without trading

```bash
cd ~/Tradeinc && .venv/bin/python -X utf8 -m scout doctor
```

Expect: keys shown masked, relayer ok, LIVE off. If doctor complains about the signature
type, do not guess: 1 for email login, 3 for a fresh MetaMask deposit wallet, 2 if the first
live order later fails on signature.

## 5. One real cycle, then the loop

```bash
~/Tradeinc/live_once.sh
```

(runs doctor, then one live cycle with the $2 caps; the paper job keeps running paper).

Read the output. It syncs cash from the CLOB, scans, and either posts one taker order at
≤ $2 or reports why not. Check the position on polymarket.com. Only then start the loop as a
service. A ready-made plist is in `docs/com.tradeinc.scout.live.plist` — copy it, do not
edit the paper one:

```bash
cp ~/Tradeinc/docs/com.tradeinc.scout.live.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.tradeinc.scout.live.plist
tail -f ~/Tradeinc/data/live.log
```

The dashboard has a **Paper / Live** switch in the top bar; the Live view shows
`data/ledger_live.json` with a red REAL MONEY badge.

## 6. Kill switch

```bash
launchctl bootout gui/501/com.tradeinc.scout.live      # stops new orders immediately
```

Open positions are binary shares: they settle on their own at the window end and the venue
credits winnings; nothing needs closing.

## 7. Judge it like paper

Same rule as before: 200 fills, t ≥ 2, seven positive days before any size increase. The
dashboard computes all three on the live ledger. Fees show in every fill; compare
`python -m scout performance` weekly against the paper record.

## 8. Lock profile (2026-09-10)

The lag/underdog model lost 5 of 5 live trades. The real-book study in
`docs/STRATEGY_RESEARCH.md` section 12 found the one pocket with a positive real-book record:
the late-window TWAP lock, favourite at 0.955–0.98 inside the last 45 s with |z| ≥ 3.5
(133 windows, 0 losses; 1 loss in 143 at 3.3). To run the live job on that profile (and
nothing else):

```bash
cd ~/Tradeinc && zsh live_switch_to_lock.sh
```

It copies `docs/com.tradeinc.scout.live.lock.plist` over the installed live plist and restarts
the job. Verify with `tail -f data/live.log`: cycles every ~2 s, `locks=` counts on the
`crypto 5m/15m` line, fills with `edge_type twap_lock`. Reverse with the old template:

```bash
cp docs/com.tradeinc.scout.live.plist ~/Library/LaunchAgents/com.tradeinc.scout.live.plist && zsh live_switch_to_lock.sh --restart-only
```

Read-only preview of what it would buy, without touching the live job:

```bash
.venv/bin/python -m lab.lock_dryrun --minutes 30
```
