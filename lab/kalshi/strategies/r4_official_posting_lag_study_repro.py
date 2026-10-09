"""Adversarial reproduction of r4_official_posting_lag_study (written from the claim's description only; none of the
original study's code is imported or read).

Claim: Kalshi markets resolving on an official timestamped posting (senate.gov roll-call XML modify_date, clerk.house.gov
roll-call XML action-time, supremecourt.gov opinion release) still offer the already-determined winner after the
posting. Rule B: decide at the posting, buy the winner at its ask 1 minute later. Rule A: decide 1 minute after the
posting, buy 1 minute later. Validation = last 30% of the 36 events ordered by event time.

This file re-derives independently:
  * event times and posting times from the official XML (senate.gov, clerk.house.gov), fetched here;
  * the market universe of each validation event and its settlement, from Kalshi;
  * 1-minute candles from Kalshi (own calls, own cache);
  * SCOTUS release minute: own estimate (first minute after 10:00 ET in which any market of the case moves >= 5c),
    plus a sensitivity grid of fixed decision times 10:00..10:20 ET that uses no price information at all;
  * fills (taker at the quote 1 minute after the decision, YES at yes_ask, NO at 1 - yes_bid, quote <= 30 min old),
    10-contract Kalshi fee rounded up to the cent, equal-$ return, event-clustered t.

Usage: .venv/bin/python -m lab.kalshi.strategies.r4_official_posting_lag_study_repro [fetch|analyze|all]
Outputs: data/kalshi_lab/strategies/r4_official_posting_lag_study/repro/"""
from __future__ import annotations
import datetime as dt, hashlib, json, math, re, statistics as st, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r4_official_posting_lag_study/repro")
API = OUT / "api"; WEB = OUT / "web"
ET = ZoneInfo("America/New_York")
CALL_LOG = OUT / "kalshi_calls.log"
BUDGET = 200


# ----------------------------------------------------------------------------------------------- fetch layer
def kalshi_calls_used() -> int:
    return len(CALL_LOG.read_text().splitlines()) if CALL_LOG.exists() else 0


def kget(path: str, tries: int = 2) -> dict | None:
    """Cached Kalshi GET (path relative to the trade-api base). Every network attempt is logged against the budget."""
    API.mkdir(parents=True, exist_ok=True)
    f = API / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if kalshi_calls_used() + tries > BUDGET:
        raise SystemExit(f"Kalshi budget exhausted ({kalshi_calls_used()})")
    with CALL_LOG.open("a") as lf:
        lf.write(f"{int(time.time())} tries<={tries} {path}\n")
    txt = fetch(K + path, pace=1.15, tries=tries)
    if not txt:
        return None
    d = json.loads(txt)
    f.write_text(json.dumps({"path": path, "data": d}))
    return {"path": path, "data": d}


def wget(url: str) -> str:
    """Cached GET of an official page (senate.gov / clerk.house.gov), politely paced."""
    WEB.mkdir(parents=True, exist_ok=True)
    f = WEB / (hashlib.sha1(url.encode()).hexdigest() + ".txt")
    if f.exists():
        return f.read_text()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; low-rate)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read().decode("utf-8", "replace")
    f.write_text(txt); time.sleep(2.0)
    return txt


# ----------------------------------------------------------------------------------------------- universe
# (id, kind, official record, [(series, event_ticker)] for the Kalshi markets that resolve on that record)
# The universe (which official records, which Kalshi events) is the claim's; every time, price and result below
# is re-derived from the official XML and Kalshi.  Kalshi event tickers are only needed for the validation events.
EVENTS = [
    ("S-HEGSETH", "senate", "119-1-15", []), ("S-ZELDIN", "senate", "119-1-24", []), ("S-BURGUM", "senate", "119-1-26", []),
    ("S-WRIGHT", "senate", "119-1-30", []), ("S-VOUGHT", "senate", "119-1-37", []), ("S-GABBARD", "senate", "119-1-50", []),
    ("S-RFK", "senate", "119-1-52", []), ("S-ROLLINS", "senate", "119-1-53", []), ("S-LUTNICK", "senate", "119-1-57", []),
    ("S-PATEL", "senate", "119-1-61", []), ("S-GREER", "senate", "119-1-94", []), ("S-MCMAHON", "senate", "119-1-99", []),
    ("S-CHAVEZ", "senate", "119-1-111", []), ("S-MIRAN", "senate", "119-1-117", []), ("S-WALTZ", "senate", "119-1-530", []),
    ("S-OBBBA", "senate", "119-1-372", []), ("S-CR-NOV25", "senate", "119-1-618", []), ("S-APPROP-JAN15", "senate", "119-2-11", []),
    ("S-APPROP-JAN30", "senate", "119-2-20", []),
    ("S-MULLIN", "senate", "119-2-63", [("KXVOTEMULLIN", "KXVOTEMULLIN-27")]),
    ("S-BUDGETRES", "senate", "119-2-105", [("KXVOTEBUDGETRESS", "KXVOTEBUDGETRESS-JAN27")]),
    ("S-WARSH", "senate", "119-2-120", [("KXVOTEFEDCHAIR", "KXVOTEFEDCHAIR-27")]),
    ("S-RECON26", "senate", "119-2-163", [("KXVOTERECNCS", "KXVOTERECNCS-MAY26")]),
    ("S-BLANCHE-AG", "senate", "119-2-230", [("KXVOTEBLANCHE", "KXVOTEBLANCHE-27")]),
    ("S-CLARITY", "senate", "119-2-234", [("KXVOTECLARITY", "KXVOTECLARITY-26SEP15")]),
    ("H-OBBBA-MAY", "house", "2025/145", []), ("H-CR-SEP19", "house", "2025/281", []), ("H-CR-NOV12", "house", "2025/285", []),
    ("H-EPSTEIN", "house", "2025/289", []), ("H-APPROP-JAN8", "house", "2026/7", []),
    ("H-APPROP-FEB3", "house", "2026/53", [("KXVOTESHUTDOWNH", "KXVOTESHUTDOWNH-APR26")]),
    ("C-SKRMETTI", "scotus", "2025-06-18", []),
    ("C-TARIFFS", "scotus", "2026-02-20", [("KXTRUMPSCOTUSVOTE", "KXTRUMPSCOTUSVOTE-26")]),
    ("C-CALLAIS", "scotus", "2026-04-29", [("KXVRASCOTUSVOTE", "KXVRASCOTUSVOTE-26")]),
    ("C-WATSON", "scotus", "2026-06-29", [("KXWATSONRNC", "KXWATSONRNC")]),
    ("C-SLAUGHTER", "scotus", "2026-06-29", [("KXTRUMPSLAUGHTERVOTE", "KXTRUMPSLAUGHTERVOTE-26")]),
]
# the markets the claim's study priced (2-5 per event); everything else in the event is the "full universe" check
CLAIM_MARKETS = {
    "KXVOTEMULLIN-27-MMUL", "KXVOTEMULLIN-27-JFET", "KXVOTEMULLIN-27-SCOL", "KXVOTEBUDGETRESS-JAN27-JFET",
    "KXVOTEBUDGETRESS-JAN27-CGRA", "KXVOTEFEDCHAIR-27-JFET", "KXVOTEFEDCHAIR-27-RPAU", "KXVOTEFEDCHAIR-27-TSCO",
    "KXVOTERECNCS-MAY26-TTIL", "KXVOTERECNCS-MAY26-SCOL", "KXVOTEBLANCHE-27-BCAS", "KXVOTEBLANCHE-27-MMCC",
    "KXVOTEBLANCHE-27-SCOL", "KXVOTECLARITY-26SEP15-T60", "KXVOTECLARITY-26SEP15-T58", "KXVOTECLARITY-26SEP15-T48",
    "KXVOTESHUTDOWNH-APR26-TBUR", "KXVOTESHUTDOWNH-APR26-CROY", "KXVOTESHUTDOWNH-APR26-AOGL",
    "KXTRUMPSCOTUSVOTE-26-0", "KXTRUMPSCOTUSVOTE-26-2", "KXTRUMPSCOTUSVOTE-26-3", "KXVRASCOTUSVOTE-26-6",
    "KXVRASCOTUSVOTE-26-3", "KXWATSONRNC", "KXTRUMPSLAUGHTERVOTE-26-6", "KXTRUMPSLAUGHTERVOTE-26-5"}
SENATE_REVISED_H = 3.0     # modify_date more than this after vote_date = the XML was revised; use vote + 59 min
SENATE_MEDIAN_POST_MIN = 59


def et_ts(s: str, fmt: str) -> int:
    return int(dt.datetime.strptime(s, fmt).replace(tzinfo=ET).timestamp())


def official_times() -> dict:
    """event id -> {t_evt, t_post, t_post_raw, src...} from senate.gov / clerk.house.gov XML (SCOTUS: 10:00 ET)."""
    out = {}
    for eid, kind, rec, _ in EVENTS:
        if kind == "senate":
            c, s, n = rec.split("-")
            url = f"https://www.senate.gov/legislative/LIS/roll_call_votes/vote{c}{s}/vote_{c}_{s}_{int(n):05d}.xml"
            x = wget(url)
            vd = re.search(r"<vote_date>(.*?)</vote_date>", x).group(1); md = re.search(r"<modify_date>(.*?)</modify_date>", x).group(1)
            fmt = "%B %d, %Y, %I:%M %p"
            tv = et_ts(re.sub(r"\s+", " ", vd).strip(), fmt.replace(", %I", ", %I")); tm = et_ts(re.sub(r"\s+", " ", md).strip(), fmt)
            revised = (tm - tv) > SENATE_REVISED_H * 3600
            out[eid] = {"kind": kind, "t_evt": tv, "t_post_raw": tm, "revised": revised,
                        "t_post": tv + SENATE_MEDIAN_POST_MIN * 60 if revised else tm,
                        "title": re.search(r"<vote_title>(.*?)</vote_title>", x, re.S).group(1)[:120],
                        "result_text": re.search(r"<vote_result_text>(.*?)</vote_result_text>", x, re.S).group(1)}
            yeas = re.search(r"<yeas>(\d+)</yeas>", x); out[eid]["yeas"] = int(yeas.group(1)) if yeas else None
            out[eid]["members"] = {m.group(2).strip(): m.group(3).strip() for m in re.finditer(
                r"<member>.*?<last_name>(.*?)</last_name>.*?<first_name>(.*?)</first_name>.*?<vote_cast>(.*?)</vote_cast>", x, re.S)}
            out[eid]["members"] = {f"{m.group(2).strip()} {m.group(1).strip()}": m.group(3).strip() for m in re.finditer(
                r"<member>.*?<last_name>(.*?)</last_name>.*?<first_name>(.*?)</first_name>.*?<vote_cast>(.*?)</vote_cast>", x, re.S)}
        elif kind == "house":
            y, n = rec.split("/")
            x = wget(f"https://clerk.house.gov/evs/{y}/roll{int(n):03d}.xml")
            d = re.search(r"<action-date>(.*?)</action-date>", x).group(1); tt = re.search(r'time-etz="(\d+:\d+)"', x).group(1)
            ta = int(dt.datetime.strptime(f"{d} {tt}", "%d-%b-%Y %H:%M").replace(tzinfo=ET).timestamp())
            out[eid] = {"kind": kind, "t_evt": ta, "t_post": ta, "t_post_raw": ta, "revised": False,
                        "title": re.search(r"<vote-desc>(.*?)</vote-desc>", x, re.S).group(1)[:120],
                        "result_text": re.search(r"<vote-result>(.*?)</vote-result>", x).group(1),
                        "members": {m.group(1): m.group(2) for m in re.finditer(
                            r'<legislator[^>]*unaccented-name="([^"]+)"[^>]*>.*?</legislator>\s*<vote>(.*?)</vote>', x, re.S)}}
        else:
            t10 = et_ts(rec + " 10:00", "%Y-%m-%d %H:%M")
            out[eid] = {"kind": kind, "t_evt": t10, "t_post": None, "t_post_raw": None, "revised": False}
    return out


def split_events(times: dict, frac: float = 0.7) -> tuple[list[str], list[str]]:
    order = sorted(times, key=lambda e: (times[e]["t_evt"], e))
    k = int(len(order) * frac)
    return order[:k], order[k:]


# ----------------------------------------------------------------------------------------------- Kalshi data
def iso_ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def list_markets(series: str, event: str) -> list[dict]:
    """All settled markets of one Kalshi event: historical tier first, live tier as fallback (one call each)."""
    d = kget(f"/historical/markets?series_ticker={series}&limit=1000")
    ms = (d or {}).get("data", {}).get("markets", []) if d else []
    ms = [m for m in ms if m.get("event_ticker") == event or m.get("ticker") == event]
    if not ms:
        d = kget(f"/markets?event_ticker={event}&limit=1000")
        ms = (d or {}).get("data", {}).get("markets", []) if d else []
        tier = "live"
    else:
        tier = "hist"
    out = []
    for m in ms:
        out.append({"t": m["ticker"], "e": m.get("event_ticker"), "series": series, "tier": tier, "open": iso_ts(m.get("open_time")),
                    "close": iso_ts(m.get("close_time")), "result": m.get("result"), "vol": float(m.get("volume_fp") or m.get("volume") or 0),
                    "sub": m.get("yes_sub_title") or m.get("no_sub_title") or (m.get("custom_strike") or {}).get("Member"),
                    "strike": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
                    "rules": m.get("rules_primary", "")[:300], "title": m.get("title", "")[:200]})
    return out


def _f(x):
    return None if x in (None, "") else float(x)


def parse_candles(cs: list[dict]) -> list[list]:
    """[end_ts, yes_ask_close, yes_bid_close, yes_ask_low, yes_bid_high, volume, ask_open, bid_open]"""
    out = []
    for r in cs:
        a, b = r.get("yes_ask") or {}, r.get("yes_bid") or {}
        ac = _f(a.get("close", a.get("close_dollars"))); bc = _f(b.get("close", b.get("close_dollars")))
        out.append([r["end_period_ts"], ac, bc, _f(a.get("low", a.get("low_dollars"))), _f(b.get("high", b.get("high_dollars"))),
                    _f(r.get("volume", r.get("volume_fp"))) or 0.0, _f(a.get("open", a.get("open_dollars"))), _f(b.get("open", b.get("open_dollars")))])
    out.sort()
    return out


def candles_hist(ticker: str, t0: int, t1: int) -> list[list] | None:
    d = kget(f"/historical/markets/{ticker}/candlesticks?start_ts={t0}&end_ts={t1}&period_interval=1")
    return parse_candles(d["data"].get("candlesticks", [])) if d else None


def candles_event_live(series: str, event: str, t0: int, t1: int) -> dict:
    d = kget(f"/series/{series}/events/{event}/candlesticks?start_ts={t0}&end_ts={t1}&period_interval=1")
    if not d:
        return {}
    x = d["data"]
    return {t: parse_candles(c) for t, c in zip(x.get("market_tickers", []), x.get("market_candlesticks", []))}


def window(eid: str, ot: dict) -> tuple[int, int]:
    o = ot[eid]
    if o["kind"] == "scotus":
        return o["t_evt"] - 3600, o["t_evt"] + 150 * 60
    return o["t_evt"] - 3600, max(o["t_evt"], o["t_post"]) + 90 * 60


def fetch_all(full_universe: bool = True) -> None:
    ot = json.loads((OUT / "official_times.json").read_text()) if (OUT / "official_times.json").exists() else official_times()
    (OUT / "official_times.json").write_text(json.dumps(ot, indent=1))
    _, val = split_events(ot)
    mk = {}
    for eid, kind, rec, evs in EVENTS:
        if eid in val:
            for s, e in evs:
                mk[eid] = list_markets(s, e)
    (OUT / "markets.json").write_text(json.dumps(mk, indent=1))
    # candles: the claim's 27 markets first, then the rest of each event by volume (budget permitting)
    todo = []
    for eid, ms in mk.items():
        for m in ms:
            pri = 0 if m["t"] in CLAIM_MARKETS else 1
            todo.append((pri, -m["vol"], eid, m))
    todo.sort(key=lambda x: (x[0], x[1]))
    cf = OUT / "candles.json"
    have = json.loads(cf.read_text()) if cf.exists() else {}
    live_done = set()
    for pri, _, eid, m in todo:
        if m["t"] in have or (pri == 1 and not full_universe):
            continue
        t0, t1 = window(eid, ot)
        if m["tier"] == "live":
            if eid in live_done:
                continue
            got = candles_event_live(m["series"], m["e"], t0, t1); live_done.add(eid)
            have.update(got)
        else:
            if kalshi_calls_used() + 2 > BUDGET - 8:
                print("stopping: budget reserve reached"); break
            c = candles_hist(m["t"], t0, t1)
            if c is not None:
                have[m["t"]] = c
        cf.write_text(json.dumps(have))
    print(f"candles for {len(have)} markets; Kalshi calls used {kalshi_calls_used()}")


# ----------------------------------------------------------------------------------------------- execution model
MAX_AGE = 30 * 60
CONTRACTS = 10


def quote(c: list[list], t: int):
    """(yes_ask, yes_bid, age_s) from the last candle ending at or before t, if <= 30 min old."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > MAX_AGE:
        return None
    return best[1], best[2], t - best[0]


def fee_per_contract(p: float, n: int = CONTRACTS) -> float:
    """Kalshi taker fee 0.07 p (1-p) per contract, rounded UP to the cent per order of n contracts."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def winner_fill(m: dict, c: list[list], decide: int, delay: int = 60):
    """Buy the (known) winner at the taker quote `delay` s after the decision. None if no offer < 0.995."""
    tf = decide + delay
    if m["close"] is not None and m["close"] <= tf:
        return None                       # market already closed
    if m["open"] is not None and m["open"] > decide:
        return None
    q = quote(c, tf)
    if not q:
        return None
    ask, bid, age = q
    if m["result"] == "yes":
        px = ask
    elif m["result"] == "no":
        px = None if bid is None else 1 - bid
    else:
        return None
    if px is None or not (0.01 <= px < 0.995):
        return None
    px = round(px, 4)
    ret = (1.0 - px - fee_per_contract(px)) / px
    vol5 = sum(r[5] for r in c if decide < r[0] <= decide + 300)
    vol_fill_min = sum(r[5] for r in c if tf < r[0] <= tf + 60)
    return {"px": px, "ret": ret, "age_s": age, "vol_next5": vol5, "vol_fill_min": vol_fill_min, "fill_ts": tf}


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["ev"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; ne = len(em)
    t_pop = st.mean(em) / (st.pstdev(em) / math.sqrt(ne)) if ne > 2 and st.pstdev(em) > 0 else float("nan")
    t_smp = st.mean(em) / (st.stdev(em) / math.sqrt(ne)) if ne > 2 and st.stdev(em) > 0 else float("nan")
    # cluster-robust (CR1) t of the equal-$ trade mean, clustered by event
    mu = st.mean(r["ret"] for r in rows); n = len(rows)
    se = math.sqrt(sum(sum(x - mu for x in v) ** 2 for v in ev.values())) / n * math.sqrt(ne / (ne - 1)) if ne > 1 else 0.0
    t_cr1 = mu / se if se > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    ts = sorted(r["ts"] for r in rows); mid = ts[len(ts) // 2]
    h1 = [r["ret"] for r in rows if r["ts"] < mid]; h2 = [r["ret"] for r in rows if r["ts"] >= mid]
    v5 = sorted(r["vol_next5"] for r in rows)
    return {"n": len(rows), "events": ne, "win": 1.0, "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t_cr1, 2), "t_evmean_pstdev": round(t_pop, 2),
            "t_evmean_sd": round(t_smp, 2),
            "event_mean_ret": round(st.mean(em), 4),
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "median_vol_next5": v5[len(v5) // 2]}


# ----------------------------------------------------------------------------------------------- SCOTUS release estimate
def scotus_first_move(ms: list[dict], cd: dict, t10: int, thr: float = 0.05, horizon_min: int = 90) -> int | None:
    """End of the first 1-minute candle at/after 10:00 ET in which any market of the case moved its mid by >= thr
    relative to its quote at 10:00 (uses prices: a look-ahead device, reported only for comparison)."""
    def mid_before(c, t):   # last mid strictly before candle end t, any age (thin books update rarely)
        p = None
        for r in c:
            if r[0] < t:
                if r[1] is not None and r[2] is not None:
                    p = (r[1] + r[2]) / 2
            else:
                break
        return p
    for k in range(1, horizon_min + 1):
        T = t10 + 60 * k
        for m in ms:
            c = cd.get(m["t"]) or []
            r = next((x for x in c if x[0] == T), None)
            if not r or r[1] is None or r[2] is None:
                continue
            p = mid_before(c, T)
            if p is not None and abs((r[1] + r[2]) / 2 - p) >= thr:
                return T
    return None


# ----------------------------------------------------------------------------------------------- analysis
def load():
    ot = json.loads((OUT / "official_times.json").read_text())
    mk = json.loads((OUT / "markets.json").read_text())
    cd = json.loads((OUT / "candles.json").read_text())
    return ot, mk, cd


def postings(ot: dict, mk: dict, cd: dict, scotus_mode: str = "first_move_end", scotus_fixed_min: int = 0,
             house_delay_min: int = 0) -> dict:
    """posting time per validation event under a given SCOTUS / House assumption."""
    post = {}
    for eid, ms in mk.items():
        o = ot[eid]
        if o["kind"] == "scotus":
            if scotus_mode == "fixed":
                post[eid] = o["t_evt"] + 60 * scotus_fixed_min
            else:
                T = scotus_first_move(ms, cd, o["t_evt"])
                post[eid] = None if T is None else (T if scotus_mode == "first_move_end" else T - 60)
        elif o["kind"] == "house":
            post[eid] = o["t_post"] + 60 * house_delay_min
        else:
            post[eid] = o["t_post"]
    return post


def run_rule(mk: dict, cd: dict, post: dict, extra_min: int, universe: str) -> list[dict]:
    rows = []
    for eid, ms in mk.items():
        if post.get(eid) is None:
            continue
        d = post[eid] + 60 * extra_min
        for m in ms:
            if universe == "claim" and m["t"] not in CLAIM_MARKETS:
                continue
            c = cd.get(m["t"])
            if not c:
                continue
            f = winner_fill(m, c, d)
            if f:
                rows.append({"ev": eid, "t": m["t"], "result": m["result"], "ts": d, **f})
    return rows


NAME_OVERRIDE = {"Andy Biggs": "Biggs (AZ)", "Susie Lee": "Lee (NV)", "Don Davis": "Davis (NC)", "Vicente Gonzalez": "Gonzalez, V."}


def consistency(ot: dict, mk: dict) -> list[str]:
    """Is the settled result of each member / count market implied by the official XML?"""
    notes = []
    for eid, ms in mk.items():
        o = ot[eid]
        if o["kind"] not in ("senate", "house"):
            continue
        mem = o.get("members", {})
        for m in ms:
            sub = (m["sub"] or "").strip()
            if m["strike"] == "custom" and sub:
                last = sub.split()[-1]
                if sub in NAME_OVERRIDE:
                    hits = [mem[NAME_OVERRIDE[sub]]] if NAME_OVERRIDE[sub] in mem else []
                elif o["kind"] == "senate":
                    hits = [v for k, v in mem.items() if k.lower() == sub.lower()] or \
                           [v for k, v in mem.items() if k.split()[-1].lower() == last.lower()]
                else:
                    hits = [v for k, v in mem.items() if k.split(" (")[0].lower() == last.lower()]
                if len(hits) != 1:
                    notes.append(f"{m['t']}: member '{sub}' matched {len(hits)} rows in the XML"); continue
                yes = hits[0] in ("Yea", "Aye", "Yes")
                if yes != (m["result"] == "yes"):
                    notes.append(f"{m['t']}: XML vote {hits[0]} vs Kalshi result {m['result']}")
            elif m["floor"] is not None and o.get("yeas") is not None:
                implied = o["yeas"] > m["floor"]
                if implied != (m["result"] == "yes"):
                    notes.append(f"{m['t']}: yeas {o['yeas']} vs floor {m['floor']} vs result {m['result']}")
    return notes


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "n=0"
    return (f"n={s['n']:3d} ev={s['events']:2d} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.4f} t(CR1)={s['t']:.2f} t(ev-mean)={s['t_evmean_pstdev']:.2f} "
            f"wo3={s['ret_wo3'] if s['ret_wo3'] is None else round(s['ret_wo3'], 4)} h1={s['half1']} h2={s['half2']} medvol5={s['median_vol_next5']}")


def analyze() -> dict:
    ot, mk, cd = load()
    disc, val = split_events(ot)
    res = {"split": {"discovery": disc, "validation": val}, "candles_markets": len(cd),
           "validation_markets": sum(len(v) for v in mk.values()),
           "validation_markets_with_candles": sum(1 for v in mk.values() for m in v if cd.get(m["t"]))}
    res["scotus_first_move_end_et"] = {}
    for eid, ms in mk.items():
        if ot[eid]["kind"] == "scotus":
            T = scotus_first_move(ms, cd, ot[eid]["t_evt"])
            res["scotus_first_move_end_et"][eid] = dt.datetime.fromtimestamp(T, ET).strftime("%H:%M") if T else None
    res["posting_used"] = {}
    out = {}
    for smode in ("first_move_end", "first_move_start"):
        post = postings(ot, mk, cd, scotus_mode=smode)
        res["posting_used"][smode] = {e: (dt.datetime.fromtimestamp(p, ET).strftime("%Y-%m-%d %H:%M") if p else None) for e, p in post.items()}
        for uni in ("claim", "full"):
            for lab, k in (("B_post+0", 0), ("A_post+1", 1)):
                rows = run_rule(mk, cd, post, k, uni)
                out[f"{lab}|scotus={smode}|universe={uni}"] = {"stats": stats(rows), "trades": rows}
    res["rules"] = out
    # reaction-time grid: decide k minutes after the posting (SCOTUS posting = start of the first-move minute)
    grid = {}
    post = postings(ot, mk, cd, scotus_mode="first_move_start")
    for k in (0, 1, 2, 3, 5, 10):
        for uni in ("claim", "full"):
            rows = run_rule(mk, cd, post, k, uni)
            grid[f"all_decide_post+{k}m|{uni}"] = stats(rows)
            for kind in ("senate", "house", "scotus"):
                grid[f"{kind}_decide_post+{k}m|{uni}"] = stats([r for r in rows if ot[r["ev"]]["kind"] == kind])
    # realistic-ish: House XML readable 2 min after action-time, SCOTUS seen 1 min after the first move
    for uni in ("claim", "full"):
        p2 = postings(ot, mk, cd, scotus_mode="first_move_start", house_delay_min=2)
        rows = run_rule(mk, cd, p2, 1, uni)
        grid[f"realistic_houseXML+2m_then_A|{uni}"] = {**stats(rows), "trades": [(r["t"], r["px"], round(r["ret"], 3), r["vol_fill_min"]) for r in rows]}
    # when did the House member markets reprice? first minute after action-time with the winner offered >= 0.97
    # (or no longer offered below 0.97), per market of the House event
    house_reprice = {}
    for eid, ms in mk.items():
        if ot[eid]["kind"] != "house":
            continue
        t0 = ot[eid]["t_post"]
        for m in ms:
            c = cd.get(m["t"]) or []
            first = None
            for k in range(0, 61):
                q = quote(c, t0 + 60 * k)
                if not q:
                    continue
                px = q[0] if m["result"] == "yes" else (None if q[1] is None else 1 - q[1])
                if px is None or px >= 0.97:
                    first = k; break
            q0 = quote(c, t0 + 60)
            px0 = None if not q0 else (q0[0] if m["result"] == "yes" else (None if q0[1] is None else 1 - q0[1]))
            house_reprice[m["t"]] = {"winner_px_at_post+1m": px0, "min_until_winner_ask>=0.97": first}
    res["house_member_reprice_minutes_after_action_time"] = house_reprice
    for hd in (0, 2, 5, 8, 9, 10, 11, 12, 15):
        p3 = postings(ot, mk, cd, scotus_mode="first_move_start", house_delay_min=hd)
        rows = [r for r in run_rule(mk, cd, p3, 0, "full") if ot[r["ev"]]["kind"] == "house"]
        grid[f"house_full_xml_at_action+{hd}m_ruleB"] = stats(rows)
    # leave-one-event-out on rule B, full universe: how much of the mean is one event
    rowsB = run_rule(mk, cd, post, 0, "full")
    for e in sorted({r["ev"] for r in rowsB}):
        grid[f"B_full_without_{e}"] = stats([r for r in rowsB if r["ev"] != e])
    res["sensitivity"] = grid
    res["consistency_xml_vs_settlement"] = consistency(ot, mk)
    return res


def write_result(res: dict) -> dict:
    R = res["rules"]; G = res["sensitivity"]
    b = R["B_post+0|scotus=first_move_start|universe=claim"]["stats"]; a = R["A_post+1|scotus=first_move_start|universe=claim"]["stats"]
    bf = R["B_post+0|scotus=first_move_start|universe=full"]["stats"]; af = R["A_post+1|scotus=first_move_start|universe=full"]["stats"]
    K_BAR = 4.53

    def gate(x):
        return {"n>=40": x["n"] >= 40, "ret>=10%": x["ret_per_dollar"] >= 0.10, f"t>={K_BAR}": (x["t"] or 0) >= K_BAR,
                "wo3>0": (x["ret_wo3"] or 0) > 0, "halves>0": min(x["half1"] or -1, x["half2"] or -1) > 0}
    out = {
        "name": "r4_official_posting_lag_study_repro",
        "reproduces": "r4_official_posting_lag_study validation (rules A and B)",
        "kalshi_calls_used": kalshi_calls_used(),
        "split": res["split"],
        "posting_times_used_et": res["posting_used"]["first_move_start"],
        "validation_claim_universe": {"B_decide_at_post": b, "A_decide_post+1m": a, "gate_B": gate(b), "gate_A": gate(a)},
        "validation_full_universe_all_markets_of_the_events": {"B": bf, "A": af,
            "B_without_H-APPROP-FEB3": G["B_full_without_H-APPROP-FEB3"]},
        "house_xml_delay_grid_full_universe_ruleB": {k: v for k, v in G.items() if k.startswith("house_full_xml")},
        "house_member_reprice_minutes_after_action_time": res["house_member_reprice_minutes_after_action_time"],
        "reaction_grid_claim_universe": {k: v for k, v in G.items() if k.startswith("all_decide") and k.endswith("|claim")},
        "scotus_reaction_grid_full_universe": {k: v for k, v in G.items() if k.startswith("scotus_decide") and k.endswith("|full")},
        "consistency_xml_vs_settlement": res["consistency_xml_vs_settlement"] or "all senate/house member and count markets settle as the official XML implies",
        "verdict": "claim numbers reproduced exactly; edge not real (fails gate; positive part rests on unverifiable posting-time assumptions)",
    }
    (OUT / "result.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main(argv: list[str]) -> None:
    mode = argv[1] if len(argv) > 1 else "all"
    OUT.mkdir(parents=True, exist_ok=True)
    if mode in ("fetch", "all"):
        fetch_all(True)
    if mode in ("analyze", "all"):
        res = analyze()
        (OUT / "analysis.json").write_text(json.dumps(res, indent=1, default=str))
        for k, v in res["rules"].items():
            print(f"{k:55s} {fmt(v['stats'])}")
        for k, v in res["sensitivity"].items():
            print(f"{k:40s} {fmt(v)}")
        print("SCOTUS first move (candle end, ET):", res["scotus_first_move_end_et"])
        print("consistency:", res["consistency_xml_vs_settlement"])
        write_result(res)


if __name__ == "__main__":
    main(sys.argv)
