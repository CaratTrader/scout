"""Read-only index of Kalshi responses already cached on disk by earlier lab researchers (no Kalshi calls).

Every researcher's api_cache stores one JSON response per URL path, keyed by sha1/md5 of the path (with or without the
API base). The calls.log next to each cache lists the paths, so the index maps path -> file. Used by
r4_nested_deadline_term_structure_data to avoid re-spending the shared Kalshi budget on listings/candles that exist.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_nested_deadline_term_structure_cache"""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STRAT = ROOT / "data/kalshi_lab/strategies"
KBASE = "https://api.elections.kalshi.com/trade-api/v2"


def _cands(path: str):
    for s in (path, KBASE + path):
        yield hashlib.sha1(s.encode()).hexdigest()
        yield hashlib.md5(s.encode()).hexdigest()


def index() -> dict[str, Path]:
    """path -> cached response file, over every calls.log under data/kalshi_lab/strategies."""
    out: dict[str, Path] = {}
    for log in STRAT.glob("**/calls.log"):
        cache = log.parent / "api_cache"
        if not cache.exists():
            continue
        for line in log.read_text(errors="ignore").splitlines():
            p = line.split("\t")
            path = p[-1] if p else ""
            if not path.startswith("/"):
                if "trade-api/v2" in path:
                    path = path.split("trade-api/v2", 1)[1]
                else:
                    continue
            for h in _cands(path):
                f = cache / f"{h}.json"
                if f.exists():
                    out.setdefault(path, f)
                    break
    return out


def load(f: Path) -> dict:
    try:
        d = json.loads(f.read_text())
    except Exception:
        return {}
    return d.get("d", d) if isinstance(d, dict) and set(d) <= {"d", "ts", "url"} else d


if __name__ == "__main__":
    ix = index()
    print(len(ix), "cached paths")
    kinds = {}
    for p in ix:
        k = p.split("?")[0]
        k = "/historical/markets/{t}/candlesticks" if k.startswith("/historical/markets/") and k.endswith("candlesticks") else k
        k = "/series/{s}/events/{e}/candlesticks" if "/events/" in k and k.endswith("candlesticks") else k
        kinds[k] = kinds.get(k, 0) + 1
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:30]:
        print(f"{v:5d} {k}")
