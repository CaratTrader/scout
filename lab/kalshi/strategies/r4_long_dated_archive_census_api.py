"""Counted, cached Kalshi GET for the r4_long_dated_archive_census researcher.

Every call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is logged
to data/kalshi_lab/strategies/r4_long_dated_archive_census/calls.log as "unix_ts<TAB>calls_spent<TAB>bytes<TAB>path".
Hard budget 190 calls for the whole task (task cap 200; the forward logger keeps its own log, see _logger.py).
Responses are cached on disk by sha1(path), so a re-run never spends budget twice.

cached_markets() reads, as DATA only, every market listing any lab researcher has already cached on disk
(data/kalshi_lab/strategies/**/api_cache/*.json, content-indexed): listings already paid for cost nothing here."""
from __future__ import annotations

import glob
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K  # noqa: E402

OUT = ROOT / "data/kalshi_lab/strategies/r4_long_dated_archive_census"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 190


def used(log: Path = LOG) -> int:
    if not log.exists():
        return 0
    return sum(int(l.split("\t")[1]) for l in log.read_text().splitlines() if l.strip())


def cpath(path: str) -> Path:
    return CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")


def kget(path: str, cache: bool = True, tries: int = 2, budget: int = BUDGET, log: Path = LOG) -> dict:
    """GET K+path (path starts with '/'). Cached; counted (an empty reply counts every try); raises at the budget."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = cpath(path)
    if cache and f.exists():
        return json.loads(f.read_text())
    if used(log) + tries > budget:
        raise RuntimeError(f"Kalshi call budget exhausted ({used(log)})")
    txt = fetch(K + path, pace=1.15, tries=tries)
    with log.open("a") as g:
        g.write(f"{int(time.time())}\t{1 if txt else tries}\t{len(txt)}\t{path[:300]}\n")
    if not txt:
        return {"_error": "empty"}
    try:
        d = json.loads(txt)
    except Exception:
        return {"_error": txt[:300]}
    if cache:
        f.write_text(txt)
    return d


def cached_markets() -> dict[str, dict]:
    """Every market dict found in any researcher's cached listing responses (ticker -> raw market JSON)."""
    mk: dict[str, dict] = {}
    for f in sorted(glob.glob(str(ROOT / "data/kalshi_lab/strategies/**/api_cache/*.json"), recursive=True)):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("markets"), list):
            for m in d["markets"]:
                if isinstance(m, dict) and "ticker" in m and "event_ticker" in m and "open_time" in m and "candlesticks" not in m:
                    prev = mk.get(m["ticker"])
                    # keep the most final record (settled beats open)
                    if prev is None or (m.get("result") in ("yes", "no") and prev.get("result") not in ("yes", "no")):
                        mk[m["ticker"]] = m
    return mk


def cached_series_volume() -> dict[str, float]:
    """Series ticker -> cumulative volume from any cached /series?category=..&include_volume=true response."""
    vol: dict[str, float] = {}
    for f in sorted(glob.glob(str(ROOT / "data/kalshi_lab/strategies/**/api_cache/*.json"), recursive=True)):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("series"), list) and d["series"] and "volume_fp" in d["series"][0]:
            for s in d["series"]:
                vol[s["ticker"]] = max(vol.get(s["ticker"], 0.0), float(s.get("volume_fp") or 0))
    return vol


if __name__ == "__main__":
    p = sys.argv[1]
    d = kget(p, cache=True)
    print(json.dumps(d)[: int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
    print("calls used", used())
