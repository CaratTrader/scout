"""r3_seasonal_regime_monitors forward logger: one pass per invocation, at most 10 Kalshi calls, appends to
data/kalshi_lab/strategies/r3_seasonal_regime_monitors/forward.jsonl and rewrites status.json. Paper only: it never
places, cancels or sizes an order and holds no credentials.

Schedule: hourly, at any fixed minute (e.g. launchd StartInterval 3600, or cron '7 * * * *'), from the repo root:
    .venv/bin/python -m lab.kalshi.strategies.r3_seasonal_regime_monitors_logger
The owner schedules it; this code never installs or touches launchd jobs.

Each pass:
  1. KXRAIN spread regime (1 call: /markets?series_ticker=KXRAIN&status=open). Snapshot classification with the frozen
     indicator of r3_seasonal_regime_monitors.classify (window 60 s < close - now <= 18 h; eligible = two-sided,
     0.06 <= ask <= 0.96; wide = eligible and ask - bid >= 0.06). -> {"kind": "rain_snap"}.
  2. Monthly rain (<= 4 calls): refresh the open-market list of the 2 least recently refreshed KXRAIN*M series
     (/markets?series_ticker=S&status=open), then quote every known open market of the current month's events
     (/markets?tickers=..., 60 per call; ~110 markets -> 2 calls). -> {"kind": "monthly_snap"} with the markets whose NO price 1 - yes_bid is in the C2 band [0.35, 0.65).
     Frozen C2 (r2_slow_accumulators frozen.json): the first snapshot in [10:00, 11:00) local time (station zone) on a
     day of the event month decides (NO price in band; in-band implies the strike is not yet exceeded); the first
     snapshot in [11:00, 12:00) local on the same day fills as taker at 1 - yes_bid, $5 order (floor(5/px) contracts),
     fee 0.07 p (1-p) rounded up to the cent per order. Once per market; a decided market whose fill snapshot is missing
     stays eligible on later days (as in the backtest). -> {"kind": "c2_decide"|"c2_entry"}.
  3. Daily jobs, at the first passes at or after 13:00 UTC (resumable, they never push a pass over 10 calls):
     a. settle C2 entries whose market closed (/markets?tickers=...) -> {"kind": "c2_settle"};
     b. frozen rule E forward on each completed KXRAIN event date (settled list: 1 call; 1-minute candles with the trade
        'price' block: ~4-5 batched calls) -> {"kind": "e_day"} and one row per order in e_orders.jsonl, with two fill
        models: the frozen microstructure quote rule (YES bid high >= our offer + 1c) and the shared
        r2_infra_timestamp_fill_audit_fill.maker_fill() (prints at or through our created level);
     c. trigger evaluation -> {"kind": "daily"} and status.json.
Triggers (frozen, see r3_seasonal_regime_monitors.py and preregistration.json):
  T1 W > 10% on 5 consecutive event dates (each with >= 12 snapshots that had an eligible market) -> regime returned;
     rule E's forward measurement (3b) runs regardless, so the flag only marks the regime.
  T2 the first 30 settled forward C2 entries average >= +10% per $ -> C2 second forward sample starts (entries decided
     after the fire date), judged by the gate in preregistration.json.
Usage: python -m lab.kalshi.strategies.r3_seasonal_regime_monitors_logger [--no-daily] [--summary]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_seasonal_regime_monitors_api import kget, set_cap, calls_this_process, OUT
from lab.kalshi.strategies.r3_seasonal_regime_monitors import (classify, event_date, stats, WIN_H, TRIG1_SHARE, TRIG1_DAYS,
                                                                MIN_SNAPS, TRIG2_N, TRIG2_RET, C2_LO, C2_HI, RULE_E)

FWD = OUT / "forward.jsonl"
STATE = OUT / "state.json"
STATUS = OUT / "status.json"
EORD = OUT / "e_orders.jsonl"
CAP = 10
SERIES_TZ = {"KXRAINNYCM": "America/New_York", "KXRAINCHIM": "America/Chicago", "KXRAINSTPM": "America/New_York",
             "KXRAINNAPAM": "America/Los_Angeles", "KXRAINDENM": "America/Denver", "KXRAINLEXM": "America/New_York",
             "KXRAINAUSM": "America/Chicago", "KXRAINCMHM": "America/New_York", "KXRAINLAXM": "America/Los_Angeles",
             "KXRAINSFOM": "America/Los_Angeles", "KXRAINPVDM": "America/New_York", "KXRAINMKEM": "America/Chicago",
             "KXRAINDALM": "America/Chicago", "KXRAINHOUM": "America/Chicago", "KXRAINSEAM": "America/Los_Angeles",
             "KXRAINCLLM": "America/Chicago", "KXRAINMIAM": "America/New_York"}
MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
FREEZE_TS = int(dt.datetime(2026, 10, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp())   # forward period starts here
E_FIRST_DATE = "2026-10-08"     # first KXRAIN event date of the forward rule-E sample (backcast ends 2026-10-07)
MAX_CANDLES = 9500


# ------------------------------------------------------------------ io helpers
def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def fnum(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_state() -> dict:
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    s.setdefault("monthly", {}); s.setdefault("c2", {"entered": {}, "pending": {}})
    s.setdefault("e_done", []); s.setdefault("passes", 0)
    return s


def save_state(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=0))


def emit(rec: dict) -> None:
    with FWD.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def read_fwd() -> list[dict]:
    return [json.loads(l) for l in FWD.open()] if FWD.exists() else []


def fee_per(p: float, n: int) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def quote(m: dict) -> tuple[float | None, float | None]:
    return fnum(m.get("yes_ask_dollars")), fnum(m.get("yes_bid_dollars"))


def event_month(e: str) -> tuple[int, int]:
    code = e.split("-")[1]
    return 2000 + int(code[:2]), MON[code[2:5]]


# ------------------------------------------------------------------ 1. KXRAIN spread regime
def rain_snapshot(now: int) -> dict | None:
    ms, cursor = [], ""
    for _ in range(2):
        d = kget("/markets?series_ticker=KXRAIN&status=open&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        if d.get("_error"):
            return None
        ms += d.get("markets") or []
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets"):
            break
    by_date = defaultdict(lambda: [0, 0, 0, 0]); wide = []
    tot = [0, 0, 0, 0]; n_win = 0
    for m in ms:
        c = ts(m.get("close_time"))
        if not c or not (60 < c - now <= WIN_H * 3600):
            continue
        n_win += 1
        a, b = quote(m)
        q, wa, el, we = classify(a, b)
        k = by_date[event_date(m["event_ticker"])]
        for i, v in enumerate((q, wa, el, we)):
            k[i] += int(v); tot[i] += int(v)
        if we:
            wide.append([m["ticker"], b, a, round((c - now) / 3600, 2)])
    rec = {"kind": "rain_snap", "t": now, "iso": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()[:16],
           "n_open": len(ms), "n_window": n_win, "quoted": tot[0], "wide_any": tot[1], "eligible": tot[2], "wide_eligible": tot[3],
           "by_date": dict(by_date), "wide_markets": wide}
    emit(rec)
    return rec


# ------------------------------------------------------------------ 2. monthly rain C2
def refresh_series(state: dict, now: int, k: int = 2) -> None:
    mon = state["monthly"]
    order = sorted(SERIES_TZ, key=lambda s: mon.get(s, {}).get("refreshed", 0))
    for s in order[:k]:
        d = kget(f"/markets?series_ticker={s}&status=open&limit=1000")
        if d.get("_error"):
            continue
        mk = {}
        for m in d.get("markets") or []:
            mk[m["ticker"]] = {"e": m["event_ticker"], "close": ts(m.get("close_time")), "open": ts(m.get("open_time")),
                               "floor": m.get("floor_strike"), "type": m.get("strike_type")}
        mon[s] = {"refreshed": now, "markets": mk}


def monthly_pass(state: dict, now: int) -> None:
    mon = state["monthly"]; c2 = state["c2"]
    u = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    known = {t: (s, x) for s, v in mon.items() for t, x in v.get("markets", {}).items()
             if (x.get("close") or 0) > now and event_month(x["e"]) == (u.year, u.month)}   # decision-eligible month only
    if not known:
        return
    tick = sorted(known); quotes = {}
    for i in range(0, len(tick), 60):
        d = kget("/markets?limit=1000&tickers=" + ",".join(tick[i:i + 60]))
        for m in d.get("markets") or []:
            quotes[m["ticker"]] = m
    band = []
    for t in tick:
        m = quotes.get(t)
        if not m or m.get("status") not in ("active", "open"):
            continue
        s, x = known[t]
        a, b = quote(m)
        if a is None or b is None:
            continue
        tz = ZoneInfo(SERIES_TZ[s]); L = dt.datetime.fromtimestamp(now, tz); y, mo = event_month(x["e"])
        nopx = round(1 - b, 4)
        inband = C2_LO <= nopx < C2_HI and x.get("type") == "greater"
        if inband:
            band.append([t, b, a, L.hour])
        if t in c2["entered"] or (L.year, L.month) != (y, mo):
            continue
        day = L.date().isoformat()
        p = c2["pending"].get(t)
        if L.hour == 10 and inband and not (p and p["date"] == day):
            c2["pending"][t] = {"date": day, "t": now, "no_px": nopx}
            emit({"kind": "c2_decide", "t": now, "ticker": t, "series": s, "local": L.isoformat()[:16], "no_px": nopx, "yes_ask": a})
        elif L.hour == 11 and p and p["date"] == day and not p.get("filled"):
            px = nopx
            p["filled"] = True
            if 0.01 <= px <= 0.99:
                n = max(1, int(5 / px))
                c2["entered"][t] = now
                emit({"kind": "c2_entry", "t": now, "ticker": t, "e": x["e"], "series": s, "local": L.isoformat()[:16], "decided": p["t"],
                      "no_px_decision": p["no_px"], "px": px, "contracts": n, "fee_per": fee_per(px, n), "yes_ask": a, "yes_bid": b,
                      "spread": round(a - b, 4), "close": x["close"], "backfill": False})
    # drop stale pending entries (older than 2 days)
    c2["pending"] = {t: p for t, p in c2["pending"].items() if now - p["t"] < 2 * 86400 and t not in c2["entered"]}
    emit({"kind": "monthly_snap", "t": now, "n_known": len(known), "n_quoted": len(quotes), "n_band": len(band), "band": band})


def settle_c2(now: int) -> None:
    F = read_fwd()
    done = {r["ticker"] for r in F if r["kind"] == "c2_settle"}
    todo = [r for r in F if r["kind"] == "c2_entry" and r["ticker"] not in done and (r.get("close") or 0) < now]
    for i in range(0, min(len(todo), 120), 60):
        chunk = todo[i:i + 60]
        d = kget("/markets?limit=1000&tickers=" + ",".join(r["ticker"] for r in chunk))
        res = {m["ticker"]: m for m in d.get("markets") or []}
        for r in chunk:
            m = res.get(r["ticker"])
            if not m or m.get("result") not in ("yes", "no"):
                continue
            won = m["result"] == "no"
            pnl = (1.0 if won else 0.0) - r["px"] - r["fee_per"]
            emit({"kind": "c2_settle", "t": now, "ticker": r["ticker"], "e": r["e"], "entry_t": r["t"], "px": r["px"], "result": m["result"],
                  "won": won, "ret": round(pnl / r["px"], 5), "yes_mid_at_entry": round((r["yes_ask"] + r["yes_bid"]) / 2, 4),
                  "backfill": r.get("backfill", False)})


# ------------------------------------------------------------------ 3b. rule E forward from 1-minute candles
def _compact(c: dict) -> list:
    f = lambda k, s: fnum((c.get(k) or {}).get(s))
    return [int(c["end_period_ts"]), f("yes_ask", "close_dollars"), f("yes_bid", "close_dollars"), f("yes_ask", "low_dollars"),
            f("yes_bid", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)]


def e_orders(g, cs_norm: list[dict]) -> list[dict]:
    """Frozen rule E decisions (identical to microstructure.rules_maker for E|rain|S0.06|H30|NO) with both fill models."""
    from lab.kalshi.strategies.microstructure import fee_order, maker_fee
    from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_fill import maker_fill
    out = []; last = -10 ** 12; S, H = 0.06, 30
    for i in range(0, g.n - 2):
        if not g.fresh[i] or g.mid[i] is None:
            continue
        a, b = g.ask[i], g.bid[i]; s = a - b; T = g.T(i)
        if s < S - 1e-9 or T - last < 30 * 60:
            continue
        P = round(1 - (a - 0.01), 4)
        if not (0.05 <= P <= 0.95):
            continue
        last = T
        j = i + 1
        if g.ask[j] is None or g.bid[j] is None:
            continue
        won = not g.won
        o = {"m": g.t, "d": event_date(g.e), "t": T, "spread": round(s, 3), "P": P, "won": won, "close": g.close}
        # (a) frozen quote rule
        fp = None; taker = False
        if (1 - g.bid[j]) <= P:
            fp = 1 - g.bid[j]; taker = True
        else:
            for x in range(j + 1, min(g.n, j + 1 + H)):
                if g.fresh[x] and g.bhigh[x] is not None and (1 - g.bhigh[x]) <= P - 0.01 + 1e-9:
                    fp = P; break
        if fp is not None and 0.01 <= fp <= 0.99:
            n = max(1, int(5 / fp)); f = fee_order(fp, n) if taker else maker_fee("KXRAIN", fp, n)
            o["q"] = {"px": round(fp, 4), "taker": taker, "ret": ((1.0 if won else 0.0) - fp - f) / fp}
        # (b) shared maker_fill(): NO bid at P = YES offer at level a - 0.01, live T+60 .. T+60+30 min (or close)
        lvl = round(a - 0.01, 4); tl = T + 60; te = min(tl + H * 60, g.close - 1)
        r = maker_fill("no", lvl, tl, te, cs_norm, series="KXRAIN")
        if r["status"] == "crossed":
            fp = round(1 - r["px"], 4); n = max(1, int(5 / fp)) if fp > 0 else 1
            if 0.01 <= fp <= 0.99:
                o["mf"] = {"px": fp, "taker": True, "kind": "crossed", "ret": ((1.0 if won else 0.0) - fp - fee_order(fp, n)) / fp}
        elif r["status"] == "filled":
            o["mf"] = {"px": P, "taker": False, "kind": r["kind"], "ret": ((1.0 if won else 0.0) - P) / P}
        out.append(o)
    return out


def e_forward(state: dict, now: int, budget: int) -> int:
    """Process completed KXRAIN event dates >= E_FIRST_DATE not yet done. Returns calls used."""
    from lab.kalshi.strategies.microstructure_data import Grid
    from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_fill import norm_candle
    used = 0
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    d0 = dt.date.fromisoformat(E_FIRST_DATE)
    pend = [(d0 + dt.timedelta(days=k)).isoformat() for k in range((today - d0).days)]
    pend = [d for d in pend if d not in state["e_done"]]
    for D in pend:
        Dd = dt.date.fromisoformat(D)
        hi = int(dt.datetime(Dd.year, Dd.month, Dd.day, tzinfo=dt.timezone.utc).timestamp()) + 36 * 3600
        if now < hi + 3600:          # regular markets close 04:00-08:00 UTC on D+1 (a few later); all must be settled below
            break
        cache = OUT / "e_cache" / f"{D}.json"
        got = json.loads(cache.read_text()) if cache.exists() else {"markets": None, "candles": {}}
        if got["markets"] is None:
            if used >= budget:
                break
            # by event ticker: every market of the event, whatever its close time (a new city, e.g. KXRAIN-26SEP30-DTW,
            # opened mid-day and closed at 14:00 UTC, outside any fixed close-time window)
            d = kget(f"/markets?event_ticker=KXRAIN-{Dd.strftime('%y%b%d').upper()}&limit=1000"); used += 1
            if d.get("_error"):
                break
            ms = [m for m in d.get("markets") or [] if event_date(m["event_ticker"]) == D]
            if not ms or any(m.get("result") not in ("yes", "no") for m in ms):
                break                # not all of D's markets settled yet: retry on a later pass (never a partial date)
            got["markets"] = [{"t": m["ticker"], "e": m["event_ticker"], "series": "KXRAIN", "open": ts(m.get("open_time")),
                               "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")), "result": m["result"],
                               "type": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
                               "sub": m.get("yes_sub_title")} for m in ms]
        todo = sorted((m for m in got["markets"] if m["t"] not in got["candles"]), key=lambda m: m["close"])
        while todo and used < budget:
            batch = [todo[0]]
            while len(batch) < len(todo):
                nxt = todo[len(batch)]
                span = (max(m["close"] for m in batch + [nxt]) - (min(m["close"] for m in batch + [nxt]) - WIN_H * 3600)) // 60 + 1
                if (len(batch) + 1) * span > MAX_CANDLES:
                    break
                batch.append(nxt)
            st_ = min(m["close"] for m in batch) - WIN_H * 3600; en = max(m["close"] for m in batch)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={st_}&end_ts={en}&period_interval=1"); used += 1
            if d.get("_error"):
                break
            res = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            for m in batch:
                got["candles"][m["t"]] = res.get(m["t"], [])
            todo = todo[len(batch):]
        cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(json.dumps(got))
        if todo:
            break                    # resume next pass
        orders = []; quoted = el = we = 0
        for m in got["markets"]:
            raw = [c for c in got["candles"].get(m["t"], []) if m["close"] - WIN_H * 3600 <= c["end_period_ts"] <= m["close"] + 60]
            if not raw:
                continue
            comp = [_compact(c) for c in raw]
            g = Grid(m, "rain", comp, m["close"])
            if g.n < 2:
                continue
            cs = sorted((norm_candle(c) for c in raw), key=lambda c: c["T"])
            orders += e_orders(g, cs)
            for i in range(g.n):          # minute-level opportunity share (fresh minutes)
                if g.fresh[i] and g.ask[i] is not None:
                    _, _, e1, w1 = classify(g.ask[i], g.bid[i]); el += e1; we += w1
        with EORD.open("a") as f:
            for o in orders:
                f.write(json.dumps(o) + "\n")
        q = [o["q"]["ret"] for o in orders if "q" in o]; mf = [o["mf"]["ret"] for o in orders if "mf" in o]
        emit({"kind": "e_day", "t": now, "date": D, "markets": len(got["markets"]), "orders": len(orders),
              "fills_quote": len(q), "ret_quote": round(st.mean(q), 4) if q else None,
              "fills_maker_fill": len(mf), "ret_maker_fill": round(st.mean(mf), 4) if mf else None,
              "minute_wide_share": round(we / el, 4) if el else None})
        state["e_done"].append(D)
    return used


# ------------------------------------------------------------------ 3c. triggers and summary
def summarize(now: int | None = None) -> dict:
    now = now or int(time.time())
    F = read_fwd()
    # W per event date from the hourly snapshots (first snapshot per clock hour)
    per = defaultdict(lambda: {"hours": set(), "good": 0, "q": 0, "wa": 0, "el": 0, "we": 0})
    for r in F:
        if r["kind"] != "rain_snap":
            continue
        h = r["t"] // 3600
        for D, (q, wa, el, we) in r["by_date"].items():
            P = per[D]
            if h in P["hours"]:
                continue
            P["hours"].add(h); P["good"] += int(el > 0); P["q"] += q; P["wa"] += wa; P["el"] += el; P["we"] += we
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    W = {}
    for D, P in sorted(per.items()):
        complete = now > int(dt.datetime.fromisoformat(D).replace(tzinfo=dt.timezone.utc).timestamp()) + 36 * 3600   # D's markets close by ~D+1 08:00 UTC
        W[D] = {"W": round(P["we"] / P["el"], 4) if P["el"] else None, "W_all": round(P["wa"] / P["q"], 4) if P["q"] else None,
                "snapshots": len(P["hours"]), "snapshots_with_eligible": P["good"], "complete": complete}
    streak = 0; best = 0; fired = None
    for D in sorted(d for d, v in W.items() if v["complete"]):
        v = W[D]
        ok = v["W"] is not None and v["W"] > TRIG1_SHARE and v["snapshots_with_eligible"] >= MIN_SNAPS
        prev = (dt.date.fromisoformat(D) - dt.timedelta(days=1)).isoformat()
        streak = streak + 1 if ok and (streak == 0 or prev in W) else (1 if ok else 0)
        best = max(best, streak)
        if streak >= TRIG1_DAYS and not fired:
            fired = D
    # C2 forward
    ent = sorted((r for r in F if r["kind"] == "c2_entry"), key=lambda r: r["t"])
    setl = {r["ticker"]: r for r in F if r["kind"] == "c2_settle"}
    S = [dict(setl[r["ticker"]], d=dt.datetime.fromtimestamp(r["t"], dt.timezone.utc).date().isoformat()) for r in ent if r["ticker"] in setl]
    first30 = S[:TRIG2_N]
    t2 = None
    if len(first30) >= TRIG2_N:
        m30 = st.mean(r["ret"] for r in first30)
        t2 = {"mean_first30": round(m30, 4), "fired": m30 >= TRIG2_RET,
              "fired_at_entry_t": first30[-1]["entry_t"] if m30 >= TRIG2_RET else None}
    yes_over = (round(st.mean(r["yes_mid_at_entry"] for r in S) - st.mean(r["result"] == "yes" for r in S), 4) if S else None)
    # E forward
    O = [json.loads(l) for l in EORD.open()] if EORD.exists() else []
    eq = [dict(o["q"], e=o["d"], won=o["won"]) for o in O if "q" in o]
    emf = [dict(o["mf"], e=o["d"], won=o["won"]) for o in O if "mf" in o]
    days = sorted({o["d"] for o in O})
    out = {"updated": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()[:16],
           "kxrain_W_by_event_date": W,
           "trigger1": {"rule": f"W > {TRIG1_SHARE} on {TRIG1_DAYS} consecutive event dates (>= {MIN_SNAPS} snapshots with an eligible market)",
                        "current_streak": streak, "best_streak": best, "fired_on": fired},
           "rule_E_forward": {"rule": RULE_E, "first_date": E_FIRST_DATE, "event_dates": len(days), "orders": len(O),
                              "quote_fill": stats(eq), "maker_fill": stats(emf),
                              "gate": "preregistration.json E_forward: n >= 150 quote-model fills on >= 15 event dates; mean >= +10%/$ on BOTH fill models; day-clustered t >= 2.0; mean > 0 without the best 3 trades AND without the 3 most profitable markets; both halves (by date) > 0. Kill after 10 event dates if the quote-model mean < 0.",
                              "without_top3_markets": _wo_top_markets(O, "q", 3), "maker_fill_without_top3_markets": _wo_top_markets(O, "mf", 3)},
           "c2_forward": {"entries": len(ent), "entries_backfill": sum(1 for r in ent if r.get("backfill")), "settled": len(S),
                          "all": stats(S, order="entry_t"), "excluding_backfill": stats([r for r in S if not r.get("backfill")], order="entry_t"),
                          "yes_overpricing_mid_minus_rate": yes_over, "trigger2": t2 or {"rule": f"first {TRIG2_N} settled entries average >= +{TRIG2_RET:.0%}/$", "settled_so_far": len(S)}},
           "flags": {"E_regime_returned": bool(fired), "C2_regime_returned": bool(t2 and t2["fired"])}}
    return out


def _wo_top_markets(O: list[dict], model: str, k: int) -> dict:
    X = [dict(o[model], e=o["d"], won=o["won"], m=o["m"]) for o in O if model in o]
    by = defaultdict(float)
    for x in X:
        by[x["m"]] += x["ret"]
    top = {m for m, _ in sorted(by.items(), key=lambda kv: -kv[1])[:k]}
    return stats([x for x in X if x["m"] not in top])


def daily(state: dict, now: int) -> None:
    s = summarize(now)
    emit({"kind": "daily", "t": now, "trigger1": s["trigger1"], "flags": s["flags"],
          "E": {k: s["rule_E_forward"][k] for k in ("event_dates", "orders")} | {"quote_fill": s["rule_E_forward"]["quote_fill"]},
          "C2": {k: s["c2_forward"][k] for k in ("entries", "settled", "yes_overpricing_mid_minus_rate")} | {"all": s["c2_forward"]["all"]}})


# ------------------------------------------------------------------ one-time seed (research budget): ticker lists + October backfill
def seed(now: int | None = None) -> dict:
    """Lists every KXRAIN*M series' open markets (17 calls) and backfills frozen-C2 entries for the October 2026 events'
    decision days that ended before this seed (hourly candles from each market's open, batched <= 9,500 market-hours per
    call). Quotes carry forward from the last hourly candle at or before 10:00 / 11:00 local (the logger's REST semantics;
    the r2 backtest required a candle ending exactly on the hour). Entries are flagged backfill=True; the C2 rule was
    frozen 2026-10-08T21:26Z and never saw October, so they are out of sample, but they are reported with and without."""
    now = now or int(time.time())
    state = load_state()
    if any(r["kind"] == "c2_entry" and r.get("backfill") for r in read_fwd()):
        return {"skipped": "already seeded"}
    for s_ in SERIES_TZ:
        d = kget(f"/markets?series_ticker={s_}&status=open&limit=1000", tag="research")
        mk = {m["ticker"]: {"e": m["event_ticker"], "close": ts(m.get("close_time")), "open": ts(m.get("open_time")),
                            "floor": m.get("floor_strike"), "type": m.get("strike_type")} for m in d.get("markets") or []}
        state["monthly"][s_] = {"refreshed": now, "markets": mk}
    save_state(state)
    octm = sorted(((t, s_, x) for s_, v in state["monthly"].items() for t, x in v["markets"].items()
                   if event_month(x["e"]) == (2026, 10) and x.get("type") == "greater"), key=lambda z: z[2]["open"])
    lo = min(x["open"] for _, _, x in octm) // 3600 * 3600; hi = (now // 3600) * 3600
    per = (hi - lo) // 3600 + 1; k = max(1, MAX_CANDLES // per)
    cand = {}
    for i in range(0, len(octm), k):
        b = octm[i:i + k]
        d = kget(f"/markets/candlesticks?market_tickers={','.join(t for t, _, _ in b)}&start_ts={lo}&end_ts={hi}&period_interval=60", tag="research")
        for x in d.get("markets") or []:
            cand[x.get("market_ticker") or x.get("ticker")] = sorted((int(c["end_period_ts"]), fnum((c.get("yes_ask") or {}).get("close_dollars")),
                                                                       fnum((c.get("yes_bid") or {}).get("close_dollars"))) for c in x.get("candlesticks") or [])
    (OUT / "seed_oct_candles.json").write_text(json.dumps(cand))
    def q_at(c, t):
        best = None
        for r in c:
            if r[0] <= t:
                best = r
            else:
                break
        return (best[1], best[2]) if best and best[1] is not None and best[2] is not None else None
    n_ent = 0
    for t, s_, x in octm:
        c = cand.get(t) or []
        tz = ZoneInfo(SERIES_TZ[s_])
        for day in range(1, 32):
            try:
                tdec = int(dt.datetime(2026, 10, day, 10, tzinfo=tz).timestamp())
            except ValueError:
                break
            if tdec + 3600 > now - 60 or t in state["c2"]["entered"]:
                break
            if not (x["open"] <= tdec < x["close"]):
                continue
            q0 = q_at(c, tdec)
            if not q0 or not (C2_LO <= round(1 - q0[1], 4) < C2_HI):
                continue
            q1 = q_at(c, tdec + 3600)
            if not q1:
                continue
            px = round(1 - q1[1], 4)
            if not (0.01 <= px <= 0.99):
                continue
            n = max(1, int(5 / px)); state["c2"]["entered"][t] = tdec + 3600; n_ent += 1
            emit({"kind": "c2_entry", "t": tdec + 3600, "ticker": t, "e": x["e"], "series": s_,
                  "local": dt.datetime.fromtimestamp(tdec + 3600, tz).isoformat()[:16], "decided": tdec, "no_px_decision": round(1 - q0[1], 4),
                  "px": px, "contracts": n, "fee_per": fee_per(px, n), "yes_ask": q1[0], "yes_bid": q1[1], "spread": round(q1[0] - q1[1], 4),
                  "close": x["close"], "backfill": True})
    save_state(state)
    return {"series": len(SERIES_TZ), "october_markets": len(octm), "with_candles": sum(1 for t, _, _ in octm if cand.get(t)), "backfill_entries": n_ent}


def main(argv: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if "--summary" in argv:
        print(json.dumps(summarize(), indent=1, default=str)); return
    if "--seed" in argv:
        print(json.dumps(seed(), indent=1)); return
    set_cap(CAP)
    now = int(time.time()); state = load_state(); state["passes"] += 1
    rain_snapshot(now)                                                  # 1 call
    utc = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    daily_due = "--no-daily" not in argv and utc.hour >= 13 and state.get("daily_done") != utc.date().isoformat()
    c2_window = any(dt.datetime.fromtimestamp(now, ZoneInfo(z)).hour in (10, 11) for z in set(SERIES_TZ.values()))
    if not daily_due or c2_window:
        refresh_series(state, now, k=2 if not daily_due else 1)        # <= 2 calls
        monthly_pass(state, now)                                        # <= 3 calls
    if daily_due:
        settle_c2(now)                                                  # <= 2 calls
        left = CAP - calls_this_process()
        if left > 0:
            e_forward(state, now, left)
        d0 = dt.date.fromisoformat(E_FIRST_DATE)
        all_done = all((d0 + dt.timedelta(days=k)).isoformat() in state["e_done"] for k in range(max(0, (utc.date() - d0).days)))
        if all_done or utc.hour >= 23:     # the summary is written even if a date is still unsettled late in the day
            daily(state, now); state["daily_done"] = utc.date().isoformat()
    save_state(state)
    s = summarize(now)
    STATUS.write_text(json.dumps(s, indent=1, default=str))
    print(f"pass {state['passes']}: calls {calls_this_process()}, T1 streak {s['trigger1']['current_streak']} fired {s['trigger1']['fired_on']}, "
          f"E fwd orders {s['rule_E_forward']['orders']}, C2 entries {s['c2_forward']['entries']} settled {s['c2_forward']['settled']}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
