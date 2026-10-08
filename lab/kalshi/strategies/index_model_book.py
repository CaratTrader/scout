"""index_model: live order-book snapshots of the next-closing KXINXU / KXNASDAQ100U event, for the capacity estimate.

Lists the open markets of each series (1 call), keeps the event that closes next, and snapshots the order book of the
markets whose quoted YES ask is inside [lo, hi] (1 call each). Raw books + the index level are appended to
data/kalshi_lab/strategies/index_model/books.jsonl. Every Kalshi call is counted in .../kalshi_calls.json.
Usage: python -m lab.kalshi.strategies.index_model_book [max_per_series] [lo] [hi]"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/index_model"); OUT.mkdir(parents=True, exist_ok=True)
CALLS = OUT / "kalshi_calls.json"


def kcall(url: str) -> dict:
    n = json.loads(CALLS.read_text())["n"] if CALLS.exists() else 0
    CALLS.write_text(json.dumps({"n": n + 1}))
    return json.loads(fetch(url, pace=1.15) or "{}")


def snap(series: str, max_n: int, lo: float, hi: float) -> None:
    d = kcall(f"{K}/markets?series_ticker={series}&status=open&limit=1000")
    ms = d.get("markets") or []
    if not ms:
        print(series, "no open markets"); return
    nxt = min(m["close_time"] for m in ms)
    ev = [m for m in ms if m["close_time"] == nxt]
    px = lambda m, k: float(m.get(k + "_dollars") or (m.get(k) or 0) / 100)
    cand = [m for m in ev if lo <= px(m, "yes_ask") <= hi]
    cand.sort(key=lambda m: abs(px(m, "yes_ask") - 0.5))
    cand = cand[:max_n]
    with (OUT / "books.jsonl").open("a") as fh:
        for m in cand:
            b = kcall(f"{K}/markets/{m['ticker']}/orderbook")
            row = {"ts": int(time.time()), "series": series, "t": m["ticker"], "close_time": m["close_time"], "floor": m.get("floor_strike"),
                   "yes_ask": px(m, "yes_ask"), "yes_bid": px(m, "yes_bid"), "book": b.get("orderbook_fp") or b.get("orderbook")}
            fh.write(json.dumps(row) + "\n")
            print(series, m["ticker"], row["yes_bid"], row["yes_ask"], flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    n = int(a[0]) if a else 6; lo = float(a[1]) if len(a) > 1 else 0.04; hi = float(a[2]) if len(a) > 2 else 0.96
    for s in ("KXINXU", "KXNASDAQ100U"):
        snap(s, n, lo, hi)
