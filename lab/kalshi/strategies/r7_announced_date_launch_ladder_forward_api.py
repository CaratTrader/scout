"""Counted Kalshi GET for r7_announced_date_launch_ladder_forward (forward-only shadow; it never places orders).

Every live call goes through lab.us.data_refresh.fetch (waits for the temperature bot's idle window, retries 429s,
pace 1.15 s) and is appended to <OUT>/calls.log (ISO time, bytes, url). A Budget object enforces the per-pass cap
(10 calls by default; an environment variable or flag may only lower it). Responses are not cached: a forward logger
must read the live book. R7_OUT overrides the output directory (the offline selftest uses a scratch directory).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ.get("R7_OUT", str(ROOT / "data/kalshi_lab/strategies/r7_announced_date_launch_ladder_forward")))
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
HARD_CAP = 10


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    """At most `cap` live Kalshi calls in one pass (cap <= HARD_CAP)."""

    def __init__(self, cap: int = HARD_CAP, out: Path = OUT, fetcher=None):
        self.cap = max(0, min(int(cap), HARD_CAP))
        self.used = 0
        self.out = Path(out)
        self.fetcher = fetcher          # offline selftest injects a fake; None = live fetch

    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(f"pass cap {self.cap} reached")
        url = path if path.startswith("http") else KBASE + path
        self.used += 1
        if self.fetcher is not None:
            txt = self.fetcher(url)
        else:
            from lab.us.data_refresh import fetch
            txt = fetch(url, pace=1.15)
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.out / "calls.log", "a") as lg:
            lg.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{len(txt or '')}\t{url}\n")
        try:
            return json.loads(txt) if txt else {"_error": "empty response"}
        except Exception:  # noqa: BLE001
            return {"_error": (txt or "")[:300]}


def calls_logged(out: Path = OUT) -> int:
    f = Path(out) / "calls.log"
    return sum(1 for _ in open(f)) if f.exists() else 0
