"""r2_slow_accumulators: monthly precipitation markets (KXRAIN*M), where the settling quantity builds up publicly from
the daily NWS CLI. Hypothesis: retail-traded, few market makers, information once a day -> a climatology model
conditioned on month-to-date (MTD) beats the price, or locked strikes stay mispriced for hours.

Protocol (docs/KALSHI_LAB.md + round-2 brief):
  * decision once a day at 10:00 local (station time zone); MTD = CLI total through yesterday (ACIS daily values);
  * fill as taker at the next hourly quote (11:00 local candle close; YES at yes_ask, NO at 1 - yes_bid), and at the
    12:00 quote as a latency check; fee 0.07 p (1-p) per contract, rounded up to the cent per order of 10 contracts;
  * events (series-month) ordered by close: discovery = events through May 2026 (69%), validation = Jun-Sep 2026;
  * t-stat clustered by event (series-month).
Usage: python -m lab.kalshi.strategies.r2_slow_accumulators [kill|grid|locked|validate|all]"""
from __future__ import annotations
import calendar, datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_slow_accumulators_data import load_markets, load_candles, station_of, OUT
from lab.kalshi.strategies.r2_slow_accumulators_model import p_yes, mtd, ym, TZ
from lab.kalshi.strategies.r2_slow_accumulators_select import selected
from lab.kalshi.strategies.r2_slow_accumulators_nbm import p_yes_nbm

CAL_CUT = (2026, 6)           # model B calibration pairs: station-months before June 2026 (discovery only);
                              # discovery rows leave their own calendar month out (no in-sample leakage)

DISC_END = (2026, 5)          # events through May 2026 = discovery (69% of 133 complete events)
DEC_HOUR = 10


def fee_per(p: float, n: int = 10) -> float:
    """Per-contract fee for an n-contract order, Kalshi rounds the order fee up to the cent."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def at(c: list[list], t: int) -> tuple[float, float, float] | None:
    """Quote from the hourly candle that ENDS exactly at t (its close = quote at t): (ask, bid, volume)."""
    lo, hi = 0, len(c)
    while lo < hi:
        mid = (lo + hi) // 2
        if c[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    if lo < len(c) and c[lo][0] == t and c[lo][1] is not None and c[lo][2] is not None:
        return c[lo][1], c[lo][2], c[lo][5]
    return None


def vol_near(c: list[list], t: int) -> float:
    return sum(r[5] for r in c if abs(r[0] - t) <= 3600)


def rows(sample_only: bool = True) -> list[dict]:
    """One row per (market, decision day) while the market is open at the decision time."""
    mk = load_markets(); cd = load_candles(); sel = set(selected())
    out = []
    for t, c in cd.items():
        m = mk.get(t)
        if not m or m["type"] != "greater" or m["floor"] is None or not c:
            continue
        st_ = station_of(m)
        if not st_:
            continue
        y, mo = ym(m["e"])
        live_full = (y, mo) >= (2026, 8)
        insample = t in sel or not live_full
        if sample_only and not insample:
            continue
        tz = ZoneInfo(TZ[st_]); last = calendar.monthrange(y, mo)[1]; K = float(m["floor"])
        won = m["result"] == "yes"
        for day in range(1, last + 1):
            tdec = int(dt.datetime(y, mo, day, DEC_HOUR, tzinfo=tz).timestamp())
            if not (m["open"] <= tdec < m["close"]):
                continue
            have = mtd(st_, y, mo, day)
            if have is None:
                continue
            q0 = at(c, tdec); q1 = at(c, tdec + 3600); q2 = at(c, tdec + 7200)
            if not q0:
                continue
            locked = have > K + 1e-9
            p = 1.0 if locked else p_yes(st_, y, mo, day, K, have)
            if p is None:
                continue
            pb = 1.0 if locked else p_yes_nbm(st_, y, mo, day, tdec, K, have, CAL_CUT, loo=True)
            out.append({"t": t, "e": m["e"], "series": m["series"], "st": st_, "ym": (y, mo), "day": day, "left": last - day + 1,
                        "K": K, "have": have, "need": K - have, "locked": locked, "p": p, "pb": pb, "won": won,
                        "ask0": q0[0], "bid0": q0[1], "q1": q1, "q2": q2 if q2 and tdec + 7200 < m["close"] else None,
                        "q1ok": bool(q1) and tdec + 3600 < m["close"], "sel": t in sel, "full": live_full,
                        "vol1": vol_near(c, tdec + 3600), "disc": (y, mo) <= DISC_END})
    return out


def brier(rs: list[dict]) -> dict:
    if not rs:
        return {"n": 0}
    def ll(p, w):
        p = min(max(p, 0.005), 0.995); return -math.log(p if w else 1 - p)
    mid = [(r["ask0"] + r["bid0"]) / 2 for r in rs]
    return {"n": len(rs), "markets": len({r["t"] for r in rs}), "events": len({r["e"] for r in rs}),
            "brier_mid": st.mean((m - r["won"]) ** 2 for m, r in zip(mid, rs)),
            "brier_model": st.mean((r["p"] - r["won"]) ** 2 for r in rs),
            "logloss_mid": st.mean(ll(m, r["won"]) for m, r in zip(mid, rs)),
            "logloss_model": st.mean(ll(r["p"], r["won"]) for r in rs),
            "yes_rate": st.mean(r["won"] for r in rs),
            **({"n_b": len(rb), "brier_mid_b": st.mean(((r["ask0"] + r["bid0"]) / 2 - r["won"]) ** 2 for r in rb),
                "brier_model_b": st.mean((r["pb"] - r["won"]) ** 2 for r in rb), "brier_model_a_b": st.mean((r["p"] - r["won"]) ** 2 for r in rb),
                "logloss_mid_b": st.mean(ll((r["ask0"] + r["bid0"]) / 2, r["won"]) for r in rb), "logloss_model_b": st.mean(ll(r["pb"], r["won"]) for r in rb)}
               if (rb := [r for r in rs if r.get("pb") is not None]) else {})}


def kill() -> dict:
    R = [r for r in rows() if r["disc"] and not r["locked"]]
    out = {"all": brier(R), "spread<=0.10": brier([r for r in R if r["ask0"] - r["bid0"] <= 0.10])}
    for lab, f in (("left>20", lambda r: r["left"] > 20), ("left 11-20", lambda r: 10 < r["left"] <= 20), ("left<=10", lambda r: r["left"] <= 10)):
        out[lab] = brier([r for r in R if f(r)])
    # one row per market-week to reduce autocorrelation in the comparison
    out["weekly"] = brier([r for r in R if r["day"] in (1, 8, 15, 22, 29)])
    return out


# ---------------------------------------------------------------- trading rules
def trade(r: dict, side: str, lag: int = 1) -> dict | None:
    q = r["q1"] if lag == 1 else r["q2"]
    if not q or (lag == 1 and not r["q1ok"]):
        return None
    ask, bid, _ = q
    px = ask if side == "YES" else 1 - bid
    if not (0.01 <= px <= 0.99):
        return None
    w = r["won"] if side == "YES" else not r["won"]
    pnl = (1.0 if w else 0.0) - px - fee_per(px)
    return {"t": r["t"], "e": r["e"], "day": r["day"], "ym": r["ym"], "side": side, "px": px, "won": w, "ret": pnl / px,
            "p": r["p"], "vol1": r["vol1"], "left": r["left"]}


def stats(T: list[dict]) -> dict:
    if not T:
        return {"n": 0, "events": 0}
    ev = defaultdict(list)
    for x in T:
        ev[x["e"]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em); mean = st.mean(x["ret"] for x in T)
    # clustered t: event-summed residuals (equal trade weights), Liang-Zeger
    num = sum(x["ret"] for x in T); res = defaultdict(float)
    for x in T:
        res[x["e"]] += x["ret"] - mean
    var = sum(v * v for v in res.values()) * (n_e / (n_e - 1) if n_e > 1 else 1)
    t = (mean * len(T)) / math.sqrt(var) if var > 0 else float("nan")
    rs = sorted((x["ret"] for x in T), reverse=True)
    T2 = sorted(T, key=lambda x: (x["ym"], x["day"]))
    h = len(T2) // 2
    return {"n": len(T), "events": n_e, "win": st.mean(x["won"] for x in T), "avg_px": st.mean(x["px"] for x in T), "ret": mean, "t": t,
            "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(x["ret"] for x in T2[:h]) if h else float("nan"), "half2": st.mean(x["ret"] for x in T2[h:]) if h else float("nan"),
            "median_vol_pm1h": st.median(x["vol1"] for x in T)}


def apply(R: list[dict], side: str, thr: float, minpx: float, window: str, once: bool, lag: int = 1, key: str = "pb") -> list[dict]:
    """Model-edge rule (model B by default). Edge measured on the decision (10:00) quote; fill at the next hourly quote."""
    T = []; done = set()
    for r in sorted(R, key=lambda r: (r["t"], r["day"])):
        if r["locked"] or (once and r["t"] in done) or r.get(key) is None:
            continue
        if window == "early" and r["left"] <= 10 or window == "late" and r["left"] > 10:
            continue
        for sd in (("YES", "NO") if side == "both" else (side,)):
            px0 = r["ask0"] if sd == "YES" else 1 - r["bid0"]
            pm = r[key] if sd == "YES" else 1 - r[key]
            if px0 < minpx or px0 > 0.99 or pm - px0 - fee_per(px0) < thr:
                continue
            x = trade(r, sd, lag)
            if x:
                T.append(x); done.add(r["t"])
            break
    return T


GRID = [(side, thr, minpx, window, once) for side in ("YES", "NO", "both") for thr in (0.05, 0.10, 0.15, 0.20)
        for minpx in (0.0, 0.30, 0.50) for window in ("all", "early", "late") for once in (True, False)]


def name(v) -> str:
    side, thr, minpx, window, once = v
    return f"MODEL {side} edge>={thr:.2f} px>={minpx:.2f} {window} {'once' if once else 'daily'}"


def grid() -> list[dict]:
    R = [r for r in rows() if r["disc"]]
    out = []
    for v in GRID:
        s = stats(apply(R, *v)); s["rule"] = name(v); out.append(s)
    out.sort(key=lambda s: -(s.get("t") or -9) if s["n"] >= 20 else 9)
    return out


# ---------------------------------------------------------------- locked-strike slowness probe
def locked_probe(hours=(6, 8, 10), maxpx=(0.90, 0.95, 0.97)) -> dict:
    """Strike already exceeded by the CLI total through yesterday, market still open at H:00 local: buy YES at the
    H+1 quote if ask <= maxpx. Uses every market with candles (sampled + full ladders)."""
    mk = load_markets(); cd = load_candles(); out = {}
    for H in hours:
        for mx in maxpx:
            T = {"disc": [], "val": []}; seen = set(); opp = {"disc": 0, "val": 0}
            for t, c in cd.items():
                m = mk.get(t)
                if not m or m["type"] != "greater" or m["floor"] is None or not c:
                    continue
                st_ = station_of(m); y, mo = ym(m["e"]); tz = ZoneInfo(TZ[st_]); last = calendar.monthrange(y, mo)[1]
                seg = "disc" if (y, mo) <= DISC_END else "val"
                for day in range(2, last + 1):
                    dd = dt.date(y, mo, day)
                    have = mtd(st_, y, mo, day)
                    if have is None or not have > float(m["floor"]) + 1e-9 or t in seen:
                        continue
                    tdec = int(dt.datetime(dd.year, dd.month, dd.day, H, tzinfo=tz).timestamp())
                    if not (m["open"] <= tdec and tdec + 3600 < m["close"]):
                        continue
                    q = at(c, tdec + 3600)
                    if not q:
                        continue
                    opp[seg] += 1; seen.add(t)
                    ask = q[0]
                    if ask <= mx:
                        w = m["result"] == "yes"
                        T[seg].append({"t": t, "e": m["e"], "day": day, "ym": (y, mo), "side": "YES", "px": ask, "won": w,
                                       "ret": ((1.0 if w else 0.0) - ask - fee_per(ask)) / ask, "vol1": vol_near(c, tdec + 3600), "left": 0})
                    break
            out[f"LOCKED YES {H:02d}:00 ask<={mx:.2f}"] = {"opportunities": opp, "disc": stats(T["disc"]), "val": stats(T["val"])}
    return out


# ---------------------------------------------------------------- frozen validation (run once)
def price_no(R: list[dict], lo: float, hi: float, lag: int = 1) -> list[dict]:
    T = []; seen = set()
    for r in sorted(R, key=lambda r: (r["t"], r["day"])):
        if r["locked"] or r["t"] in seen:
            continue
        if lo <= 1 - r["bid0"] < hi:
            x = trade(r, "NO", lag)
            if x:
                T.append(x); seen.add(r["t"])
    return T


def sampled_all() -> set[str]:
    from lab.kalshi.strategies.r2_slow_accumulators_select import events, pick
    out = set()
    for e, L in events().items():
        b = pick(L)
        if b:
            out.add(b[0]["t"])
    return out


def validate() -> dict:
    fz = json.loads((OUT / "frozen.json").read_text())["candidates"]
    allr = rows(sample_only=False)
    V = [r for r in allr if not r["disc"]]; D = [r for r in allr if r["disc"]]
    samp = sampled_all()
    Vs = [r for r in V if r["t"] in samp]
    c1 = fz["C1_modelB_NO"]; c2 = fz["C2_price_NO_control"]
    res = {}
    for lab, R in (("primary", V), ("sampled", Vs), ("discovery", D)):
        res[lab] = {
            "C1 lag1": stats(apply(R, c1["side"], c1["thr"], c1["min_px"], c1["window"], c1["once"])),
            "C1 lag2": stats(apply(R, c1["side"], c1["thr"], c1["min_px"], c1["window"], c1["once"], lag=2)),
            "C2 lag1": stats(price_no(R, c2["lo"], c2["hi"])),
            "C2 lag2": stats(price_no(R, c2["lo"], c2["hi"], lag=2)),
            "brier": brier([r for r in R if not r["locked"]]),
        }
    lp = locked_probe(hours=(fz["C3_locked_YES"]["hour"],), maxpx=(fz["C3_locked_YES"]["max_px"],))
    res["C3"] = lp
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "kill"
    if cmd == "kill":
        print(json.dumps(kill(), indent=1))
    elif cmd == "grid":
        g = grid()
        for s in g[:25]:
            print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()})
        print("variants", len(GRID))
    elif cmd == "locked":
        print(json.dumps(locked_probe(), indent=1, default=str))
    elif cmd == "validate":
        v = validate(); (OUT / "validation.json").write_text(json.dumps(v, indent=1, default=str)); print(json.dumps(v, indent=1, default=str))
