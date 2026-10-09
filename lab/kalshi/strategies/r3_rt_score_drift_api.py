"""r3_rt_score_drift: cached, budget-counted fetchers.
Kalshi: every call goes through lab.us.data_refresh.fetch(url, pace=1.15), counted in
data/kalshi_lab/strategies/r3_rt_score_drift/calls.json (hard budget 200). Wayback Machine: polite (>= 1.5 s apart),
cached under .../wb/. Re-runs cost nothing."""
from __future__ import annotations
import datetime as dt, gzip, hashlib, json, sys, time, urllib.request, urllib.error, zlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r3_rt_score_drift"); RAW = OUT / "raw"; WB = OUT / "wb"; CALLS = OUT / "calls.json"
BUDGET = 200
RAW.mkdir(parents=True, exist_ok=True); WB.mkdir(parents=True, exist_ok=True)


def _key(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:20]


def calls_used() -> int:
    return json.loads(CALLS.read_text())["n"] if CALLS.exists() else 0


def kalshi(path: str, refresh: bool = False) -> dict:
    url = path if path.startswith("http") else K + path
    f = RAW / (_key(url) + ".json")
    if f.exists() and not refresh:
        return json.loads(f.read_text() or "{}")
    c = json.loads(CALLS.read_text()) if CALLS.exists() else {"n": 0, "log": []}
    if c["n"] >= BUDGET:
        raise RuntimeError("Kalshi budget exhausted")
    txt = fetch(url, pace=1.15)
    c["n"] += 1; c["log"].append([int(time.time()), url]); CALLS.write_text(json.dumps(c))
    f.write_text(txt or "{}")
    return json.loads(txt or "{}")


def iso(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


_last = [0.0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def wb_get(url: str, follow: bool = True, pace: float = 1.5, tries: int = 4, strip: bool = False) -> tuple[int, str, dict]:
    """GET from web.archive.org with caching. Returns (status, body, headers). Cached by URL (+follow flag)."""
    f = WB / (_key(url + str(follow)) + ".json")
    if f.exists():
        d = json.loads(f.read_text()); return d["s"], d["b"], d["h"]
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    for i in range(tries):
        w = pace - (time.time() - _last[0])
        if w > 0:
            time.sleep(w)
        _last[0] = time.time()
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "research-script (rt score history)"}), timeout=90) as r:
                raw = r.read(); s, h = r.status, dict(r.headers); h["final_url"] = r.geturl()
                if raw[:2] == b"\x1f\x8b":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
                body = raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            s, h = e.code, dict(e.headers or {}); body = ""
            if s in (429, 503, 504, 502, 520, 522):
                time.sleep(10 * (i + 1)); continue
        except Exception as e:
            print("wb retry", i, str(e)[:80], flush=True); time.sleep(45 * (i + 1)); continue
        if strip:     # keep only the score JSON fragments of an RT page (the full page is ~200 KB)
            import re as _re
            keep = [m.group(0) for m in _re.finditer(r'"criticsScore":\{[^{}]*\}|"aggregateRating":\{[^{}]*\}', body)]
            body = " ".join(keep)
        f.write_text(json.dumps({"s": s, "b": body, "h": {k: v for k, v in h.items() if k.lower() in ("location", "x-archive-redirect-reason", "memento-datetime", "final_url")}}))
        return s, body, h
    return 0, "", {}
