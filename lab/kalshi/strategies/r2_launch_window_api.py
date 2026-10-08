"""Counted, cached Kalshi GET for the r2_launch_window researcher.
Every call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is logged
to data/kalshi_lab/strategies/r2_launch_window/calls.log. Hard budget 195 calls for the task (task cap 200).
Responses are cached on disk by URL path, so a re-run never spends budget twice."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K

OUT = ROOT / "data/kalshi_lab/strategies/r2_launch_window"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 195


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str, cache: bool = True) -> dict:
    """GET K+path (path starts with '/'). Cached; counted; raises when the budget is exhausted."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15)
    with LOG.open("a") as g:   # an empty reply means fetch() burned its hidden retries: count them all
        for _ in range(1 if txt else 6):
            g.write(f"{int(time.time())}\t{len(txt)}\t{path[:240]}\n")
    if not txt:
        return {"_error": "empty"}
    d = json.loads(txt)
    if cache:
        f.write_text(txt)
    return d
