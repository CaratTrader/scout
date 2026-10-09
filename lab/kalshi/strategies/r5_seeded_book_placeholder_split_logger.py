"""r5_seeded_book_placeholder_split_logger: forward PAPER ledger that pools the three seeded-book maker loggers and adds
the placeholder-only arm (frozen in data/kalshi_lab/strategies/r5_seeded_book_placeholder_split/preregistration.json).

One pass per invocation (recommended every 10 min at minutes 5,15,...,55, after the source loggers' passes). At most 10
Kalshi calls per pass (default 6; R5PS_MAX_CALLS or --max-calls may only lower the hard cap of 10), every call through
lab.us.data_refresh.fetch (temperature-bot idle window, 429 retries). It never places orders.

Sources (read-only, append-only files written by their own launchd jobs):
  r3m  data/kalshi_lab/strategies/r3_mentions_maker_forward/forward.jsonl       (C1, cancel at the event's first close)
  r4e  data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward/forward.jsonl (C1E arm A, cancel_A; arm B ignored)
  r4s  data/kalshi_lab/strategies/r4_single_appearance_seeded_books/forward.jsonl (C1S / V1, cancel C)
Stage 0 (no call): ingest new rows. A posted 'entry' becomes a pooled order; its arm is fixed now from the logged entry
  quote: P (placeholder) iff bid_c <= 4 and ask_c >= 79, else NP. Duplicate tickers keep the first source. A source 'fill'
  row gives through-only contracts (prints strictly above s after post + 120 s, before the source's cancel, block trades
  excluded); the mirror (buy YES at b = bid + 1c, prints strictly below b) is taken from r4s's row or recomputed from the
  source's saved prints (trades/<ticker>.json). A source 'settle' row gives the result.
Stage 1 (calls): live order-book probe of new P orders within 30 min of their post (<= 3 per pass, <= 30 per UTC day):
  size already resting at our price (queue ahead), size inside our price (undercut), best YES bid (crossing), whether the
  book is still a placeholder.
Stage 2 (calls): independent audit of filled P orders (<= 2 per pass): re-fetch the prints of the source's fill window and
  recount; the ledger uses f = min(N, source count, audit count) (conservative).
Stage 3 (calls): fallback for P orders whose source never produced a fill row (source logger stopped): after
  max(post + 21 d, cancel hint + 7 d), read /markets/{t}; once settled, fetch its prints over (post + 120 s, min(cancel
  hint, close)) and settle it here (<= 2 calls per pass).
Stage 4 (calls, optional, never in the arm statistics): census probe of one currently tracked listing whose entry hour
  H1 is still ahead (<= 1 per pass, <= 24 per UTC day): what the seeded book offers before entry (depth at the seeded ask).
Files (data/kalshi_lab/strategies/r5_seeded_book_placeholder_split/): forward.jsonl (append-only rows typed order /
probe / census / fill / audit / settle / fallback / pass), state.json, trades/<ticker>.json (audit and fallback prints),
calls.log (every Kalshi call), .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_seeded_book_placeholder_split_logger [--max-calls N] [--selftest]"""
from __future__ import annotations
import datetime as dt, fcntl, json, math, os, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_seeded_book_placeholder_split_api import Budget, BudgetExhausted

OUT = Path(os.environ.get("R5PS_OUT", "data/kalshi_lab/strategies/r5_seeded_book_placeholder_split"))
SRC_ROOT = Path(os.environ.get("R5PS_SRC_ROOT", "data/kalshi_lab/strategies"))
SOURCES = {"r3m": "r3_mentions_maker_forward", "r4e": "r4_earnings_seeded_book_48h_forward",
           "r4s": "r4_single_appearance_seeded_books"}
MAX_CALLS = 10
DEFAULT_CALLS = 6
P_BID_MAX_C, P_ASK_MIN_C = 4, 79          # frozen placeholder definition (cents, inclusive)
POST_GAP_S = 120
PROBE_WINDOW_S = 30 * 60
PROBE_PER_PASS, PROBE_PER_DAY = 3, 30
AUDIT_PER_PASS = 2
FALLBACK_PER_PASS = 2
FALLBACK_AFTER_POST_S = 21 * 86400
FALLBACK_AFTER_CANCEL_S = 7 * 86400
CENSUS_PER_DAY = 24
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended"}


# ------------------------------------------------------------------------------------------------ helpers
def ts_of(s) -> float | None:
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fnum(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cents(x) -> int | None:
    v = fnum(x)
    return None if v is None else int(round(v * 100))


def is_P(bid_c, ask_c) -> bool:
    return bid_c is not None and ask_c is not None and bid_c <= P_BID_MAX_C and ask_c >= P_ASK_MIN_C


def book_levels(ob: dict) -> tuple[list, list]:
    """(NO bids, YES bids) as [[cents, size], ...] from an /orderbook response (orderbook_fp dollars or legacy cents)."""
    fp = ob.get("orderbook_fp") or {}
    if fp:
        return ([[cents(p), fnum(q)] for p, q in fp.get("no_dollars") or []],
                [[cents(p), fnum(q)] for p, q in fp.get("yes_dollars") or []])
    o = ob.get("orderbook") or {}
    return [[int(p), fnum(q)] for p, q in o.get("no") or []], [[int(p), fnum(q)] for p, q in o.get("yes") or []]


def count_prints(prints: list, lo: float, hi: float, s_c: int | None = None, b_c: int | None = None) -> dict:
    """prints [ts, yes_cents, count, taker_side, is_block]; window lo < ts < hi; blocks excluded."""
    w = [p for p in prints if p[0] is not None and p[1] is not None and lo < p[0] < hi and not p[4]]
    out = {"n_prints": len(w)}
    if s_c is not None:
        out["thr"] = round(sum(p[2] for p in w if p[1] > s_c), 2)
        out["any"] = round(sum(p[2] for p in w if p[1] >= s_c), 2)
        out["t_first_thr"] = min((p[0] for p in w if p[1] > s_c), default=None)
    if b_c is not None:
        out["m_thr"] = round(sum(p[2] for p in w if p[1] < b_c), 2)
    return out


def parse_trades(d: dict) -> list:
    return [[ts_of(x.get("created_time")), cents(x.get("yes_price_dollars")), fnum(x.get("count_fp") or x.get("count")) or 0.0,
             x.get("taker_side"), bool(x.get("is_block_trade"))] for x in d.get("trades") or []]


# ------------------------------------------------------------------------------------------------ the logger
class Logger:
    def __init__(self, out: Path, src_root: Path, budget, now: float | None = None) -> None:
        self.out = out; self.src = src_root; self.B = budget; self.now = now or time.time(); self._t0 = time.time()
        self.rows: list[dict] = []; self.stage = defaultdict(int); self.errors: list[str] = []
        sf = out / "state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        s = self.st
        s.setdefault("version", 1); s.setdefault("started", self.now)
        for k in ("offsets", "orders", "probe_day", "census_day", "census_seen", "listings"):
            s.setdefault(k, {})
        if s.get("version") == 1:                            # v2: only 'tracked' listings are kept for the census
            s["listings"] = {}; s["version"] = 2

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

    def day(self) -> str:
        return dt.datetime.fromtimestamp(self.now, dt.timezone.utc).strftime("%Y-%m-%d")

    # ---- stage 0: ingest the source ledgers
    def ingest(self) -> None:
        for src, d in SOURCES.items():
            f = self.src / d / "forward.jsonl"
            if not f.exists():
                self.stage[f"src_missing_{src}"] += 1; continue
            off = self.st["offsets"].get(src, 0)
            size = f.stat().st_size
            if size < off:                                   # a source file was rewritten: never re-ingest, flag it
                self.errors.append(f"source_shrank:{src}"); self.st["offsets"][src] = size; continue
            with f.open("rb") as fh:
                fh.seek(off); data = fh.read()
            cut = data.rfind(b"\n") + 1                      # only complete lines
            for line in data[:cut].decode().splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    self.errors.append(f"bad_line:{src}"); continue
                self.take(src, r)
            self.st["offsets"][src] = off + cut

    def take(self, src: str, r: dict) -> None:
        typ = r.get("type")
        if typ == "listing":
            t = r.get("ticker")
            if t and r.get("status") == "tracked":           # eligible for a future entry ('prestart' never is)
                self.st["listings"][t] = {"src": src, "e": r.get("e"), "H1": r.get("H1") or r.get("T1"), "seen": r.get("ts")}
        elif typ in ("missed", "entry"):
            t = r.get("ticker")
            if t:
                self.st["listings"].pop(t, None)
            if typ == "missed":
                return
            if not r.get("posted") or not t:
                return
            O = self.st["orders"]
            if t in O:
                if O[t]["src"] != src:
                    self.stage["dup_ticker"] += 1
                return
            ask_c, bid_c, s_c = r.get("ask_c"), r.get("bid_c"), r.get("s_c")
            N = r.get("N") or int(500 // (100 - s_c))
            b_c = bid_c + 1 if (bid_c is not None and ask_c is not None and bid_c + 1 < ask_c) else None
            arm = "P" if is_P(bid_c, ask_c) else "NP"
            hint = r.get("cancel_A_at_post") if src == "r4e" else (r.get("C_planned") if src == "r4s" else None)
            o = {"src": src, "e": r.get("e"), "series": r.get("series"), "post": r.get("ts"), "ask_c": ask_c, "bid_c": bid_c,
                 "s_c": s_c, "N": N, "b_c": b_c, "Nb": int(500 // b_c) if b_c else None, "arm": arm, "cancel_hint": hint,
                 "ask_size": r.get("ask_size"), "bid_size": r.get("bid_size"), "fill": None, "audit": None, "result": None,
                 "probe": None, "fallback": None}
            O[t] = o
            self.row("order", ticker=t, **{k: o[k] for k in ("src", "e", "series", "post", "ask_c", "bid_c", "s_c", "N", "b_c",
                                                              "Nb", "arm", "cancel_hint", "ask_size", "bid_size")},
                     taker_no_px_ref=round(1 - bid_c / 100, 2) if bid_c is not None else None)
            self.stage["orders_ingested"] += 1
        elif typ == "probe":
            o = self.st["orders"].get(r.get("ticker") or "")
            if o and r.get("phase") == "post" and o["src"] == src:
                o["src_probe_at_q"] = r.get("size_at_our_price")
        elif typ == "fill":
            t = r.get("ticker"); o = self.st["orders"].get(t or "")
            if not o or o["src"] != src or o["fill"]:
                return
            if src == "r4e":
                m = r.get("A") or {}; lo, hi = r.get("lo"), r.get("cancel_A")
                thr, anyc = m.get("through_cnt"), m.get("any_cnt")
            elif src == "r4s":
                lo, hi = r.get("lo"), r.get("hi"); thr, anyc = r.get("thr_cnt"), r.get("any_cnt")
            else:
                lo, hi = r.get("lo"), r.get("hi"); thr, anyc = r.get("through_cnt"), r.get("any_cnt")
            mthr = r.get("m_thr_cnt") if src == "r4s" else self.mirror_from_prints(src, t, lo, hi, o["b_c"])
            o["fill"] = {"lo": lo, "hi": hi, "thr": thr, "any": anyc, "m_thr": mthr, "pages": r.get("pages")}
            self.row("fill", ticker=t, src=src, arm=o["arm"], lo=lo, hi=hi, s_c=o["s_c"], N=o["N"], thr_cnt=thr, any_cnt=anyc,
                     b_c=o["b_c"], Nb=o["Nb"], m_thr_cnt=mthr, f_thr=min(o["N"], thr or 0.0))
            self.stage["fills_ingested"] += 1
        elif typ == "settle":
            mk = r.get("markets") or {}
            for t, v in mk.items():
                o = self.st["orders"].get(t)
                if o and o["src"] == src and o["result"] is None:
                    res = (v[0] if isinstance(v, list) and v else "") or "void"
                    o["result"] = res; o["settle_ts"] = r.get("ts")
                    self.row("settle", ticker=t, src=src, arm=o["arm"], result=res, e=o["e"], first_close=r.get("first_close"))
                    self.stage["settles_ingested"] += 1

    def mirror_from_prints(self, src: str, t: str, lo, hi, b_c) -> float | None:
        if b_c is None or lo is None or hi is None:
            return None
        f = self.src / SOURCES[src] / "trades" / f"{t}.json"
        if not f.exists():
            return None
        try:
            pr = json.loads(f.read_text()).get("prints") or []
        except ValueError:
            return None
        return count_prints(pr, lo, hi, b_c=b_c)["m_thr"]

    # ---- stage 1: live order-book probes of new P orders
    def probes(self) -> None:
        day = self.day(); used = self.st["probe_day"].get(day, 0)
        cand = sorted((o["post"], t) for t, o in self.st["orders"].items()
                      if o["arm"] == "P" and o["probe"] is None and o["post"] and 0 <= self.now - o["post"] <= PROBE_WINDOW_S)
        for _, t in cand[: max(0, min(PROBE_PER_PASS, PROBE_PER_DAY - used))]:
            if self.B.left <= 0:
                break
            o = self.st["orders"][t]
            ob = self.call(f"/markets/{t}/orderbook")
            self.st["probe_day"][day] = self.st["probe_day"].get(day, 0) + 1
            if not ob:
                o["probe"] = {"ok": False}; continue
            rec = self.book_record(ob, o["s_c"])
            o["probe"] = {"ok": True, **rec}
            self.row("probe", ticker=t, src=o["src"], since_post_s=round(self.clock() - o["post"], 1), **rec)
            self.stage["probes"] += 1

    def book_record(self, ob: dict, s_c: int | None) -> dict:
        no, yes = book_levels(ob)
        best_no = max((p for p, sz in no if sz and sz > 0), default=None)
        best_yes = max((p for p, sz in yes if sz and sz > 0), default=None)
        ask_c = 100 - best_no if best_no is not None else None
        rec = {"best_yes_bid_c": best_yes, "implied_yes_ask_c": ask_c,
               "size_at_best_ask": round(sum(sz for p, sz in no if p == best_no), 2) if best_no is not None else 0.0,
               "still_placeholder": is_P(best_yes if best_yes is not None else 0, ask_c if ask_c is not None else 100),
               "yes_levels": len(yes), "no_levels": len(no)}
        if s_c is not None:
            q_c = 100 - s_c
            rec.update({"s_c": s_c, "queue_at_our_price": round(sum(sz for p, sz in no if p == q_c), 2),
                        "size_inside_ours": round(sum(sz for p, sz in no if p > q_c), 2),
                        "crossed": best_yes is not None and best_yes >= s_c,
                        "depth_within_5c_of_s": round(sum(sz for p, sz in no if q_c - 5 <= p <= q_c), 2)})
        return rec

    # ---- stage 2: independent audits of filled P orders
    def audits(self) -> None:
        n = 0
        for _, t in sorted((o["post"], t) for t, o in self.st["orders"].items()
                           if o["arm"] == "P" and o["fill"] and o["audit"] is None and (o["fill"].get("thr") or 0) > 0):
            if n >= AUDIT_PER_PASS or self.B.left <= 0:
                break
            o = self.st["orders"][t]; f = o["fill"]
            lo, hi = int(f["lo"]), int(math.ceil(f["hi"]))
            d = self.call(f"/markets/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000"); n += 1
            if "trades" not in d:
                continue
            pr = parse_trades(d)
            self.save_prints(t, f["lo"], f["hi"], pr)
            c = count_prints(pr, f["lo"], f["hi"], s_c=o["s_c"], b_c=o["b_c"])
            partial = bool(d.get("cursor"))
            o["audit"] = {"thr": c["thr"], "m_thr": c.get("m_thr"), "partial": partial, "n_prints": c["n_prints"]}
            self.row("audit", ticker=t, src=o["src"], source_thr=f["thr"], audit_thr=c["thr"], partial=partial,
                     source_m_thr=f.get("m_thr"), audit_m_thr=c.get("m_thr"), match=abs((f["thr"] or 0) - c["thr"]) < 1e-6)
            self.stage["audits"] += 1

    def save_prints(self, t: str, lo, hi, pr: list) -> None:
        tf = self.out / "trades" / f"{t}.json"; tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(json.dumps({"lo": lo, "hi": hi, "prints": pr}))

    # ---- stage 3: fallback when a source stopped
    def fallbacks(self) -> None:
        n = 0
        for _, t in sorted((o["post"], t) for t, o in self.st["orders"].items() if o["arm"] == "P" and not o["fill"]):
            if n >= FALLBACK_PER_PASS or self.B.left <= 0:
                break
            o = self.st["orders"][t]; fb = o["fallback"] or {}
            due = max(o["post"] + FALLBACK_AFTER_POST_S, (o["cancel_hint"] or 0) + FALLBACK_AFTER_CANCEL_S)
            if self.now < due or self.now < fb.get("next", 0):
                continue
            if not fb.get("close"):
                d = self.call(f"/markets/{t}"); n += 1
                m = d.get("market") or {}
                if m.get("result") in ("yes", "no", "void") or m.get("status") in CLOSED:
                    fb.update({"close": ts_of(m.get("close_time")), "result": m.get("result") or "void"})
                else:
                    fb["next"] = self.now + 86400
                o["fallback"] = fb; continue
            hi = min(c for c in (o["cancel_hint"], fb["close"]) if c)
            lo = o["post"] + POST_GAP_S
            d = self.call(f"/markets/trades?ticker={t}&min_ts={int(lo)}&max_ts={int(math.ceil(hi))}&limit=1000"); n += 1
            if "trades" not in d:
                continue
            pr = parse_trades(d); self.save_prints(t, lo, hi, pr)
            c = count_prints(pr, lo, hi, s_c=o["s_c"], b_c=o["b_c"])
            o["fill"] = {"lo": lo, "hi": hi, "thr": c["thr"], "any": c["any"], "m_thr": c.get("m_thr"), "pages": 1,
                         "fallback": True, "partial": bool(d.get("cursor"))}
            self.row("fallback", ticker=t, src=o["src"], arm=o["arm"], lo=lo, hi=hi, thr_cnt=c["thr"], any_cnt=c["any"],
                     m_thr_cnt=c.get("m_thr"), partial=bool(d.get("cursor")))
            if o["result"] is None:
                o["result"] = fb["result"]
                self.row("settle", ticker=t, src=o["src"], arm=o["arm"], result=fb["result"], e=o["e"], via="fallback")
            self.stage["fallbacks"] += 1

    # ---- stage 4: census of one not-yet-entered listing (descriptive only)
    def census(self) -> None:
        day = self.day()
        if self.B.left <= 1 or self.st["census_day"].get(day, 0) >= CENSUS_PER_DAY:
            return
        L = [(v.get("seen") or 0, t) for t, v in self.st["listings"].items()
             if t not in self.st["census_seen"] and t not in self.st["orders"] and (v.get("H1") or 0) > self.now]
        if not L:
            return
        _, t = max(L)
        ob = self.call(f"/markets/{t}/orderbook")
        self.st["census_day"][day] = self.st["census_day"].get(day, 0) + 1
        self.st["census_seen"][t] = self.now
        if ob:
            v = self.st["listings"][t]
            self.row("census", ticker=t, src=v["src"], e=v.get("e"), H1=v.get("H1"), **self.book_record(ob, None))
            self.stage["census"] += 1

    # ---- one pass
    def run(self) -> dict:
        try:
            self.ingest()
            self.probes()
            self.audits()
            self.fallbacks()
            self.census()
        except BudgetExhausted:
            self.errors.append("budget_exhausted")
        cnt = defaultdict(int)
        for o in self.st["orders"].values():
            cnt[o["arm"]] += 1
            cnt[o["arm"] + "_filled"] += bool(o["fill"] and min(o["N"], o["fill"].get("thr") or 0) > 0)
            cnt[o["arm"] + "_settled"] += o["result"] in ("yes", "no")
        self.row("pass", calls=self.B.used, stages=dict(self.stage), errors=self.errors[:10], orders=dict(cnt),
                 listings_tracked=len(self.st["listings"]), dur_s=round(time.time() - self._t0, 1))
        self.save()
        return {"calls": self.B.used, "stages": dict(self.stage), "orders": dict(cnt), "errors": self.errors}

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "forward.jsonl").open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in self.rows))
        tmp = self.out / "state.json.tmp"
        tmp.write_text(json.dumps(self.st)); os.replace(tmp, self.out / "state.json")


def main(argv: list[str]) -> None:
    cap = DEFAULT_CALLS
    env = os.environ.get("R5PS_MAX_CALLS")
    if env:
        cap = int(env)
    if "--max-calls" in argv:
        cap = int(argv[argv.index("--max-calls") + 1])
    cap = max(0, min(cap, MAX_CALLS))
    OUT.mkdir(parents=True, exist_ok=True)
    lock = (OUT / ".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another pass is running"); return
    try:
        print(json.dumps(Logger(OUT, SRC_ROOT, Budget(cap)).run()))
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
    """Three synthetic source ledgers -> ingest, arm tagging, probe, fill, audit (min rule), settle, fallback, census."""
    tmp = Path(tempfile.mkdtemp(prefix="r5ps_selftest_")); src = tmp / "src"; out = tmp / "out"
    T0 = 1_800_000_000.0
    for d in SOURCES.values():
        (src / d / "trades").mkdir(parents=True)

    def put(srcname, rows):
        with (src / SOURCES[srcname] / "forward.jsonl").open("a") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
    # r3m: one placeholder (bid 2, ask 85) and one human book (bid 40, ask 55)
    put("r3m", [{"type": "listing", "ts": T0 - 600, "ticker": "M-1-ZZZ", "e": "M-1", "status": "tracked", "H1": T0 + 3600},
                {"type": "listing", "ts": T0 - 600, "ticker": "M-1-YYY", "e": "M-1", "status": "prestart", "H1": T0 - 3600},
                {"type": "entry", "ts": T0, "posted": True, "ticker": "M-1-AAA", "e": "M-1", "series": "M", "ask_c": 85, "bid_c": 2, "s_c": 84, "N": 31},
                {"type": "entry", "ts": T0, "posted": True, "ticker": "M-1-BBB", "e": "M-1", "series": "M", "ask_c": 55, "bid_c": 40, "s_c": 54, "N": 10},
                {"type": "entry", "ts": T0, "posted": False, "ticker": "M-1-CCC", "e": "M-1", "series": "M", "ask_c": 55, "bid_c": 54}])
    # r4e: placeholder order whose source will never write a fill (fallback path)
    put("r4e", [{"type": "entry", "ts": T0, "posted": True, "ticker": "E-1-AAA", "e": "E-1", "series": "E", "ask_c": 90, "bid_c": 1,
                 "s_c": 89, "N": 45, "cancel_A_at_post": T0 + 5 * 86400}])
    # r4s: a duplicate ticker (ignored) and a non-placeholder order
    put("r4s", [{"type": "entry", "ts": T0, "posted": True, "ticker": "M-1-AAA", "e": "M-1", "series": "M", "ask_c": 85, "bid_c": 2, "s_c": 84, "N": 31},
                {"type": "entry", "ts": T0, "posted": True, "ticker": "S-1-AAA", "e": "S-1", "series": "S", "ask_c": 80, "bid_c": 10, "s_c": 79, "N": 23,
                 "C_planned": T0 + 86400}])

    def responder(path):
        if "/orderbook" in path:
            return {"orderbook_fp": {"no_dollars": [["0.1500", "200.00"], ["0.1600", "7.00"]], "yes_dollars": [["0.0200", "50.00"]]}}
        if path.startswith("/markets/trades?ticker=M-1-AAA"):       # audit finds fewer through contracts than the source
            return {"trades": [{"created_time": dt.datetime.fromtimestamp(T0 + 4000, dt.timezone.utc).isoformat(), "yes_price_dollars": "0.8600",
                                "count_fp": "6.00", "taker_side": "yes"},
                               {"created_time": dt.datetime.fromtimestamp(T0 + 5000, dt.timezone.utc).isoformat(), "yes_price_dollars": "0.0100",
                                "count_fp": "9.00", "taker_side": "no"}], "cursor": ""}
        if path.startswith("/markets/E-1-AAA"):
            return {"market": {"ticker": "E-1-AAA", "status": "finalized", "result": "no",
                               "close_time": dt.datetime.fromtimestamp(T0 + 6 * 86400, dt.timezone.utc).isoformat()}}
        if path.startswith("/markets/trades?ticker=E-1-AAA"):
            return {"trades": [{"created_time": dt.datetime.fromtimestamp(T0 + 3 * 86400, dt.timezone.utc).isoformat(), "yes_price_dollars": "0.9500",
                                "count_fp": "100.00", "taker_side": "yes"}], "cursor": ""}
        return {}
    # pass 1 at T0 + 5 min: ingest, probe the 2 new P orders (M-1-AAA, E-1-AAA), census one listing
    lg = Logger(out, src, FakeBudget(6, responder), now=T0 + 300); r = lg.run()
    O = lg.st["orders"]
    assert set(O) == {"M-1-AAA", "M-1-BBB", "E-1-AAA", "S-1-AAA"}, O
    assert O["M-1-AAA"]["arm"] == "P" and O["M-1-BBB"]["arm"] == "NP" and O["E-1-AAA"]["arm"] == "P" and O["S-1-AAA"]["arm"] == "NP", O
    assert O["M-1-AAA"]["src"] == "r3m" and r["stages"].get("dup_ticker") == 1, r
    assert r["stages"]["probes"] == 2 and r["stages"]["census"] == 1, r
    assert O["M-1-AAA"]["probe"]["queue_at_our_price"] == 7.0 and O["M-1-AAA"]["probe"]["still_placeholder"], O["M-1-AAA"]["probe"]
    # source fill + settle for M-1 (source through 12, saved prints with a mirror print below b = 3c)
    (src / SOURCES["r3m"] / "trades" / "M-1-AAA.json").write_text(json.dumps({"lo": T0 + 120, "hi": T0 + 86400, "prints": [
        [T0 + 4000, 86, 12.0, "yes", False], [T0 + 5000, 1, 9.0, "no", False], [T0 + 60, 99, 50.0, "yes", False]]}))
    put("r3m", [{"type": "fill", "ts": T0 + 90000, "ticker": "M-1-AAA", "e": "M-1", "lo": T0 + 120, "hi": T0 + 86400, "s_c": 84, "N": 31,
                 "through_cnt": 12.0, "any_cnt": 12.0, "pages": 1},
                {"type": "fill", "ts": T0 + 90000, "ticker": "M-1-BBB", "e": "M-1", "lo": T0 + 120, "hi": T0 + 86000, "s_c": 54, "N": 10,
                 "through_cnt": 0.0, "any_cnt": 3.0, "pages": 1},
                {"type": "settle", "ts": T0 + 90000, "e": "M-1", "markets": {"M-1-AAA": ["no", T0 + 86400, "finalized"],
                                                                              "M-1-BBB": ["yes", T0 + 86000, "finalized"]}}])
    lg = Logger(out, src, FakeBudget(6, responder), now=T0 + 90500); r = lg.run()
    a = lg.st["orders"]["M-1-AAA"]
    assert a["fill"]["thr"] == 12.0 and a["fill"]["m_thr"] == 9.0 and a["result"] == "no", a
    assert a["audit"]["thr"] == 6.0 and a["audit"]["m_thr"] == 9.0, a["audit"]
    assert lg.st["orders"]["M-1-BBB"]["result"] == "yes"
    # pass 3, 22 days later: fallback for E-1-AAA (source never wrote a fill)
    lg = Logger(out, src, FakeBudget(6, responder), now=T0 + 22 * 86400); r = lg.run()
    lg = Logger(out, src, FakeBudget(6, responder), now=T0 + 22 * 86400 + 600); r = lg.run()
    e = lg.st["orders"]["E-1-AAA"]
    assert e["fill"]["fallback"] and e["fill"]["thr"] == 100.0 and e["result"] == "no", e
    # summary on the synthetic ledger: conservative f = min(N, source, audit) = 6 for M-1-AAA
    from lab.kalshi.strategies import r5_seeded_book_placeholder_split_summary as SM
    S_ = SM.summarize(out)
    rows = {x["ticker"]: x for x in SM.final_orders(out)}
    assert rows["M-1-AAA"]["f"] == 6.0 and rows["E-1-AAA"]["f"] == 45.0 and rows["M-1-BBB"]["f"] == 0.0, rows
    print("selftest OK:", tmp, json.dumps({k: S_["arms"][k]["sw_through"] for k in ("P", "NP", "ALL")}, default=str)[:400])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main(sys.argv[1:])
