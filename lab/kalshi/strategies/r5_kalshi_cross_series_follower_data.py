"""Data layer for r5_kalshi_cross_series_follower: market metadata (listings) and hourly / 1-minute candles.

Metadata comes from listings already cached by earlier researchers (0 calls) plus a few counted calls.
Candles: every cached response for a ticker (any researcher, period 60 or 1) is merged; missing windows are fetched with
/historical/markets/{t}/candlesticks (archive tier, one market per call) or /markets/candlesticks (live tier, batched).
Parsed candles are stored in data/kalshi_lab/strategies/r5_kalshi_cross_series_follower/candles_{60,1}.jsonl as
{"t": ticker, "c": [[end_ts, yes_ask_close, yes_bid_close, volume], ...]} (ask None if no seller, bid None if no buyer)."""
from __future__ import annotations
import datetime as dt, json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.r5_kalshi_cross_series_follower_api import kget, cached_paths, OUT

HIST_CUTOFF = int(dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc).timestamp())   # archive tier: settled before this

LISTINGS = [f"/historical/markets?series_ticker={s}&limit=1000" for s in
            ("KXFEDDECISION", "KXFED", "KXRATECUTCOUNT", "KXRATECUT", "KXGOVSHUT", "KXGOVSHUTLENGTH", "KXSHUTDOWNBY")] + \
           ["/markets?series_ticker=KXFEDDECISION&status=settled&min_close_ts=1785542400&limit=1000",
            "/markets?series_ticker=KXFED&status=settled&min_close_ts=1785542400&limit=1000"]


def ts(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def meta() -> dict[str, dict]:
    out = {}
    for p in LISTINGS:
        d = kget(p, cache_only=True)
        for m in d.get("markets", []):
            out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "open": ts(m["open_time"]), "close": ts(m["close_time"]),
                                "settled": ts(m.get("settlement_ts")), "result": m.get("result"), "sub": m.get("yes_sub_title") or "",
                                "floor": m.get("floor_strike"), "title": m.get("title", ""), "rules": m.get("rules_primary", ""),
                                "vol": float(m.get("volume_fp") or m.get("volume") or 0),
                                "tier": "hist" if p.startswith("/historical") else "live"}
    return out


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def parse(cs: list[dict]) -> list[list]:
    """API candles -> [[end_ts, ask, bid, vol]]; ask=None when the ask is 1.00 (no seller), bid=None when 0.00 (no buyer)."""
    rows = []
    for c in cs:
        a = c.get("yes_ask") or {}; b = c.get("yes_bid") or {}
        ask = _f(a.get("close", a.get("close_dollars"))); bid = _f(b.get("close", b.get("close_dollars")))
        vol = _f(c.get("volume", c.get("volume_fp"))) or 0.0
        if ask is not None and ask >= 0.995:
            ask = None
        if bid is not None and bid <= 0.005:
            bid = None
        rows.append([int(c["end_period_ts"]), ask, bid, vol])
    return rows


def _store_path(period: int) -> Path:
    return OUT / f"candles_{period}.jsonl"


def load_store(period: int = 60) -> dict[str, dict[int, list]]:
    f = _store_path(period); out: dict[str, dict[int, list]] = {}
    if f.exists():
        for line in f.open():
            x = json.loads(line)
            d = out.setdefault(x["t"], {})
            for r in x["c"]:
                d[r[0]] = r
    return out


def save_store(store: dict[str, dict[int, list]], period: int = 60) -> None:
    with _store_path(period).open("w") as g:
        for t, d in sorted(store.items()):
            g.write(json.dumps({"t": t, "c": [d[k] for k in sorted(d)]}) + "\n")


def harvest_cache(tickers: list[str], period: int = 60) -> dict[str, dict[int, list]]:
    """Merge every cached candle response (any researcher) for these tickers into the store (0 calls)."""
    store = load_store(period); want = set(tickers)
    for p in cached_paths("/historical/markets/"):
        m = re.match(r"/historical/markets/([^/]+)/candlesticks\?(.*)", p)
        if not m or m.group(1) not in want or f"period_interval={period}" not in m.group(2):
            continue
        d = kget(p, cache_only=True)
        for r in parse(d.get("candlesticks", [])):
            store.setdefault(m.group(1), {})[r[0]] = r
    for prefix in ("/markets/candlesticks", "/series/"):
        for p in cached_paths(prefix):
            if f"period_interval={period}" not in p or "candlesticks" not in p:
                continue
            d = kget(p, cache_only=True)
            tk = d.get("market_tickers") or []
            for t, cs in zip(tk, d.get("market_candlesticks") or []):
                if t in want:
                    for r in parse(cs):
                        store.setdefault(t, {})[r[0]] = r
            for x in d.get("markets") or []:     # /markets/candlesticks format
                t = x.get("market_ticker") or x.get("ticker")
                if t in want:
                    for r in parse(x.get("candlesticks", [])):
                        store.setdefault(t, {})[r[0]] = r
    save_store(store, period)
    return store


def coverage(store: dict, t: str, t0: int, t1: int, period: int = 60) -> float:
    d = store.get(t, {})
    if t1 <= t0:
        return 1.0
    n = sum(1 for k in d if t0 <= k <= t1)
    return n / max(1, (t1 - t0) // (period * 60))


def fetch_hist(store: dict, t: str, t0: int, t1: int, period: int = 60) -> int:
    """One archive call for ticker t over [t0, t1]; returns candles added."""
    p = f"/historical/markets/{t}/candlesticks?start_ts={t0}&end_ts={t1}&period_interval={period}"
    d = kget(p)
    rows = parse(d.get("candlesticks", []))
    for r in rows:
        store.setdefault(t, {})[r[0]] = r
    return len(rows)


def fetch_live(store: dict, tickers: list[str], t0: int, t1: int, period: int = 60) -> int:
    p = f"/markets/candlesticks?market_tickers={','.join(tickers)}&start_ts={t0}&end_ts={t1}&period_interval={period}"
    d = kget(p); n = 0
    for x in d.get("markets") or []:
        t = x.get("market_ticker") or x.get("ticker")
        for r in parse(x.get("candlesticks", [])):
            store.setdefault(t, {})[r[0]] = r; n += 1
    if "_error" in d or not d.get("markets"):
        print("live fetch issue", str(d)[:300])
    return n


def series_of(store: dict, t: str) -> list[list]:
    d = store.get(t, {})
    return [d[k] for k in sorted(d)]


def requested_spans(period: int = 60) -> dict[str, list[tuple[int, int]]]:
    """ticker -> [(start_ts, end_ts)] of every cached candle request (any researcher, plus ours): what is known."""
    out: dict[str, list[tuple[int, int]]] = {}
    for p in cached_paths("/"):
        if "candlesticks" not in p or f"period_interval={period}" not in p:
            continue
        q = dict(kv.split("=", 1) for kv in p.split("?", 1)[1].split("&") if "=" in kv)
        s0, s1 = int(q.get("start_ts", 0)), int(q.get("end_ts", 0))
        m = re.match(r"/historical/markets/([^/]+)/candlesticks", p)
        tick = [m.group(1)] if m else (q.get("market_tickers", "").split(",") if "market_tickers" in q else [])
        if p.startswith("/series/"):
            d = kget(p, cache_only=True); tick = d.get("market_tickers") or []
        for t in tick:
            out.setdefault(t, []).append((s0, s1))
    return out


def span_cover(spans: list[tuple[int, int]], t0: int, t1: int) -> float:
    """Fraction of [t0, t1] inside the union of spans."""
    if t1 <= t0:
        return 1.0
    iv = sorted((max(a, t0), min(b, t1)) for a, b in spans if b > t0 and a < t1)
    tot = 0; cur = None
    for a, b in iv:
        if cur is None or a > cur[1]:
            if cur:
                tot += cur[1] - cur[0]
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        tot += cur[1] - cur[0]
    return tot / (t1 - t0)
