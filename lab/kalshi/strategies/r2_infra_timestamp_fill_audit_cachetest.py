"""REST cache test: alternate /markets/{ticker} and /markets?tickers={ticker} (and /orderbook) for one active ticker
every ~2.5 s inside the bot idle window, N calls in total, then fetch the ticker's prints. Tells whether the ~15 s
step in /markets volume/quotes is a per-URL cache or a backend snapshot cadence, and how stale each endpoint is.
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_infra_timestamp_fill_audit_cachetest TICKER N"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_api import klive, kget, OUT, used

URLS = ["/markets/{tk}", "/markets?tickers={tk}"]


def main(tk: str, n: int) -> None:
    f = (OUT / "cachetest.jsonl").open("a"); t_first = None
    for i in range(n):
        t = time.time(); u = URLS[i % 2].format(tk=tk)
        b, t0, t1 = klive(u)
        t_first = t_first or t0
        m = b.get("market") or (b.get("markets") or [{}])[0]
        f.write(json.dumps({"u": URLS[i % 2], "t0": t0, "t1": t1, "vol": m.get("volume_fp"), "bid": m.get("yes_bid_dollars"), "ask": m.get("yes_ask_dollars"),
                            "last": m.get("last_price_dollars")}) + "\n"); f.flush()
        time.sleep(max(0.0, 2.5 - (time.time() - t)))
    lo, hi = int(t_first) - 150, int(time.time()) + 20
    time.sleep(5)
    d = kget(f"/markets/trades?ticker={tk}&min_ts={lo}&max_ts={hi}&limit=1000")
    (OUT / "prints_cachetest.json").write_text(json.dumps({"tk": tk, "lo": lo, "hi": hi, "prints": d.get("trades") or [], "cursor": d.get("cursor")}))
    print("done prints", len(d.get("trades") or []), "calls", used())


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
