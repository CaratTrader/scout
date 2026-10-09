"""Counted, cached GETs for the r5_scheduled_release_poller researcher.

Kalshi: every call goes through lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is
logged to data/kalshi_lab/strategies/r5_scheduled_release_poller/calls.log (one line per fetch() invocation; fetch may
retry a 429 internally). Hard budget 195 calls for the research phase (task cap 200). The forward logger has its own
per-pass cap (10) and log (forward_calls.log), so research and forward use are reported separately.
Outside sources (billboard.com WordPress REST API, clerk.house.gov, supremecourt.gov): plain urllib, polite pacing,
cached on disk by URL so a re-run never repeats a request."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.error, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.us.data_refresh import fetch, K  # noqa: E402

OUT = ROOT / "data/kalshi_lab/strategies/r5_scheduled_release_poller"
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
WEB = OUT / "web_cache"
BUDGET = 195
UA = "Mozilla/5.0 (research; scout r5; standard-library urllib)"


def used(log: Path = LOG) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def kget(path: str, cache: bool = True, log: Path = LOG, budget: int = BUDGET) -> dict:
    """GET K+path (path starts with '/'). Cached when cache=True; counted; raises when the budget is exhausted."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    if used(log) >= budget:
        raise RuntimeError(f"Kalshi call budget exhausted ({log.name})")
    t0 = time.time()
    txt = fetch(K + path, pace=1.15, tries=3)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as g:
        g.write(f"{int(t0)}\t{len(txt)}\t{path[:240]}\n")
    if not txt:
        return {"_error": "empty"}
    try:
        d = json.loads(txt)
    except Exception:
        return {"_error": txt[:300]}
    if cache:
        f.write_text(txt)
    return d


def wget(url: str, pace: float = 1.2, cache: bool = True, timeout: int = 40) -> tuple[int, str, dict]:
    """Polite GET of a public non-Kalshi page. Returns (status, text, headers). Cached when cache=True."""
    WEB.mkdir(parents=True, exist_ok=True)
    f = WEB / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache and f.exists():
        d = json.loads(f.read_text())
        return d["status"], d["text"], d["headers"]
    status, txt, hdr = -1, "", {}
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status; hdr = dict(r.headers.items()); txt = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            status = e.code; hdr = dict(e.headers.items()) if e.headers else {}
            txt = ""
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(5 * (i + 1)); continue
            break
        except Exception as e:  # network
            status = -1; txt = str(e)[:200]; time.sleep(4 * (i + 1))
    time.sleep(pace)
    if cache and status in (200, 404):
        f.write_text(json.dumps({"url": url, "status": status, "text": txt, "headers": hdr, "fetched": int(time.time())}))
    return status, txt, hdr
