"""Counted Kalshi GET for r3_seasonal_regime_monitors (research runs and the forward logger).
Every call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s, pace 1.15 s)
and is appended to calls.log. A per-process cap protects the logger's <= 10 calls per invocation; the research task
budget (200) is checked against calls.log entries tagged 'research'."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r3_seasonal_regime_monitors")
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
RESEARCH_BUDGET = 200
_n = 0
CAP = 10**9


def used(tag: str | None = None) -> int:
    if not LOG.exists():
        return 0
    return sum(1 for l in LOG.read_text().splitlines() if tag is None or l.split("\t")[1] == tag)


def set_cap(n: int) -> None:
    global CAP, _n
    CAP, _n = n, 0


def calls_this_process() -> int:
    return _n


def kget(path: str, tag: str = "logger", cache: bool = False) -> dict:
    """GET K + path -> dict ({'_error': ...} on failure). cache=True only for immutable research data."""
    global _n
    OUT.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    if _n >= CAP:
        return {"_error": "per-invocation cap reached"}
    if tag == "research" and used("research") >= RESEARCH_BUDGET:
        raise RuntimeError("research call budget exhausted")
    _n += 1
    txt = fetch(K + path, pace=1.15)
    with LOG.open("a") as g:
        g.write(f"{int(time.time())}\t{tag}\t{len(txt)}\t{path[:300]}\n")
    if not txt:
        return {"_error": "empty"}
    try:
        d = json.loads(txt)
    except ValueError:
        return {"_error": "bad json"}
    if cache:
        CACHE.mkdir(parents=True, exist_ok=True); f.write_text(txt)
    return d
