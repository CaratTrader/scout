"""Counted, cached Kalshi GET for the r5_announced_date_slip researcher, plus readers for data other lab researchers
already paid for (read as DATA only).

kget(path): every live call goes through lab.us.data_refresh.fetch (bot idle window, 429 retries) and is appended to
data/kalshi_lab/strategies/r5_announced_date_slip/calls.log as "unix_ts<TAB>calls_spent<TAB>bytes<TAB>path".
Hard budget 195 calls for the task (cap 200). Responses cached by sha1(path).

cached_markets(): every market dict in any researcher's cached listing response (ticker -> raw JSON).
cached_hist_candles(): ticker -> raw hourly candlesticks from any researcher's cached
  /historical/markets/{t}/candlesticks?...period_interval=60 response (located through their calls.log paths)."""
from __future__ import annotations

import glob
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K  # noqa: E402

OUT = ROOT / "data/kalshi_lab/strategies/r5_announced_date_slip"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 195


def used() -> int:
    if not LOG.exists():
        return 0
    return sum(int(l.split("\t")[1]) for l in LOG.read_text().splitlines() if l.strip())


def cpath(path: str) -> Path:
    return CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")


def kget(path: str, cache: bool = True, tries: int = 3, cache_only: bool = False) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = cpath(path)
    if cache and f.exists():
        return json.loads(f.read_text())
    if cache_only:
        return {"_error": "not cached"}
    if used() + tries > BUDGET:
        raise RuntimeError(f"Kalshi call budget exhausted ({used()})")
    txt = fetch(K + path, pace=1.15, tries=tries)
    with LOG.open("a") as g:
        g.write(f"{int(time.time())}\t{1 if txt else tries}\t{len(txt)}\t{path[:400]}\n")
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
                    if prev is None or (m.get("result") in ("yes", "no") and prev.get("result") not in ("yes", "no")):
                        mk[m["ticker"]] = m
    return mk


def cached_hist_candles(prefixes: tuple[str, ...]) -> dict[str, list[dict]]:
    """ticker -> raw candlesticks list, from other researchers' cached /historical/markets/{t}/candlesticks responses
    (hourly only). Their cache file names are sha1(path) or sha1(url)[:24]; both are tried."""
    out: dict[str, list[dict]] = {}
    for logf in glob.glob(str(ROOT / "data/kalshi_lab/strategies/**/calls.log"), recursive=True):
        d = Path(logf).parent
        cdir = d / "api_cache"
        if not cdir.exists():
            continue
        for line in Path(logf).read_text().splitlines():
            m = re.search(r"(/historical/markets/([^/\s]+)/candlesticks\?\S+)", line)
            if not m or "period_interval=60" not in m.group(1):
                continue
            path, t = m.group(1), m.group(2)
            if not t.startswith(prefixes):
                continue
            for name in (hashlib.sha1(path.encode()).hexdigest() + ".json",
                         hashlib.sha1((K + path).encode()).hexdigest()[:24] + ".json",
                         hashlib.sha1((K + path).encode()).hexdigest() + ".json"):
                f = cdir / name
                if f.exists():
                    try:
                        cs = json.loads(f.read_text()).get("candlesticks") or []
                    except Exception:
                        continue
                    if len(cs) > len(out.get(t, [])):
                        out[t] = cs
                    break
    return out


if __name__ == "__main__":
    p = sys.argv[1]
    d = kget(p)
    print(json.dumps(d)[: int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
    print("calls used", used())
