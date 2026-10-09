"""Counted Kalshi GET for r4_earnings_seeded_book_48h_forward (research cache + forward-logger budget).

Every request goes through lab.us.data_refresh.fetch(url, pace=1.15) (waits for the temperature paper bot's idle window,
retries 429s). Two users:
  * kget(path)  research calls (backtest on archive events): cached under api_cache/, logged to research_calls.log,
                hard budget RESEARCH_BUDGET for the whole task.
  * Budget(cap) forward logger: never cached (live data), logged to calls.log (unix ts, bytes, path), cap per pass.
total_used() = research + logger calls, so the task's 200-call budget can be audited."""
from __future__ import annotations
import hashlib, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path(os.environ.get("R4E_OUT", "data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward"))
CACHE = OUT / "api_cache"
RLOG = OUT / "research_calls.log"
LOG = OUT / "calls.log"
RESEARCH_BUDGET = 175


class BudgetExhausted(RuntimeError):
    pass


def _n(f: Path) -> int:
    return len(f.read_text().splitlines()) if f.exists() else 0


def research_used() -> int:
    return _n(RLOG)


def total_used() -> int:
    return _n(RLOG) + _n(LOG)


def kget(path: str, cache_only: bool = False) -> dict:
    """Research GET K + path (path starts with '/'); cached; {} on failure."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cf = CACHE / (hashlib.sha1(path.encode()).hexdigest()[:24] + ".json")
    if cf.exists():
        return json.loads(cf.read_text())
    if cache_only:
        return {}
    if research_used() >= RESEARCH_BUDGET:
        raise BudgetExhausted(path)
    t0 = time.time()
    txt = fetch(K + path, pace=1.15)
    with RLOG.open("a") as f:
        f.write(f"{int(t0)}\t{len(txt)}\t{path[:300]}\n")
    if not txt:
        return {}
    try:
        d = json.loads(txt)
    except ValueError:
        return {}
    cf.write_text(txt)
    return d


class Budget:
    """Hard cap on Kalshi calls in one forward-logger pass (live, never cached)."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        t0 = time.time()
        txt = fetch(K + path, pace=1.15)
        OUT.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{int(t0)}\t{len(txt)}\t{path[:300]}\n")
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except ValueError:
            return {}
