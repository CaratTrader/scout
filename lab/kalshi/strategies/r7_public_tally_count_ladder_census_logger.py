"""r7_public_tally_count_ladder_census - FORWARD-ONLY shadow logger (gate amendment (l)); frozen before any forward data.

Series: KXEOWEEK (weekly executive orders, settles on the Federal Register signing date), KXTRUMPNOMNUM (weekly
nominations, settles on the White House Presidential Actions page), KXTORNADO (monthly SPC preliminary tornado count).
KXTRUTHSOCIAL has its own r6 logger and is not duplicated here.

One pass per invocation (schedule: every 10 minutes; a pass with nothing due makes no calls at all):
  1. Due windows: phase 'c1' = the last 14 h of a count window [W_end - 14 h, W_end) (the r6 C1 clock: Sat 10:00 ET ..
     Sat 23:59 ET for the weekly series; for KXTORNADO 10Z..24Z on the last UTC day of the month); phase 'post' =
     [W_end, W_end + 72 h) while markets are still open, throttled to one pass per 30 min (diagnostic only).
  2. Free keyless tallies (no Kalshi calls): White House presidential-actions RSS (EO posts, nomination posts),
     Federal Register documents API (EO signing dates) + public-inspection API (audit), SPC rough-log JSON.
  3. Kalshi: ONE call per due series, GET /markets?series_ticker=S&status=open (<= 3 calls; cap 10 per pass incl.
     settlement), through lab.us.data_refresh.fetch (bot-idle window, 429 retries), logged to forward_calls.log.
  4. Frozen count model (frozen_model.json) -> P(YES) per market; signals per the frozen rules below.
  5. Paper fill, conservative: taker at this pass's logged executable quote (YES at yes_ask, NO at 1 - yes_bid);
     fee = ceil-to-cent of 0.07 p (1 - p) on a 10-contract order; capacity = top-of-book size at that price.
Rules (preregistration.json): C1 PRIMARY = NO when P_model(NO) - NO price >= 0.10, NO price in [0.10, 0.90], phase c1.
Diagnostics only: C2 both sides theta 0.15; C3 YES theta 0.20 (phase c1); P1 both sides theta 0.10 (phase post).
One paper position per (rule, market, side): the first signal. Forced passes (--force) are tests: logged with
"forced": true and never create positions.

Usage: .venv/bin/python -m lab.kalshi.strategies.r7_public_tally_count_ladder_census_logger [pass|settle|status] [--force]
Outputs: data/kalshi_lab/strategies/r7_public_tally_count_ladder_census/{forward.jsonl, signals_forward.jsonl, settled.jsonl}"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_data import (OUT, wh_items_since, wh_eo, wh_nominations,  # noqa: E402
                                                                            fr_eos, fr_public_inspection_eos, spc_year)
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_model import (Frozen, p_yes, strike_of,  # noqa: E402
                                                                             conv_day_start)

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ("KXEOWEEK", "KXTRUMPNOMNUM", "KXTORNADO")
C1_H = 14            # hours before the window end at which phase c1 starts
POST_H = 72          # post-window diagnostic horizon
POST_EVERY_S = 1800  # post-window throttle
PER_PASS_CAP = 10
PX_LO, PX_HI = 0.10, 0.90
RULES = {"C1": {"phase": "c1", "side": "NO", "theta": 0.10, "primary": True},
         "C2": {"phase": "c1", "side": None, "theta": 0.15, "primary": False},
         "C3": {"phase": "c1", "side": "YES", "theta": 0.20, "primary": False},
         "P1": {"phase": "post", "side": None, "theta": 0.10, "primary": False}}
FWD, SIG, SET, CLOG, STATE = (OUT / "forward.jsonl", OUT / "signals_forward.jsonl", OUT / "settled.jsonl",
                              OUT / "forward_calls.log", OUT / "logger_state.json")
MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def fee10(p: float) -> float:
    """per-contract fee of a 10-contract taker order; Kalshi rounds the order fee up to the cent"""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- windows
def week_bounds(t: int) -> tuple[int, int]:
    lt = dt.datetime.fromtimestamp(t, ET)
    sun = (lt - dt.timedelta(days=(lt.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(sun.timestamp()), int((sun + dt.timedelta(days=7)).timestamp())


def month_bounds(y: int, m: int) -> tuple[int, int]:
    a = dt.datetime(y, m, 1, tzinfo=UTC); b = dt.datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return int(a.timestamp()), int(b.timestamp())


def candidate_windows(series: str, t: int) -> list[tuple[int, int]]:
    if series == "KXTORNADO":
        u = dt.datetime.fromtimestamp(t, UTC); prev = (u.replace(day=1) - dt.timedelta(days=1))
        return [month_bounds(u.year, u.month), month_bounds(prev.year, prev.month)]
    A, B = week_bounds(t)
    return [(A, B), (A - 7 * 86400, A)]


def phase(t: int, B: int) -> str | None:
    if B - C1_H * 3600 <= t < B:
        return "c1"
    if B <= t < B + POST_H * 3600:
        return "post"
    return None


def market_window(series: str, m: dict) -> tuple[int, int] | None:
    """Count window of a market from its rules text (weekly: 'during|from D1 to|through D2'; tornado: event ticker month)."""
    if series == "KXTORNADO":
        mm = re.search(r"-(\d{2})([A-Z]{3})$", m.get("event_ticker") or "")
        return month_bounds(2000 + int(mm.group(1)), MON[mm.group(2)]) if mm else None
    mm = re.search(r"(?:during|from|between) (\w+ \d+, \d{4}) (?:to|through) (\w+ \d+, \d{4})", m.get("rules_primary") or "")
    if not mm:
        return None
    a = dt.datetime.strptime(mm.group(1), "%b %d, %Y").replace(tzinfo=ET)
    b = dt.datetime.strptime(mm.group(2), "%b %d, %Y").replace(tzinfo=ET) + dt.timedelta(days=1)
    return int(a.timestamp()), int(b.timestamp())


# ---------------------------------------------------------------- tallies
def tally_state(series: str, A: int, B: int, t: int, T: dict) -> dict:
    """What a poller sees at t for the window [A, B)."""
    if series == "KXEOWEEK":
        a_d = dt.datetime.fromtimestamp(A, ET).date().isoformat(); b_d = dt.datetime.fromtimestamp(B, ET).date().isoformat()
        wh = [x for x in wh_eo(T["wh"]) if a_d <= x["date_et"] < b_d and x["ts"] <= t]
        fr = [x for x in T["fr"] if x.get("signing_date") and a_d <= x["signing_date"] < b_d]
        days = {x["date_et"] for x in wh} | {x["signing_date"] for x in fr}
        return {"c_seen": max(len(wh), len(fr)), "b_seen": len(days), "fr_count": len(fr), "wh_count": len(wh),
                "wh_titles": [x["title"][:80] for x in wh], "fr_eos": [x["eo"] for x in fr],
                "pi_eos": [x["eo"] for x in T.get("pi", [])]}
    if series == "KXTRUMPNOMNUM":
        a_d = dt.datetime.fromtimestamp(A, ET).date().isoformat(); b_d = dt.datetime.fromtimestamp(B, ET).date().isoformat()
        posts = [x for x in wh_nominations(T["wh"]) if a_d <= x["date_et"] < b_d and x["ts"] <= t]
        return {"c_seen": sum(x["n_nominations"] for x in posts), "b_seen": len(posts),
                "posts": [(x["date_et"], x["n_nominations"]) for x in posts]}
    u = dt.datetime.fromtimestamp(A, UTC)
    d = T["spc"].get(u.year) or {}
    c_month = int((d.get("month") or {}).get(str(u.month), {}).get("torn", 0))
    cd = conv_day_start(t)
    dd = T["spc"].get(cd.year) or {}
    c_today = int((dd.get("daily") or {}).get(cd.strftime("%m%d"), {}).get("torn", 0)) if A <= int(cd.timestamp()) < B else 0
    return {"c_seen": c_month, "c_today": c_today, "conv_day": cd.strftime("%Y-%m-%d"),
            "spc_updated": d.get("_fetched")}


def final_pmf(series: str, A: int, B: int, t: int, S: dict, FZ: Frozen):
    if series == "KXTORNADO":
        return FZ.tornado_pmf(t, B, S["c_seen"], S["c_today"])
    return FZ.weekly_pmf(series, A, B, t, S["c_seen"], S["b_seen"], fr_floor=S.get("fr_count", 0))


def signals_for(row: dict, ph: str) -> list[dict]:
    out = []
    for b in row["markets"]:
        p, ask, bid = b["model"], b["ask"], b["bid"]
        if p is None or ask is None or bid is None:
            continue
        for name, r in RULES.items():
            if r["phase"] != ph:
                continue
            for side, px, pw, size in (("YES", ask, p, b["ask_size"]), ("NO", round(1 - bid, 4), 1 - p, b["bid_size"])):
                if r["side"] and side != r["side"]:
                    continue
                if PX_LO <= px <= PX_HI and pw - px >= r["theta"]:
                    out.append({"rule": name, "primary": r["primary"], "t": b["t"], "event": b["event"], "side": side, "px": px,
                                "model": round(pw, 4), "edge": round(pw - px, 4), "fee": fee10(px), "size_at_px": size})
    return out


def evaluate(series: str, markets: list[dict], t: int, T: dict, FZ: Frozen, force: bool = False) -> list[dict]:
    """One forward row per (series, count window) that is due (or every open window when forced)."""
    by_w = {}
    for m in markets:
        w = market_window(series, m)
        if w:
            by_w.setdefault(w, []).append(m)
    rows = []
    for (A, B), ms in sorted(by_w.items()):
        ph = phase(t, B)
        if ph is None and not force:
            continue
        S = tally_state(series, A, B, t, T)
        pmf, info = final_pmf(series, A, B, t, S, FZ)
        row = {"series": series, "ts": t, "iso": dt.datetime.fromtimestamp(t, ET).isoformat(timespec="seconds"),
               "A": A, "B": B, "phase": ph or "idle", "h_to_window_end": round((B - t) / 3600, 2), "tally": S, "model_info": info,
               "markets": [], "signals": [], "forced": force}
        for m in ms:
            stype, fl, cap = strike_of(m)
            pv = p_yes(pmf, stype, fl, cap) if stype else None
            row["markets"].append({"t": m["ticker"], "event": m.get("event_ticker"), "sub": m.get("yes_sub_title"),
                                   "strike": [stype, fl, cap], "bid": _f(m.get("yes_bid_dollars")), "ask": _f(m.get("yes_ask_dollars")),
                                   "bid_size": _f(m.get("yes_bid_size_fp")), "ask_size": _f(m.get("yes_ask_size_fp")),
                                   "close": m.get("close_time"), "model": None if pv is None else round(pv, 4)})
        if ph:
            row["signals"] = signals_for(row, ph)
        rows.append(row)
    return rows


# ---------------------------------------------------------------- Kalshi (counted)
class Budget:
    def __init__(self, cap: int = PER_PASS_CAP):
        self.cap, self.n = cap, 0

    def get(self, url: str, tag: str) -> dict:
        if self.n >= self.cap:
            raise RuntimeError("per-pass Kalshi call cap reached")
        from lab.us.data_refresh import fetch
        txt = fetch(url, pace=1.15); self.n += 1
        OUT.mkdir(parents=True, exist_ok=True)
        with CLOG.open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{tag}] {len(txt or '')} {url}\n")
        return json.loads(txt) if txt else {}


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def due_series(t: int, force: bool) -> list[str]:
    st_ = _state(); out = []
    for s in SERIES:
        phs = [phase(t, B) for _, B in candidate_windows(s, t)]
        if force or "c1" in phs:
            out.append(s)
        elif "post" in phs and t - st_.get(f"last_post_{s}", 0) >= POST_EVERY_S:
            out.append(s)
    return out


def gather_tallies(series: list[str], t: int) -> dict:
    T = {"wh": [], "fr": [], "pi": [], "spc": {}}
    if {"KXEOWEEK", "KXTRUMPNOMNUM"} & set(series):
        A_min = min(candidate_windows("KXEOWEEK", t)[1][0], candidate_windows("KXTRUMPNOMNUM", t)[1][0])
        T["wh"] = wh_items_since(A_min)
    if "KXEOWEEK" in series:
        since = dt.datetime.fromtimestamp(candidate_windows("KXEOWEEK", t)[1][0], ET).date().isoformat()
        T["fr"] = fr_eos(since)
        try:
            T["pi"] = fr_public_inspection_eos()
        except RuntimeError:
            T["pi"] = []
    if "KXTORNADO" in series:
        u = dt.datetime.fromtimestamp(t, UTC)
        for y in sorted({u.year, (u - dt.timedelta(days=31)).year}):
            d = spc_year(y); d["_fetched"] = t; T["spc"][y] = d
    return T


def one_pass(force: bool = False, now: int | None = None) -> list[dict] | None:
    t = int(now or time.time())
    due = due_series(t, force)
    if not due:
        return None
    T = gather_tallies(due, t)
    FZ = Frozen.load(OUT / "frozen_model.json")
    bud = Budget(); rows = []
    for s in due:
        d = bud.get(f"{KBASE}/markets?series_ticker={s}&status=open&limit=200", "pass")
        rows += evaluate(s, d.get("markets") or [], int(time.time()), T, FZ, force=force)
    with FWD.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    st_ = _state()
    for r in rows:
        if r["phase"] == "post":
            st_[f"last_post_{r['series']}"] = r["ts"]
    STATE.write_text(json.dumps(st_))
    if not force:
        have = set()
        if SIG.exists():
            have = {(x["rule"], x["t"], x["side"]) for x in map(json.loads, SIG.open())}
        with SIG.open("a") as fh:
            for r in rows:
                for s in r["signals"]:
                    k = (s["rule"], s["t"], s["side"])
                    if k not in have:
                        have.add(k)
                        fh.write(json.dumps({**s, "series": r["series"], "ts": r["ts"], "iso": r["iso"], "phase": r["phase"],
                                             "h_to_window_end": r["h_to_window_end"], "c_seen": r["tally"]["c_seen"]}) + "\n")
    return rows


def settle(max_calls: int = PER_PASS_CAP) -> list[dict]:
    if not SIG.exists():
        return []
    done = {(x["rule"], x["t"], x["side"]) for x in map(json.loads, SET.open())} if SET.exists() else set()
    todo = [x for x in map(json.loads, SIG.open()) if (x["rule"], x["t"], x["side"]) not in done]
    bud = Budget(max_calls); out = []
    for ev in sorted({x["event"] for x in todo if x.get("event")}):
        if bud.n >= bud.cap:
            break
        d = bud.get(f"{KBASE}/markets?event_ticker={ev}&limit=200", "settle")
        res = {m["ticker"]: m.get("result") for m in d.get("markets") or []}
        for x in todo:
            r = res.get(x["t"])
            if x["event"] != ev or r not in ("yes", "no"):
                continue
            won = (r == "yes") == (x["side"] == "YES")
            out.append({**x, "result": r, "won": won, "ret_per_dollar": round(((1.0 if won else 0.0) - x["px"] - x["fee"]) / x["px"], 4)})
    with SET.open("a") as fh:
        for x in out:
            fh.write(json.dumps(x) + "\n")
    return out


# ---------------------------------------------------------------- paper-harness plug-in (optional; not registered)
try:
    from scout.kalshi_lab_strategies.base import Strategy, fetcher

    @fetcher("r7_tallies", ttl=240)
    def _r7_tallies(t: int) -> dict:
        return gather_tallies(list(SERIES), int(t))

    class CountLadderShadow(Strategy):
        series = SERIES
        poll_s = 600

        def series_now(self, now):
            return [s for s in SERIES if any(phase(int(now.timestamp()), B) == "c1" for _, B in candidate_windows(s, int(now.timestamp())))]

        def external_now(self, now):
            return {"r7_tallies": {"t": int(now.timestamp())}} if self.series_now(now) else {}

        def decide(self, now, market_quotes, external_data):
            T = external_data.get("r7_tallies")
            if not T:
                return []
            FZ = Frozen.load(OUT / "frozen_model.json"); out = []
            for s in self.series_now(now):
                q = market_quotes.get(s) or {}
                for r in evaluate(s, list(q.values()), int(now.timestamp()), T, FZ):
                    out += [{"ticker": x["t"], "side": x["side"], "max_price": x["px"], "why": f"C1 model {x['model']:.2f} vs {x['px']:.2f}"}
                            for x in r["signals"] if x["rule"] == "C1"]
            return out

    STRATEGY = CountLadderShadow
except Exception:  # noqa: BLE001  (standalone use without the scout package)
    pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pass"
    force = "--force" in sys.argv
    if cmd == "pass":
        rows = one_pass(force)
        if rows is None:
            print("nothing due (no calls made)")
        else:
            for r in rows:
                print(json.dumps({"series": r["series"], "window_end": dt.datetime.fromtimestamp(r["B"], ET).isoformat(), "phase": r["phase"],
                                  "c_seen": r["tally"]["c_seen"], "model": r["model_info"],
                                  "markets": [(m["sub"], m["bid"], m["ask"], m["model"]) for m in r["markets"]],
                                  "signals": [(s["rule"], s["side"], s["sub"] if "sub" in s else s["t"], s["px"], s["model"]) for s in r["signals"]]}))
    elif cmd == "settle":
        print(json.dumps(settle(), indent=1))
    elif cmd == "status":
        t = int(time.time())
        print(json.dumps({s: [(dt.datetime.fromtimestamp(B, ET).isoformat(), phase(t, B)) for _, B in candidate_windows(s, t)] for s in SERIES}, indent=1))
