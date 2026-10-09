"""Counted, cached GETs for the r4_official_posting_lag_study researcher.
Kalshi: every call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is
logged to data/kalshi_lab/strategies/r4_official_posting_lag_study/calls.log. Hard budget 190 calls (task cap 200).
Official sources (senate.gov, clerk.house.gov, supremecourt.gov, Wayback CDX): plain urllib, polite pacing, cached.
Responses are cached on disk by URL, so a re-run never spends budget twice."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K

OUT = ROOT / "data/kalshi_lab/strategies/r4_official_posting_lag_study"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
WEB = OUT / "web_cache"
BUDGET = 190


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str, cache: bool = True) -> dict:
    """GET K+path (path starts with '/'). Cached; counted; raises when the budget is exhausted."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15, tries=3)
    with LOG.open("a") as g:
        g.write(f"{int(time.time())}\t{len(txt)}\t{path[:240]}\n")
    if not txt:
        return {"_error": "empty"}
    try:
        d = json.loads(txt)
    except Exception:
        return {"_error": txt[:300]}
    if cache:
        f.write_text(txt)
    return d


def wget(url: str, pace: float = 1.0, want_headers: bool = False) -> tuple[str, dict]:
    """Polite cached GET of a public non-Kalshi page. Returns (text, headers)."""
    WEB.mkdir(parents=True, exist_ok=True)
    f = WEB / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if f.exists():
        d = json.loads(f.read_text()); return d["text"], d["headers"]
    txt, hdr = "", {}
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; scout)"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read(); hdr = dict(r.headers.items())
                txt = raw.decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            hdr = {"_status": e.code}; time.sleep(2 * (i + 1))
            if e.code in (403, 404, 405):
                break
        except Exception as e:
            hdr = {"_error": str(e)[:100]}; time.sleep(3 * (i + 1))
    time.sleep(pace)
    f.write_text(json.dumps({"url": url, "text": txt, "headers": hdr}))
    return txt, hdr


if __name__ == "__main__":
    p = sys.argv[1]
    d = kget(p, cache=True)
    print(json.dumps(d)[: int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
    print("calls used", used())
