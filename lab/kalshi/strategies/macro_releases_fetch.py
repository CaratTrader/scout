"""macro_releases: cached Kalshi fetches with a hard call budget (counted in raw/calls.json)."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/macro_releases"); RAW = OUT / "raw"; RAW.mkdir(parents=True, exist_ok=True)
CALLS = RAW / "calls.json"; BUDGET = 200


def kget(path: str) -> dict:
    key = hashlib.sha1(path.encode()).hexdigest()[:16]; f = RAW / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text())
    n = json.loads(CALLS.read_text())["n"] if CALLS.exists() else 0
    if n >= BUDGET:
        raise SystemExit(f"Kalshi budget exhausted ({n})")
    txt = fetch(K + path, pace=1.15)
    CALLS.write_text(json.dumps({"n": n + 1}))
    d = json.loads(txt) if txt else {}
    if txt:
        f.write_text(json.dumps({"path": path, "d": d}))
    return {"path": path, "d": d}


def list_markets(series: str, hist: bool = True, recent: bool = True, min_close: int = 0) -> list[dict]:
    out = {}
    paths = []
    if hist:
        paths.append(f"/historical/markets?series_ticker={series}&limit=1000")
    if recent:
        paths.append(f"/markets?series_ticker={series}&status=settled&limit=1000" + (f"&min_close_ts={min_close}" if min_close else ""))
    for p in paths:
        cur = ""
        while True:
            r = kget(p + (f"&cursor={cur}" if cur else ""))["d"]
            for m in r.get("markets", []):
                out[m["ticker"]] = m
            cur = r.get("cursor") or ""
            if not cur or not r.get("markets"):
                break
    return list(out.values())


if __name__ == "__main__":
    for s in sys.argv[1:]:
        ms = list_markets(s)
        print(s, len(ms))
    print("calls", json.loads(CALLS.read_text())["n"] if CALLS.exists() else 0)
