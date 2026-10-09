"""Counted Kalshi GET for the r3_mentions_maker_forward logger (live data: never cached).

Every request goes through lab.us.data_refresh.fetch(url, pace=1.15), which waits for the temperature paper bot's idle
window and retries 429s. Each call is appended to data/kalshi_lab/strategies/r3_mentions_maker_forward/calls.log
(unix ts, bytes, path) so the per-pass and total budgets can be audited. A Budget object caps the calls of one pass."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r3_mentions_maker_forward")
LOG = OUT / "calls.log"


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    """Hard cap on Kalshi calls in one pass."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0
        self.log: list[str] = []

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        """GET K + path. Returns {} on failure (empty body or bad JSON); raises BudgetExhausted if the cap is reached."""
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        t0 = time.time()
        txt = fetch(K + path, pace=1.15)
        OUT.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{int(t0)}\t{len(txt)}\t{path[:300]}\n")
        self.log.append(path[:120])
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except ValueError:
            return {}


def total_used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0
