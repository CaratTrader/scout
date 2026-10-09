"""Counted, cached Kalshi GET for the r3_listing_spread_yes_premium_scan researcher.

Every Kalshi request goes through lab.us.data_refresh.fetch(url, pace=1.15) (waits for the paper bot's idle window,
retries 429s). Each call is logged to data/kalshi_lab/strategies/r3_listing_spread_yes_premium_scan/calls.log and the
response is cached under api_cache/, so re-running the analysis never re-hits the API. Hard budget: 200 calls."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r3_listing_spread_yes_premium_scan")
CACHE = OUT / "api_cache"
LOG = OUT / "calls.log"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str, cache_only: bool = False) -> dict:
    """GET K + path (path starts with '/'). Returns {} on failure (empty body)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = CACHE / (hashlib.sha1(path.encode()).hexdigest()[:24] + ".json")
    if cf.exists():
        return json.loads(cf.read_text())
    if cache_only:
        return {}
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15)
    with LOG.open("a") as f:
        f.write(f"{int(time.time())}\t{len(txt)}\t{path[:300]}\n")
    if not txt:
        return {}
    try:
        d = json.loads(txt)
    except Exception:
        return {}
    cf.write_text(txt)
    return d
