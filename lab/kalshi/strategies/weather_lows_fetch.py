"""weather_lows data helper: cached, counted Kalshi GETs (shared ~1 req/s IP limit, budget 200 calls for the task).
Every response is cached under data/kalshi_lab/strategies/weather_lows/raw/ keyed by URL hash, so re-runs cost nothing.
Usage (library): from lab.kalshi.strategies.weather_lows_fetch import kget, calls_used"""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/weather_lows"); RAW = OUT / "raw"; CNT = OUT / "kalshi_calls.txt"
BUDGET = 200


def calls_used() -> int:
    return int(CNT.read_text()) if CNT.exists() else 0


def kget(path: str, cache: bool = True) -> dict:
    """GET K+path, cached by URL. Refuses to exceed the call budget."""
    RAW.mkdir(parents=True, exist_ok=True)
    url = K + path; f = RAW / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    n = calls_used()
    if n >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    CNT.write_text(str(n + 1))
    txt = fetch(url, pace=1.15)
    d = json.loads(txt) if txt else {}
    if d and cache:
        f.write_text(json.dumps({"_url": url, **d}))
    return d


if __name__ == "__main__":
    print(json.dumps(kget(sys.argv[1]), indent=1)[:4000]); print("calls used", calls_used())
