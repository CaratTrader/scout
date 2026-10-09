"""r7_announced_date_launch_ladder_forward_logger: forward PAPER log of the frozen announced-date launch-ladder NO rule.

Forward-only shadow (gate amendment (l)): the pattern was found post hoc in r5/r6, so it is judged on forward data only.
Frozen rule and universe: lab/kalshi/strategies/r7_announced_date_launch_ladder_forward_common.py and
data/kalshi_lab/strategies/r7_announced_date_launch_ladder_forward/preregistration.json (written before any forward look).

One pass per invocation, at most 10 Kalshi calls (R7_MAX_CALLS or --max-calls may only lower it), every call through
lab.us.data_refresh.fetch (temperature-bot idle window, 429 retries, pace 1.15 s). It never places orders.
What a pass does depends on the US/Eastern clock (the Mac runs on ET; the schedule is every 10 min, 11:00-12:50 ET):
  12:00-12:59 ET  DECIDE   every open qualifying rung with D - now in [24 h, 168 h] that has not been looked at today
                           (ET date) is quoted with GET /markets?tickers=... (<= 40 tickers per call). The FIRST look with
                           NO = 1 - yes_bid in [0.70, 0.95] is a paper taker buy of NO for $5: one GET /markets/{t}/orderbook
                           (while the pass budget lasts), fill = the worse of the look price and the book walked for $5;
                           fee = ceil-to-cent on a 10-lot of 0.07 p (1 - p). Leftover calls go to settlement checks.
  11:00-11:59 ET  MAINTAIN settlement checks (GET /markets?tickers=..., positions whose D has passed or whose market was
                           seen closed; GET /historical/markets/{t} if missing from the live tier 20 d after D); on Sundays (or when the last catalog is > 8 days old) the weekly catalog diff
                           (GET /series?category=C&include_volume=true for 10 categories, the whole pass); otherwise the
                           inventory refresh: GET /markets?series_ticker=S&status=open per candidate series, priority
                           live (an open qualifying rung; every 20 h) > warm (open markets, catalog volume moved, or new;
                           every 3 d) > cold (every 28 d, <= 5 per pass), stalest first.
  other hours     no-op (exit 0), unless --mode is given. A forced DECIDE outside 12:00-12:59 ET is a DRY run: its looks
                  and entries are flagged dry, never count, and do not change the state.
Files under data/kalshi_lab/strategies/r7_announced_date_launch_ladder_forward/ (R7_OUT overrides):
  forward.jsonl (append-only rows typed catalog / listing / rung_new / rung_closed / look / entry / settle / pass),
  state.json (inventory and bookkeeping), calls.log (every Kalshi call), .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_logger
           [--mode decide|maintain|catalog|refresh|settle] [--max-calls N] [--selftest]"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r7_announced_date_launch_ladder_forward_common as C  # noqa: E402
from lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_api import HARD_CAP, Budget, BudgetExhausted  # noqa: E402

TICKERS_PER_CALL = 40
LIVE_EVERY = 20 * C.H
WARM_EVERY = 3 * C.DAY
COLD_EVERY = 28 * C.DAY
COLD_PER_PASS = 5
NEW_SERIES_WARM_FOR = 28 * C.DAY
SETTLE_RECHECK = 6 * C.H
ARCHIVE_AFTER = 20 * C.DAY      # a position missing from the live tier this long after D is looked up in the archive
CATALOG_MAX_AGE = 8 * C.DAY
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended", "inactive"}


def out_dir() -> Path:
    from lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_api import OUT
    return Path(os.environ.get("R7_OUT", str(OUT)))


def _iso(t: float) -> str:
    return C.et_now(t).isoformat(timespec="seconds")


def _h(s: str) -> str:
    return hashlib.sha1(("r7" + s).encode()).hexdigest()


class Logger:
    def __init__(self, budget: Budget, now: float | None = None, out: Path | None = None,
                 catalog_file: Path | None = None, seeds=None):
        self.catalog_file = Path(catalog_file) if catalog_file else Path(__file__).resolve().parents[3] / "data/kalshi_lab/series_all.json"
        self.seeds = C.SEED_SERIES if seeds is None else seeds
        self.b = budget
        self.now = float(now if now is not None else time.time())
        self.out = Path(out or out_dir())
        self.out.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        self.rows: list[dict] = []
        self.notes: list[str] = []

    # ---------------------------------------------------------------- persistence
    def _load_state(self) -> dict:
        f = self.out / "state.json"
        st = json.loads(f.read_text()) if f.exists() else {}
        st.setdefault("series", {}); st.setdefault("rungs", {}); st.setdefault("catalog", {})
        st.setdefault("positions", {}); st.setdefault("created", _iso(self.now))
        return st

    def save(self) -> None:
        f = self.out / "state.json"
        tmp = tempfile.NamedTemporaryFile("w", dir=self.out, delete=False, suffix=".tmp")
        json.dump(self.state, tmp, indent=0, sort_keys=True); tmp.close()
        os.replace(tmp.name, f)
        if self.rows:
            with open(self.out / "forward.jsonl", "a") as fh:
                for r in self.rows:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            self.rows = []

    def emit(self, typ: str, **kw) -> dict:
        r = {"type": typ, "ts": int(self.now), "iso": _iso(self.now), **kw}
        self.rows.append(r)
        return r

    # ---------------------------------------------------------------- universe bootstrap
    def ensure_universe(self) -> None:
        """Seed the candidate list from the frozen seed set and the catalog on disk (0 calls)."""
        S = self.state["series"]
        for s in self.seeds:
            S.setdefault(s, {"tier": "warm", "last_list": 0, "src": "seed"})
        cat = self.state["catalog"]
        if not cat.get("candidates"):
            rows = []
            f = self.catalog_file
            try:
                rows = json.loads(f.read_text()).get("series") or []
            except Exception:  # noqa: BLE001
                rows = []
            for s in rows:
                if C.series_candidate(s):
                    S.setdefault(s["ticker"], {"tier": "cold", "last_list": 0, "src": "catalog_file"})
            cat["candidates"] = sorted(k for k in S)

    # ---------------------------------------------------------------- catalog diff (weekly)
    def catalog(self) -> None:
        cat = self.state["catalog"]
        prev_vol = cat.get("vol") or {}
        prev_all = set(cat.get("all_tickers") or [])
        vol, alltk, rows_by_t, fetched = {}, set(), {}, []
        for c in C.CATALOG_CATEGORIES:
            try:
                d = self.b.get(f"/series?category={c.replace(' ', '%20')}&include_volume=true")
            except BudgetExhausted:
                break
            if "_error" in d:
                self.notes.append(f"catalog {c}: error {str(d.get('_error', ''))[:80]}"); continue
            ss = d.get("series") or []      # {"series": null} is a valid empty category
            fetched.append(c)
            for s in ss:
                alltk.add(s["ticker"]); rows_by_t[s["ticker"]] = s
                if C.series_candidate(s):
                    vol[s["ticker"]] = C.fnum(s.get("volume_fp")) or C.fnum(s.get("volume")) or 0.0
        complete = len(fetched) >= len(C.CATALOG_CATEGORIES) - 2
        if len(fetched) < len(C.CATALOG_CATEGORIES):
            self.notes.append(f"catalog: {len(fetched)} of {len(C.CATALOG_CATEGORIES)} categories; missing ones keep their old volumes")
        S = self.state["series"]
        moved, new = [], []
        for tk, v in vol.items():
            row = S.setdefault(tk, {"tier": "cold", "last_list": 0, "src": "catalog"})
            dv = None if tk not in prev_vol else v - prev_vol[tk]
            row["dvol"] = dv
            if prev_all and tk not in prev_all:
                row["new_since"] = int(self.now); new.append(tk)
            if (dv or 0) > 0:
                moved.append(tk)
            if row.get("tier") != "live":
                recent_new = self.now - row.get("new_since", -1e18) < NEW_SERIES_WARM_FOR
                row["tier"] = "warm" if ((dv or 0) > 0 or recent_new or row.get("n_open", 0) > 0 or tk in self.seeds) else "cold"
        if complete:
            merged = dict(prev_vol); merged.update(vol)
            cat.update({"last": int(self.now), "vol": merged, "all_tickers": sorted(prev_all | alltk), "baseline_from": cat.get("last")})
        self.emit("catalog", categories=fetched, n_candidates=len(vol), new_candidates=sorted(new), vol_moved=sorted(moved),
                  had_baseline=bool(prev_vol))

    def seed_catalog_baseline(self, paths: list[Path]) -> int:
        """Use catalog responses already on disk (other researchers' caches) as the first Delta-volume baseline. 0 calls."""
        vol, alltk = {}, set()
        for p in paths:
            try:
                d = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            d = d.get("d", d) if isinstance(d, dict) else {}
            for s in d.get("series") or []:
                alltk.add(s["ticker"])
                if C.series_candidate(s):
                    vol[s["ticker"]] = C.fnum(s.get("volume_fp")) or 0.0
        cat = self.state["catalog"]
        if vol and not cat.get("vol"):
            cat.update({"vol": vol, "all_tickers": sorted(alltk), "last": 0, "baseline_note": "cached catalogs of earlier researchers"})
        return len(vol)

    # ---------------------------------------------------------------- inventory refresh
    def _due(self) -> list[str]:
        S = self.state["series"]
        due = []
        for tk, r in S.items():
            age = self.now - (r.get("last_list") or 0)
            tier = r.get("tier", "cold")
            every = LIVE_EVERY if tier == "live" else WARM_EVERY if tier == "warm" else COLD_EVERY
            if age >= every:
                due.append((0 if tier == "live" else 1 if tier == "warm" else 2, r.get("last_list") or 0, _h(tk), tk))
        due.sort()
        out, cold = [], 0
        for pri, _, _, tk in due:
            if pri == 2:
                if cold >= COLD_PER_PASS:
                    continue
                cold += 1
            out.append(tk)
        return out

    def list_series(self, tk: str) -> None:
        d = self.b.get(f"/markets?series_ticker={tk}&status=open&limit=1000")
        S = self.state["series"].setdefault(tk, {"tier": "cold", "last_list": 0, "src": "listing"})
        R = self.state["rungs"]
        if "_error" in d and not d.get("markets"):
            S["last_err"] = int(self.now); S["last_list"] = int(self.now)   # retried at the tier's next turn
            self.emit("listing", series=tk, error=str(d.get("_error"))[:200]); return
        ms = d.get("markets") or []
        reasons, q_open = {}, set()
        for m in ms:
            ok, why, D = C.qualify(m)
            reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
            if not ok or (m.get("status") or "active") in CLOSED:
                continue
            t = m["ticker"]; q_open.add(t)
            if t not in R:
                R[t] = {"s": tk, "e": m.get("event_ticker"), "D": D, "D_how": why.split(":", 1)[1], "title": (m.get("title") or "")[:160],
                        "sub": (m.get("yes_sub_title") or "")[:80], "kind": C.kind(tk, m.get("title") or "", m.get("rules_primary") or ""),
                        "first_seen": int(self.now), "status": "open", "bought": False, "looks": 0, "last_look_date": None}
                self.emit("rung_new", ticker=t, series=tk, event=m.get("event_ticker"), D=D, D_iso=_iso(D), D_how=R[t]["D_how"],
                          kind=R[t]["kind"], title=R[t]["title"], sub=R[t]["sub"], rules=(m.get("rules_primary") or "")[:300],
                          ecc=(m.get("early_close_condition") or "")[:160], open_time=m.get("open_time"), close_time=m.get("close_time"))
            R[t]["last_seen"] = int(self.now); R[t]["status"] = "open"
        for t, r in R.items():   # rungs of this series no longer in the open listing (only if the listing was complete)
            if not d.get("cursor") and r["s"] == tk and r.get("status") == "open" and t not in q_open:
                r["status"] = "closed"; r["closed_seen"] = int(self.now)
                self.emit("rung_closed", ticker=t, series=tk, bought=r.get("bought", False))
        S.update({"last_list": int(self.now), "n_open": len(ms), "n_open_q": len(q_open), "cursor": bool(d.get("cursor"))})
        if q_open:
            S["tier"] = "live"
        else:
            recent_new = self.now - S.get("new_since", -1e18) < NEW_SERIES_WARM_FOR
            S["tier"] = "warm" if (ms or (S.get("dvol") or 0) > 0 or recent_new) else "cold"
        self.emit("listing", series=tk, n_open=len(ms), n_open_qualifying=len(q_open), reasons=reasons, tier=S["tier"], cursor=bool(d.get("cursor")))

    def refresh(self) -> None:
        for tk in self._due():
            if self.b.left() <= 0:
                break
            try:
                self.list_series(tk)
            except BudgetExhausted:
                break

    # ---------------------------------------------------------------- decision looks (12:00-12:59 ET)
    def due_rungs(self) -> list[str]:
        today = C.et_date(self.now)
        out = []
        for t, r in self.state["rungs"].items():
            if r.get("status") != "open" or r.get("bought") or r.get("last_look_date") == today:
                continue
            h = r["D"] - self.now
            if C.H_MIN <= h <= C.H_MAX:
                out.append((r["D"], t))
        return [t for _, t in sorted(out)]

    def quotes(self, tickers: list[str]) -> dict[str, dict]:
        got = {}
        for i in range(0, len(tickers), TICKERS_PER_CALL):
            chunk = tickers[i:i + TICKERS_PER_CALL]
            d = self.b.get(f"/markets?tickers={','.join(chunk)}&limit=1000")
            for m in d.get("markets") or []:
                got[m["ticker"]] = m
            if "_error" in d:
                self.notes.append(f"tickers call error {str(d['_error'])[:80]}")
        return got

    def decide(self, dry: bool = False) -> None:
        R = self.state["rungs"]
        today = C.et_date(self.now)
        due = self.due_rungs()
        # reserve one call per expected signal is impossible to know in advance: quote first, books while budget lasts
        n_quote_calls = min(-(-len(due) // TICKERS_PER_CALL), max(self.b.left() - 1, 1)) if due else 0
        due = due[:n_quote_calls * TICKERS_PER_CALL]
        try:
            q = self.quotes(due) if due else {}
        except BudgetExhausted:
            q = {}
        signals = []
        for t in due:
            r = R[t]; m = q.get(t)
            if m is None:
                self.emit("look", ticker=t, series=r["s"], event=r["e"], missing=True, dry=dry); continue
            status = m.get("status") or "active"
            yb, ya = C.fnum(m.get("yes_bid_dollars")), C.fnum(m.get("yes_ask_dollars"))
            no_px = None if yb is None else round(1 - yb, 4)
            ok_now, why_now, _ = C.qualify(m)
            in_band = status not in CLOSED and no_px is not None and C.BAND_LO - 1e-9 <= no_px <= C.BAND_HI + 1e-9
            look = self.emit("look", ticker=t, series=r["s"], event=r["e"], D=r["D"], h_to_D=round((r["D"] - self.now) / C.H, 2),
                             status=status, yes_bid=yb, yes_ask=ya, no_px=no_px, yes_bid_size=C.fnum(m.get("yes_bid_size_fp")),
                             yes_ask_size=C.fnum(m.get("yes_ask_size_fp")), vol24=C.fnum(m.get("volume_24h_fp")),
                             in_band=in_band, look_no=r.get("looks", 0) + 1, qualify_now=why_now, dry=dry)
            if not dry:
                r["looks"] = r.get("looks", 0) + 1; r["last_look_date"] = today
                if status in CLOSED:
                    r["status"] = "closed"; r["closed_seen"] = int(self.now)
            if in_band:
                signals.append((t, look))
        for t, look in signals:
            self.enter(t, look, dry)

    def enter(self, t: str, look: dict, dry: bool) -> None:
        r = self.state["rungs"][t]
        sig = look["no_px"]
        book, wk = None, None
        if self.b.left() > 0:
            try:
                ob = self.b.get(f"/markets/{t}/orderbook")
                lv = C.book_yes_bids(ob)
                wk = C.walk_no(lv, C.STAKE)
                book = {"levels_top5": [[round(1 - p, 4), q] for p, q in lv[:5]], **wk}
            except BudgetExhausted:
                pass
        if wk is not None and wk["best_no"] is None:
            px, filled, note = None, False, "book has no YES bids at fill time"
        elif wk is not None:
            px = max(sig, wk["vwap"] or sig, wk["best_no"] or sig); filled = True; note = "worse of look price and book walk for $5"
        else:
            px = sig; filled = True; note = "no book call left in this pass: filled at the look price"
        cap_contracts = (wk or {}).get("best_size") if wk else look.get("yes_bid_size")
        row = {"ticker": t, "series": r["s"], "event": r["e"], "kind": r.get("kind"), "D": r["D"], "D_iso": _iso(r["D"]),
               "h_to_D": look["h_to_D"], "signal_no": sig, "yes_ask": look.get("yes_ask"), "filled": filled, "px": px,
               "fee_per_contract": C.fee_per_contract(px) if px else None,
               "contracts_5usd": round(C.STAKE / px, 2) if px else None, "cap_contracts_at_signal": cap_contracts,
               "cap_usd_at_signal": round((cap_contracts or 0) * sig, 2), "book": book, "fill_note": note, "dry": dry,
               "look_no": look["look_no"], "title": r.get("title"), "sub": r.get("sub")}
        self.emit("entry", **row)
        if not dry and filled:
            r["bought"] = True
            self.state["positions"][t] = {"e": r["e"], "s": r["s"], "D": r["D"], "px": px, "entry_ts": int(self.now),
                                          "settled": False, "last_check": 0}
        elif not dry:
            r["no_fill_dates"] = (r.get("no_fill_dates") or []) + [C.et_date(self.now)]

    # ---------------------------------------------------------------- settlement
    def settle(self) -> None:
        P = self.state["positions"]; R = self.state["rungs"]
        todo = [t for t, p in P.items() if not p.get("settled") and (self.now >= p["D"] or R.get(t, {}).get("status") == "closed")
                and self.now - (p.get("last_check") or 0) >= SETTLE_RECHECK]
        todo.sort(key=lambda t: P[t]["D"])
        for i in range(0, len(todo), TICKERS_PER_CALL):
            if self.b.left() <= 0:
                break
            chunk = todo[i:i + TICKERS_PER_CALL]
            try:
                q = self.quotes(chunk)
            except BudgetExhausted:
                break
            for t in chunk:
                p = P[t]; p["last_check"] = int(self.now)
                m = q.get(t) or {}
                if not m and self.now - p["D"] > ARCHIVE_AFTER and self.b.left() > 0:
                    try:   # settled markets move to the archive tier (~60 days): one call per missing position
                        m = (self.b.get(f"/historical/markets/{t}") or {}).get("market") or {}
                    except BudgetExhausted:
                        m = {}
                res = m.get("result")
                if m.get("status") in CLOSED and t in R:
                    R[t]["status"] = "closed"
                if res in ("yes", "no"):
                    won = res == "no"
                    p.update({"settled": True, "result": res, "won": won})
                    self.emit("settle", ticker=t, series=p["s"], event=p["e"], result=res, won=won, px=p["px"],
                              ret_per_dollar=round(C.ret_per_dollar(p["px"], won), 5), status=m.get("status"),
                              settled_after_h=round((self.now - p["entry_ts"]) / C.H, 1))

    # ---------------------------------------------------------------- pass driver
    def run(self, mode: str, dry: bool = False) -> dict:
        self.ensure_universe()
        try:
            if mode == "decide":
                self.decide(dry=dry)
                if not dry:
                    self.settle()
            elif mode == "maintain":
                self.settle()
                cat_age = self.now - (self.state["catalog"].get("last") or 0)
                if (C.et_now(self.now).weekday() == 6 and cat_age > 6 * C.DAY) or cat_age > CATALOG_MAX_AGE:
                    if self.b.left() >= len(C.CATALOG_CATEGORIES):
                        self.catalog()
                self.refresh()
            elif mode == "catalog":
                self.catalog()
            elif mode == "refresh":
                self.refresh()
            elif mode == "settle":
                self.settle()
        except BudgetExhausted:
            self.notes.append("budget exhausted")
        S = self.state["series"]
        summ = {"mode": mode, "dry": dry, "calls": self.b.used, "cap": self.b.cap, "notes": self.notes,
                "series": len(S), "live": sum(1 for r in S.values() if r.get("tier") == "live"),
                "warm": sum(1 for r in S.values() if r.get("tier") == "warm"),
                "never_listed": sum(1 for r in S.values() if not r.get("last_list")),
                "open_rungs": sum(1 for r in self.state["rungs"].values() if r.get("status") == "open"),
                "in_window_now": len([1 for r in self.state["rungs"].values() if r.get("status") == "open" and not r.get("bought")
                                      and C.H_MIN <= r["D"] - self.now <= C.H_MAX]),
                "positions": len(self.state["positions"]),
                "open_positions": sum(1 for p in self.state["positions"].values() if not p.get("settled"))}
        self.emit("pass", **summ)
        if not dry:
            self.save()
        else:   # a dry run keeps its rows (flagged) but never changes the state
            with open(self.out / "forward.jsonl", "a") as fh:
                for r in self.rows:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            self.rows = []
        return summ


def clock_mode(now: float) -> str | None:
    h = C.et_now(now).hour
    return "decide" if h == C.DECISION_HOUR_ET else "maintain" if h == C.DECISION_HOUR_ET - 1 else None


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        from lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_selftest import selftest
        return selftest()
    cap = HARD_CAP
    if "--max-calls" in argv:
        cap = min(cap, int(argv[argv.index("--max-calls") + 1]))
    cap = min(cap, int(os.environ.get("R7_MAX_CALLS", HARD_CAP)))
    now = time.time()
    forced = argv[argv.index("--mode") + 1] if "--mode" in argv else None
    mode = forced or clock_mode(now)
    if mode is None:
        print(json.dumps({"mode": None, "note": "outside 11:00-12:59 ET: no-op"})); return 0
    dry = mode == "decide" and clock_mode(now) != "decide"
    out = out_dir(); out.mkdir(parents=True, exist_ok=True)
    with open(out / ".lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"mode": mode, "note": "another pass holds the lock"})); return 0
        lg = Logger(Budget(cap, out), now, out)
        print(json.dumps(lg.run(mode, dry=dry)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
