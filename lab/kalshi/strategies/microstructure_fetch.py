"""microstructure: archived KXRAIN 1-minute candles for the pre-registered second holdout (frozen.json addendum_2).
All markets of each archived date, most recent date first, one /historical/markets/{ticker}/candlesticks call per market
over the last 18 h before close, until the call budget is used. Market list read from rain_history's markets.jsonl (data).
Usage: python -m lab.kalshi.strategies.microstructure_fetch [max_calls]"""
from __future__ import annotations
import json, sys, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K
from lab.kalshi.fetch import compact, window, PLAN

OUT = Path("data/kalshi_lab/strategies/microstructure")
CF = OUT / "arch_candles.jsonl"; MF = OUT / "arch_markets.jsonl"; LOG = OUT / "kalshi_calls.log"
CUTOFF = 1786147200   # 2026-08-08 00:00 UTC: markets closing before are archived


def compact_any(cs: list[dict]) -> list[list]:
    """Live candles use *_dollars keys; archived (/historical) candles use plain close/low/high and volume."""
    def g(c, k, sub):
        d = c.get(k) or {}
        v = d.get(sub + "_dollars", d.get(sub))
        return float(v) if v is not None else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask", "close"), g(c, "yes_bid", "close"), g(c, "yes_ask", "low"), g(c, "yes_bid", "high"),
             float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def main(max_calls: int = 170) -> None:
    ms = [json.loads(l) for l in open("data/kalshi_lab/strategies/rain_history/markets.jsonl")]
    ms = [m for m in ms if m.get("series") == "KXRAIN" and m.get("result") in ("yes", "no") and m["close"] < CUTOFF + 86400]
    by = defaultdict(list)
    for m in ms:
        by[m["e"]].append(m)
    have = {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()
    calls = 0
    with CF.open("a") as fh, MF.open("a") as fm, LOG.open("a") as lg:
        for e in sorted(by, key=lambda e: -max(m["close"] for m in by[e])):
            todo = [m for m in by[e] if m["t"] not in have]
            if calls + len(todo) > max_calls:
                print("budget reached before", e); break
            for m in sorted(by[e], key=lambda m: m["t"]):
                if m["t"] in have:
                    continue
                lo, hi = window(m, PLAN["KXRAIN"])
                arch = m["close"] < CUTOFF + 86400   # dates up to 08-07 are archived (08-07 closes early on 08-08)
                url = (f"{K}/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=1" if arch else
                       f"{K}/markets/candlesticks?market_tickers={m['t']}&start_ts={lo}&end_ts={hi}&period_interval=1")
                txt = fetch(url, pace=1.15); calls += 1
                lg.write(f"{time.time():.0f}\t{len(txt)}\t{url[len(K):][:160]}\n"); lg.flush()
                d = json.loads(txt or "{}")
                cs = d.get("candlesticks")
                if cs is None and d.get("markets"):
                    cs = d["markets"][0].get("candlesticks")
                if cs is None:
                    print("no candles", m["t"], txt[:120]); continue
                fh.write(json.dumps({"t": m["t"], "c": compact_any(cs)}) + "\n"); fh.flush()
                m2 = dict(m); m2.pop("rules", None); fm.write(json.dumps(m2) + "\n"); fm.flush()
                print(e, m["t"], len(cs), "calls", calls, flush=True)
    print("done, calls", calls)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 170)
