"""Lead researcher, round 1 synthesis: two cross-family diagnostics that steer round 2 (no Kalshi calls, disk only).

D1 rain_bias   Is KXRAIN YES overpriced in a stable way? Unconditional taker NO / taker YES at fixed hours before the
               (fixed, local-midnight) close, split into the four contiguous periods round 1 used:
               P1 07-31..08-07 (microstructure archive), P2 08-08..08-22 (rain_history cache),
               P3 08-23..09-13 and P4 09-14..10-07 (lab candles; P4 = rain_history validation window).
               Fill = quote one minute after the decision, carried forward <= 30 min; fee 0.07p(1-p) rounded up per
               10-contract order. t clustered by event date (all cities of one date move together).
D2 young_wx    Are young / thin weather daily-high series worse calibrated than mature ones on the SAME dates?
               Mid at 14/10/6 h before close, logistic recalibration slope/intercept on logit(mid), Brier, spread,
               and pooled taker returns. Groups: legacy 7 series (2025-07 launch), new series in their first 21 days,
               new series after 21 days, and the 4 thin cities (SAN/EWR/TTN/SDF, median volume < 250).

Diagnostic only: nothing here is a frozen trading rule. Every cell is counted in variants_examined.
Usage: .venv/bin/python lab/kalshi/strategies/lead_round1.py [rain|wx|all]"""
from __future__ import annotations
import json, math, random, statistics as st, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/lead_round1"
OUT.mkdir(parents=True, exist_ok=True)
MAX_AGE = 30 * 60


def fee_pc(p: float, n: int = 10) -> float:
    """Per-contract fee for an n-contract order, rounded up to the cent per order."""
    return math.ceil(round(100 * 0.07 * p * (1 - p) * n, 6)) / 100 / n


def quote(c: list, t: int):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > MAX_AGE or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


def clustered(rows: list[tuple[str, float]]) -> dict:
    """rows = (cluster, ret). Mean of trades, cluster-robust t (CR1)."""
    n = len(rows)
    if n < 2:
        return {"n": n, "ret": rows[0][1] if rows else None, "t": None, "clusters": n}
    m = sum(r for _, r in rows) / n
    by = defaultdict(float)
    for c, r in rows:
        by[c] += r - m
    g = len(by)
    if g < 2:
        return {"n": n, "ret": round(m, 4), "t": None, "clusters": g}
    v = sum(s * s for s in by.values()) / n ** 2 * g / (g - 1)
    t = m / math.sqrt(v) if v > 0 else None
    return {"n": n, "clusters": g, "ret": round(m, 4), "t": round(t, 2) if t else None}


# ---------------------------------------------------------------- D1 rain
def load_rain():
    mk, cd = {}, {}
    srcs = [(ROOT / "data/kalshi_lab/strategies/microstructure/arch_markets.jsonl",
             ROOT / "data/kalshi_lab/strategies/microstructure/arch_candles.jsonl"),
            (ROOT / "data/kalshi_lab/strategies/rain_history/markets.jsonl",
             ROOT / "data/kalshi_lab/strategies/rain_history/candles.jsonl"),
            (ROOT / "data/kalshi_lab/markets/KXRAIN.jsonl", ROOT / "data/kalshi_lab/candles/KXRAIN.jsonl")]
    for fm, fc in srcs:
        for l in fm.open():
            x = json.loads(l)
            if x.get("series", "KXRAIN") == "KXRAIN" and x["t"].startswith("KXRAIN-") and x.get("result") in ("yes", "no"):
                mk[x["t"]] = x
        for l in fc.open():
            x = json.loads(l)
            if x["t"].startswith("KXRAIN-") and x.get("c"):
                cd[x["t"]] = sorted(x["c"])
    return {t: m for t, m in mk.items() if t in cd}, cd


def period(close: int) -> str:
    d = datetime.fromtimestamp(close - 6 * 3600, timezone.utc).strftime("%m-%d")
    if d < "07-31":
        return "P0"
    if d <= "08-07":
        return "P1"
    if d <= "08-22":
        return "P2"
    if d <= "09-13":
        return "P3"
    return "P4"


def rain() -> dict:
    mk, cd = load_rain()
    taus = (16, 12, 10, 8, 6, 4)
    cells = defaultdict(list)
    bands = defaultdict(list)
    spread_wide = defaultdict(lambda: [0, 0])
    first_city = {}
    for t, m in sorted(mk.items(), key=lambda kv: kv[1]["close"]):
        city = t.split("-")[-1]
        first_city.setdefault(city, m["close"])
    for t, m in mk.items():
        c, e, P = cd[t], m["e"], period(m["close"])
        if P == "P0":
            continue
        won_yes = m["result"] == "yes"
        young = (m["close"] - first_city[t.split("-")[-1]]) < 14 * 86400
        for h in taus:
            q = quote(c, m["close"] - h * 3600 + 60)
            if not q:
                continue
            ask, bid = q
            spread_wide[(P, h)][1] += 1
            if ask - bid >= 0.06:
                spread_wide[(P, h)][0] += 1
            pn = round(1 - bid, 2)
            if 0.05 <= pn <= 0.95:
                r = ((0 if won_yes else 1) - pn - fee_pc(pn)) / pn
                cells[(P, h, "NO")].append((e, r))
                cells[("ALL", h, "NO")].append((e, r))
                if young:
                    cells[("YOUNGCITY", h, "NO")].append((e, r))
                if h == 10:
                    b = next(f"{lo:.2f}-{hi:.2f}" for lo, hi in ((0.05, .3), (.3, .5), (.5, .7), (.7, .9), (.9, .96)) if lo <= pn < hi)
                    bands[(P, b)].append((e, r))
                    bands[("ALL", b)].append((e, r))
            if 0.05 <= ask <= 0.95:
                r = ((1 if won_yes else 0) - ask - fee_pc(ask)) / ask
                cells[(P, h, "YES")].append((e, r))
                cells[("ALL", h, "YES")].append((e, r))
    res = {"markets": len(mk), "dates": len({m["e"] for m in mk.values()}),
           "cells": {f"{P}|tau{h}h|{s}": clustered(v) for (P, h, s), v in sorted(cells.items())},
           "no_bands_tau10h": {f"{P}|{b}": clustered(v) for (P, b), v in sorted(bands.items())},
           "share_spread_ge_6c": {f"{P}|tau{h}h": round(a / n, 3) for (P, h), (a, n) in sorted(spread_wide.items()) if n}}
    # date-level NO mean at tau 10h (equal weight per date) for a regime picture
    byd = defaultdict(list)
    for e, r in cells[("ALL", 10, "NO")]:
        byd[e].append(r)
    res["no_tau10h_by_date"] = {e: round(sum(v) / len(v), 3) for e, v in sorted(byd.items())}
    dm = list(res["no_tau10h_by_date"].values())
    res["no_tau10h_date_mean"] = {"dates": len(dm), "mean": round(st.mean(dm), 4),
                                  "t": round(st.mean(dm) / (st.stdev(dm) / math.sqrt(len(dm))), 2),
                                  "share_dates_positive": round(sum(x > 0 for x in dm) / len(dm), 3)}
    res["variants"] = len(res["cells"]) + len(res["no_bands_tau10h"])
    (OUT / "d1_rain_bias.json").write_text(json.dumps(res, indent=1))
    return res


# ---------------------------------------------------------------- D2 weather young vs mature
LEGACY = {"KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHPHIL", "KXHIGHDEN", "KXHIGHAUS"}
THIN = {"KXHIGHTSAN", "KXHIGHTEWR", "KXHIGHTTTN", "KXHIGHTSDF"}


def logit(p):
    return math.log(p / (1 - p))


def fit_logistic(xs, ys, iters=30):
    a, b = 0.0, 1.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            z = max(-30.0, min(30.0, a + b * x))
            p = 1 / (1 + math.exp(-z))
            w = p * (1 - p)
            ga += y - p; gb += (y - p) * x
            haa += w; hab += w * x; hbb += w * x * x
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        da = (hbb * ga - hab * gb) / det
        db = (haa * gb - hab * ga) / det
        step = max(1.0, (abs(da) + abs(db)) / 2.0)
        a += da / step; b += db / step
        if abs(da) + abs(db) < 1e-8:
            break
    return a, b


def wx() -> dict:
    mkts = json.load((ROOT / "data/lab/us/kalshi/markets.json").open())
    first = {}
    for m in mkts.values():
        ct = int(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        first[m["series"]] = min(first.get(m["series"], ct), ct)
    taus = (14, 10, 6)
    rows = []
    for tk, m in mkts.items():
        if m.get("result") not in ("yes", "no"):
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
        c.sort()
        ct = int(datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        s = m["series"]
        age = (ct - first[s]) / 86400
        grp = "legacy" if s in LEGACY else ("thin" if s in THIN else ("young" if age < 21 else "new_mature"))
        y = 1 if m["result"] == "yes" else 0
        for h in taus:
            q = quote(c, ct - h * 3600)
            q1 = quote(c, ct - h * 3600 + 60)
            if not q or not q1:
                continue
            ask, bid = q
            if ask <= 0 or bid < 0 or ask < bid:
                continue
            rows.append({"g": grp, "s": s, "e": m["event_ticker"], "d": m["event_ticker"].split("-")[1], "h": h,
                         "mid": (ask + bid) / 2, "spr": ask - bid, "y": y, "ask1": q1[0], "bid1": q1[1]})
    # same-date control: legacy markets on the dates the young / thin groups trade
    dates = {g: {r["d"] for r in rows if r["g"] == g} for g in ("young", "thin", "new_mature")}
    out = {}
    rnd = random.Random(7)
    for g in ("legacy", "young", "new_mature", "thin", "legacy_on_young_dates", "legacy_on_thin_dates"):
        for h in taus:
            if g == "legacy_on_young_dates":
                R = [r for r in rows if r["g"] == "legacy" and r["h"] == h and r["d"] in dates["young"]]
            elif g == "legacy_on_thin_dates":
                R = [r for r in rows if r["g"] == "legacy" and r["h"] == h and r["d"] in dates["thin"]]
            else:
                R = [r for r in rows if r["g"] == g and r["h"] == h]
            if len(R) < 50:
                continue
            fitR = [r for r in R if 0.03 <= r["mid"] <= 0.97]
            a, b = fit_logistic([logit(r["mid"]) for r in fitR], [r["y"] for r in fitR])
            # cluster bootstrap (by date) for the slope
            byd = defaultdict(list)
            for r in fitR:
                byd[r["d"]].append(r)
            keys = list(byd)
            bs = []
            for _ in range(150):
                samp = [r for k in (rnd.choice(keys) for _ in keys) for r in byd[k]]
                bs.append(fit_logistic([logit(r["mid"]) for r in samp], [r["y"] for r in samp], iters=12)[1])
            bs.sort()
            brier = st.mean((r["mid"] - r["y"]) ** 2 for r in R)
            yes, no = [], []
            for r in R:
                if 0.10 <= r["mid"] <= 0.90:
                    pa = r["ask1"]
                    if 0.02 <= pa <= 0.98:
                        yes.append((r["e"], (r["y"] - pa - fee_pc(pa)) / pa))
                    pn = round(1 - r["bid1"], 2)
                    if 0.02 <= pn <= 0.98:
                        no.append((r["e"], ((1 - r["y"]) - pn - fee_pc(pn)) / pn))
            out[f"{g}|tau{h}h"] = {"n": len(R), "events": len({r['e'] for r in R}), "dates": len(keys),
                                   "median_spread": round(st.median(r["spr"] for r in R), 3),
                                   "brier_mid": round(brier, 4), "calib_intercept": round(a, 3),
                                   "calib_slope": round(b, 3),
                                   "slope_ci90": [round(bs[7], 3), round(bs[142], 3)],
                                   "taker_yes_mid10_90": clustered(yes), "taker_no_mid10_90": clustered(no)}
    res = {"cells": out, "variants": len(out)}
    (OUT / "d2_young_wx.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("rain", "all"):
        r = rain()
        print("D1 rain", r["markets"], "markets", r["dates"], "dates")
        for k, v in r["cells"].items():
            print(" ", k, v)
        for k, v in r["no_bands_tau10h"].items():
            print("  band", k, v)
        print("  spread>=6c", r["share_spread_ge_6c"])
        print("  date-level NO tau10h", r["no_tau10h_date_mean"])
    if what in ("wx", "all"):
        r = wx()
        for k, v in r["cells"].items():
            print(" ", k, v)
