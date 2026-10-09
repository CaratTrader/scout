"""Counted, cached Kalshi GET for the r4_mve_combo_no researcher.
Every call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is logged
to data/kalshi_lab/strategies/r4_mve_combo_no/calls.log. Hard budget 190 calls (task cap 200).
Responses are cached on disk by URL path, so a re-run never spends budget twice."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K

OUT = ROOT / "data/kalshi_lab/strategies/r4_mve_combo_no"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
BUDGET = 190


def used() -> int:
    if not LOG.exists():
        return 0
    return sum(int(l.split("\t")[1]) for l in LOG.read_text().splitlines() if l.strip())


def kget(path: str, cache: bool = True, tries: int = 2) -> dict:
    """GET K+path (path starts with '/'). Cached; counted (an empty reply counts every try); raises at the budget."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    if used() + tries > BUDGET:
        raise RuntimeError(f"Kalshi call budget exhausted ({used()})")
    txt = fetch(K + path, pace=1.15, tries=tries)
    with LOG.open("a") as g:
        g.write(f"{int(time.time())}\t{1 if txt else tries}\t{len(txt)}\t{path[:240]}\n")
    if not txt:
        return {"_error": "empty"}
    try:
        d = json.loads(txt)
    except Exception:
        return {"_error": txt[:300]}
    if cache:
        f.write_text(txt)
    return d


if __name__ == "__main__":
    p = sys.argv[1]
    d = kget(p, cache=True)
    print(json.dumps(d)[: int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
    print("calls used", used())
