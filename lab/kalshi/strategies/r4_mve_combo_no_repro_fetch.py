"""Recover the market records (result, legs, expiry) of NO-taker combo prints whose /markets?tickers= lookup came back
empty in the original r4_mve_combo_no run. These fills were never analysed, so they are a random hold-out for C1/C3.
Small batches (20 tickers), counted, paced via lab.us.data_refresh.fetch (bot idle window, 429 retries).
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_mve_combo_no_repro_fetch [max_calls]"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K
from lab.kalshi.strategies.r4_mve_combo_no_repro import load, OUT, REC

BATCH = 20


def main(max_calls: int = 20) -> None:
    REC.mkdir(parents=True, exist_ok=True)
    windows, markets, _ = load(rec=True)
    need = sorted({t["ticker"] for tr in windows for t in tr if t["ticker"].startswith("KXMVE") and t["taker_side"] == "no"
                   and 0.005 <= float(t["yes_price_dollars"]) <= 0.995 and t["ticker"] not in markets})
    print("missing NO-print tickers", len(need), flush=True)
    log = (OUT / "calls.log").open("a"); calls = 0
    for i in range(0, len(need), BATCH):
        if calls >= max_calls:
            break
        b = need[i:i + BATCH]
        url = f"{K}/markets?limit=1000&tickers={','.join(b)}"
        txt = fetch(url, pace=1.15); calls += 1
        log.write(f"{int(time.time())}\t{len(b)}\t{len(txt)}\t{url[:200]}\n"); log.flush()
        try:
            x = json.loads(txt)
        except Exception:
            print("batch", i, "bad reply", len(txt), flush=True); continue
        x["_fetched_at"] = time.time()
        (REC / f"lookup_{i:05d}.json").write_text(json.dumps(x))
        print("batch", i, "asked", len(b), "got", len(x.get("markets", [])), flush=True)
    print("calls", calls, flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
