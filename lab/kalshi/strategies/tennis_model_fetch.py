"""Cached, counted fetches for the tennis_model study. Every Kalshi request goes through the shared paced fetch()
(lab.us.data_refresh), is cached on disk and counted in data/kalshi_lab/strategies/tennis_model/kalshi_calls.json
(budget: 200 Kalshi requests for the whole study)."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/tennis_model")
CACHE = OUT / "cache"
COUNT = OUT / "kalshi_calls.json"
BUDGET = 200


def calls() -> int:
    return json.loads(COUNT.read_text())["n"] if COUNT.exists() else 0


def kget(path: str, use_cache: bool = True) -> dict:
    """GET {K}{path} (Kalshi), cached by URL hash. Raises if the 200-call budget would be exceeded."""
    CACHE.mkdir(parents=True, exist_ok=True)
    url = path if path.startswith("http") else K + path
    f = CACHE / ("k_" + hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if use_cache and f.exists():
        return json.loads(f.read_text())
    n = calls()
    if n >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(url, pace=1.15)
    COUNT.write_text(json.dumps({"n": n + 1, "last": url, "ts": int(time.time())}))
    d = json.loads(txt) if txt else {}
    if txt:
        f.write_text(txt)
    return d


def wget(url: str, name: str, pace: float = 1.0, headers: dict | None = None) -> str:
    """Polite cached GET for non-Kalshi free sources."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / name
    if f.exists():
        return f.read_text()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; tennis_model)", "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
    except Exception as e:
        print("wget fail", url[:100], str(e)[:80], flush=True)
        return ""
    time.sleep(pace)
    f.write_text(txt)
    return txt


def fetch_early(events: list[dict], path: Path = OUT / "early_candles.jsonl", per_call: int = 100) -> None:
    """Hourly candles from market open to the lab window start (exp - 6 h) for the alphabetically first market of each
    event (its book gives both sides: YES at ask, NO at 1 - bid). Stored as {"t", "c": [[ts, ask, bid, vol], ...]}."""
    have = {json.loads(l)["t"] for l in path.open()} if path.exists() else set()
    todo = sorted((e for e in events if sorted(e["mk"])[0] not in have), key=lambda e: e["open"])
    with path.open("a") as fh:
        for i in range(0, len(todo), per_call):
            b = todo[i:i + per_call]
            lo = min(e["open"] for e in b); hi = max(e["exp"] - 360 * 60 for e in b)
            tk = [sorted(e["mk"])[0] for e in b]
            d = kget(f"/markets/candlesticks?market_tickers={','.join(tk)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {x.get("market_ticker"): x.get("candlesticks") or [] for x in d.get("markets") or []}
            f = lambda c, k: float(c[k]["close_dollars"]) if (c.get(k) or {}).get("close_dollars") is not None else None
            for e, t in zip(b, tk):
                if t not in got:
                    continue
                cs = [[int(c["end_period_ts"]), f(c, "yes_ask"), f(c, "yes_bid"), float(c.get("volume_fp") or 0)] for c in got[t]
                      if int(c["end_period_ts"]) <= e["exp"] - 360 * 60]
                fh.write(json.dumps({"t": t, "c": cs}) + "\n")
            print("early batch", i // per_call, "calls", calls(), flush=True)
