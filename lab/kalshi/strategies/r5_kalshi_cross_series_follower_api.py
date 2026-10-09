"""Counted, cached Kalshi GET for r5_kalshi_cross_series_follower.

Every network call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is
logged to data/kalshi_lab/strategies/r5_kalshi_cross_series_follower/calls.log. Hard budget 195 calls (task cap 200).
Before calling, the path is looked up in this family's cache and then in every other lab researcher's api_cache
(read-only index from r4_nested_deadline_term_structure_cache), so responses already on disk are never fetched twice."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K
from lab.kalshi.strategies.r4_nested_deadline_term_structure_cache import index, load

OUT = ROOT / "data/kalshi_lab/strategies/r5_kalshi_cross_series_follower"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 195
_IX: dict | None = None


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def cached(path: str) -> dict | None:
    global _IX
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if _IX is None:
        _IX = index()
    if path in _IX:
        return load(_IX[path])
    return None


def kget(path: str, cache_only: bool = False) -> dict:
    """GET K+path (path starts with '/'). Cached; counted; raises when the budget is exhausted."""
    d = cached(path)
    if d is not None:
        return d
    if cache_only:
        return {"_error": "not cached"}
    OUT.mkdir(parents=True, exist_ok=True); CACHE.mkdir(parents=True, exist_ok=True)
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15, tries=3)
    with LOG.open("a") as g:   # an empty reply means fetch() burned all 3 tries: count them all
        for _ in range(1 if txt else 3):
            g.write(f"{int(time.time())}\t{len(txt)}\t{path[:300]}\n")
    if not txt:
        return {"_error": "empty"}
    try:
        d = json.loads(txt)
    except Exception:
        return {"_error": txt[:300]}
    (CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")).write_text(txt)
    return d


def cached_paths(prefix: str = "") -> list[str]:
    global _IX
    if _IX is None:
        _IX = index()
    own = []
    if LOG.exists():
        own = [l.split("\t")[-1] for l in LOG.read_text().splitlines()]
    return sorted({p for p in list(_IX) + own if p.startswith(prefix)})


if __name__ == "__main__":
    p = sys.argv[1]
    d = kget(p)
    print(json.dumps(d)[: int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
    print("calls used", used())
