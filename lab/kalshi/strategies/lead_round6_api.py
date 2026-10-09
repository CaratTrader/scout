"""Counted, cached Kalshi GET for the round-6 lead probe. Every live call goes through lab.us.data_refresh.fetch
(bot-idle window, 429 retries) and is appended to data/kalshi_lab/strategies/lead_round6/calls.log.
Hard cap: 20 live calls for the whole lead task (task budget 200)."""
from __future__ import annotations

import hashlib
import json
import os
import time

from lab.us.data_refresh import K, fetch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/lead_round6")
CACHE = os.path.join(OUT, "api_cache")
LOG = os.path.join(OUT, "calls.log")
CAP = 20


def calls_used() -> int:
    return sum(1 for _ in open(LOG)) if os.path.exists(LOG) else 0


def get(path: str) -> dict:
    url = path if path.startswith("http") else K + path
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest()[:24] + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    if calls_used() >= CAP:
        raise RuntimeError("lead probe call cap reached")
    txt = fetch(url, pace=1.15)
    with open(LOG, "a") as lg:
        lg.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {len(txt)} {url}\n")
    d = json.loads(txt) if txt else {}
    if txt:
        json.dump(d, open(f, "w"))
    return d
