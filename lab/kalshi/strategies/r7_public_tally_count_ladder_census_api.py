"""Counted, cached Kalshi GET for r7_public_tally_count_ladder_census.

Every live call goes through lab.us.data_refresh.fetch (bot-idle window, 429 retries, pace 1.15 s) and is appended to
data/kalshi_lab/strategies/r7_public_tally_count_ladder_census/calls.log. Before a live call, the response is looked up
in the api_cache folders of earlier rounds (same URL -> same sha1 key), so data already fetched costs nothing.
Hard cap: 200 live calls for the whole task (census <= 25, candles <= 120, logger tests <= 20)."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from pathlib import Path

from lab.us.data_refresh import K, fetch

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r7_public_tally_count_ladder_census"
CACHE = OUT / "api_cache"
LOG = OUT / "calls.log"
CAP = 200
OTHER_CACHES = sorted(glob.glob(str(ROOT / "data/kalshi_lab/strategies/*/api_cache")))


def calls_used(tag: str | None = None) -> int:
    if not LOG.exists():
        return 0
    return sum(1 for ln in LOG.open() if tag is None or f" [{tag}] " in ln)


def _keys(url: str) -> list[str]:
    path = url[len(K):] if url.startswith(K) else url
    out = []
    for s in (url, path):
        h = hashlib.sha1(s.encode()).hexdigest()
        out += [h[:24] + ".json", h + ".json"]
    return out


def cached(url: str) -> dict | None:
    for d in [str(CACHE)] + OTHER_CACHES:
        for k in _keys(url):
            f = os.path.join(d, k)
            if os.path.exists(f):
                try:
                    return json.load(open(f))
                except (OSError, ValueError):
                    pass
    return None


def get(path: str, tag: str = "misc", reuse: bool = True, cap: int | None = None) -> dict:
    """GET K+path (or a full URL). reuse=False forces a live call (used for live open-market passes)."""
    url = path if path.startswith("http") else K + path
    if reuse:
        d = cached(url)
        if d is not None:
            return d
    if calls_used() >= CAP:
        raise RuntimeError("r7 call cap (200) reached")
    if cap is not None and calls_used(tag) >= cap:
        raise RuntimeError(f"r7 call cap for [{tag}] ({cap}) reached")
    OUT.mkdir(parents=True, exist_ok=True)
    txt = fetch(url, pace=1.15)
    with LOG.open("a") as lg:
        lg.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{tag}] {len(txt or '')} {url}\n")
    d = json.loads(txt) if txt else {}
    if txt and reuse:
        CACHE.mkdir(parents=True, exist_ok=True)
        json.dump(d, open(CACHE / _keys(url)[0], "w"))
    return d
