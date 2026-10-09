"""Counted Kalshi GET for r4_long_dated_maker_no (backtest: cached; logger: live, never cached).

Every request goes through lab.us.data_refresh.fetch(url, pace=1.15) (waits for the temperature paper bot's idle window,
retries 429s). Each call is appended to data/kalshi_lab/strategies/r4_long_dated_maker_no/calls.log
(unix ts, bytes, tag, path) so the task budget (200 calls) and the logger's per-pass cap (10) can be audited."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

ROOT = Path("/Users/roomyhome/Tradeinc")
OUT = ROOT / "data/kalshi_lab/strategies/r4_long_dated_maker_no"
CACHE = OUT / "api_cache"
LOG = OUT / "calls.log"


class BudgetExhausted(RuntimeError):
    pass


def total_used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def _log(t0: float, n: int, tag: str, path: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{int(t0)}\t{n}\t{tag}\t{path[:300]}\n")


def cached(path: str, budget_total: int = 200, tag: str = "backtest") -> dict | None:
    """Backtest GET: cache hit costs nothing; a miss costs one call (refused once the task total reaches budget_total)."""
    h = hashlib.sha1((K + path).encode()).hexdigest()
    f = CACHE / f"{h}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except ValueError:
            pass
    if total_used() >= budget_total:
        raise BudgetExhausted(path)
    t0 = time.time()
    txt = fetch(K + path, pace=1.15, tries=3)
    _log(t0, len(txt), tag, path)
    if not txt:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(txt)
    try:
        return json.loads(txt)
    except ValueError:
        return None


class Budget:
    """Live GET with a hard cap per logger pass (no cache)."""

    def __init__(self, cap: int, tag: str = "logger") -> None:
        self.cap = cap; self.used = 0; self.tag = tag

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        t0 = time.time()
        txt = fetch(K + path, pace=1.15, tries=3)
        _log(t0, len(txt), self.tag, path)
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except ValueError:
            return {}
