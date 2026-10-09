"""Forward (post-freeze) paper logger for r4_long_dated_archive_census. One pass per invocation, <= 10 Kalshi calls,
all through lab.us.data_refresh.fetch (bot-idle window, 429 retries). Never places orders.

Frozen rule: data/kalshi_lab/strategies/r4_long_dated_archive_census/preregistration.json (written before the first pass).
Definitions (deadline S, eligibility, cells, fee) come from r4_long_dated_archive_census_common.py.

Pass:
 1. Sweep (<= 6 calls): /events?status=open&with_nested_markets=true&limit=200&min_close_ts=now+13d, continuing a saved
    cursor across passes (a full sweep takes a few passes). Every open binary non-combo market with a listing-time
    deadline S (rules text / ticker date / fixed close), S - open >= 45 d and S within 400 d goes on the watchlist.
 2. Decide (<= 2 calls): watchlist (market, D) pairs whose decision time t = S - D days has passed by at most 12 h are
    quoted in one batched /markets?tickers=... call (<= 100 tickers). Executable NO price = 1 - yes_bid; capacity =
    yes_bid_size (contracts resting at the best YES bid = the NO ask). Paper TAKER fill at that logged quote for
    min(floor($5 / px), capacity) contracts if the price is in a cell band: C1 every (market, D) in [0.80, 0.92) with
    D in {60, 30, 14}; B the first entry per market in [0.70, 0.97); M the first entry per market in [0.50, 0.70).
    Fee 0.07 * fee_multiplier * n * p * (1 - p), rounded up to the cent per order. Pairs found later than 12 h after
    t are logged as "missed" (no fill).
 3. Settle (<= 1 call): up to 100 open paper positions (oldest check first) re-read via /markets?tickers=...; a market
    with a yes/no result writes a "settle" row.
 4. Depth (<= 1 call): the first new fill of the pass gets a /markets/{t}/orderbook snapshot (depth at the NO ask).
Outputs: forward.jsonl (rows: decision | fill | missed | settle | pass), forward_state.json (watchlist, cursor, keys),
forward_calls.log (one line per call; separate from the research budget log).
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_archive_census_logger [--max-calls 10]"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_archive_census_api import OUT, ROOT, kget  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_archive_census_common import (  # noqa: E402
    CELLS, DAY, DGRID, STAKE, deadline, is_macro, series_of, ts)

FWD = OUT / "forward.jsonl"
STATE = OUT / "forward_state.json"
FLOG = OUT / "forward_calls.log"
PREREG = OUT / "preregistration.json"
SWEEP_CALLS, QUOTE_CALLS, SETTLE_CALLS, BOOK_CALLS = 6, 2, 1, 1
LATE_MAX = 12 * 3600
HORIZON = 400 * DAY


class Budget:
    def __init__(self, n: int):
        self.left = n
        self.spent = 0

    def get(self, path: str) -> dict:
        if self.left <= 0:
            return {"_error": "pass budget"}
        before = _used()
        d = kget(path, cache=False, tries=1, budget=10 ** 9, log=FLOG)
        spent = max(_used() - before, 1)
        self.left -= spent
        self.spent += spent
        return d


def _used() -> int:
    if not FLOG.exists():
        return 0
    return sum(int(l.split("\t")[1]) for l in FLOG.read_text().splitlines() if l.strip())


def _catalog() -> tuple[dict, dict]:
    cat, mult = {}, {}
    for s in json.loads((ROOT / "data/kalshi_lab/series_all.json").read_text())["series"]:
        cat[s["ticker"]] = s.get("category")
        mult[s["ticker"]] = float(s.get("fee_multiplier") or 1)
    return cat, mult


def _f(x) -> float | None:
    try:
        return float(x)
    except Exception:
        return None


def _write(rows: list[dict]) -> None:
    with FWD.open("a") as g:
        for r in rows:
            g.write(json.dumps(r) + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"watch": {}, "cursor": None, "decided": [], "entered": {"B": [], "M": []}, "positions": {}, "sweeps_done": 0}


def sweep(st: dict, B: Budget, now: int, cat: dict) -> int:
    added = 0
    for _ in range(SWEEP_CALLS):
        cur = st.get("cursor")
        path = f"/events?status=open&with_nested_markets=true&limit=200&min_close_ts={now + 13 * DAY}" + (f"&cursor={cur}" if cur else "")
        d = B.get(path)
        if d.get("_error"):
            st["last_error"] = str(d.get("_error"))[:200]
            break
        for ev in d.get("events", []):
            for m in ev.get("markets") or []:
                if m.get("market_type") not in (None, "binary") or m["ticker"].startswith("KXMVE") or m.get("status") not in ("active", "open", "initialized"):
                    continue
                S, how = deadline(m)
                op = ts(m.get("open_time"))
                if S is None or op is None or S - op < 45 * DAY or S <= now or S - now > HORIZON:
                    continue
                ser = series_of(m)
                c = cat.get(ser) or cat.get(ser.removeprefix("KX"))
                if m["ticker"] not in st["watch"]:
                    added += 1
                st["watch"][m["ticker"]] = {"e": m["event_ticker"], "series": ser, "cat": c, "S": S, "S_how": how, "open": op,
                                            "close": ts(m.get("close_time")), "macro": is_macro(ser, c), "seen": now}
        st["cursor"] = d.get("cursor") or None
        if not st["cursor"]:
            st["sweeps_done"] = st.get("sweeps_done", 0) + 1
            st["last_full_sweep"] = now
            break
    # drop watch entries whose last decision time (S - 14 d) is more than a day past and that hold no position
    for t in [t for t, w in st["watch"].items() if w["S"] - 14 * DAY + DAY < now and t not in st["positions"]]:
        del st["watch"][t]
    return added


def decide(st: dict, B: Budget, now: int, mult: dict) -> list[dict]:
    rows, due = [], []
    decided = set(st["decided"])
    for t, w in st["watch"].items():
        for D in DGRID:
            k = f"{t}|{D}"
            dt_ = w["S"] - D * DAY
            if k in decided or dt_ > now:
                continue
            if now - dt_ > LATE_MAX or dt_ < w["open"]:
                rows.append({"type": "missed", "ts": now, "ticker": t, "D": D, "t_dec": dt_, "late_h": round((now - dt_) / 3600, 1),
                             "why": "listed after t" if dt_ < w["open"] else "found > 12 h after t"})
                decided.add(k)
                continue
            due.append((t, D))
    fills = []
    tick = sorted({t for t, _ in due})
    quotes = {}
    for i in range(0, min(len(tick), 100 * QUOTE_CALLS), 100):
        d = B.get("/markets?limit=1000&tickers=" + ",".join(tick[i:i + 100]))
        for m in d.get("markets", []):
            quotes[m["ticker"]] = m
    entered = {k: set(v) for k, v in st["entered"].items()}
    for t, D in due:
        m = quotes.get(t)
        if m is None:
            continue                       # not quoted this pass (budget); retried next pass while still within 12 h
        k = f"{t}|{D}"
        decided.add(k)
        w = st["watch"][t]
        yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        ybs = _f(m.get("yes_bid_size_fp")) or 0.0
        status = m.get("status")
        px = round(1 - yb, 4) if yb else None
        cells = []
        if px is not None and status in ("active", "open"):
            for c, spec in CELLS.items():
                if spec["lo"] <= px < spec["hi"] and D in spec["D"]:
                    if spec["first_entry"]:
                        if t in entered[c]:
                            continue
                        entered[c].add(t)
                    cells.append(c)
        rows.append({"type": "decision", "ts": now, "ticker": t, "e": w["e"], "series": w["series"], "cat": w["cat"], "macro": w["macro"],
                     "D": D, "S": w["S"], "S_how": w["S_how"], "t_dec": w["S"] - D * DAY, "status": status, "yes_bid": yb, "yes_ask": ya,
                     "yes_bid_size": ybs, "no_px": px, "cells": cells})
        for c in cells:
            n_want = max(1, int(STAKE // px))
            n = int(min(n_want, math.floor(ybs)))
            if n < 1:
                rows.append({"type": "fill", "ts": now, "ticker": t, "cell": c, "D": D, "px": px, "contracts": 0, "why": "no size at the NO ask"})
                continue
            fee = math.ceil(round(0.07 * mult.get(w["series"], 1.0) * n * px * (1 - px) * 100, 9)) / 100
            f = {"type": "fill", "ts": now, "ticker": t, "e": w["e"], "series": w["series"], "cat": w["cat"], "macro": w["macro"], "cell": c,
                 "D": D, "S": w["S"], "px": px, "contracts": n, "wanted": n_want, "fee": fee, "capacity_at_px": ybs}
            rows.append(f)
            fills.append(f)
            st["positions"].setdefault(t, {"opened": now, "checked": 0})
    st["decided"] = sorted(decided)
    st["entered"] = {k: sorted(v) for k, v in entered.items()}
    return rows, fills


def settle(st: dict, B: Budget, now: int) -> list[dict]:
    rows = []
    pos = sorted(st["positions"].items(), key=lambda kv: kv[1].get("checked", 0))[:100]
    if not pos:
        return rows
    d = B.get("/markets?limit=1000&tickers=" + ",".join(t for t, _ in pos))
    got = {m["ticker"]: m for m in d.get("markets", [])}
    for t, p in pos:
        p["checked"] = now
        m = got.get(t)
        if m and m.get("result") in ("yes", "no"):
            rows.append({"type": "settle", "ts": now, "ticker": t, "result": m["result"], "status": m.get("status"),
                         "close": m.get("close_time"), "settled": m.get("settlement_ts")})
            del st["positions"][t]
    return rows


def depth(B: Budget, f: dict) -> dict:
    d = B.get(f"/markets/{f['ticker']}/orderbook")
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    yes = ob.get("yes_dollars") or ob.get("yes") or []
    lv = []
    for x in yes:
        try:
            p, q = float(x[0]), float(x[1])
            lv.append((p / 100 if p > 1.0001 else p, q))
        except Exception:
            pass
    lv.sort(reverse=True)
    at = sum(q for p, q in lv if 1 - p <= f["px"] + 1e-9)
    within2 = sum(q for p, q in lv if 1 - p <= f["px"] + 0.02 + 1e-9)
    return {"type": "depth", "ts": int(time.time()), "ticker": f["ticker"], "no_ask": f["px"], "contracts_at_px": at,
            "contracts_within_2c": within2, "levels": lv[:5], "err": d.get("_error")}


def run(max_calls: int = 10) -> dict:
    if not PREREG.exists():
        raise SystemExit("preregistration.json missing: the rule must be frozen before any forward data")
    now = int(time.time())
    cat, mult = _catalog()
    st = load_state()
    B = Budget(max_calls)
    added = sweep(st, B, now, cat)
    rows, fills = decide(st, B, now, mult)
    rows += settle(st, B, now)
    if fills and B.left > 0:
        rows.append(depth(B, fills[0]))
    rows.append({"type": "pass", "ts": now, "calls": B.spent, "watch": len(st["watch"]), "added": added, "cursor_open": bool(st.get("cursor")),
                 "sweeps_done": st.get("sweeps_done", 0), "positions": len(st["positions"]), "fills": len(fills),
                 "decisions": sum(r["type"] == "decision" for r in rows), "missed": sum(r["type"] == "missed" for r in rows),
                 "err": st.get("last_error")})
    _write(rows)
    STATE.write_text(json.dumps(st))
    print(json.dumps(rows[-1]))
    return rows[-1]


if __name__ == "__main__":
    a = sys.argv
    run(int(a[a.index("--max-calls") + 1]) if "--max-calls" in a else 10)
