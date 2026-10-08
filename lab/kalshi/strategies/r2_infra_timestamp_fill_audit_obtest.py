"""Orderbook freshness test: poll /markets/{ticker}/orderbook every ~3 s (inside the bot idle window) for SECONDS,
then fetch the ticker's trade prints over the same span. Output: obtest.jsonl + prints_obtest.json.
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_infra_timestamp_fill_audit_obtest TICKER SECONDS"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_api import klive, kget, OUT, used


def main(tk: str, seconds: float) -> None:
    stop = time.time() + seconds; f = (OUT / "obtest.jsonl").open("a"); t_first = None
    while time.time() < stop:
        t = time.time()
        b, t0, t1 = klive(f"/markets/{tk}/orderbook?depth=10")
        t_first = t_first or t0
        f.write(json.dumps({"v": "OB", "t0": t0, "t1": t1, "tk": tk, "book": b}) + "\n"); f.flush()
        time.sleep(max(0.0, 3.0 - (time.time() - t)))
    lo, hi = int(t_first) - 120, int(time.time()) + 30
    time.sleep(5)
    d = kget(f"/markets/trades?ticker={tk}&min_ts={lo}&max_ts={hi}&limit=1000")
    (OUT / "prints_obtest.json").write_text(json.dumps({"tk": tk, "lo": lo, "hi": hi, "prints": d.get("trades") or [], "cursor": d.get("cursor")}))
    print("orderbooks done, prints", len(d.get("trades") or []), "calls", used())


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]))
