"""Adversarial reproduction of r6_near_deadline_nothing_happens_census, written from the claim's description only
(the original code was not read or imported).

Claim: in 'will X happen by D' (hazard) markets, taker NO bought at 0.70-0.95 one to seven days before the deadline D
wins more often than its price implies. Frozen validation candidates (split at deadline D = 2026-06-11 00:00 ET):
  C1  NO 0.80-0.95 at D-72h, news topics (everything except price-touch ladders)
  C2  NO 0.70-0.95 at D-24h, news topics
  C3  NO 0.50-0.97 at D-120h, all topics

Own implementation:
  universe   every settled binary market in ANY cached Kalshi listing under data/kalshi_lab/strategies/ (all
             researchers) plus this script's own listing calls, whose early_close_condition says it closes early when
             the event happens (data releases, winners, mentions, charts, awards ... excluded), with deadline D parsed
             from rules_primary ('before <date>' = that midnight ET; 'by/through/and <date>' = the following midnight
             ET; explicit times honoured), and latest_expiration_time within [D-1d, D+16d] (deadline sanity check).
             Excluded: mention, rain/snow accumulator and MVE combo series.
  decision   t = D - h hours; the market must be listed and still open at t (open_time <= t < close_time; both are
             observable at t). Signal = last hourly candle with end_period_ts <= t (Kalshi emits an hourly candle only
             when the book or a trade changes, so the last one is the current book; carried forward <= 48 h by default).
  fill       taker NO at 1 - yes_bid of the last candle at or before t + 60 s (main) or t + 1 h (variant); fee
             0.07*p*(1-p)*fee_multiplier, rounded up to the cent on a 10-contract order.
  stats      equal-$ return per trade, t clustered by event_ticker, ret without the 3 best trades, halves split at
             the median D, exact-binomial (Clopper-Pearson) 95% one-sided lower bound on the per-event win rate turned
             into a per-$ return bound (gate amendment d).

Kalshi calls go through lab.us.data_refresh.fetch (pace 1.15 s, bot idle window), are cached under
data/kalshi_lab/strategies/r6_near_deadline_nothing_happens_census/repro/api_cache/ and are capped at BUDGET in total.
Identical URLs already fetched by the original researcher are read from their raw response cache (raw API data only).

Usage: .venv/bin/python -m lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_repro [scan|list|candles|eval|all]
"""
from __future__ import annotations
import calendar, hashlib, json, math, re, statistics as st, sys, time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
ROOT = Path("data/kalshi_lab/strategies/r6_near_deadline_nothing_happens_census")
OUT = ROOT / "repro"
CACHE = OUT / "api_cache"
THEIR_CACHE = ROOT / "api_cache"
CALLS = OUT / "calls.log"
K = "https://api.elections.kalshi.com/trade-api/v2"
BUDGET = 195
CUT_D = 1781150400            # 2026-06-11 00:00 ET, the claimed 70/30 split on D
NOW = int(time.time())
HOURS = (24, 72, 120)
CANDS = {"C1_h72_NO80-95_news": (72, 0.80, 0.95, "news"),
         "C2_h24_NO70-95_news": (24, 0.70, 0.95, "news"),
         "C3_h120_NO50-97_all": (120, 0.50, 0.97, "all")}

# ----------------------------------------------------------------------------------------------- time / deadline
MON = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MON.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m}); MON["sept"] = 9


def _nth_sunday(y: int, m: int, n: int):
    days = [d for d in calendar.Calendar().itermonthdates(y, m) if d.month == m and d.weekday() == 6]
    return days[n - 1]


def et_ts(y: int, mo: int, d: int, hh: int = 0, mi: int = 0) -> int:
    day = datetime(y, mo, d).date()
    off = 4 if _nth_sunday(y, 3, 2) <= day < _nth_sunday(y, 11, 1) else 5
    return int(datetime(y, mo, d, hh, mi, tzinfo=timezone.utc).timestamp()) + off * 3600


MP = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december)\.?"
TIME = r"(?:\s*(?:at|,)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:et|est|edt)?)?"
RX_THROUGH = re.compile(r"through\s+(\d{1,2}):(\d{2})\s*(am|pm)\s*et\s+on\s+" + MP + r"\s+(\d{1,2}),?\s+(\d{4})", re.I)
RX_DATE = [(how, re.compile(rf"\b{w}\s+" + MP + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})" + TIME, re.I))
           for how, w in (("before", "before"), ("by", "by"), ("and", "(?:and|to|until|through)"))]
RX_MONTH = re.compile(r"\bbefore\s+" + MP + r"\s+(\d{4})\b", re.I)


def _hm(h, mi, ap):
    h = int(h); mi = int(mi or 0)
    if ap.lower() == "pm" and h != 12:
        h += 12
    if ap.lower() == "am" and h == 12:
        h = 0
    return h, mi


def deadline(rules: str) -> tuple[int | None, str | None]:
    if not rules:
        return None, None
    r = rules.replace("\xa0", " ")
    m = RX_THROUGH.search(r)
    if m:
        h, mi = _hm(m.group(1), m.group(2), m.group(3))
        t = et_ts(int(m.group(6)), MON[m.group(4).lower()], int(m.group(5)), h, mi)
        return t + (60 if (h, mi) == (23, 59) else 0), "through"
    for how, rx in RX_DATE:
        m = rx.search(r)
        if not m:
            continue
        mo, d, y = MON[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        if m.group(4):
            h, mi = _hm(m.group(4), m.group(5), m.group(6))
            return et_ts(y, mo, d, h, mi) + (60 if (h, mi) == (23, 59) else 0), how + "_time"
        if how == "before":
            return et_ts(y, mo, d), how
        nd = datetime(y, mo, d) + timedelta(days=1)
        return et_ts(nd.year, nd.month, nd.day), how
    m = RX_MONTH.search(r)
    if m:
        return et_ts(int(m.group(2)), MON[m.group(1).lower()], 1), "before_month"
    return None, None


def iso(s) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1); frac = re.match(r"\d+", b).group(0); s = a + b[len(frac):]
    return int(datetime.fromisoformat(s).timestamp())


# ----------------------------------------------------------------------------------------------- universe
HAZ = re.compile(r"expire early if|close early if|if this event occurs|expire once the event occurs|close and expire early once (?:the )?(?:event|person)", re.I)
EXCL_ECC = re.compile(r"economic data|data is released|word or phrase|billboard|chart|winner|polling average|federal register|usgs|"
                      r"eliminated|setlist|advertisement|award|title holder|is stated|is said|announced on|nominations", re.I)
EXCL_SER = re.compile(r"MENTION|RAIN|SNOW|MVE|^KXYT|TOPSONG|TOPALBUM|RANKLIST|JOBLESS|ECONSTAT|FOMEN|TVSTATS|TOTAL$|SPREAD$|GAME$|MATCH$")
PRICE = re.compile(r"price of (?:bitcoin|btc|ethereum|eth|oil|gold|silver|solana)|spot price|real-time index|ICE reports|front-month|"
                   r"S&P|Nasdaq|Dow Jones|settle prices|\bprice of\b", re.I)
TOPICS = [("price_touch", PRICE),
          ("launch_release", re.compile(r"release|launch|album|trailer|starship|falcon|dragon|crew-|iphone|gta|model called|gemini|gpt|claude|"
                                        r"grok|llama|deepseek|ipo|spacex|artemis|vulcan|wrapped|switch", re.I)),
          ("shutdown_congress", re.compile(r"shut ?down|funding|appropriation|continuing resolution|reconciliation|rescission|fisa|vote[sd]? |"
                                           r"bill|law\b|senate|house |congress|filibuster|impeach|discharge", re.I)),
          ("fed_macro", re.compile(r"federal reserve|fed |fomc|powell|rate (?:cut|hike)|recession|hikes", re.I)),
          ("cabinet_personnel", re.compile(r"leaves? as|leave(?:s)? (?:office|their|or)|out as|resign|fired|confirmed|nominee|nominat|director|secretary", re.I)),
          ("trump_actions", re.compile(r"trump|pardon|executive order|tariff", re.I)),
          ("geopolitics", re.compile(r"iran|israel|china|russia|ukraine|hormuz|ceasefire|venezuela|maduro|khamenei|nuclear|greenland|war\b|leader", re.I))]


def topic(m: dict) -> str:
    txt = (m.get("rules_primary") or "") + " " + (m.get("title") or "")
    for name, rx in TOPICS:
        if rx.search(txt):
            return name
    return "culture_other"


def series_of(m: dict) -> str:
    return m["event_ticker"].split("-")[0]


KEEP = ("ticker", "event_ticker", "title", "rules_primary", "early_close_condition", "open_time", "close_time",
        "expected_expiration_time", "latest_expiration_time", "result", "market_type", "yes_sub_title", "volume", "volume_fp", "status")


def scan() -> dict:
    """Every market object with an early_close_condition in any cached listing (all researchers + own calls)."""
    out: dict[str, dict] = {}

    def walk(o, src):
        if isinstance(o, dict):
            if "ticker" in o and "early_close_condition" in o and "event_ticker" in o:
                d = {k: o.get(k) for k in KEEP}; d["_src"] = src
                p = out.get(o["ticker"])
                if p is None or (p.get("result") not in ("yes", "no") and d.get("result") in ("yes", "no")):
                    out[o["ticker"]] = d
                return
            for v in o.values():
                walk(v, src)
        elif isinstance(o, list):
            for v in o:
                walk(v, src)

    # census responses hold only the markets that closed at the census minute (NO outcomes): never a universe source
    skip = {hashlib.sha1(f"{K}/markets?status=settled&min_close_ts={X - 30}&max_close_ts={X + 30}&limit=1000".encode()).hexdigest()[:24] + ".json"
            for X in CENSUS}
    for p in sorted(Path("data/kalshi_lab/strategies").rglob("*")):
        if p.suffix not in (".json", ".jsonl") or not p.is_file() or p.name in skip:
            continue
        try:
            txt = p.read_text()
        except Exception:
            continue
        if "early_close_condition" not in txt:
            continue
        try:
            if p.suffix == ".jsonl":
                for line in txt.splitlines():
                    if "early_close_condition" in line:
                        try:
                            walk(json.loads(line), str(p))
                        except Exception:
                            pass
            else:
                walk(json.loads(txt), str(p))
        except Exception:
            pass
    return out


def universe(M: dict) -> list[dict]:
    fees = fee_table()
    U = []
    for m in M.values():
        if m.get("result") not in ("yes", "no") or (m.get("market_type") or "binary") != "binary":
            continue
        ecc = m.get("early_close_condition") or ""
        s = series_of(m)
        if not HAZ.search(ecc) or EXCL_ECC.search(ecc) or EXCL_SER.search(s):
            continue
        D, how = deadline(m.get("rules_primary") or "")
        if D is None:
            continue
        lat = iso(m.get("latest_expiration_time"))
        sane = lat is not None and D - 86400 <= lat <= D + 16 * 86400
        o, c = iso(m.get("open_time")), iso(m.get("close_time"))
        if o is None or c is None:
            continue
        ft, fm = fees.get(s, ("quadratic", 1.0))
        U.append({"t": m["ticker"], "e": m["event_ticker"], "s": s, "D": D, "D_how": how, "sane": sane, "open": o, "close": c,
                  "result": m["result"], "topic": topic(m), "fee_mult": fm, "src": m["_src"].split("/")[3] if m["_src"].count("/") > 3 else m["_src"]})
    return U


def fee_table() -> dict:
    S = json.loads(Path("data/kalshi_lab/series_all.json").read_text())
    rows = list(S.values())[0] if isinstance(S, dict) else S
    return {r["ticker"]: (r.get("fee_type"), float(r.get("fee_multiplier") if r.get("fee_multiplier") is not None else 1)) for r in rows}


# ----------------------------------------------------------------------------------------------- Kalshi I/O
def ncalls() -> int:
    return len(CALLS.read_text().splitlines()) if CALLS.exists() else 0


def kget(url: str) -> dict | None:
    key = hashlib.sha1(url.encode()).hexdigest()[:24] + ".json"
    for d in (CACHE, THEIR_CACHE):
        f = d / key
        if f.exists():
            try:
                return json.loads(f.read_text())
            except Exception:
                pass
    if ncalls() >= BUDGET:
        print("BUDGET reached; skip", url[:120], flush=True); return None
    from lab.us.data_refresh import fetch
    txt = fetch(url, pace=1.15)
    CACHE.mkdir(parents=True, exist_ok=True)
    with CALLS.open("a") as w:
        w.write(f"{datetime.now(timezone.utc).isoformat()[:19]}\t{len(txt)}\t{url}\n")
    if not txt:
        return None
    (CACHE / key).write_text(txt)
    try:
        return json.loads(txt)
    except Exception:
        return None


def _num(x):
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def parse_candles(lst: list) -> list[tuple]:
    out = []
    for c in lst or []:
        yb = c.get("yes_bid") or {}; ya = c.get("yes_ask") or {}
        bid = _num(yb.get("close_dollars", yb.get("close"))); ask = _num(ya.get("close_dollars", ya.get("close")))
        if bid is not None and bid > 1.5:   # cents format
            bid /= 100
        if ask is not None and ask > 1.5:
            ask /= 100
        out.append((int(c["end_period_ts"]), ask, bid, _num(c.get("volume_fp", c.get("volume")))))
    return sorted(out)


def their_candles() -> dict[str, list[tuple]]:
    """Raw candle responses the original researcher fetched (identified by URL in their calls.log)."""
    got: dict[str, dict] = defaultdict(dict)
    log = ROOT / "calls.log"
    if not log.exists():
        return {}
    for line in log.read_text().splitlines():
        u = line.split("\t")[-1].strip()
        if "candlesticks" not in u:
            continue
        f = THEIR_CACHE / (hashlib.sha1(u.encode()).hexdigest()[:24] + ".json")
        if not f.exists():
            continue
        d = json.loads(f.read_text())
        if "/historical/markets/" in u:
            t = u.split("/historical/markets/")[1].split("/")[0]
            for c in parse_candles(d.get("candlesticks")):
                got[t][c[0]] = c
        else:
            for x in d.get("markets") or []:
                for c in parse_candles(x.get("candlesticks")):
                    got[x["market_ticker"]][c[0]] = c
    return {t: sorted(v.values()) for t, v in got.items()}


def own_candles() -> dict[str, list[tuple]]:
    got: dict[str, dict] = defaultdict(dict)
    if not CALLS.exists():
        return {}
    for line in CALLS.read_text().splitlines():
        u = line.split("\t")[-1].strip()
        if "candlesticks" not in u:
            continue
        f = CACHE / (hashlib.sha1(u.encode()).hexdigest()[:24] + ".json")
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if "/historical/markets/" in u:
            t = u.split("/historical/markets/")[1].split("/")[0]
            for c in parse_candles(d.get("candlesticks")):
                got[t][c[0]] = c
        elif "/events/" in u:   # event endpoint: omits markets that closed early, so it is only a supplement
            for t, lst in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                for c in parse_candles(lst):
                    got[t][c[0]] = c
        else:
            for x in d.get("markets") or []:
                for c in parse_candles(x.get("candlesticks")):
                    got[x["market_ticker"]][c[0]] = c
    return {t: sorted(v.values()) for t, v in got.items()}


# ----------------------------------------------------------------------------------------------- decisions
def fee(p: float, mult: float = 1.0, n: int = 10) -> float:
    raw = 0.07 * p * (1 - p) * mult * n
    return math.ceil(raw * 100 - 1e-9) / 100 / n if raw > 0 else 0.0


def last_at(c: list[tuple], t: int, max_age_h: float) -> tuple | None:
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > max_age_h * 3600 or best[2] is None:
        return None
    return best


def decisions(U: list[dict], C: dict, max_age_h: float = 48, fill_delay: int = 60) -> list[dict]:
    out = []
    for m in U:
        c = C.get(m["t"])
        for h in HOURS:
            t = m["D"] - h * 3600
            if not (m["open"] <= t < m["close"]):
                continue
            row = {"t": m["t"], "e": m["e"], "s": m["s"], "D": m["D"], "h": h, "topic": m["topic"], "sane": m["sane"], "has_c": c is not None}
            if c is None:
                out.append({**row, "fill": False, "why": "no candles"}); continue
            sig = last_at(c, t, max_age_h)
            fl = last_at(c, t + fill_delay, max_age_h)
            if sig is None or fl is None:
                out.append({**row, "fill": False, "why": "no fresh quote"}); continue
            sp = 1 - sig[2]; px = round(1 - fl[2], 4)
            if not (0.005 <= px <= 0.995):
                out.append({**row, "fill": False, "why": "px out of range", "sig": sp, "px": px}); continue
            f = fee(px, m["fee_mult"]); won = m["result"] == "no"
            pnl = (1.0 if won else 0.0) - px - f
            out.append({**row, "fill": True, "sig": round(sp, 4), "px": px, "fee": f, "won": won, "ret": pnl / px,
                        "age_h": round((t - sig[0]) / 3600, 2),
                        "vol_sig": sum(r[3] or 0 for r in c if t - 86400 < r[0] <= t)})   # contracts traded in the 24 h before t
    return out


# ----------------------------------------------------------------------------------------------- statistics
def binom_lo(k: int, n: int, a: float = 0.05) -> float:
    """One-sided (1-a) Clopper-Pearson lower bound on p given k successes in n."""
    if k <= 0:
        return 0.0
    if k == n:
        return a ** (1 / n)
    lo, hi = 0.0, 1.0
    for _ in range(80):
        p = (lo + hi) / 2
        tail = sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
        if tail < a:
            lo = p
        else:
            hi = p
    return (lo + hi) / 2


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r)
    em = [st.mean(x["ret"] for x in v) for v in ev.values()]
    ne = len(em)
    sd = st.pstdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne)) if ne > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    Ds = sorted(r["D"] for r in rows); mid = Ds[len(Ds) // 2]
    h1 = [r["ret"] for r in rows if r["D"] < mid]; h2 = [r["ret"] for r in rows if r["D"] >= mid]
    # event-level win = every trade of the event won (an event either happened or not before its deadlines)
    ewin = sum(all(x["won"] for x in v) for v in ev.values())
    epx = st.mean(st.mean(x["px"] for x in v) for v in ev.values())
    lo = binom_lo(ewin, ne)
    span_days = max(1.0, (max(Ds) - min(Ds)) / 86400)
    return {"n": len(rows), "events": ne, "series": len({r["s"] for r in rows}), "win": round(st.mean(r["won"] for r in rows), 4),
            "avg_px": round(st.mean(r["px"] for r in rows), 4), "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4),
            "t": round(t, 2) if t == t else None, "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "event_wins": ewin, "cp_win_lo95": round(lo, 4), "beta_lb_ret": round((lo - epx - fee(epx)) / epx, 4),
            "trades_per_day": round(len(rows) / span_days, 3),
            "topic_n": dict(Counter(r["topic"] for r in rows)), "losses": [r["t"] for r in rows if not r["won"]],
            "median_vol24_contracts": st.median([r["vol_sig"] for r in rows if r.get("vol_sig") is not None] or [0])}


def cell(dec: list[dict], h: int, lo: float, hi: float, stratum: str, period: str, first_only: bool = False) -> list[dict]:
    rows = [d for d in dec if d.get("fill") and d["h"] == h and lo <= d["sig"] <= hi and d["sane"]]
    if stratum == "news":
        rows = [d for d in rows if d["topic"] != "price_touch"]
    if period == "val":
        rows = [d for d in rows if d["D"] >= CUT_D]
    elif period == "disc":
        rows = [d for d in rows if d["D"] < CUT_D]
    return rows


# ----------------------------------------------------------------------------------------------- stages
def load_universe() -> list[dict]:
    f = OUT / "universe.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def stage_scan() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    M = scan(); U = universe(M)
    with (OUT / "universe.jsonl").open("w") as w:
        for u in U:
            w.write(json.dumps(u) + "\n")
    v = [u for u in U if CUT_D <= u["D"] <= NOW]
    print(f"scanned markets {len(M)}; hazard universe {len(U)}; validation-window D {len(v)} in {len({u['e'] for u in v})} events, "
          f"{len({u['s'] for u in v})} series", flush=True)


def stage_list(series: list[str]) -> None:
    """Own listing calls: live tier (settled, closed since the CUT) and archive tier, for the given series."""
    for s in series:
        kget(f"{K}/markets?series_ticker={s}&status=settled&min_close_ts={CUT_D - 6 * 86400}&limit=1000")
        kget(f"{K}/historical/markets?series_ticker={s}&min_close_ts={CUT_D - 6 * 86400}&limit=1000")


CENSUS = [et_ts(2026, mo, d) - 60 for mo, d in ((8, 16), (8, 21), (9, 1), (9, 8), (9, 16), (9, 23), (9, 30), (10, 1))] + \
         [et_ts(2026, 9, 1, 10), et_ts(2026, 10, 1, 10)]


def stage_census(max_series: int) -> list[str]:
    """Universe expansion independent of anyone's series choice: every market that settled at 23:59 ET (or 10:00 ET)
    on sampled deadline days (one /markets call each), keep the hazard series not yet in any cache, then list each such
    series in the live tier (settled since 2026-08-02), which includes its early-closed (YES) markets.
    Residual bias: a series with no NO market closing in the census windows is missed (it favours the claim)."""
    U = load_universe(); known = {u["s"] for u in U}
    found = Counter()
    for X in CENSUS:
        d = kget(f"{K}/markets?status=settled&min_close_ts={X - 30}&max_close_ts={X + 30}&limit=1000") or {}
        for m in d.get("markets") or []:
            ecc = m.get("early_close_condition") or ""; s = series_of(m)
            if (m.get("market_type") or "binary") == "binary" and HAZ.search(ecc) and not EXCL_ECC.search(ecc) and not EXCL_SER.search(s) \
                    and deadline(m.get("rules_primary") or "")[0] is not None:
                found[s] += 1
    new = sorted((s for s in found if s not in known), key=lambda s: hashlib.sha1(s.encode()).hexdigest())
    print(f"census: hazard series seen {len(found)}, new (not cached) {len(new)}", flush=True)
    (OUT / "census_series.json").write_text(json.dumps({"seen": found, "new": new}, indent=1))
    for s in new[:max_series]:
        kget(f"{K}/markets?series_ticker={s}&status=settled&min_close_ts={HIST_CUT - 6 * 86400}&limit=1000")
    return new


def need_candles(U: list[dict], period: str = "val") -> list[dict]:
    out = []
    for m in U:
        if not m["sane"] or (period == "val" and not (CUT_D <= m["D"] <= NOW)):
            continue
        if any(m["open"] <= m["D"] - h * 3600 < m["close"] for h in HOURS):
            out.append(m)
    return out


HIST_CUT = 1786147200    # 2026-08-08 00:00 UTC: markets settled before this live only in the archive tier


def stage_candles(max_calls: int) -> None:
    U = load_universe(); have = {**their_candles(), **own_candles()}
    need = [m for m in need_candles(U) if m["t"] not in have]
    print(f"validation markets needing candles: {len(need)}", flush=True)
    start_calls = ncalls()
    live = [m for m in need if m["close"] >= HIST_CUT + 86400]
    hist = [m for m in need if m["close"] < HIST_CUT + 86400]
    # live tier: batch by deadline window; span = max(D)+1h - (min(D) - 170h); <= 9,500 candles per call
    live.sort(key=lambda m: m["D"])
    i = 0
    while i < len(live) and ncalls() - start_calls < max_calls:
        grp = [live[i]]; j = i + 1
        while j < len(live):
            g2 = grp + [live[j]]
            span_h = (max(x["D"] for x in g2) + 3600 - (min(x["D"] for x in g2) - 170 * 3600)) / 3600
            if len(g2) * span_h > 9000 or len(g2) > 90:
                break
            grp = g2; j += 1
        a = min(x["D"] for x in grp) - 170 * 3600; b = max(x["D"] for x in grp) + 3600
        kget(f"{K}/markets/candlesticks?market_tickers={','.join(x['t'] for x in grp)}&start_ts={a}&end_ts={b}&period_interval=60")
        i = j
    for m in sorted(hist, key=lambda m: (m["topic"] == "price_touch", hashlib.sha1(m["t"].encode()).hexdigest())):
        if ncalls() - start_calls >= max_calls:
            break
        a = m["D"] - 170 * 3600; b = min(m["D"] + 3600, m["close"] + 3600)
        kget(f"{K}/historical/markets/{m['t']}/candlesticks?start_ts={a}&end_ts={b}&period_interval=60")
    print(f"calls used so far {ncalls()}", flush=True)


def census_series() -> set:
    f = OUT / "census_series.json"
    if not f.exists():
        return set()
    listed = {u.split("series_ticker=")[1].split("&")[0] for u in (l.split("\t")[-1] for l in CALLS.read_text().splitlines())
              if "series_ticker=" in u}
    return set(json.loads(f.read_text())["new"]) & listed


def stage_eval() -> dict:
    U_all = load_universe(); cen = census_series()
    universes = {"base": [u for u in U_all if u["s"] not in cen],          # every cached listing (the claim's sources and more)
                 "expanded": U_all}                                       # + series discovered by the close-time census
    C = {**their_candles(), **own_candles()}
    variants = {"main_age48_fill+1m": (48, 60), "strict_age0.5_fill+1m": (0.5, 60), "age48_fill+1h": (48, 3600)}
    res = {"kalshi_calls_used": ncalls(), "census_series_listed": sorted(cen), "candle_markets": len(C), "universes": {}}
    for un, U in universes.items():
        res["universes"][un] = {"markets": len(U), "val_window_markets": sum(CUT_D <= u["D"] <= NOW for u in U), "variants": {}}
        for vn, (age, delay) in variants.items():
            dec = decisions(U, C, age, delay)
            vd = [d for d in dec if CUT_D <= d["D"] <= NOW and d["sane"]]
            out = {"val_decisions": len(vd), "val_fills": sum(d.get("fill", False) for d in vd),
                   "val_unfilled_reasons": dict(Counter(d.get("why") for d in vd if not d.get("fill"))), "candidates": {}}
            for name, (h, lo, hi, stratum) in CANDS.items():
                out["candidates"][name] = {"validation": stats(cell(dec, h, lo, hi, stratum, "val")),
                                           "discovery_partial_candles": stats(cell(dec, h, lo, hi, stratum, "disc"))}
            res["universes"][un]["variants"][vn] = out
            if vn == "main_age48_fill+1m":
                with (OUT / f"decisions_{un}.jsonl").open("w") as w:
                    for d in dec:
                        w.write(json.dumps(d) + "\n")
            print(f"\n== {un} / {vn}: validation decisions {out['val_decisions']}, fills {out['val_fills']}, unfilled {out['val_unfilled_reasons']}")
            for name, x in out["candidates"].items():
                v = x["validation"]
                if v["n"]:
                    print(f"  {name:22s} n={v['n']:3d} ev={v['events']:3d} win={v['win']:.3f} px={v['avg_px']:.3f} ret={v['ret_per_dollar']:+.4f} t={v['t']} "
                          f"wo3={v['ret_wo3']} h1={v['half1']} h2={v['half2']} beta_lb={v['beta_lb_ret']} vol24={v['median_vol24_contracts']:.0f} losses={v['losses']}")
    (OUT / "eval.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "eval"
    if a in ("scan", "all"):
        stage_scan()
    if a == "census":
        stage_census(int(sys.argv[2]) if len(sys.argv) > 2 else 40)
        stage_scan()
    if a == "list":
        stage_list(sys.argv[2].split(","))
        stage_scan()
    if a in ("candles", "all"):
        stage_candles(int(sys.argv[2]) if len(sys.argv) > 2 and a == "candles" else 60)
    if a in ("eval", "all"):
        stage_eval()
