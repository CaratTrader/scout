"""Counted, cached Kalshi GET for r6_near_deadline_nothing_happens_census.

Every live call goes through lab.us.data_refresh.fetch (bot-idle window, 429 retries, pace 1.15 s) and is appended to
data/kalshi_lab/strategies/r6_near_deadline_nothing_happens_census/calls.log. Responses are cached by sha1(full url).
Before any live call, responses already cached on disk by earlier researchers are looked up (0 calls) through
r4_nested_deadline_term_structure_cache.index(). Hard cap: 195 live calls for the whole task (task budget 200)."""
from __future__ import annotations

import hashlib
import json
import os
import time

from lab.us.data_refresh import K, fetch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/r6_near_deadline_nothing_happens_census")
CACHE = os.path.join(OUT, "api_cache")
LOG = os.path.join(OUT, "calls.log")
CAP = 195
_IX = None


def calls_used() -> int:
    return sum(1 for _ in open(LOG)) if os.path.exists(LOG) else 0


def _other(path: str):
    global _IX
    if _IX is None:
        from lab.kalshi.strategies.r4_nested_deadline_term_structure_cache import index
        _IX = index()
    f = _IX.get(path)
    if f is None:
        return None
    from lab.kalshi.strategies.r4_nested_deadline_term_structure_cache import load
    return load(f)


def get(path: str, reuse: bool = True) -> dict:
    url = path if path.startswith("http") else K + path
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest()[:24] + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    if reuse and not path.startswith("http"):
        d = _other(path)
        if d:
            return d
    if calls_used() >= CAP:
        raise RuntimeError("r6 call cap reached")
    txt = fetch(url, pace=1.15)
    with open(LOG, "a") as lg:
        lg.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{len(txt or '')}\t{url}\n")
    try:
        d = json.loads(txt) if txt else {}
    except Exception:
        d = {"_error": (txt or "")[:300]}
    if txt and "_error" not in d:
        json.dump(d, open(f, "w"))
    return d
