"""Data for r6_near_deadline_nothing_happens_census: settled 'will X happen by D' (hazard) markets and their hourly
quotes in the last days before the deadline.

Universe (listing fields only, never prices or results; hazard rule widened once, after the first listings and before any candle was read, to every "close and expire early if ..." condition except data releases, winners and announcements):
  * binary market, settled yes/no, not a multivariate combo;
  * hazard market: early_close_condition says the market closes early when the event occurs ('close and expire early
    if the event occurs', '... for any person', '... if the specific individual leaves', '... if the vote occurs',
    'If this event occurs, the market will close ...'); data-release, winner and title-holder markets are excluded;
  * deadline D parsed from rules_primary (text fixed at listing): 'before <date>' -> date 00:00 ET; 'by / on or before /
    through / until / between Issuance and <date>' -> next day 00:00 ET; 'before <Month YYYY>' -> 1st of that month;
    'by|in <Month YYYY>' / 'by the end of <Month YYYY>' -> 1st of the next month; 'before YYYY' -> Jan 1 YYYY;
    'by|in|during YYYY' -> Jan 1 of YYYY+1. Sanity (outcome-free): latest_expiration_time within [D - 1 d, D + 16 d];
  * series frequency taken from the /series catalog (one_off / custom / annual is the pre-registered universe;
    weekly / monthly hazards are kept as a separate stratum).
Never used for selection: close_time beyond 'is the market still open at the decision time', result, volume.

Sources: every Kalshi listing already cached on disk by earlier researchers (0 calls, via the r4 cache index) plus
this family's own listings: /historical/markets?series_ticker=S (archive) and /markets?series_ticker=S&status=settled
(live tier, settled after the 2026-08 archive cutoff).

Candles (hourly, period_interval=60): earlier researchers' cached hourly and 1-minute candles first (0 calls); then
/markets/candlesticks batches (<= 100 tickers, <= 9,800 candles per call) for live-tier markets and one
/historical/markets/{t}/candlesticks call per archived market over [D - 194 h, D + 1 h] (the first 28 archive calls used D - 170 h).
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_data
       [catalog|list S1,S2|frame|plan|candles N]"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_nested_deadline_term_structure_cache import index, load
from lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_api import OUT, calls_used, get

OUTP = Path(OUT)
ET = ZoneInfo("America/New_York")
H, DAY = 3600, 86400
CUTOFF = int(dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc).timestamp())   # archive / live-tier boundary (approx.)
NOW = int(dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc).timestamp())
WIN_LO, WIN_HI = 194 * H, 1 * H    # candle window [D - 194 h, D + 1 h] (first calls used D - 170 h)
DEC_H = (168, 120, 72, 48, 24)      # decision grid: deadline - 7 d, 5 d, 3 d, 2 d, 1 d (the lead plan: 3 d and 1 d)
ACCUM = re.compile(r"RAIN|SNOW")    # weather accumulators (r2 family): kept out of the primary universe, no calls spent

HAZ = re.compile(r"(close and expire early if|if this event occurs, the market will close)", re.I)
NOT_HAZ = re.compile(r"(data is released|data become|economic data|winner is declared|title holder|federal register|"
                     r"results are|is announced|are announced)", re.I)
PRICE_HAZ = re.compile(r"(price criterion|specified price|crosses the specified|price level)", re.I)
_MON = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTH = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
RX_FULL = re.compile(r"\b(before|by|through|until|prior to|and|on or before)\s+(?:11:59\s*(?:PM|pm)\s*(?:ET|EST|EDT)?\s*(?:on\s+)?)?"
                     + MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)", re.I)
RX_MONYR = re.compile(r"\b(before|by|in|during)\s+(?:the\s+end\s+of\s+)?" + MONTH + r",?\s+(20\d\d)\b", re.I)
RX_YR = re.compile(r"\b(before|by|in|during)\s+(?:the\s+end\s+of\s+)?(20\d\d)\b", re.I)


def ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _et(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, tzinfo=ET).timestamp())


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def deadline(m: dict) -> tuple[int | None, str]:
    txt = m.get("rules_primary") or ""
    x = RX_FULL.search(txt)
    if x:
        w = x.group(1).lower()
        pre = txt[max(0, x.start() - 30):x.start()].lower()
        if w == "and" and "between" not in pre:
            x = None
        if x:
            try:
                d = _et(int(x.group(4)), _MON[x.group(2)[:3].lower()], int(x.group(3)))
            except Exception:
                d = None
            if d:
                incl = w in ("by", "through", "until", "and", "on or before") or "on or" in pre[-8:]
                return (d + DAY if incl else d), "rules:" + w
    x = RX_MONYR.search(txt)
    if x:
        w = x.group(1).lower(); y = int(x.group(3)); mo = _MON[x.group(2)[:3].lower()]
        if w == "before" and "end of" not in x.group(0).lower():
            return _et(y, mo, 1), "rules:before-month"
        y2, m2 = _next_month(y, mo)
        return _et(y2, m2, 1), "rules:" + w + "-month"
    x = RX_YR.search(txt)
    if x:
        w = x.group(1).lower(); y = int(x.group(2))
        if w == "before" and "end of" not in x.group(0).lower():
            return _et(y, 1, 1), "rules:before-year"
        return _et(y + 1, 1, 1), "rules:" + w + "-year"
    return None, "none"


def catalog() -> dict[str, dict]:
    cat = {}
    try:
        for s in json.load(open("data/kalshi_lab/series_all.json"))["series"]:
            cat[s["ticker"]] = {"freq": s.get("frequency"), "cat": s.get("category"), "title": s.get("title"), "vol": 0.0,
                                "fee_type": s.get("fee_type"), "fee_mult": s.get("fee_multiplier", 1)}
    except Exception:
        pass
    for p, f in index().items():
        if p.startswith("/series?"):
            for s in load(f).get("series") or []:
                c = cat.setdefault(s["ticker"], {"freq": s.get("frequency"), "cat": s.get("category"), "title": s.get("title"),
                                                 "fee_type": s.get("fee_type"), "fee_mult": s.get("fee_multiplier", 1)})
                c["vol"] = float(s.get("volume_fp") or 0)
    return cat


def _own_listings() -> list[tuple[str, dict]]:
    out = []
    for f in sorted((OUTP / "api_cache").glob("*.json")):
        d = json.load(open(f))
        if isinstance(d, dict) and isinstance(d.get("markets"), list) and d["markets"] and "rules_primary" in d["markets"][0]:
            out.append((f.name, d))
    return out


def raw_markets() -> dict[str, tuple[str, dict]]:
    """ticker -> (tier, raw market) over every cached listing (other researchers' and this family's)."""
    M: dict[str, tuple[str, dict]] = {}
    for p, f in index().items():
        if p.startswith("/historical/markets?") or p.startswith("/markets?"):
            tier = "hist" if p.startswith("/historical") else "live"
            for m in load(f).get("markets", []) or []:
                if "ticker" in m:
                    M.setdefault(m["ticker"], (tier, m))
    log = OUTP / "calls.log"
    if log.exists():
        own = {}
        for line in log.read_text().splitlines():
            url = line.split("\t")[-1]
            h = hashlib.sha1(url.encode()).hexdigest()[:24]
            own[h] = "hist" if "/historical/markets?" in url else ("live" if "/markets?" in url else None)
        for name, d in _own_listings():
            tier = own.get(name[:-5])
            if tier:
                for m in d["markets"]:
                    M[m["ticker"]] = (tier, m)
    return M


def topic(series: str, title: str, cat: str | None) -> str:
    """Coarse topic family (one news story drives several series). Fixed list, written before any price was read."""
    s = (series + " " + (title or "")).upper()
    rules = [("price_touch", r"BTC|ETH|BITCOIN|ETHEREUM|SOL\b|DOGE|XRP|WTI|OIL|GOLD|INX|NASDAQ|S&P|DOW|SPX|MAXY|MINY|MAX1|MINM|MAXM|SHIBA|CRYPTO PRICE"),
             ("fed_macro", r"FED|RATE|POWELL|WARSH|RECESS|GDP|CPI|INFLATION|TARIFF|TREASURY|DEBT"),
             ("shutdown_congress", r"SHUTDOWN|GOVTFUND|GOVFUND|DHS|FUNDING|RECNC|RECISSION|RESCISSION|RECONCILIATION|SENATE|HOUSE|CONGRESS|FISA|BILL|VOTE|SAVEACT|CLARITY|EPSTEIN|ACT\b"),
             ("cabinet_personnel", r"OUT\b|LEAVE|CABOUT|ADMIN|RESIGN|FIRED|CONFIRM|NOMINAT|SECRETARY|SEC[A-Z]{2,}|COUNT\b|BONDI|KASH|HEGSETH|NOEM|GABBARD|LUTNICK|WALZ|COOK|PATEL"),
             ("trump_actions", r"TRUMP|PARDON|EXECUTIVE ORDER|EO\b|ATTEND|VISIT|MEET|PUTIN|XI\b|CHINA"),
             ("geopolitics", r"IRAN|ISRAEL|GAZA|UKRAINE|RUSSIA|HORMUZ|CEASEFIRE|KHAMENEI|MADURO|VENEZUELA|GREENLAND|NATO|WAR\b|STRIKE|LEADER"),
             ("launch_release", r"STARSHIP|SPACEX|LAUNCH|RELEASE|ALBUM|GTA|GPT|GEMINI|IPHONE|IPO|WRAPPED|MODEL|LLM|OPENAI|APPLE|TESLA|ROBOTAXI"),
             ("weather_nature", r"HURRICANE|HURCAT|STORM|SNOW|QUAKE|EARTHQUAKE|TEMP|HEAT|TORNADO|VOLCANO|RAIN"),
             ("culture_other", r".")]
    for name, rx in rules:
        if re.search(rx, s):
            return name
    return "culture_other"


def compact(tier: str, m: dict, cat: dict) -> dict | None:
    """Universe row (listing fields; result kept only for scoring), else None."""
    if m.get("market_type") not in (None, "binary") or m.get("result") not in ("yes", "no"):
        return None
    if m["ticker"].startswith("KXMVE") or m["event_ticker"].startswith("KXMVE"):
        return None
    ecc = m.get("early_close_condition") or ""
    if not HAZ.search(ecc) or NOT_HAZ.search(ecc):
        return None
    D, how = deadline(m)
    if D is None:
        return None
    le = ts(m.get("latest_expiration_time"))
    if le is not None and not (D - DAY <= le <= D + 16 * DAY):
        return None
    ser = m["event_ticker"].split("-")[0]
    c = cat.get(ser, {})
    op, cl = ts(m.get("open_time")), ts(m.get("close_time"))
    if op is None or cl is None:
        return None
    return {"t": m["ticker"], "e": m["event_ticker"], "series": ser, "tier": tier, "freq": c.get("freq"), "cat": c.get("cat"),
            "fee_type": c.get("fee_type"), "fee_mult": c.get("fee_mult", 1) or 1,
            "topic": "price_touch" if PRICE_HAZ.search(ecc) else topic(ser, c.get("title") or m.get("title") or "", c.get("cat")),
            "open": op, "close": cl, "D": D, "D_how": how, "latest_exp": le, "result": m["result"],
            "title": (m.get("title") or "")[:120], "sub": (m.get("yes_sub_title") or "")[:60]}


def frame(write: bool = True) -> list[dict]:
    cat = catalog(); M = raw_markets()
    rows = [r for r in (compact(tier, m, cat) for tier, m in M.values()) if r]
    # tradeable at decision time t = floor_hour(D - h): opened at least 1 h before t and not closed by t + 1 min
    # (whether it is still open at t is observable at t; nothing later is used)
    for r in rows:
        r["dec_ok"] = [h for h in DEC_H if r["open"] <= (r["D"] - h * H) // H * H - H and r["close"] > (r["D"] - h * H) // H * H + 60]
    rows = [r for r in rows if r["dec_ok"]]
    for r in rows:
        r["accum"] = bool(ACCUM.search(r["series"]))
    if write:
        with open(OUTP / "frame.jsonl", "w") as g:
            for r in rows:
                g.write(json.dumps(r) + "\n")
    return rows


# ---------------------------------------------------------------- candles
def _px(c: dict, side: str):
    x = c.get(side) or {}
    v = x.get("close", x.get("close_dollars"))
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def rows_of(cs: list[dict]) -> list:
    """[end_ts, yes_ask, yes_bid, volume] per candle."""
    out = []
    for c in cs or []:
        try:
            v = float(c.get("volume") or c.get("volume_fp") or 0)
        except (TypeError, ValueError):
            v = 0.0
        out.append([int(c["end_period_ts"]), _px(c, "yes_ask"), _px(c, "yes_bid"), v])
    return sorted(out)


def merge(a: list, b: list) -> list:
    d = {r[0]: r for r in a}
    for r in b:
        if r[0] not in d or (d[r[0]][1] is None and r[1] is not None):
            d[r[0]] = r
    return [d[k] for k in sorted(d)]


def cached_candles(want: set[str]) -> dict[str, list]:
    """ticker -> merged candle rows from every cached candle response on disk (hourly and 1-minute)."""
    out: dict[str, list] = {}
    paths = list(index().items())
    log = OUTP / "calls.log"
    if log.exists():
        for line in log.read_text().splitlines():
            url = line.split("\t")[-1]
            f = OUTP / "api_cache" / (hashlib.sha1(url.encode()).hexdigest()[:24] + ".json")
            if f.exists():
                paths.append((url.split("trade-api/v2", 1)[-1], f))
    for p, f in paths:
        if "candlesticks" not in p:
            continue
        x = re.match(r"/historical/markets/([^/?]+)/candlesticks", p)
        try:
            if x:
                if x.group(1) in want:
                    out[x.group(1)] = merge(out.get(x.group(1), []), rows_of(load(f).get("candlesticks", [])))
            elif p.startswith("/markets/candlesticks"):
                for mk in load(f).get("markets", []) or []:
                    t = mk.get("market_ticker") or mk.get("ticker")
                    if t in want:
                        out[t] = merge(out.get(t, []), rows_of(mk.get("candlesticks", [])))
            elif "/events/" in p:
                d = load(f)
                for t, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                    if t in want:
                        out[t] = merge(out.get(t, []), rows_of(cs))
        except Exception:
            continue
    return out


MAXAGE = 48 * H   # a carried-forward quote may be at most 48 h old (hourly candles are emitted only when the book or a
                  # trade changes, so an unchanged quote is still the live quote; set before any price was read)


def last_quote(c: list, t: int, maxage: int = MAXAGE):
    """Last candle row with ts <= t and age <= maxage, else None. Rows: [ts, yes_ask, yes_bid, volume]."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > maxage:
        return None
    return best


def covers(c: list, D: int, h: int) -> bool:
    """A quote (carried forward <= 48 h) exists at the decision time D - h hours (rounded down to the hour)."""
    return last_quote(c, (D - h * H) // H * H) is not None


def plan(rows: list[dict], have: dict[str, list]) -> list[tuple]:
    todo_live, todo_hist = [], []
    for r in rows:
        if r.get("accum"):
            continue
        c = have.get(r["t"], [])
        if any(covers(c, r["D"], h) for h in r["dec_ok"]):   # partly covered markets are not re-fetched
            continue
        lo = r["D"] - WIN_LO; hi = min(r["D"] + WIN_HI, r["close"] + H)
        (todo_hist if r["tier"] == "hist" else todo_live).append((r["t"], lo, hi, r))
    batches = []
    todo_live.sort(key=lambda x: x[1])
    cur = []
    for x in todo_live:
        trial = cur + [x]
        span = (max(y[2] for y in trial) - min(y[1] for y in trial)) // H + 1
        if len(trial) > 100 or span * len(trial) > 9800:
            batches.append(cur); cur = [x]
        else:
            cur = trial
    if cur:
        batches.append(cur)
    out = [("batch", [y[0] for y in b], min(y[1] for y in b), max(y[2] for y in b)) for b in batches]
    # archive (1 call per market): price-touch markets are skipped (model-priced topic; live-tier ones come in batches).
    # One market per (event, deadline) group, chosen by sha1 of the ticker; groups ordered round-robin over events
    # (event order and in-event order by sha1), so the budget buys as many independent events as possible.
    # Never ordered by volume, price or outcome.
    hsh = lambda k: hashlib.sha1(("r6" + k).encode()).hexdigest()
    grp: dict[tuple, list] = defaultdict(list)
    for x in todo_hist:
        if x[3]["topic"] != "price_touch":
            grp[(x[3]["e"], x[3]["D"])].append(x)
    by_ev: dict[str, list] = defaultdict(list)
    for (e, D), xs in grp.items():
        by_ev[e].append(min(xs, key=lambda x: hsh(x[0])))
    for e in by_ev:
        by_ev[e].sort(key=lambda x: hsh(x[0]))
    evs = sorted(by_ev, key=hsh)
    k = 0
    while any(by_ev[e] for e in evs):
        for e in evs:
            if len(by_ev[e]) > k:
                x = by_ev[e][k]
                out.append(("hist", [x[0]], x[1], x[2]))
        k += 1
        if k > 50:
            break
    return out


def fetch_candles(rows: list[dict], max_calls: int, only: str | None = None) -> None:
    have = cached_candles({r["t"] for r in rows})
    todo = plan(rows, have)
    if only:
        todo = [x for x in todo if x[0] == only]
    start = calls_used()
    for kind, tks, lo, hi in todo:
        if calls_used() - start >= max_calls:
            print("call cap for this run reached"); break
        if kind == "hist":
            get(f"/historical/markets/{tks[0]}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
        else:
            get(f"/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={lo}&end_ts={hi}&period_interval=60")
        print(f"{kind:5s} n={len(tks):3d} {tks[0]:40s} calls {calls_used()}", flush=True)


def list_series(series: list[str], hist: bool = True, live: bool = True) -> None:
    for s in series:
        if hist:
            d = get(f"/historical/markets?series_ticker={s}&limit=1000")
            print(f"{s:28s} hist {len(d.get('markets') or []):5d} cursor={bool(d.get('cursor'))} calls {calls_used()}", flush=True)
        if live:
            d = get(f"/markets?series_ticker={s}&status=settled&min_close_ts={CUTOFF - 3 * DAY}&limit=1000")
            print(f"{s:28s} live {len(d.get('markets') or []):5d} calls {calls_used()}", flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "frame"
    if what == "frame":
        rows = frame()
        have = cached_candles({r["t"] for r in rows})
        ev = defaultdict(list)
        for r in rows:
            ev[r["e"]].append(r)
        cov = [r for r in rows if any(covers(have.get(r["t"], []), r["D"], h) for h in r["dec_ok"])]
        print("markets", len(rows), "events", len(ev), "covered markets", len(cov), "covered events", len({r["e"] for r in cov}))
        print(Counter((r["freq"], r["tier"]) for r in rows).most_common())
        print(Counter(r["topic"] for r in rows).most_common())
        print(Counter(r["series"] for r in rows).most_common(40))
    elif what == "list":
        list_series(sys.argv[2].split(","), hist="nohist" not in sys.argv, live="nolive" not in sys.argv)
    elif what == "plan":
        rows = [json.loads(l) for l in open(OUTP / "frame.jsonl")]
        pl = plan(rows, cached_candles({r["t"] for r in rows}))
        print(Counter(x[0] for x in pl), "markets in batches", sum(len(x[1]) for x in pl if x[0] == "batch"))
    elif what == "candles":
        rows = [json.loads(l) for l in open(OUTP / "frame.jsonl")]
        fetch_candles(rows, int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)


# ---------------------------------------------------------------- series selection for new listings
# Written before any listing, price or result of the selected series was read. Catalog fields only: frequency,
# category, title wording, catalog lifetime volume (series level). Rank by series volume; list each series' archive
# (/historical/markets) and live-tier settled markets unless an earlier researcher already cached that listing.
HZ_TITLE = re.compile(r"(\bby\b|before|when will|\bout\b|\bleave|release|visit|this year|this month|this week|in 20\d\d|resign|"
                      r"fired|announce|happen|occur|\breach|\bhit\b|step down|ceasefire|\bdeal\b|\bmeet|attend|\bsign|pardon|"
                      r"indict|arrest|impeach|launch|confirm|drop out|declare|recogni|\bban\b|return|travel|trip|invade|unveil|"
                      r"\bipo\b|acquire|one touch|how high|how low|tariff)", re.I)
NOT_TITLE = re.compile(r"(\bwin\b|winner|nominee|primary|election|margin|approval|ranking|chart|#1|number one|billboard|"
                       r"spotify|netflix|price range|range|above|below|cpi|inflation|payroll|jobs|unemployment|gas price|yield|"
                       r"rate\b|temperature|snow|rain|hottest|tornado|eliminat|view count|which song|debates|sweep|posts this "
                       r"week|check-ins|fed meeting)", re.I)
NEW_CALL_BUDGET = 46


def select_series() -> list[tuple[str, bool, bool]]:
    """[(series, need_hist, need_live)] in catalog-volume order until NEW_CALL_BUDGET listing calls are planned."""
    ix = index()
    have_h = {m.group(1) for p in ix for m in [re.match(r"/historical/markets\?series_ticker=([^&]+)", p)] if m}
    have_l = {m.group(1) for p in ix for m in [re.match(r"/markets\?series_ticker=([^&]+)", p)] if m}
    cat = catalog()
    rows = sorted(((c.get("vol") or 0, s, c) for s, c in cat.items()
                   if c.get("freq") in ("one_off", "custom", "annual", "weekly", "monthly") and c.get("cat") not in ("Sports", "Mentions")
                   and HZ_TITLE.search(c.get("title") or "") and not NOT_TITLE.search(c.get("title") or "")), key=lambda x: -x[0])
    out, n = [], 0
    for v, s, c in rows:
        nh, nl = s not in have_h, s not in have_l
        if not (nh or nl):
            continue
        if n + nh + nl > NEW_CALL_BUDGET:
            break
        out.append((s, nh, nl)); n += nh + nl
    return out


if __name__ == "__main__" and sys.argv[1:2] == ["select"]:
    sel = select_series()
    print(len(sel), sum(a + b for _, a, b in sel))
    for s, a, b in sel:
        print(s, "hist" if a else "", "live" if b else "")
    if "run" in sys.argv:
        for s, a, b in sel:
            list_series([s], hist=a, live=b)
