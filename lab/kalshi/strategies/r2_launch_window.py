"""r2_launch_window: are NEW Kalshi daily product types mispriced in their first weeks, before market makers arrive?

Two tests (pre-registered in data/kalshi_lab/strategies/r2_launch_window/prereg_analysis.json before any candle was
fetched):

T1 seasonality  KXRAINNYC (a single-city rain product live since at least 2025-01) on the same calendar windows as
                the KXRAIN launch periods, one year earlier (2025-07-31..10-07), plus 40 off-season days on disk.
                If NYC shows the same summer NO bias that fades in mid-September, the KXRAIN decay was weather.
T2 launch       Rule R_LW1: taker NO on every market of a new daily product whose NO price (1 - yes_bid) is in
                [0.30, 0.90], decision at scheduled close - 10 h, fill at the first quote at or after t + 60 s.
                Treatment = event closes < 28 days after the series' first close (weeks 1-4); control = weeks 5+.
                New products launched inside the live /markets window (2026-08-06..10-01): KXSOFRD, KXAAAGASD<state>
                (5 states, staggered launches), KXTXERCOTPEAKD, KXTRUMPAPPROVE, KXBIGGESTQUAKE, KXVHGCB,
                KXTRUTHSOCIALD. New CITIES of mature weather products (KXLOWT/KXHIGHT SAN, EWR, TTN, SDF) are a
                separate stratum. KXRAIN (2026-07-15) is the in-sample motivating case and is only reported.

Fill realism: 1-minute candles -> last candle <= t + 60 s, at most 30 min old (lab convention); hourly candles -> the
candle ending in [t + 60 s, t + 60 min]. Fee 0.07 p (1 - p) per contract rounded up per 10-contract order. Early-
closing markets (KXBIGGESTQUAKE) use the scheduled end of day and are skipped if already closed at the fill time.
Statistics: equal-$ return per trade, t clustered by product x date (CR1) and by series x date.
T3 (secondary, added after T2 showed books of 15-60c in launch weeks): one resting maker order per market (sell or buy
                YES, 1c inside the best quote or at the mid) posted at close - 10 h, resting 6 h, filled only on a strict
                trade-through in the hourly trade high/low (re-parsed from the cached batch responses), maker fee 0.
No Kalshi calls here (disk only). Usage: .venv/bin/python -m lab.kalshi.strategies.r2_launch_window [t1|t2|t3|all]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data/kalshi_lab/strategies/r2_launch_window"
D = 86400

PRODUCT = {**{f"KXAAAGASD{s}": "GAS_STATE" for s in ("FL", "NC", "SC", "OH", "CA")},
           "KXSOFRD": "SOFR", "KXTXERCOTPEAKD": "ERCOT", "KXTRUMPAPPROVE": "TRUMPAPPROVE", "KXVHGCB": "VHGCB",
           "KXTRUTHSOCIALD": "TRUTHSOCIAL", "KXBIGGESTQUAKE": "QUAKE"}
NEWCITY = {f"KXLOWT{c}": "NEWCITY_LOW" for c in ("SAN", "EWR", "TTN", "SDF")}
NEWCITY.update({f"KXHIGHT{c}": "NEWCITY_HIGH" for c in ("SAN", "EWR", "TTN", "SDF")})


def fee_pc(p: float, n: int = 10) -> float:
    return math.ceil(round(100 * 0.07 * p * (1 - p) * n, 6)) / 100 / n


def q_min(c: list, t: int, max_age: int = 1800):
    """1-minute candles: (ask, bid) of the last candle <= t, at most max_age old."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > max_age or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


def q_hour(c: list, t: int):
    """Hourly candles: (ask, bid) of the first candle ending in [t, t + 3600 - 60] (t is already decision + 60 s)."""
    for r in c:
        if t <= r[0] <= t + 3540:
            return (r[1], r[2]) if r[1] is not None and r[2] is not None else None
        if r[0] > t + 3540:
            break
    return None


def jl(p: Path):
    return [json.loads(l) for l in p.open()] if p.exists() else []


# ------------------------------------------------------------------ loaders: rows = dict(product, series, e, t, S, close, won_yes, c, kind)
def load_launch() -> list[dict]:
    ms = {m["t"]: m for m in jl(OUT / "markets.jsonl")}
    cd = {x["t"]: sorted(x["c"]) for x in jl(OUT / "candles_h.jsonl")}
    out = []
    for t, m in ms.items():
        if m["series"] not in PRODUCT or t not in cd or m["result"] not in ("yes", "no"):
            continue
        out.append({"product": PRODUCT[m["series"]], "series": m["series"], "e": m["e"], "t": t, "S": m["close"], "close": m["close"],
                    "won_yes": m["result"] == "yes", "c": cd[t], "kind": "h", "vol": m["vol"]})
    qd = ROOT / "data/kalshi_lab/strategies/quake_entertain"
    qm = {m["t"]: m for m in jl(qd / "quake_markets.jsonl")}
    qc = {x["t"]: sorted(x["c"]) for x in jl(qd / "quake_candles.jsonl")}
    for t, m in qm.items():
        if t in qc and m["result"] in ("yes", "no"):
            out.append({"product": "QUAKE", "series": "KXBIGGESTQUAKE", "e": m["e"], "t": t, "S": m["exp"], "close": m["close"],
                        "won_yes": m["result"] == "yes", "c": qc[t], "kind": "m", "vol": m["vol"]})
    return out


def load_newcity() -> list[dict]:
    out = []
    wl = ROOT / "data/kalshi_lab/strategies/weather_lows"
    ms = {m["t"]: m for m in jl(wl / "markets.jsonl") if m["series"] in NEWCITY}
    for x in jl(wl / "candles_h.jsonl"):
        m = ms.get(x["t"])
        if m and m["result"] in ("yes", "no"):
            out.append({"product": NEWCITY[m["series"]], "series": m["series"], "e": m["e"], "t": m["t"], "S": m["close"], "close": m["close"],
                        "won_yes": m["result"] == "yes", "c": sorted(x["c"]), "kind": "h", "vol": m["vol"]})
    mk = json.load((ROOT / "data/lab/us/kalshi/markets.json").open())
    for tk, m in mk.items():
        if m["series"] not in NEWCITY or m.get("result") not in ("yes", "no"):
            continue
        f = ROOT / f"data/lab/us/kalshi/{tk}.json"
        if not f.exists():
            continue
        try:
            raw = json.load(f.open())
        except Exception:
            continue
        c = []
        for r in raw:
            try:
                c.append([r["end_period_ts"], float(r["yes_ask"]["close_dollars"]), float(r["yes_bid"]["close_dollars"])])
            except (KeyError, TypeError, ValueError):
                pass
        ct = int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        out.append({"product": NEWCITY[m["series"]], "series": m["series"], "e": m["event_ticker"], "t": tk, "S": ct, "close": ct,
                    "won_yes": m["result"] == "yes", "c": sorted(c), "kind": "m", "vol": float(m.get("volume_fp") or 0)})
    return out


def load_kxrain() -> list[dict]:
    from lab.kalshi.strategies.lead_round1 import load_rain
    mk, cd = load_rain()
    return [{"product": "KXRAIN", "series": "KXRAIN", "e": m["e"], "t": t, "S": m["close"], "close": m["close"],
             "won_yes": m["result"] == "yes", "c": cd[t], "kind": "m", "vol": m.get("vol", 0)} for t, m in mk.items()]


# ------------------------------------------------------------------ trades
def first_close(rows: list[dict]) -> dict:
    f = {}
    for r in rows:
        f[r["series"]] = min(f.get(r["series"], r["S"]), r["S"])
    return f


def quote_at(r: dict, t: int):
    return q_min(r["c"], t) if r["kind"] == "m" else q_hour(r["c"], t)


def make_trades(rows: list[dict], tau_h: float = 10, lo: float = 0.30, hi: float = 0.90, side: str = "NO", first=None) -> list[dict]:
    first = first or first_close(rows)
    out = []
    for r in rows:
        t = int(r["S"] - tau_h * 3600) + 60
        if r["close"] <= t:          # already closed (early close) at the fill time: not tradable
            continue
        q = quote_at(r, t)
        if not q:
            continue
        ask, bid = q
        px = round(1 - bid, 2) if side == "NO" else round(ask, 2)
        if not (lo <= px <= hi):
            continue
        win = (not r["won_yes"]) if side == "NO" else r["won_yes"]
        ret = ((1.0 if win else 0.0) - px - fee_pc(px)) / px
        age = (r["S"] - first[r["series"]]) / D
        day = dt.datetime.fromtimestamp(r["S"] - 12 * 3600, dt.timezone.utc).strftime("%Y-%m-%d")
        out.append({"product": r["product"], "series": r["series"], "e": r["e"], "t": r["t"], "S": r["S"], "age": age,
                    "week": int(age // 7) + 1, "px": px, "won": win, "ret": ret, "spread": round(ask - bid, 2),
                    "cl_pd": f"{r['product']}|{day}", "cl_sd": f"{r['series']}|{day}", "vol": r.get("vol", 0)})
    return out


def clustered(rows: list[dict], key: str = "cl_pd") -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    m = sum(r["ret"] for r in rows) / n
    by = defaultdict(float)
    for r in rows:
        by[r[key]] += r["ret"] - m
    g = len(by)
    v = sum(s * s for s in by.values()) / n ** 2 * g / (g - 1) if g > 1 else 0
    rs = sorted((r["ret"] for r in rows), reverse=True)
    return {"n": n, "clusters": g, "events": len({r["e"] for r in rows}), "win": round(sum(r["won"] for r in rows) / n, 3),
            "avg_px": round(st.mean(r["px"] for r in rows), 3), "ret": round(m, 4), "t": round(m / math.sqrt(v), 2) if v > 0 else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if n > 3 else None}


def full_stats(rows: list[dict]) -> dict:
    s = clustered(rows, "cl_pd")
    if not rows:
        return s
    s["t_series_date"] = clustered(rows, "cl_sd")["t"]
    srt = sorted(rows, key=lambda r: r["S"]); mid = srt[len(srt) // 2]["S"]
    h1 = [r["ret"] for r in rows if r["S"] < mid]; h2 = [r["ret"] for r in rows if r["S"] >= mid]
    s["half1"] = round(st.mean(h1), 4) if h1 else None; s["half2"] = round(st.mean(h2), 4) if h2 else None
    span = (max(r["S"] for r in rows) - min(r["S"] for r in rows)) / D + 1
    s["trades_per_day"] = round(len(rows) / span, 2)
    return s


def diff_test(a: list[dict], b: list[dict]) -> dict:
    """Treatment minus control, clusters independent (different dates or products): SE^2 = SE_a^2 + SE_b^2."""
    def se(rows):
        n = len(rows); m = sum(r["ret"] for r in rows) / n
        by = defaultdict(float)
        for r in rows:
            by[r["cl_pd"]] += r["ret"] - m
        g = len(by)
        return m, math.sqrt(sum(s * s for s in by.values()) / n ** 2 * g / (g - 1)) if g > 1 else float("nan")
    if len(a) < 5 or len(b) < 5:
        return {"diff": None}
    ma, sa = se(a); mb, sb = se(b)
    d = ma - mb; s = math.sqrt(sa ** 2 + sb ** 2)
    return {"treat": round(ma, 4), "control": round(mb, 4), "diff": round(d, 4), "t": round(d / s, 2) if s > 0 else None}


def by_week(rows_all: list[dict], tr: list[dict]) -> dict:
    out = {}
    for w in sorted({r["week"] for r in tr}):
        x = [r for r in tr if r["week"] == w]
        out[f"w{w}"] = {k: v for k, v in clustered(x).items() if k in ("n", "clusters", "ret", "t", "win", "avg_px")}
    return out


def spread_by_week(rows: list[dict], tau_h: float = 10) -> dict:
    """Share of decision-time quotes (all markets with 0.03 <= mid <= 0.97) whose spread is >= 6c, by week since launch."""
    first = first_close(rows); agg = defaultdict(lambda: [0, 0])
    for r in rows:
        t = int(r["S"] - tau_h * 3600) + 60
        if r["close"] <= t:
            continue
        q = quote_at(r, t)
        if not q:
            continue
        ask, bid = q; mid = (ask + bid) / 2
        if not (0.03 <= mid <= 0.97):
            continue
        w = int(((r["S"] - first[r["series"]]) / D) // 7) + 1
        k = f"w{min(w, 9)}"
        agg[k][1] += 1; agg[k][0] += (ask - bid) >= 0.06 - 1e-9
    return {k: {"n": n, "share_ge6c": round(a / n, 3)} for k, (a, n) in sorted(agg.items())}


# ------------------------------------------------------------------ T1: KXRAINNYC seasonality
def t1() -> dict:
    mk = {m["t"]: m for m in jl(ROOT / "data/kalshi_lab/strategies/rain_history/markets.jsonl") if m["series"] == "KXRAINNYC"}
    cd = {x["t"]: sorted(x["c"]) for x in jl(OUT / "nyc_candles.jsonl")}
    for x in jl(ROOT / "data/kalshi_lab/strategies/rain_history/candles.jsonl"):
        if x["t"].startswith("KXRAINNYC") and x["t"] not in cd:
            cd[x["t"]] = sorted(x["c"])

    def period(t: str) -> str:
        d = dt.datetime.strptime(t.split("-")[1], "%y%b%d").date()
        md = d.strftime("%m-%d"); y = d.year
        if "07-31" <= md <= "08-07":
            p = "P1"
        elif "08-08" <= md <= "08-22":
            p = "P2"
        elif "08-23" <= md <= "09-13":
            p = "P3"
        elif "09-14" <= md <= "10-07":
            p = "P4"
        else:
            p = "OFF"
        return f"{y}-{p}"
    cells = defaultdict(list); spread = defaultdict(lambda: [0, 0]); yes = defaultdict(list)
    for t, m in mk.items():
        if t not in cd or m["result"] not in ("yes", "no"):
            continue
        P = period(t); c = cd[t]
        for h in (14, 10, 6):
            q = q_min(c, m["close"] - h * 3600 + 60)
            if not q:
                continue
            ask, bid = q
            if h == 10:
                spread[P][1] += 1; spread[P][0] += (ask - bid) >= 0.06 - 1e-9
            pn = round(1 - bid, 2)
            row = {"e": m["e"], "S": m["close"], "cl_pd": m["e"], "cl_sd": m["e"], "px": pn, "won": m["result"] == "no"}
            if 0.05 <= pn <= 0.95:
                cells[(P, h)].append({**row, "ret": ((m["result"] == "no") - pn - fee_pc(pn)) / pn})
            if 0.05 <= ask <= 0.95 and h == 10:
                yes[P].append({**row, "px": ask, "won": m["result"] == "yes", "ret": ((m["result"] == "yes") - ask - fee_pc(ask)) / ask})
    res = {"no_by_period": {f"{P}|tau{h}h": clustered(v) for (P, h), v in sorted(cells.items())},
           "yes_tau10h_by_period": {P: clustered(v) for P, v in sorted(yes.items())},
           "share_spread_ge6c_tau10h": {P: {"n": n, "share": round(a / n, 3)} for P, (a, n) in sorted(spread.items()) if n},
           "markets_with_candles": len([t for t in mk if t in cd])}
    s25 = [r for (P, h), v in cells.items() if h == 10 and P in ("2025-P1", "2025-P2", "2025-P3") for r in v]
    p4 = cells.get(("2025-P4", 10), [])
    res["summer_2025_P1toP3_tau10h"] = clustered(s25)
    res["autumn_2025_P4_tau10h"] = clustered(p4)
    off = [r for (P, h), v in cells.items() if h == 10 and P.endswith("OFF") for r in v]
    res["offseason_tau10h"] = clustered(off)
    if len(s25) > 4 and len(p4) > 4:
        res["summer_minus_P4"] = diff_test(s25, p4)
    vol = [m["vol"] for t, m in mk.items() if period(t).startswith("2025-P")]
    res["median_market_volume_2025_P1toP4"] = st.median(vol) if vol else None
    (OUT / "t1_nyc_seasonality.json").write_text(json.dumps(res, indent=1))
    return res


# ------------------------------------------------------------------ T2: launch window
def split(tr: list[dict], frac: float = 0.7):
    ev = sorted({(r["S"], r["e"]) for r in tr}); cut = ev[int(len(ev) * frac)][0] if ev else 0
    return [r for r in tr if r["S"] < cut], [r for r in tr if r["S"] >= cut], cut


def t2() -> dict:
    L = load_launch(); NC = load_newcity(); KR = load_kxrain()
    fL, fN, fK = first_close(L), first_close(NC), first_close(KR)
    res = {"universe": {s: {"first_close": dt.datetime.utcfromtimestamp(f).strftime("%Y-%m-%d"),
                            "markets_with_candles": sum(r["series"] == s for r in L + NC)} for s, f in {**fL, **fN}.items()}}
    variants = 0
    grid = [(10, 0.30, 0.90), (6, 0.30, 0.90), (14, 0.30, 0.90), (10, 0.60, 0.95), (10, 0.05, 0.95)]
    # every-event split over the NEW-PRODUCT universe (both arms), by event close time
    base = make_trades(L, *grid[0], first=fL)
    _, _, cut = split(base)
    res["split_cut_utc"] = dt.datetime.utcfromtimestamp(cut).isoformat()
    tabs = {}
    for tau, lo, hi in grid:
        for side in ("NO", "YES"):
            if side == "YES":
                lo2, hi2 = round(1 - hi, 2), round(1 - lo, 2)
            else:
                lo2, hi2 = lo, hi
            tr = make_trades(L, tau, lo2, hi2, side, first=fL)
            disc = [r for r in tr if r["S"] < cut]; val = [r for r in tr if r["S"] >= cut]
            key = f"{side}|tau{tau}h|{lo2:.2f}-{hi2:.2f}"
            cell = {}
            for nm, rows in (("disc", disc), ("val", val), ("all", tr)):
                T = [r for r in rows if r["week"] <= 4]; C = [r for r in rows if r["week"] >= 5]
                cell[nm] = {"treat_w1to4": full_stats(T), "control_w5plus": full_stats(C), "diff": diff_test(T, C)}
                variants += 2 if nm == "disc" else 0
            cell["by_week_all"] = by_week(L, tr)
            cell["by_product_all"] = {p: {"w1to4": clustered([r for r in tr if r["product"] == p and r["week"] <= 4]),
                                          "w5plus": clustered([r for r in tr if r["product"] == p and r["week"] >= 5])}
                                      for p in sorted({r["product"] for r in tr})}
            tabs[key] = cell
    res["new_products"] = tabs
    res["spread_by_week_new_products"] = spread_by_week(L)
    # new-city stratum and in-sample KXRAIN, frozen rule only
    for nm, rows, f in (("new_cities", NC, fN), ("kxrain_in_sample", KR, fK)):
        tr = make_trades(rows, 10, 0.30, 0.90, "NO", first=f)
        T = [r for r in tr if r["week"] <= 4]; C = [r for r in tr if r["week"] >= 5]
        res[nm] = {"treat_w1to4": full_stats(T), "control_w5plus": full_stats(C), "diff": diff_test(T, C), "by_week": by_week(rows, tr),
                   "spread_by_week": spread_by_week(rows)}
        variants += 1
    # same-date control for new cities: mature KXLOWT / KXHIGH cities on the dates the new cities were in weeks 1-4
    res["variants_examined_t2"] = variants
    (OUT / "t2_launch.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def show(d, ind=""):
    for k, v in d.items():
        if isinstance(v, dict) and not ({"n", "ret"} <= set(v) or {"diff"} <= set(v)):
            print(f"{ind}{k}:"); show(v, ind + "  ")
        else:
            print(f"{ind}{k}: {v}")


# ------------------------------------------------------------------ T3 (secondary, added after T2): resting maker orders in new products
def load_launch_full() -> dict:
    """Hourly candles with trade prices, re-parsed from the cached batch responses (no new calls):
    ticker -> [[end_ts, yes_ask, yes_bid, trade_high, trade_low, volume], ...]"""
    import glob
    out = {}
    g = lambda c, k, s: (float(c[k][s]) if (c.get(k) or {}).get(s) is not None else None)
    for f in glob.glob(str(OUT / "api_cache" / "*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for m in d.get("markets") or []:
            if "candlesticks" not in m:
                continue
            tk = m.get("market_ticker") or m.get("ticker")
            out[tk] = sorted([[int(c["end_period_ts"]), g(c, "yes_ask", "close_dollars"), g(c, "yes_bid", "close_dollars"),
                               g(c, "price", "high_dollars"), g(c, "price", "low_dollars"), float(c.get("volume_fp") or 0)]
                              for c in m["candlesticks"]])
    return out


def maker_trades(rows: list[dict], full: dict, side: str, price: str, tau_h: float = 10, rest_to_h: float = 4, first=None) -> list[dict]:
    """Post one resting order at the first hourly quote at or after S - tau_h + 60 s; it rests until S - rest_to_h.
    side NO = sell YES at a (we hold NO at cost 1 - a); side YES = buy YES at b.
    price join1 = 1c inside the best quote; mid = the midpoint (rounded to the cent, kept strictly inside the spread).
    Filled only on a strict trade-through: a later hourly candle with trade high >= a + 0.01 (NO) or low <= b - 0.01 (YES).
    Maker fee 0 ('quadratic' series). Adverse selection is in the sample by construction (only filled orders count)."""
    first = first or first_close(rows)
    out = []
    for r in rows:
        c = full.get(r["t"])
        if not c:
            continue
        t = int(r["S"] - tau_h * 3600) + 60
        post = next((x for x in c if t <= x[0] <= t + 3540), None)
        if not post or post[1] is None or post[2] is None:
            continue
        ask, bid = post[1], post[2]
        if ask - bid < 0.03 - 1e-9:
            continue
        if side == "NO":
            a = round(ask - 0.01, 2) if price == "join1" else max(round((ask + bid) / 2 + 0.004, 2), round(bid + 0.01, 2))
            if not (0.10 <= a <= 0.90):
                continue
            fill = any(x[3] is not None and x[3] >= a + 0.01 - 1e-9 for x in c if post[0] < x[0] <= r["S"] - rest_to_h * 3600)
            cost = round(1 - a, 2); win = not r["won_yes"]
        else:
            b = round(bid + 0.01, 2) if price == "join1" else min(round((ask + bid) / 2 - 0.004, 2), round(ask - 0.01, 2))
            if not (0.10 <= b <= 0.90):
                continue
            fill = any(x[4] is not None and x[4] <= b - 0.01 + 1e-9 for x in c if post[0] < x[0] <= r["S"] - rest_to_h * 3600)
            cost = b; win = r["won_yes"]
        if not fill:
            continue
        age = (r["S"] - first[r["series"]]) / D
        day = dt.datetime.fromtimestamp(r["S"] - 12 * 3600, dt.timezone.utc).strftime("%Y-%m-%d")
        out.append({"product": r["product"], "series": r["series"], "e": r["e"], "t": r["t"], "S": r["S"], "age": age, "week": int(age // 7) + 1,
                    "px": cost, "won": win, "ret": ((1.0 if win else 0.0) - cost) / cost, "spread": round(ask - bid, 2),
                    "cl_pd": f"{r['product']}|{day}", "cl_sd": f"{r['series']}|{day}"})
    return out


def t3() -> dict:
    L = [r for r in load_launch() if r["kind"] == "h"]   # quake (1-min, no trade prices stored) excluded
    full = load_launch_full(); fL = first_close(L)
    base = make_trades(L, 10, 0.30, 0.90, "NO", first=fL)
    _, _, cut = split(base)
    res = {"cut": dt.datetime.utcfromtimestamp(cut).isoformat(), "cells": {}}
    posted = defaultdict(int)
    for side in ("NO", "YES"):
        for price in ("join1", "mid"):
            tr = maker_trades(L, full, side, price, first=fL)
            k = f"MAKER_{side}_{price}_tau10h_rest6h"
            cell = {}
            # discovery only: every variant lost on discovery, so no maker candidate was frozen and validation is not evaluated
            for nm, rows in (("disc", [r for r in tr if r["S"] < cut]),):
                T = [r for r in rows if r["week"] <= 4]; C = [r for r in rows if r["week"] >= 5]
                cell[nm] = {"treat_w1to4": full_stats(T), "control_w5plus": full_stats(C)}
            cell["by_product_w1to4_disc"] = {p: clustered([r for r in tr if r["product"] == p and r["week"] <= 4 and r["S"] < cut]) for p in sorted({r["product"] for r in tr})}
            res["cells"][k] = cell
    (OUT / "t3_maker.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("t1", "all"):
        show(t1())
    if what in ("t2", "all"):
        r = t2()
        show({k: v for k, v in r.items() if k != "new_products"})
        show({"R_LW1 NO|tau10h|0.30-0.90": r["new_products"]["NO|tau10h|0.30-0.90"]})
    if what in ("t3", "all"):
        show(t3())
