"""Data collection for r3_frozen_c1_other_mention_formats (every Kalshi call counted + cached via the _api module).

Commands (python -m lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_data <cmd>):
  lists     settled markets, live tier (/markets?status=settled) and archive (/historical/markets), for every series of
            the three groups -> markets.jsonl (one row per settled market, both tiers merged by ticker)
  plan      which markets need candles (complete events only; markets whose open + 1 h precedes the event's first close)
  candles   live-tier markets: batched hourly /markets/candlesticks, window [own open - 1 h, end of the hour of the
            event's first close + 1 h] (no 96 h truncation); archive markets: /historical/markets/{t}/candlesticks,
            one call per market, only for the seeded sample in plan()
  trades    /markets/trades prints for audited orders (see the analysis module)
Candle rows are stored as [T, ask_c, bid_c, ask_lo, ask_hi, bid_lo, bid_hi, px_lo, px_hi, vol] (norm_candle 10-field form).
"""
from __future__ import annotations
import datetime as dt, hashlib, json, random, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_api import kget, used, OUT

GROUPS = {
    "sports": ["KXNFLMENTION", "KXNBAMENTION", "KXNHLMENTION", "KXMLBMENTION", "KXNCAAMENTION", "KXSNFMENTION", "KXNASCARMENTION",
               "KXWNBAMENTION"],
    "trump_accum": ["KXTRUMPSAYMONTH", "KXTRUMPSAYCOUNTRY", "KXTRUMPSAYCOMPANY", "KXTRUMPSAYNICKNAME", "KXTRUMPSAY"],
    "post_ladder": ["KXTRUTHSOCIAL", "KXELONTWEETS", "KXTRUTHSOCIALD"],
}
SERIES_GROUP = {s: g for g, ss in GROUPS.items() for s in ss}
DATA_TS = 1791504000          # 2026-10-08 ~00:00 UTC: an event is complete if every market's expiration_time <= this
R2_CACHE = Path("data/kalshi_lab/strategies/r2_mentions_baserate/api_cache")   # read-only reuse (KXTRUMPSAY live list)
SPORT_SAMPLE_EVENTS = 56      # archive sports events sampled (one market each), seeded
# Series whose markets close early word by word, so the frozen cancel trigger ("any market of the event closed") fires
# when the broadcast starts. KXNFLMENTION / KXNCAAMENTION / KXSNFMENTION close all words together after the game, so
# the frozen trigger never fires pre-game there; they are not sampled (declared before any candle was fetched).
SPORT_FUNCTIONAL = ("KXMLBMENTION", "KXNBAMENTION", "KXNHLMENTION", "KXWNBAMENTION")
SEED = 20261009


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def _r2_cached(path: str) -> dict:
    f = R2_CACHE / (hashlib.sha1(path.encode()).hexdigest()[:24] + ".json")
    return json.loads(f.read_text()) if f.exists() else {}


def slim(m: dict, series: str, tier: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": series, "group": SERIES_GROUP[series], "tier": tier,
            "open": ts(m.get("open_time")), "close": ts(m.get("close_time")), "exp": ts(m.get("expiration_time")),
            "result": m.get("result"), "sub": m.get("yes_sub_title") or (m.get("custom_strike") or {}).get("Word"),
            "vol": float(m.get("volume_fp") or m.get("volume") or 0), "strike_type": m.get("strike_type"),
            "floor": m.get("floor_strike"), "cap": m.get("cap_strike")}


def lists() -> None:
    have = {}
    for g, ss in GROUPS.items():
        for s in ss:
            # live lists round 2 already fetched (min_close 2026-08-01) are reused read-only (KXTRUMPSAY; the sports
            # series it found empty); everything else is fetched here
            d = _r2_cached(f"/markets?series_ticker={s}&status=settled&min_close_ts=1785542400&limit=1000")
            if not d:
                d = kget(f"/markets?series_ticker={s}&status=settled&limit=1000")
            for m in d.get("markets") or []:
                if m.get("result") in ("yes", "no"):
                    have[m["ticker"]] = slim(m, s, "live")
            h = kget(f"/historical/markets?series_ticker={s}&limit=1000")
            for m in h.get("markets") or []:
                if m.get("result") in ("yes", "no") and m["ticker"] not in have:
                    have[m["ticker"]] = slim(m, s, "hist")
            print(s, "markets so far", len(have), "calls", used(), flush=True)
    (OUT / "markets.jsonl").write_text("".join(json.dumps(m) + "\n" for m in sorted(have.values(), key=lambda m: (m["e"], m["t"]))))


def load_markets() -> list[dict]:
    return [json.loads(l) for l in (OUT / "markets.jsonl").open()]


def events_of(mk: list[dict]) -> dict:
    ev = defaultdict(list)
    for m in mk:
        ev[m["e"]].append(m)
    out = {}
    for e, x in ev.items():
        out[e] = {"e": e, "group": x[0]["group"], "series": x[0]["series"], "n": len(x), "first_close": min(m["close"] for m in x),
                  "last_close": max(m["close"] for m in x), "first_open": min(m["open"] for m in x),
                  # ladders settle every bucket at once (their expiration_time is a far 'latest' date); accumulators
                  # settle word by word, so they are complete only once the scheduled end (expiration_time) has passed
                  "complete": max(m["close"] for m in x) <= DATA_TS and (x[0]["group"] == "post_ladder" or max(m["exp"] or m["close"] for m in x) <= DATA_TS),
                  "tiers": sorted({m["tier"] for m in x}),
                  "multi_close": len({m["close"] for m in x}) > 1, "markets": sorted(m["t"] for m in x)}
    return out


def plan() -> dict:
    """Markets to fetch. Live-tier events: every market. Archive-only events (sports): a seeded sample of
    SPORT_SAMPLE_EVENTS events, one random market each. Events straddling the tiers: the archive markets too."""
    mk = load_markets(); E = events_of(mk); M = {m["t"]: m for m in mk}
    live, hist = [], []
    rng = random.Random(SEED)
    arch_sports = sorted(e for e, v in E.items() if v["complete"] and v["series"] in SPORT_FUNCTIONAL and v["tiers"] == ["hist"])
    pick = set(rng.sample(arch_sports, min(SPORT_SAMPLE_EVENTS, len(arch_sports))))
    for e, v in sorted(E.items()):
        if not v["complete"]:
            continue
        tks = [t for t in v["markets"] if M[t]["open"] + 3600 < v["first_close"]]
        if v["tiers"] == ["hist", "live"]:
            continue      # straddles the archive cutoff (KXTRUMPSAY-26AUG03: 15 archive markets, KXTRUMPSAYNICKNAME-26OCT01: 11):
                          # excluded whole (budget: one archive call per market), decided before any candle was fetched
        if "live" in v["tiers"]:
            live += tks
        elif e in pick:
            cand = [t for t in tks]
            if cand:
                hist.append(rng.choice(cand))
    p = {"live": live, "hist": hist, "sports_events_sampled": sorted(pick)}
    (OUT / "plan.json").write_text(json.dumps(p, indent=1))
    print("live markets", len(live), "archive markets", len(hist), "(sports sample events", len(pick), "of", len(arch_sports), ")")
    return p


def _f(d: dict, k: str):
    if not d:
        return None
    v = d.get(k + "_dollars", d.get(k))
    return float(v) if v is not None else None


def row(c: dict) -> list:
    vol = float(c.get("volume_fp") or c.get("volume") or 0)
    a, b, p = c.get("yes_ask") or {}, c.get("yes_bid") or {}, c.get("price") or {}
    return [int(c["end_period_ts"]), _f(a, "close"), _f(b, "close"), _f(a, "low"), _f(a, "high"), _f(b, "low"), _f(b, "high"),
            _f(p, "low") if vol > 0 else None, _f(p, "high") if vol > 0 else None, vol]


def window(m: dict, ev: dict) -> tuple[int, int]:
    lo = (m["open"] // 3600) * 3600 - 3600
    stop = min(ev["first_close"], m["close"])
    hi = ((stop + 3599) // 3600) * 3600 + 3600
    return lo, hi


def candles(max_calls: int = 120) -> None:
    mk = load_markets(); E = events_of(mk); M = {m["t"]: m for m in mk}
    p = json.loads((OUT / "plan.json").read_text())
    cf = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
    start = used()
    # live: batch by event, <= 100 tickers and markets x span-hours <= 9,500
    jobs = defaultdict(list)
    for t in p["live"]:
        if t not in have:
            jobs[M[t]["e"]].append(t)
    jl = []
    for e, tks in jobs.items():
        ws = [window(M[t], E[e]) for t in tks]
        jl.append((min(w[0] for w in ws), max(w[1] for w in ws), e, tks))
    jl.sort()
    batches, cur = [], None
    for lo, hi, e, tks in jl:
        for i in range(0, len(tks), 100):
            part = tks[i:i + 100]
            if cur:
                nlo, nhi = min(cur[0], lo), max(cur[1], hi); n = len(cur[2]) + len(part)
                if n <= 100 and n * ((nhi - nlo) // 3600 + 1) <= 9500:
                    cur = [nlo, nhi, cur[2] + part]; continue
                batches.append(cur)
            span = (hi - lo) // 3600 + 1
            if len(part) * span > 9500:          # one event too long for one call: split its tickers
                k = max(1, 9500 // span)
                for j in range(0, len(part), k):
                    batches.append([lo, hi, part[j:j + k]])
                cur = None
            else:
                cur = [lo, hi, part]
    if cur:
        batches.append(cur)
    print("live batches", len(batches), flush=True)
    with cf.open("a") as fh:
        for lo, hi, tks in batches:
            if used() - start >= max_calls:
                print("max_calls reached"); break
            d = kget(f"/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in (d.get("markets") or [])}
            if not got:
                print("failed batch", tks[:2], len(tks)); continue
            for t in tks:
                fh.write(json.dumps({"t": t, "src": "batch", "c": [row(c) for c in got.get(t, [])]}) + "\n")
            print(f"batch {len(tks)} tickers span {(hi - lo) // 3600}h; calls {used()}", flush=True)
        for t in p["hist"]:
            if t in have:
                continue
            if used() - start >= max_calls:
                print("max_calls reached"); break
            lo, hi = window(M[t], E[M[t]["e"]])
            d = kget(f"/historical/markets/{t}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
            fh.write(json.dumps({"t": t, "src": "hist", "c": [row(c) for c in d.get("candlesticks") or []]}) + "\n")
            print(f"hist {t} {len(d.get('candlesticks') or [])} candles; calls {used()}", flush=True)


def trades(orders: list[tuple[str, int, int]], max_calls: int) -> None:
    """orders: (ticker, min_ts, max_ts). Stores every print (paged, <= 3 pages)."""
    f = OUT / "trades.jsonl"
    have = {json.loads(l)["t"] for l in f.open()} if f.exists() else set()
    start = used()
    with f.open("a") as fh:
        for t, lo, hi in orders:
            if t in have:
                continue
            if used() - start >= max_calls:
                print("max_calls reached"); break
            allt, cursor = [], ""
            for _ in range(3):
                d = kget(f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
                allt += d.get("trades") or []
                cursor = d.get("cursor") or ""
                if not cursor:
                    break
            fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "complete": not cursor,
                                 "trades": [[x.get("created_time"), float(x.get("yes_price_dollars") or 0), float(x.get("count_fp") or x.get("count") or 0),
                                             x.get("taker_side")] for x in allt]}) + "\n")
            print(t, len(allt), "prints; calls", used(), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "lists":
        lists()
    elif cmd == "plan":
        plan()
    elif cmd == "candles":
        candles(int(sys.argv[2]) if len(sys.argv) > 2 else 120)
