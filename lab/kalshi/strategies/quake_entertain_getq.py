"""quake_entertain: download KXBIGGESTQUAKE markets + 1-minute candles (whole event life) into
data/kalshi_lab/strategies/quake_entertain/quake_{markets,candles}.jsonl. ~2 Kalshi calls per event."""
import json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.quake_entertain_fetch import series_markets, batch_candles, calls_used, OUT

ms = series_markets("KXBIGGESTQUAKE")
(OUT / "quake_markets.jsonl").write_text("".join(json.dumps(m) + "\n" for m in ms))
ev = defaultdict(list)
for m in ms:
    ev[m["e"]].append(m)
cf = OUT / "quake_candles.jsonl"
have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
for e, xs in sorted(ev.items(), key=lambda kv: kv[1][0]["open"]):
    todo = [m for m in xs if m["t"] not in have]
    if not todo:
        continue
    start = min(m["open"] for m in xs); end = max(m["close"] for m in xs)
    cs = batch_candles([m["t"] for m in xs], start, end)
    with cf.open("a") as f:
        for m in xs:
            f.write(json.dumps({"t": m["t"], "c": cs.get(m["t"], [])}) + "\n")
    print(e, len(xs), sum(len(v) for v in cs.values()), "calls", calls_used(), flush=True)
