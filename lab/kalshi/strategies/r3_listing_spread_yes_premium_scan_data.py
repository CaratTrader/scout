"""Data collection for r3_listing_spread_yes_premium_scan (every Kalshi call counted and cached via ..._api.kget).

  list     : settled markets of each screened series, closed since 2026-08-01 (live tier, ~68 days) -> markets.jsonl
  candles  : hourly candles (yes_ask/yes_bid close, trade price low/high, volume) for every market of the chosen events
             over [first open of the event - 1 h, min(last close + 1 h, first open + 100 h)] via batched
             /markets/candlesticks (<= 100 tickers, <= 9,500 hourly candles per call) -> candles.jsonl
             (a fetch window only: decision rules never use close times they could not have seen; markets opening more
             than 3 h after the event's first open are skipped by the rule, so every evaluated market is fully covered)

Usage: python -m lab.kalshi.strategies.r3_listing_spread_yes_premium_scan_data list S1,S2,...
       python -m lab.kalshi.strategies.r3_listing_spread_yes_premium_scan_data candles S1,S2 part max_calls cap   (part = disc|val|all;
       cap = max markets per event, sampled by sha1(ticker); 0 = all)"""
from __future__ import annotations
import datetime as dt, hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_listing_spread_yes_premium_scan_api import kget, used, OUT

MIN_CLOSE = 1785542400   # 2026-08-01 00:00 UTC (fixed so the cache key is stable)
KEEP = ("ticker", "event_ticker", "title", "yes_sub_title", "open_time", "close_time", "expiration_time", "result", "volume_fp",
        "created_time", "can_close_early", "strike_type", "floor_strike", "cap_strike", "market_type")
SPLIT = 0.7
SPAN_H = 100             # fetch window length after the event's first open (hours)


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def slim(m: dict, series: str) -> dict:
    x = {k: m.get(k) for k in KEEP}
    x["series"] = series
    return x


def list_series(series: list[str], max_pages: int = 2) -> None:
    f = OUT / "markets.jsonl"
    have = {json.loads(l)["ticker"]: json.loads(l) for l in f.open()} if f.exists() else {}
    summ = OUT / "list_summary.json"
    S = json.loads(summ.read_text()) if summ.exists() else {}
    for s in series:
        cursor = ""; n = 0; pages = 0; more = False
        for _ in range(max_pages):
            d = kget(f"/markets?series_ticker={s}&status=settled&min_close_ts={MIN_CLOSE}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            pages += 1
            ms = d.get("markets") or []
            for m in ms:
                if m.get("result") in ("yes", "no"):
                    have[m["ticker"]] = slim(m, s); n += 1
            cursor = d.get("cursor") or ""
            more = bool(cursor) and len(ms) >= 1000
            if not more:
                break
        ev = {m["event_ticker"] for m in have.values() if m["series"] == s}
        S[s] = {"markets": n, "events": len(ev), "pages": pages, "truncated": more}
        print(f"{s}: {n} settled markets, {len(ev)} events{' (TRUNCATED)' if more else ''}; calls used {used()}", flush=True)
        f.write_text("".join(json.dumps(m) + "\n" for m in have.values()))
        summ.write_text(json.dumps(S, indent=1))


def import_peer(series: str, peer_file: str) -> None:
    """Add a series from another researcher's cached raw /markets?series_ticker=S&status=settled response (read-only,
    no Kalshi call). Only markets closing on/after MIN_CLOSE are kept, as in list_series."""
    f = OUT / "markets.jsonl"
    have = {json.loads(l)["ticker"]: json.loads(l) for l in f.open()} if f.exists() else {}
    d = json.loads(Path(peer_file).read_text()); n = 0
    for m in d.get("markets") or []:
        if m.get("result") in ("yes", "no") and ts(m["close_time"]) >= MIN_CLOSE:
            have[m["ticker"]] = slim(m, series); n += 1
    f.write_text("".join(json.dumps(m) + "\n" for m in have.values()))
    summ = OUT / "list_summary.json"; S = json.loads(summ.read_text()) if summ.exists() else {}
    ev = {m["event_ticker"] for m in have.values() if m["series"] == series}
    S[series] = {"markets": n, "events": len(ev), "pages": 0, "truncated": bool(d.get("cursor")) and len(d.get("markets") or []) >= 1000,
                 "source": peer_file}
    summ.write_text(json.dumps(S, indent=1))
    print(f"{series}: {n} settled markets, {len(ev)} events (from peer cache)")


def events_of(series: str) -> dict:
    """event -> list of markets, plus the discovery/validation split by the event's last close."""
    ms = [json.loads(l) for l in (OUT / "markets.jsonl").open()]
    ev = {}
    for m in ms:
        if m["series"] != series:
            continue
        m["open_ts"], m["close_ts"] = ts(m["open_time"]), ts(m["close_time"])
        ev.setdefault(m["event_ticker"], []).append(m)
    order = sorted(ev, key=lambda e: (max(m["close_ts"] for m in ev[e]), e))
    cut = int(len(order) * SPLIT)
    part = {e: ("disc" if i < cut else "val") for i, e in enumerate(order)}
    return ev, part


def _f(c: dict, k: str, sub: str):
    v = (c.get(k) or {}).get(sub)
    return float(v) if v is not None else None


def sample_markets(x: list[dict], cap: int | None) -> list[dict]:
    """Markets of one event the rule can evaluate (open within 3 h of the event's first open), at most `cap` of them,
    chosen by sha1(ticker) order (independent of prices and outcomes)."""
    omin = min(m["open_ts"] for m in x)
    ok = [m for m in x if m["open_ts"] <= omin + 3 * 3600]
    ok.sort(key=lambda m: hashlib.sha1(m["ticker"].encode()).hexdigest())
    return ok[:cap] if cap else ok


def candles(series_list: list[str], part: str = "disc", max_calls: int = 30, cap: int | None = 8) -> None:
    """Hourly candles for the sampled markets of the chosen events of several series, packed into shared calls."""
    cf = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
    jobs = []
    for series in series_list:
        ev, prt = events_of(series)
        for e, x in ev.items():
            if part != "all" and prt[e] != part:
                continue
            if any(m["ticker"] in have for m in x):
                continue                     # event already fetched (all its sampled markets were requested together)
            omin = min(m["open_ts"] for m in x); cmax = max(m["close_ts"] for m in x)
            tks = [m["ticker"] for m in sample_markets(x, cap)]
            if not tks:
                continue
            lo = (omin // 3600) * 3600 - 3600; hi = min(cmax + 3600, omin + SPAN_H * 3600)
            jobs.append((lo, hi, e, tks))
    jobs.sort()
    print(f"{len(jobs)} events to fetch, {sum(len(j[3]) for j in jobs)} tickers", flush=True)
    start_used = used(); i = 0
    while i < len(jobs):
        lo, hi, _, tks = jobs[i]; batch = [jobs[i]]; j = i + 1
        while j < len(jobs):
            lo2, hi2, _, tk2 = jobs[j]
            n = sum(len(b[3]) for b in batch) + len(tk2); span = (max(hi, hi2) - min(lo, lo2)) // 3600 + 1
            if n > 100 or n * span > 9500:
                break
            batch.append(jobs[j]); lo, hi = min(lo, lo2), max(hi, hi2); j += 1
        if used() - start_used >= max_calls:
            print("max_calls reached"); break
        tickers = [t for b in batch for t in b[3]]
        d = kget(f"/markets/candlesticks?market_tickers={','.join(tickers)}&start_ts={lo}&end_ts={hi}&period_interval=60")
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in (d.get("markets") or [])}
        if not got:
            print("failed batch", [b[2] for b in batch], str(d)[:200]); i = j; continue
        with cf.open("a") as fh:
            for t in tickers:
                cs = got.get(t, [])
                fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "c": [[int(c["end_period_ts"]), _f(c, "yes_ask", "close_dollars"), _f(c, "yes_bid", "close_dollars"),
                                                     _f(c, "price", "close_dollars"), float(c.get("volume_fp") or c.get("volume") or 0),
                                                     _f(c, "price", "low_dollars"), _f(c, "price", "high_dollars")] for c in cs]}) + "\n")
        print(f"batch {len(batch)} events {len(tickers)} tickers span {(hi - lo) // 3600}h; calls used {used()}", flush=True)
        i = j


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "list":
        list_series(sys.argv[2].split(","))
    elif what == "candles":
        candles(sys.argv[2].split(","), sys.argv[3] if len(sys.argv) > 3 else "disc", int(sys.argv[4]) if len(sys.argv) > 4 else 30,
                int(sys.argv[5]) if len(sys.argv) > 5 else 8)
