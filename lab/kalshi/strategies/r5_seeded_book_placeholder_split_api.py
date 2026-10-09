"""Counted Kalshi GET for r5_seeded_book_placeholder_split (research checks and the forward logger).

Every live request goes through lab.us.data_refresh.fetch(url, pace=1.15) (temperature-bot idle window, 429 retries)
and is appended to data/kalshi_lab/strategies/r5_seeded_book_placeholder_split/calls.log (unix ts, bytes, kind, path).
TASK_CAP = 200 counts every call of the family (research + logger test passes). Research GETs are cached on disk
(api_cache/); logger GETs are live and never cached. A Budget object caps the calls of one logger pass."""
from __future__ import annotations
import hashlib, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path(os.environ.get("R5PS_OUT", "data/kalshi_lab/strategies/r5_seeded_book_placeholder_split"))
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
RESEARCH_CAP = 170


class BudgetExhausted(RuntimeError):
    pass


def used(kind: str | None = None) -> int:
    if not LOG.exists():
        return 0
    rows = [l.split("\t") for l in LOG.read_text().splitlines() if l.strip()]
    return sum(1 for r in rows if kind is None or (len(r) > 2 and r[2] == kind))


def _get(path: str, kind: str) -> str:
    t0 = time.time()
    txt = fetch(K + path, pace=1.15)
    OUT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{int(t0)}\t{len(txt)}\t{kind}\t{path[:300]}\n")
    return txt


def research_get(path: str) -> dict:
    """Cached GET for archive checks (hard cap RESEARCH_CAP live calls)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest()[:24] + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if used("research") >= RESEARCH_CAP:
        raise BudgetExhausted("research cap")
    txt = _get(path, "research")
    try:
        d = json.loads(txt) if txt else {}
    except ValueError:
        d = {}
    if d:
        f.write_text(json.dumps(d))
    return d


class Budget:
    """Hard cap on live (uncached) Kalshi calls in one logger pass."""

    def __init__(self, cap: int) -> None:
        self.cap = cap; self.used = 0; self.log: list[str] = []

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        txt = _get(path, "logger")
        self.log.append(path[:120])
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except ValueError:
            return {}
