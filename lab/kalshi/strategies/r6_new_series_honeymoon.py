"""r6_new_series_honeymoon: are newly launched recurring Kalshi series mispriced in their first 28 days?

Part A (0 calls, this file): every launch whose first weeks are already on disk (r2 launch-window products, the four new
weather cities for highs and lows, KXRAIN, KXBIGGESTQUAKE), plus established weather cities as a same-date control.
  Arms:  H = new series, market opened < 28 d after the series' first market (outcome-free);
         M = same new series, opened >= 28 d after launch; E = established weather series (control).
  Quote: minute candles -> decide on the last candle <= t (<= 30 min old), fill at the last candle <= t + 60 s
         (<= 30 min old); hourly candles (filled every hour) -> decide and fill on the first hourly close >= t
         (t shifts by < 60 min; hourly data cannot resolve the 1-minute delay).
  Rules (taker, fee 0.07 p (1-p) rounded up per 10-lot):
         FAV[lo,hi]  buy the side the mid favours (YES if mid >= 0.5) at its taker price, if that price is in [lo, hi];
         LSNO[lo,hi] buy NO at 1 - yes_bid if in [lo, hi] (YES is a longshot);
         optional spread filter (ask - bid <= 6c).
  Times: tau = 12, 8, 6, 4 h before close (r2 hourly candles end at close - 5 h, so their tau 4 is missing).
  Split: new series ordered by launch date, same-day launches kept together; DISCOVERY = launches up to and including
         2026-08-28 (13 of 20 series, 65%), VALIDATION = launches from 2026-09-01 (7 series). Cells are chosen on
         discovery H only; at most 3 frozen cells are judged once on validation H.
  Stats: equal-$ return per trade, t clustered by event (series-day) and by family-date; mean without the 3 best
         EVENTS; one-sided 95% Beta lower bound on the event win rate turned into a per-$ bound at the mean price.
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_new_series_honeymoon [partA]
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import os
import statistics as st
import sys
from collections import defaultdict

from lab.kalshi.strategies.r6_new_series_honeymoon_data import ROOT, load_all

OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/r6_new_series_honeymoon")
TAUS = (12, 8, 6, 4)
HONEY_D = 28
MAX_AGE = 30 * 60
SPLIT_DATE = int(dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc).timestamp())   # launches before -> discovery
RULES = [("FAV", 0.55, 0.70), ("FAV", 0.70, 0.90), ("FAV", 0.55, 0.90), ("LSNO", 0.85, 0.97), ("LSNO", 0.90, 0.97)]
SPREADS = (None, 0.06)


def fee(p: float, lot: int = 10) -> float:
    """Taker fee per contract, Kalshi rounds the order's fee up to the cent (10-contract order)."""
    return math.ceil(round(0.07 * p * (1 - p) * lot * 100, 6)) / 100 / lot


def q_min(c: list, t: int):
    """Last minute-candle quote at or before t, if <= MAX_AGE old."""
    i = bisect.bisect_right(c, (t, 9.0, 9.0)) - 1
    if i < 0 or t - c[i][0] > MAX_AGE:
        return None
    return c[i][1], c[i][2], c[i][0]


def q_hour(c: list, t: int):
    """First hourly close at or after t (within 60 min)."""
    i = bisect.bisect_left(c, (t, -1.0, -1.0))
    if i >= len(c) or c[i][0] - t >= 3600:
        return None
    return c[i][1], c[i][2], c[i][0]


def decide(m: dict, tau: int):
    """(decision quote, fill quote) at close - tau hours, or None."""
    t = m["close"] - tau * 3600
    if t <= m["open"]:
        return None
    c = m["c"]
    if not c:
        return None
    if m["res"] == "min":
        d = q_min(c, t); f = q_min(c, t + 60)
    else:
        d = q_hour(c, t); f = d
    if not d or not f:
        return None
    return d, f


def take(rule, d, f):
    """Side and fill price if the rule fires on decision quote d and fills on quote f."""
    kind, lo, hi = rule
    a, b = d[0], d[1]
    if not (0 < b and a < 1 and a >= b):
        return None
    if kind == "FAV":
        side = "YES" if (a + b) / 2 >= 0.5 else "NO"
    else:
        side = "NO"
    fa, fb = f[0], f[1]
    px = fa if side == "YES" else 1 - fb
    pd = a if side == "YES" else 1 - b
    if not (lo <= pd <= hi) or not (0.01 <= px <= 0.99):
        return None
    if kind == "LSNO" and not (lo - 0.02 <= px <= 0.99):
        return None
    return side, round(px, 4)


def arm(m: dict) -> str | None:
    if m["grp"] == "EST":
        return "E"
    if m["grp"] == "RELAUNCH":
        return "R_H" if m["age_d"] < HONEY_D else "R_M"
    return "H" if m["age_d"] < HONEY_D else "M"


def period(m: dict) -> str | None:
    if m["grp"] != "NEW":
        return None
    return "disc" if m["launch"] < SPLIT_DATE else "val"


def gen_trades(rows: list[dict]) -> list[dict]:
    out = []
    for m in rows:
        a = arm(m)
        won_yes = m["result"] == "yes"
        day = dt.datetime.utcfromtimestamp(m["close"] - 6 * 3600).strftime("%Y-%m-%d")
        for tau in TAUS:
            q = decide(m, tau)
            if not q:
                continue
            d, f = q
            spread = round(d[0] - d[1], 4)
            for rule in RULES:
                x = take(rule, d, f)
                if not x:
                    continue
                side, px = x
                won = won_yes if side == "YES" else not won_yes
                ret = ((1.0 if won else 0.0) - px - fee(px)) / px
                for smax in SPREADS:
                    if smax is not None and spread > smax + 1e-9:
                        continue
                    out.append({"cell": f"{rule[0]}[{rule[1]:.2f},{rule[2]:.2f}]|tau{tau}|{'s<=6c' if smax else 'any'}",
                                "t": m["t"], "e": m["e"], "series": m["series"], "fam": m["fam"], "grp": m["grp"],
                                "arm": a, "per": period(m), "age_d": m["age_d"], "close": m["close"], "day": day,
                                "fd": f"{m['fam']}|{m['grp']}|{day}", "side": side, "px": px, "won": won, "ret": ret,
                                "spread": spread})
    return out


def beta_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound on a binomial rate, by bisection on the Binomial tail."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        tail = sum(math.comb(n, j) * mid ** j * (1 - mid) ** (n - j) for j in range(k, n + 1))
        if tail < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def tstat(groups: dict) -> float:
    em = [st.mean(v) for v in groups.values()]
    if len(em) < 3 or st.pstdev(em) == 0:
        return float("nan")
    return st.mean(em) / (st.stdev(em) / math.sqrt(len(em)))


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list); fd = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"]); fd[r["fd"]].append(r["ret"])
    rs = [r["ret"] for r in rows]
    best_ev = sorted(ev, key=lambda e: -st.mean(ev[e]))[:3]
    wo3 = [r["ret"] for r in rows if r["e"] not in best_ev]
    px = st.mean(r["px"] for r in rows)
    # event-level binomial bound: events won (mean event return > 0) is crude; use trade-level wins over unique events
    k = sum(r["won"] for r in rows); n = len(rows)
    ne = len(ev)
    kw = round(k / n * ne)
    lb_rate = beta_lower(kw, ne) if ne <= 400 else (k / n - 1.645 * math.sqrt(k / n * (1 - k / n) / ne))
    lb_ret = (lb_rate - px - fee(px)) / px
    closes = sorted(r["close"] for r in rows); mid = closes[len(closes) // 2]
    h1 = [r["ret"] for r in rows if r["close"] < mid]; h2 = [r["ret"] for r in rows if r["close"] >= mid]
    return {"n": n, "events": ne, "fd_clusters": len(fd), "win": round(k / n, 3), "avg_px": round(px, 3),
            "ret": round(st.mean(rs), 4), "t_event": round(tstat(ev), 2), "t_famdate": round(tstat(fd), 2),
            "ret_wo3ev": round(st.mean(wo3), 4) if wo3 else None, "beta_lb_ret": round(lb_ret, 4),
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "med_spread": round(st.median(r["spread"] for r in rows), 3)}


def spread_table(rows: list[dict]) -> dict:
    """Quoted spread at each tau, by family x arm (descriptive, every market with a quote)."""
    acc = defaultdict(list); two = defaultdict(lambda: [0, 0])
    for m in rows:
        a = arm(m)
        for tau in TAUS:
            q = decide(m, tau)
            if not q:
                continue
            ask, bid = q[0][0], q[0][1]
            k = (m["fam"], a, tau)
            two[k][1] += 1
            if 0 < bid and ask < 1:
                two[k][0] += 1; acc[k].append(ask - bid)
    out = {}
    for k, v in sorted(acc.items()):
        v.sort()
        out["|".join(map(str, k))] = {"quotes": two[k][1], "two_sided": round(two[k][0] / two[k][1], 3),
                                      "med_spread": round(v[len(v) // 2], 3), "share_ge6c": round(sum(x >= 0.06 for x in v) / len(v), 3),
                                      "share_le2c": round(sum(x <= 0.02 for x in v) / len(v), 3)}
    return out


def partA() -> dict:
    rows = load_all()
    T = gen_trades(rows)
    cells = sorted({r["cell"] for r in T})
    res = {"cells": len(cells), "spread": spread_table(rows)}
    # index once: (cell, arm, per, fam) -> trades
    ix = defaultdict(list)
    for r in T:
        ix[(r["cell"], r["arm"], r["per"], r["fam"])].append(r)
    fams = sorted({r["fam"] for r in T})

    def pick(c, a, per=None, fam=None, days=None):
        out = []
        for f in ([fam] if fam else fams):
            for p in ([per] if per else ("disc", "val", None)):
                out += ix.get((c, a, p, f), [])
        if days is not None:
            out = [r for r in out if r["day"] in days]
        return out
    # Discovery: H arm of discovery series, pooled over families; M (same series, mature) and E (established) as controls
    disc = {}
    for c in cells:
        H = pick(c, "H", "disc"); M = pick(c, "M", "disc")
        dates = {r["day"] for r in H}
        E = pick(c, "E", days=dates)
        sH, sM = stats(H), stats(M)
        disc[c] = {"H": sH, "M": sM, "E_same_dates": stats(E),
                   "H_minus_M": round(sH["ret"] - sM["ret"], 4) if sH.get("n") and sM.get("n") else None}
    res["discovery"] = disc
    # same table without KXRAIN (the in-sample case that motivated the family in round 1)
    res["discovery_ex_rain"] = {c: {"H": stats([r for r in pick(c, "H", "disc") if r["fam"] != "RAIN"]),
                                    "M": stats([r for r in pick(c, "M", "disc") if r["fam"] != "RAIN"])} for c in cells}
    # per family H vs M on discovery (diagnostic, every cell)
    fam = {}
    for c in cells:
        for f in fams:
            H = pick(c, "H", "disc", f); M = pick(c, "M", "disc", f); E = pick(c, "E", fam=f); R = pick(c, "R_H", fam=f)
            if H or M:
                fam[f"{f}|{c}"] = {"H": stats(H), "M": stats(M), "E_all": stats(E), "R_H": stats(R)}
    res["by_family_discovery"] = fam
    # by week since launch, pooled new series (all periods), for the broadest cell of each rule kind
    wk = {}
    for c in cells:
        if not c.endswith("any"):
            continue
        by = defaultdict(list)
        for r in pick(c, "H") + pick(c, "M"):
            by[min(int(r["age_d"] // 7) + 1, 9)].append(r)
        wk[c] = {f"w{w}": {k: v for k, v in stats(by[w]).items() if k in ("n", "events", "ret", "t_event", "avg_px", "med_spread")} for w in sorted(by)}
    res["by_week"] = wk
    res["_trades"] = T
    return res


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    res = partA()
    T = res.pop("_trades")
    with open(os.path.join(OUT, "partA_trades.jsonl"), "w") as fh:
        for r in T:
            fh.write(json.dumps(r) + "\n")
    json.dump(res, open(os.path.join(OUT, "partA.json"), "w"), indent=1)
    print("cells", res["cells"])
    print("\nSPREAD at tau (fam|arm|tau): quotes two_sided med_spread share>=6c share<=2c")
    for k, v in res["spread"].items():
        print(f"  {k:22s} {v['quotes']:6d} {v['two_sided']:.2f} {v['med_spread']:.3f} {v['share_ge6c']:.2f} {v['share_le2c']:.2f}")
    print("\nDISCOVERY H vs M vs E (pooled new series launched <= 08-28)")
    for c, v in sorted(res["discovery"].items(), key=lambda kv: -(kv[1]["H"].get("ret") or -9)):
        H, M, E = v["H"], v["M"], v["E_same_dates"]
        if not H.get("n"):
            continue
        print(f"  {c:34s} H n={H['n']:4d} ev={H['events']:3d} fd={H['fd_clusters']:3d} px={H['avg_px']:.2f} ret={H['ret']:+.3f} "
              f"t={H['t_event']:+.2f}/{H['t_famdate']:+.2f} wo3={H['ret_wo3ev']:+.3f} lb={H['beta_lb_ret']:+.3f} sp={H['med_spread']:.2f} | "
              f"M n={M.get('n',0):4d} ret={M.get('ret',float('nan')):+.3f} | E n={E.get('n',0):5d} ret={E.get('ret',float('nan')):+.3f}")


if __name__ == "__main__":
    main()
