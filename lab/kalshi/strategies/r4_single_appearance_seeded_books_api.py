"""Counted, cached Kalshi GET for r4_single_appearance_seeded_books (research) plus the shared call log.

Every Kalshi request goes through lab.us.data_refresh.fetch(url, pace=1.15): it waits for the temperature paper bot's
idle window and retries 429s. Each call is appended to data/kalshi_lab/strategies/r4_single_appearance_seeded_books/
calls.log as "unix_ts<TAB>bytes<TAB>tag<TAB>path" (tag 'research' or 'logger'), so the task budget (200 calls in all,
research plus the forward logger's test passes) can be audited. Research responses are cached under api_cache/ and
never re-fetched; the forward logger uses live_get() (no cache)."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r4_single_appearance_seeded_books")
CACHE = OUT / "api_cache"
LOG = OUT / "calls.log"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def _log(t0: float, n: int, tag: str, path: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{int(t0)}\t{n}\t{tag}\t{path[:300]}\n")


def cache_file(path: str) -> Path:
    return CACHE / (hashlib.sha1(path.encode()).hexdigest()[:24] + ".json")


def kget(path: str, cache_only: bool = False) -> dict:
    """Research GET of K + path (path starts with '/'), cached. Returns {} on failure (empty body / bad JSON)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = cache_file(path)
    if cf.exists():
        return json.loads(cf.read_text())
    if cache_only:
        return {}
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    t0 = time.time()
    txt = fetch(K + path, pace=1.15)
    _log(t0, len(txt), "research", path)
    if not txt:
        return {}
    try:
        d = json.loads(txt)
    except ValueError:
        return {}
    cf.write_text(txt)
    return d


def live_get(path: str) -> str:
    """Uncached GET for the forward logger (counted in the same log, tag 'logger')."""
    t0 = time.time()
    txt = fetch(K + path, pace=1.15)
    _log(t0, len(txt), "logger", path)
    return txt
