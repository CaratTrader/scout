"""r6_weather_d1_mos_brackets: do free station-calibrated NWS guidance (NBM text NBS, GFS MOS, NAM MOS) probabilities
beat the Kalshi daily-high ladder one day ahead (D-1 16:00 local) or at dawn (D 07:00 local)?

Family universe: every KXHIGH* ladder (24 cities) with climate day 2026-08-02..2026-10-06 (Kalshi's live tier, where
hourly candles from listing are available in batch). Split by climate day: discovery D <= 2026-09-17 (~69% of events
ordered by close), validation D >= 2026-09-18. Parameters (guidance combo, bias mode, cells) chosen on discovery only.
Bias and spread are walk-forward from CLI residuals known at the decision (2025-05..), never from the future.

Steps:  build  -> rows.json.gz (one row per market x decision x model variant, with quote and outcome)
        kill   -> log-loss of the calibrated model vs the mid on discovery, blend regression (does the model add
                  information beyond the mid?), per decision / tier
        cells  -> discovery trading cells (taker, EV after fee >= thr), K counted
        validate <frozen.json> -> one-shot validation of at most 3 frozen cells (writes validation.lock)
Usage: python -m lab.kalshi.strategies.r6_weather_d1_mos_brackets [build|kill|cells|validate]"""
from __future__ import annotations
import datetime as dt, gzip, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_weather_d1_mos_brackets_common import (
    OUT, MAIN7, COMBOS, DEC, load_guidance, load_cli, build_calib, combo_forecast, dec_ts, bracket_p, interval, quote_at,
    ladders, load_candles, ret_5usd, fee_order)

D0, DCUT, DEND = dt.date(2026, 8, 2), dt.date(2026, 9, 18), dt.date(2026, 10, 6)
MODES = ("none", "station", "month", "roll", "mix")
ROWS = OUT / "rows.json.gz"


def build() -> None:
    G = load_guidance(); CLI = load_cli(); L = ladders(); C = load_candles()
    # Kalshi settlement values extend the CLI truth for the ladder days (used only once known, via the walk-forward store)
    for e in L.values():
        for m in e["markets"]:
            try:
                CLI[e["icao"]].setdefault(e["D"], float(m.get("xv")))
                break
            except (TypeError, ValueError):
                continue
    cal = build_calib(G, CLI, dt.date(2025, 5, 26), DEND)
    rows = []; miss = defaultdict(int)
    for e in sorted(L.values(), key=lambda e: (e["close"], e["e"])):
        if not (D0 <= e["D"] <= DEND):
            continue
        if not all(m["t"] in C for m in e["markets"]):
            miss["no_candles"] += 1; continue
        phase = "disc" if e["D"] < DCUT else "val"
        for which in DEC:
            tf = dec_ts(e["D"], e["tz"], which); td = tf - 60
            qs = {m["t"]: quote_at(C[m["t"]], tf) for m in e["markets"]}
            if not any(qs.values()):
                miss["no_quote_" + which] += 1; continue
            for combo in COMBOS:
                f = combo_forecast(G, e["icao"], e["D"], td, combo)
                if f is None:
                    miss[f"no_fc_{which}_{combo}"] += 1; continue
                mu0, nsd, age = f
                for mode in MODES:
                    bs = cal.bias_sd((e["icao"], which, combo), e["D"], which, mode)
                    if bs is None:
                        miss["no_calib"] += 1; continue
                    b, sd, ncal = bs
                    mu = mu0 + b
                    ps = {m["t"]: bracket_p(*interval(m), mu, sd) for m in e["markets"]}
                    z = sum(ps.values()) or 1.0
                    for m in e["markets"]:
                        q = qs[m["t"]]
                        if q is None:
                            continue
                        rows.append({"e": e["e"], "s": e["series"], "D": e["D"].isoformat(), "close": e["close"], "ph": phase, "w": which,
                                     "combo": combo, "mode": mode, "t": m["t"], "ask": q[0], "bid": q[1], "age": round(q[2], 2), "v3": round(q[3], 1),
                                     "p": ps[m["t"]] / z, "mu": round(mu, 2), "sd": round(sd, 2), "nsd": nsd, "fage": round(age, 1),
                                     "y": 1 if m["result"] == "yes" else 0, "main": e["series"] in MAIN7})
    with gzip.open(ROWS, "wt") as fh:
        json.dump(rows, fh)
    print("rows", len(rows), dict(miss))


def load_rows(phase: str) -> list[dict]:
    with gzip.open(ROWS, "rt") as fh:
        rows = json.load(fh)
    if phase == "disc":
        lock = OUT / "validation.lock"
        return [r for r in rows if r["ph"] == "disc"]
    return rows


def ll(p: float, y: int) -> float:
    p = min(max(p, 0.005), 0.995)
    return -math.log(p if y else 1 - p)


def logit(p: float) -> float:
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def logreg(X: list[list[float]], y: list[int], iters: int = 50, l2: float = 1e-4):
    """Newton IRLS logistic regression; returns (coef, se)."""
    k = len(X[0]); b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; H = [[0.0] * k for _ in range(k)]
        for x, yy in zip(X, y):
            eta = sum(bi * xi for bi, xi in zip(b, x)); p = 1 / (1 + math.exp(-max(min(eta, 30), -30)))
            for i in range(k):
                g[i] += (yy - p) * x[i]
                for j in range(k):
                    H[i][j] += p * (1 - p) * x[i] * x[j]
        for i in range(k):
            H[i][i] += l2; g[i] -= l2 * b[i]
        step = solve(H, g); b = [bi + si for bi, si in zip(b, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    inv = inverse(H)
    return b, [math.sqrt(max(inv[i][i], 0)) for i in range(k)]


def solve(A, b):
    n = len(b); M = [row[:] + [bb] for row, bb in zip(A, b)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c])); M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[c][c] != 0:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def inverse(A):
    n = len(A)
    return [list(col) for col in zip(*[solve(A, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)])]


def kill() -> dict:
    rows = load_rows("disc")
    res = {}
    groups = defaultdict(list)
    for r in rows:
        if r["ask"] - r["bid"] > 0.5:       # no two-sided market
            continue
        for tier in ("all", "main7" if r["main"] else "thin17"):
            groups[(r["w"], r["combo"], r["mode"], tier)].append(r)
    print("DISCOVERY log loss per market (lower is better): model vs mid; blend logit(y) ~ a + b*logit(mid) + c*logit(model)")
    for k, g in sorted(groups.items()):
        mid = [ll((r["ask"] + r["bid"]) / 2, r["y"]) for r in g]; mod = [ll(r["p"], r["y"]) for r in g]
        d = [a - b for a, b in zip(mod, mid)]
        # cluster the difference by date
        byd = defaultdict(list)
        for r, x in zip(g, d):
            byd[r["D"]].append(x)
        dm = [sum(v) for v in byd.values()]
        se = st.pstdev(dm) * math.sqrt(len(dm)) / len(d) if len(dm) > 2 else float("nan")
        res["|".join(k)] = {"n": len(g), "events": len({r["e"] for r in g}), "ll_mid": st.mean(mid), "ll_model": st.mean(mod),
                            "diff": st.mean(d), "diff_se_date": se}
    best = sorted(res.items(), key=lambda kv: kv[1]["diff"])
    for k, v in best[:12] + [("...", None)] + [kv for kv in best if kv[0].endswith("|all") and "|mix|" in kv[0]]:
        if v is None:
            print("  ..."); continue
        print(f"  {k:28s} n={v['n']:5d} ev={v['events']:4d} LLmid={v['ll_mid']:.4f} LLmodel={v['ll_model']:.4f} diff={v['diff']:+.4f} (se_date {v['diff_se_date']:.4f})")
    # blend regressions for the best variant per decision
    bl = {}
    for which in DEC:
        cand = [kv for kv in best if kv[0].startswith(which + "|") and kv[0].endswith("|all")]
        if not cand:
            continue
        key = cand[0][0]; w, combo, mode, tier = key.split("|")
        g = groups[(w, combo, mode, tier)]
        X = [[1.0, logit((r["ask"] + r["bid"]) / 2), logit(r["p"])] for r in g]; y = [r["y"] for r in g]
        b, se = logreg(X, y)
        X2 = [[1.0, logit((r["ask"] + r["bid"]) / 2)] for r in g]; b2, _ = logreg(X2, y)
        ll2 = st.mean(ll(1 / (1 + math.exp(-(b2[0] + b2[1] * x[1]))), yy) for x, yy in zip(X2, y))
        ll3 = st.mean(ll(1 / (1 + math.exp(-(b[0] + b[1] * x[1] + b[2] * x[2]))), yy) for x, yy in zip(X, y))
        X1 = [[1.0, logit(r["p"])] for r in g]; b1, _ = logreg(X1, y)
        ll1 = st.mean(ll(1 / (1 + math.exp(-(b1[0] + b1[1] * x[1]))), yy) for x, yy in zip(X1, y))
        # date-clustered (sandwich-free) check: refit the blend leaving out one date at a time is too slow; instead the
        # bootstrap over dates of c_model
        import random
        rnd = random.Random(7); byd = defaultdict(list)
        for x, yy, r in zip(X, y, g):
            byd[r["D"]].append((x, yy))
        ds = sorted(byd); cs = []
        for _ in range(40):
            xs = []; ys = []
            for d in (rnd.choice(ds) for _ in ds):
                for x, yy in byd[d]:
                    xs.append(x); ys.append(yy)
            cs.append(logreg(xs, ys, iters=25)[0][2])
        rel = defaultdict(lambda: [0, 0, 0.0])
        for r in g:
            k2 = min(int(r["p"] * 10), 9); rel[k2][0] += 1; rel[k2][1] += r["y"]; rel[k2][2] += r["p"]
        bl[which] = {"variant": key, "coef": b, "se_naive": se, "c_model_boot_date_sd": st.pstdev(cs), "ll_mid_recal": ll2,
                     "ll_model_recal": ll1, "model_recal_coef": b1, "ll_blend": ll3,
                     "reliability_model": {f"{k2/10:.1f}": {"n": v[0], "obs": v[1] / v[0], "pred": v[2] / v[0]} for k2, v in sorted(rel.items())},
                     "mean_sd": st.mean(r["sd"] for r in g)}
        print(f"    c_model bootstrap-by-date sd {st.pstdev(cs):.3f}; recalibrated model alone LL {ll1:.4f} (slope {b1[1]:.2f}); mean sigma {bl[which]['mean_sd']:.2f}F")
        print("    model reliability:", " ".join(f"{k}:{v['pred']:.2f}->{v['obs']:.2f}(n{v['n']})" for k, v in bl[which]["reliability_model"].items()))
        print(f"  BLEND {which} [{key}] a={b[0]:+.3f} b_mid={b[1]:.3f}±{se[1]:.3f} c_model={b[2]:.3f}±{se[2]:.3f} (naive se) "
              f"LL recal-mid {ll2:.4f} -> blend {ll3:.4f}")
    (OUT / "kill_discovery.json").write_text(json.dumps({"loglosses": res, "blend": bl}, indent=1, default=str))
    return res


def beta_lower(k: int, n: int, a: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound of a binomial proportion."""
    if k == 0:
        return 0.0
    def tail(p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < a:
            lo = mid
        else:
            hi = mid
    return lo


def tstat(xs: list[float]) -> float:
    return st.mean(xs) / (st.pstdev(xs) / math.sqrt(len(xs))) if len(xs) > 2 and st.pstdev(xs) > 0 else float("nan")


def stats(tr: list[dict]) -> dict:
    if not tr:
        return {"n": 0}
    ev = defaultdict(list); dd = defaultdict(list)
    for r in tr:
        ev[r["e"]].append(r["ret"]); dd[r["D"]].append(r["ret"])
    rs = sorted((r["ret"] for r in tr), reverse=True)
    dates = sorted(dd); mid = dates[len(dates) // 2] if dates else None
    h1 = [r["ret"] for r in tr if r["D"] < mid]; h2 = [r["ret"] for r in tr if r["D"] >= mid]
    first = {}
    for r in sorted(tr, key=lambda r: (r["D"], -r["ev"])):
        first.setdefault(r["e"], r)
    k = sum(r["won"] for r in first.values()); n1 = len(first); pbar = st.mean(r["px"] for r in first.values())
    lb = beta_lower(k, n1)
    return {"n": len(tr), "events": len(ev), "dates": len(dd), "win": sum(r["won"] for r in tr) / len(tr), "avg_px": st.mean(r["px"] for r in tr),
            "ret": st.mean(r["ret"] for r in tr), "t_event": tstat([st.mean(v) for v in ev.values()]),
            "t_date": tstat([st.mean(v) for v in dd.values()]), "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
            "beta_lb_ret": (lb - pbar - FEE * pbar * (1 - pbar)) / pbar, "per_day": len(tr) / max(len(dd), 1),
            "med_v3": sorted(r["v3"] for r in tr)[len(tr) // 2]}


FEE = 0.07
THRS = (0.04, 0.08, 0.12, 0.16, 0.20)
BANDS = {"b05": (0.05, 0.95), "b15": (0.15, 0.85)}
SIDES = ("YES", "NO", "both")
TIERS = ("all", "main7", "thin17")


def trades_for(rows: list[dict], side: str, thr: float, band: tuple, tier: str, max_age: float = 6.0) -> list[dict]:
    out = []
    for r in rows:
        if tier == "main7" and not r["main"] or tier == "thin17" and r["main"]:
            continue
        if r["age"] > max_age:
            continue
        p = r["p"]; cand = []
        if side in ("YES", "both") and r["ask"] < 1.0:
            px = r["ask"]; cand.append(("YES", px, p - px - FEE * px * (1 - px), r["y"] == 1))
        if side in ("NO", "both") and r["bid"] > 0.0:
            px = 1 - r["bid"]; cand.append(("NO", px, (1 - p) - px - FEE * px * (1 - px), r["y"] == 0))
        cand = [c for c in cand if band[0] <= c[1] <= band[1] and c[2] >= thr]
        if not cand:
            continue
        sd, px, evv, won = max(cand, key=lambda c: c[2])
        out.append({"e": r["e"], "D": r["D"], "t": r["t"], "side": sd, "px": px, "ev": evv, "won": won, "ret": ret_5usd(px, won), "v3": r["v3"],
                    "main": r["main"]})
    return out


def cells(variants: dict) -> dict:
    """variants: decision -> (combo, mode) chosen by the discovery kill test."""
    rows = load_rows("disc")
    out = {}
    for which, (combo, mode) in variants.items():
        g = [r for r in rows if r["w"] == which and r["combo"] == combo and r["mode"] == mode]
        for tier in TIERS:
            for side in SIDES:
                for thr in THRS:
                    for bn, band in BANDS.items():
                        tr = trades_for(g, side, thr, band, tier)
                        if len(tr) < 20:
                            continue
                        out[f"{which}|{combo}|{mode}|{tier}|{side}|thr{thr}|{bn}"] = stats(tr)
    best = sorted(out.items(), key=lambda kv: -kv[1]["ret"])
    print(f"DISCOVERY CELLS: {len(out)} with n>=20")
    for k, v in best[:25]:
        print(f"  {k:42s} n={v['n']:4d} ev={v['events']:4d} dt={v['dates']:3d} win={v['win']:.2f} px={v['avg_px']:.2f} ret={v['ret']:+.3f} "
              f"t_ev={v['t_event']:.2f} t_dt={v['t_date']:.2f} wo3={v['ret_wo3']:+.3f} h={v['half1']:+.3f}/{v['half2']:+.3f}")
    allc = {k: v for k, v in out.items()}
    (OUT / "discovery_cells.json").write_text(json.dumps(allc, indent=1, default=str))
    return out


def validate(frozen_path: str) -> dict:
    """One shot: the frozen cells (<= 3) on validation events. Refuses to run twice."""
    lock = OUT / "validation.lock"
    if lock.exists():
        print("validation already run:", lock.read_text()); return json.loads((OUT / "validation.json").read_text())
    fz = json.loads(Path(frozen_path).read_text())["cells"]
    assert len(fz) <= 3
    with gzip.open(ROWS, "rt") as fh:
        rows = [r for r in json.load(fh) if r["ph"] == "val"]
    out = {}
    for c in fz:
        g = [r for r in rows if r["w"] == c["w"] and r["combo"] == c["combo"] and r["mode"] == c["mode"]]
        tr = trades_for(g, c["side"], c["thr"], tuple(c["band"]), c["tier"])
        v = stats(tr); v["rule"] = c["name"]; out[c["name"]] = v
        (OUT / "trades").mkdir(exist_ok=True)
        (OUT / "trades" / f"val_{c['name'].replace('|', '_')}.json").write_text(json.dumps(tr))
        print(json.dumps(v, default=str))
    (OUT / "validation.json").write_text(json.dumps(out, indent=1, default=str))
    lock.write_text(dt.datetime.now(dt.timezone.utc).isoformat())
    return out


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "kill"
    if what == "build":
        build()
    elif what == "kill":
        kill()
    elif what == "validate":
        validate(sys.argv[2])
    elif what == "cells":
        v = json.loads(sys.argv[2])
        cells({k: tuple(x) for k, x in v.items()})
