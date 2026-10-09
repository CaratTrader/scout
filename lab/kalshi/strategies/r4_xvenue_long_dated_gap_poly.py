"""Polymarket access for r4_xvenue_long_dated_gap (free gamma + CLOB APIs, no key, no Kalshi calls).
All responses are cached on disk under data/kalshi_lab/strategies/r4_xvenue_long_dated_gap/poly_cache (sha1 of URL).

- search(q): gamma /public-search (events, incl. closed), used only to build the frozen pair mapping (metadata only).
- event(slug): gamma /events?slug=... (markets, questions, descriptions, end dates, token ids).
- hourly(token, t0, t1): CLOB /prices-history?market=<token>&startTs&endTs&fidelity=60 in <= 10-day chunks
  (interval=max refuses fidelity < 720 on closed markets, but short startTs/endTs windows return hourly points)."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r4_xvenue_long_dated_gap"
PC = OUT / "poly_cache"
G, C = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
DAY = 86400


def get(url: str, pace: float = 0.6, cache: bool = True):
    PC.mkdir(parents=True, exist_ok=True)
    f = PC / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if cache and f.exists():
        return json.loads(f.read_text())
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research", "Accept": "application/json"}), timeout=60) as r:
                txt = r.read().decode()
            time.sleep(pace)
            if cache:
                f.write_text(txt)
            return json.loads(txt)
        except Exception as e:
            print("poly retry", i, str(e)[:80], url[:120], flush=True); time.sleep(4 * (i + 1))
    return None


def search(q: str, closed: bool = True, n: int = 20) -> list[dict]:
    u = f"{G}/public-search?q={urllib.parse.quote(q)}&limit_per_type={n}" + ("&events_status=closed" if closed else "")
    return (get(u) or {}).get("events", []) or []


def event(slug: str) -> dict | None:
    d = get(f"{G}/events?slug={urllib.parse.quote(slug)}")
    return d[0] if d else None


def hourly(token: str, t0: int, t1: int, chunk: int = 10 * DAY) -> list[tuple[int, float]]:
    out = {}
    a = t0
    while a < t1:
        b = min(a + chunk, t1)
        d = get(f"{C}/prices-history?market={token}&startTs={a}&endTs={b}&fidelity=60", pace=0.35)
        for x in (d or {}).get("history", []) or []:
            out[int(x["t"])] = float(x["p"])
        a = b
    return sorted(out.items())


if __name__ == "__main__":
    for e in search(" ".join(sys.argv[1:])):
        print(e.get("slug"), e.get("startDate", "")[:10], e.get("endDate", "")[:10], round(float(e.get("volume") or 0)), len(e.get("markets") or []))
