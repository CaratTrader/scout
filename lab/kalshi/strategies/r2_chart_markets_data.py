"""Data for r2_chart_markets: settled Kalshi chart markets (Netflix weekly Top 10 #1 US/Global show/movie, Billboard
Hot 100 #1, Billboard 200 #1) and hourly candles.

Live window (settled in the last ~70 days): /markets listing + batched /markets/candlesticks (period_interval=60,
the 10,000-candle cap counts hourly candles), window [close - 7 d, close].
Archive (settled before 2026-08-08): /historical/markets listing (all markets, with the archive's previous_* fields).
Storage under data/kalshi_lab/strategies/r2_chart_markets/: markets.jsonl, candles.jsonl
  candles: {"t": ticker, "c": [[end_ts, yes_ask_close, yes_bid_close, volume], ...]}
Usage: python -m lab.kalshi.strategies.r2_chart_markets_data [list|candles|hist]"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_chart_markets_api import kget, used, OUT

SERIES = ("KXNETFLIXRANKSHOW", "KXNETFLIXRANKMOVIE", "KXNETFLIXRANKSHOWGLOBAL", "KXNETFLIXRANKMOVIEGLOBAL", "KXTOPSONG", "KXTOPALBUM")
MF = OUT / "markets.jsonl"; HF = OUT / "markets_hist.jsonl"; CF = OUT / "candles.jsonl"
WINDOW_H = 7 * 24
MAX_CANDLES = 9500
MIN_CLOSE = int(dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc).timestamp())   # fixed so the cached listing call is reused


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def slim(m: dict, src: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["ticker"].split("-")[0], "src": src, "open": ts(m.get("open_time")),
            "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")), "settle": ts(m.get("settlement_ts")),
            "result": m.get("result"), "sub": m.get("yes_sub_title"), "vol": f(m.get("volume_fp")) or 0.0,
            "prev_bid": f(m.get("previous_yes_bid_dollars")), "prev_ask": f(m.get("previous_yes_ask_dollars")), "prev_px": f(m.get("previous_price_dollars")),
            "last_px": f(m.get("last_price_dollars")), "created": ts(m.get("created_time")), "rules": (m.get("rules_primary") or "")[:300]}


def list_live() -> list[dict]:
    out = []
    for s in SERIES:
        cursor = ""
        while True:
            d = kget(f"/markets?series_ticker={s}&status=settled&min_close_ts={MIN_CLOSE}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            out += [slim(m, "live") for m in d.get("markets") or [] if m.get("result") in ("yes", "no")]
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
    MF.write_text("".join(json.dumps(m) + "\n" for m in sorted(out, key=lambda m: (m["close"], m["t"]))))
    print("live markets", len(out), "used", used())
    return out


def list_hist(series=SERIES) -> list[dict]:
    out = []
    for s in series:
        cursor = ""
        while True:
            d = kget(f"/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            ms = d.get("markets") or []
            out += [slim(m, "hist") for m in ms if m.get("result") in ("yes", "no")]
            cursor = d.get("cursor") or ""
            print(s, "page", len(ms), "used", used(), flush=True)
            if not cursor or not ms:
                break
    HF.write_text("".join(json.dumps(m) + "\n" for m in sorted(out, key=lambda m: (m["close"] or 0, m["t"]))))
    print("hist markets", len(out), "used", used())
    return out


def compact(cs):
    def g(c, k):
        v = c.get(k) or {}
        x = v.get("close_dollars", v.get("close"))
        return float(x) if x not in (None, "") else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask"), g(c, "yes_bid"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def candles_live() -> None:
    ms = [json.loads(l) for l in MF.open()]
    have = {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()
    for s in SERIES:
        todo = sorted([m for m in ms if m["series"] == s and m["t"] not in have], key=lambda m: (m["close"], m["t"]))
        i = 0
        while i < len(todo):
            lo = todo[i]["close"] - WINDOW_H * 3600; hi = todo[i]["close"]; batch = [todo[i]]
            while i + len(batch) < len(todo) and len(batch) < 100:
                m2 = todo[i + len(batch)]
                lo2, hi2 = min(lo, m2["close"] - WINDOW_H * 3600), max(hi, m2["close"])
                if (len(batch) + 1) * ((hi2 - lo2) // 3600 + 1) > MAX_CANDLES:
                    break
                batch.append(m2); lo, hi = lo2, hi2
            i += len(batch)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            if not got:
                print("FAIL", s, d.get("_error"), str(d.get("_body"))[:200]); continue
            with CF.open("a") as fh:
                for m in batch:
                    fh.write(json.dumps({"t": m["t"], "c": compact(got.get(m["t"], []))}) + "\n")
            print(s, "batch", len(batch), "span_h", (hi - lo) // 3600, "used", used(), flush=True)


def candles_hist(plan_file: str, since: str) -> None:
    """Archive markets: one /historical/markets/{t}/candlesticks call per market (hourly, [close - 7 d, close]).
    plan_file: [[event, [tickers...]], ...] chosen without outcomes for trading candidates (Spotify #1, incumbent)
    plus the winner, which is used only for scoring the market's log loss."""
    import re
    MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
    def cd(e):
        x = re.search(r"-(\d\d)([A-Z]{3})(\d\d)$", e)
        return dt.date(2000 + int(x.group(1)), MON[x.group(2)], int(x.group(3)))
    hm = {json.loads(l)["t"]: json.loads(l) for l in HF.open()}
    have = {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()
    plan = [p for p in json.loads((OUT / plan_file).read_text()) if cd(p[0]) >= dt.date.fromisoformat(since)]
    for e, tks in plan:
        for t in tks:
            if t in have:
                continue
            c = hm[t]["close"]
            d = kget(f"/historical/markets/{t}/candlesticks?start_ts={c - WINDOW_H * 3600}&end_ts={c}&period_interval=60")
            if "candlesticks" not in d:
                print("FAIL", t, d.get("_error"), str(d.get("_body"))[:120]); continue
            with CF.open("a") as fh:
                fh.write(json.dumps({"t": t, "c": compact(d["candlesticks"])}) + "\n")
            have.add(t)
        print(e, "used", used(), flush=True)


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "list"
    if a == "list":
        list_live()
    elif a == "candles":
        candles_live()
    elif a == "hcandles":
        candles_hist(sys.argv[2], sys.argv[3])
    elif a == "hist":
        list_hist(tuple(sys.argv[2].split(",")) if len(sys.argv) > 2 else SERIES)
