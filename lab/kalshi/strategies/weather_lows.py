"""weather_lows: Kalshi daily LOW temperature ladders (KXLOWT*, 24 US cities), observation rules and an ASOS
climatology model vs the market, hourly decision grid, taker fills.

Facts verified on the data (2026-08-03..2026-10-07, 1,517 city-days):
  * every market closes at 00:00 local STANDARD time (01:00 daylight) = the end of the NWS climate day; trading runs
    through the whole climate day, so the minimum is locked only when trading stops;
  * official minimum == the running upper bound U from ASOS rows on 94.6% of days, 1F lower on 3.7%.
Execution: decision at H - 1 min with ASOS rows valid <= H - 6 min; fill at the hourly-candle close quote at H (YES at
yes_ask, NO at 1 - yes_bid), only if a candle ends exactly at H (quote age 0); fee 0.07 p (1-p) rounded up to the cent
on a 10-contract order; one trade per market (first qualifying hour). Split: events by close time, first 70% discovery.
Usage: python -m lab.kalshi.strategies.weather_lows [explore|disc|val]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import weather_lows_obs as O
from lab.kalshi.strategies import weather_lows_model as Mo
from lab.kalshi.strategies.weather_lows_data import SERIES, MF, CF
from lab.kalshi.strategies.weather_lows_fetch import OUT, calls_used

SPLIT = 0.7


def fee10(p: float) -> float:
    """per-contract fee on a 10-contract taker order, rounded up to the cent per order."""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def interval(m):
    if m["type"] == "less":
        return -1e9, float(m["cap"]) - 1
    if m["type"] == "greater":
        return float(m["floor"]) + 1, 1e9
    return float(m["floor"]), float(m["cap"])


def load_rows():
    ms = [json.loads(l) for l in MF.open()]
    cand = {}
    for l in CF.open():
        try:
            x = json.loads(l)
        except Exception:
            continue   # a line being written by a running fetch
        cand[x["t"]] = {r[0]: (r[1], r[2], r[5]) for r in x["c"]}
    ev = defaultdict(list)
    for m in ms:
        ev[m["e"]].append(m)
    obs_cache = {}; rows = []
    for e, mk in ev.items():
        s0 = mk[0]["series"]; stn, tz = SERIES[s0]
        day = dt.datetime.strptime(e.split("-")[1], "%y%b%d").strftime("%Y-%m-%d")
        d0, d1 = O.day_bounds(day, tz); close = mk[0]["close"]
        if stn not in obs_cache:
            obs_cache[stn] = O.load(stn, tz)
        obs = obs_cache[stn].get(day, [])
        for k in range(1, 24):
            H = d0 + k * 3600
            if H >= close:
                continue
            s = O.state(obs, H - 60, d0, lag=Mo.LAG - 60)
            if not s or s["cur_t"] is None or H - s["cur_t"] > 90 * 60:
                continue
            for m in mk:
                q = cand.get(m["t"], {}).get(H)
                if not q or q[0] is None or q[1] is None:
                    continue
                lo, hi = interval(m)
                rows.append({"e": e, "t": m["t"], "stn": stn, "day": day, "k": k, "close": close, "lo": lo, "hi": hi, "U": s["U"], "cur": s["cur"],
                             "L6": s["L6"], "ask": q[0], "bid": q[1], "vol": q[2], "won": m["result"] == "yes"})
    return rows, obs_cache


def cut_time(rows=None):
    """first close time of the VALIDATION events: all settled events (markets file, independent of candles) by close."""
    evc = sorted({(m["close"], m["e"]) for m in map(json.loads, MF.open())})
    return evc[int(len(evc) * SPLIT)][0]


def trade(r, side):
    px = r["ask"] if side == "YES" else 1 - r["bid"]
    w = r["won"] if side == "YES" else not r["won"]
    return {**r, "side": side, "px": px, "win": w, "ret": ((1.0 if w else 0.0) - px - fee10(px)) / px}


def stats(tr):
    if not tr:
        return {"n": 0}
    ev = defaultdict(list)
    for x in tr:
        ev[x["e"]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))) if len(em) > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((x["ret"] for x in tr), reverse=True)
    return {"n": len(tr), "events": len(em), "win": round(sum(x["win"] for x in tr) / len(tr), 3), "avg_px": round(st.mean(x["px"] for x in tr), 3),
            "ret": round(st.mean(rs), 4), "t": round(t, 2), "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None}


def first_per_market(cands):
    best = {}
    for x in sorted(cands, key=lambda x: x["k"]):
        best.setdefault(x["t"], x)
    return list(best.values())


def build(fit_before="2026-08-02"):
    """rows with the climatology model probability p_mod (fit on ASOS days before fit_before, months 6-11 only)
    and the per-station measurement term (delta) estimated on DISCOVERY Kalshi days only."""
    rows, obs_cache = load_rows()
    cut = cut_time(rows)
    stations = {stn: (obs_cache[stn], SERIES[s][1]) for s, (stn, _) in SERIES.items() if stn in obs_cache}
    drops, pooled = Mo.fit(stations, fit_before)
    # delta: official = U_end - delta, from discovery events only (U_end = state at the close, lag 0)
    ms = [json.loads(l) for l in MF.open()]
    dcount = defaultdict(lambda: defaultdict(int))
    seen = set()
    for m in ms:
        if m["e"] in seen or m["close"] >= cut:
            continue
        seen.add(m["e"]); stn, tz = SERIES[m["series"]]
        day = dt.datetime.strptime(m["e"].split("-")[1], "%y%b%d").strftime("%Y-%m-%d"); d0, d1 = O.day_bounds(day, tz)
        s = O.state(obs_cache[stn].get(day, []), d1, d0, lag=0)
        try:
            off = int(s["U"] - float(m["xv"]))
        except Exception:
            continue
        off = max(-1, min(2, off))
        dcount[stn][off] += 1; dcount["ALL"][off] += 1
    def dl(stn):
        a = dcount.get(stn, {}); b = dcount["ALL"]; na = sum(a.values()); nb = sum(b.values())
        return {d: (a.get(d, 0) + 20 * b.get(d, 0) / nb) / (na + 20) for d in (-1, 0, 1, 2)}
    DL = {stn: dl(stn) for stn in stations}
    for r in rows:
        r["p_mod"] = Mo.p_bucket(drops, pooled, r["stn"], r["k"], r["U"], r["cur"], r["lo"], r["hi"], DL[r["stn"]])
        r["disc"] = r["close"] < cut
    return rows, cut, DL


def variants():
    """The pre-declared grid (every variant counts toward variants_examined)."""
    V = []
    for g in (1, 2):
        for cap in (1.0, 0.97, 0.93):
            V.append((f"DEAD g{g} NOpx<={cap}", "NO", lambda r, g=g, cap=cap: r["lo"] >= r["U"] + g and 1 - r["bid"] <= cap))
    for K in (16, 19, 21, 22):
        for G in (3, 5, 7, 10):
            for A in (0.95, 0.90):
                V.append((f"LOCKYES k>={K} gap>={G} ask<={A}", "YES",
                          lambda r, K=K, G=G, A=A: r["k"] >= K and r["cur"] - r["U"] >= G and r["lo"] <= r["U"] <= r["hi"] and r["ask"] <= A))
    for J in (2, 4):
        for K in (19, 21, 22):
            for G in (1, 2, 3, 5):
                for P in (0.97, 0.92):
                    V.append((f"BELOWNO hi>=U-{J} k>={K} gap>={G} NOpx<={P}", "NO",
                              lambda r, J=J, K=K, G=G, P=P: r["hi"] <= r["U"] - 1 and r["hi"] >= r["U"] - J and r["k"] >= K and r["cur"] - r["U"] >= G and 1 - r["bid"] <= P))
    for E in (0.05, 0.10, 0.20):
        for K in (1, 16):
            V.append((f"MODEL YES edge>={E} k>={K}", "YES", lambda r, E=E, K=K: r["k"] >= K and r["p_mod"] - r["ask"] - fee10(r["ask"]) >= E))
            V.append((f"MODEL NO edge>={E} k>={K}", "NO", lambda r, E=E, K=K: r["k"] >= K and (1 - r["p_mod"]) - (1 - r["bid"]) - fee10(1 - r["bid"]) >= E))
    return V


def run_variant(rows, side, cond):
    return first_per_market([trade(r, side) for r in rows if 0.01 <= (r["ask"] if side == "YES" else 1 - r["bid"]) <= 0.99 and cond(r)])


def halves(tr):
    if not tr:
        return None, None
    cs = sorted({x["close"] for x in tr}); mid = cs[len(cs) // 2]
    a = [x["ret"] for x in tr if x["close"] < mid]; b = [x["ret"] for x in tr if x["close"] >= mid]
    return (round(st.mean(a), 4) if a else None), (round(st.mean(b), 4) if b else None)


def main(mode="disc"):
    rows, cut, DL = build()
    D = [r for r in rows if r["disc"]]; Vd = [r for r in rows if not r["disc"]]
    V = variants(); res = []
    for name, side, cond in V:
        s = stats(run_variant(D, side, cond)); s["rule"] = name; res.append(s)
    res.sort(key=lambda s: -(s.get("t") if s.get("n", 0) >= 40 and s.get("t") == s.get("t") else -99))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "discovery.json").write_text(json.dumps({"cut": cut, "n_variants": len(V), "results": res}, indent=1))
    print("variants", len(V), "| discovery events", len({r['e'] for r in D}), "| validation events", len({r['e'] for r in Vd}))
    for s in res[:15]:
        print(s)
    return rows, res


def validate():
    """Run the 3 frozen candidates once on discovery and validation; write result rows + trades."""
    fz = json.loads((OUT / "frozen.json").read_text())["candidates"]
    rows, cut, DL = build()
    V = {name: (side, cond) for name, side, cond in variants()}
    out = {"cut": cut, "kalshi_calls_used": calls_used(), "candidates": []}
    days_val = len({r["day"] for r in rows if not r["disc"]})
    for name in fz:
        side, cond = V[name]
        res = {"rule": name}
        for part, R in (("discovery", [r for r in rows if r["disc"]]), ("validation", [r for r in rows if not r["disc"]])):
            tr = run_variant(R, side, cond); s = stats(tr); h1, h2 = halves(tr)
            s.update({"half1": h1, "half2": h2, "trades_per_day": round(len(tr) / max(1, len({r['day'] for r in R})), 2),
                      "median_hour_volume": (sorted(x["vol"] for x in tr)[len(tr) // 2] if tr else None)})
            res[part] = s
            if part == "validation":
                (OUT / f"trades_{name.replace(' ', '_').replace('>=', 'ge').replace('<=', 'le')}.json").write_text(
                    json.dumps([{k: x[k] for k in ("e", "t", "k", "U", "cur", "lo", "hi", "px", "win", "ret", "vol")} for x in tr]))
        out["candidates"].append(res); print(json.dumps(res))
    (OUT / "validation.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "disc"
    if mode == "disc":
        main()
    elif mode == "val":
        validate()
