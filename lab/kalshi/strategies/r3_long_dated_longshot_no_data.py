"""Data for r3_long_dated_longshot_no: settled long-dated markets in retail categories, and their hourly/daily quotes.

Step 1 (list):  /series?category=C&include_volume=true (5 calls) -> pick the highest-volume long-dated series per
                category (choice made from the catalog + volume only, before any price or result is seen);
                /historical/markets?series_ticker=S (archive, settled before 2026-08-08) and, for some series,
                /markets?series_ticker=S&status=settled (live tier) -> markets.jsonl
Step 2 (sample): leak-free eligibility: binary, result yes/no, scheduled life (expected expiration - open) >= 45 d.
                 Stratified random sample (seeded) by category, at most MAX_PER_EVENT markets per event.
Step 3 (candles): archive: one /historical/markets/{t}/candlesticks call per market, hourly, from
                 max(open, exp - 62 d) to min(close, exp) + 1 h; live tier: same via the batch endpoint.
Usage: python -m lab.kalshi.strategies.r3_long_dated_longshot_no_data [list|sample|candles|all]"""
from __future__ import annotations
import datetime as dt, hashlib, json, random, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_long_dated_longshot_no_api import kget, used, OUT, CACHE

CATS = ["Politics", "World", "Companies", "Science%20and%20Technology", "Economics"]
# Chosen from the /series volume ranking only (no prices, no results): long-dated retail questions per category.
# (A first attempt passed mve_filter=exclude together with series_ticker, which /historical/markets rejects as
# mutually exclusive filters: 90 calls were burned on 400s. The list was then cut to fit the remaining budget.)
SERIES = {
    "Politics": ["KXFEDCHAIRNOM", "KXGOVSHUT", "KXTRUMPPARDON", "KXCABOUT", "KXTRUMPADMINLEAVE", "KXUSAIRANAGREEMENT",
                 "KXNOBELPEACE", "KXGREENLAND"],
    "Science and Technology": ["KXLLM1", "KXALIENS", "KXSPACEXSTARSHIP", "KXOAIAGI"],
    "Companies": ["KXTIKTOKBAN", "KXOAIPROFIT"],
    "Economics": ["KXRECSSNBER", "KXRATECUTCOUNT", "KXLAYOFFSYINFO"],
}
# Live tier (settled after the 2026-08-08 archive cutoff, plus open markets for the capacity check): no status filter.
LIVE = ["KXGOVSHUT", "KXTRUMPPARDON", "KXCABOUT", "KXTRUMPADMINLEAVE", "KXLLM1", "KXSPACEXSTARSHIP", "KXRATECUTCOUNT",
        "KXLAYOFFSYINFO"]
DAY = 86400


def ts(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def row(m: dict, cat: str, tier: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0], "cat": cat, "tier": tier,
            "created": ts(m.get("created_time")), "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
            "exp": ts(m.get("expected_expiration_time")), "latest_exp": ts(m.get("latest_expiration_time")),
            "settled": ts(m.get("settlement_ts")), "result": m.get("result"), "type": m.get("market_type"),
            "early": m.get("can_close_early"), "vol": float(m.get("volume_fp") or m.get("volume") or 0),
            "title": (m.get("title") or "")[:120], "sub": (m.get("yes_sub_title") or "")[:80]}


def list_markets() -> None:
    rows = {}
    for cat, ss in SERIES.items():
        for s in ss:
            cursor = None
            for _ in range(2):
                d = kget(f"/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
                for m in d.get("markets", []):
                    rows[m["ticker"]] = row(m, cat, "hist")
                cursor = d.get("cursor")
                if not cursor or len(d.get("markets", [])) < 1000:
                    break
            if s in LIVE:
                d = kget(f"/markets?series_ticker={s}&limit=1000")
                for m in d.get("markets", []):
                    r = row(m, cat, "live" if m.get("status") in ("settled", "finalized") else "open")
                    r["status"] = m.get("status"); r["yes_bid"] = float(m.get("yes_bid_dollars") or 0); r["yes_ask"] = float(m.get("yes_ask_dollars") or 0)
                    r["yes_bid_size"] = float(m.get("yes_bid_size_fp") or 0); r["yes_ask_size"] = float(m.get("yes_ask_size_fp") or 0)
                    rows[m["ticker"]] = r
            print(f"{s:22s} markets so far {len(rows):6d}  calls {used()}", flush=True)
    with (OUT / "markets.jsonl").open("w") as f:
        for r in rows.values():
            f.write(json.dumps(r) + "\n")




# ---------------------------------------------------------------- scheduled deadline (leak-free: set at creation)
import re
MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_T_DATE = re.compile(r"\b(?:by|before|on|until|through)\s+(?:the\s+end\s+of\s+)?([A-Z][a-z]{2,8})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_T_MONTH = re.compile(r"\bin\s+([A-Z][a-z]{2,8})\s+(\d{4})")
_E_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")


def deadline(r: dict) -> tuple[int | None, str]:
    """Scheduled end of the question, from text fixed when the market was listed (title, event ticker); never from the
    actual close (which moves when a market closes early). Returns (unix ts, source)."""
    m = _T_DATE.search(r["title"])
    if m and m.group(1)[:3].upper() in MON:
        d = dt.datetime(int(m.group(3)), MON[m.group(1)[:3].upper()], int(m.group(2)), tzinfo=dt.timezone.utc)
        return int(d.timestamp()) + DAY, "title_date"
    m = _E_DATE.search(r["e"] + "-")
    if m and m.group(2) in MON:
        d = dt.datetime(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3)), tzinfo=dt.timezone.utc)
        S = int(d.timestamp()) + DAY
        while S < r["open"] - 2 * DAY:      # old tickers carry a wrong year (GOVSHUT-21MAR12 is March 2022)
            S += 365 * DAY
        if S - r["open"] <= 2 * DAY:        # the date is the listing date of an open-ended event (KXCABOUT-26APR20)
            return None, "open_ended"
        return S, "event_ticker_date"
    m = re.search(r"-(\d{2})([A-Z]{3})$", r["t"])     # monthly deadline ladder (KXUSAIRANAGREEMENT-27-26JUL)
    if m and m.group(2) in MON:
        return int(dt.datetime(2000 + int(m.group(1)), MON[m.group(2)], 1, tzinfo=dt.timezone.utc).timestamp()), "market_ticker_month"
    m = _T_MONTH.search(r["title"])
    if m and m.group(1)[:3].upper() in MON:
        y, mo = int(m.group(2)), MON[m.group(1)[:3].upper()]
        d = dt.datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=dt.timezone.utc)
        return int(d.timestamp()), "title_month"
    if r["series"] == "KXNOBELPEACE":           # announced on a date published a year ahead; close = announcement
        return r["close"], "scheduled_announcement"
    m = re.search(r"-(\d{2})$", r["e"])
    yrs = re.findall(r"\b(20\d\d)\b", r["title"])
    if m and yrs and int(yrs[-1]) != 2000 + int(m.group(1)) - 1:
        return None, "ambiguous_year"
    if m and re.search(r"this year|in 20\d\d|\b20\d\d\?", r["title"]):
        return int(dt.datetime(2000 + int(m.group(1)), 1, 1, tzinfo=dt.timezone.utc).timestamp()) - 1, "event_year"
    return None, "open_ended"


def eligible(r: dict) -> int | None:
    """Scheduled deadline if the market qualifies (binary, settled yes/no, scheduled life >= 45 d), else None."""
    S, _ = deadline(r)
    if r["series"] in ("KXLAYOFFSYINFO", "LAYOFFSYINFO"):   # resolves on a data release months after the named year
        return None
    if S is None or r.get("result") not in ("yes", "no") or r.get("type") not in (None, "binary"):
        return None
    return S if S - r["open"] >= 45 * DAY else None


def sample(per_event: int = 2, seed: int = 20261008) -> list[dict]:
    """Seeded random sample, at most per_event markets per event (an event is one cluster: extra markets of the same
    event add little information per call). Uses nothing but listing-time fields (no price, volume or result)."""
    R = [json.loads(l) for l in (OUT / "markets.jsonl").open()]
    by = defaultdict(list)
    for r in R:
        S = eligible(r)
        if S:
            r["S"] = S; by[r["e"]].append(r)
    rng = random.Random(seed); out = []
    for e in sorted(by):
        ms = sorted(by[e], key=lambda r: r["t"]); rng.shuffle(ms)
        out += ms[:per_event]
    (OUT / "sample.jsonl").write_text("".join(json.dumps(r) + "\n" for r in out))
    return out


def candles(max_calls: int = 70) -> None:
    """Hourly candles over [max(open, S - 62 d), min(close, S) + 1 h]: archive one call per market, live tier batched."""
    S_ = [json.loads(l) for l in (OUT / "sample.jsonl").open()]
    f = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in f.open()} if f.exists() else set()
    start = used(); n = 0
    with f.open("a") as g:
        for r in S_:
            if r["t"] in have:
                continue
            if used() - start >= max_calls:
                print("call cap reached"); break
            lo = max(r["open"], r["S"] - 62 * DAY) - 3600; hi = min(r["close"], r["S"]) + 3600
            if r["tier"] == "hist":
                d = kget(f"/historical/markets/{r['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
                cs = d.get("candlesticks", [])
            else:
                d = kget(f"/markets/candlesticks?market_tickers={r['t']}&start_ts={lo}&end_ts={hi}&period_interval=60")
                mk = d.get("markets", [])
                cs = mk[0].get("candlesticks", []) if mk else []
            rows = []
            for c in cs:
                a = (c.get("yes_ask") or {}).get("close"); b = (c.get("yes_bid") or {}).get("close")
                rows.append([c["end_period_ts"], float(a) if a is not None else None, float(b) if b is not None else None,
                             float(c.get("volume") or c.get("volume_fp") or 0)])
            g.write(json.dumps({"t": r["t"], "c": rows, "err": d.get("_error")}) + "\n"); g.flush(); n += 1
            print(f"{r['t']:40s} candles {len(rows):5d}  calls {used()}", flush=True)
    print("fetched", n, "calls used", used())


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "list"
    if what == "list":
        list_markets()
    elif what == "sample":
        x = sample()
        from collections import Counter
        print(len(x), "markets,", len({r["e"] for r in x}), "events"); print(Counter((r["series"], r["tier"]) for r in x))
    elif what == "candles":
        candles(int(sys.argv[2]) if len(sys.argv) > 2 else 70)
