"""Data collection for r2_mentions_baserate (all Kalshi calls counted and cached via r2_mentions_baserate_api.kget).

  list      : settled markets of the chosen mention series, last 68 days (live tier) -> markets_recent.jsonl
  history   : /historical/markets for the same series (settled before 2026-08-08) -> markets_hist.jsonl (results only;
              used for word base rates, never for prices)
  candles   : hourly candles per event via /series/{s}/events/{e}/candlesticks (one call per event) -> candles/<event>.json

Usage: python -m lab.kalshi.strategies.r2_mentions_baserate_data list|history|candles [max_calls]"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_mentions_baserate_api import kget, used, OUT

# Recurring "will <speaker> say <word> during <scheduled appearance>" series (one event = one appearance).
# Month/period accumulators (KXTRUMPSAYMONTH, KXTRUMPSAYCOMPANY, ...) and duration/count markets are excluded.
SERIES = ["KXTRUMPMENTION", "KXTRUMPMENTIONB", "KXTRUMPSAY", "KXNFLMENTION", "KXMLBMENTION", "KXNCAAMENTION", "KXSNFMENTION",
          "KXTNFMENTION", "KXSECPRESSMENTION", "KXVANCEMENTION", "KXBERNIEMENTION", "KXFEDMENTION", "KXFOXNEWSMENTION",
          "KXLASTWORDMENTION", "KXWORLDNEWSMENTION", "KXMENTION", "KXPRESMENTION", "KXLATENIGHTMENTION", "KXPOLITICSMENTION",
          "KXPERSONMENTION", "KXMAMDANIMENTION", "KXFIGHTMENTION", "KXWNBAMENTION", "KXNBAMENTION", "KXFTNMENTION",
          "KXMTPMENTION", "KXSNLMENTION", "KXLEAVITTMENTION", "KXHEGSETHMENTION", "KXCONGRESSMENTION", "KXHEARINGMENTION",
          "KXNEWSOMMENTION", "KX60MINMENTION", "KXMELANIAMENTION", "KXKIMMELMENTION"]
MIN_CLOSE = 1785542400   # 2026-08-01 00:00 UTC (fixed so the cache key is stable)
KEEP = ("ticker", "event_ticker", "title", "yes_sub_title", "custom_strike", "open_time", "close_time", "expiration_time",
        "result", "volume_fp", "created_time", "can_close_early")


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def slim(m: dict, series: str) -> dict:
    x = {k: m.get(k) for k in KEEP}
    x["series"] = series
    return x


def list_recent(series: list[str], max_pages: int = 4) -> None:
    f = OUT / "markets_recent.jsonl"
    have = {json.loads(l)["ticker"]: json.loads(l) for l in f.open()} if f.exists() else {}
    mc = MIN_CLOSE
    for s in series:
        cursor = ""; n = 0
        for _ in range(max_pages):
            d = kget(f"/markets?series_ticker={s}&status=settled&min_close_ts={mc}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            ms = d.get("markets") or []
            for m in ms:
                if m.get("result") in ("yes", "no"):
                    have[m["ticker"]] = slim(m, s); n += 1
            cursor = d.get("cursor") or ""
            if not cursor or len(ms) < 1000:
                break
        ev = len({m["event_ticker"] for m in have.values() if m["series"] == s})
        print(f"{s}: {n} settled markets, {ev} events; calls used {used()}", flush=True)
        f.write_text("".join(json.dumps(m) + "\n" for m in have.values()))


def list_hist(series: list[str], max_pages: int = 6) -> None:
    f = OUT / "markets_hist.jsonl"
    have = {json.loads(l)["ticker"]: json.loads(l) for l in f.open()} if f.exists() else {}
    for s in series:
        cursor = ""; n = 0
        for _ in range(max_pages):
            d = kget(f"/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            ms = d.get("markets") or []
            for m in ms:
                if m.get("result") in ("yes", "no"):
                    have[m["ticker"]] = slim(m, s); n += 1
            cursor = d.get("cursor") or ""
            if not cursor or len(ms) < 1000:
                break
        ev = len({m["event_ticker"] for m in have.values() if m["series"] == s})
        print(f"{s}: {n} historical markets, {ev} events; calls used {used()}", flush=True)
        f.write_text("".join(json.dumps(m) + "\n" for m in have.values()))


def event_candles(events: list[tuple[str, str, int, int]], max_calls: int) -> None:
    """events: (series, event_ticker, start_ts, end_ts). Hourly candles for every market of the event."""
    cd = OUT / "candles"; cd.mkdir(parents=True, exist_ok=True)
    start_used = used()
    for s, e, lo, hi in events:
        out = cd / f"{e}.json"
        if out.exists():
            continue
        if used() - start_used >= max_calls:
            break
        d = kget(f"/series/{s}/events/{e}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
        if not d:
            continue
        res = {}
        for tk, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
            res[tk] = [[int(c["end_period_ts"]), _f(c, "yes_ask", "close_dollars"), _f(c, "yes_bid", "close_dollars"),
                        _f(c, "price", "close_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]
        out.write_text(json.dumps({"adjusted_end_ts": d.get("adjusted_end_ts"), "c": res}))
        print(e, len(res), "markets", sum(len(v) for v in res.values()), "candles; calls used", used(), flush=True)


def _f(c: dict, k: str, sub: str):
    v = (c.get(k) or {}).get(sub)
    return float(v) if v is not None else None


def batch_candles(skip_series: tuple = ("KXTRUMPSAY",), max_calls: int = 70, max_hours: int = 96) -> None:
    """Hourly candles for every market of every recent event, packed into /markets/candlesticks calls
    (<= 100 tickers and <= 9,500 hourly candles per call). Window per event: [max(first open, last close - max_hours) - 1 h,
    last close + 1 h] (a fetch window only; decision rules never look at close times they could not have seen)."""
    f = OUT / "markets_recent.jsonl"; cf = OUT / "candles.jsonl"
    ms = [json.loads(l) for l in f.open()]
    have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
    ev = {}
    for m in ms:
        if m["series"] in skip_series or m["ticker"] in have:
            continue
        ev.setdefault(m["event_ticker"], []).append(m)
    jobs = []
    for e, x in ev.items():
        cmax = max(ts(m["close_time"]) for m in x); omin = min(ts(m["open_time"]) for m in x)
        lo = (max(omin, cmax - max_hours * 3600) // 3600) * 3600 - 3600; hi = cmax + 3600
        jobs.append((lo, hi, e, [m["ticker"] for m in x]))
    jobs.sort()
    start_used = used(); i = 0
    while i < len(jobs):
        lo, hi, _, tks = jobs[i]; batch = [jobs[i]]; j = i + 1
        while j < len(jobs):
            lo2, hi2, _, tk2 = jobs[j]
            n = sum(len(b[3]) for b in batch) + len(tk2); span = (max(hi, hi2) - min(lo, lo2)) // 3600 + 1
            if n > 100 or n * span > 9500:
                break
            batch.append(jobs[j]); lo, hi = min(lo, lo2), max(hi, hi2); j += 1
        if len(batch[0][3]) > 100:
            print("event too large, skipped", batch[0][2]); i = j; continue
        if used() - start_used >= max_calls:
            print("max_calls reached"); break
        tickers = [t for b in batch for t in b[3]]
        d = kget(f"/markets/candlesticks?market_tickers={','.join(tickers)}&start_ts={lo}&end_ts={hi}&period_interval=60")
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in (d.get("markets") or [])}
        if not got:
            print("failed batch", [b[2] for b in batch]); i = j; continue
        with cf.open("a") as fh:
            for t in tickers:
                cs = got.get(t, [])
                fh.write(json.dumps({"t": t, "c": [[int(c["end_period_ts"]), _f(c, "yes_ask", "close_dollars"), _f(c, "yes_bid", "close_dollars"),
                                                     _f(c, "price", "close_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]}) + "\n")
        print(f"batch {len(batch)} events {len(tickers)} tickers span {(hi - lo) // 3600}h; calls used {used()}", flush=True)
        i = j


def sample_trades(orders: list[tuple[str, int, int]], max_calls: int = 40) -> None:
    """orders: (ticker, start_ts, end_ts). Trade prints (<= 1000 per call) to calibrate the maker fill model."""
    f = OUT / "trades_sample.jsonl"
    have = {json.loads(l)["t"] for l in f.open()} if f.exists() else set()
    start_used = used()
    with f.open("a") as fh:
        for t, lo, hi in orders:
            if t in have:
                continue
            if used() - start_used >= max_calls:
                break
            d = kget(f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000")
            tr = [[x.get("created_time"), float(x.get("yes_price_dollars") or 0), float(x.get("count_fp") or x.get("count") or 0), x.get("taker_side")]
                  for x in d.get("trades") or []]
            fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "cursor": bool(d.get("cursor")), "trades": tr}) + "\n")
            print(t, len(tr), "trades; calls used", used(), flush=True)


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "list":
        list_recent(sys.argv[2].split(",") if len(sys.argv) > 2 else SERIES)
    elif what == "history":
        list_hist(sys.argv[2].split(",") if len(sys.argv) > 2 else SERIES)
    elif what == "candles":
        batch_candles(max_calls=int(sys.argv[2]) if len(sys.argv) > 2 else 70)
