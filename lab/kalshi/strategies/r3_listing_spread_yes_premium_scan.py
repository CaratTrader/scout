"""r3_listing_spread_yes_premium_scan: is the listing-time YES premium of mention markets present in other zero-maker-fee
retail series (Entertainment, Politics, World, Companies, Social) that list with wide spreads?

Pre-registration: data/kalshi_lab/strategies/r3_listing_spread_yes_premium_scan/scan_preregistration.json (written before
any data; amendments 1-2 written before any candle was analysed).

Frozen rule C1 (round-2 mention maker, plus a 72 h order-life cap):
  * H = first hourly candle end with open + 1 h <= H <= open + 24 h, H < market close, no market of the event closed at
    or before H, 0 < yes_bid <= yes_ask < 1 (candle close quotes). Only this hour is evaluated.
  * if ask - bid >= 2c: rest SELL-YES at s = ask - 0.01 (= BUY-NO at 1 - s); no fee (quadratic series: makers free).
  * cancel at min(first close of the event, market close, H + 72 h).
maker_fill(): filled if a candle ending in (H, end] printed price_high > s, end = min(first close + 1 h, close + 1 h, H + 72 h)
  ("window B": the hour of the first close counts, so in-event adverse fills are included).
  full fill = some candle in the window traded >= 2c through (high >= s + 0.02) with volume >= 20; marginal = other fills.
Calibration: logit fit y ~ a + b logit(mid) on the same first-eligible snapshot; 90% event-cluster bootstrap CI.
Split per series: events ordered by last close; discovery = first 70%, validation = last 30%.

Usage: .venv/bin/python -m lab.kalshi.strategies.r3_listing_spread_yes_premium_scan [disc|val S1,S2,S3]"""
from __future__ import annotations
import json, math, random, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_listing_spread_yes_premium_scan_data import OUT, events_of

SERIES = ["KXYTVIEWSD", "KXBBCHARTPOSITIONALBUM", "KXYTDAILYTOPVIDEO", "KXYTTOPVIDEO2D", "KXYTTOPVIDEOG2D", "KXBBCHARTPOSITIONSONG",
          "KXTRUMPAPPROVE", "KXALBUMEQUIV", "KXPUREALBUMS"]
TRUNCATED = {"KXYTVIEWSD"}        # list hit 2 pages x 1000: the oldest event may be incomplete -> dropped
LIFE = 72 * 3600
STAKE = 5.0
K_LAB = 15450
CELLS = len(SERIES)


def load_candles() -> dict:
    C = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l)
        C[x["t"]] = x
    return C


def logit(p):
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def sig(z):
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def fit2(xs, ys, iters=60):
    """y ~ a + b x, Newton-Raphson."""
    a = b = 0.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            p = sig(a + b * x); w = p * (1 - p)
            ga += y - p; gb += (y - p) * x; haa += w; hab += w * x; hbb += w * x * x
        haa += 1e-9; hbb += 1e-9
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        da = (hbb * ga - hab * gb) / det; db = (haa * gb - hab * ga) / det
        a += da; b += db
        if abs(da) < 1e-9 and abs(db) < 1e-9:
            break
    return a, b


def fit_offset(xs, ys, iters=60):
    """y ~ a + x (slope fixed at 1)."""
    a = 0.0
    for _ in range(iters):
        g = h = 0.0
        for x, y in zip(xs, ys):
            p = sig(a + x); g += y - p; h += p * (1 - p)
        if h <= 0:
            break
        d = g / h; a += d
        if abs(d) < 1e-10:
            break
    return a


def cstats(rows):
    """Equal-$ return per row; t clustered by event (mean of event means / SE with n_e - 1); halves by event close."""
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; ne = len(em)
    sd = st.pstdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne - 1)) if ne > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    mid = sorted(r["tclose"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["tclose"] < mid]; h2 = [r["ret"] for r in rows if r["tclose"] >= mid]
    return {"n": len(rows), "events": ne, "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None}


def fee_pc(p: float) -> float:
    n = max(1, int(STAKE / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def snapshots(series: str, C: dict, part: str) -> list[dict]:
    """First-eligible-hour snapshot per market of the series' events in `part` (only markets with fetched candles)."""
    ev, prt = events_of(series)
    if series in TRUNCATED:
        oldest = min(ev, key=lambda e: max(m["close_ts"] for m in ev[e])); ev.pop(oldest)
    out = []
    for e, x in ev.items():
        if prt[e] != part:
            continue
        omin = min(m["open_ts"] for m in x); fc = min(m["close_ts"] for m in x); lc = max(m["close_ts"] for m in x)
        for m in x:
            c = C.get(m["ticker"])
            if c is None or m["open_ts"] > omin + 3 * 3600:
                continue
            if c["lo"] > m["open_ts"]:
                continue                         # fetch window did not cover the open (never expected; guard)
            snap = None
            for r in c["c"]:
                H, ask, bid = r[0], r[1], r[2]
                if H < m["open_ts"] + 3600 or H > m["open_ts"] + 24 * 3600 or H >= m["close_ts"] or H >= fc:
                    continue
                if ask is None or bid is None or not (0 < bid <= ask < 1):
                    continue
                snap = (H, ask, bid); break
            if snap is None:
                continue
            H, ask, bid = snap
            out.append({"t": m["ticker"], "e": e, "H": H, "ask": ask, "bid": bid, "mid": (ask + bid) / 2, "y": m["result"] == "yes",
                        "fc": fc, "close": m["close_ts"], "open": m["open_ts"], "tclose": lc, "rows": c["c"], "hi_fetch": c["hi"], "vol_m": float(m.get("volume_fp") or 0)})
    return out


def maker_fill(s0: dict):
    """C1 order for snapshot s0 -> (order price s, end, through?, full?, fill candle volume) or None if not posted."""
    if round(s0["ask"] - s0["bid"], 4) < 0.02:
        return None
    s = round(s0["ask"] - 0.01, 2)
    end = min(s0["fc"] + 3600, s0["close"] + 3600, s0["H"] + LIFE)
    through = full = False; vol = None
    for r in s0["rows"]:
        if r[0] <= s0["H"] or r[0] > end:
            continue
        hi = r[6]
        if hi is None:
            continue
        if hi > s + 1e-9 and not through:
            through = True; vol = r[4]
        if hi >= s + 0.02 - 1e-9 and r[4] >= 20:
            full = True
    return s, end, through, full, vol


def run_series(series: str, C: dict, part: str, boot: int = 300) -> dict:
    S = snapshots(series, C, part)
    if not S:
        return {"series": series, "part": part, "n_snap": 0}
    res = {"series": series, "part": part, "n_snap": len(S), "events": len({s["e"] for s in S}),
           "median_spread": round(st.median(s["ask"] - s["bid"] for s in S), 3),
           "mean_spread": round(st.mean(s["ask"] - s["bid"] for s in S), 3),
           "median_mid": round(st.median(s["mid"] for s in S), 3), "yes_rate": round(st.mean(s["y"] for s in S), 3),
           "mean_mid": round(st.mean(s["mid"] for s in S), 3),
           "median_hours_after_open": round(st.median((s["H"] - s["open"]) / 3600 for s in S), 2),
           "median_hours_to_first_close": round(st.median((s["fc"] - s["H"]) / 3600 for s in S), 1)}
    # calibration (free slope + offset), event-cluster bootstrap
    xs = [logit(s["mid"]) for s in S]; ys = [1 if s["y"] else 0 for s in S]
    a, b = fit2(xs, ys); a1 = fit_offset(xs, ys)
    by = defaultdict(list)
    for s, x, y in zip(S, xs, ys):
        by[s["e"]].append((x, y))
    keys = list(by); rng = random.Random(7); A = []; B = []; A1 = []
    for _ in range(boot):
        smp = [o for k in (rng.choice(keys) for _ in keys) for o in by[k]]
        aa, bb = fit2([o[0] for o in smp], [o[1] for o in smp]); A.append(aa); B.append(bb)
        A1.append(fit_offset([o[0] for o in smp], [o[1] for o in smp]))
    A.sort(); B.sort(); A1.sort()
    q = lambda v, f: v[min(len(v) - 1, max(0, int(f * len(v))))]
    res["calib"] = {"intercept": round(a, 3), "slope": round(b, 3), "intercept_ci90": [round(q(A, 0.05), 3), round(q(A, 0.95), 3)],
                    "slope_ci90": [round(q(B, 0.05), 3), round(q(B, 0.95), 3)], "offset_intercept": round(a1, 3),
                    "offset_ci90": [round(q(A1, 0.05), 3), round(q(A1, 0.95), 3)],
                    "brier_mid": round(st.mean((s["mid"] - s["y"]) ** 2 for s in S), 4)}
    # C1 maker
    allf, fullf, unfilled, posted = [], [], [], 0
    vols = []
    for s0 in S:
        o = maker_fill(s0)
        if o is None:
            continue
        posted += 1
        s, end, through, full, vol = o
        px = 1 - s; won = not s0["y"]; ret = ((1.0 if won else 0.0) - px) / px
        row = {"e": s0["e"], "t": s0["t"], "ret": ret, "won": won, "px": px, "tclose": s0["tclose"], "H": s0["H"], "s": s}
        if through:
            allf.append(row); vols.append(vol)
            if full:
                fullf.append(row)
        else:
            unfilled.append(row)
    res["C1"] = {"posted": posted, "through_fill_rate": round(len(allf) / posted, 3) if posted else None,
                 "full_fill_rate": round(len(fullf) / posted, 3) if posted else None,
                 "full": cstats(fullf), "all_through": cstats(allf), "unfilled_counterfactual": cstats(unfilled),
                 "posted_counterfactual": cstats(allf + unfilled),
                 "median_fill_candle_volume": round(st.median(vols), 1) if vols else None}
    # taker diagnostics at H (fee rounded up per $5 order)
    ty = [{"e": s["e"], "ret": ((1.0 if s["y"] else 0.0) - s["ask"] - fee_pc(s["ask"])) / s["ask"], "won": s["y"], "px": s["ask"], "tclose": s["tclose"]} for s in S]
    tn = [{"e": s["e"], "ret": ((0.0 if s["y"] else 1.0) - (1 - s["bid"]) - fee_pc(1 - s["bid"])) / (1 - s["bid"]), "won": not s["y"], "px": 1 - s["bid"], "tclose": s["tclose"]} for s in S]
    res["taker_YES_at_H"] = cstats(ty); res["taker_NO_at_H"] = cstats(tn)
    res["_rows_full"] = fullf; res["_rows_all"] = allf
    return res


def criteria(r: dict) -> dict:
    c = r.get("calib", {}); f = r.get("C1", {}).get("full", {})
    inc = r.get("median_spread", 0) >= 0.08          # the >= 30 events rule was applied to the whole series at listing
    cand = (inc and c.get("intercept", 0) <= -0.15 and c.get("intercept_ci90", [0, 0])[1] < 0
            and (f.get("ret_per_dollar") or -9) >= 0.08 and f.get("n", 0) >= 30)
    return {"spread_ok": r.get("median_spread", 0) >= 0.08, "intercept_ok": c.get("intercept", 0) <= -0.15 and c.get("intercept_ci90", [0, 0])[1] < 0,
            "full_ret_ok": (f.get("ret_per_dollar") or -9) >= 0.08, "full_n_ok": f.get("n", 0) >= 30, "candidate": bool(cand)}


def show(r):
    c = r.get("calib", {}); C1 = r.get("C1", {}); f = C1.get("full", {}); a = C1.get("all_through", {})
    print(f"{r['series']:24s} [{r['part']}] snaps={r['n_snap']:5d} ev={r.get('events')} spread med={r.get('median_spread')} mid={r.get('mean_mid')} yes={r.get('yes_rate')} | "
          f"a={c.get('intercept')} CI={c.get('intercept_ci90')} b={c.get('slope')} off={c.get('offset_intercept')} {c.get('offset_ci90')} | posted={C1.get('posted')} "
          f"fill={C1.get('through_fill_rate')}/{C1.get('full_fill_rate')} | FULL n={f.get('n')} ev={f.get('events')} ret={f.get('ret_per_dollar')} t={f.get('t')} wo3={f.get('ret_wo3')} "
          f"| ALL n={a.get('n')} ret={a.get('ret_per_dollar')} t={a.get('t')} | unfilled={C1.get('unfilled_counterfactual', {}).get('ret_per_dollar')} "
          f"| takerNO={r.get('taker_NO_at_H', {}).get('ret_per_dollar')} takerYES={r.get('taker_YES_at_H', {}).get('ret_per_dollar')}", flush=True)


def strip(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}


def discovery():
    C = load_candles(); out = {}
    for s in SERIES:
        r = run_series(s, C, "disc"); r["criteria"] = criteria(r) if r.get("n_snap") else None
        show(r) if r.get("n_snap") else print(s, "no snapshots")
        out[s] = strip(r)
    cands = sorted([s for s in out if (out[s].get("criteria") or {}).get("candidate")], key=lambda s: -(out[s]["C1"]["full"].get("t") or 0))
    K = K_LAB + CELLS; z = NormalDist().inv_cdf(1 - 0.05 / K)
    summary = {"K_lab_plus_cells": K, "bar_t": round(z, 2), "candidates_ranked_by_full_t": cands, "chosen_for_validation": cands[:3]}
    print(json.dumps(summary))
    (OUT / "discovery.json").write_text(json.dumps({"summary": summary, "series": out}, indent=1))
    return out, summary


def validation(names: list[str]):
    C = load_candles(); out = {}
    K = K_LAB + CELLS; z = NormalDist().inv_cdf(1 - 0.05 / K)
    for s in names:
        r = run_series(s, C, "val"); show(r)
        f = r["C1"]["full"]
        gate = {"n>=40": f.get("n", 0) >= 40, "ret>=0.10": (f.get("ret_per_dollar") or -9) >= 0.10, f"t>={z:.2f}": (f.get("t") or 0) >= z,
                "wo3>0": (f.get("ret_wo3") or -9) > 0, "halves>0": min(f.get("half1") or -9, f.get("half2") or -9) > 0}
        r["gate"] = gate; r["gate_pass"] = all(gate.values())
        days = (max(x["tclose"] for x in r["_rows_full"]) - min(x["tclose"] for x in r["_rows_full"])) / 86400 if r["_rows_full"] else 0
        r["full_fills_per_day"] = round(len(r["_rows_full"]) / days, 2) if days else None
        out[s] = strip(r)
        print(s, "GATE", gate)
    (OUT / "validation.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "disc"
    if what == "disc":
        discovery()
    elif what == "val":
        validation(sys.argv[2].split(","))
