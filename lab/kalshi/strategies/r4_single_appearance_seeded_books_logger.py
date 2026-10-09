"""r4_single_appearance_seeded_books_logger: forward PAPER log of C1S (frozen in
data/kalshi_lab/strategies/r4_single_appearance_seeded_books/preregistration.json, written before the first pass).

One pass per invocation (launchd/cron; recommended every 10 min at minutes 3,13,...,53). At most 10 Kalshi calls per
pass (R4SA_MAX_CALLS or --max-calls may only lower it), every call through lab.us.data_refresh.fetch (temperature-bot
idle window, 429 retries) and logged with tag 'logger' in the family's calls.log. It never places orders.

Rule: every market of the 30 single-appearance mention series is evaluated ONCE at H1 = first top of the UTC hour
>= open_time + 1 h, from a quote read in [H1, H1 + 12 min] (/markets?event_ticker). Skip if any market of the event has
closed, if not 0 < yes_bid < yes_ask < 1, if the spread is < 2c, or if post + 120 s >= C_planned = min(T_day, close).
T_day = 00:00 ET on D* = min(event-ticker date, ET date of the earliest expected_expiration_time - 1 day). Virtual
SELL-YES at s = ask - 1c for floor(5 / (1 - s)) contracts plus the mirror BUY-YES at b = bid + 1c for floor(5 / b).
Cancel C = min(T_day, first close of any market of the event, own close).
Fills (gate amendment c): trade prints strictly after post + 120 s and strictly before C, block trades excluded;
'through' = yes_price > s (mirror: < b); 'any' = >= s (<= b); 'queue' (probed orders) = through + prints at s after the
size already resting at our NO price when we posted.
Stages per pass (priority order): missed bookkeeping (0 calls); entries due now (1 call per event); post probes
(<= 2/pass, 10/day); event checks after C or a seen close (first close + results); fill jobs (1 call per page);
discovery rotation (<= 5/pass).
Files (data/kalshi_lab/strategies/r4_single_appearance_seeded_books/): forward.jsonl (rows listing / entry / missed /
probe / event_check / fill / settle / pass), state.json, trades/<ticker>.json, .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_single_appearance_seeded_books_logger [--max-calls N] [--selftest]"""
from __future__ import annotations
import datetime as dt, fcntl, json, math, os, re, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_single_appearance_seeded_books_api import live_get

OUT = Path(os.environ.get("R4SA_OUT", "data/kalshi_lab/strategies/r4_single_appearance_seeded_books"))
ET = ZoneInfo("America/New_York")

# ------------------------------------------------------------------------------------------------ frozen constants
SERIES = ["KXJPOWMENTION", "KXPOWELLMENTION", "KXWARSHMENTION", "KXECBMENTION", "KXWALLERMENTION", "KXFEDGOVMENTION",
          "KXDJTJOINTSESSION", "KXPRESMENTION", "KXBIDENMENTION", "KXUNMENTION", "KXKINGMENTION", "KXSTARMERMENTIONB",
          "KXSTARMERMENTION", "KXNETANYAHUMENTION", "KXCARNEYMENTION", "KXDEBATEMENTION", "KXNYCMAYORDEBATEMENTION",
          "KXNYCMDEBMENTION", "KXCANDEBMENTION", "KXAWARDMENTION", "KXAPPLEMENTION", "KXWWDCMENTION", "KXJENSENMENTION",
          "KXALTMANMENTION", "KXWEFMENTION", "KXINFANTINOMENTION", "KXATHLETEMENTION", "KXFIGHTMENTION", "KXPAULMENTION",
          "KXFURYMENTION"]
STAKE = 5.0
ENTRY_WINDOW_S = 12 * 60
POST_GAP_S = 120
MAX_CALLS = 10
DISC_MAX = 5
DISC_GAP_ACTIVE_S = 20 * 60
DISC_GAP_DORMANT_S = 50 * 60       # every series about hourly: a new listing is seen before its H1 (open + 1-2 h)
ACTIVE_WINDOW_S = 7 * 86400
PROBE_PER_PASS = 2
PROBE_PER_DAY = 10
CHECK_DELAY_S = 5 * 60          # event check this long after C (or after a close was seen)
CHECK_RETRY_S = 2 * 3600
CHECK_MAX = 60
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended"}
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
TICKER_DATE = re.compile(r"-[A-Z]*?(\d{2})([A-Z]{3})(\d{2})")


# ------------------------------------------------------------------------------------------------ helpers
def ts_of(s) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def cents(x) -> int | None:
    try:
        return int(round(float(x) * 100))
    except (TypeError, ValueError):
        return None


def fnum(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def h1_of(open_ts: float) -> int:
    return int(math.ceil((open_ts + 3600) / 3600.0) * 3600)


def ticker_date(e: str) -> dt.date | None:
    g = TICKER_DATE.search(e)
    if not g:
        return None
    mon = MONTHS.get(g.group(2).lower())
    try:
        return dt.date(2000 + int(g.group(1)), mon, int(g.group(3))) if mon else None
    except ValueError:
        return None


def t_day_of(event_ticker: str, exps: list[float]) -> tuple[int | None, str | None]:
    """T_day = 00:00 ET on D* (identical to r4_single_appearance_seeded_books_data.sched_date + t_day)."""
    c = []
    d = ticker_date(event_ticker)
    if d:
        c.append(d)
    if exps:
        c.append(dt.datetime.fromtimestamp(min(exps), ET).date() - dt.timedelta(days=1))
    if not c:
        return None, None
    D = min(c)
    return int(dt.datetime(D.year, D.month, D.day, tzinfo=ET).timestamp()), D.isoformat()


def word_of(m: dict) -> str:
    return ((m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or "")[:80]


def series_of(t: str) -> str:
    return t.split("-")[0]


def book_levels(ob: dict) -> tuple[list, list]:
    """(NO bids, YES bids) as [[cents, size], ...] from /orderbook (orderbook_fp dollars or legacy cents)."""
    fp = ob.get("orderbook_fp") or {}
    if fp:
        return ([[cents(p), fnum(q)] for p, q in fp.get("no_dollars") or []], [[cents(p), fnum(q)] for p, q in fp.get("yes_dollars") or []])
    o = ob.get("orderbook") or {}
    return [[int(p), fnum(q)] for p, q in o.get("no") or []], [[int(p), fnum(q)] for p, q in o.get("yes") or []]


def fill_metrics(prints: list, s_c: int, N: int, b_c: int, Nb: int, lo: float, hi: float, queue_ahead: float | None) -> dict:
    """prints: [ts, yes_cents, count, taker_side, is_block]; window lo < ts < hi."""
    w = [p for p in prints if p[0] is not None and lo < p[0] < hi and not p[4] and p[1] is not None]
    thr = sum(p[2] for p in w if p[1] > s_c); anyc = sum(p[2] for p in w if p[1] >= s_c)
    at_s_yes = sum(p[2] for p in w if p[1] == s_c and p[3] == "yes")
    at_s_no = sum(p[2] for p in w if p[1] == s_c and p[3] != "yes")
    mthr = sum(p[2] for p in w if p[1] < b_c); many = sum(p[2] for p in w if p[1] <= b_c)
    out = {"n_prints": len(w), "thr_cnt": round(thr, 2), "any_cnt": round(anyc, 2), "f_thr": round(min(N, thr) / N, 4),
           "f_any": round(min(N, anyc) / N, 4), "m_thr_cnt": round(mthr, 2), "m_any_cnt": round(many, 2),
           "mf_thr": round(min(Nb, mthr) / Nb, 4), "mf_any": round(min(Nb, many) / Nb, 4),
           "t_first_thr": min((p[0] for p in w if p[1] > s_c), default=None)}
    if queue_ahead is not None:
        q = thr + at_s_no + max(0.0, at_s_yes - queue_ahead)
        out["queue_cnt"] = round(q, 2); out["f_queue"] = round(min(N, q) / N, 4)
    return out


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    def __init__(self, cap: int) -> None:
        self.cap = cap; self.used = 0

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        txt = live_get(path)
        if not txt:
            return {}
        try:
            return json.loads(txt)
        except ValueError:
            return {}


# ------------------------------------------------------------------------------------------------ the logger
class Logger:
    def __init__(self, out: Path, budget, now: float | None = None) -> None:
        self.out = out; self.B = budget; self.now = now or time.time(); self._t0 = time.time()
        self.rows: list[dict] = []; self.stage = defaultdict(int); self.errors: list[str] = []
        sf = out / "state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        s = self.st
        s.setdefault("version", 1); s.setdefault("started", self.now)
        for k in ("series_checked", "series_active", "markets", "events", "orders", "probe_day"):
            s.setdefault(k, {})

    def clock(self) -> float:
        return self.now + (time.time() - self._t0)

    def row(self, typ: str, **kw) -> None:
        r = {"type": typ, "ts": round(kw.pop("ts", None) or self.clock(), 1)}
        r.update(kw); self.rows.append(r)

    def call(self, path: str) -> dict:
        d = self.B.get(path)
        if not d:
            self.errors.append("empty:" + path[:80])
        return d

    def ev(self, e: str, series: str) -> dict:
        return self.st["events"].setdefault(e, {"series": series, "first_seen": self.now, "exps": [], "closed_seen": None,
                                                "first_close": None, "settled": False, "next_check": None, "checks": 0})

    def add_market(self, m: dict, series: str, via: str) -> dict | None:
        t = m["ticker"]; S = self.st["markets"]
        E = self.ev(m["event_ticker"], series)
        x = ts_of(m.get("expected_expiration_time"))
        if x and x not in E["exps"]:
            E["exps"].append(x)
        if t in S:
            return S[t]
        op = ts_of(m.get("open_time"))
        if op is None:
            return None
        H1 = h1_of(op)
        if H1 + ENTRY_WINDOW_S <= self.now:
            status = "prestart" if H1 < self.st["started"] else "missed"
        else:
            status = "tracked"
        S[t] = {"e": m["event_ticker"], "s": series, "open": op, "H1": H1, "status": status, "seen_open": None, "word": word_of(m)}
        self.row("listing", ticker=t, e=m["event_ticker"], series=series, open=op, H1=H1, status=status, via=via, word=word_of(m),
                 created=ts_of(m.get("created_time")), mstatus=m.get("status"), exp=x, occ=ts_of(m.get("occurrence_datetime")),
                 close_time=ts_of(m.get("close_time")))
        if status == "missed":
            self.row("missed", ticker=t, e=m["event_ticker"], H1=H1, reason="discovered_late")
        return S[t]

    # ---- stage 0
    def mark_missed(self) -> None:
        for t, x in self.st["markets"].items():
            if x["status"] == "tracked" and self.now >= x["H1"] + ENTRY_WINDOW_S:
                x["status"] = "missed"
                self.row("missed", ticker=t, e=x["e"], H1=x["H1"], reason="no_quote_in_window")

    def due_events(self) -> list[str]:
        due = {}
        for t, x in self.st["markets"].items():
            if x["status"] == "tracked" and x["H1"] <= self.now < x["H1"] + ENTRY_WINDOW_S:
                due[x["e"]] = min(due.get(x["e"], 9e18), x["H1"])
        return sorted(due, key=lambda e: due[e])

    # ---- stage 1: entries
    def entries(self, done: set) -> list[str]:
        posted = []
        for e in self.due_events():
            if e in done or self.B.left <= 0:
                continue
            series = self.st["events"].get(e, {}).get("series") or series_of(e)
            d = self.call(f"/markets?event_ticker={e}&limit=1000")
            ms = d.get("markets") or []
            if not ms:
                continue
            done.add(e); self.stage["entry_calls"] += 1
            tq = self.clock()
            started = any((m.get("status") in CLOSED) or (m.get("result") in ("yes", "no")) for m in ms)
            for m in ms:
                x = self.add_market(m, series, "entry")
                if x and m.get("status") == "active":
                    x["seen_open"] = tq
            E = self.st["events"][e]
            T_day, D = t_day_of(e, E["exps"])
            for m in ms:
                x = self.st["markets"].get(m["ticker"])
                if not x or x["status"] != "tracked" or not (x["H1"] <= tq < x["H1"] + ENTRY_WINDOW_S):
                    continue
                posted += self.evaluate(m, x, e, series, tq, started, T_day, D)
        return posted

    def evaluate(self, m: dict, x: dict, e: str, series: str, tq: float, started: bool, T_day: int | None, D: str | None) -> list[str]:
        t = m["ticker"]; x["status"] = "entered"
        ask, bid = cents(m.get("yes_ask_dollars")), cents(m.get("yes_bid_dollars"))
        close = ts_of(m.get("close_time"))
        Cp = min(c for c in (T_day, close) if c is not None) if (T_day or close) else None
        base = {"ticker": t, "e": e, "series": series, "H1": x["H1"], "lag_s": round(tq - x["H1"], 1), "open": x["open"],
                "ask_c": ask, "bid_c": bid, "ask_size": fnum(m.get("yes_ask_size_fp")), "bid_size": fnum(m.get("yes_bid_size_fp")),
                "vol": fnum(m.get("volume_fp")), "mstatus": m.get("status"), "word": x.get("word"), "D": D, "T_day": T_day,
                "C_planned": Cp, "close_time": close, "exp": ts_of(m.get("expected_expiration_time")),
                "occ": ts_of(m.get("occurrence_datetime"))}
        reason = None
        if started:
            reason = "event_started"
        elif m.get("status") != "active":
            reason = "market_not_active"
        elif T_day is None:
            reason = "no_schedule_date"
        elif ask is None or bid is None or not (0 < bid < ask < 100):
            reason = "no_two_sided_quote"
        elif ask - bid < 2:
            reason = "spread_lt_2c"
        elif tq + POST_GAP_S >= Cp:
            reason = "no_time_before_cancel"
        if reason:
            self.row("entry", ts=tq, posted=False, reason=reason, **base)
            return []
        s_c = ask - 1; N = int(500 // (100 - s_c)); b_c = bid + 1; Nb = int(500 // b_c)
        self.row("entry", ts=tq, posted=True, reason=None, s_c=s_c, N=N, no_px=round(1 - s_c / 100, 2), b_c=b_c, Nb=Nb, **base)
        self.st["orders"][t] = {"e": e, "post": tq, "s_c": s_c, "N": N, "b_c": b_c, "Nb": Nb, "C_planned": Cp, "T_day": T_day,
                                "close": close, "probe": False, "probes": {}, "job": None, "fill_done": False}
        self.stage["posted"] += 1
        return [t]

    # ---- stage 2: queue probes at post
    def post_probes(self, posted: list[str]) -> None:
        day = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).strftime("%Y-%m-%d")
        for t in posted:
            if self.stage["post_probes"] >= PROBE_PER_PASS or self.st["probe_day"].get(day, 0) >= PROBE_PER_DAY or self.B.left <= 0:
                break
            o = self.st["orders"][t]
            ob = self.call(f"/markets/{t}/orderbook")
            self.stage["post_probes"] += 1; self.st["probe_day"][day] = self.st["probe_day"].get(day, 0) + 1
            if not ob:
                continue
            no, yes = book_levels(ob)
            q_c = 100 - o["s_c"]
            at_q = sum(sz for p, sz in no if p == q_c)
            o["probe"] = True; o["probes"]["post"] = {"ts": self.clock(), "at_q": at_q}
            self.row("probe", ticker=t, e=o["e"], phase="post", since_post_s=round(self.clock() - o["post"], 1), s_c=o["s_c"], q_c=q_c,
                     size_at_our_no_price=round(at_q, 2), best_no_c=max((p for p, sz in no if sz > 0), default=None),
                     best_yes_bid_c=max((p for p, sz in yes if sz > 0), default=None),
                     no_depth=sum(sz for p, sz in no), yes_depth=sum(sz for p, sz in yes),
                     size_at_mirror_yes_price=round(sum(sz for p, sz in yes if p == o["b_c"]), 2))

    # ---- stage 3: event checks (first close, results)
    def checks(self) -> None:
        due = []
        for e in sorted({o["e"] for o in self.st["orders"].values()}):
            E = self.st["events"].get(e)
            if not E or E["settled"] or E["checks"] >= CHECK_MAX:
                continue
            Cp = min(o["C_planned"] for o in self.st["orders"].values() if o["e"] == e)
            nc = E.get("next_check") or (min(Cp, E["closed_seen"] or 9e18) + CHECK_DELAY_S)
            if self.now >= nc:
                due.append((nc, e))
        for _, e in sorted(due):
            if self.B.left <= 0:
                break
            self.event_check(e)

    def event_check(self, e: str) -> None:
        E = self.st["events"][e]
        d = self.call(f"/markets?event_ticker={e}&limit=1000")
        E["checks"] += 1; E["next_check"] = self.now + CHECK_RETRY_S * (1 if E["checks"] < 12 else 6)
        td = min((o["T_day"] for o in self.st["orders"].values() if o["e"] == e and o.get("T_day")), default=None)
        if td and self.now < td + 26 * 3600:
            E["next_check"] = max(E["next_check"], td + 26 * 3600)       # the appearance is on D*: results after its end
        ms = d.get("markets") or []
        if not ms:
            return
        self.stage["check_calls"] += 1
        closes = [ts_of(m.get("close_time")) for m in ms if m.get("status") in CLOSED or m.get("result") in ("yes", "no", "void")]
        closes = [c for c in closes if c]
        fc = min(closes) if closes else None
        if fc and (E["first_close"] is None or fc < E["first_close"]):
            E["first_close"] = fc
        res = {m["ticker"]: (m.get("result") or "") for m in ms}
        self.row("event_check", e=e, first_close=E["first_close"], n_markets=len(ms), n_closed=len(closes),
                 n_results=sum(1 for v in res.values() if v in ("yes", "no", "void")))
        # fill jobs: once C has passed (C = min(planned cancel, the event's first close))
        for t, o in self.st["orders"].items():
            if o["e"] != e or o.get("job"):
                continue
            C = min(o["C_planned"], E["first_close"] or 9e18)
            if self.now >= C + CHECK_DELAY_S or (E["first_close"] and E["first_close"] <= self.now):
                o["C"] = C
                o["job"] = {"lo": o["post"] + POST_GAP_S, "hi": C, "cursor": "", "pages": 0}
        order_t = [t for t, o in self.st["orders"].items() if o["e"] == e]
        if order_t and all(res.get(t) in ("yes", "no", "void") for t in order_t):
            E["settled"] = True
            self.row("settle", e=e, series=E["series"], first_close=E["first_close"],
                     markets={m["ticker"]: [m.get("result") or "", ts_of(m.get("close_time")), m.get("status")] for m in ms})

    # ---- stage 4: fills
    def fill_jobs(self) -> None:
        for _, t in sorted((o["post"], t) for t, o in self.st["orders"].items() if o.get("job") and not o["fill_done"]):
            if self.B.left <= 0:
                break
            self.fill_page(t)

    def fill_page(self, t: str) -> None:
        o = self.st["orders"][t]; j = o["job"]
        lo, hi = int(j["lo"]), int(math.ceil(j["hi"]))
        if hi <= lo:
            self.finish_fill(t, []); return
        path = f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={j['cursor']}" if j["cursor"] else "")
        d = self.call(path)
        if "trades" not in d:
            return
        self.stage["fill_calls"] += 1
        tf = self.out / "trades" / f"{t}.json"; tf.parent.mkdir(parents=True, exist_ok=True)
        cur = json.loads(tf.read_text()) if (tf.exists() and j["pages"] > 0) else {"lo": j["lo"], "hi": j["hi"], "prints": []}
        for x in d.get("trades") or []:
            cur["prints"].append([ts_of(x.get("created_time")), cents(x.get("yes_price_dollars")), fnum(x.get("count_fp") or x.get("count")),
                                  x.get("taker_side"), bool(x.get("is_block_trade"))])
        tf.write_text(json.dumps(cur))
        j["pages"] += 1; j["cursor"] = d.get("cursor") or ""
        if not j["cursor"] or not d.get("trades"):
            self.finish_fill(t, cur["prints"])

    def finish_fill(self, t: str, prints: list) -> None:
        o = self.st["orders"][t]; j = o["job"]
        qa = (o["probes"].get("post") or {}).get("at_q")
        fm = fill_metrics(prints, o["s_c"], o["N"], o["b_c"], o["Nb"], j["lo"], j["hi"], qa)
        o["fill_done"] = True
        self.row("fill", ticker=t, e=o["e"], post=o["post"], lo=j["lo"], hi=j["hi"], C=o.get("C"), s_c=o["s_c"], N=o["N"],
                 b_c=o["b_c"], Nb=o["Nb"], pages=j["pages"], **fm)
        self.stage["fills"] += 1

    # ---- stage 5: discovery
    def discover(self) -> None:
        sc = self.st["series_checked"]; act = self.st["series_active"]
        def gap(s):
            return DISC_GAP_ACTIVE_S if self.now - act.get(s, 0) < ACTIVE_WINDOW_S else DISC_GAP_DORMANT_S
        for s in sorted(SERIES, key=lambda s: sc.get(s, 0)):
            if self.stage["disc_calls"] >= DISC_MAX or self.B.left <= 0:
                break
            if self.now - sc.get(s, 0) < gap(s):
                continue
            d = self.call(f"/markets?series_ticker={s}&status=open&limit=1000")
            self.stage["disc_calls"] += 1
            if "markets" not in d:
                continue
            tq = self.clock(); sc[s] = tq
            present = set()
            for m in d.get("markets") or []:
                if m.get("status") not in (None, "active"):
                    continue
                x = self.add_market(m, s, "discovery")
                if x:
                    x["seen_open"] = tq; present.add(m["ticker"]); act[s] = tq
            if d.get("cursor"):
                continue
            for t, x in self.st["markets"].items():
                if x["s"] == s and x.get("seen_open") and t not in present:
                    E = self.ev(x["e"], s)
                    if not E["closed_seen"]:
                        E["closed_seen"] = tq
                        self.row("event_closed_seen", ts=tq, e=x["e"], series=s, ticker=t, last_seen_open=x["seen_open"])

    # ---- one pass
    def run(self) -> dict:
        done: set = set()
        try:
            self.mark_missed()
            posted = self.entries(done)
            self.post_probes(posted)
            self.discover()
            posted2 = self.entries(done)
            self.post_probes(posted2)
            self.checks()
            self.fill_jobs()
        except BudgetExhausted:
            pass
        except Exception as ex:
            self.errors.append(f"{type(ex).__name__}: {str(ex)[:200]}")
        n_tr = sum(1 for x in self.st["markets"].values() if x["status"] == "tracked")
        self.row("pass", calls=self.B.used, stages=dict(self.stage), errors=self.errors[:10], tracked=n_tr,
                 markets=len(self.st["markets"]), orders=len(self.st["orders"]), dur_s=round(time.time() - self._t0, 1))
        self.save()
        return self.rows[-1]

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "forward.jsonl").open("a") as f:
            f.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in self.rows))
        fd, tmp = tempfile.mkstemp(dir=self.out, prefix=".state.")
        with os.fdopen(fd, "w") as f:
            json.dump(self.st, f, separators=(",", ":"))
        os.replace(tmp, self.out / "state.json")


def run_pass(max_calls: int = MAX_CALLS) -> dict | None:
    OUT.mkdir(parents=True, exist_ok=True)
    lock = open(OUT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another pass is running; exit", flush=True)
        return None
    try:
        lg = Logger(OUT, Budget(min(max_calls, MAX_CALLS)))
        r = lg.run()
        print(json.dumps(r), flush=True)
        return r
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()


# ------------------------------------------------------------------------------------------------ offline self-test
class FakeBudget:
    def __init__(self, cap: int, responder) -> None:
        self.cap = cap; self.used = 0; self.responder = responder; self.paths = []

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1; self.paths.append(path)
        return self.responder(path)


def selftest() -> None:
    """One event end to end without network: listing -> entry at H1 -> probe -> T_day cancel -> fills -> settle."""
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="r4sa_selftest_"))
    # event on 2099-01-15 (ticker date); T_day = 2099-01-15 00:00 ET; listing on 2099-01-13 13:10 UTC -> H1 = 15:00 UTC
    E = "KXDEBATEMENTION-99JAN15"
    T_day = int(dt.datetime(2099, 1, 15, tzinfo=ET).timestamp())
    op = int(dt.datetime(2099, 1, 13, 13, 10, tzinfo=dt.timezone.utc).timestamp()); H = h1_of(op)
    exp = int(dt.datetime(2099, 1, 30, 14, 0, tzinfo=dt.timezone.utc).timestamp())
    def iso(t): return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    def mk(tk, ask, bid, status="active", result="", close=exp):
        return {"ticker": tk, "event_ticker": E, "status": status, "result": result, "open_time": iso(op), "close_time": iso(close),
                "expected_expiration_time": iso(exp), "occurrence_datetime": iso(exp), "yes_ask_dollars": f"{ask:.4f}",
                "yes_bid_dollars": f"{bid:.4f}", "yes_ask_size_fp": "10", "yes_bid_size_fp": "5", "volume_fp": "0",
                "custom_strike": {"Word": tk[-3:]}, "created_time": iso(op - 60)}
    fc = T_day + 24 * 3600            # appearance on the 15th evening ET
    state = {"phase": "open"}
    def responder(path):
        if path.startswith("/markets?series_ticker=KXDEBATEMENTION&status=open"):
            return {"markets": [mk(E + "-AAA", 0.89, 0.02), mk(E + "-BBB", 0.30, 0.29)] if state["phase"] == "open" else []}
        if path.startswith("/markets?series_ticker="):
            return {"markets": []}
        if path.startswith(f"/markets?event_ticker={E}"):
            if state["phase"] == "open":
                return {"markets": [mk(E + "-AAA", 0.89, 0.02), mk(E + "-BBB", 0.30, 0.29)]}
            return {"markets": [mk(E + "-AAA", 0.99, 0.98, "finalized", "no", fc + 3600), mk(E + "-BBB", 0.99, 0.98, "finalized", "yes", fc)]}
        if "/orderbook" in path:
            return {"orderbook_fp": {"no_dollars": [["0.1200", "30.00"], ["0.0500", "7.00"]], "yes_dollars": [["0.0200", "100.00"]]}}
        if path.startswith("/markets/trades?ticker="):
            post = state["post"]
            return {"trades": [{"created_time": iso(post + 60), "yes_price_dollars": "0.9500", "count_fp": "100", "taker_side": "yes"},
                               {"created_time": iso(post + 600), "yes_price_dollars": "0.8800", "count_fp": "4", "taker_side": "yes"},
                               {"created_time": iso(post + 900), "yes_price_dollars": "0.9000", "count_fp": "30", "taker_side": "yes"},
                               {"created_time": iso(post + 1000), "yes_price_dollars": "0.0200", "count_fp": "9", "taker_side": "no"},
                               {"created_time": iso(T_day + 60), "yes_price_dollars": "0.9900", "count_fp": "500", "taker_side": "yes"}],
                    "cursor": ""}
        return {}
    lg = Logger(tmp, FakeBudget(10, responder), now=H - 50 * 60); lg.st["started"] = H - 5 * 3600
    lg.st["series_checked"] = {s: 1 for s in SERIES if s != "KXDEBATEMENTION"}; lg.run()
    assert len(lg.st["markets"]) == 2 and all(x["status"] == "tracked" for x in lg.st["markets"].values()), lg.st["markets"]
    lg = Logger(tmp, FakeBudget(10, responder), now=H + 90); lg.run()
    o = lg.st["orders"]
    assert set(o) == {E + "-AAA"}, o                         # BBB spread 1c -> skipped
    a = o[E + "-AAA"]
    assert a["s_c"] == 88 and a["N"] == 41 and a["b_c"] == 3 and a["Nb"] == 166 and a["T_day"] == T_day, a
    assert a["probe"] and a["probes"]["post"]["at_q"] == 30.0, a
    state["post"] = a["post"]
    # after T_day: event check -> fill job (window ends at T_day) -> fills; event not settled yet
    lg = Logger(tmp, FakeBudget(10, responder), now=T_day + CHECK_DELAY_S + 10); lg.st["series_checked"] = {s: 9e18 for s in SERIES}; lg.run()
    rows = [json.loads(l) for l in (tmp / "forward.jsonl").open()]
    fl = [r for r in rows if r["type"] == "fill"]
    assert len(fl) == 1, rows[-5:]
    f = fl[0]
    # window (post+120, T_day): 0.88 x4 (at s), 0.90 x30 (through), 0.02 x9 (at b-1 -> mirror through); 0.95 @post+60 and 0.99 after T_day excluded
    assert (f["thr_cnt"], f["any_cnt"], f["m_thr_cnt"], f["m_any_cnt"]) == (30.0, 34.0, 9.0, 9.0), f
    assert f["queue_cnt"] == 30.0 and f["hi"] == T_day, f              # 4 at-s YES-taker prints sit behind 30 resting
    state["phase"] = "closed"
    lg = Logger(tmp, FakeBudget(10, responder), now=fc + 3 * 3600); lg.st["series_checked"] = {s: 9e18 for s in SERIES}
    lg.st["events"][E]["next_check"] = 0; lg.run()
    rows = [json.loads(l) for l in (tmp / "forward.jsonl").open()]
    assert any(r["type"] == "settle" for r in rows), rows[-3:]
    print("selftest OK", {k: f[k] for k in ("thr_cnt", "any_cnt", "queue_cnt", "f_thr", "m_thr_cnt", "mf_thr")})
    shutil.rmtree(tmp)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mc = int(os.environ.get("R4SA_MAX_CALLS", MAX_CALLS))
        if "--max-calls" in sys.argv:
            mc = int(sys.argv[sys.argv.index("--max-calls") + 1])
        run_pass(mc)
