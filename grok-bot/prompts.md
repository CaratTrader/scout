# Paste these into Grok Bot, in order

## 1. Create the Bot

New chat → Create new agent. Then Edit Profile.

Name: `Scout`
Title: `Polymarket paper scout`

Paste `grok-bot/charter.md` into the description. Save.

## 2. First job (send as the first message)

```
Read grok-bot/charter.md and grok-bot/skills/polymarket-scan/SKILL.md.

Outcome: one paper scan of liquid Polymarket binaries.
Sources: Polymarket public pages / Gamma keyset. No login yet.
Constraints: do not click Buy or Sell. Do not message anyone.
Deliverable: last_scan.md in the format from the skill, plus the same report in this chat.
Review point: stop after the report. Ask me which candidate, if any, to paper-fill.
```

## 3. Teach a task (optional, after a good scan)

Open computer view → Teach a task. Demonstrate only the **read path**: open Polymarket, open a liquid market, read yes/no, liquidity, spread. Do not submit an order during the recording. After it drafts a skill, merge the filters and approval rules from `polymarket-scan/SKILL.md`.

## 4. Paper fill

```
Run size-and-ledger. Paper-fill candidate <N> from the last scan. Show the new cash and positions. Do not place a live order.
```

## 5. Routine (only after two scans you would sign)

```
Every 10 minutes, run polymarket-scan against live Polymarket data.
Write last_scan.md and post the report in this conversation.
Do not place orders. Do not send Telegram.
If Gamma or Polymarket is down, report the failure. Do not reuse a stale scan.
Timezone: the one in Settings → General → Agent.
```

Then **Test run**. Confirm it stops at the report. Enable only after that.

## 6. Telegram (optional, later)

Connect Telegram in Settings → Plugins. Then:

```
When a scan flags edge ≥ 8%, draft a Telegram message from last_scan.md. Do not send it until I approve.
```

## 7. Live trading (off by default)

Not part of setup. If you ever want the Bot to click through a real order, you must type `ARM LIVE` that day, stay on the computer view, and complete login / 2FA / CAPTCHA yourself. Size and filters still apply. This repo will not sign CLOB orders for you.
