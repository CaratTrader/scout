"""Counted Kalshi fetch for the rain_history researcher. Every call goes through lab.us.data_refresh.fetch (which waits
for the paper bot's idle window and retries 429s) and is logged to calls.log; hard budget 200 calls for the task.
Responses are cached on disk by URL so a re-run never spends budget twice."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/rain_history")
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str) -> dict:
    """GET K+path (path starts with '/'). Cached; counted; raises when the budget is exhausted."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15)
    with LOG.open("a") as g:   # an empty reply means fetch() burned its 6 hidden attempts: log all of them
        for _ in range(1 if txt else 6):
            g.write(f"{int(time.time())}\t{len(txt)}\t{path[:220]}\n")
    if not txt:
        return {"_error": "empty"}
    d = json.loads(txt)
    f.write_text(txt)
    return d
