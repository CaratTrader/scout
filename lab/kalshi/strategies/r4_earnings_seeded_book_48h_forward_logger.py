"""r4_earnings_seeded_book_48h_forward_logger: forward PAPER log of the earnings-call seeded-book maker, arms A/B/C.

One pass per invocation (launchd or cron; recommended every 10 min at minutes 3,13,23,33,43,53). At most 10 Kalshi calls
per pass (--max-calls / R4E_MAX_CALLS may only lower it), every call through lab.us.data_refresh.fetch (temperature-bot
idle window, 429 retries). It never places orders. Frozen rules: data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_
forward/preregistration.json (written before the first forward pass).

Order (C1E, round 3): every KXEARNINGSMENTION* market is evaluated ONCE, at the first pass at or after open_time + 1 h
and no later than open_time + 4 h (a market whose window passes unseen is logged 'missed', never entered later), from
the /markets?event_ticker quote. No order if any market of the event has closed, if not 0 < yes_bid < yes_ask < 1, if
yes_ask - yes_bid < 2c, or if the post is not before the arm-A cancel. Otherwise a virtual SELL-YES at s = yes_ask - 1c
(= BUY-NO at 1 - s) for N = floor(5 / (1 - s)) contracts.
  arm A  cancel at the C1E pre-call time computed from the schedule known at each moment (cancel_time() below)
  arm B  cancel at min(post + 48 h, arm A cancel)
  arm C  arm B in a book of <= 10 concurrent orders/positions, earliest-listed first (computed by the summary)
Fills: Kalshi trade prints (GET /markets/trades) strictly after post + 120 s and up to the cancel, block trades
excluded, size-weighted against N. 'through' (yes_price > s; gate amendment c) is primary; 'any' (>= s), 'strict' and
'queue' (needs the post-time order-book probe) are reported. The prints of (post + 120 s, arm-A cancel] are fetched
once after the event settles; arm B uses the prefix up to its own cancel.

Stages of a pass, in priority order:
  0 missed-entry bookkeeping (no call)
  1 discovery of new listings: GET /markets?min_created_ts=lo&max_created_ts=hi&mve_filter=exclude&limit=1000 in
    windows from the watermark (<= 3 calls per pass), keep KXEARNINGSMENTION* tickers. If the filter is ever ignored
    by the API (rows created outside [lo, hi]), the pass falls back to 6 series open-list calls (rotation).
  2 entries due now: one GET /markets?event_ticker=E per event with markets in their entry window; logs the schedule
  3 order-book probes at post: GET /markets/{t}/orderbook for <= 3 orders per pass, <= 20 per UTC day, earliest-listed
    first (queue ahead at our price, undercut, crossed)
  4 schedule refresh for events with resting arm-A orders (every 12 h; every 3 h inside 30 h of the cancel)
  5 settlement checks (from arm-A cancel + 6 h, then every 6 h; after 20 checks every 24 h; at most 50)
  6 trade-print fill jobs of settled orders (paged)
  7 one series open-list call per pass in rotation (backup discovery and schedule refresh)
Files: forward.jsonl (append-only rows: listing / entry / missed / probe / sched / settle / fill / pass), state.json,
trades/<ticker>.json, calls.log (every Kalshi call), .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_logger [--max-calls N] [--selftest]"""
from __future__ import annotations
import datetime as dt, fcntl, json, math, os, re, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_api import Budget, BudgetExhausted

OUT = Path(os.environ.get("R4E_OUT", "data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward"))
ET = ZoneInfo("America/New_York")

# ------------------------------------------------------------------------------------------------ frozen constants
PREFIX = "KXEARNINGSMENTION"
ENTRY_DELAY_S = 3600          # evaluate at the first pass >= open + 1 h ...
ENTRY_WINDOW_S = 3 * 3600     # ... and no later than open + 4 h (C1E: polls <= 3 h apart)
POST_GAP_S = 120
H_B_S = 48 * 3600
STAKE = 5.0
MAX_CALLS = 10
DISC_OVERLAP_S = 5 * 60
DISC_MAX_PAGES = 3            # discovery calls per pass
FALLBACK_ROTATE = 6
PROBE_PER_PASS = 3
PROBE_PER_DAY = 20
SCHED_EVERY_S = 12 * 3600
SCHED_NEAR_S = 3 * 3600
SCHED_NEAR_WINDOW_S = 30 * 3600
SCHED_PER_PASS = 2
SETTLE_FIRST_S = 6 * 3600
SETTLE_RETRY_S = 6 * 3600
SETTLE_SLOW_S = 24 * 3600
SETTLE_MAX_CHECKS = 50
FILL_PAGES_PER_PASS = 6
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended"}
MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)


FROZEN_FILE = Path("data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward/frozen_series.json")


def frozen_series() -> list[str]:
    """The 115 KXEARNINGSMENTION* series with lifetime volume >= 100,000 on 2026-10-08 (PRIMARY universe)."""
    for f in (OUT / "frozen_series.json", FROZEN_FILE):
        if f.exists():
            return json.loads(f.read_text())["series"]
    return []


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


def series_of(ticker: str) -> str:
    return ticker.split("-")[0]


def ticker_date(e: str):
    g = re.search(r"-(\d\d)([A-Z]{3})(\d\d)$", e)
    try:
        return dt.date(2000 + int(g[1]), MON[g[2]], int(g[3])) if g else None
    except (KeyError, ValueError):
        return None


def word_of(m: dict) -> str:
    return (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""


def cancel_time(e: str, occ_iso: str | None) -> float:
    """C1E pre-call cancel from the schedule as known now. occurrence_datetime is the scheduled call start when Kalshi
    knows it; 15:00:00 UTC values are placeholders (dates up to ~2 weeks after the expected call). Call day D = the
    earlier of the event ticker's date and the occurrence date (ET). If the occurrence is precise and on D: cancel =
    min(start - 60 min, 15:00 ET on D) for a start at or after 12:00 ET, else min(start - 60 min, 07:00 ET on D).
    Otherwise (no precise start on D): 07:00 ET on D (C1E's morning-call cancel, the conservative choice)."""
    td = ticker_date(e)
    occ = ts_of(occ_iso)
    occ_et = dt.datetime.fromtimestamp(occ, ET) if occ else None
    cands = [d for d in (td, occ_et.date() if occ_et else None) if d is not None]
    if not cands:
        return float("inf")
    D = min(cands)
    at = lambda h: dt.datetime(D.year, D.month, D.day, h, 0, tzinfo=ET).timestamp()
    precise = occ is not None and not dt.datetime.fromtimestamp(occ, dt.timezone.utc).strftime("%H:%M:%S") == "15:00:00"
    if precise and occ_et.date() == D:
        return min(occ - 3600, at(15) if occ_et.hour >= 12 else at(7))
    return at(7)


def effective_cancel(e: str, obs: list, first_close: float | None = None) -> float:
    """Causal arm-A cancel: obs = [[obs_ts, occurrence_iso], ...] in time order. Between observations the latest one
    rules; the order is cancelled at the first moment T >= cancel_time(schedule known at T). An observed close of any
    market of the event (first_close) also cancels."""
    obs = sorted(obs, key=lambda x: x[0])
    out = float("inf")
    for i, (t0, occ) in enumerate(obs):
        c = cancel_time(e, occ)
        t1 = obs[i + 1][0] if i + 1 < len(obs) else float("inf")
        if c <= t0:
            out = t0; break
        if c < t1:
            out = c; break
    if first_close is not None:
        out = min(out, first_close)
    return out


def book_levels(ob: dict) -> tuple[list, list]:
    """(NO bids, YES bids) as [[cents, size], ...] from an /orderbook response."""
    fp = ob.get("orderbook_fp") or {}
    if fp:
        return ([[cents(p), fnum(q)] for p, q in fp.get("no_dollars") or []], [[cents(p), fnum(q)] for p, q in fp.get("yes_dollars") or []])
    o = ob.get("orderbook") or {}
    return [[int(p), fnum(q)] for p, q in o.get("no") or []], [[int(p), fnum(q)] for p, q in o.get("yes") or []]


def fill_metrics(prints: list, s_c: int, N: int, lo: float, hi: float, queue_ahead: float | None) -> dict:
    """prints: [ts, yes_cents, count, taker_side, is_block]; window lo < ts <= hi; block trades excluded."""
    win = sorted((p for p in prints if lo < p[0] <= hi and not p[4]), key=lambda p: p[0])
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
        self.frozen = set(frozen_series())
        sf = out / "state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        s = self.st
        s.setdefault("version", 1); s.setdefault("started", self.now)
        s.setdefault("disc_ts", self.now - 2 * 3600)          # discovery watermark; the first pass looks back 2 h
        s.setdefault("disc_filter_ok", True)
        for k in ("markets", "events", "orders", "probe_day", "series_checked"):
            s.setdefault(k, {})

    def clock(self) -> float:
        return self.now + (time.time() - self._t0)

    def row(self, typ: str, **kw) -> None:
        r = {"type": typ, "ts": round(kw.pop("ts", None) or self.clock(), 1)}
        r.update(kw); self.rows.append(r)

    def call(self, path: str) -> dict:
        d = self.B.get(path)
        if not d:
            self.errors.append("empty:" + path[:90])
        return d

    def ev(self, e: str) -> dict:
        return self.st["events"].setdefault(e, {"series": series_of(e), "first_seen": self.now, "sched": [], "settled": False,
                                                "next_check": None, "checks": 0, "first_close": None, "last_sched": 0, "closed_seen": None})

    def note_schedule(self, e: str, m: dict, tq: float) -> None:
        E = self.ev(e)
        occ = m.get("occurrence_datetime")
        if not E["sched"] or E["sched"][-1][1] != occ:
            E["sched"].append([round(tq, 1), occ])
            self.row("sched", ts=tq, e=e, occurrence=occ, cancel_A=cancel_time(e, occ),
                     expected_expiration=m.get("expected_expiration_time"))
        E["last_sched"] = tq
        if (m.get("status") in CLOSED or m.get("result") in ("yes", "no", "void")) and not E["closed_seen"]:
            E["closed_seen"] = tq
            c = ts_of(m.get("close_time"))
            E["first_close_obs"] = min(c, tq) if c else tq

    def cancel_now(self, e: str) -> float:
        E = self.ev(e)
        return effective_cancel(e, E["sched"], E.get("first_close_obs"))

    def add_market(self, m: dict, via: str, tq: float) -> dict | None:
        t = m.get("ticker") or ""
        if not t.startswith(PREFIX):
            return None
        S = self.st["markets"]
        if t in S:
            return S[t]
        op = ts_of(m.get("open_time"))
        if op is None:
            return None
        T1 = op + ENTRY_DELAY_S
        if T1 + ENTRY_WINDOW_S <= self.now:
            status = "prestart" if T1 < self.st["started"] else "missed"
        else:
            status = "tracked"
        S[t] = {"e": m["event_ticker"], "s": series_of(t), "open": op, "T1": T1, "status": status,
                "created": ts_of(m.get("created_time")), "word": word_of(m)[:80], "frozen": series_of(t) in self.frozen}
        self.ev(m["event_ticker"])
        self.note_schedule(m["event_ticker"], m, tq)
        self.row("listing", ticker=t, e=m["event_ticker"], series=series_of(t), open=op, T1=T1, status=status, via=via,
                 word=word_of(m)[:80], created=S[t]["created"], mstatus=m.get("status"), frozen=S[t]["frozen"])
        if status == "missed":
            self.row("missed", ticker=t, e=m["event_ticker"], T1=T1, reason="discovered_late")
        return S[t]

    # ---- stage 0
    def mark_missed(self) -> None:
        for t, x in self.st["markets"].items():
            if x["status"] == "tracked" and self.now >= x["T1"] + ENTRY_WINDOW_S:
                x["status"] = "missed"
                self.row("missed", ticker=t, e=x["e"], T1=x["T1"], reason="no_quote_in_window")

    # ---- stage 1
    def discover(self) -> None:
        """Created-time feed in windows [watermark - 5 min, watermark + W] (newest first, <= 1000 rows per page). A
        window read to its last page advances the watermark; a window still paging when the pass's discovery calls
        run out is re-read next pass with W halved; W doubles (to 6 h) after small windows. Lookback <= 24 h."""
        if not self.st["disc_filter_ok"]:
            self.rotate(FALLBACK_ROTATE)
        st_ = self.st
        st_.setdefault("disc_w", 1800)
        calls = 0; n_earn = 0
        while calls < DISC_MAX_PAGES and self.B.left > 0:
            lo = int(max(st_["disc_ts"] - DISC_OVERLAP_S, self.now - 86400))
            hi = int(min(self.clock(), lo + st_["disc_w"]))
            cursor = ""; n_rows = 0; complete = False
            while calls < DISC_MAX_PAGES and self.B.left > 0:
                d = self.call(f"/markets?min_created_ts={lo}&max_created_ts={hi}&mve_filter=exclude&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
                calls += 1; self.stage["disc_calls"] += 1
                if "markets" not in d:
                    break
                ms = d.get("markets") or []
                bad = [m for m in ms if not (lo - 60 <= (ts_of(m.get("created_time")) or lo) <= hi + 60)]
                if bad:                                # the API ignored the filter: fall back to rotation
                    st_["disc_filter_ok"] = False
                    self.row("disc_filter_ignored", n=len(ms), n_bad=len(bad)); return
                tq = self.clock(); n_rows += len(ms)
                for m in ms:
                    if (m.get("event_ticker") or "").startswith(PREFIX):
                        n_earn += 1
                        self.add_market(m, "created_feed", tq)
                cursor = d.get("cursor") or ""
                if not cursor or len(ms) < 1000:
                    complete = True; break
            if not complete:
                st_["disc_w"] = max(120, st_["disc_w"] // 2)
                self.row("disc_window_incomplete", lo=lo, hi=hi, rows=n_rows, next_w=st_["disc_w"]); break
            st_["disc_ts"] = hi + DISC_OVERLAP_S       # everything created up to hi has been read
            st_["disc_filter_ok"] = True
            if n_rows < 300:
                st_["disc_w"] = min(6 * 3600, st_["disc_w"] * 2)
            if hi >= self.clock() - 60:
                break
        self.stage["disc_earnings_rows"] += n_earn

    def rotate(self, k: int) -> None:
        sc = self.st["series_checked"]
        allS = sorted(self.frozen | {x["s"] for x in self.st["markets"].values()})
        for s in sorted(allS, key=lambda s: sc.get(s, 0))[:k]:
            if self.B.left <= 0:
                break
            d = self.call(f"/markets?series_ticker={s}&status=open&limit=1000")
            self.stage["rotate_calls"] += 1
            if "markets" not in d:
                continue
            tq = self.clock(); sc[s] = tq
            for m in d.get("markets") or []:
                x = self.add_market(m, "rotation", tq)
                if x:
                    self.note_schedule(m["event_ticker"], m, tq)

    # ---- stage 2
    def due_events(self) -> list[str]:
        due = {}
        for t, x in self.st["markets"].items():
            if x["status"] == "tracked" and x["T1"] <= self.now < x["T1"] + ENTRY_WINDOW_S:
                due[x["e"]] = min(due.get(x["e"], 9e18), x["open"])
        return sorted(due, key=lambda e: due[e])

    def entries(self, done: set) -> list[str]:
        posted = []
        for e in self.due_events():
            if e in done or self.B.left <= 0:
                continue
            d = self.call(f"/markets?event_ticker={e}&limit=1000")
            ms = d.get("markets") or []
            if not ms:
                continue
            done.add(e); self.stage["entry_calls"] += 1
            tq = self.clock()
            started = any((m.get("status") in CLOSED) or (m.get("result") in ("yes", "no", "void")) for m in ms)
            for m in ms:
                self.add_market(m, "entry", tq)
            self.note_schedule(e, ms[0], tq)
            CA = self.cancel_now(e)
            for m in ms:
                x = self.st["markets"].get(m["ticker"])
                if not x or x["status"] != "tracked" or not (x["T1"] <= tq < x["T1"] + ENTRY_WINDOW_S):
                    continue
                posted += self.evaluate(m, x, e, tq, started, CA)
        return posted

    def evaluate(self, m: dict, x: dict, e: str, tq: float, started: bool, CA: float) -> list[str]:
        t = m["ticker"]; x["status"] = "entered"
        ask, bid = cents(m.get("yes_ask_dollars")), cents(m.get("yes_bid_dollars"))
        base = {"ticker": t, "e": e, "series": x["s"], "frozen": x["frozen"], "open": x["open"], "lag_s": round(tq - x["open"], 1),
                "ask_c": ask, "bid_c": bid, "ask_size": fnum(m.get("yes_ask_size_fp")), "bid_size": fnum(m.get("yes_bid_size_fp")),
                "vol": fnum(m.get("volume_fp")), "mstatus": m.get("status"), "word": x.get("word"),
                "occurrence": m.get("occurrence_datetime"), "cancel_A_at_post": CA}
        reason = None
        if started:
            reason = "event_started"
        elif m.get("status") != "active":
            reason = "market_not_active"
        elif ask is None or bid is None or not (0 < bid < ask < 100):
            reason = "no_two_sided_quote"
        elif ask - bid < 2:
            reason = "spread_lt_2c"
        elif tq >= CA:
            reason = "post_after_cancel"
        if reason:
            self.row("entry", ts=tq, posted=False, reason=reason, **base)
            return []
        s_c = ask - 1; N = int(500 // (100 - s_c))
        self.row("entry", ts=tq, posted=True, reason=None, s_c=s_c, N=N, no_px=round(1 - s_c / 100, 2),
                 cancel_B_at_post=min(tq + H_B_S, CA), **base)
        self.st["orders"][t] = {"e": e, "post": tq, "open": x["open"], "s_c": s_c, "N": N, "frozen": x["frozen"],
                                "probe": False, "probes": {}, "job": None, "fill_done": False}
        self.stage["posted"] += 1
        return [t]

    # ---- stage 3
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
        o["probes"][phase] = {"ts": tq, "at_q": at_q, "better": better}
        self.row("probe", ts=tq, ticker=t, e=o["e"], phase=phase, since_post_s=round(tq - o["post"], 1), s_c=o["s_c"], q_c=q_c,
                 best_no_c=best_no, best_yes_bid_c=best_yes, size_at_our_price=round(at_q, 2), size_better_than_ours=round(better, 2),
                 undercut=(best_no is not None and best_no > q_c), crossed=(best_yes is not None and best_yes >= o["s_c"]),
                 no_levels=no[-5:], yes_levels=yes[-5:])
        self.stage["probes"] += 1

    def post_probes(self, posted: list[str]) -> None:
        day = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).strftime("%Y-%m-%d")
        used = self.st["probe_day"].get(day, 0)
        cand = sorted(posted, key=lambda t: (not self.st["orders"][t]["frozen"], self.st["orders"][t]["open"], t))
        for t in cand[: max(0, min(PROBE_PER_PASS - self.stage["probes"], PROBE_PER_DAY - used))]:
            if self.B.left <= 0:
                break
            self.st["orders"][t]["probe"] = True
            self.st["probe_day"][day] = self.st["probe_day"].get(day, 0) + 1
            self.probe(t, "post")

    # ---- stage 4
    def sched_refresh(self) -> None:
        live = defaultdict(list)
        for t, o in self.st["orders"].items():
            live[o["e"]].append(o)
        due = []
        for e in live:
            E = self.ev(e)
            if E["settled"] or E.get("closed_seen"):
                continue
            CA = self.cancel_now(e)
            if self.now >= CA:
                continue
            gap = SCHED_NEAR_S if CA - self.now <= SCHED_NEAR_WINDOW_S else SCHED_EVERY_S
            if self.now - E.get("last_sched", 0) >= gap:
                due.append((CA, e))
        for _, e in sorted(due)[:SCHED_PER_PASS]:
            if self.B.left <= 0:
                break
            d = self.call(f"/markets?event_ticker={e}&limit=1000")
            ms = d.get("markets") or []
            if not ms:
                continue
            tq = self.clock(); self.stage["sched_calls"] += 1
            for m in ms:
                self.add_market(m, "sched", tq)
                self.note_schedule(e, m, tq)

    # ---- stage 5
    def settle_checks(self) -> None:
        due = []
        for e in {o["e"] for o in self.st["orders"].values()}:
            E = self.ev(e)
            if E["settled"] or E["checks"] >= SETTLE_MAX_CHECKS:
                continue
            nc = E.get("next_check")
            if nc is None:
                CA = self.cancel_now(e)
                nc = CA + SETTLE_FIRST_S if CA != float("inf") else max(o["post"] for o in self.st["orders"].values() if o["e"] == e) + 14 * 86400
            if self.now >= nc:
                due.append((nc, e))
        for _, e in sorted(due):
            if self.B.left <= 0:
                break
            self.settle(e)

    def settle(self, e: str) -> None:
        E = self.st["events"][e]
        d = self.call(f"/markets?event_ticker={e}&limit=1000")
        E["checks"] += 1
        E["next_check"] = self.now + (SETTLE_RETRY_S if E["checks"] < 20 else SETTLE_SLOW_S)
        ms = d.get("markets") or []
        if not ms:
            return
        self.stage["settle_calls"] += 1
        tq = self.clock()
        for m in ms:
            if m.get("status") in CLOSED or m.get("result") in ("yes", "no", "void"):
                if not E["closed_seen"]:
                    E["closed_seen"] = tq
        order_t = [t for t, o in self.st["orders"].items() if o["e"] == e]
        res = {m["ticker"]: m.get("result") or "" for m in ms}
        closes = [ts_of(m.get("close_time")) for m in ms if m.get("status") in CLOSED or m.get("result") in ("yes", "no", "void")]
        if not closes or any(res.get(t) not in ("yes", "no", "void") for t in order_t):
            return
        fc = min(c for c in closes if c)
        E["settled"] = True; E["first_close"] = fc
        CA = effective_cancel(e, E["sched"], fc)
        self.row("settle", ts=tq, e=e, series=E["series"], first_close=fc, cancel_A=CA, n_markets=len(ms),
                 markets={m["ticker"]: [m.get("result") or "", ts_of(m.get("close_time")), m.get("status")] for m in ms})
        for t in order_t:
            o = self.st["orders"][t]
            o["job"] = {"lo": o["post"] + POST_GAP_S, "hi": CA, "cursor": "", "pages": 0}

    # ---- stage 6
    def fill_jobs(self) -> None:
        pages = 0
        for _, t in sorted(((o["post"], t) for t, o in self.st["orders"].items() if o.get("job") and not o["fill_done"])):
            while self.B.left > 0 and pages < FILL_PAGES_PER_PASS and not self.st["orders"][t]["fill_done"]:
                if not self.fill_page(t):
                    break
                pages += 1

    def fill_page(self, t: str) -> bool:
        o = self.st["orders"][t]; j = o["job"]
        lo, hi = int(j["lo"]), int(math.ceil(j["hi"]))
        if hi <= lo:
            self.finish_fill(t, []); return True
        path = f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={j['cursor']}" if j["cursor"] else "")
        d = self.call(path)
        if "trades" not in d:
            return False
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
        return True

    def finish_fill(self, t: str, prints: list) -> None:
        o = self.st["orders"][t]; j = o["job"]
        qa = (o["probes"].get("post") or {}).get("at_q")
        cb = min(o["post"] + H_B_S, j["hi"])
        fa = fill_metrics(prints, o["s_c"], o["N"], j["lo"], j["hi"], qa)
        fb = fill_metrics(prints, o["s_c"], o["N"], j["lo"], cb, qa)
        o["fill_done"] = True
        self.row("fill", ticker=t, e=o["e"], post=o["post"], lo=j["lo"], cancel_A=j["hi"], cancel_B=cb, s_c=o["s_c"], N=o["N"],
                 pages=j["pages"], A=fa, B=fb)
        self.stage["fills"] += 1

    # ---- one pass
    def run(self) -> dict:
        done: set = set()
        try:
            self.mark_missed()
            self.discover()
            posted = self.entries(done)
            self.post_probes(posted)
            self.sched_refresh()
            self.settle_checks()
            self.fill_jobs()
            if self.B.left > 0:
                self.rotate(1)
                posted2 = self.entries(done)             # markets the rotation found inside their window
                self.post_probes(posted2)
        except BudgetExhausted:
            pass
        except Exception as ex:                          # keep the state consistent; the pass row records the error
            self.errors.append(f"{type(ex).__name__}: {str(ex)[:200]}")
        n_tr = sum(1 for x in self.st["markets"].values() if x["status"] == "tracked")
        self.row("pass", calls=self.B.used, stages=dict(self.stage), errors=self.errors[:10], tracked=n_tr,
                 markets=len(self.st["markets"]), orders=len(self.st["orders"]), disc_filter_ok=self.st["disc_filter_ok"],
                 dur_s=round(time.time() - self._t0, 1))
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


def selftest(keep: Path | None = None) -> None:
    """One event end to end without network: created feed -> entry at open + 1 h -> probe -> schedule refresh ->
    settle -> trade-print fills for arms A and B; plus unit checks of cancel_time / effective_cancel."""
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="r4e_selftest_"))
    (tmp / "frozen_series.json").write_text(json.dumps({"series": ["KXEARNINGSMENTIONZZZ"]}))
    global OUT
    out0 = OUT
    # cancel rules: precise afternoon start, precise morning start, placeholder, ticker date earlier than occurrence
    e = "KXEARNINGSMENTIONZZZ-99NOV18"
    c = cancel_time(e, "2099-11-18T22:00:00Z")                  # 17:00 ET -> min(16:00 ET, 15:00 ET) = 15:00 ET
    assert dt.datetime.fromtimestamp(c, ET).strftime("%m-%d %H:%M") == "11-18 15:00", c
    c = cancel_time(e, "2099-11-18T13:00:00Z")                  # 08:00 ET -> 07:00 ET
    assert dt.datetime.fromtimestamp(c, ET).strftime("%m-%d %H:%M") == "11-18 07:00", c
    c = cancel_time(e, "2099-12-02T15:00:00Z")                  # placeholder later than the ticker date -> ticker date 07:00 ET
    assert dt.datetime.fromtimestamp(c, ET).strftime("%m-%d %H:%M") == "11-18 07:00", c
    c = cancel_time(e, "2099-11-16T21:30:00Z")                  # call moved earlier than the ticker date: 16:30 ET Nov 16
    assert dt.datetime.fromtimestamp(c, ET).strftime("%m-%d %H:%M") == "11-16 15:00", c
    t_obs = cancel_time(e, "2099-11-18T22:00:00Z")
    assert effective_cancel(e, [[t_obs - 86400, "2099-11-18T22:00:00Z"]]) == t_obs
    assert effective_cancel(e, [[t_obs - 86400, "2099-11-18T22:00:00Z"], [t_obs + 600, "2099-11-19T22:00:00Z"]]) == t_obs   # later news cannot undo a cancel
    cB = cancel_time(e, "2099-11-16T21:30:00Z")
    assert effective_cancel(e, [[t_obs - 5 * 86400, "2099-11-18T22:00:00Z"], [cB + 7200, "2099-11-16T21:30:00Z"]]) == cB + 7200  # learnt late -> cancel on learning

    H = 4_000_000_000 - (4_000_000_000 % 3600)                 # a top of the hour in 2096
    op = H - 3600                                              # opened 1 h before H -> T1 = H
    E = "KXEARNINGSMENTIONZZZ-96OCT30"
    occ_iso = iso(H + 3 * 86400 + 6 * 3600)                    # precise start 3 days later
    CA_expect = cancel_time(E, occ_iso)
    state = {"phase": "open"}

    def mk(tk, word, ask, bid, status="active", result="", close=None):
        return {"ticker": tk, "event_ticker": E, "status": status, "result": result, "open_time": iso(op), "close_time": iso(close or H + 30 * 86400),
                "yes_ask_dollars": f"{ask:.4f}", "yes_bid_dollars": f"{bid:.4f}", "yes_ask_size_fp": "100.00", "yes_bid_size_fp": "50.00",
                "volume_fp": "0", "custom_strike": {"Word": word}, "yes_sub_title": word, "created_time": iso(op - 1800),
                "occurrence_datetime": occ_iso, "expected_expiration_time": iso(H + 20 * 86400)}
    fc = CA_expect + 10 * 3600

    def open_list():
        return [mk(E + "-AAA", "Tariff", 0.85, 0.02), mk(E + "-BBB", "Moon", 0.30, 0.29), mk(E + "-CCC", "Robot", 0.80, 0.03)]

    def responder(path):
        if path.startswith("/markets?min_created_ts="):
            lo = int(path.split("min_created_ts=")[1].split("&")[0]); hi = int(path.split("max_created_ts=")[1].split("&")[0])
            rows = open_list() + [{"ticker": "KXNFLGAME-X", "event_ticker": "KXNFLGAME-X", "created_time": iso(H - 1000), "open_time": iso(H)}]
            return {"markets": [m for m in rows if lo <= ts_of(m["created_time"]) <= hi]}
        if path.startswith("/markets?series_ticker="):
            return {"markets": open_list() if state["phase"] == "open" else []}
        if path.startswith(f"/markets?event_ticker={E}"):
            if state["phase"] == "open":
                return {"markets": open_list()}
            return {"markets": [mk(E + "-AAA", "Tariff", 0.99, 0.98, "finalized", "yes", fc), mk(E + "-BBB", "Moon", 0.01, 0.0, "finalized", "no", fc + 5),
                                mk(E + "-CCC", "Robot", 0.01, 0.0, "finalized", "no", fc + 3)]}
        if "/orderbook" in path:
            return {"orderbook_fp": {"no_dollars": [["0.1500", "40.00"], ["0.1600", "7.00"]], "yes_dollars": [["0.0200", "9.00"]]}}
        if path.startswith("/markets/trades?ticker="):
            tk = path.split("ticker=")[1].split("&")[0]; post = state["post"]
            if tk.endswith("AAA"):     # s = 84c
                return {"trades": [{"created_time": iso(post + 60), "yes_price_dollars": "0.8500", "count_fp": "100", "taker_side": "yes"},     # before post+120: ignored
                                   {"created_time": iso(post + 3600), "yes_price_dollars": "0.8500", "count_fp": "4", "taker_side": "yes"},    # through (B and A)
                                   {"created_time": iso(post + 7200), "yes_price_dollars": "0.8400", "count_fp": "3", "taker_side": "yes"},    # at s
                                   {"created_time": iso(post + 50 * 3600), "yes_price_dollars": "0.8600", "count_fp": "10", "taker_side": "yes"},  # after 48 h: A only
                                   {"created_time": iso(post + 51 * 3600), "yes_price_dollars": "0.9500", "count_fp": "50", "taker_side": "yes", "is_block_trade": True}],
                        "cursor": ""}
            return {"trades": [], "cursor": ""}
        return {}

    try:
        OUT = tmp
        lg = Logger(tmp, FakeBudget(10, responder), now=H - 30 * 60); lg.st["started"] = H - 3 * 3600; lg.run()
        assert set(lg.st["markets"]) == {E + "-AAA", E + "-BBB", E + "-CCC"}, lg.st["markets"]
        assert all(x["status"] == "tracked" for x in lg.st["markets"].values())
        assert lg.st["disc_filter_ok"]
        lg = Logger(tmp, FakeBudget(10, responder), now=H + 120); lg.run()
        o = lg.st["orders"]
        assert set(o) == {E + "-AAA", E + "-CCC"}, o          # BBB spread 1c
        assert o[E + "-AAA"]["s_c"] == 84 and o[E + "-AAA"]["N"] == 31, o[E + "-AAA"]
        assert o[E + "-AAA"]["probe"] and "post" in o[E + "-AAA"]["probes"]
        state["post"] = o[E + "-AAA"]["post"]
        # 2 days later: schedule refresh due (12 h), then close + settle + fills
        lg = Logger(tmp, FakeBudget(10, responder), now=H + 2 * 86400); lg.run()
        assert lg.st["events"][E]["last_sched"] >= H + 2 * 86400
        state["phase"] = "closed"
        lg = Logger(tmp, FakeBudget(10, responder), now=CA_expect + SETTLE_FIRST_S + 60); lg.run()
        rows = [json.loads(l) for l in (tmp / "forward.jsonl").open()]
        fills = {r["ticker"]: r for r in rows if r["type"] == "fill"}
        a = fills[E + "-AAA"]
        assert a["cancel_A"] == CA_expect and abs(a["cancel_B"] - (state["post"] + H_B_S)) < 1e-6, a
        assert (a["B"]["through_cnt"], a["B"]["any_cnt"]) == (4.0, 7.0), a["B"]
        assert (a["A"]["through_cnt"], a["A"]["any_cnt"]) == (14.0, 17.0), a["A"]
        assert a["B"]["queue_cnt"] == 4.0, a["B"]             # 40 contracts already at our NO price 16c -> 3 at s do not reach us
        st_ = [r for r in rows if r["type"] == "settle"][0]
        assert st_["first_close"] == fc
        assert all(json.loads(l).get("disc_filter_ok", True) for l in (tmp / "forward.jsonl").open() if '"pass"' in l)
        print("selftest OK", {k: {"A": v["A"]["through_cnt"], "B": v["B"]["through_cnt"]} for k, v in fills.items()})
    finally:
        OUT = out0
        if keep is not None:
            shutil.copytree(tmp, keep, dirs_exist_ok=True)
        shutil.rmtree(tmp)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        mc = int(os.environ.get("R4E_MAX_CALLS", MAX_CALLS))
        if "--max-calls" in sys.argv:
            mc = int(sys.argv[sys.argv.index("--max-calls") + 1])
        run_pass(mc)
