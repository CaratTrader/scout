"""r7_truth_social_daily_forward_shadow - forward-only paper logger (shadow) for KXTRUTHSOCIALD, the daily count of
Donald Trump's Truth Social posts (00:00-23:59 ET, close 23:59 ET, 8 brackets <5, 5-9, 10-14, 15-19, 20-24, 25-29,
30-39, 40+, settled on Roll Call; launched 2026-10-01).

Frozen 2026-10-09 before any forward data (preregistration.json). Nothing here was fitted or tuned on KXTRUTHSOCIALD
prices or outcomes (gate amendment (l)): the model is the r6 weekly model (frozen_model.json, sha1 b5cb287f...),
fitted on the 23 discovery weeks 2026-02-08..07-18, applied to the daily window without refit.

pass  (at most once per 15-minute slot, 10:00-23:45 ET; a no-op outside that window, no calls):
  1. Roll Call / Factba.se feed (the resolution source), pages of 50 newest-first, back to now - 7 days (2-4 pages).
  2. Kalshi call 1: GET /markets?series_ticker=KXTRUTHSOCIALD&status=open -> today's brackets (bid, ask, sizes).
  3. Frozen model -> P(final daily count in each bracket):
       final = seen - Del + R; seen = today's visible count; Del ~ Binomial(today's posts since the last 03:00 ET
       flag run, delta) = later deletions (the weekly model's haircut, as a distribution); R ~ NegBin(mean mu, size
       phi = 1) = remaining posts; mu = L x profile mass over (now, 24:00 ET];
       L = 0.7 (c7 + 15)/(E7 + 15) + 0.3 (c24 + 5)/(E24 + 5) over the trailing 7 days / 24 hours (c = visible
       counts, E = hour-of-day x day-of-week profile mass; L = 1 is the discovery-period average rate).
       The trailing 7 days reproduce the information the weekly model had at its fitted decision points
       (Fri 18:00 .. Sat 23:00 ET, 5.75-7 days of window). Diagnostics, probabilities only and never traded:
       level from the weekly window to date (= the weekly logger's state) and from today only.
  4. Rule D1 (primary): buy side S of a bracket when P_model(S) - taker price(S) >= 0.10, price in [0.10, 0.90]
     (taker price YES = yes_ask, NO = 1 - yes_bid, from call 1, which is made after the Roll Call fetch).
  5. Per new signal, largest edge first, at most 8 books and 10 Kalshi calls per pass: GET /markets/{t}/orderbook;
     paper fill = min(contracts displayed at or better than the signal price, floor($5 / price)) AT the signal
     price, fee ceil-to-cent on that order. A bracket-side is taken once per day (its first signal with a non-zero
     displayed fill); zero-fill signals are logged and may signal again at a later pass.
  6. Saturdays only, Kalshi call 2 (descriptive arm, never filled): GET /markets?series_ticker=KXTRUTHSOCIAL&
     status=open. The weekly window ends with Saturday's daily window, so daily D = weekly W - F with F = kept
     posts Sun 00:00 .. Sat 00:00 ET. The weekly mids imply P(D >= W_edge - F) at the weekly bracket edges and,
     spread uniformly within 20-wide weekly brackets, a daily bracket distribution; both are logged against the
     daily ladder and the model, with would-be signals.
settle (run 09:00-09:59 ET): one GET /markets?event_ticker=E per finished event (<= 8 per run), plus a Roll Call
  fetch to check the reconstructed count against Kalshi's winning bracket (data-error check).
auto: pass inside 10:00-23:59 ET; settle + summary inside 09:00-09:59 ET; otherwise nothing.

Usage: .venv/bin/python -m lab.kalshi.strategies.r7_truth_social_daily_forward_shadow_logger [auto|pass|settle|status]
       [--force]  run a pass now regardless of the clock, into test_passes/ (not the forward record)
       [--sat]    with --force: also run the Saturday arm
Outputs: data/kalshi_lab/strategies/r7_truth_social_daily_forward_shadow/{forward.jsonl, settled.jsonl, state.json,
         calls.log}; --force writes under test_passes/ instead."""
from __future__ import annotations
import bisect, datetime as dt, hashlib, json, math, re, sys, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.r6_truth_social_count_endgame import Model, nb_cdf_table   # frozen weekly model, no refit
from lab.kalshi.strategies.r6_truth_social_count_endgame_data import http, RC_URL

NAME = "r7_truth_social_daily_forward_shadow"
OUT = ROOT / "data/kalshi_lab/strategies" / NAME
FROZEN_MODEL = ROOT / "data/kalshi_lab/strategies/r6_truth_social_count_endgame/frozen_model.json"
FROZEN_SHA1 = "b5cb287f065d4a78f60105011da7dfe8d966556a"
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
DAILY, WEEKLY = "KXTRUTHSOCIALD", "KXTRUTHSOCIAL"

# ---- frozen rule D1 (preregistration.json) ----
THETA = 0.10                 # model edge over the taker price
PX_LO, PX_HI = 0.10, 0.90    # taker price band
FIRST_MIN, LAST_MIN, SLOT_MIN = 10 * 60, 23 * 60 + 45, 15   # pass slots 10:00, 10:15, ..., 23:45 ET (56 a day)
CLOSE_MIN = 23 * 60 + 59     # the daily market closes at 23:59 ET
LOOKBACK_S = 7 * 86400       # level window of the primary model
STAKE = 5.0                  # paper stake cap per signal, $
MAX_CALLS, MAX_BOOKS = 10, 8
BIG = 10 ** 6
WIN_RE = re.compile(r"begins at 12:00 AM ET on (\w+ \d+, \d+) and ends at 11:59 PM ET on (\w+ \d+, \d+)")


# ---------------------------------------------------------------- network (replaced by fakes in the selftest)
class CallCap(Exception):
    pass


class Net:
    """Kalshi only through lab.us.data_refresh.fetch (waits for the bot's idle window, retries 429s), paced 1.15 s,
    logged to calls.log and capped per pass; Roll Call through the r6 stdlib http()."""

    def __init__(self, out: Path, cap: int = MAX_CALLS):
        self.out, self.cap, self.calls = out, cap, 0

    def kalshi(self, path: str) -> dict | None:
        if self.calls >= self.cap:
            raise CallCap(path)
        from lab.us.data_refresh import fetch
        url = KBASE + path
        self.calls += 1
        txt = fetch(url, pace=1.15)
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "calls.log").open("a") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {len(txt)} {url}\n")
        try:
            return json.loads(txt) if txt else None
        except ValueError:
            return None

    def rollcall_page(self, p: int) -> str:
        return http(RC_URL.format(p=p))

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def clock(self) -> float:
        return time.time()


def _iso_ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def rollcall_rows(net, A: int, max_pages: int = 20) -> tuple[list[dict], dict]:
    """Posts with ts >= A, newest-first pages until the oldest post on a page precedes A. ok=False unless a post older
    than A was reached (then the pass emits no signals: an undercount would bias every bracket)."""
    rows, ok, pages = {}, False, 0
    for p in range(1, max_pages + 1):
        txt = net.rollcall_page(p); pages += 1
        try:
            data = json.loads(txt).get("data") if txt else None
        except ValueError:
            data = None
        if not data:            # failed page, or the feed ended before reaching A (it holds years of posts): not ok
            break
        for x in data:
            s = x.get("social") or {}
            dd = s.get("deleted_date")
            rows[x["id"]] = {"ts": _iso_ts(x["date"]), "deleted": bool(x.get("deleted_flag")),
                             "flag_ts": _iso_ts(dd) if dd else None}
        if min(_iso_ts(x["date"]) for x in data) < A:
            ok = True; break
        net.sleep(1.0)
    kept = [r for r in rows.values() if r["ts"] >= A]
    newest = max((r["ts"] for r in rows.values()), default=None)
    return kept, {"pages": pages, "ok": ok, "n": len(kept), "newest_ts": newest}


class Posts:
    """Same semantics as the r6 weekly logger's LivePosts: seen(A, t) = posts in [A, t] minus deletions Roll Call had
    flagged by t (a deleted post without a flag time counts as flagged now)."""

    def __init__(self, rows: list[dict], now: int):
        self.all = sorted(r["ts"] for r in rows)
        self.dele = sorted((r["ts"], r["flag_ts"] or now) for r in rows if r["deleted"])
        self.kept = sorted(r["ts"] for r in rows if not r["deleted"])

    def seen(self, A: int, t: int) -> int:
        n = bisect.bisect_right(self.all, t) - bisect.bisect_left(self.all, A)
        return n - sum(1 for ts, fl in self.dele if A <= ts <= t and fl <= t)

    def n_all(self, a: int, b: int) -> int:
        """all posts (deleted or not) with a <= ts <= b"""
        return bisect.bisect_right(self.all, b) - bisect.bisect_left(self.all, a)

    def kept_between(self, a: int, b: int) -> int:
        """posts not (yet) deleted with a <= ts < b"""
        return bisect.bisect_left(self.kept, b) - bisect.bisect_left(self.kept, a)


# ---------------------------------------------------------------- frozen model applied to the daily window
def load_model() -> Model:
    raw = FROZEN_MODEL.read_bytes()
    if hashlib.sha1(raw).hexdigest() != FROZEN_SHA1:
        raise RuntimeError("frozen_model.json changed since the freeze")
    return Model.from_frozen(json.loads(raw))


def week_bounds(ts: int) -> tuple[int, int]:
    """Sunday 00:00 ET at or before ts, and the next Sunday 00:00 ET (the weekly count window)."""
    lt = dt.datetime.fromtimestamp(ts, ET)
    sun = (lt - dt.timedelta(days=(lt.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = (sun + dt.timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(sun.timestamp()), int(nxt.timestamp())


def profile_mass(M: Model, t0: float, t1: float) -> float:
    """Expected posts in (t0, t1] at L = 1 under the frozen profile base x hod[ET hour] x dow[ET day, Sunday = 0], by
    ET wall clock. Equal to the weekly code's M.mass (minutes since Sunday 00:00 ET) except in the two DST-change weeks
    a year, where the weekly indexing is one hour off and drops the week's last hour."""
    tot, a = 0.0, t0
    while a < t1:
        lt = dt.datetime.fromtimestamp(a, ET)
        b = min(t1, lt.replace(minute=0, second=0, microsecond=0).timestamp() + 3600)
        tot += M.base * M.hod[lt.hour] * M.dow[(lt.weekday() + 1) % 7] * (b - a) / 60
        a = b
    return tot


def last_flag_run(t: int) -> int:
    lt = dt.datetime.fromtimestamp(t, ET)
    last = lt.replace(hour=3, minute=0, second=0, microsecond=0)
    if last > lt:
        last = (last - dt.timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
    return int(last.timestamp())


def kept_est(M: Model, P: Posts, A: int, t: int) -> tuple[float, int, int]:
    """(kept-count estimate, visible count, posts since the last 03:00 ET deletion-flag run) for the window [A, t];
    the haircut is delta per post not yet through a flag run (as in the weekly model)."""
    seen = P.seen(A, t)
    recent = max(0, P.n_all(max(last_flag_run(t), A), t))
    return seen - M.delta * recent, seen, recent


def level(M: Model, P: Posts, t: int, A: int) -> float:
    """The weekly model's level L with the level window starting at A (weekly logger: A = Sunday 00:00 ET)."""
    c, _, _ = kept_est(M, P, A, t)
    E = profile_mass(M, A, t)
    L = (c + M.a) / (E + M.a)
    if M.w > 0:
        t24 = max(A, t - 86400)
        c24 = P.seen(A, t) - P.seen(A, t24)
        E24 = profile_mass(M, t24, t)
        L = (1 - M.w) * L + M.w * (c24 + M.a / 3) / (E24 + M.a / 3)
    return L


def ladder_probs(seen: int, recent: int, delta: float, mu: float, phi: float, brackets: list[dict], kmax: int = 400) -> dict:
    """P(final in [lo, hi]) for final = seen - Del + R, R ~ NB(mu, phi) (remaining posts), Del ~ Binomial(recent, delta)
    (later deletions among the posts not yet through a flag run; same mean as the weekly model's haircut c = seen -
    delta x recent). The weekly code shifts by the fractional mean instead, which drops one R value at every bracket
    edge (its probabilities sum to < 1) and, with rounding, would move a count that just entered a bracket back out."""
    cdf = nb_cdf_table(max(mu, 1e-9), phi, kmax)

    def F(k: int) -> float:   # P(R <= k)
        return 0.0 if k < 0 else (cdf[k] if k <= kmax else 1.0)
    dmax = max(0, min(recent, seen, 25))
    pd = [math.comb(recent, d) * delta ** d * (1 - delta) ** (recent - d) for d in range(dmax + 1)]
    z = sum(pd); pd = [x / z for x in pd]
    out = {}
    for b in brackets:
        p = 0.0
        for d, w in enumerate(pd):
            a = max(b["lo"] - seen + d, 0)
            if b["hi"] >= BIG:
                p += w * (1.0 - F(a - 1))
            else:
                hi = b["hi"] - seen + d
                if hi >= a:
                    p += w * (F(hi) - F(a - 1))
        out[b["t"]] = max(0.0, min(1.0, p))
    return out


def daily_model(M: Model, P: Posts, t: int, A_day: int, B_day: int, brackets: list[dict]) -> dict:
    c, seen, recent = kept_est(M, P, A_day, t)
    rem = profile_mass(M, t, B_day)
    out = {"seen": seen, "c": round(c, 3), "recent": recent, "mass_rem": round(rem, 3), "L": {}, "mu": {}, "p": {}}
    for name, A_lvl in (("d1", t - LOOKBACK_S), ("week", week_bounds(t)[0]), ("day", A_day)):
        L = level(M, P, t, A_lvl); mu = L * rem
        out["L"][name] = round(L, 4); out["mu"][name] = round(mu, 3)
        out["p"][name] = ladder_probs(seen, recent, M.delta, mu, M.phi, brackets)
    return out


# ---------------------------------------------------------------- market parsing
def bracket(m: dict) -> dict | None:
    st = m.get("strike_type"); lo = m.get("floor_strike"); hi = m.get("cap_strike")
    try:
        if st == "between":
            L, H = int(lo), int(hi)
        elif st == "less":
            L, H = 0, int(hi) - 1
        elif st == "less_or_equal":
            L, H = 0, int(hi)
        elif st == "greater":
            L, H = int(lo) + 1, BIG
        elif st == "greater_or_equal":
            L, H = int(lo), BIG
        else:
            return None
    except (TypeError, ValueError):
        return None
    return {"t": m["ticker"], "lo": L, "hi": H}


def ladder_ok(brs: list[dict]) -> bool:
    if not brs or brs[0]["lo"] != 0 or brs[-1]["hi"] < BIG:
        return False
    return all(b2["lo"] == b1["hi"] + 1 for b1, b2 in zip(brs, brs[1:]))


def market_window(m: dict, weekly: bool = False) -> tuple[int, int] | None:
    """Observation window [A, B) from the rules text; fallback from the close time (daily: the close's ET date;
    weekly: the 7 days ending at the close's ET midnight)."""
    mm = WIN_RE.search(m.get("rules_secondary") or "")
    if mm:
        try:
            a = dt.datetime.strptime(mm.group(1), "%b %d, %Y").replace(tzinfo=ET)
            b = (dt.datetime.strptime(mm.group(2), "%b %d, %Y") + dt.timedelta(days=1)).replace(tzinfo=ET)
            return int(a.timestamp()), int(b.timestamp())
        except ValueError:
            pass
    if not m.get("close_time"):
        return None
    ce = dt.datetime.fromtimestamp(_iso_ts(m["close_time"]), ET)
    if weekly:
        b = ce.replace(hour=0, minute=0, second=0, microsecond=0)
        a = (b - dt.timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        a = ce.replace(hour=0, minute=0, second=0, microsecond=0)
        b = (a + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(a.timestamp()), int(b.timestamp())


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def taker_prices(m: dict) -> tuple[float | None, float | None]:
    """(YES taker price = yes_ask, NO taker price = 1 - yes_bid); None when that side has no displayed liquidity."""
    ask, bid = _f(m.get("yes_ask_dollars")), _f(m.get("yes_bid_dollars"))
    asz, bsz = _f(m.get("yes_ask_size_fp")), _f(m.get("yes_bid_size_fp"))
    y = ask if ask is not None and 0 < ask < 1 and (asz is None or asz > 0) else None
    n = round(1 - bid, 4) if bid is not None and 0 < bid < 1 and (bsz is None or bsz > 0) else None
    return y, n


def fee_order(px: float, n: int) -> float:
    """Kalshi taker fee for an n-contract order: 0.07 p (1-p) n rounded UP to the cent (total $)."""
    return math.ceil(round(0.07 * px * (1 - px) * n * 100, 6)) / 100


def book_depth(book: dict | None, side: str, px: float) -> dict:
    """Contracts displayed at or better than px for a taker buy of `side`. Kalshi books list bids only: a YES buy
    lifts NO bids (YES price = 1 - NO bid), a NO buy hits YES bids (NO price = 1 - YES bid)."""
    if not book:
        return {"read": False}
    ob = book.get("orderbook_fp") or {}
    lv = ob.get("no_dollars" if side == "YES" else "yes_dollars")
    if lv is None and book.get("orderbook"):           # older cents format
        o = book["orderbook"]; lv = [[q / 100, s] for q, s in (o.get("no" if side == "YES" else "yes") or [])]
    lv = [(float(q), float(s)) for q, s in (lv or [])]
    ex = sorted(((round(1 - q, 4), s) for q, s in lv), key=lambda x: x[0])     # executable prices, best first
    at = sum(s for p, s in ex if p <= px + 1e-9)
    return {"read": True, "best_exec": ex[0][0] if ex else None, "best_size": ex[0][1] if ex else 0.0,
            "disp_at_px": round(at, 2), "levels": [[p, s] for p, s in ex[:6]]}


# ---------------------------------------------------------------- state
def _paths(test: bool) -> tuple[Path, Path, Path, Path]:
    d = OUT / "test_passes" if test else OUT
    return d, d / ("forward_test.jsonl" if test else "forward.jsonl"), d / ("settled_test.jsonl" if test else "settled.jsonl"), d / "state.json"


def load_state(sf: Path) -> dict:
    try:
        s = json.loads(sf.read_text())
    except (OSError, ValueError):
        s = {}
    for k in ("slots", "taken", "events"):
        s.setdefault(k, {})
    return s


def save_state(sf: Path, s: dict) -> None:
    keep = sorted(s["slots"])[-4:]
    s["slots"] = {k: s["slots"][k] for k in keep}
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(".tmp"); tmp.write_text(json.dumps(s)); tmp.replace(sf)


def _append(f: Path, rows: list[dict]) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def slot_of(lt: dt.datetime) -> int | None:
    m = lt.hour * 60 + lt.minute
    if m < FIRST_MIN or m >= CLOSE_MIN:
        return None
    return min((m - FIRST_MIN) // SLOT_MIN, (LAST_MIN - FIRST_MIN) // SLOT_MIN)


# ---------------------------------------------------------------- Saturday arm (descriptive)
def saturday_arm(wk_markets: list[dict], P: Posts, t: int, A_day: int, B_day: int, daily_brs: list[dict],
                 quotes: dict, p_model: dict) -> dict:
    """Weekly-ladder-implied distribution of Saturday's daily count D = W - F (F = kept posts Sun 00:00 .. Sat 00:00)."""
    ev = [m for m in wk_markets if (w := market_window(m, weekly=True)) and w[1] == B_day]
    if not ev:
        return {"row": "sat_arm", "ts": t, "skip": "no weekly event ending with this daily window"}
    A_wk = market_window(ev[0], weekly=True)[0]
    F = P.kept_between(A_wk, A_day)
    wb = []
    for m in ev:
        b = bracket(m)
        bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        if b is None or bid is None or ask is None or ask <= 0:
            return {"row": "sat_arm", "ts": t, "skip": f"weekly quote missing {m.get('ticker')}"}
        wb.append({**b, "bid": bid, "ask": ask, "mid": (bid + ask) / 2})
    wb.sort(key=lambda b: b["lo"])
    if not ladder_ok(wb):
        return {"row": "sat_arm", "ts": t, "skip": "weekly ladder incomplete"}
    S = sum(b["mid"] for b in wb)
    for b in wb:
        b["p"] = b["mid"] / S
    # daily bracket probabilities implied by the weekly ladder, mass spread uniformly within each weekly bracket
    # (open ends: the bottom bracket's width is its own span, the top bracket's mass sits in [lo, lo + 20))
    imp = {}
    for d in daily_brs:
        lo, hi = d["lo"] + F, (d["hi"] + F if d["hi"] < BIG else BIG)
        p = 0.0
        for b in wb:
            blo, bhi = b["lo"], (b["hi"] if b["hi"] < BIG else b["lo"] + 19)
            if d["hi"] >= BIG and b["hi"] >= BIG and blo >= lo:
                p += b["p"]; continue
            ov = min(hi, bhi) - max(lo, blo) + 1
            if ov > 0:
                p += b["p"] * ov / (bhi - blo + 1)
        imp[d["t"]] = round(p, 4)
    # survival comparisons exactly at the weekly edges k = W_edge - F inside the daily ladder's range
    dm = {}
    for d in daily_brs:
        q = quotes.get(d["t"]) or {}
        bid, ask = q.get("bid"), q.get("ask")
        dm[d["t"]] = None if bid is None or ask is None else (bid + ask) / 2
    edges = []
    if all(v is not None for v in dm.values()) and sum(dm.values()) > 0:
        Sd = sum(dm.values()); pd = {k: v / Sd for k, v in dm.items()}

        def surv(prob: dict, k: int) -> float:
            s = 0.0
            for d in daily_brs:
                if d["lo"] >= k:
                    s += prob[d["t"]]
                elif d["hi"] >= k:
                    w = (d["hi"] - d["lo"] + 1) if d["hi"] < BIG else 20
                    s += prob[d["t"]] * min(1.0, (d["hi"] - k + 1) / w if d["hi"] < BIG else (d["lo"] + w - k) / w)
            return s
        for j, b in enumerate(wb):
            k = b["lo"] - F
            if 1 <= k <= 60:
                edges.append({"k": k, "S_weekly": round(sum(x["p"] for x in wb[j:]), 4), "S_daily_mid": round(surv(pd, k), 4),
                              "S_model": round(surv(p_model, k), 4)})
    would = []
    for d in daily_brs:
        q = quotes.get(d["t"]) or {}
        for side, px, pw in (("YES", q.get("px_yes"), imp[d["t"]]), ("NO", q.get("px_no"), 1 - imp[d["t"]])):
            if px is not None and PX_LO <= px <= PX_HI and pw - px >= THETA:
                would.append({"t": d["t"], "side": side, "px": px, "implied": round(pw, 4)})
    return {"row": "sat_arm", "ts": t, "weekly_event": ev[0]["event_ticker"], "F_kept_sun_fri": F,
            "weekly": [[b["t"], b["lo"], b["hi"], b["bid"], b["ask"]] for b in wb], "weekly_mid_sum": round(S, 4),
            "implied_daily": imp, "edges": edges, "would_signals": would}


# ---------------------------------------------------------------- one pass
def one_pass(now: float | None = None, net=None, force: bool = False, sat: bool = False, M: Model | None = None) -> dict | None:
    test = force
    d, ff, _, sf = _paths(test)
    net = net or Net(d)
    now = time.time() if now is None else now
    lt = dt.datetime.fromtimestamp(now, ET)
    slot = slot_of(lt)
    if slot is None and not force:
        return None
    today = lt.date().isoformat()
    state = load_state(sf)
    if not force and slot in state["slots"].get(today, []):
        return None
    M = M or load_model()
    rows, rc = rollcall_rows(net, int(now) - LOOKBACK_S - 3600)
    t = int(net.clock())                     # information time: the Roll Call fetch is complete
    P = Posts(rows, t)
    rec = {"row": "pass", "ts": t, "iso": dt.datetime.fromtimestamp(t, ET).isoformat(timespec="seconds"), "slot": slot,
           "test": test, "rollcall": {**rc, "newest_age_min": round((t - rc["newest_ts"]) / 60, 1) if rc["newest_ts"] else None}}
    try:
        lst = net.kalshi(f"/markets?series_ticker={DAILY}&status=open&limit=100")
    except CallCap:
        lst = None
    ms = (lst or {}).get("markets") or []
    t_q = int(net.clock())
    todays = [m for m in ms if (w := market_window(m)) and w[0] <= t < w[1]]
    if not todays:
        rec.update({"event": None, "eligible": False, "why": "no open daily event for today" if lst is not None else "quote call failed",
                    "kalshi_calls": net.calls})
        _append(ff, [rec]); state["slots"].setdefault(today, []).append(slot); save_state(sf, state)
        return rec
    A_day, B_day = market_window(todays[0]); event = todays[0]["event_ticker"]
    brs = sorted((b for m in todays if (b := bracket(m))), key=lambda b: b["lo"])
    mod = daily_model(M, P, t, A_day, B_day, brs)
    quotes = {}
    for m in todays:
        y, n = taker_prices(m)
        quotes[m["ticker"]] = {"bid": _f(m.get("yes_bid_dollars")), "ask": _f(m.get("yes_ask_dollars")),
                               "bid_size": _f(m.get("yes_bid_size_fp")), "ask_size": _f(m.get("yes_ask_size_fp")),
                               "px_yes": y, "px_no": n, "sub": m.get("yes_sub_title"), "vol": _f(m.get("volume_fp"))}
    eligible = bool(rc["ok"]) and t < B_day - 60 and (slot is not None or force)
    rec.update({"event": event, "A": A_day, "B": B_day, "t_quote": t_q, "eligible": eligible, "ladder_ok": ladder_ok(brs),
                "seen_today": mod["seen"], "c_today": mod["c"], "recent_today": mod["recent"], "mass_rem": mod["mass_rem"],
                "L": mod["L"], "mu": mod["mu"],
                "brackets": [[b["t"], quotes[b["t"]]["sub"], b["lo"], b["hi"] if b["hi"] < BIG else None, quotes[b["t"]]["bid"],
                              quotes[b["t"]]["ask"], quotes[b["t"]]["bid_size"], quotes[b["t"]]["ask_size"], quotes[b["t"]]["vol"],
                              round(mod["p"]["d1"][b["t"]], 4), round(mod["p"]["week"][b["t"]], 4), round(mod["p"]["day"][b["t"]], 4)]
                             for b in brs]})
    state["events"].setdefault(event, {"A": A_day, "B": B_day})
    taken = set(state["taken"].get(event, []))
    sigs = []
    if eligible:
        for b in brs:
            q = quotes[b["t"]]; p = mod["p"]["d1"][b["t"]]
            for side, px, pw in (("YES", q["px_yes"], p), ("NO", q["px_no"], 1 - p)):
                if px is None or not (PX_LO <= px <= PX_HI) or pw - px < THETA - 1e-9:
                    continue
                sigs.append({"row": "signal", "ts": t, "iso": rec["iso"], "test": test, "event": event, "t": b["t"], "sub": q["sub"],
                             "lo": b["lo"], "hi": b["hi"] if b["hi"] < BIG else None, "side": side, "px": px, "model": round(pw, 4), "edge": round(pw - px, 4),
                             "p_week": round(mod["p"]["week"][b["t"]] if side == "YES" else 1 - mod["p"]["week"][b["t"]], 4),
                             "p_day": round(mod["p"]["day"][b["t"]] if side == "YES" else 1 - mod["p"]["day"][b["t"]], 4),
                             "seen_today": mod["seen"], "mu": mod["mu"]["d1"], "h_to_close": round((B_day - 60 - t) / 3600, 2),
                             "taken_before": f"{b['t']}|{side}" in taken})
    sat_row = None
    if lt.weekday() == 5 or sat:
        try:
            wk = net.kalshi(f"/markets?series_ticker={WEEKLY}&status=open&limit=100")
            sat_row = saturday_arm((wk or {}).get("markets") or [], P, t, A_day, B_day, brs, quotes, mod["p"]["d1"])
            sat_row.update({"event": event, "test": test, "iso": rec["iso"]})
        except CallCap:
            sat_row = {"row": "sat_arm", "ts": t, "skip": "call cap"}
    books = 0
    for s in sorted((s for s in sigs if not s["taken_before"]), key=lambda s: -s["edge"]):
        bk = None
        if books < MAX_BOOKS and net.calls < net.cap:
            try:
                bk = net.kalshi(f"/markets/{s['t']}/orderbook"); books += 1
            except CallCap:
                bk = None
        dep = book_depth(bk, s["side"], s["px"])
        s["book"] = dep; s["t_book"] = int(net.clock())
        n_fill = int(min(math.floor(dep.get("disp_at_px") or 0), math.floor(STAKE / s["px"]))) if dep.get("read") else 0
        s["n_fill"] = n_fill; s["filled"] = n_fill >= 1
        s["fee_per_contract"] = round(fee_order(s["px"], n_fill) / n_fill, 5) if n_fill else None
        s["fee10_per_contract"] = round(fee_order(s["px"], 10) / 10, 5)
        if s["filled"]:
            taken.add(f"{s['t']}|{s['side']}")
    rec.update({"n_signals": len(sigs), "n_new_signals": sum(not s["taken_before"] for s in sigs),
                "n_filled": sum(bool(s.get("filled")) for s in sigs), "kalshi_calls": net.calls, "books": books})
    rows_out = [rec] + [s for s in sigs if not s["taken_before"]] + ([sat_row] if sat_row else [])
    _append(ff, rows_out)
    state["taken"][event] = sorted(taken)
    state["slots"].setdefault(today, []).append(slot)
    save_state(sf, state)
    return rec


# ---------------------------------------------------------------- settlement
def _read(f: Path) -> list[dict]:
    if not f.exists():
        return []
    out = []
    for line in f.open():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def settle(now: float | None = None, net=None, test: bool = False, max_events: int = 3) -> list[dict]:
    """Settle finished events (window end + 30 min passed, not yet settled): one Kalshi call per event (Kalshi settled
    the 7 launch events 0.5-41 h after close, so unsettled events are retried at the next hourly attempt); a Roll Call
    fetch only when some event has settled, to check the reconstructed count against the winning bracket."""
    d, ff, stf, sf = _paths(test)
    net = net or Net(d)
    now = time.time() if now is None else now
    state = load_state(sf)
    if now - state.get("last_settle_try", 0) < 50 * 60:
        return []
    done = {r["event"] for r in _read(stf) if r.get("row") == "event"}
    todo = sorted((e for e, w in state["events"].items() if e not in done and now >= w["B"] + 1800),
                  key=lambda e: state["events"][e]["B"])[:max_events]
    if not todo:
        return []
    state["last_settle_try"] = int(now); save_state(sf, state)
    got = {}
    for e in todo:
        try:
            res = net.kalshi(f"/markets?event_ticker={e}&limit=100")
        except CallCap:
            break
        ms = (res or {}).get("markets") or []
        if ms and all(m.get("result") in ("yes", "no") for m in ms):
            got[e] = ms
    if not got:
        return []
    rows, rc = rollcall_rows(net, min(state["events"][e]["A"] for e in got) - 3600, max_pages=30)
    P = Posts(rows, int(now))
    fw = [r for r in _read(ff) if r.get("row") == "signal"]
    out = []
    for e, ms in got.items():
        w = state["events"][e]
        r = {m["ticker"]: m["result"] for m in ms}
        win = [m for m in ms if m["result"] == "yes"]
        brs = {b["t"]: b for m in ms if (b := bracket(m))}
        rc_final = P.kept_between(w["A"], w["B"]) if rc["ok"] else None
        wb = brs.get(win[0]["ticker"]) if win else None
        out.append({"row": "event", "event": e, "A": w["A"], "B": w["B"], "winner": win[0]["ticker"] if win else None,
                    "winner_sub": win[0].get("yes_sub_title") if win else None, "expiration_value": ms[0].get("expiration_value"),
                    "rollcall_final": rc_final,
                    "rc_bracket_match": (wb["lo"] <= rc_final <= wb["hi"]) if (wb and rc_final is not None) else None,
                    "settled_at": int(now), "event_volume": round(sum(_f(m.get("volume_fp")) or 0 for m in ms), 2)})
        for s in fw:
            if s["event"] != e or s["t"] not in r:
                continue
            won = (r[s["t"]] == "yes") == (s["side"] == "YES")
            fpc = s["fee_per_contract"] if s.get("filled") else s["fee10_per_contract"]
            pnl = (1.0 if won else 0.0) - s["px"] - fpc
            out.append({"row": "settled", "event": e, "t": s["t"], "side": s["side"], "lo": s.get("lo"), "hi": s.get("hi"),
                        "seen_at_signal": s.get("seen_today"), "ts": s["ts"], "px": s["px"], "model": s["model"],
                        "edge": s.get("edge"), "filled": bool(s.get("filled")), "n_fill": s.get("n_fill", 0),
                        "disp_at_px": (s.get("book") or {}).get("disp_at_px"), "result": r[s["t"]], "won": won,
                        "fee_per_contract": fpc, "ret_per_dollar": round(pnl / s["px"], 5), "B": w["B"]})
    _append(stf, out)
    return out


def main() -> None:
    a = sys.argv[1:]
    cmd = a[0] if a and not a[0].startswith("--") else "auto"
    force, sat = "--force" in a, "--sat" in a
    lt = dt.datetime.now(ET)
    if cmd == "pass":
        r = one_pass(force=force, sat=sat)
        print("no pass (outside 10:00-23:59 ET or slot done)" if r is None else
              json.dumps({k: r.get(k) for k in ("iso", "event", "eligible", "seen_today", "mu", "n_signals", "n_filled", "kalshi_calls")}))
    elif cmd == "settle":
        print(json.dumps(settle(test=force), indent=1))
    elif cmd == "auto":
        # launchd every 15 min: a pass inside 10:00-23:59 ET; an hourly settlement attempt (first quarter-hour) that
        # shares the pass's 10-call cap; the summary (no calls) whenever something settled.
        net = Net(OUT)
        r = one_pass(net=net) if slot_of(lt) is not None else None
        if r:
            print(json.dumps({k: r.get(k) for k in ("iso", "event", "eligible", "seen_today", "n_signals", "n_filled", "kalshi_calls")}))
        if lt.minute < 15:
            s = settle(net=net)
            if s:
                print(f"settled rows {len(s)}")
                from lab.kalshi.strategies.r7_truth_social_daily_forward_shadow_summary import main as summary
                summary([])
    elif cmd == "status":
        _, ff, stf, sf = _paths(force)
        print(json.dumps({"now_et": lt.isoformat(timespec="seconds"), "slot": slot_of(lt), "state": load_state(sf)["slots"],
                          "forward_rows": len(_read(ff)), "settled_rows": len(_read(stf))}, indent=1))


if __name__ == "__main__":
    main()
