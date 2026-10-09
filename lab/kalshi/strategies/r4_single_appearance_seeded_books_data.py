"""Data collection for r4_single_appearance_seeded_books (every Kalshi call counted + cached via
r4_single_appearance_seeded_books_api.kget). Series and sampling rules: data/kalshi_lab/strategies/
r4_single_appearance_seeded_books/preregistration_backtest.json (written before any of this data was fetched).

  lists      : /historical/markets?series_ticker=S and /markets?series_ticker=S (live tier) for every pre-registered
               series -> markets.jsonl (settled + open, slim fields incl. rules_primary for the schedule date)
  plan       : eligibility (listing fields + close times only) and the seeded sample -> plan.json
  candles N  : candles for the sampled markets (archive: one call per market; live: batched) -> candles.jsonl
  trades N   : /markets/trades prints for live-tier sampled markets over their order window -> trades.jsonl
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_single_appearance_seeded_books_data lists|plan|candles N|trades N"""
from __future__ import annotations
import datetime as dt, json, math, random, re, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_single_appearance_seeded_books_api import kget, used, OUT

ET = ZoneInfo("America/New_York")
PRE = json.loads((OUT / "preregistration_backtest.json").read_text())
GROUPS = PRE["universe"]["groups"]
SERIES = [s for g in GROUPS.values() for s in g]
GROUP_OF = {s: g for g, ss in GROUPS.items() for s in ss}
ARCHIVE_CUTOFF = 1786147200          # 2026-08-08 00:00 UTC: settled before -> /historical tier
SEED = 20261008
KEEP = ("ticker", "event_ticker", "title", "yes_sub_title", "custom_strike", "open_time", "close_time", "created_time",
        "occurrence_datetime", "expected_expiration_time", "latest_expiration_time", "result", "status", "volume_fp",
        "can_close_early", "rules_primary", "market_type")
MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"))}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9


def ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def slim(m: dict, series: str, tier: str) -> dict:
    x = {k: m.get(k) for k in KEEP}
    x["series"] = series; x["group"] = GROUP_OF[series]; x["tier"] = tier
    x["word"] = (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title")
    return x


def load_markets() -> dict:
    f = OUT / "markets.jsonl"
    return {json.loads(l)["ticker"]: json.loads(l) for l in f.open()} if f.exists() else {}


def lists(tiers: tuple = ("hist", "live"), series: list[str] | None = None, max_pages: int = 3) -> None:
    have = load_markets()
    for s in series or SERIES:
        for tier, base in (("hist", f"/historical/markets?series_ticker={s}&limit=1000"), ("live", f"/markets?series_ticker={s}&limit=1000")):
            if tier not in tiers:
                continue
            cursor = ""; n = 0
            for _ in range(max_pages):
                d = kget(base + (f"&cursor={cursor}" if cursor else ""))
                ms = d.get("markets") or []
                for m in ms:
                    if m["ticker"] not in have or tier == "live":      # live copy wins (fresher status)
                        have[m["ticker"]] = slim(m, s, tier); n += 1
                cursor = d.get("cursor") or ""
                if not cursor or len(ms) < 1000:
                    break
            print(f"{tier} {s}: {n} markets; calls used {used()}", flush=True)
        (OUT / "markets.jsonl").write_text("".join(json.dumps(m) + "\n" for m in have.values()))


# ------------------------------------------------------------------------------------------------ schedule (listing fields only)
TICKER_DATE = re.compile(r"-[A-Z]*?(\d{2})([A-Z]{3})(\d{2})")


def ticker_date(e: str) -> dt.date | None:
    g = TICKER_DATE.search(e)
    if not g:
        return None
    mon = MONTHS.get(g.group(2).lower())
    try:
        return dt.date(2000 + int(g.group(1)), mon, int(g.group(3))) if mon else None
    except ValueError:
        return None


def sched_date(event_ticker: str, exps: list[int]) -> tuple[dt.date | None, str]:
    """D* (amendment 1): min(event-ticker date, ET date of min expected_expiration_time - 1 day). Listing fields only."""
    c = []
    d = ticker_date(event_ticker)
    if d:
        c.append((d, "ticker"))
    if exps:
        c.append(((dt.datetime.fromtimestamp(min(exps), ET).date() - dt.timedelta(days=1)), "exp-1d"))
    return min(c) if c else (None, "none")


def t_day(d: dt.date) -> int:
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=ET).timestamp())


def h1_of(open_ts: int) -> int:
    return int(math.ceil((open_ts + 3600) / 3600.0) * 3600)


def events() -> dict:
    """event -> {markets, first_close, last_close, complete} over settled yes/no binary markets."""
    E = defaultdict(lambda: {"markets": []})
    for m in load_markets().values():
        E[m["event_ticker"]]["markets"].append(m)
    out = {}
    for e, x in E.items():
        ms = x["markets"]
        done = [m for m in ms if m.get("result") in ("yes", "no")]
        if not done:
            continue
        closes = [ts(m["close_time"]) for m in ms if m.get("status") in ("closed", "settled", "determined", "finalized") or m.get("result") in ("yes", "no", "void")]
        closes = [c for c in closes if c]
        exps = [ts(m.get("expected_expiration_time")) for m in ms if ts(m.get("expected_expiration_time"))]
        out[e] = {"markets": done, "all": ms, "first_close": min(closes), "last_close": max(closes), "exps": exps,
                  "complete": all(m.get("result") in ("yes", "no", "void") for m in ms), "series": ms[0]["series"], "group": ms[0]["group"]}
    return out


def eligible(m: dict, ev: dict) -> dict | None:
    op = ts(m["open_time"]); cl = ts(m["close_time"])
    if op is None or cl is None or (m.get("market_type") not in (None, "binary")):
        return None
    d, src = sched_date(m["event_ticker"], ev["exps"])
    if d is None:
        return None
    H1 = h1_of(op); T = t_day(d)
    C = min(T, ev["first_close"], cl)
    C_lit = min(ev["first_close"], cl)
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["series"], "group": m["group"], "tier": m["tier"], "open": op,
            "close": cl, "H1": H1, "D": d.isoformat(), "D_src": src, "T_day": T, "C": C, "C_lit": C_lit,
            "elig_P": H1 + 120 < C, "elig_lit": H1 + 120 < C_lit, "ev_first_close": ev["first_close"], "ev_last_close": ev["last_close"],
            "exp": ts(m.get("expected_expiration_time")), "occ": ts(m.get("occurrence_datetime"))}


def plan(max_archive_calls: int = 70) -> None:
    E = events()
    rows = []
    for e, ev in E.items():
        if not ev["complete"]:
            continue
        for m in ev["markets"]:
            x = eligible(m, ev)
            if x and x["elig_P"]:
                rows.append(x)
    by_ev = defaultdict(list)
    for x in rows:
        by_ev[x["e"]].append(x)
    rng = random.Random(SEED)
    evs = sorted(by_ev)
    rng.shuffle(evs)
    order_mk = {}
    for e in evs:
        ms = sorted(by_ev[e], key=lambda x: x["t"])
        rng.shuffle(ms)
        order_mk[e] = [x["t"] for x in ms]
    is_live = {e: any(x["tier"] == "live" for x in by_ev[e]) or by_ev[e][0]["ev_last_close"] >= ARCHIVE_CUTOFF for e in evs}
    live_events = [e for e in evs if is_live[e]]
    arch = defaultdict(list)
    for e in evs:
        if not is_live[e]:
            arch[by_ev[e][0]["group"]].append(e)
    rr = []
    gs = sorted(arch)
    queues = {g: list(arch[g]) for g in gs}
    while any(queues[g] for g in gs):
        for g in gs:
            if queues[g]:
                rr.append(queues[g].pop(0))
    sample, n = {}, 0
    for e in rr:                                   # pass 1: one market per archive event
        if n + 1 > max_archive_calls:
            break
        sample[e] = order_mk[e][:1]; n += 1
    for e in rr:                                   # pass 2: a second market if budget remains
        if e in sample and len(order_mk[e]) > 1 and n + 1 <= max_archive_calls:
            sample[e] = order_mk[e][:2]; n += 1
    for e in live_events:
        sample[e] = order_mk[e][:2]
    info = {"n_eligible_markets": len(rows), "n_events": len(by_ev),
            "events_by_group": {g: sum(1 for e in by_ev if by_ev[e][0]["group"] == g) for g in GROUPS},
            "archive_events_sampled": sum(1 for e in sample if not is_live[e]), "archive_calls": n, "live_events": len(live_events),
            "sampled_by_group": {g: sum(1 for e in sample if by_ev[e][0]["group"] == g) for g in GROUPS}}
    (OUT / "plan.json").write_text(json.dumps({"info": info, "eligible": rows, "sample": sample, "order_mk": order_mk,
                                               "archive_events": [e for e in rr if e in sample], "live_events": live_events}, indent=0))
    print(json.dumps(info, indent=1))


# ------------------------------------------------------------------------------------------------ candles
def _f(c: dict, k: str, sub: str):
    x = c.get(k) or {}
    v = x.get(sub)
    if v is None:
        v = x.get(sub.replace("_dollars", ""))
        if v is not None and isinstance(v, (int, float)) and v > 1.5:
            v = v / 100.0
    return float(v) if v is not None else None


def crow(c: dict) -> list:
    """[end_ts, ask_close, bid_close, ask_low, ask_high, bid_low, bid_high, px_low, px_high, volume]"""
    return [int(c["end_period_ts"]), _f(c, "yes_ask", "close_dollars"), _f(c, "yes_bid", "close_dollars"),
            _f(c, "yes_ask", "low_dollars"), _f(c, "yes_ask", "high_dollars"), _f(c, "yes_bid", "low_dollars"),
            _f(c, "yes_bid", "high_dollars"), _f(c, "price", "low_dollars"), _f(c, "price", "high_dollars"),
            float(c.get("volume_fp") or c.get("volume") or 0)]


def window(x: dict) -> tuple[int, int, int]:
    """[open - 1 h, max(C_lit, C) + 1 h]; 1-minute candles if the window is <= 4,980 minutes (the /historical endpoint
    returns nothing for 1-minute requests above ~5,000 candles: 13 calls were lost to this on 2026-10-08), else hourly."""
    lo = (x["open"] // 60) * 60 - 3600
    hi = max(x["C_lit"], x["C"]) + 3600
    if hi - lo <= 4980 * 60:
        return lo, hi, 1
    return (lo // 3600) * 3600, ((hi + 3599) // 3600) * 3600, 60


def candles(max_calls: int) -> None:
    P = json.loads((OUT / "plan.json").read_text())
    el = {x["t"]: x for x in P["eligible"]}
    cf = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in cf.open() if json.loads(l)["c"]} if cf.exists() else set()
    start = used()
    with cf.open("a") as fh:
        # live tier: batch per event (all eligible markets of the event, <= 10,000 candles)
        for e in P["live_events"]:
            tks = [t for t in P["sample"][e] if t not in have]
            while tks:
                if used() - start >= max_calls:
                    print("max_calls reached"); return
                batch = []; lo = hi = None
                for t in tks:
                    l2, h2, _ = window(el[t])
                    nl, nh = min(lo or l2, l2), max(hi or h2, h2)
                    if batch and (len(batch) + 1) * ((nh - nl) // 3600 + 1) > 9500:
                        break
                    batch.append(t); lo, hi = nl, nh
                period = 1 if (len(batch) * ((hi - lo) // 60 + 1)) <= 9500 else 60
                if period == 60:
                    lo, hi = (lo // 3600) * 3600, ((hi + 3599) // 3600) * 3600
                d = kget(f"/markets/candlesticks?market_tickers={','.join(batch)}&start_ts={lo}&end_ts={hi}&period_interval={period}")
                got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in (d.get("markets") or [])}
                for t in batch:
                    fh.write(json.dumps({"t": t, "period": period, "lo": lo, "hi": hi, "c": [crow(c) for c in got.get(t, [])]}) + "\n")
                    have.add(t)
                fh.flush()
                print(f"live {e}: {len(batch)} markets period {period}; calls used {used()}", flush=True)
                tks = [t for t in tks if t not in have]
        for e in P["archive_events"]:
            for t in P["sample"][e]:
                if t in have:
                    continue
                if used() - start >= max_calls:
                    print("max_calls reached"); return
                lo, hi, period = window(el[t])
                d = kget(f"/historical/markets/{t}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval={period}")
                cs = d.get("candlesticks")
                if cs is None:
                    print("no candles", t, str(d)[:160], flush=True); cs = []
                fh.write(json.dumps({"t": t, "period": period, "lo": lo, "hi": hi, "c": [crow(c) for c in cs]}) + "\n"); fh.flush()
                have.add(t)
                print(f"arch {t}: {len(cs)} candles period {period}; calls used {used()}", flush=True)


def trades(orders: list[tuple[str, int, int]], max_calls: int) -> None:
    """orders: (ticker, lo, hi). Trade prints in (lo, hi) -> trades.jsonl"""
    f = OUT / "trades.jsonl"
    have = {json.loads(l)["t"] for l in f.open()} if f.exists() else set()
    start = used()
    with f.open("a") as fh:
        for t, lo, hi in orders:
            if t in have:
                continue
            if used() - start >= max_calls:
                break
            d = kget(f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000")
            tr = [[ts(x.get("created_time")), float(x.get("yes_price_dollars") or 0), float(x.get("count_fp") or x.get("count") or 0),
                   x.get("taker_side"), bool(x.get("is_block_trade"))] for x in d.get("trades") or []]
            fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "cursor": bool(d.get("cursor")), "trades": tr}) + "\n")
            print(t, len(tr), "prints; calls used", used(), flush=True)


if __name__ == "__main__":
    what = sys.argv[1]
    if what == "lists":
        lists(tuple(sys.argv[2].split(",")) if len(sys.argv) > 2 else ("hist", "live"), sys.argv[3].split(",") if len(sys.argv) > 3 else None)
    elif what == "plan":
        plan(int(sys.argv[2]) if len(sys.argv) > 2 else 70)
    elif what == "candles":
        candles(int(sys.argv[2]))
