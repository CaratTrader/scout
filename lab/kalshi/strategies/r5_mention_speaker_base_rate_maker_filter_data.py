"""Data layer for r5_mention_speaker_base_rate_maker_filter (no Kalshi calls except `fetch_missing`, which goes through
lab.us.data_refresh.fetch with the bot-idle pacing and is counted in calls.log).

1. Settlement pool: every settled mention market on disk (other researchers' caches, read-only) plus this family's own
   /historical listings for series whose history was never fetched (api_cache/). One row per ticker:
   {t, e, series, spk, w, open, close, yes}.
2. Speaker-format key: the series ticker with a trailing 'B' after 'MENTION' removed (KXTRUMPMENTIONB -> KXTRUMPMENTION,
   the round-2 grouping). Word key: round-2 wkey (lower case, punctuation stripped, '/'-separated alternatives sorted).
3. Leak-free prior: for an order whose event was listed at L (earliest open_time of the event's markets), the history is
   every pool market with the same (spk, w), from a different event, whose close_time <= L - 3600 s. The prior hit rate
   is k / n over that history.
4. Order files of the four seeded-book maker samples, normalised to one schema:
   {sample, t, e, series, spk, w, listing, post, s, px (NO price), N, filled (through-only), q (filled contracts, size
    proxy), no_won, ret (per $ if filled, maker fee 0)}.
     S1 r2 C1         r2_mentions_baserate frozen C1 on the 229 live-tier events, entries that follow the frozen rule only
                      (the 'truncated' entries the round-2 lead check flagged are dropped), hourly candle through-fills.
     S2 r3 C1E        the round-3 verifier's reproduction rows (r3_earnings_call_mentions/repro/rows_base.jsonl).
     S3 r4 arm A      r4_earnings_seeded_book_48h_forward archive_orders(0) + arm_rows(None, 'through').
     S4 r4 V1         r4_single_appearance_seeded_books/orders.jsonl scored with that module's score(o, 'P', 'sell', 'thr').
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sys
import time
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
S = ROOT / "data/kalshi_lab/strategies"
OUT = S / "r5_mention_speaker_base_rate_maker_filter"
CACHE = OUT / "api_cache"
BASE = "https://api.elections.kalshi.com/trade-api/v2"
BUFFER = 3600


def ts(x) -> int | None:
    if x in (None, "", "None"):
        return None
    if isinstance(x, (int, float)):
        return int(x)
    try:
        return int(dt.datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def spk_key(series: str) -> str:
    return re.sub(r"MENTIONB$", "MENTION", series)


def _word_of(m: dict) -> str:
    cs = m.get("custom_strike")
    if isinstance(cs, str):
        try:
            import ast
            cs = ast.literal_eval(cs)
        except Exception:  # noqa: BLE001
            cs = None
    w = (cs or {}).get("Word") if isinstance(cs, dict) else None
    return w or m.get("word") or m.get("yes_sub_title") or ""


def wkey(word: str) -> str:
    parts = sorted(p.strip() for p in re.sub(r"[^a-z0-9/+() ]", "", word.lower()).split("/") if p.strip())
    return "/".join(parts)


# ------------------------------------------------------------------------------------------------ settlement pool
POOL_FILES = [
    "r2_mentions_baserate/markets_hist.jsonl",
    "r2_mentions_baserate/markets_recent.jsonl",
    "r3_earnings_call_mentions/markets_hist.jsonl",
    "r3_earnings_call_mentions/markets_live.jsonl",
    "r4_single_appearance_seeded_books/markets.jsonl",
    "r3_mentions_maker_forward/settled.jsonl",
]


def _row(m: dict) -> dict | None:
    if m.get("result") not in ("yes", "no"):
        return None
    t = m.get("ticker")
    if not t:
        return None
    series = m.get("series") or t.split("-")[0]
    e = m.get("event_ticker") or "-".join(t.split("-")[:2])
    return {"t": t, "e": e, "series": series, "spk": spk_key(series), "w": wkey(_word_of(m)), "open": ts(m.get("open_time")),
            "close": ts(m.get("close_time")), "yes": m["result"] == "yes"}


def own_listings() -> list[dict]:
    """Markets from this family's own /historical listing calls (api_cache/*.json)."""
    out = []
    if CACHE.exists():
        for f in sorted(CACHE.glob("*.json")):
            x = json.loads(f.read_text())
            for m in x.get("markets", []) or []:
                out.append(m)
    return out


def load_pool() -> dict:
    pool = {}
    for rel in POOL_FILES:
        f = S / rel
        if not f.exists():
            continue
        for line in f.open():
            r = _row(json.loads(line))
            if r and r["close"] is not None:
                old = pool.get(r["t"])
                if old is None or (old["open"] is None and r["open"] is not None):
                    pool[r["t"]] = r
    for m in own_listings():
        r = _row(m)
        if r and r["close"] is not None and r["t"] not in pool:
            pool[r["t"]] = r
    return pool


class History:
    """(spk, w) -> sorted closes with outcomes; prior(spk, w, L, e) uses closes <= L - BUFFER from other events."""

    def __init__(self, pool: dict):
        self.d = defaultdict(list)
        self.series_first = {}
        for r in pool.values():
            self.d[(r["spk"], r["w"])].append((r["close"], r["yes"], r["e"]))
            self.series_first[r["spk"]] = min(self.series_first.get(r["spk"], r["close"]), r["close"])
        for v in self.d.values():
            v.sort()
        self.closes = {k: [c for c, _, _ in v] for k, v in self.d.items()}

    def prior(self, spk: str, w: str, L: int, e: str) -> tuple[int, int]:
        v = self.d.get((spk, w))
        if not v:
            return 0, 0
        i = bisect_right(self.closes[(spk, w)], L - BUFFER)
        x = [y for c, y, ee in v[:i] if ee != e]
        return sum(x), len(x)

    def speaker_prior_events(self, spk: str, L: int, e: str, pool_by_spk: dict) -> int:
        return len({ee for c, ee in pool_by_spk.get(spk, []) if c <= L - BUFFER and ee != e})


# ------------------------------------------------------------------------------------------------ order samples
def _event_listing(pool: dict, extra: dict | None = None) -> dict:
    lst = {}
    for r in list(pool.values()) + list((extra or {}).values()):
        if r.get("open") is None:
            continue
        lst[r["e"]] = min(lst.get(r["e"], r["open"]), r["open"])
    return lst


def orders_s1() -> list[dict]:
    from lab.kalshi.strategies import r2_mentions_baserate as MB
    mk, events, C, pool = MB.load()
    ev_m = defaultdict(list)
    for m in mk:
        ev_m[m["event_ticker"]].append(m)
    lo = {}
    for e, x in ev_m.items():
        cmax = max(m["close_ts"] for m in x); omin = min(m["open_ts"] for m in x)
        lo[e] = (max(omin, cmax - 96 * 3600) // 3600) * 3600 - 3600
    listing = {e: min(m["open_ts"] for m in x) for e, x in ev_m.items()}
    BR = MB.BaseRates(pool); F = MB.load_full()
    out = []
    for part in ("disc", "val"):
        for seq in MB.candidates(mk, events, C, BR, part, "first"):
            if not seq:
                continue
            s0 = seq[0]; m = s0["m"]
            sp = round(s0["ask"] - 0.01, 2)
            if not sp > s0["bid"]:
                continue
            trunc = m["open_ts"] < lo[m["event_ticker"]]
            fc = events[m["event_ticker"]]["first_close"]; end = fc + 3600
            filled = False; cls = None; fill_ts = None
            for r in F.get(m["ticker"], []):
                if r[0] <= s0["H"] or r[0] > end or r[0] > m["close_ts"] + 3600:
                    continue
                if r[6] is not None and r[6] > sp:
                    filled = True; fill_ts = r[0]; break
            if filled:
                cls = MB.fill_class(s0, m, F, events)
            px = round(1 - sp, 4); N = max(1, math.floor(5 / px)); no_won = not m["y"]
            out.append({"sample": "S1_r2_C1", "t": m["ticker"], "e": m["event_ticker"], "series": m["series"], "spk": spk_key(m["series"]),
                        "w": wkey(_word_of(m)), "listing": listing[m["event_ticker"]], "post": s0["H"], "s": sp, "px": px, "N": N,
                        "ask": s0["ask"], "bid": s0["bid"],
                        "filled": filled, "q": (N if cls == "full" else round(0.74 * N, 2)) if filled else 0.0, "fill_class": cls,
                        "no_won": no_won, "ret": ((1.0 if no_won else 0.0) - px) / px, "tclose": events[m["event_ticker"]]["last_close"],
                        "rule_consistent": not trunc, "r2_part": part, "fill_ts": fill_ts, "first_close": fc})
    return out


def orders_s2(pool: dict) -> list[dict]:
    rows = [json.loads(l) for l in (S / "r3_earnings_call_mentions/repro/rows_base.jsonl").open()]
    listing = _event_listing(pool)
    out = []
    for r in rows:
        series = r["t"].split("-")[0]
        pm = pool.get(r["t"])
        out.append({"sample": "S2_r3_C1E", "t": r["t"], "e": r["e"], "series": series, "spk": spk_key(series),
                    "w": pm["w"] if pm else wkey(r.get("word") or ""), "listing": listing.get(r["e"]), "post": r["post"], "s": r["s"],
                    "px": r["px"], "N": r["n_order"], "ask": round(r["s"] + 0.01, 2), "bid": round(r["s"] + 0.01 - r["spread"], 2),
                    "filled": bool(r["fill_thr"]),
                    "q": float(r["vol_proxy"]) if r["fill_thr"] else 0.0, "fill_class": None, "no_won": bool(r["won"]),
                    "ret": r["ret"], "tclose": r["t_close"], "rule_consistent": True})
    return out


def orders_s3(pool: dict) -> list[dict]:
    from lab.kalshi.strategies import r4_earnings_seeded_book_48h_forward as E4
    base, _ = E4.archive_orders(0)
    rows = {r["t"]: r for r in E4.arm_rows(base, None, "through")}
    listing = _event_listing(pool)
    plan = {r["t"]: r for r in json.loads((S / "r4_earnings_seeded_book_48h_forward/plan.json").read_text())["rows"]}
    out = []
    for o in base:
        r = rows[o["t"]]
        if r["f"] is None:
            continue                                    # unresolved fill evidence: dropped, as in round 4
        px = o["q"]; no_won = bool(o["won"])
        out.append({"sample": "S3_r4_armA", "t": o["t"], "e": o["e"], "series": o["series"], "spk": spk_key(o["series"]),
                    "w": wkey(plan[o["t"]]["word"]), "listing": listing.get(o["e"], o["open"]), "post": o["post"], "s": o["s"], "px": px,
                    "N": o["N"], "ask": o["ask"], "bid": o["bid"], "filled": r["f"] > 0, "q": float(r["f"]), "fill_class": None, "no_won": no_won,
                    "ret": ((1.0 if no_won else 0.0) - px) / px, "tclose": o["tclose"], "rule_consistent": True})
    return out


def orders_s4(pool: dict) -> list[dict]:
    from lab.kalshi.strategies import r4_single_appearance_seeded_books as S4
    M = {json.loads(l)["ticker"]: json.loads(l) for l in (S / "r4_single_appearance_seeded_books/markets.jsonl").open()}
    listing = _event_listing(pool)
    out = []
    for l in (S / "r4_single_appearance_seeded_books/orders.jsonl").open():
        o = json.loads(l)
        if not o["posted"]:
            continue
        sc = S4.score(o, "P", "sell", "thr")
        m = M[o["t"]]; no_won = not o["yes"]
        out.append({"sample": "S4_r4_V1", "t": o["t"], "e": o["e"], "series": o["series"], "spk": spk_key(o["series"]),
                    "w": wkey(_word_of(m)), "listing": listing.get(o["e"], ts(m.get("open_time"))), "post": o["tq"], "s": o["s"],
                    "px": o["px"], "N": o["N"], "ask": o["ask"], "bid": o["bid"], "filled": sc is not None, "q": float(sc["q"]) if sc else 0.0,
                    "fill_class": None, "no_won": no_won, "ret": ((1.0 if no_won else 0.0) - o["px"]) / o["px"], "tclose": o["ev_last_close"],
                    "rule_consistent": True, "tier": o["tier"]})
    return out


def all_orders(pool: dict | None = None) -> list[dict]:
    pool = pool if pool is not None else load_pool()
    s1 = orders_s1()
    lst = _event_listing(pool)
    for o in s1:
        o["listing"] = min(o["listing"], lst.get(o["e"], o["listing"]))
    out = s1 + orders_s2(pool) + orders_s3(pool) + orders_s4(pool)
    # one order per ticker across samples (the first sample listed keeps it)
    seen, uniq = set(), []
    for o in out:
        if o["t"] in seen:
            continue
        seen.add(o["t"]); uniq.append(o)
    return uniq


def annotate(orders: list[dict], hist: History) -> None:
    for o in orders:
        k, n = hist.prior(o["spk"], o["w"], o["listing"], o["e"])
        o["k"], o["n"] = k, n
        o["rate"] = k / n if n else None


# ------------------------------------------------------------------------------------------------ missing listings
def _log(path: str, nbytes: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "calls.log").open("a") as f:
        f.write(f"{int(time.time())}\t{nbytes}\t{path}\n")


def kget(path: str) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    fn = CACHE / (hashlib.sha1(path.encode()).hexdigest()[:24] + ".json")
    if fn.exists():
        return json.loads(fn.read_text())
    from lab.us.data_refresh import fetch
    text = fetch(BASE + path, pace=1.15)
    _log(path, len(text or ""))
    if not text:
        raise RuntimeError(f"empty response for {path}")
    x = json.loads(text)
    fn.write_text(json.dumps(x))
    return x


def fetch_missing(series: list[str], max_calls: int = 30) -> int:
    used = 0
    for s in series:
        cursor = None
        for _ in range(4):
            if used >= max_calls:
                return used
            p = f"/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else "")
            fn = CACHE / (hashlib.sha1(p.encode()).hexdigest()[:24] + ".json")
            fresh = not fn.exists()
            x = kget(p)
            used += fresh
            for m in x.get("markets", []) or []:
                m.setdefault("series", s)
            if fresh:
                fn.write_text(json.dumps(x))
            cursor = x.get("cursor")
            if not cursor or not x.get("markets"):
                break
    return used
