"""r5_midterms_2026_thin_race_markets_logger: forward PAPER log for the 2026-11-03 midterm night (frozen in
data/kalshi_lab/strategies/r5_midterms_2026_thin_race_markets/preregistration.json, written before the first pass).

One pass per invocation. At most 10 Kalshi calls per pass (R5MID_MAX_CALLS may only lower it), every call through
lab.us.data_refresh.fetch (temperature-bot idle window, 429 retries), logged to forward_calls.log. Never places orders.

Universe (frozen series list in the preregistration): markets <SERIES>-26-R / <SERIES>-26-D of every House / Senate /
Governor per-race series in the 2026-10-08 catalog, found with /markets?tickers=... (160 guessed tickers per call),
plus the open KXAPCALLHOUSE / KXAPCALLSENATE ladders (descriptive only). Rebuilt at most once a day, never at night.
Snapshot: /markets?tickers=<160 universe tickers> (2-3 calls) -> yes_bid / yes_ask / last / volume / status / result.
Modes: baseline (before NIGHT_START), night (NIGHT_START..NIGHT_END), after. Signals only fire at night.
Descriptive measures per book (same definitions as the archive kill test): ref = mid of the last snapshot before
NIGHT_START (else the first night snapshot); Q trigger = yes_bid >= ref+0.25 (leader YES) or yes_ask <= ref-0.25
(leader NO); M trigger = |mid-ref| >= 0.25; then the first pass with leader ask >= 0.95 / mid >= 0.95 / bid >= 0.95.
Cross-book stale minute: one book's committed quote says the race is decided at >= 0.90 while the sibling still
offers the same leader at <= 0.85.
Paper rules (taker; decide on pass k's snapshot, fill at pass k+1's quote if pass k+1 is <= 300 s later and the quote
is <= the cap; one fill per event and rule; 10 contracts; fee 0.07 p (1-p) rounded up to the cent per order):
  F1 cross-book (archive C1, X=0.85 cap=0.90): book A yes_bid >= 0.85 (A's candidate leads) -> buy NO on the sibling
     at 1 - yes_bid; book A yes_ask <= 0.15 (A trails) -> buy YES on the sibling at yes_ask.
  F2 own-book leader (archive C2, Q trigger, cap 0.95): buy the leader at its offer on the pass after the trigger.
  F3 contrarian (archive C3, M trigger, cap 0.30): buy the trailer at its offer on the pass after the trigger.
No maker rule (taker-only family). Depth probes: /markets/{t}/orderbook for new signals (<= 2 per pass); in baseline
mode one rotating race book per pass for a capacity baseline.
Files: forward.jsonl (rows snap / signal / fill / nofill / probe / trigger / settle / pass), forward_state.json, .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_midterms_2026_thin_race_markets_logger [--max-calls N] [--selftest]"""
from __future__ import annotations
import datetime as dt, fcntl, json, math, os, sys, tempfile, time
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path(os.environ.get("R5MID_OUT", "data/kalshi_lab/strategies/r5_midterms_2026_thin_race_markets"))
ET = ZoneInfo("America/New_York")

# ------------------------------------------------------------------------------------------------ frozen constants
NIGHT_START = int(dt.datetime(2026, 11, 3, 17, 0, tzinfo=ET).timestamp())
NIGHT_END = int(dt.datetime(2026, 11, 4, 14, 0, tzinfo=ET).timestamp())
MAX_CALLS = 10
CHUNK = 160
UNIVERSE_TTL_S = 86400
MOVE = 0.25
F1_X, F1_CAP = 0.85, 0.90
F2_CAP = 0.95
F3_CAP = 0.30
FILL_MAX_GAP_S = 300
N_CONTRACTS = 10
FULL_SNAP_EVERY_S = 1800
STALE_SIG, STALE_OFF = 0.90, 0.85
AP_SERIES = ["KXAPCALLHOUSE", "KXAPCALLSENATE"]
FROZEN = {"NIGHT_START": NIGHT_START, "NIGHT_END": NIGHT_END, "MAX_CALLS": MAX_CALLS, "MOVE": MOVE, "F1_X": F1_X, "F1_CAP": F1_CAP,
          "F2_CAP": F2_CAP, "F3_CAP": F3_CAP, "FILL_MAX_GAP_S": FILL_MAX_GAP_S, "N_CONTRACTS": N_CONTRACTS,
          "STALE_SIG": STALE_SIG, "STALE_OFF": STALE_OFF, "AP_SERIES": AP_SERIES}
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended"}


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fee_pc(p: float, n: int = N_CONTRACTS) -> float:
    """Taker fee per contract for an n-contract order at p, rounded up to the cent per order."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def mode_of(now: float) -> str:
    return "baseline" if now < NIGHT_START else ("night" if now <= NIGHT_END else "after")


# ------------------------------------------------------------------------------------------------ I/O
class BudgetExhausted(RuntimeError):
    pass


class Budget:
    """Live GETs through the bot-idle fetch, counted per pass and logged to forward_calls.log."""
    def __init__(self, cap: int, out: Path) -> None:
        self.cap = cap; self.used = 0; self.log = out / "forward_calls.log"

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        from lab.us.data_refresh import fetch, K
        self.used += 1; t0 = time.time()
        txt = fetch(K + path, pace=1.15, tries=3)
        with self.log.open("a") as f:
            f.write(f"{int(t0)}\t{len(txt)}\tlogger\t{path[:300]}\n")
        try:
            return json.loads(txt) if txt else {}
        except ValueError:
            return {}


def atomic_write(p: Path, obj) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------------------------------------ logger
class Logger:
    def __init__(self, out: Path, budget, now: float | None = None, prereg: dict | None = None) -> None:
        self.out = out; self.B = budget; self.live = now is None; self.now = now or time.time()
        self.rows: list[dict] = []; self.errors: list[str] = []
        pr = prereg if prereg is not None else json.loads((out / "preregistration.json").read_text())
        if pr.get("frozen") != json.loads(json.dumps(FROZEN)):
            raise SystemExit("preregistration.frozen does not match the logger constants: refusing to run")
        self.series = pr["universe"]["series"]
        sf = out / "forward_state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        s = self.st
        s.setdefault("version", 1); s.setdefault("started", self.now)
        for k in ("universe", "last", "ref", "books", "pending", "fills", "done", "stale"):
            s.setdefault(k, {})
        s.setdefault("universe_ts", 0); s.setdefault("last_full", 0); s.setdefault("probe_i", 0); s.setdefault("prev_ts", None)

    def row(self, typ: str, **kw) -> None:
        r = {"type": typ, "ts": round(kw.pop("ts", None) or self.now, 1)}
        r.update(kw); self.rows.append(r)

    # ---------------------------------------------------------------- universe
    def build_universe(self) -> None:
        tick = [f"{s}-26-{p}" for s in self.series for p in ("R", "D")]
        found = {}
        for i in range(0, len(tick), CHUNK):
            d = self.B.get(f"/markets?tickers={','.join(tick[i:i + CHUNK])}&limit=1000")
            for m in d.get("markets") or []:
                found[m["ticker"]] = {"e": m["event_ticker"], "sub": (m.get("yes_sub_title") or "")[:60], "kind": "race"}
        for s in AP_SERIES:
            d = self.B.get(f"/markets?series_ticker={s}&status=open&limit=1000")
            for m in d.get("markets") or []:
                found[m["ticker"]] = {"e": m["event_ticker"], "sub": (m.get("yes_sub_title") or "")[:60], "kind": "apcall"}
        if found:
            # never drop a market that is already in the universe (it may be closed / settled now)
            self.st["universe"].update(found); self.st["universe_ts"] = self.now
        self.row("universe", n=len(self.st["universe"]), new=len(set(found) - set(self.st["last"])),
                 races=sum(1 for v in self.st["universe"].values() if v["kind"] == "race"))

    # ---------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        u = sorted(self.st["universe"]); q = {}
        for i in range(0, len(u), CHUNK):
            d = self.B.get(f"/markets?tickers={','.join(u[i:i + CHUNK])}&limit=1000")
            for m in d.get("markets") or []:
                q[m["ticker"]] = [fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")), fnum(m.get("last_price_dollars")),
                                  fnum(m.get("volume_fp")), m.get("status"), m.get("result") or None]
        missing = [t for t in u if t not in q]
        if missing and self.open_positions(missing):
            # archived markets move to /historical: one call for the open positions only
            want = [t for t in missing if self.open_positions([t])][:CHUNK]
            try:
                d = self.B.get(f"/historical/markets?tickers={','.join(want)}&limit=1000")
                for m in d.get("markets") or []:
                    q[m["ticker"]] = [fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")), fnum(m.get("last_price_dollars")),
                                      fnum(m.get("volume_fp")), m.get("status"), m.get("result") or None]
            except BudgetExhausted:
                pass
        return q

    def open_positions(self, tickers) -> bool:
        ts = set(tickers)
        return any(f["book"] in ts and f.get("settled") is None for f in self.st["fills"].values())

    def log_snap(self, q: dict) -> None:
        last = self.st["last"]; full = self.now - self.st["last_full"] >= FULL_SNAP_EVERY_S
        ch = {t: v for t, v in q.items() if full or last.get(t) != v}
        self.row("snap", full=full, n=len(q), q=ch)
        if full:
            self.st["last_full"] = self.now
        last.update(q)

    # ---------------------------------------------------------------- night logic (0 calls)
    @staticmethod
    def two_sided(v) -> tuple[float, float] | None:
        if not v or v[0] is None or v[1] is None:
            return None
        bid, ask = v[0], v[1]
        ask = 1.0 if ask <= 0 else ask
        return bid, ask

    def events(self, q: dict) -> dict:
        ev = {}
        for t, v in q.items():
            u = self.st["universe"].get(t)
            if u and u["kind"] == "race" and (t.endswith("-R") or t.endswith("-D")):
                ev.setdefault(u["e"], {})[t[-1]] = t
        return {e: d for e, d in ev.items() if "R" in d and "D" in d}

    def set_refs(self, q: dict) -> None:
        ref = self.st["ref"]
        for t, v in q.items():
            if t in ref:
                continue
            p = self.two_sided(v)
            if p is not None and (v[4] or "") not in CLOSED:
                ref[t] = {"mid": (p[0] + p[1]) / 2, "ts": self.now, "src": "night_first" if self.now >= NIGHT_START else "baseline"}

    def fill_pending(self, q: dict) -> None:
        """Pending decisions from the previous pass are filled (or not) at this pass's quote."""
        for key, p in list(self.st["pending"].items()):
            del self.st["pending"][key]
            rule, e = p["rule"], p["e"]
            if f"{rule}|{e}" in self.st["done"]:
                continue
            gap = self.now - p["ts"]
            pq = self.two_sided(q.get(p["book"]))
            if gap > FILL_MAX_GAP_S or pq is None or (q[p["book"]][4] or "") in CLOSED:
                self.row("nofill", rule=rule, e=e, book=p["book"], why="gap" if gap > FILL_MAX_GAP_S else "no_quote", gap=round(gap)); continue
            bid, ask = pq
            px = ask if p["side"] == "YES" else 1 - bid
            if not (0.01 <= px <= p["cap"]):
                self.row("nofill", rule=rule, e=e, book=p["book"], why="above_cap", px=round(px, 4), gap=round(gap)); continue
            fid = f"{rule}|{e}"
            self.st["fills"][fid] = {"rule": rule, "e": e, "book": p["book"], "side": p["side"], "px": round(px, 4),
                                     "fee": fee_pc(px), "ts": self.now, "t_sig": p["ts"], "settled": None, "why": p["why"]}
            self.st["done"][fid] = self.now
            self.row("fill", **{k: v for k, v in self.st["fills"][fid].items() if k != "settled"}, gap=round(gap))

    def add_pending(self, rule: str, e: str, book: str, side: str, cap: float, why: dict) -> None:
        if f"{rule}|{e}" in self.st["done"] or any(p["rule"] == rule and p["e"] == e for p in self.st["pending"].values()):
            return
        key = f"{rule}|{e}|{int(self.now)}"
        self.st["pending"][key] = {"rule": rule, "e": e, "book": book, "side": side, "cap": cap, "ts": self.now, "why": why}
        self.row("signal", rule=rule, e=e, book=book, side=side, cap=cap, why=why)

    def night(self, q: dict) -> list[str]:
        new_sig_books = []
        books = self.st["books"]; ref = self.st["ref"]
        for t, v in q.items():
            u = self.st["universe"].get(t)
            p = self.two_sided(v)
            if not u or p is None or t not in ref:
                continue
            bid, ask = p; mid = (bid + ask) / 2; r = ref[t]["mid"]
            b = books.setdefault(t, {})
            for trig in ("Q", "M"):
                d = b.get(trig)
                if d is None:
                    lead = None
                    if trig == "Q":
                        if bid >= r + MOVE: lead = True
                        elif ask <= r - MOVE: lead = False
                    elif abs(mid - r) >= MOVE:
                        lead = mid > r
                    if lead is None:
                        continue
                    d = b[trig] = {"t0": self.now, "lead_yes": lead, "ask95": None, "mid95": None, "bid95": None}
                    la = ask if lead else 1 - bid
                    self.row("trigger", book=t, trig=trig, lead_yes=lead, ref=round(r, 4), bid=bid, ask=ask, leader_ask=round(la, 4))
                    if u["kind"] == "race":
                        if trig == "Q":
                            self.add_pending("F2", u["e"], t, "YES" if lead else "NO", F2_CAP, {"trig": "Q", "t0": self.now})
                        else:
                            self.add_pending("F3", u["e"], t, "NO" if lead else "YES", F3_CAP, {"trig": "M", "t0": self.now})
                        new_sig_books.append(t)
                lb, la = (bid, ask) if d["lead_yes"] else (1 - ask, 1 - bid)
                for k, val in (("ask95", la), ("mid95", (la + lb) / 2), ("bid95", lb)):
                    if d[k] is None and val >= 0.95:
                        d[k] = self.now
        # cross-book F1 and stale-minute bookkeeping
        for e, d in self.events(q).items():
            for a, b in ((d["R"], d["D"]), (d["D"], d["R"])):
                pa, pb = self.two_sided(q[a]), self.two_sided(q[b])
                if pa is None or pb is None or (q[a][4] or "") in CLOSED or (q[b][4] or "") in CLOSED:
                    continue
                if pa[0] >= F1_X:
                    self.add_pending("F1", e, b, "NO", F1_CAP, {"sig_book": a, "sig_bid": pa[0], "sig_ask": pa[1]}); new_sig_books.append(b)
                elif pa[1] <= 1 - F1_X:
                    self.add_pending("F1", e, b, "YES", F1_CAP, {"sig_book": a, "sig_bid": pa[0], "sig_ask": pa[1]}); new_sig_books.append(b)
                if (pa[0] >= STALE_SIG and 1 - pb[0] <= STALE_OFF) or (pa[1] <= 1 - STALE_SIG and pb[1] <= STALE_OFF):
                    s = self.st["stale"].setdefault(e, [])
                    if not s or s[-1] != int(self.now):
                        s.append(int(self.now))
                    self.row("stale", e=e, sig_book=a, book=b, a_q=list(pa), b_q=list(pb))
        return new_sig_books

    def settle(self, q: dict) -> None:
        for fid, f in self.st["fills"].items():
            if f.get("settled") is not None:
                continue
            v = q.get(f["book"])
            if not v or v[5] not in ("yes", "no"):
                continue
            win = (v[5] == "yes") == (f["side"] == "YES")
            pnl = (1.0 if win else 0.0) - f["px"] - f["fee"]
            f["settled"] = {"result": v[5], "win": win, "pnl": round(pnl, 4), "ret": round(pnl / f["px"], 4), "ts": self.now}
            self.row("settle", fid=fid, rule=f["rule"], e=f["e"], book=f["book"], side=f["side"], px=f["px"], **f["settled"])

    def probe(self, t: str, why: str) -> None:
        d = self.B.get(f"/markets/{t}/orderbook")
        fp = d.get("orderbook_fp") or {}
        yes = [[fnum(p), fnum(s)] for p, s in (fp.get("yes_dollars") or [])]
        no = [[fnum(p), fnum(s)] for p, s in (fp.get("no_dollars") or [])]
        if not fp:
            o = d.get("orderbook") or {}
            yes = [[p / 100, s] for p, s in (o.get("yes") or [])]; no = [[p / 100, s] for p, s in (o.get("no") or [])]
        # bids only on Kalshi: a YES offer at a is a NO bid at 1-a. Contracts offered at <= cap:
        yes_off = lambda cap: sum(s for p, s in no if p is not None and 1 - p <= cap + 1e-9)
        no_off = lambda cap: sum(s for p, s in yes if p is not None and 1 - p <= cap + 1e-9)
        self.row("probe", book=t, why=why, yes_bids=sorted(yes, reverse=True)[:5], no_bids=sorted(no, reverse=True)[:5],
                 yes_offered_le90=yes_off(0.90), no_offered_le90=no_off(0.90), yes_offered_le95=yes_off(0.95), no_offered_le95=no_off(0.95))

    # ---------------------------------------------------------------- pass
    def run(self) -> None:
        mode = mode_of(self.now)
        try:
            if not self.st["universe"] or (mode != "night" and self.now - self.st["universe_ts"] > UNIVERSE_TTL_S):
                self.build_universe()
            q = self.snapshot()
            if self.live:
                self.now = time.time()          # quote time = when the snapshot arrived (after the bot-idle wait)
                mode = mode_of(self.now)
            self.log_snap(q)
            if mode == "baseline":
                # the reference is the LAST pre-night snapshot: overwrite until the night starts
                for t, v in q.items():
                    p = self.two_sided(v)
                    if p is not None and (v[4] or "") not in CLOSED:
                        self.st["ref"][t] = {"mid": (p[0] + p[1]) / 2, "ts": self.now, "src": "baseline"}
            self.fill_pending(q)
            new = []
            if mode == "night":
                self.set_refs(q)
                new = self.night(q)
            self.settle(q)
            for t in list(dict.fromkeys(new))[:2]:
                if self.B.used < self.B.cap:
                    self.probe(t, "signal")
            if mode == "baseline" and self.B.used < self.B.cap:
                races = sorted(t for t, u in self.st["universe"].items() if u["kind"] == "race" and t in q)
                if races:
                    t = races[self.st["probe_i"] % len(races)]; self.st["probe_i"] += 1
                    self.probe(t, "baseline")
        except BudgetExhausted as e:
            self.errors.append("budget:" + str(e)[:80])
        self.st["prev_ts"] = self.now
        self.row("pass", mode=mode, calls=self.B.used, universe=len(self.st["universe"]), pending=len(self.st["pending"]),
                 fills=len(self.st["fills"]), settled=sum(1 for f in self.st["fills"].values() if f.get("settled")), errors=self.errors)

    def flush(self) -> None:
        with (self.out / "forward.jsonl").open("a") as f:
            for r in self.rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        atomic_write(self.out / "forward_state.json", self.st)


# ------------------------------------------------------------------------------------------------ selftest (0 calls)
class FakeBudget:
    def __init__(self, script):
        self.script = script; self.used = 0; self.cap = 10

    def get(self, path):
        self.used += 1
        return self.script(path)


def selftest() -> None:
    d = Path(tempfile.mkdtemp(prefix="r5mid_"))
    pr = {"frozen": json.loads(json.dumps(FROZEN)), "universe": {"series": ["HOUSEXX1"]}}
    quotes = {}
    def mk(t, bid, ask, status="active", result=None):
        return {"ticker": t, "event_ticker": "HOUSEXX1-26", "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
                "last_price_dollars": str(bid), "volume_fp": "100", "status": status, "result": result or "", "yes_sub_title": t[-1]}
    def script(path):
        if path.startswith("/markets?tickers=") or path.startswith("/historical"):
            return {"markets": [mk(t, *v) for t, v in quotes.items()]}
        if path.startswith("/markets?series_ticker="):
            return {"markets": []}
        if "/orderbook" in path:
            return {"orderbook_fp": {"yes_dollars": [["0.40", "12"]], "no_dollars": [["0.12", "30"]]}}
        return {}
    t = NIGHT_START - 600
    quotes.update({"HOUSEXX1-26-R": (0.48, 0.52), "HOUSEXX1-26-D": (0.47, 0.51)})
    L = Logger(d, FakeBudget(script), t, pr); L.run(); L.flush()
    # night: R book jumps (bid 0.88), D book still bids 0.40 -> F1 buys NO on D next pass at 1-0.40 = 0.60
    t = NIGHT_START + 600
    quotes.update({"HOUSEXX1-26-R": (0.88, 0.92), "HOUSEXX1-26-D": (0.40, 0.45)})
    L = Logger(d, FakeBudget(script), t, pr); L.run(); L.flush()
    t += 60
    quotes.update({"HOUSEXX1-26-R": (0.93, 0.94), "HOUSEXX1-26-D": (0.12, 0.20)})
    L = Logger(d, FakeBudget(script), t, pr); L.run(); L.flush()
    t = NIGHT_END + 86400 * 30
    quotes.update({"HOUSEXX1-26-R": (0.0, 1.0, "finalized", "yes"), "HOUSEXX1-26-D": (0.0, 1.0, "finalized", "no")})
    L = Logger(d, FakeBudget(script), t, pr); L.run(); L.flush()
    rows = [json.loads(l) for l in (d / "forward.jsonl").open()]
    fills = [r for r in rows if r["type"] == "fill"]; settles = [r for r in rows if r["type"] == "settle"]
    f1 = [r for r in fills if r["rule"] == "F1"]
    assert f1 and f1[0]["book"] == "HOUSEXX1-26-D" and f1[0]["side"] == "NO" and abs(f1[0]["px"] - 0.88) < 1e-9, f1
    assert all(s["win"] for s in settles if s["rule"] == "F1"), settles
    f2 = [r for r in fills if r["rule"] == "F2"]
    assert f2 and f2[0]["px"] <= F2_CAP
    trig = [r for r in rows if r["type"] == "trigger"]
    assert any(r["trig"] == "Q" and r["book"] == "HOUSEXX1-26-R" and r["lead_yes"] for r in trig)
    print("selftest ok:", len(rows), "rows;", [(r["rule"], r["book"], r["side"], r["px"]) for r in fills], [(s["rule"], s["win"], s["ret"]) for s in settles])


# ------------------------------------------------------------------------------------------------ main
def main() -> None:
    if "--selftest" in sys.argv:
        selftest(); return
    cap = MAX_CALLS
    if os.environ.get("R5MID_MAX_CALLS"):
        cap = min(cap, int(os.environ["R5MID_MAX_CALLS"]))
    if "--max-calls" in sys.argv:
        cap = min(cap, int(sys.argv[sys.argv.index("--max-calls") + 1]))
    OUT.mkdir(parents=True, exist_ok=True)
    if not (OUT / "preregistration.json").exists():
        raise SystemExit("no preregistration.json: refusing to run")
    with (OUT / ".lock").open("w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another pass is running"); return
        L = Logger(OUT, Budget(cap, OUT))
        L.run(); L.flush()
        p = L.rows[-1]
        print(f"pass {mode_of(L.now)} calls {p['calls']} universe {p['universe']} fills {p['fills']} settled {p['settled']} errors {p['errors']}")


if __name__ == "__main__":
    main()
