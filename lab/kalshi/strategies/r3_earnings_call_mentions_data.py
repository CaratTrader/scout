"""Data collection for r3_earnings_call_mentions (every Kalshi call counted + cached via r3_earnings_call_mentions_api.kget).

  rank      : /series?category=Mentions&include_volume=true -> earnings-mention series ranked by lifetime volume
  hist N    : /historical/markets?series_ticker=S for the N highest-volume earnings series -> markets_hist.jsonl
              (settled before ~2026-08-08: results for base rates, never prices)
  live S,.. : /markets?series_ticker=S (all statuses, ~68-day live tier: recently settled + upcoming) -> markets_live.jsonl
  candles   : hourly /markets/candlesticks for live-tier settled events, two windows per event:
              W1 = [first open - 1 h, first open + 49 h]  (listing / first-quote decision)
              W2 = [first close - 52 h, last close + 1 h] (call detection, pre-call decision, maker cancel, in-call path)
              daily /markets/candlesticks over [first open - 1 d, last close + 1 d] (maker fill path between W1 and W2)
Usage: python -m lab.kalshi.strategies.r3_earnings_call_mentions_data rank|hist N|live S1,S2|candles MAXCALLS"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_earnings_call_mentions_api import kget, used, OUT

ET = ZoneInfo("America/New_York")
KEEP = ("ticker", "event_ticker", "title", "yes_sub_title", "custom_strike", "open_time", "close_time", "created_time",
        "occurrence_datetime", "expected_expiration_time", "result", "status", "volume_fp", "can_close_early",
        "yes_ask_dollars", "yes_bid_dollars", "yes_ask_size_fp", "yes_bid_size_fp", "last_price_dollars")


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def ranked() -> list[tuple[str, float]]:
    d = kget("/series?category=Mentions&include_volume=true")
    e = [(x["ticker"], float(x.get("volume_fp") or 0)) for x in d.get("series") or []
         if "EARNINGSMENTION" in x["ticker"] or x["ticker"] == "KXBRKEM"]
    return sorted(e, key=lambda kv: -kv[1])


def slim(m: dict, series: str) -> dict:
    x = {k: m.get(k) for k in KEEP}
    x["series"] = series
    w = (m.get("custom_strike") or {}).get("Word")
    x["word"] = w if w else m.get("yes_sub_title")
    return x


def _store(fname: str, rows: dict) -> None:
    (OUT / fname).write_text("".join(json.dumps(m) + "\n" for m in rows.values()))


def _load(fname: str) -> dict:
    f = OUT / fname
    return {json.loads(l)["ticker"]: json.loads(l) for l in f.open()} if f.exists() else {}


def list_series(series: list[str], tier: str, max_pages: int = 3) -> None:
    fname = "markets_hist.jsonl" if tier == "hist" else "markets_live.jsonl"
    have = _load(fname)
    for s in series:
        cursor = ""; n = 0
        for _ in range(max_pages):
            base = f"/historical/markets?series_ticker={s}&limit=1000" if tier == "hist" else f"/markets?series_ticker={s}&limit=1000"
            d = kget(base + (f"&cursor={cursor}" if cursor else ""))
            ms = d.get("markets") or []
            for m in ms:
                have[m["ticker"]] = slim(m, s); n += 1
            cursor = d.get("cursor") or ""
            if not cursor or len(ms) < 1000:
                break
        ev = sorted({m["event_ticker"] for m in have.values() if m["series"] == s})
        print(f"{tier} {s}: {n} markets, events {ev}; calls used {used()}", flush=True)
        _store(fname, have)


def _f(c: dict, k: str, sub: str):
    v = (c.get(k) or {}).get(sub)
    return float(v) if v is not None else None


def _row(c: dict) -> list:
    """[end_ts, ask_close, bid_close, ask_low, bid_high, price_close, price_high, price_low, volume]"""
    return [int(c["end_period_ts"]), _f(c, "yes_ask", "close_dollars"), _f(c, "yes_bid", "close_dollars"), _f(c, "yes_ask", "low_dollars"),
            _f(c, "yes_bid", "high_dollars"), _f(c, "price", "close_dollars"), _f(c, "price", "high_dollars"), _f(c, "price", "low_dollars"),
            float(c.get("volume_fp") or c.get("volume") or 0)]


def call_day_start(close_ts: int) -> int:
    """00:00 ET of the call day, judged from the event's first close (closes come 0.5-5 h after the call ends; a close
    before 06:00 ET belongs to the previous evening's call). Fetch window only - never a decision input."""
    d = dt.datetime.fromtimestamp(close_ts, ET)
    if d.hour < 6:
        d = d - dt.timedelta(days=1)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp())


def jobs_for(window: str) -> list[tuple[int, int, str, list[str]]]:
    ms = [m for m in _load("markets_live.jsonl").values() if m["result"] in ("yes", "no")]
    ev = {}
    for m in ms:
        ev.setdefault(m["event_ticker"], []).append(m)
    jobs = []
    for e, x in ev.items():
        o = min(ts(m["open_time"]) for m in x); c1 = min(ts(m["close_time"]) for m in x); c2 = max(ts(m["close_time"]) for m in x)
        if window == "W1":
            lo, hi = (o // 3600) * 3600 - 3600, (o // 3600) * 3600 + 49 * 3600
            hi = min(hi, c2 + 3600)
        elif window == "W2":   # closes can come up to ~27 h after the call (COST, MU): look back 52 h from the first close
            lo = max((c1 // 3600) * 3600 - 52 * 3600, (o // 3600) * 3600 - 3600); hi = (c2 // 3600) * 3600 + 3600
        else:   # daily
            lo = (o // 86400) * 86400 - 86400; hi = (c2 // 86400) * 86400 + 2 * 86400
        jobs.append((lo, hi, e, sorted(m["ticker"] for m in x)))
    return sorted(jobs)


def candles(window: str, max_calls: int) -> None:
    period = 1440 if window == "D" else 60
    cf = OUT / f"candles_{window}.jsonl"
    have = {json.loads(l)["e"] for l in cf.open()} if cf.exists() else set()
    jobs = [j for j in jobs_for(window) if j[2] not in have]
    start_used = used(); i = 0; unit = period * 60
    while i < len(jobs):
        lo, hi, _, tks = jobs[i]; batch = [jobs[i]]; j = i + 1
        while j < len(jobs):
            lo2, hi2, _, tk2 = jobs[j]
            n = sum(len(b[3]) for b in batch) + len(tk2); span = (max(hi, hi2) - min(lo, lo2)) // unit + 1
            if n > 100 or n * span > 9000:
                break
            batch.append(jobs[j]); lo, hi = min(lo, lo2), max(hi, hi2); j += 1
        if len(tks) * ((hi - lo) // unit + 1) > 9000 or len(tks) > 100:
            print("event too large for one call, clipped:", batch[0][2], len(tks), (hi - lo) // unit)
            hi = lo + (9000 // max(len(tks), 1) - 1) * unit
        if used() - start_used >= max_calls:
            print("max_calls reached"); break
        tickers = [t for b in batch for t in b[3]]
        d = kget(f"/markets/candlesticks?market_tickers={','.join(tickers)}&start_ts={lo}&end_ts={hi}&period_interval={period}")
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in (d.get("markets") or [])}
        if not got:
            print("failed batch", [b[2] for b in batch], str(d)[:200]); i = j; continue
        with cf.open("a") as fh:
            for b in batch:
                fh.write(json.dumps({"e": b[2], "lo": lo, "hi": hi, "c": {t: [_row(c) for c in got.get(t, [])] for t in b[3]}}) + "\n")
        print(f"{window}: batch {len(batch)} events {len(tickers)} tickers span {(hi - lo) // unit} periods; calls used {used()}", flush=True)
        i = j


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "rank":
        for i, (s, v) in enumerate(ranked()):
            print(i, s, int(v))
    elif what == "hist":
        n = int(sys.argv[2]); list_series([s for s, _ in ranked()[:n]], "hist")
    elif what == "live":
        list_series(sys.argv[2].split(","), "live")
    elif what == "candles":
        candles(sys.argv[2], int(sys.argv[3]))


def trades(orders: list[tuple[str, int, int, str]], max_calls: int) -> None:
    """orders: (ticker, min_ts, max_ts, tag). Trade prints (<= 1000 per call) inside the first fill candle of a simulated
    maker order, to size its fill. -> trades_sample.jsonl"""
    f = OUT / "trades_sample.jsonl"
    have = {(json.loads(l)["t"], json.loads(l)["lo"]) for l in f.open()} if f.exists() else set()
    start = used()
    with f.open("a") as fh:
        for t, lo, hi, tag in orders:
            if (t, lo) in have:
                continue
            if used() - start >= max_calls:
                break
            d = kget(f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000")
            tr = [[x.get("created_time"), float(x.get("yes_price_dollars") or 0), float(x.get("count_fp") or x.get("count") or 0), x.get("taker_side")]
                  for x in d.get("trades") or []]
            fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "tag": tag, "cursor": bool(d.get("cursor")), "trades": tr}) + "\n")
            print(t, tag, len(tr), "trades; calls used", used(), flush=True)
