# tools/ — third-party Polymarket repos kept for reference

Cloned read-only with `git clone --depth 1` (2026-09-12). They are **not** part of this project,
not committed here (see `.gitignore`), and must never be run with the live keys. Use them to read
how others solved data collection, market making and execution, then port what survives our
real-book tests into `lab/` or `scout/`.

| repo | what it is | why it is here |
|---|---|---|
| `poly-maker` (warproxxx) | automated Polymarket market-making bot | reference for resting-order lifecycle, cancels, position limits — the "rest at 0.99" lock variant |
| `poly_data` (warproxxx) | Polymarket data retriever (markets, trades, order books) | bulk historical trades for tape studies |
| `polyledger` (nahrek) | resumable indexer: CLOB metadata + on-chain trades from Polygon | complete trade history without API limits |
| `polybot` (ent0n29) | reverse-engineering of Polymarket strategies and HFT infrastructure (Java) | what the fast takers in the 5m markets do |
| `polymarket-arbitrage-trading-bot` (radioman) | C++ BTC/ETH 5-minute arbitrage bot | its arbitrage definition and latency tricks |
| `5min-btc-polymarket` (Novals83) | BTC 5-minute momentum skill | a public momentum rule to put through our real-book replay |
| `Polymarket-BTC-15-Minute-Trading-Bot` (aulekator) | 15-minute BTC bot | same, 15m |
| `prediction-market-analysis` (Jon-Becker) | data framework + public datasets | cross-market analysis ideas |

Other notable repos not cloned: `Polymarket/py-clob-client` (already a dependency),
`pmxt-dev/pmxt` (unified API across Polymarket/Kalshi), `SII-WANGZJ/Polymarket_data` (1.1 B trade
records dataset), `FrondEnt/PolymarketBTC15mAssistant`.

Refresh a clone: `cd tools/<name> && git pull`. Our own research lives in `lab/` and `docs/`.
