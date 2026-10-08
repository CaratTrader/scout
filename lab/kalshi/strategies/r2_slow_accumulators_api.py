"""Counted, cached Kalshi GET for the r2_slow_accumulators researcher. Every call goes through
lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is logged to calls.log.
Hard budget: 200 calls for the whole task. Responses are cached on disk by URL so re-runs never spend budget twice.
Also: polite cached GET for free non-Kalshi sources (ACIS, IEM)."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r2_slow_accumulators")
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str, cache_only: bool = False) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if cache_only:
        return {"_error": "not cached"}
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15)
    with LOG.open("a") as g:
        for _ in range(1 if txt else 6):
            g.write(f"{int(time.time())}\t{len(txt)}\t{path[:240]}\n")
    if not txt:
        return {"_error": "empty"}
    d = json.loads(txt)
    f.write_text(txt)
    return d


def oget(url: str, sub: str = "ext", pace: float = 2.0, post: bytes | None = None) -> str:
    """Cached polite GET/POST for free public sources (no keys)."""
    d = OUT / sub; d.mkdir(parents=True, exist_ok=True)
    f = d / (hashlib.sha1((url + (post.decode() if post else "")).encode()).hexdigest() + ".txt")
    if f.exists():
        return f.read_text()
    for i in range(4):
        try:
            req = urllib.request.Request(url, data=post, headers={"User-Agent": "scout-research (personal, polite)", "Content-Type": "application/json"} if post else {"User-Agent": "scout-research (personal, polite)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read().decode()
            f.write_text(txt); time.sleep(pace); return txt
        except Exception as e:
            print("oget retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return ""
