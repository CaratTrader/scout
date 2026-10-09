"""r5_scheduled_release_poller_logger: forward PAPER log of the scheduled-release poller (frozen in
data/kalshi_lab/strategies/r5_scheduled_release_poller/preregistration.json, written before the first pass).

One pass per invocation (launchd/cron every 60 s; a pass lasts <= ~55 s, or ~2-3 min when a source fires). It never
places orders. Kalshi: at most 10 calls per pass (--max-calls may only lower it), every call through
lab.us.data_refresh.fetch (temperature-bot idle window, 429 retries), logged to forward_calls.log. Daily cap on
watch-list discovery calls: 40.

Legs (times ET):
  B200   TRADE.  Sundays 14:50-23:55. Source: billboard.com WordPress REST API, newest 20 posts, cache-busted, every 15 s
         until 20:30 then every 60 s. Fires on the first post dated that Sunday whose title passes the frozen rule
         (r5_scheduled_release_poller_parse.py). Kalshi KXTOPALBUM open-market snapshot every minute 14:56-15:10, every
         10 min after, and at fire, fire + 60 s (the FILL snapshot, with up to 3 order books), then each minute to
         fire + 10 min and at + 20 / + 30 min. Decided sides: KXTOPALBUM via match_markets; KXALBUMDEBUT (snapshot at
         the fill) by the same album rule. Paper buy of 10 contracts of every decided side whose taker price in the
         fill snapshot is <= 0.90, at max(snapshot price, 10-lot VWAP from the order book), fee ceil(0.07 p (1-p) 10).
  HOUSE  TRADE when unambiguous, else OBSERVE. Source: clerk.house.gov/evs/<Y>/roll<NNN>.xml for the next roll number,
         every 15 s 08:00-03:00 when a watch-list House market is open, else every 60 s 09:00-01:00 Mon-Fri. On a new
         roll call: Kalshi snapshot of the open watch-list series at fire and fire + 60 s (fill) and later at + 5,
         + 10, + 20, + 30 min. Frozen ambiguity rule: trade only passage-type questions (PASSAGE_Q) whose legis-num
         appears in the market's rules; per-member markets need exactly one surname (+ state) match and a 'votes
         for / yea / in favor' rule; count markets need a strike and a 'yea / in favor' rule.
  SCOTUS OBSERVE.  Mon-Fri 09:58-13:00, Oct-Jul. Source: supremecourt.gov slip-opinion page of the term, every 15 s
         when a watch-list SCOTUS market is open, else every 60 s. On a new opinion PDF: Kalshi snapshots of open
         SCOTUS watch-list series at fire, + 60 s, + 5 and + 15 min. No automatic side (needs legal reading).
  STRUCT weekly (Mon >= 10:00): KXNETFLIXRANKSHOW and KXTOPSONG open markets, to log whether they still close before
         their publication (Netflix Top 10 Tuesday, Hot 100 top 10 Monday/Tuesday). If not, the leg becomes testable.
  SETTLE daily >= 12:00: results of open paper trades (/markets/{ticker}).
Files (data/kalshi_lab/strategies/r5_scheduled_release_poller/): forward.jsonl, state.json, forward_calls.log, .lock;
  test runs (--test) write to test_passes/.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_scheduled_release_poller_logger [--max-calls N] [--test]
       [--simulate-b200]   (test only: treat the latest qualifying Billboard post as new, to exercise fire/fill)"""
from __future__ import annotations
import datetime as dt, fcntl, json, math, os, re, sys, threading, time
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies import r5_scheduled_release_poller_api as API  # noqa: E402
from lab.kalshi.strategies.r5_scheduled_release_poller_parse import (album_from_title, clean_title,  # noqa: E402
                                                                      is_b200_no1_title, match_markets, norm, tokens)

ET = ZoneInfo("America/New_York")
OUT = API.OUT

# ------------------------------------------------------------------------------------------------ frozen constants
CAP = 0.90
CONTRACTS = 10
FILL_DELAY_S = 60
POLL_FAST_S = 15
PASS_S = 52
MAX_CALLS = 10
DISC_PER_PASS = 5
DISC_PER_DAY = 40
B200_WIN = ((14, 50), (23, 55)); B200_FAST_END = (20, 30)
B200_SNAP_MINUTES = list(range(56, 60))            # 14:56-14:59, plus 15:00-15:10 below
BB_URL = "https://www.billboard.com/wp-json/wp/v2/posts?per_page=20&orderby=date&order=desc&_fields=id,date_gmt,modified_gmt,title,link"
CLERK = "https://clerk.house.gov/evs/{y}/roll{n:03d}.xml"
SCOTUS = "https://www.supremecourt.gov/opinions/slipopinion/{term}"
PASSAGE_Q = ("on passage", "on motion to suspend the rules and pass", "on motion to suspend the rules and pass, as amended",
             "on agreeing to the conference report", "on motion to concur in the senate amendment",
             "on motion to concur in the senate amendments", "on concurring in senate amendment",
             "on motion to suspend the rules and concur in the senate amendment", "on agreeing to the resolution",
             "on motion to suspend the rules and agree")
HOUSE_SERIES = ["KXVOTESHUTDOWNH", "KXVOTERECNCH", "KXRECNCHVOTE", "KXGOVTFUNDHVOTES", "KXHOUSEGOVTFUND", "KXHOUSEEPSTEIN",
                "KXHOUSEEPSTEIN2", "KXACAHOUSEVOTE", "KXPCSAHOUSEVOTE", "KXPCSAHOUSEVOTES", "KXEDWARDSCENSUREVOTES",
                "KXEXPELSWALWELLVOTES", "KXDISAPPROVETARIFF", "KXBILLCONTEMPTVOTE", "KXHILLARYCONTEMPTVOTE",
                "KXHOUSERUSSIASANCTION", "KXBIRTHTOURISMVOTE", "KXHOUSEETHICS", "KXPPUNISH"]
STRUCT_SERIES = ["KXNETFLIXRANKSHOW", "KXTOPSONG"]
CLOSED = {"closed", "settled", "determined", "finalized"}


def scotus_series() -> list[str]:
    try:
        s = json.loads((ROOT / "data/kalshi_lab/series_all.json").read_text())["series"]
        return sorted(x["ticker"] for x in s if "supremecourt" in json.dumps(x.get("settlement_sources")).lower())
    except Exception:
        return []


# ------------------------------------------------------------------------------------------------ io helpers
class Ctx:
    def __init__(self, test: bool, max_calls: int):
        self.test = test
        self.dir = OUT / "test_passes" if test else OUT
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fwd = self.dir / ("forward_test.jsonl" if test else "forward.jsonl")
        self.state_f = self.dir / ("state_test.json" if test else "state.json")
        self.clog = self.dir / ("forward_calls_test.log" if test else "forward_calls.log")
        self.max_calls = min(MAX_CALLS, max_calls)
        self.calls = 0
        self.lock = threading.Lock()
        self.state = json.loads(self.state_f.read_text()) if self.state_f.exists() else {}
        self.t0 = time.time()

    def log(self, row: dict) -> None:
        row = {"ts": round(time.time(), 3), **row}
        if self.test:
            row["test"] = True
        with self.lock, self.fwd.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def save(self) -> None:
        tmp = self.state_f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str)); tmp.replace(self.state_f)

    def kalshi(self, path: str) -> dict | None:
        with self.lock:
            if self.calls >= self.max_calls:
                return None
            self.calls += 1
        t = time.time()
        d = API.kget(path, cache=False, log=self.clog, budget=10 ** 9)
        d["_t_req"] = round(t, 3); d["_t_rcv"] = round(time.time(), 3)
        return d

    def web(self, url: str, bust: bool = False) -> tuple[int, str, dict, float]:
        u = url + (("&" if "?" in url else "?") + f"_cb={int(time.time() * 1000)}" if bust else "")
        s, t, h = API.wget(u, pace=0.0, cache=False, timeout=12)
        return s, t, h, time.time()


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def hm(d: dt.datetime) -> tuple[int, int]:
    return d.hour, d.minute


def fee_order(p: float, q: int) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * q * 100, 6)) / 100


def px_side(m: dict, side: str) -> float | None:
    def f(k):
        v = m.get(k + "_dollars", None)
        if v is None and m.get(k) is not None:
            v = m[k] / 100.0 if isinstance(m[k], (int, float)) and m[k] > 1 else m[k]
        try:
            return float(v)
        except Exception:
            return None
    if side == "yes":
        a = f("yes_ask"); return a if a is not None and 0 < a < 1 else None
    b = f("yes_bid"); return (1 - b) if b is not None and 0 < b < 1 else None


def compact_markets(d: dict) -> list[list]:
    out = []
    for m in d.get("markets") or []:
        out.append([m.get("ticker"), m.get("event_ticker"), m.get("yes_sub_title"), m.get("yes_bid_dollars"),
                    m.get("yes_ask_dollars"), m.get("last_price_dollars"), m.get("volume_fp") or m.get("volume"),
                    m.get("status"), m.get("open_time"), m.get("close_time")])
    return out


def vwap_from_book(book: dict, side: str, q: int, cap: float) -> tuple[float | None, int]:
    """Buy `q` contracts of `side` by taking the opposite side's bids (YES ask = 1 - NO bid). Returns (vwap, filled)."""
    ob = (book or {}).get("orderbook_fp") or (book or {}).get("orderbook") or {}
    opp = ob.get("no_dollars") or ob.get("no") if side == "yes" else ob.get("yes_dollars") or ob.get("yes")
    if not opp:
        return None, 0
    lv = []
    for x in opp:
        p, n = float(x[0]), float(x[1])
        p = p / 100.0 if p > 1 else p
        lv.append((1 - p, n))
    lv.sort()
    got, cost = 0, 0.0
    for p, n in lv:
        if p > cap + 1e-9:
            break
        take = min(q - got, int(n))
        got += take; cost += take * p
        if got >= q:
            break
    return (cost / got if got else None), got


# ------------------------------------------------------------------------------------------------ B200 leg
def b200_window(d: dt.datetime) -> bool:
    return d.weekday() == 6 and B200_WIN[0] <= hm(d) <= B200_WIN[1]


def b200_snapshot(c: Ctx, tag: str) -> dict | None:
    d = c.kalshi("/markets?series_ticker=KXTOPALBUM&status=open&limit=200")
    if d is None:
        return None
    row = {"row": "snap", "leg": "b200", "tag": tag, "t_req": d.get("_t_req"), "t_rcv": d.get("_t_rcv"),
           "err": d.get("_error"), "markets": compact_markets(d)}
    c.log(row)
    return {"raw": d, "row": row}


def b200_decide(album: str, snap: dict, sunday: dt.date) -> dict:
    ms = [m for m in snap["raw"].get("markets") or []
          if m.get("close_time") and dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).astimezone(ET).date() == sunday]
    subs = {m["ticker"]: m.get("yes_sub_title") or "" for m in ms}
    mm = match_markets(album, subs)
    sides = ({mm["yes"]: "yes"} if mm["yes"] else {}) | {t: "no" for t in mm["no"]}
    return {"status": mm["status"], "sides": sides, "n_markets": len(ms)}


def debut_decide(album: str, d: dict, sunday: dt.date) -> dict:
    """KXALBUMDEBUT markets of THIS reveal only (close date == the reveal Sunday, ET)."""
    sides = {}
    a = norm(album)
    for m in d.get("markets") or []:
        try:
            if dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).astimezone(ET).date() != sunday:
                continue
        except Exception:
            continue
        r = m.get("rules_primary") or ""
        x = re.search(r"If (.+?) is the #1 album", r)
        name = x.group(1) if x else (m.get("yes_sub_title") or "")
        n = norm(name)
        if not n:
            continue
        if n == a or (min(len(n), len(a)) >= 3 and (n in a or a in n)):
            sides[m["ticker"]] = "yes"
        elif not (tokens(name) & tokens(album)):
            import difflib
            if difflib.SequenceMatcher(None, n, a).ratio() < 0.6:
                sides[m["ticker"]] = "no"
    return sides


def paper_trades(c: Ctx, leg: str, fire_id: str, sides: dict, snap_raw: dict, first_serve: float, max_books: int = 3) -> None:
    ms = {m["ticker"]: m for m in snap_raw.get("markets") or []}
    cand, noq, over = [], [], []
    for tk, side in sides.items():
        m = ms.get(tk)
        if not m or (m.get("status") or "active") in CLOSED:
            continue
        p = px_side(m, side)
        if p is None:
            noq.append(tk); continue
        if p > CAP:
            over.append([tk, side, p]); continue
        cand.append((p, tk, side))
    c.log({"row": "fill_screen", "leg": leg, "fire": fire_id, "n_decided": len(sides), "no_quote": noq, "over_cap": over,
           "candidates": [[tk, sd, p] for p, tk, sd in cand]})
    cand.sort()
    for i, (p, tk, side) in enumerate(cand):
        book, vw, got = None, None, CONTRACTS
        if i < max_books:
            book = c.kalshi(f"/markets/{tk}/orderbook")
            if book is not None:
                vw, got = vwap_from_book(book, side, CONTRACTS, CAP)
        px = max(p, vw) if vw is not None else p
        q = got if (book is not None and vw is not None) else CONTRACTS
        if q <= 0:
            c.log({"row": "no_depth", "leg": leg, "fire": fire_id, "t": tk, "side": side, "px": p}); continue
        f = fee_order(px, q)
        tr = {"row": "trade", "leg": leg, "fire": fire_id, "t": tk, "side": side, "px": round(px, 4), "snap_px": p,
              "vwap": vw, "qty": q, "fee": f, "depth_checked": book is not None and vw is not None,
              "t_quote": snap_raw.get("_t_rcv"), "first_serve": first_serve,
              "book": ((book or {}).get("orderbook_fp") or (book or {}).get("orderbook")) if book else None}
        c.log(tr)
        c.state.setdefault("open_trades", []).append({k: tr[k] for k in ("leg", "fire", "t", "side", "px", "qty", "fee")})


def b200_fire(c: Ctx, post: dict, t_serve: float, sunday: dt.date, simulated: bool) -> None:
    ti = clean_title(post["title"]["rendered"])
    album = album_from_title(ti)
    fid = f"b200-{sunday.isoformat()}-{post['id']}"
    c.log({"row": "fire", "leg": "b200", "fire": fid, "first_serve": t_serve, "date_gmt": post.get("date_gmt"),
           "modified_gmt": post.get("modified_gmt"), "title": ti, "album": album, "link": post.get("link"),
           "simulated": simulated})
    st = c.state.setdefault("b200", {})
    st["fired"] = {"id": fid, "t": t_serve, "sunday": sunday.isoformat(), "album": album}
    c.save()
    if not album:
        c.log({"row": "fire_unparsed", "leg": "b200", "fire": fid}); return
    s0 = b200_snapshot(c, "fire")
    dec = b200_decide(album, s0, sunday) if s0 else None
    if dec:
        c.log({"row": "decision", "leg": "b200", "fire": fid, **dec})
    wait = t_serve + FILL_DELAY_S - time.time()
    if wait > 0:
        time.sleep(wait)
    s1 = b200_snapshot(c, "fill")
    if s1 is None:
        c.log({"row": "fill_missed_budget", "leg": "b200", "fire": fid}); return
    dec1 = b200_decide(album, s1, sunday)
    sides = dec1["sides"]
    deb = c.kalshi("/markets?series_ticker=KXALBUMDEBUT&status=open&limit=200")
    if deb is not None:
        c.log({"row": "snap", "leg": "b200", "tag": "fill_debut", "t_req": deb.get("_t_req"), "t_rcv": deb.get("_t_rcv"),
               "markets": compact_markets(deb)})
        dsides = debut_decide(album, deb, sunday)
        c.log({"row": "decision", "leg": "b200_debut", "fire": fid, "sides": dsides})
    c.log({"row": "decision", "leg": "b200", "fire": fid, "at": "fill", **dec1})
    paper_trades(c, "b200", fid, sides, s1["raw"], t_serve)
    if deb is not None and dsides:
        paper_trades(c, "b200_debut", fid, dsides, deb, t_serve, max_books=1)
    st["post_snaps"] = [t_serve + 60 * k for k in (2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30)]
    c.save()


def b200_pass(c: Ctx, simulate: bool) -> None:
    d = now_et()
    st = c.state.setdefault("b200", {})
    sunday = d.date()
    if st.get("sunday") != sunday.isoformat():
        st.clear(); st["sunday"] = sunday.isoformat(); st["seen"] = []
    # scheduled snapshots (in a thread so the source keeps its 15 s cadence)
    threads = []
    due = []
    if not st.get("fired"):
        mins = d.hour * 60 + d.minute
        last = st.get("last_snap_min", -999)
        if (14 * 60 + 56 <= mins <= 15 * 60 + 10 and mins != last) or (mins > 15 * 60 + 10 and mins - last >= 10):
            due.append(("sched", mins))
    else:
        ps = st.get("post_snaps", [])
        nowt = time.time()
        if ps and ps[0] <= nowt:
            while ps and ps[0] <= nowt:
                ps.pop(0)
            due.append(("post", None))
    for tag, mins in due:
        th = threading.Thread(target=b200_snapshot, args=(c, tag)); th.start(); threads.append(th)
        if mins is not None:
            st["last_snap_min"] = mins
    fast = hm(d) <= B200_FAST_END or simulate
    end = c.t0 + (PASS_S if fast else 5)
    if simulate:   # test only: next Sunday's event, latest B200 article from the search endpoint
        sunday = d.date() + dt.timedelta(days=(6 - d.weekday()) % 7)
    url = BB_URL + ("&categories=8546590" if simulate else "")
    while not st.get("fired"):
        s, txt, h, t_rcv = c.web(url, bust=True)
        posts = json.loads(txt) if s == 200 and txt.strip().startswith("[") else []
        c.state.setdefault("bb_polls", 0); c.state["bb_polls"] += 1
        hit = None
        for p in sorted(posts, key=lambda p: p.get("date_gmt") or ""):
            try:
                pd = dt.datetime.fromisoformat(p["date_gmt"]).replace(tzinfo=dt.timezone.utc).astimezone(ET)
            except Exception:
                continue
            ti = clean_title(p["title"]["rendered"])
            if (pd.date() == sunday or simulate) and is_b200_no1_title(ti) and p["id"] not in st["seen"]:
                hit = p; break
        if s != 200:
            c.log({"row": "source_error", "leg": "b200", "status": s})
        if hit:
            st["seen"].append(hit["id"])
            b200_fire(c, hit, t_rcv, sunday, simulate)
            break
        if time.time() + POLL_FAST_S > end:
            break
        time.sleep(POLL_FAST_S)
    for th in threads:
        th.join()
    c.save()


# ------------------------------------------------------------------------------------------------ HOUSE leg
def house_window(d: dt.datetime, active: bool) -> bool:
    h = d.hour
    if active:
        return h >= 8 or h < 3
    return d.weekday() < 5 and (h >= 9 or h < 1)


def house_init_next(c: Ctx, year: int) -> int:
    def ok(n):
        s, t, _, _ = c.web(CLERK.format(y=year, n=n))
        return s == 200 and "<rollcall-vote" in t
    if not ok(1):
        return 1
    lo, hi = 1, 2
    while ok(hi):
        lo, hi = hi, hi * 2
    while hi - lo > 1:
        m = (lo + hi) // 2
        lo, hi = (m, hi) if ok(m) else (lo, m)
    return lo + 1


def parse_roll(x: str) -> dict:
    g = lambda tag: (re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", x, re.S) or [None, None])[1]
    at = re.search(r'<action-time time-etz="([^"]*)"', x)
    votes = re.findall(r'<legislator name-id="([^"]*)" sort-field="([^"]*)" unaccented-name="([^"]*)" party="([^"]*)" '
                       r'state="([^"]*)"[^>]*>.*?</legislator>\s*<vote>(.*?)</vote>', x, re.S)
    return {"legis_num": g("legis-num"), "question": g("vote-question"), "result": g("vote-result"),
            "action_date": g("action-date"), "action_time": at.group(1) if at else None, "desc": (g("vote-desc") or "")[:200],
            "votes": [{"id": v[0], "sort": v[1], "name": v[2], "party": v[3], "state": v[4], "vote": v[5]} for v in votes]}


def legis_rx(legis_num: str):
    """'H R 6938' -> regex matching 'H.R. 6938', 'HR6938', 'H. R. 6938' (whole number only); None if unparseable."""
    m = re.match(r"^\s*([A-Za-z .]+?)\s*(\d+)\s*$", legis_num or "")
    if not m:
        return None
    letters = re.sub(r"[^A-Z]", "", m.group(1).upper())
    if not letters:
        return None
    return re.compile(r"(?<![A-Z0-9])" + r"[\s.]*".join(letters) + r"[\s.]*" + m.group(2) + r"(?!\d)")


def house_decide(roll: dict, markets: list[dict]) -> dict:
    """Frozen ambiguity rule (see module doc). Returns {ticker: side} and per-market reasons."""
    sides, why = {}, {}
    q = (roll.get("question") or "").strip().lower()
    lrx = legis_rx(roll.get("legis_num"))
    yeas = sum(1 for v in roll["votes"] if v["vote"] in ("Yea", "Aye"))
    for m in markets:
        tk = m["ticker"]; rules = (m.get("rules_primary") or "") + " " + (m.get("title") or "")
        if q not in PASSAGE_Q:
            why[tk] = "question"; continue
        if lrx is None or not lrx.search(rules.upper()):
            why[tk] = "legis_num_not_in_rules"; continue
        rl = rules.lower()
        if not re.search(r"vote[sd]? (yes|yea|in favor|for)\b|yea votes|votes in favor", rl) or re.search(r"vote[sd]? (no|nay|against)\b", rl):
            why[tk] = "direction"; continue
        if m.get("floor_strike") is not None or m.get("cap_strike") is not None:
            fl, cp, stp = m.get("floor_strike"), m.get("cap_strike"), m.get("strike_type")
            if stp == "greater" and fl is not None:
                sides[tk] = "yes" if yeas > fl else "no"
            elif stp == "less" and cp is not None:
                sides[tk] = "yes" if yeas < cp else "no"
            elif stp == "between" and fl is not None and cp is not None:
                sides[tk] = "yes" if fl <= yeas <= cp else "no"
            else:
                why[tk] = "strike_type"; continue
            why[tk] = f"count yeas={yeas}"; continue
        sub = m.get("yes_sub_title") or ""
        stm = re.search(r"\(([A-Z]{2})\)", sub)
        last = norm(re.sub(r"\(.*?\)", "", sub)).split()
        if not last:
            why[tk] = "no_name"; continue
        hits = [v for v in roll["votes"] if norm(v["name"]) == last[-1] or norm(v["sort"]).startswith(last[-1])]
        if stm:
            hits = [v for v in hits if v["state"] == stm.group(1)]
        if len(hits) != 1:
            why[tk] = f"name_matches={len(hits)}"; continue
        sides[tk] = "yes" if hits[0]["vote"] in ("Yea", "Aye") else "no"
        why[tk] = f"member {hits[0]['name']}-{hits[0]['state']} {hits[0]['vote']}"
    return {"sides": sides, "why": why}


def house_pass(c: Ctx) -> None:
    d = now_et()
    st = c.state.setdefault("house", {})
    open_ser = {s: v for s, v in c.state.get("watch", {}).get("house", {}).items() if v.get("open")}
    active = bool(open_ser)
    if not house_window(d, active):
        return
    year = d.year if d.month > 1 or d.day > 1 or d.hour >= 3 else d.year - 1
    if st.get("year") != year or not st.get("next"):
        st["year"] = year; st["next"] = house_init_next(c, year)
        c.log({"row": "house_init", "year": year, "next": st["next"]})
    if not active and time.time() - st.get("last_poll", 0) < 55:
        return
    end = c.t0 + (PASS_S if active else 3)
    while True:
        s, txt, h, t_rcv = c.web(CLERK.format(y=year, n=st["next"]))
        st["last_poll"] = time.time()
        if s == 200 and "<rollcall-vote" in txt:
            roll = parse_roll(txt)
            n = st["next"]; st["next"] = n + 1
            fid = f"house-{year}-{n}"
            c.log({"row": "house_roll", "fire": fid, "first_serve": t_rcv, **{k: v for k, v in roll.items() if k != "votes"},
                   "n_votes": len(roll["votes"]), "yeas": sum(1 for v in roll["votes"] if v["vote"] in ("Yea", "Aye"))})
            if active:
                house_fire(c, fid, roll, t_rcv, open_ser)
            c.save()
            continue
        if time.time() + POLL_FAST_S > end:
            break
        time.sleep(POLL_FAST_S)
    c.save()


def house_fire(c: Ctx, fid: str, roll: dict, t_serve: float, open_ser: dict) -> None:
    sers = sorted(open_ser)[:4]
    snaps0 = {}
    for s in sers:
        d = c.kalshi(f"/markets?series_ticker={s}&status=open&limit=200")
        if d is not None:
            snaps0[s] = d
            c.log({"row": "snap", "leg": "house", "tag": "fire", "series": s, "t_rcv": d.get("_t_rcv"), "markets": compact_markets(d)})
    ms = [m for d in snaps0.values() for m in d.get("markets") or []]
    dec = house_decide(roll, ms)
    c.log({"row": "decision", "leg": "house", "fire": fid, **dec})
    if not dec["sides"]:
        c.state.setdefault("house", {})["post_snaps"] = {"fire": fid, "series": sers, "at": [t_serve + 60 * k for k in (1, 5, 10, 20, 30)]}
        return
    wait = t_serve + FILL_DELAY_S - time.time()
    if wait > 0:
        time.sleep(wait)
    for s in sers:
        tks = [t for t in dec["sides"] if any(m["ticker"] == t for m in (snaps0.get(s) or {}).get("markets") or [])]
        if not tks:
            continue
        d = c.kalshi(f"/markets?series_ticker={s}&status=open&limit=200")
        if d is None:
            c.log({"row": "fill_missed_budget", "leg": "house", "fire": fid, "series": s}); continue
        c.log({"row": "snap", "leg": "house", "tag": "fill", "series": s, "t_rcv": d.get("_t_rcv"), "markets": compact_markets(d)})
        paper_trades(c, "house", fid, {t: dec["sides"][t] for t in tks}, d, t_serve, max_books=1)
    c.state.setdefault("house", {})["post_snaps"] = {"fire": fid, "series": sers, "at": [t_serve + 60 * k for k in (5, 10, 20, 30)]}


def post_snaps(c: Ctx, leg: str) -> None:
    ps = c.state.get(leg, {}).get("post_snaps")
    if not isinstance(ps, dict) or not ps.get("at") or ps["at"][0] > time.time():
        return
    while ps["at"] and ps["at"][0] <= time.time():
        ps["at"].pop(0)
    for s in ps["series"][:3]:
        d = c.kalshi(f"/markets?series_ticker={s}&status=open&limit=200")
        if d is not None:
            c.log({"row": "snap", "leg": leg, "tag": "post", "fire": ps["fire"], "series": s, "t_rcv": d.get("_t_rcv"),
                   "markets": compact_markets(d)})


# ------------------------------------------------------------------------------------------------ SCOTUS leg
def scotus_window(d: dt.datetime) -> bool:
    return d.weekday() < 5 and d.month not in (8, 9) and (9, 58) <= hm(d) <= (13, 0)


def scotus_pass(c: Ctx) -> None:
    d = now_et()
    if not scotus_window(d):
        return
    st = c.state.setdefault("scotus", {})
    term = d.year % 100 if d.month >= 10 else d.year % 100 - 1
    open_ser = sorted(s for s, v in c.state.get("watch", {}).get("scotus", {}).items() if v.get("open"))
    active = bool(open_ser)
    if not active and time.time() - st.get("last_poll", 0) < 55:
        return
    end = c.t0 + (PASS_S if active else 3)
    while True:
        s, txt, h, t_rcv = c.web(SCOTUS.format(term=term), bust=True)
        st["last_poll"] = time.time()
        pdfs = sorted(set(re.findall(rf"/opinions/{term}pdf/([0-9A-Za-z\-]+_[a-z0-9]{{4}})\.pdf", txt))) if s == 200 else []
        known = set(st.get("known", []))
        if s == 200 and not st.get("known_init"):
            st["known"] = pdfs; st["known_init"] = True; known = set(pdfs)
            c.log({"row": "scotus_init", "term": term, "n_pdfs": len(pdfs)})
        new = [p for p in pdfs if p not in known]
        if new:
            st["known"] = sorted(known | set(new))
            for p in new:
                fid = f"scotus-{term}-{p}"
                c.log({"row": "scotus_opinion", "fire": fid, "first_serve": t_rcv, "pdf": p, "last_modified": h.get("Last-Modified")})
            if active:
                for sser in open_ser[:3]:
                    dd = c.kalshi(f"/markets?series_ticker={sser}&status=open&limit=200")
                    if dd is not None:
                        c.log({"row": "snap", "leg": "scotus", "tag": "fire", "series": sser, "t_rcv": dd.get("_t_rcv"), "markets": compact_markets(dd)})
                st["post_snaps"] = {"fire": f"scotus-{term}-{new[0]}", "series": open_ser[:3], "at": [t_rcv + 60 * k for k in (1, 5, 15)]}
            c.save()
        if time.time() + POLL_FAST_S > end:
            break
        time.sleep(POLL_FAST_S)
    c.save()


# ------------------------------------------------------------------------------------------------ watch lists, structure, settlement
def discovery(c: Ctx) -> None:
    w = c.state.setdefault("watch", {"house": {}, "scotus": {}})
    day = now_et().date().isoformat()
    if w.get("day") != day:
        w["day"] = day; w["calls_today"] = 0
    todo = []
    for leg, sers, ttl_open, ttl_closed in (("house", HOUSE_SERIES, 86400, 3 * 86400), ("scotus", scotus_series(), 86400, 7 * 86400)):
        for s in sers:
            v = w.setdefault(leg, {}).get(s, {})
            ttl = ttl_open if v.get("open") else ttl_closed
            if time.time() - v.get("checked", 0) > ttl:
                todo.append((v.get("checked", 0), leg, s))
    todo.sort()
    n = 0
    for _, leg, s in todo:
        if n >= DISC_PER_PASS or w["calls_today"] >= DISC_PER_DAY:
            break
        d = c.kalshi(f"/markets?series_ticker={s}&status=open&limit=200")
        if d is None:
            break
        n += 1; w["calls_today"] += 1
        ms = d.get("markets") or []
        w[leg][s] = {"checked": time.time(), "open": len(ms),
                     "markets": [{k: m.get(k) for k in ("ticker", "event_ticker", "yes_sub_title", "close_time", "strike_type", "floor_strike", "cap_strike")} for m in ms[:60]]}
        c.log({"row": "watch", "leg": leg, "series": s, "open": len(ms), "err": d.get("_error"),
               "sample": [m.get("ticker") for m in ms[:5]]})


def structure(c: Ctx) -> None:
    d = now_et()
    st = c.state.setdefault("struct", {})
    wk = d.strftime("%G-%V")
    if d.weekday() != 0 or d.hour < 10 or st.get("week") == wk:
        return
    out = {}
    for s in STRUCT_SERIES:
        r = c.kalshi(f"/markets?series_ticker={s}&status=open&limit=200")
        if r is None:
            return
        out[s] = sorted({m.get("close_time") for m in r.get("markets") or []})
    st["week"] = wk
    c.log({"row": "structure", "close_times": out,
           "note": "Netflix Top 10 publishes Tuesday ~12:00 PT, Hot 100 top 10 Monday/Tuesday; tradeable only if close_time is after"})


def settle(c: Ctx) -> None:
    d = now_et()
    if d.hour < 12:
        return
    st = c.state.setdefault("settle", {})
    if st.get("day") == d.date().isoformat():
        return
    left = []
    n = 0
    for tr in c.state.get("open_trades", []):
        if n >= 5:
            left.append(tr); continue
        r = c.kalshi(f"/markets/{tr['t']}")
        n += 1
        m = (r or {}).get("market") or {}
        res = m.get("result")
        if res in ("yes", "no"):
            won = res == tr["side"]
            pnl = tr["qty"] * ((1.0 if won else 0.0) - tr["px"]) - tr["fee"]
            c.log({"row": "settle", **tr, "result": res, "won": won, "pnl": round(pnl, 4),
                   "ret_per_dollar": round(pnl / (tr["qty"] * tr["px"]), 4)})
        else:
            left.append(tr)
    c.state["open_trades"] = left
    if n == 0 or not left:
        st["day"] = d.date().isoformat()


# ------------------------------------------------------------------------------------------------ main
def main() -> None:
    args = sys.argv[1:]
    test = "--test" in args
    simulate = "--simulate-b200" in args
    if simulate and not test:
        print("--simulate-b200 needs --test"); return
    mc = int(args[args.index("--max-calls") + 1]) if "--max-calls" in args else MAX_CALLS
    c = Ctx(test, mc)
    lockf = (c.dir / ".lock").open("w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    d = now_et()
    try:
        if b200_window(d) or simulate:
            b200_pass(c, simulate)
        else:
            post_snaps(c, "house"); post_snaps(c, "scotus")
            house_pass(c)
            scotus_pass(c)
            settle(c)
            structure(c)
            if c.calls < c.max_calls:
                discovery(c)
    except Exception as e:  # never die silently: log and keep state
        c.log({"row": "error", "err": repr(e)[:300]})
    finally:
        c.log({"row": "pass", "kalshi_calls": c.calls, "dur_s": round(time.time() - c.t0, 1), "et": d.isoformat()})
        c.save()


if __name__ == "__main__":
    main()
