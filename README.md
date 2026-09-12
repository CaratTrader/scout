# Scout — Polymarket crypto up/down bot, strategy lab and research

**For collaborators (Sep 2026):** the live edge we trade and how it was found is in
[docs/STRATEGY_RESEARCH.md](docs/STRATEGY_RESEARCH.md) (sections 9–12 are the real-book
work); the strategy lab that tests ideas on recorded order books is described in
[lab/README.md](lab/README.md) (`python -m lab.replay_ticks --only <strategy>`); recorded
1-second books for BTC/ETH/SOL/XRP/DOGE/BNB/HYPE 5m/15m/4h windows are in `data/lab/ticks/`.
Add a strategy to `lab/strategies.py`, replay it, and open a PR with the numbers. Never commit
`.env` or anything under `data/` except those recordings.

**Start here: [QUICKSTART.md](QUICKSTART.md)** — paper loop in ~5 minutes.

```bash
./setup.sh
# paste XAI_API_KEY into .env
.venv/bin/python -m scout doctor
.venv/bin/python -m scout loop
```

Paper unless `--live` is explicitly present. `.env` alone cannot activate live orders.

It will not “just win.” Completeness arb is the only locked edge. Grok directional bets can lose the bankroll.

| Command | What it does |
| --- | --- |
| `./setup.sh` | venv, deps, `.env`, doctor |
| `python -m scout doctor` | keys, Gamma, xAI |
| `python -m scout loop` | paper loop, 60s |
| `python -m scout once` | one cycle |
| `python -m scout status` | ledger |
| `python -m scout performance` | realized P&L, fees, latency, and calibration |
| `python -m scout loop --live` | real USDC |

Short-window live orders are fail-closed: signals and CLOB quotes expire after three
seconds, entries stop before resolution, and a large favorable-looking price collapse
forces a rescore. Grok research runs in the background and cannot delay crypto execution.

Money: `MONEY.md`. Optional Grok Bot paste pack: `grok-bot/`.
