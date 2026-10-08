"""quake_entertain: cached, budget-counted Kalshi + external fetches. Every Kalshi call goes through
lab.us.data_refresh.fetch(url, pace=1.15) and is counted in data/kalshi_lab/strategies/quake_entertain/calls.json.
Responses are cached under data/kalshi_lab/strategies/quake_entertain/raw/ so re-runs cost nothing."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/quake_entertain"); RAW = OUT / "raw"; CALLS = OUT / "calls.json"
BUDGET = 200


def _key(url: str) -> Path:
    return RAW / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")


def kalshi(path: str, refresh: bool = False) -> dict:
    url = path if path.startswith("http") else K + path
    f = _key(url)
    if f.exists() and not refresh:
        return json.loads(f.read_text() or "{}")
    c = json.loads(CALLS.read_text()) if CALLS.exists() else {"n": 0, "log": []}
    if c["n"] >= BUDGET:
        raise RuntimeError("Kalshi budget exhausted")
    txt = fetch(url, pace=1.15)
    c["n"] += 1; c["log"].append([int(time.time()), url]); CALLS.write_text(json.dumps(c))
    f.write_text(txt or "{}")
    return json.loads(txt or "{}")


def ext(url: str, refresh: bool = False, pace: float = 0.5) -> str:
    f = _key(url).with_suffix(".txt")
    if f.exists() and not refresh:
        return f.read_text()
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research-script"}), timeout=120) as r:
                txt = r.read().decode()
            f.write_text(txt); time.sleep(pace); return txt
        except Exception as e:
            print("ext retry", i, str(e)[:80], flush=True); time.sleep(3 * (i + 1))
    return ""


def calls_used() -> int:
    return json.loads(CALLS.read_text())["n"] if CALLS.exists() else 0


# ---------------------------------------------------------------- Kalshi quake markets + candles
import datetime as _dt
from collections import defaultdict as _dd


def iso(s: str | None) -> int | None:
    return int(_dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def compact(cs: list[dict]) -> list[list]:
    f = lambda c, k, sub: float((c.get(k) or {}).get(sub)) if (c.get(k) or {}).get(sub) is not None else None
    return [[int(c["end_period_ts"]), f(c, "yes_ask", "close_dollars"), f(c, "yes_bid", "close_dollars"), f(c, "yes_ask", "low_dollars"),
             f(c, "yes_bid", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def series_markets(series: str) -> list[dict]:
    """Settled + closed markets of a series (live endpoint, ~68 days), normalised to the lab format."""
    out = {}
    for status in ("settled", "closed"):
        for x in kalshi(f"/markets?series_ticker={series}&status={status}&limit=1000").get("markets", []):
            out[x["ticker"]] = {"t": x["ticker"], "e": x["event_ticker"], "series": series, "open": iso(x["open_time"]), "close": iso(x["close_time"]),
                                "exp": iso(x.get("expected_expiration_time")), "result": x.get("result") or "", "type": x.get("strike_type"),
                                "floor": x.get("floor_strike"), "cap": x.get("cap_strike"), "sub": x.get("yes_sub_title"),
                                "vol": float(x.get("volume_fp") or 0), "status": x.get("status")}
    return sorted(out.values(), key=lambda m: (m["close"], m["t"]))


def batch_candles(tickers: list[str], start: int, end: int) -> dict[str, list]:
    """/markets/candlesticks for <= 10 markets; splits the span so markets x minutes <= 9,000."""
    out = _dd(list)
    span = max(60, 9000 // max(1, len(tickers)) * 60)
    t0 = start
    while t0 < end:
        t1 = min(end, t0 + span)
        d = kalshi(f"/markets/candlesticks?market_tickers={','.join(tickers)}&start_ts={t0}&end_ts={t1}&period_interval=1")
        for mk in d.get("markets", []) or []:
            out[mk.get("market_ticker") or mk.get("ticker")] += compact(mk.get("candlesticks") or [])
        t0 = t1
    return {k: sorted({r[0]: r for r in v}.values()) for k, v in out.items()}
