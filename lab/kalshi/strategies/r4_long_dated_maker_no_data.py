"""Data layer for r4_long_dated_maker_no (no Kalshi calls unless asked).

Markets: the long-dated universe of r3_long_dated_longshot_no_repro (its cached /historical/markets and /markets
listings, deadline parser deadline(), eligibility S - open >= 45 d in Politics/World/Companies/Science/Economics).
Candles: the RAW cached hourly candle responses of the 209 eligible markets that round 3 fetched (both the original
researcher's cache and the reproducer's), read as data only, keeping the fields round 3 dropped: the trade-price
high/low/close (price.*), volume and open interest. Extra markets fetched by this family are cached under
data/kalshi_lab/strategies/r4_long_dated_maker_no/api_cache via r4_long_dated_maker_no_api.cached().

Candle row: (end_ts, ask_close, bid_close, ask_hi, ask_lo, bid_hi, bid_lo, p_hi, p_lo, p_close, volume, oi).
A candle ending at E covers trades in (E - 3600, E]; p_* is None when nothing traded in the hour."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r3_long_dated_longshot_no_repro as R
from lab.kalshi.strategies.r4_long_dated_maker_no_api import OUT, cached

SERIES_ALL = Path("/Users/roomyhome/Tradeinc/data/kalshi_lab/series_all.json")


def _v(side: dict | None, key: str):
    if not side:
        return None
    v = side.get(key + "_dollars", side.get(key))
    if v is None:
        return None
    v = float(v)
    return v / 100 if v > 1.0001 else v


def parse_raw(lst: list[dict]) -> list[tuple]:
    out = {}
    for c in lst or []:
        a, b = _v(c.get("yes_ask"), "close"), _v(c.get("yes_bid"), "close")
        if a is None or b is None:
            continue
        p = c.get("price") or {}
        vol = float(c.get("volume_fp") or c.get("volume") or 0)
        oi = float(c.get("open_interest_fp") or c.get("open_interest") or 0)
        out[int(c["end_period_ts"])] = (int(c["end_period_ts"]), a, b, _v(c.get("yes_ask"), "high"), _v(c.get("yes_ask"), "low"),
                                        _v(c.get("yes_bid"), "high"), _v(c.get("yes_bid"), "low"), _v(p, "high"), _v(p, "low"), _v(p, "close"), vol, oi)
    return [out[k] for k in sorted(out)]


def _resp_candles(d: dict, t: str) -> list:
    if not d:
        return []
    if "candlesticks" in d:
        return d["candlesticks"]
    return next((x["candlesticks"] for x in d.get("markets", []) if x.get("market_ticker", t) == t), [])


def round3_candle_urls() -> dict[str, list[str]]:
    by = {}
    for f in (R.SRC / "calls.log", R.OUT / "calls.log"):
        if not f.exists():
            continue
        for line in f.open():
            p = line.rstrip("\n").split("\t")
            if len(p) == 3 and int(p[1]) > 0 and "candlesticks" in p[2] and "/events/" not in p[2]:
                u = p[2]
                if "/historical/markets/" in u:
                    t = u.split("/historical/markets/")[1].split("/")[0]
                elif "market_tickers=" in u:
                    t = u.split("market_tickers=")[1].split("&")[0]
                else:
                    continue
                by.setdefault(t, []).append(u)
    return by


def raw_candles(t: str, urls: list[str]) -> list[tuple]:
    rows = []
    for u in urls:
        d = R.raw(u)
        rows += parse_raw(_resp_candles(d, t))
    m = {r[0]: r for r in rows}
    return [m[k] for k in sorted(m)]


def fee_info() -> dict[str, tuple[str, float]]:
    d = json.loads(SERIES_ALL.read_text())
    items = d.get("series", d) if isinstance(d, dict) else d
    items = items if isinstance(items, list) else list(items.values())
    return {x["ticker"]: (x.get("fee_type") or "quadratic", float(x.get("fee_multiplier") if x.get("fee_multiplier") is not None else 1)) for x in items}


def load_known() -> tuple[dict, dict]:
    """(markets, candles) for every eligible market with cached round-3 hourly candles."""
    U = R.universe()
    E = R.eligible(U)
    urls = round3_candle_urls()
    M, C = {}, {}
    for k, m in E.items():
        if k in urls:
            c = raw_candles(k, urls[k])
            if c:
                M[k] = dict(m, src="round3")
                C[k] = c
    return M, C


def fetch_market_candles(m: dict, budget_total: int = 200) -> list[tuple]:
    """Hourly candles over [S - 62 d, min(close, S)] for a market not cached by round 3 (one call)."""
    t = m["t"]
    start = max(m["open"], m["S"] - 62 * R.DAY) // 3600 * 3600
    end = min(m["close"], m["S"])
    if end <= start:
        return []
    if m["hist"]:
        d = cached(f"/historical/markets/{t}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60", budget_total)
    else:
        d = cached(f"/markets/candlesticks?market_tickers={t}&start_ts={start}&end_ts={end}&period_interval=60", budget_total)
    return parse_raw(_resp_candles(d or {}, t))


def trades_window(t: str, hist: bool, lo: int, hi: int, budget_total: int = 200, pages: int = 2) -> list[tuple] | None:
    """Trade prints in [lo, hi]: [(ts, yes_price, count, taker_side, is_block)], oldest first. None if not fetched.
    Up to `pages` pages of 1000 (newest first); 'complete' is False when the window was truncated."""
    import calendar, time as _t
    base = "/historical/trades" if hist else "/markets/trades"
    out, cur = [], ""
    for _ in range(pages):
        d = cached(f"{base}?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={cur}" if cur else ""), budget_total)
        if d is None:
            return None
        for x in d.get("trades") or []:
            s = x["created_time"].replace("Z", "")
            sec = calendar.timegm(_t.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + (float("0" + s[19:]) if len(s) > 19 else 0.0)
            yp = float(x.get("yes_price_dollars") or (x.get("yes_price", 0) / 100))
            out.append((sec, yp, float(x.get("count_fp") or x.get("count") or 0), x.get("taker_side"), bool(x.get("is_block_trade"))))
        cur = d.get("cursor") or ""
        if not cur or not d.get("trades"):
            return sorted(out)
    return sorted(out) + [("truncated",)]
