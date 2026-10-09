"""Cache-integrity audit for the r4_long_dated_maker_no reproduction: re-fetch a few raw Kalshi responses that drive the
largest validation wins/losses and compare them with the cached copies the reproduction read (<= 10 calls).

Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_maker_no_repro_audit
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_maker_no_repro import OUT, load_candles, norm_candle, load_meta, iso  # noqa: E402

CANDLE_URLS = [
    "/historical/markets/KXBONDIOUT-26MAY01/candlesticks?start_ts=1772150400&end_ts=1775584931&period_interval=60",
    "/historical/markets/KXTRUMPMENTION-26MAR02-KARO/candlesticks?start_ts=1768316400&end_ts=1771293600&period_interval=60",
    "/historical/markets/KXKASHOUT-26APR-JUN01/candlesticks?start_ts=1775221200&end_ts=1780289940&period_interval=60",
    "/historical/markets/KXGOVSHUT-26JAN31/candlesticks?start_ts=1764543600&end_ts=1769878800&period_interval=60",
    "/historical/markets/KXTRUMPSAYNICKNAME-26APR01-COMR/candlesticks?start_ts=1769558400&end_ts=1773799200&period_interval=60",
]
MARKET_URLS = ["/historical/markets/KXBONDIOUT-26MAY01", "/historical/markets/KXTRUMPMENTION-26MAR02-KARO",
               "/historical/markets/KXKASHOUT-26APR-JUN01"]
TRADE_URLS = ["/historical/trades?ticker=KXTRUMPMENTION-26MAR02-KARO&min_ts=1768611720&max_ts=1769216400&limit=1000",
              "/historical/trades?ticker=KXBONDIOUT-26MAY01&min_ts=1772413320&max_ts=1773018000&limit=1000"]


def main():
    cache = OUT / "api_cache"
    cache.mkdir(parents=True, exist_ok=True)
    cand, _ = load_candles()
    tick = {u.split("/")[3] for u in CANDLE_URLS + MARKET_URLS}
    meta = load_meta(tick)
    out = {"candles": {}, "markets": {}, "trades": {}, "calls": 0}
    for i, u in enumerate(CANDLE_URLS + MARKET_URLS + TRADE_URLS):
        f = cache / f"audit_{i}.json"
        if f.exists():
            txt = f.read_text()
        else:
            txt = fetch(K + u, pace=1.15); out["calls"] += 1
            f.write_text(txt)
        j = json.loads(txt) if txt else {}
        if u in CANDLE_URLS:
            t = j.get("ticker")
            new = {r[0]: r for r in (norm_candle(c) for c in j.get("candlesticks", []))}
            old = {r[0]: r for r in cand.get(t, [])}
            common = sorted(set(new) & set(old))
            mism = [ts for ts in common if new[ts][1:4] != old[ts][1:4]]
            out["candles"][t] = {"fresh_rows": len(new), "cached_rows": len(old), "common": len(common), "mismatch_quote_or_high": len(mism),
                                 "examples": [(ts, new[ts], old[ts]) for ts in mism[:3]]}
        elif u in MARKET_URLS:
            m = j.get("market", {})
            c = meta.get(m.get("ticker"), {})
            out["markets"][m.get("ticker")] = {"result_fresh": m.get("result"), "result_cached": c.get("result"),
                                               "close_fresh": m.get("close_time"), "close_cached": c.get("close_time"),
                                               "rules_same": m.get("rules_primary") == c.get("rules_primary")}
        else:
            tr = j.get("trades", [])
            ids = {x.get("trade_id") for x in tr}
            cached = set()
            for d in (Path("data/kalshi_lab/strategies/r4_long_dated_maker_no/api_cache"),):
                for g in d.iterdir():
                    try:
                        jj = json.loads(g.read_text())
                    except Exception:
                        continue
                    if isinstance(jj.get("trades"), list):
                        cached |= {x.get("trade_id") for x in jj["trades"] if x.get("ticker") == (tr[0]["ticker"] if tr else None)}
            out["trades"][u] = {"fresh_prints": len(ids), "in_cache": len(ids & cached)}
    (OUT / "audit_refetch.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str)[:4000])


if __name__ == "__main__":
    main()
