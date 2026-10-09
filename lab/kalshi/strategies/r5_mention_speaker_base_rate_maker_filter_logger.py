"""r5_mention_speaker_base_rate_maker_filter_logger: forward PAPER overlay that tags every seeded-book maker order of
the three running mention loggers with the leak-free prior hit rate of its (speaker-format, word) and keeps a ledger.

Frozen rules: data/kalshi_lab/strategies/r5_mention_speaker_base_rate_maker_filter/preregistration.json (written
before the first pass). It never places orders and creates no virtual orders of its own: the orders, their through-only
fills (gate amendment c) and their settlements come from the source loggers' forward.jsonl files (read incrementally):
  r4e  r4_earnings_seeded_book_48h_forward   (arm A, cancel before the call)            pre-appearance cancel
  r4s  r4_single_appearance_seeded_books     (C1S sell side, cancel before the day)    pre-appearance cancel
  r3m  r3_mentions_maker_forward             (C1, rests until the first close)        reported only
One pass per invocation (recommended every 10 min at minutes 6,16,...,56). At most 10 Kalshi calls per pass
(R5_MAX_CALLS / --max-calls may only lower it), every call through lab.us.data_refresh.fetch(pace=1.15) (temperature-
bot idle window, 429 retries), logged in calls.log with tag 'logger'. The calls are settled-history sweeps only:
  historical tier  /historical/markets?series_ticker=S&limit=1000 (paged; ignores min_close_ts), once per series that no
                   family has fetched
  live tier        /markets?series_ticker=S&status=settled&min_close_ts=..&limit=1000 (paged, lookback <= 67 days):
                   (1) a series with an untagged entry or a new listing and no live sweep after listing - 1 h;
                   (2) maintenance, <= 4 calls a pass: universe series whose live coverage is older than 30 days
Stages: read sources (0 calls) -> sweeps (<= budget) -> tag entries -> pass row. Fill and settle ledger rows are
written while reading the sources.
Files (data/kalshi_lab/strategies/r5_mention_speaker_base_rate_maker_filter/): forward.jsonl (rows tag / fill / settle /
sweep / pass), state.json, settled_fwd.jsonl (pool additions with seen_at), calls.log, .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_mention_speaker_base_rate_maker_filter_logger [--max-calls N] [--selftest]
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies import r5_mention_speaker_base_rate_maker_filter_data as RD

S = ROOT / "data/kalshi_lab/strategies"
OUT = Path(os.environ.get("R5_OUT", str(RD.OUT)))
SOURCES = {
    "r4e": Path(os.environ.get("R5_SRC_R4E", str(S / "r4_earnings_seeded_book_48h_forward/forward.jsonl"))),
    "r4s": Path(os.environ.get("R5_SRC_R4S", str(S / "r4_single_appearance_seeded_books/forward.jsonl"))),
    "r3m": Path(os.environ.get("R5_SRC_R3M", str(S / "r3_mentions_maker_forward/forward.jsonl"))),
}
PRE_CANCEL = {"r4e", "r4s"}
MAX_CALLS = 10
BUFFER_S = 3600               # history: close <= listing - 1 h
SEEN_GRACE_S = 4 * 3600       # forward pool rows count only if seen_at <= entry time + 4 h
DEFER_S = 6 * 3600            # a tag waits at most 6 h for the series' sweeps
LIVE_LOOKBACK_S = 67 * 86400  # /markets?status=settled reaches ~68 days back
MAINT_AGE_S = 30 * 86400
MAINT_PER_PASS = 4
LISTING_RECENT_S = 3 * 86400
MAX_PAGES = 5
FROZEN_POOL_TS = 1791496000   # 2026-10-08 ~17:00 UTC: the on-disk caches' live-tier snapshots
HIST_CUTOFF_TS = 1786147200   # 2026-08-08 00:00 UTC
CALL_LOGS = [S / "r2_mentions_baserate/calls.log", S / "r3_earnings_call_mentions/calls.log",
             S / "r4_single_appearance_seeded_books/calls.log", S / "r4_earnings_seeded_book_48h_forward/research_calls.log"]


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    def __init__(self, cap: int, getter=None) -> None:
        self.cap = cap; self.used = 0; self.getter = getter

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        if self.getter is not None:
            return self.getter(path)
        from lab.us.data_refresh import fetch
        t0 = time.time()
        txt = fetch(RD.BASE + path, pace=1.15)
        with (OUT / "calls.log").open("a") as f:
            f.write(f"{int(t0)}\t{len(txt or '')}\tlogger\t{path}\n")
        if not txt:
            raise RuntimeError(f"empty response {path}")
        return json.loads(txt)


def bucket_of(k: int, n: int) -> str:
    if n == 0:
        return "NH"
    r = k / n
    return "LOW20" if r <= 0.20 + 1e-12 else ("LOW40" if r <= 0.40 + 1e-12 else "HIGH40")


def hist_fetched_elsewhere() -> set:
    out = set()
    for f in CALL_LOGS + [OUT / "calls.log"]:
        if not f.exists():
            continue
        for line in f.open():
            if "logger" in line.split("\t")[2:3]:
                continue                                   # this logger's own pages are tracked in state
            m = re.search(r"/historical/markets\?series_ticker=([A-Z0-9]+)&limit=1000(\s|$)", line)
            if m:
                out.add(m.group(1))
    return out


def universe() -> set:
    u = set()
    for f, key in ((S / "r4_earnings_seeded_book_48h_forward/frozen_series.json", "series"),):
        if f.exists():
            u |= set(json.loads(f.read_text()).get(key, []))
    for mod in ("r4_single_appearance_seeded_books_logger", "r3_mentions_maker_forward_logger"):
        src = (ROOT / f"lab/kalshi/strategies/{mod}.py").read_text()
        m = re.search(r"^SERIES = \[(.*?)\]", src, re.S | re.M)
        if m:
            u |= set(re.findall(r'"([A-Z0-9]+)"', m.group(1)))
    return u


class Logger:
    def __init__(self, budget: Budget, now: float | None = None) -> None:
        self.B = budget; self.now = now or time.time(); self.rows: list[dict] = []; self.errors: list[str] = []
        self.stage = defaultdict(int)
        sf = OUT / "state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        for k, v in (("offsets", {}), ("ev_open", {}), ("entries", {}), ("tagged", {}), ("results", {}), ("fills", {}),
                     ("series", {}), ("passes", 0)):
            self.st.setdefault(k, v)
        self._pool = None

    def row(self, typ: str, **kw) -> None:
        self.rows.append({"type": typ, "ts": round(self.now, 1), **kw})

    def sr(self, spk: str, raw: str | None = None) -> dict:
        x = self.st["series"].setdefault(spk, {"raws": [], "hist": {}, "live_at": None, "live_hist": [], "live_cursor": {}, "last_listing": 0})
        for r in ([raw] if raw else []) + [spk]:
            if r and r not in x["raws"]:
                if x["raws"]:
                    x["live_at"] = None                    # a new raw series under this speaker key: sweep it before tagging
                x["raws"].append(r)
        return x

    # ---- stage 1: sources (0 calls)
    def read_sources(self) -> None:
        for src, f in SOURCES.items():
            if not f.exists():
                continue
            off = self.st["offsets"].get(src, 0)
            if f.stat().st_size < off:
                off = 0                                    # rewritten file: re-read (ingest is idempotent)
            with f.open("rb") as fh:
                fh.seek(off)
                data = fh.read()
            cut = data.rfind(b"\n")
            if cut < 0:
                continue
            for line in data[:cut + 1].decode().splitlines():
                if line.strip():
                    try:
                        self.ingest(src, json.loads(line))
                    except Exception as ex:  # noqa: BLE001
                        self.errors.append(f"{src}: {str(ex)[:80]}")
            self.st["offsets"][src] = off + cut + 1

    def ingest(self, src: str, r: dict) -> None:
        typ = r.get("type")
        if typ == "listing":
            t = r.get("ticker") or ""
            e, op = r.get("e"), RD.ts(r.get("open"))
            if e and op:
                self.st["ev_open"][e] = min(self.st["ev_open"].get(e, op), op)
            raw = r.get("series") or t.split("-")[0]
            x = self.sr(RD.spk_key(raw), raw)
            x["last_listing"] = max(x.get("last_listing") or 0, op or self.now)
            self.stage["src_listings"] += 1
        elif typ == "entry" and r.get("posted"):
            t = r["ticker"]
            if t in self.st["entries"]:
                return
            op = RD.ts(r.get("open"))
            if op:
                self.st["ev_open"][r["e"]] = min(self.st["ev_open"].get(r["e"], op), op)
            raw = r.get("series") or t.split("-")[0]
            self.st["entries"][t] = {"src": src, "e": r["e"], "series": raw, "open": op, "post": r.get("ts"), "s_c": r.get("s_c"),
                                     "N": r.get("N"), "no_px": r.get("no_px"), "word": r.get("word") or "", "ask_c": r.get("ask_c"),
                                     "bid_c": r.get("bid_c"), "frozen": r.get("frozen")}
            self.sr(RD.spk_key(raw), raw)
            self.stage["src_entries"] += 1
        elif typ == "settle":
            for t, v in (r.get("markets") or {}).items():
                if t in self.st["entries"] and t not in self.st["results"] and v and v[0] in ("yes", "no", "void"):
                    self.st["results"][t] = v[0]
                    self.row("settle", ticker=t, src=self.st["entries"][t]["src"], e=self.st["entries"][t]["e"], result=v[0])
        elif typ == "fill":
            t = r.get("ticker")
            if t not in self.st["entries"] or t in self.st["fills"]:
                return
            N = r.get("N") or self.st["entries"][t]["N"]
            if src == "r4e":
                a = r.get("A") or {}
                thr, anyc = a.get("through_cnt"), a.get("any_cnt")
            elif src == "r4s":
                thr, anyc = r.get("thr_cnt"), r.get("any_cnt")
            else:
                thr, anyc = r.get("through_cnt"), r.get("any_cnt")
            if thr is None:
                self.errors.append(f"fill without through count {t}"); return
            q = min(float(N), float(thr)); qa = min(float(N), float(anyc or 0))
            self.st["fills"][t] = {"through": q, "any": qa}
            self.row("fill", ticker=t, src=src, e=self.st["entries"][t]["e"], N=N, through=q, any=qa)

    # ---- stage 2: settled-history sweeps (own calls)
    def listing_of(self, x: dict) -> float:
        v = [y for y in (self.st["ev_open"].get(x["e"]), x.get("open")) if y]
        return min(v) if v else (x.get("post") or self.now)

    def frozen_live(self) -> set:
        """Series whose live tier (closes after the historical cutoff) is in the frozen on-disk pool."""
        if self._pool is None:
            self._pool = RD.load_pool()
        return {r["series"] for r in self._pool.values() if r["close"] and r["close"] > HIST_CUTOFF_TS}

    def targets(self) -> list[tuple[str, float | None, str]]:
        """(spk, need_live_after, why) in priority order; need_live_after None = history only."""
        out, seen = [], set()
        pend = sorted(((x.get("post") or 0, RD.spk_key(x["series"]), self.listing_of(x)) for t, x in self.st["entries"].items()
                       if t not in self.st["tagged"]))
        for _, spk, L in pend:
            if spk not in seen:
                out.append((spk, L - BUFFER_S, "entry")); seen.add(spk)
        rec = sorted(((x.get("last_listing") or 0, spk) for spk, x in self.st["series"].items()
                      if self.now - (x.get("last_listing") or 0) <= LISTING_RECENT_S))
        for L, spk in rec:
            if spk not in seen:
                out.append((spk, L - BUFFER_S, "listing")); seen.add(spk)
        return out

    def sweep(self) -> None:
        done_hist = hist_fetched_elsewhere()
        fl = None
        for spk, after, why in self.targets():
            if self.B.left <= 0:
                return
            x = self.sr(spk)
            try:
                self.ensure_hist(spk, x, done_hist)
                if after is not None and (x.get("live_at") or 0) < after:
                    self.live(spk, x)
            except BudgetExhausted:
                return
            except Exception as ex:  # noqa: BLE001
                self.errors.append(f"sweep {spk}: {str(ex)[:80]}")
        # maintenance: keep live coverage continuous (the live tier only reaches ~68 days back)
        maint = 0
        fl = self.frozen_live()
        cands = []
        for raw in sorted(universe() | {r for x in self.st["series"].values() for r in x["raws"]}):
            spk = RD.spk_key(raw)
            x = self.st["series"].get(spk)
            cov = (x or {}).get("live_at") or (FROZEN_POOL_TS if raw in fl else 0)
            if self.now - cov >= MAINT_AGE_S:
                cands.append((cov, spk, raw))
        done_spk = set()
        for cov, spk, raw in sorted(cands):
            if maint >= MAINT_PER_PASS or self.B.left <= 0 or spk in done_spk:
                continue
            x = self.sr(spk, raw); done_spk.add(spk)
            before = self.B.used
            try:
                self.ensure_hist(spk, x, done_hist, cap=MAINT_PER_PASS - maint)
                self.live(spk, x, cap=MAINT_PER_PASS - maint - (self.B.used - before))
            except BudgetExhausted:
                return
            except Exception as ex:  # noqa: BLE001
                self.errors.append(f"maint {spk}: {str(ex)[:80]}")
            maint += self.B.used - before
            self.stage["maint_calls"] = maint

    def ensure_hist(self, spk: str, x: dict, done_hist: set, cap: int = 99) -> None:
        used0 = self.B.used
        for raw in list(x["raws"]):
            if x["hist"].get(raw) == "done":
                continue
            if raw in done_hist and raw not in x["hist"]:
                x["hist"][raw] = "done"; continue
            while x["hist"].get(raw) != "done":
                if self.B.used - used0 >= cap:
                    raise BudgetExhausted("cap")
                self.page(spk, raw, "hist")

    def live(self, spk: str, x: dict, cap: int = 99) -> None:
        used0 = self.B.used
        if not x.get("live_cycle"):
            x["live_cycle"] = {"start": self.now, "done": []}
        for raw in list(x["raws"]):
            if raw in x["live_cycle"]["done"]:
                continue
            while True:
                if self.B.used - used0 >= cap:
                    raise BudgetExhausted("cap")
                more = self.page(spk, raw, "live")
                if not more:
                    x["live_cycle"]["done"].append(raw); break
        x["live_at"] = x["live_cycle"]["start"]
        x["live_hist"] = (x.get("live_hist") or [])[-19:] + [x["live_at"]]
        x["live_cycle"] = None

    def page(self, spk: str, raw: str, tier: str) -> bool:
        x = self.st["series"][spk]
        if tier == "hist":
            st_ = x["hist"].get(raw) or {}
            cur, pages = st_.get("cursor", ""), st_.get("pages", 0)
            p = f"/historical/markets?series_ticker={raw}&limit=1000"
        else:
            st_ = x["live_cursor"].get(raw) or {}
            cur, pages = st_.get("cursor", ""), st_.get("pages", 0)
            lo = self.now - LIVE_LOOKBACK_S
            if x.get("live_at"):
                lo = max(lo, x["live_at"] - 2 * 86400)
            p = f"/markets?series_ticker={raw}&status=settled&min_close_ts={int(lo)}&limit=1000"
        p += f"&cursor={cur}" if cur else ""
        d = self.B.get(p)
        ms = d.get("markets") or []
        new = 0
        with (OUT / "settled_fwd.jsonl").open("a") as fh:
            for m in ms:
                r = RD._row({**m, "series": raw})
                if r and r["close"] is not None:
                    fh.write(json.dumps({"t": r["t"], "e": r["e"], "series": raw, "spk": r["spk"], "w": r["w"], "close": r["close"],
                                         "open": r["open"], "yes": r["yes"], "seen_at": round(self.now, 1)}) + "\n")
                    new += 1
        nxt = d.get("cursor") or ""
        more = bool(nxt and ms and pages + 1 < MAX_PAGES)
        if tier == "hist":
            x["hist"][raw] = {"cursor": nxt, "pages": pages + 1} if more else "done"
        else:
            x["live_cursor"][raw] = {"cursor": nxt, "pages": pages + 1} if more else {}
        self.stage[f"sweep_{tier}_calls"] += 1
        self.row("sweep", series=raw, tier=tier, markets=len(ms), settled_added=new, more=more)
        return more

    # ---- stage 3: tags
    def history(self) -> dict:
        if self._pool is None:
            self._pool = RD.load_pool()
        H = defaultdict(list)
        for r in self._pool.values():
            H[(r["spk"], r["w"])].append((r["close"], r["yes"], r["e"], 0.0))
        f = OUT / "settled_fwd.jsonl"
        if f.exists():
            for line in f.open():
                x = json.loads(line)
                H[(x["spk"], x["w"])].append((x["close"], x["yes"], x["e"], x["seen_at"]))
        return H

    def tag(self) -> None:
        todo = [t for t in self.st["entries"] if t not in self.st["tagged"]]
        if not todo:
            return
        H = self.history()
        for t in todo:
            x = self.st["entries"][t]
            spk = RD.spk_key(x["series"]); w = RD.wkey(x["word"])
            post = x.get("post") or self.now
            listing = self.listing_of(x)
            s = self.st["series"].get(spk, {})
            hist_ok = bool(s.get("raws")) and all(s["hist"].get(r) == "done" for r in s["raws"])
            live_ok = any(listing - BUFFER_S <= v <= post + SEEN_GRACE_S for v in s.get("live_hist") or [])
            complete = hist_ok and live_ok
            if not complete and self.now < post + DEFER_S:
                self.stage["deferred"] += 1
                continue
            uniq = {}
            for c, y, e, seen in H.get((spk, w), []):
                if c > listing - BUFFER_S or e == x["e"] or seen > post + SEEN_GRACE_S:
                    continue
                uniq[(c, e)] = y                             # one row per market (close, event): duplicates across caches
            k = sum(1 for y in uniq.values() if y); n = len(uniq)
            b = bucket_of(k, n)
            self.st["tagged"][t] = b
            arm_pre = x["src"] in PRE_CANCEL and (x["src"] != "r4e" or x.get("frozen") is True)
            self.row("tag", ticker=t, src=x["src"], e=x["e"], series=x["series"], spk=spk, w=w, word=x["word"][:80], listing=listing,
                     open=x.get("open"), post=post, s_c=x["s_c"], N=x["N"], no_px=x["no_px"], ask_c=x.get("ask_c"), bid_c=x.get("bid_c"),
                     frozen=x.get("frozen"), k=k, n=n, rate=round(k / n, 4) if n else None, bucket=b, pre_cancel_arm=arm_pre,
                     pool_complete=complete, tag_lag_s=round(self.now - post, 1))
            self.stage["tagged"] += 1

    # ---- one pass
    def run(self) -> dict:
        t0 = time.time()
        try:
            self.read_sources()
            self.sweep()
            self.tag()
        except Exception as ex:  # noqa: BLE001
            self.errors.append(f"pass: {str(ex)[:120]}")
        self.st["passes"] += 1
        p = {"calls": self.B.used, "stages": dict(self.stage), "errors": self.errors[:10], "entries": len(self.st["entries"]),
             "tagged": len(self.st["tagged"]), "fills": len(self.st["fills"]), "settled": len(self.st["results"]),
             "series_tracked": len(self.st["series"]), "dur_s": round(time.time() - t0, 1)}
        self.row("pass", **p)
        self.save()
        return p

    def save(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        with (OUT / "forward.jsonl").open("a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in self.rows))
        fd, tmp = tempfile.mkstemp(dir=OUT, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.st, f)
        os.replace(tmp, OUT / "state.json")


def run_pass(max_calls: int = MAX_CALLS, getter=None, now: float | None = None) -> dict | None:
    OUT.mkdir(parents=True, exist_ok=True)
    cap = max(0, min(MAX_CALLS, max_calls, int(os.environ.get("R5_MAX_CALLS", MAX_CALLS))))
    lock = open(OUT / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another pass is running"); return None
    try:
        return Logger(Budget(cap, getter), now).run()
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()


def selftest() -> None:
    """Offline: synthetic source rows and a fake API; checks tagging, deferral, fills, settlement and the call cap."""
    global OUT, SOURCES, CALL_LOGS
    d = Path(tempfile.mkdtemp())
    OUT = d / "out"; OUT.mkdir()
    SOURCES = {k: d / f"{k}.jsonl" for k in ("r4e", "r4s", "r3m")}
    CALL_LOGS = []
    now = time.time()
    e = "KXEARNINGSMENTIONZZZ-26NOV01"
    rows = [{"type": "listing", "ticker": f"{e}-AI", "e": e, "series": "KXEARNINGSMENTIONZZZ", "open": now - 7200},
            {"type": "entry", "ticker": f"{e}-AI", "e": e, "series": "KXEARNINGSMENTIONZZZ", "open": now - 7200, "posted": True,
             "ts": now - 3500, "s_c": 83, "N": 29, "no_px": 0.17, "word": "AI / Artificial Intelligence", "frozen": True},
            {"type": "entry", "ticker": f"{e}-TARI", "e": e, "series": "KXEARNINGSMENTIONZZZ", "open": now - 7200, "posted": True,
             "ts": now - 3500, "s_c": 83, "N": 29, "no_px": 0.17, "word": "Tariff", "frozen": True}]
    SOURCES["r4e"].write_text("".join(json.dumps(r) + "\n" for r in rows))
    calls = []

    def api(path):
        calls.append(path)
        if path.startswith("/historical/markets?series_ticker=KXEARNINGSMENTIONZZZ"):
            return {"markets": [
                {"ticker": "KXEARNINGSMENTIONZZZ-26AUG01-AI", "event_ticker": "KXEARNINGSMENTIONZZZ-26AUG01", "result": "no",
                 "custom_strike": {"Word": "AI / Artificial Intelligence"}, "open_time": "2026-07-20T00:00:00Z", "close_time": "2026-08-01T20:00:00Z"},
                {"ticker": "KXEARNINGSMENTIONZZZ-26AUG01-TARI", "event_ticker": "KXEARNINGSMENTIONZZZ-26AUG01", "result": "yes",
                 "custom_strike": {"Word": "Tariff"}, "open_time": "2026-07-20T00:00:00Z", "close_time": "2026-08-01T20:00:00Z"}], "cursor": ""}
        if "status=settled" in path and "KXEARNINGSMENTIONZZZ" in path:
            return {"markets": [{"ticker": "KXEARNINGSMENTIONZZZ-26SEP01-AI", "event_ticker": "KXEARNINGSMENTIONZZZ-26SEP01", "result": "no",
                                 "custom_strike": {"Word": "AI / Artificial Intelligence"}, "open_time": "2026-08-20T00:00:00Z",
                                 "close_time": "2026-09-01T20:00:00Z"}], "cursor": ""}
        return {"markets": [], "cursor": ""}
    p1 = run_pass(10, api, now)
    tags = {json.loads(l)["ticker"].split("-")[-1]: json.loads(l) for l in (OUT / "forward.jsonl").open() if json.loads(l)["type"] == "tag"}
    assert p1["calls"] <= 10, p1
    assert tags["AI"]["bucket"] == "LOW20" and tags["AI"]["n"] == 2 and tags["TARI"]["bucket"] == "HIGH40", tags
    assert all(t["pool_complete"] and t["pre_cancel_arm"] for t in tags.values()), tags
    with SOURCES["r4e"].open("a") as f:
        f.write(json.dumps({"type": "settle", "e": e, "markets": {f"{e}-AI": ["no", now, "finalized"], f"{e}-TARI": ["yes", now, "finalized"]}}) + "\n")
        f.write(json.dumps({"type": "fill", "ticker": f"{e}-AI", "N": 29, "A": {"through_cnt": 40.0, "any_cnt": 41.0}}) + "\n")
        f.write(json.dumps({"type": "fill", "ticker": f"{e}-TARI", "N": 29, "A": {"through_cnt": 0.0, "any_cnt": 3.0}}) + "\n")
    p2 = run_pass(10, api, now + 600)
    R = [json.loads(l) for l in (OUT / "forward.jsonl").open()]
    fills = {r["ticker"].split("-")[-1]: r["through"] for r in R if r["type"] == "fill"}
    sets = {r["ticker"].split("-")[-1]: r["result"] for r in R if r["type"] == "settle"}
    assert fills == {"AI": 29.0, "TARI": 0.0}, fills
    assert sets == {"AI": "no", "TARI": "yes"}, sets
    assert p2["tagged"] == 2 and p2["calls"] <= 10, p2
    # a fresh entry on a series whose sweeps fail is deferred, then tagged incomplete after 6 h
    with SOURCES["r4s"].open("a") as f:
        f.write(json.dumps({"type": "entry", "ticker": "KXFOOMENTION-26NOV02-X", "e": "KXFOOMENTION-26NOV02", "series": "KXFOOMENTION",
                            "open": now, "posted": True, "ts": now + 3600, "s_c": 80, "N": 25, "no_px": 0.2, "word": "X"}) + "\n")
    p3 = run_pass(0, api, now + 3700)
    assert p3["tagged"] == 2 and p3["stages"].get("deferred") == 1 and p3["calls"] == 0, p3
    p4 = run_pass(0, api, now + 3600 + DEFER_S + 1)
    t4 = [json.loads(l) for l in (OUT / "forward.jsonl").open() if json.loads(l)["type"] == "tag" and "KXFOO" in json.loads(l)["ticker"]]
    assert p4["tagged"] == 3 and t4 and t4[0]["pool_complete"] is False and t4[0]["bucket"] == "NH", (p4, t4)
    print("selftest ok:", p1, p2, p3, p4, f"{len(calls)} fake calls")


if __name__ == "__main__":
    a = sys.argv
    if "--selftest" in a:
        selftest()
    else:
        mc = int(a[a.index("--max-calls") + 1]) if "--max-calls" in a else MAX_CALLS
        print(json.dumps(run_pass(mc)))
