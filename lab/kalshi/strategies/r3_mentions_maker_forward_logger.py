"""r3_mentions_maker_forward_logger: forward PAPER log of the frozen mention-market maker (r2_mentions_baserate C1/C2/C3).

One pass per invocation (launchd or cron runs it; recommended every 10 min at minutes 1,11,...,51 so that the entry quote
is read ~1-2 min after the top of the hour). At most 10 Kalshi calls per pass (R3MM_MAX_CALLS or --max-calls may only
lower it), every call through lab.us.data_refresh.fetch (temperature-bot idle window, 429 retries). It never places orders.

Frozen rule (data/kalshi_lab/strategies/r2_mentions_baserate/preregistration.json, plus amendments a-c written in
data/kalshi_lab/strategies/r3_mentions_maker_forward/preregistration.json before the first forward fill):
  * Every market of the 21 mention series is evaluated ONCE, at H1 = the first top of the UTC hour >= open_time + 1 h,
    from a quote read in [H1, H1 + 12 min] (amendment a). It is skipped (no order) if any market of its event has closed,
    if not 0 < yes_bid and yes_ask < 1, or if yes_ask - yes_bid < 2c. A market whose H1 passes without a quote is
    logged as 'missed' and never entered later.
  * C1: a virtual resting order selling YES at s = yes_ask - 1c (= buy NO at 1 - s) for N = floor(5 / (1 - s))
    contracts, cancelled at the event's first market close. C2 / C3: the subset where the frozen M1 model gives the NO
    an EV >= +20% / +10% per $. No fee (all 21 series are fee_type 'quadratic': makers pay nothing).
  * Fills (amendment b): Kalshi trade prints strictly after post + 120 s and strictly before the event's first close,
    block trades excluded, size-weighted against N. Two primary measures: 'any' (yes_price >= s) and 'through'
    (yes_price > s). Extra: 'strict' (through, plus prints AT s whose taker sold YES: a resting YES bid at s would have
    crossed our ask, so time priority does not matter) and, for probed orders, 'queue' (prints at s bought by YES
    takers count only after the size already resting at s when we posted).
  * Queue probe: GET /orderbook at post, +15 min and +60 min for up to 3 orders per pass and 20 per UTC day (highest C2 EV
    first): size already at our price at post (queue ahead), undercutting later (a NO bid above ours = someone sells YES
    below s), crossing (a YES bid >= s).
Stages of a pass, in priority order: missed-entry bookkeeping (no call); entries due now (one /markets?event_ticker
call per event); post-time probes; follow-up probes; series discovery (rotation, oldest first, each series at most once
per 20 min, <= 6 calls); settlement checks of events with orders; trade-print fill jobs; extra discovery; one settled
sweep per series per day (base-rate pool, as the r2 pre-registration's nightly refresh).
Files (under data/kalshi_lab/strategies/r3_mentions_maker_forward/): forward.jsonl (append-only rows typed
listing / entry / missed / probe / settle / fill / pass), state.json, trades/<ticker>.json (prints of the fill window),
settled.jsonl (base-rate pool additions), calls.log (every Kalshi call), .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r3_mentions_maker_forward_logger [--max-calls N] [--selftest]"""
from __future__ import annotations
import datetime as dt, fcntl, json, math, os, re, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_mentions_maker_forward_api import Budget, BudgetExhausted

OUT = Path(os.environ.get("R3MM_OUT", "data/kalshi_lab/strategies/r3_mentions_maker_forward"))
R2 = Path("data/kalshi_lab/strategies/r2_mentions_baserate")

# ------------------------------------------------------------------------------------------------ frozen constants
SERIES = ["KXTRUMPMENTION", "KXTRUMPMENTIONB", "KXWORLDNEWSMENTION", "KXVANCEMENTION", "KXMAMDANIMENTION",
          "KXLASTWORDMENTION", "KXPSAKIMENTION", "KXBERNIEMENTION", "KXALLINMENTION", "KXPOLITICSMENTION",
          "KXFOXNEWSMENTION", "KXMTPMENTION", "KXPERSONMENTION", "KXFTNMENTION", "KXFEDMENTION", "KXHEARINGMENTION",
          "KXGOVERNORMENTION", "KXRUBIOMENTION", "KXSECPRESSMENTION", "KXMADDOWMENTION", "KXHEGSETHMENTION"]
GROUP = {"KXTRUMPMENTIONB": "KXTRUMPMENTION"}                         # same speaker, same format (r2)
M1 = [-0.09174520607273995, 0.9701165898132638, 0.385610892255977]   # frozen on r2 discovery
THR = {"C2": 0.20, "C3": 0.10}
PRIOR_A = 3.0                                                        # p_br = (k + 3 p0) / (n + 3)
STAKE = 5.0

ENTRY_WINDOW_S = 12 * 60      # amendment (a): quote must be read in [H1, H1 + 12 min]
POST_GAP_S = 120              # amendment (b): prints count only strictly after post + 120 s
MAX_CALLS = 10                # hard cap per pass
DISC_MAX = 6                  # discovery calls per pass (both discovery stages together)
DISC_MIN_GAP_S = 35 * 60      # a series is revisited at most every 35 min (every ~40 min at 10-min passes)
PROBE_PER_PASS = 3
PROBE_PER_DAY = 20
PROBE_PHASES = {"p15": (15 * 60, 10 * 60, 30 * 60), "p60": (60 * 60, 50 * 60, 90 * 60)}   # target, window lo, hi
SETTLE_DELAY_S = 30 * 60      # first settlement check after the event's close was seen
SETTLE_RETRY_S = 2 * 3600     # 2 h for the first 12 checks, then 12 h (postponed appearances may take 14 days)
SETTLE_FALLBACK_S = 3 * 86400  # check events with orders after 3 days even if no close was seen
SETTLE_MAX_CHECKS = 60
SWEEP_PERIOD_S = 86400
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended"}


# ------------------------------------------------------------------------------------------------ helpers
def ts_of(s) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    """First top of the UTC hour >= open + 1 h."""
    return int(math.ceil((open_ts + 3600) / 3600.0) * 3600)


def wkey_of(word: str) -> str:
    """Word key (identical to r2_mentions_baserate.wkey): lower-case, alnum, '/'-alternatives sorted."""
    parts = sorted(p.strip() for p in re.sub(r"[^a-z0-9/+() ]", "", (word or "").lower()).split("/") if p.strip())
    return "/".join(parts)


def word_of(m: dict) -> str:
    return (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""


def logit(p: float) -> float:
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def series_of(ticker: str) -> str:
    return ticker.split("-")[0]


class BaseRates:
    """Word hit counts by (speaker group, word) as of time T: only markets closed before T - 1 h, other events (r2)."""

    def __init__(self, pool: list[dict]) -> None:
        self.by_w = defaultdict(list); self.by_g = defaultdict(list)
        for p in pool:
            self.by_w[(p["g"], p["w"])].append((p["close"], p["y"], p["e"])); self.by_g[p["g"]].append((p["close"], p["y"], p["e"]))

    def p_br(self, g: str, w: str, T: float, e: str) -> tuple[float, int, int, float]:
        x = [y for c, y, ee in self.by_w.get((g, w), []) if c < T - 3600 and ee != e]
        gx = [y for c, y, ee in self.by_g.get(g, []) if c < T - 3600 and ee != e]
        k, n = sum(x), len(x); p0 = (sum(gx) + 1) / (len(gx) + 2)
        return (k + PRIOR_A * p0) / (n + PRIOR_A), k, n, p0


def load_pool(out: Path) -> list[dict]:
    """r2 history + r2 recent (frozen files, read-only) + markets this logger saw settle (settled.jsonl)."""
    rows = {}
    for f in (R2 / "markets_hist.jsonl", R2 / "markets_recent.jsonl", out / "settled.jsonl"):
        if not f.exists():
            continue
        for line in f.open():
            m = json.loads(line)
            if m.get("result") in ("yes", "no") and m.get("close_time"):
                rows[m["ticker"]] = m
    return [{"g": GROUP.get(m["series"], m["series"]), "w": wkey_of(word_of(m)), "close": ts_of(m["close_time"]),
             "y": m["result"] == "yes", "e": m["event_ticker"]} for m in rows.values()]


def model(ask_c: int, bid_c: int, s_c: int, br: tuple) -> dict:
    mid = (ask_c + bid_c) / 200.0
    p = 1 / (1 + math.exp(-(M1[0] + M1[1] * logit(mid) + M1[2] * logit(br[0]))))
    px = 1 - s_c / 100.0
    ev = ((1 - p) - px) / px
    return {"p": round(p, 5), "p_br": round(br[0], 5), "k": br[1], "n": br[2], "p0": round(br[3], 5), "ev_c2": round(ev, 5),
            "C2": ev >= THR["C2"], "C3": ev >= THR["C3"]}


def book_levels(ob: dict) -> tuple[list, list]:
    """(NO bids, YES bids) as [[cents, size], ...] from an /orderbook response (orderbook_fp dollars or legacy cents)."""
    fp = ob.get("orderbook_fp") or {}
    if fp:
        no = [[cents(p), fnum(q)] for p, q in fp.get("no_dollars") or []]
        yes = [[cents(p), fnum(q)] for p, q in fp.get("yes_dollars") or []]
        return no, yes
    o = ob.get("orderbook") or {}
    return [[int(p), fnum(q)] for p, q in o.get("no") or []], [[int(p), fnum(q)] for p, q in o.get("yes") or []]


def fill_metrics(prints: list, s_c: int, N: int, lo: float, hi: float, queue_ahead: float | None) -> dict:
    """prints: [ts, yes_cents, count, taker_side, is_block]. Window: lo < ts < hi (lo = post + 120 s, hi = first close)."""
    win = sorted((p for p in prints if lo < p[0] < hi and not p[4]), key=lambda p: p[0])
    any_c = through_c = strict_c = at_yes = 0.0
    t_any = t_through = None
    for t, y, c, side, _ in win:
        if y > s_c:
            through_c += c; strict_c += c
            t_through = t_through or t
        elif y == s_c and side == "no":
            strict_c += c
        elif y == s_c:
            at_yes += c
        if y >= s_c:
            any_c += c; t_any = t_any or t
    out = {"n_prints": len(win), "any_cnt": round(any_c, 2), "through_cnt": round(through_c, 2), "strict_cnt": round(strict_c, 2),
           "f_any": round(min(N, any_c) / N, 4), "f_through": round(min(N, through_c) / N, 4), "f_strict": round(min(N, strict_c) / N, 4),
           "t_first_any": t_any, "t_first_through": t_through}
    if queue_ahead is not None:
        q = strict_c + max(0.0, at_yes - queue_ahead)
        out["queue_cnt"] = round(q, 2); out["f_queue"] = round(min(N, q) / N, 4)
    return out


# ------------------------------------------------------------------------------------------------ the logger
class Logger:
    def __init__(self, out: Path, budget, now: float | None = None) -> None:
        self.out = out; self.B = budget; self.now = now or time.time(); self._t0 = time.time()
        self.rows: list[dict] = []; self.stage = defaultdict(int); self.errors: list[str] = []
        self._br = None
        sf = out / "state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        s = self.st
        s.setdefault("version", 1); s.setdefault("started", self.now)
        if "docs_gate_c_at_start" not in s:      # audit trail for amendment (c): was the maker gate row in the docs?
            doc = Path("docs/KALSHI_LAB.md")
            s["docs_gate_c_at_start"] = bool(doc.exists() and "through-only" in doc.read_text().lower())
        for k in ("series_checked", "series_swept", "markets", "events", "orders", "probe_day"):
            s.setdefault(k, {})

    # ---- bookkeeping
    def clock(self) -> float:
        """Wall clock in a live pass; simulated start + elapsed in the self-test."""
        return self.now + (time.time() - self._t0)

    def row(self, typ: str, **kw) -> None:
        r = {"type": typ, "ts": round(kw.pop("ts", None) or self.clock(), 1)}
        r.update(kw); self.rows.append(r)

    def call(self, path: str) -> dict:
        d = self.B.get(path)
        if not d:
            self.errors.append("empty:" + path[:80])
        return d

    def br(self) -> BaseRates:
        if self._br is None:
            self._br = BaseRates(load_pool(self.out))
        return self._br

    def ev(self, e: str, series: str) -> dict:
        return self.st["events"].setdefault(e, {"series": series, "first_seen": self.now, "closed_seen": None, "settled": False,
                                                "next_check": None, "checks": 0, "first_close": None})

    def add_market(self, m: dict, series: str, via: str) -> dict | None:
        """Track a market seen for the first time; returns its state entry (existing or new)."""
        t = m["ticker"]; S = self.st["markets"]
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
        S[t] = {"e": m["event_ticker"], "s": series, "open": op, "H1": H1, "status": status, "seen_open": None,
                "word": word_of(m)[:80]}
        self.ev(m["event_ticker"], series)
        self.row("listing", ticker=t, e=m["event_ticker"], series=series, open=op, H1=H1, status=status, via=via,
                 word=word_of(m)[:80], created=ts_of(m.get("created_time")), mstatus=m.get("status"))
        if status == "missed":
            self.row("missed", ticker=t, e=m["event_ticker"], H1=H1, reason="discovered_late")
        return S[t]

    # ---- stage 0: missed entries
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
            if e in done:
                continue
            series = self.st["events"].get(e, {}).get("series") or series_of(e)
            d = self.call(f"/markets?event_ticker={e}&limit=1000")
            ms = d.get("markets") or []
            if not ms:
                continue
            done.add(e); self.stage["entry_calls"] += 1
            tq = self.clock()                    # quote received: the virtual order is posted now
            started = any((m.get("status") in CLOSED) or (m.get("result") in ("yes", "no")) for m in ms)
            for m in ms:
                x = self.add_market(m, series, "entry")
                if x and m.get("status") == "active":
                    x["seen_open"] = tq
            for m in ms:
                x = self.st["markets"].get(m["ticker"])
                if not x or x["status"] != "tracked" or not (x["H1"] <= tq < x["H1"] + ENTRY_WINDOW_S):
                    continue
                posted += self.evaluate(m, x, e, series, tq, started)
        return posted

    def evaluate(self, m: dict, x: dict, e: str, series: str, tq: float, started: bool) -> list[str]:
        t = m["ticker"]; x["status"] = "entered"
        ask, bid = cents(m.get("yes_ask_dollars")), cents(m.get("yes_bid_dollars"))
        base = {"ticker": t, "e": e, "series": series, "H1": x["H1"], "lag_s": round(tq - x["H1"], 1), "open": x["open"],
                "ask_c": ask, "bid_c": bid, "ask_size": fnum(m.get("yes_ask_size_fp")), "bid_size": fnum(m.get("yes_bid_size_fp")),
                "vol": fnum(m.get("volume_fp")), "mstatus": m.get("status"), "word": x.get("word")}
        reason = None
        if started:
            reason = "event_started"
        elif m.get("status") != "active":
            reason = "market_not_active"
        elif ask is None or bid is None or not (0 < bid and ask < 100):
            reason = "no_two_sided_quote"
        elif ask - bid < 2:
            reason = "spread_lt_2c"
        if reason:
            self.row("entry", ts=tq, posted=False, reason=reason, **base)
            return []
        s_c = ask - 1; N = int(500 // (100 - s_c))
        g = GROUP.get(series, series)
        br = self.br().p_br(g, wkey_of(word_of(m)), x["H1"], e)
        mod = model(ask, bid, s_c, br)
        self.row("entry", ts=tq, posted=True, reason=None, s_c=s_c, N=N, no_px=round(1 - s_c / 100, 2), C1=True, **mod, **base)
        self.st["orders"][t] = {"e": e, "post": tq, "s_c": s_c, "N": N, "ev_c2": mod["ev_c2"], "C2": mod["C2"], "C3": mod["C3"],
                                "probe": False, "probes": {}, "job": None, "fill_done": False}
        self.stage["posted"] += 1
        return [t]

    # ---- stage 2/3: queue probes
    def probe(self, t: str, phase: str) -> None:
        o = self.st["orders"][t]
        ob = self.call(f"/markets/{t}/orderbook")
        tq = self.clock()
        if not ob:
            return
        no, yes = book_levels(ob)
        q_c = 100 - o["s_c"]
        best_no = max((p for p, sz in no if sz > 0), default=None)
        best_yes = max((p for p, sz in yes if sz > 0), default=None)
        at_q = sum(sz for p, sz in no if p == q_c)
        better = sum(sz for p, sz in no if p > q_c)
        o["probes"][phase] = {"ts": tq, "at_q": at_q, "best_no": best_no}
        self.row("probe", ts=tq, ticker=t, e=o["e"], phase=phase, since_post_s=round(tq - o["post"], 1), s_c=o["s_c"], q_c=q_c,
                 best_no_c=best_no, best_yes_bid_c=best_yes, size_at_our_price=round(at_q, 2), size_better_than_ours=round(better, 2),
                 undercut=(best_no is not None and best_no > q_c), crossed=(best_yes is not None and best_yes >= o["s_c"]),
                 implied_yes_ask_c=(100 - best_no) if best_no is not None else None)
        self.stage["probes"] += 1

    def post_probes(self, posted: list[str]) -> None:
        day = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).strftime("%Y-%m-%d")
        used = self.st["probe_day"].get(day, 0)
        cand = sorted(posted, key=lambda t: -self.st["orders"][t]["ev_c2"])
        for t in cand[: max(0, min(PROBE_PER_PASS - self.stage["post_probes"], PROBE_PER_DAY - used))]:
            if self.B.left <= 0:
                break
            self.stage["post_probes"] += 1
            self.st["orders"][t]["probe"] = True
            self.st["probe_day"][day] = self.st["probe_day"].get(day, 0) + 1
            self.probe(t, "post")

    def followup_probes(self) -> None:
        for t, o in sorted(self.st["orders"].items(), key=lambda kv: kv[1]["post"]):
            if not o["probe"]:
                continue
            ev = self.st["events"].get(o["e"], {})
            for ph, (_, lo, hi) in PROBE_PHASES.items():
                if ph in o["probes"] or ev.get("closed_seen"):
                    continue
                age = self.now - o["post"]
                if lo <= age <= hi and self.B.left > 0:
                    self.probe(t, ph)

    # ---- stage 4: discovery
    def discover(self) -> None:
        sc = self.st["series_checked"]
        order = sorted(SERIES, key=lambda s: sc.get(s, 0))
        for s in order:
            if self.stage["disc_calls"] >= DISC_MAX or self.B.left <= 0:
                break
            if self.now - sc.get(s, 0) < DISC_MIN_GAP_S:
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
                    x["seen_open"] = tq; present.add(m["ticker"])
            # tracked markets of this series that left the open list have closed: the event is over (mention markets
            # close in one batch after the appearance ends); schedule its settlement check
            if d.get("cursor"):
                continue                                 # truncated list: no closure inference
            for t, x in self.st["markets"].items():
                if x["s"] == s and x.get("seen_open") and t not in present:
                    e = self.ev(x["e"], s)
                    if not e["closed_seen"]:
                        e["closed_seen"] = tq; e["next_check"] = tq + SETTLE_DELAY_S
                        self.row("event_closed_seen", ts=tq, e=x["e"], series=s, ticker=t, last_seen_open=x["seen_open"])

    # ---- stage 5: settlement
    def settle_checks(self) -> None:
        with_orders = {o["e"] for o in self.st["orders"].values()}
        due = []
        for e in with_orders:
            E = self.st["events"].get(e)
            if not E or E["settled"] or E["checks"] >= SETTLE_MAX_CHECKS:
                continue
            nc = E.get("next_check")
            if nc is None:
                last_post = max(o["post"] for o in self.st["orders"].values() if o["e"] == e)
                nc = last_post + SETTLE_FALLBACK_S
            if self.now >= nc:
                due.append((nc, e))
        for _, e in sorted(due):
            if self.B.left <= 0:
                break
            self.settle(e)

    def settle(self, e: str) -> None:
        E = self.st["events"][e]
        d = self.call(f"/markets?event_ticker={e}&limit=1000")
        E["checks"] += 1; E["next_check"] = self.now + (SETTLE_RETRY_S if E["checks"] < 12 else 6 * SETTLE_RETRY_S)
        ms = d.get("markets") or []
        if not ms:
            return
        self.stage["settle_calls"] += 1
        closes = [ts_of(m.get("close_time")) for m in ms if m.get("status") in CLOSED or m.get("result") in ("yes", "no", "void")]
        order_t = [t for t, o in self.st["orders"].items() if o["e"] == e]
        res = {m["ticker"]: m.get("result") or "" for m in ms}
        if not closes or any(res.get(t) not in ("yes", "no", "void") for t in order_t):
            if closes and not E["closed_seen"]:
                E["closed_seen"] = self.now
            return
        fc = min(c for c in closes if c)
        E["settled"] = True; E["first_close"] = fc
        self.row("settle", e=e, series=E["series"], first_close=fc, n_markets=len(ms),
                 markets={m["ticker"]: [m.get("result") or "", ts_of(m.get("close_time")), m.get("status")] for m in ms})
        # base-rate pool additions (as the r2 pre-registration's refresh)
        self.add_settled(ms, E["series"])
        for t in order_t:
            o = self.st["orders"][t]
            o["job"] = {"lo": o["post"] + POST_GAP_S, "hi": fc, "cursor": "", "pages": 0}

    def add_settled(self, ms: list[dict], series: str) -> None:
        f = self.out / "settled.jsonl"
        have = set()
        if f.exists():
            have = {json.loads(l)["ticker"] for l in f.open()}
        new = []
        for m in ms:
            if m.get("result") in ("yes", "no") and m["ticker"] not in have:
                new.append({"series": series_of(m["ticker"]) if series_of(m["ticker"]) in SERIES else series, "ticker": m["ticker"],
                            "event_ticker": m["event_ticker"], "close_time": m.get("close_time"), "result": m["result"],
                            "custom_strike": m.get("custom_strike"), "yes_sub_title": m.get("yes_sub_title")})
        if new:
            with f.open("a") as fh:
                fh.write("".join(json.dumps(x) + "\n" for x in new))

    # ---- stage 6: fills from trade prints
    def fill_jobs(self) -> None:
        jobs = sorted(((o["post"], t) for t, o in self.st["orders"].items() if o.get("job") and not o["fill_done"]))
        for _, t in jobs:
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
        fm = fill_metrics(prints, o["s_c"], o["N"], j["lo"], j["hi"], qa)
        o["fill_done"] = True
        self.row("fill", ticker=t, e=o["e"], post=o["post"], lo=j["lo"], hi=j["hi"], s_c=o["s_c"], N=o["N"], pages=j["pages"], **fm)
        self.stage["fills"] += 1

    # ---- stage 8: daily settled sweep (base-rate pool)
    def sweeps(self) -> None:
        sw = self.st["series_swept"]
        for s in sorted(SERIES, key=lambda s: (sw.get(s) or {}).get("ts", 0)):
            if self.B.left <= 0:
                break
            last = sw.get(s) or {}
            if self.now - last.get("ts", 0) < SWEEP_PERIOD_S:
                continue
            mc = int(last.get("ts", self.st["started"] - 2 * 86400) - 86400)
            d = self.call(f"/markets?series_ticker={s}&status=settled&min_close_ts={mc}&limit=1000")
            if "markets" not in d:
                continue
            self.add_settled(d.get("markets") or [], s)
            sw[s] = {"ts": self.clock(), "n": len(d.get("markets") or [])}
            self.stage["sweeps"] += 1

    # ---- one pass
    def run(self) -> dict:
        done: set = set()
        try:
            self.mark_missed()
            posted = self.entries(done)
            self.post_probes(posted)
            self.followup_probes()
            self.discover()
            posted2 = self.entries(done)               # markets discovered inside their entry window
            self.post_probes(posted2)
            self.settle_checks()
            self.fill_jobs()
            self.discover()
            self.sweeps()
        except BudgetExhausted:
            pass
        except Exception as ex:                          # keep the state consistent; the row records the error
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
    """Serves canned responses by path prefix; counts calls like Budget."""

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
    """Simulates one event end to end without network: listing -> entry at H1 -> probes -> close -> settle -> fills."""
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="r3mm_selftest_"))
    H = 1_800_000_000 - (1_800_000_000 % 3600)            # a top of the hour
    op = H - 3600 - 15 * 60                                # opened 1 h 15 min before H -> H1 = H
    E = "KXTRUMPMENTION-99JAN01"

    def mk(tk, word, ask, bid, status="active", result="", close=None):
        return {"ticker": tk, "event_ticker": E, "status": status, "result": result, "open_time": iso(op),
                "close_time": iso(close or H + 14 * 86400), "yes_ask_dollars": f"{ask:.4f}", "yes_bid_dollars": f"{bid:.4f}",
                "yes_ask_size_fp": "10.00", "yes_bid_size_fp": "5.00", "volume_fp": "100", "custom_strike": {"Word": word},
                "yes_sub_title": word, "created_time": iso(op - 600)}
    state = {"phase": "open"}
    fc = H + 6 * 3600

    def responder(path):
        if path.startswith("/markets?series_ticker=KXTRUMPMENTION&status=open"):
            if state["phase"] == "open":
                return {"markets": [mk(E + "-AAA", "Tariff", 0.60, 0.50), mk(E + "-BBB", "Crypto / Bitcoin", 0.30, 0.29),
                                    mk(E + "-CCC", "Moon", 0.20, 0.10)]}
            return {"markets": []}
        if path.startswith("/markets?series_ticker="):
            return {"markets": []}
        if path.startswith(f"/markets?event_ticker={E}"):
            if state["phase"] == "open":
                return {"markets": [mk(E + "-AAA", "Tariff", 0.60, 0.50), mk(E + "-BBB", "Crypto / Bitcoin", 0.30, 0.29),
                                    mk(E + "-CCC", "Moon", 0.20, 0.10)]}
            return {"markets": [mk(E + "-AAA", "Tariff", 0.99, 0.98, "finalized", "yes", fc),
                                mk(E + "-BBB", "Crypto / Bitcoin", 0.01, 0.0, "finalized", "no", fc + 5),
                                mk(E + "-CCC", "Moon", 0.01, 0.0, "finalized", "no", fc + 3)]}
        if "/orderbook" in path:
            return {"orderbook_fp": {"no_dollars": [["0.4000", "50.00"], ["0.4100", "7.00"]], "yes_dollars": [["0.5000", "9.00"]]}}
        if path.startswith("/markets/trades?ticker="):
            tk = path.split("ticker=")[1].split("&")[0]
            post = state["post"]
            if tk.endswith("AAA"):     # s = 59c: prints at 59 (yes taker), 60 (through), one before post+120 (ignored)
                return {"trades": [{"created_time": iso(post + 60), "yes_price_dollars": "0.9000", "count_fp": "100", "taker_side": "yes"},
                                   {"created_time": iso(post + 600), "yes_price_dollars": "0.5900", "count_fp": "4", "taker_side": "yes"},
                                   {"created_time": iso(post + 900), "yes_price_dollars": "0.5900", "count_fp": "3", "taker_side": "no"},
                                   {"created_time": iso(post + 1200), "yes_price_dollars": "0.6000", "count_fp": "2", "taker_side": "yes"},
                                   {"created_time": iso(post + 1300), "yes_price_dollars": "0.9500", "count_fp": "50", "taker_side": "yes",
                                    "is_block_trade": True}], "cursor": ""}
            if tk.endswith("CCC"):     # s = 19c: one through print of 20 -> fully filled
                return {"trades": [{"created_time": iso(post + 3000), "yes_price_dollars": "0.2500", "count_fp": "20", "taker_side": "yes"}], "cursor": ""}
            return {"trades": [], "cursor": ""}
        return {}

    # pass 1: discovery 50 min before H1 (no entry yet)
    lg = Logger(tmp, FakeBudget(10, responder), now=H - 50 * 60); lg.st["started"] = H - 3 * 3600; lg.run()
    assert all(x["status"] == "tracked" for x in lg.st["markets"].values()), lg.st["markets"]
    # pass 2: at H1 + 1 min -> entry, probes
    lg = Logger(tmp, FakeBudget(10, responder), now=H + 60); lg.run()
    o = lg.st["orders"]
    assert set(o) == {E + "-AAA", E + "-CCC"}, o      # BBB spread 1c -> skipped
    assert o[E + "-AAA"]["s_c"] == 59 and o[E + "-AAA"]["N"] == 12, o[E + "-AAA"]
    state["post"] = o[E + "-AAA"]["post"]
    assert o[E + "-AAA"]["post"] == o[E + "-CCC"]["post"]
    # pass 3: +16 min probes
    lg = Logger(tmp, FakeBudget(10, responder), now=H + 17 * 60); lg.run()
    assert all("p15" in x["probes"] for x in lg.st["orders"].values() if x["probe"])
    # pass 4: after the close -> closure seen; pass 5: settlement + fills
    state["phase"] = "closed"
    lg = Logger(tmp, FakeBudget(10, responder), now=fc + 600); lg.st["series_checked"] = {}; lg.run()
    assert lg.st["events"][E]["closed_seen"]
    lg = Logger(tmp, FakeBudget(10, responder), now=fc + 600 + SETTLE_DELAY_S + 1); lg.run()
    rows = [json.loads(l) for l in (tmp / "forward.jsonl").open()]
    fills = {r["ticker"]: r for r in rows if r["type"] == "fill"}
    a, c = fills[E + "-AAA"], fills[E + "-CCC"]
    # AAA: window prints (block excluded): 59 yes x4, 59 no x3, 60 yes x2 -> any 9, through 2, strict 5; queue ahead 0 -> queue 9
    assert (a["any_cnt"], a["through_cnt"], a["strict_cnt"]) == (9.0, 2.0, 5.0), a
    # queue: AAA's post probe saw 7 contracts already resting at our price -> the 4 YES-taker prints at s do not reach us
    assert a.get("queue_cnt") == 5.0 and c.get("queue_cnt") == 20.0, (a, c)
    assert c["f_through"] == 1.0 and c["f_any"] == 1.0, c
    settle = [r for r in rows if r["type"] == "settle"][0]
    assert settle["first_close"] == fc
    # frozen word key / model identical to the r2 implementation
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from lab.kalshi.strategies import r2_mentions_baserate as MB
    assert MB.M1_FROZEN == M1 and MB.GROUP == GROUP and MB.FROZEN["C2_maker_no_br20"]["thr"] == THR["C2"] and MB.FROZEN["C3_maker_no_br10"]["thr"] == THR["C3"]
    for w in ("Tariff (3+ times)", "Zohran / Mamdani", "AI / Artificial Intelligence", "Crypto / Bitcoin"):
        assert wkey_of(w) == MB.wkey({"custom_strike": {"Word": w}}), w
    P = json.loads((R2 / "preregistration.json").read_text())
    assert P["series"] == SERIES
    # base rates identical to r2's BaseRates on the r2 pool
    mk_, ev_, C_, pool = MB.load()
    BRr2 = MB.BaseRates(pool); BRme = BaseRates(load_pool(Path("/nonexistent")))
    for m in mk_[:300:7]:
        T = MB.ts(m["open_time"]) + 7200
        k, n = BRr2.word(m["g"], m["w"], T, m["event_ticker"]); kg, ng = BRr2.group(m["g"], T, m["event_ticker"])
        p0 = (kg + 1) / (ng + 2); pr2 = (k + 3 * p0) / (n + 3)
        mine = BRme.p_br(m["g"], m["w"], T, m["event_ticker"])
        assert abs(pr2 - mine[0]) < 1e-12 and (k, n) == mine[1:3], (m["ticker"], pr2, mine)
    print("selftest OK:", tmp, {t: {k: v for k, v in r.items() if k in ("any_cnt", "through_cnt", "strict_cnt", "queue_cnt", "f_any", "f_through")}
                                 for t, r in fills.items()})
    shutil.rmtree(tmp)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mc = int(os.environ.get("R3MM_MAX_CALLS", MAX_CALLS))
        if "--max-calls" in sys.argv:
            mc = int(sys.argv[sys.argv.index("--max-calls") + 1])
        run_pass(mc)
