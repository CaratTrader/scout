"""Live recorder for r2_infra_timestamp_fill_audit: REST /markets snapshots of a fixed ticker list every ~5 s (one
batched call per tick, inside the paper bot's idle window), plus a /markets/{t}/orderbook snapshot of one rotating
ticker every OB_EVERY-th tick. Send (t0) and receive (t1) wall-clock times are stored with every snapshot so that the
REST quote can later be compared with the exact-time trade prints (/markets/trades) and the 1-minute candles.
Output: data/kalshi_lab/strategies/r2_infra_timestamp_fill_audit/live.jsonl
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_infra_timestamp_fill_audit_live MINUTES TICKERS_CSV OB_TICKERS_CSV"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_api import klive, OUT, used

EVERY = 5.0
OB_EVERY = 3


def main(minutes: float, tickers: list[str], ob: list[str]) -> None:
    stop = time.time() + minutes * 60; out = (OUT / "live.jsonl").open("a"); i = 0
    while time.time() < stop:
        tick = time.time()
        body, t0, t1 = klive("/markets?tickers=" + ",".join(tickers) + "&limit=100")
        for m in body.get("markets", []):
            out.write(json.dumps({"v": "M", "t0": t0, "t1": t1, "tk": m["ticker"], "bid": m.get("yes_bid_dollars"), "ask": m.get("yes_ask_dollars"),
                                  "bid_sz": m.get("yes_bid_size_fp"), "ask_sz": m.get("yes_ask_size_fp"), "last": m.get("last_price_dollars"),
                                  "vol": m.get("volume_fp"), "oi": m.get("open_interest_fp"), "upd": m.get("updated_time"), "status": m.get("status")}) + "\n")
        if not body:
            out.write(json.dumps({"v": "Merr", "t0": t0, "t1": t1}) + "\n")
        if ob and i % OB_EVERY == 0:
            tk = ob[(i // OB_EVERY) % len(ob)]
            b, s0, s1 = klive(f"/markets/{tk}/orderbook?depth=20")
            out.write(json.dumps({"v": "OB", "t0": s0, "t1": s1, "tk": tk, "book": b}) + "\n")
        out.flush(); i += 1
        time.sleep(max(0.0, EVERY - (time.time() - tick)))
    print("ticks", i, "calls used", used())


if __name__ == "__main__":
    main(float(sys.argv[1]), sys.argv[2].split(","), sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else [])
