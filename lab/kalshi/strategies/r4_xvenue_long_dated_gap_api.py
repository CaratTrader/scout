"""Counted, cached Kalshi GET for r4_xvenue_long_dated_gap.
Every network call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is
logged to data/kalshi_lab/strategies/r4_xvenue_long_dated_gap/calls.log. Hard budget 195 calls (task cap 200).
Earlier researchers' raw response caches (r3_long_dated_longshot_no and its repro) are read as DATA only (same
sha1-of-URL naming), so a response somebody already paid for is never fetched again."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K

OUT = ROOT / "data/kalshi_lab/strategies/r4_xvenue_long_dated_gap"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
R3 = ROOT / "data/kalshi_lab/strategies/r3_long_dated_longshot_no"
OTHER = [R3 / "api_cache", R3 / "repro/api_cache"]
BUDGET = 195


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def _files(path: str) -> list[Path]:
    out = []
    for cand in (K + path, path):
        h = hashlib.sha1(cand.encode()).hexdigest()
        out += [CACHE / f"{h}.json"] + [d / f"{h}.json" for d in OTHER]
    return out


def cached(path: str):
    for f in _files(path):
        if f.exists():
            try:
                return json.loads(f.read_text())
            except Exception:
                pass
    return None


def kget(path: str, allow_fetch: bool = True) -> dict | None:
    """GET K+path (path starts with '/'). Served from any cache first; else counted fetch (raises past the budget)."""
    d = cached(path)
    if d is not None or not allow_fetch:
        return d
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    CACHE.mkdir(parents=True, exist_ok=True)
    txt = fetch(K + path, pace=1.15, tries=2)
    with LOG.open("a") as g:
        g.write(f"{int(time.time())}\t{len(txt)}\t{path[:300]}\n")
    if not txt:
        return None
    try:
        d = json.loads(txt)
    except Exception:
        return {"_error": txt[:300]}
    (CACHE / (hashlib.sha1((K + path).encode()).hexdigest() + ".json")).write_text(txt)
    return d


if __name__ == "__main__":
    d = kget(sys.argv[1])
    print(json.dumps(d)[: int(sys.argv[2]) if len(sys.argv) > 2 else 2000])
    print("calls used", used())
