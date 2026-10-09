"""Counted, cached Kalshi GET for r6_new_series_honeymoon. Every live call goes through lab.us.data_refresh.fetch
(bot-idle window, 429 retries, pace 1.15 s) and is appended to data/kalshi_lab/strategies/r6_new_series_honeymoon/calls.log.
Hard cap: 200 live calls for the whole task. Cached responses are never re-fetched."""
from __future__ import annotations

import hashlib
import json
import os
import time

from lab.us.data_refresh import K, fetch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/r6_new_series_honeymoon")
CACHE = os.path.join(OUT, "api_cache")
LOG = os.path.join(OUT, "calls.log")
CAP = int(os.environ.get("R6H_CAP", "200"))


def calls_used() -> int:
    return sum(1 for _ in open(LOG)) if os.path.exists(LOG) else 0


def cache_path(path: str) -> str:
    url = path if path.startswith("http") else K + path
    return os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest()[:24] + ".json")


def cached(path: str) -> bool:
    return os.path.exists(cache_path(path))


def get(path: str) -> dict:
    url = path if path.startswith("http") else K + path
    os.makedirs(CACHE, exist_ok=True)
    f = cache_path(path)
    if os.path.exists(f):
        return json.load(open(f))
    if calls_used() >= CAP:
        raise RuntimeError("r6_new_series_honeymoon call cap reached")
    txt = fetch(url, pace=1.15)
    with open(LOG, "a") as lg:
        lg.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {len(txt)} {url}\n")
    try:
        d = json.loads(txt) if txt else {}
    except ValueError:
        d = {"_raw": txt[:2000]}
    if txt:
        json.dump(d, open(f, "w"))
    return d
