"""quake_entertain: KXRT (Rotten Tomatoes) settled markets + 1-minute candles over the last 12 h before close
-> data/kalshi_lab/strategies/quake_entertain/rt_{markets,candles}.jsonl. Waits for the quake download to finish."""
import json, sys, time, subprocess
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.quake_entertain_fetch import kalshi, batch_candles, calls_used, OUT, iso

while subprocess.run(["pgrep", "-f", "quake_entertain_getq.py"], capture_output=True).stdout.strip():
    time.sleep(5)
raw = kalshi("/markets?series_ticker=KXRT&status=settled&limit=1000")["markets"]
ms = [{"t": x["ticker"], "e": x["event_ticker"], "series": "KXRT", "open": iso(x["open_time"]), "close": iso(x["close_time"]),
       "exp": iso(x.get("expected_expiration_time")), "result": x.get("result"), "type": x.get("strike_type"), "floor": x.get("floor_strike"),
       "cap": x.get("cap_strike"), "sub": x.get("yes_sub_title"), "vol": float(x.get("volume_fp") or 0), "value": x.get("expiration_value")} for x in raw]
(OUT / "rt_markets.jsonl").write_text("".join(json.dumps(m) + "\n" for m in ms))
ev = defaultdict(list)
for m in ms:
    ev[m["e"]].append(m)
cf = OUT / "rt_candles.jsonl"
have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
for e, xs in sorted(ev.items(), key=lambda kv: kv[1][0]["close"]):
    if all(m["t"] in have for m in xs):
        continue
    end = max(m["close"] for m in xs); start = end - 12 * 3600
    cs = {}
    for i in range(0, len(xs), 10):
        cs.update(batch_candles([m["t"] for m in xs[i:i + 10]], start, end))
    with cf.open("a") as f:
        for m in xs:
            f.write(json.dumps({"t": m["t"], "c": cs.get(m["t"], [])}) + "\n")
    print(e, len(xs), sum(len(v) for v in cs.values()), "calls", calls_used(), flush=True)
