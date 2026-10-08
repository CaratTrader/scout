"""multi_signal: does a learned logistic recalibration of Kalshi prices (price + spread + momentum + volume + time to close
+ ladder consistency, per family) beat the raw market price anywhere, by enough to pay the taker fee and earn >= +10%/trade?

Rows: lab/kalshi/strategies/multi_signal_features.py (one per market x decision time; features known at t, fill at t+60 s).
Split (strict, global): all events ordered by close time; DISCOVERY = first 70%, VALIDATION = last 30%.
Inside discovery a second walk-forward split (first 70% = inner-train, last 30% = inner-test) chooses everything:
  - model level per family: M0 market mid | M1 pooled a+b*logit(mid) | M2 per-family a+b*logit(mid) | M3 M2 + micro features |
    M4 M3 + interactions; L2 strength lambda; a level is kept only if it beats the simpler one by > 1 SE (event-clustered)
    on inner-test log-loss (complexity penalty);
  - the trading margin (on inner-test trades of inner-train models).
The chosen models are then refit on all of discovery and frozen; <= 3 candidates are run once on validation.
Trade: first decision time per market where the model's expected value for a side beats the decision-time quote + fee +
margin; fill at the quote 60 s later (YES at ask, NO at 1 - bid); fee 0.07 p (1-p) per contract rounded up to the cent per
order of a $5 stake. Stats: calib.cell_stats (equal-$ return per trade, t clustered by event).
Usage: python -m lab.kalshi.strategies.multi_signal [discover|validate]
"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
import multiprocessing as mp
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import cell_stats
from lab.kalshi.strategies.multi_signal_features import ROWS, OUT, lg

SPLIT = 0.7
LAMBDAS = (0.3, 3.0, 30.0)
MICRO = ("spr", "d5", "d30", "lv60", "tt", "ladr", "ovr", "nrm")
LEVELS = ("M0", "M1", "M2", "M3", "M4")
RET_MARGINS = (0.0, 0.05, 0.10, 0.20, 0.30)
ABS_MARGINS = (0.02, 0.04, 0.06)
STAKE = 5.0
VARIANTS = {"fits": 0, "margins": 0}


def sig(z: float) -> float:
    return 1 / (1 + math.exp(-z)) if z > -35 else 1e-15


def fee_order(p: float, stake: float = STAKE) -> float:
    n = max(1, int(stake / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def load() -> tuple[list[dict], int, int]:
    R = [json.loads(l) for l in ROWS.open()]
    ec = defaultdict(int)
    for r in R:
        ec[r["e"]] = max(ec[r["e"]], r["t_close"])
    closes = sorted(ec.values())
    cut = closes[int(len(closes) * SPLIT)]
    disc_closes = [c for c in closes if c < cut]
    icut = disc_closes[int(len(disc_closes) * SPLIT)]
    for r in R:
        c = ec[r["e"]]
        r["seg"] = "val" if c >= cut else ("itest" if c >= icut else "itrain")
    return R, icut, cut


def xvec(r: dict, level: str) -> list[float]:
    f = r["f"]; L = f["L"]
    if level in ("M1", "M2"):
        return [1.0, L]
    x = [1.0, L] + [f[k] for k in MICRO]
    if level == "M4":
        x += [L * f["tt"], L * f["spr"], L * f["d30"], abs(L)]
    return x


def solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b); M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda i: abs(M[i][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            M[c][c] = 1e-12
        for i in range(n):
            if i != c:
                k = M[i][c] / M[c][c]
                if k:
                    M[i] = [a - k * bb for a, bb in zip(M[i], M[c])]
    return [M[i][n] / M[i][i] for i in range(n)]


_G: dict = {}


def _fit_task(a):
    key, seg, fam, lev, lam = a
    rows = [r for s in seg for r in _G[s] if fam is None or r["fam"] == fam]
    return key, Model(lev, lam).fit(rows)


def fit_many(tasks: list[tuple]) -> dict:
    """tasks: (key, segments, family|None, level, lambda) -> {key: fitted Model}, in parallel (fork shares _G)."""
    VARIANTS["fits"] += len(tasks)
    with mp.get_context("fork").Pool(9) as pool:
        return dict(pool.map(_fit_task, tasks, chunksize=1))


class Model:
    """L2 logistic regression (Newton / IRLS), features standardised on the fit sample, intercept unpenalised."""
    def __init__(self, level: str, lam: float):
        self.level, self.lam = level, lam

    def fit(self, rows: list[dict]) -> "Model":
        X = [xvec(r, self.level) for r in rows]; y = [r["won"] for r in rows]; d = len(X[0])
        self.mu = [0.0] + [st.mean(x[j] for x in X) for j in range(1, d)]
        self.sd = [1.0] + [(st.pstdev([x[j] for x in X]) or 1.0) for j in range(1, d)]
        Z = [[(x[j] - self.mu[j]) / self.sd[j] for j in range(d)] for x in X]
        w = [0.0] * d
        # start at the market (intercept 0, slope on standardised L = sd)
        w[1] = self.sd[1]; w[0] = self.mu[1]
        for _ in range(20):
            g = [0.0] * d; H = [[0.0] * d for _ in range(d)]
            for z, yy in zip(Z, y):
                p = sig(sum(a * b for a, b in zip(w, z))); e = p - yy; v = max(p * (1 - p), 1e-6)
                for i in range(d):
                    g[i] += e * z[i]; zi = v * z[i]; Hi = H[i]
                    for j in range(i, d):
                        Hi[j] += zi * z[j]
            for i in range(d):
                for j in range(i):
                    H[i][j] = H[j][i]
                if i:
                    g[i] += self.lam * w[i]; H[i][i] += self.lam
            step = solve(H, g)
            w = [a - b for a, b in zip(w, step)]
            if max(abs(s) for s in step) < 1e-5:
                break
        self.w = w
        return self

    def p(self, r: dict) -> float:
        x = xvec(r, self.level)
        return sig(sum(w * (v - m) / s for w, v, m, s in zip(self.w, x, self.mu, self.sd)))


def ll(p: float, y: int) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if y else 1 - p)


def ev_mean_diff(rows: list[dict], pa: list[float], pb: list[float]) -> tuple[float, float]:
    """Mean log-loss difference (a - b) and its event-clustered SE."""
    ev = defaultdict(list)
    for r, a, b in zip(rows, pa, pb):
        ev[r["e"]].append(ll(a, r["won"]) - ll(b, r["won"]))
    em = [st.mean(v) for v in ev.values()]
    allv = [x for v in ev.values() for x in v]
    se = st.pstdev(em) / math.sqrt(len(em)) if len(em) > 2 else float("inf")
    return st.mean(allv), se


def trade(rows: list[dict], probs: list[float], rule: tuple[str, float]) -> list[dict]:
    """First qualifying decision time per market. rule = ('ret', m): EV per $ >= m; ('abs', m): EV per contract >= m."""
    kind, m = rule
    by_mk = defaultdict(list)
    for r, q in zip(rows, probs):
        by_mk[r["tk"]].append((r["t"], r, q))
    out = []
    for tk, lst in by_mk.items():
        lst.sort(key=lambda x: x[0])
        for t, r, q in lst:
            done = False
            for side, px_t, qs, px_f, won in (("YES", r["ask_t"], q, r["ask"], r["won"]), ("NO", 1 - r["bid_t"], 1 - q, 1 - r["bid"], 1 - r["won"])):
                if not (0.02 <= px_t <= 0.98):
                    continue
                edge = qs - px_t - fee_order(px_t)
                if (edge / px_t if kind == "ret" else edge) < m or edge <= 0:
                    continue
                if not (0.01 <= px_f <= 0.99):
                    break
                ret = (won - px_f - fee_order(px_f)) / px_f
                out.append({"e": r["e"], "tk": tk, "fam": r["fam"], "s": r["s"], "t_close": r["t_close"], "t": t, "lab": r["lab"], "side": side,
                            "px": px_f, "won": won, "ret": ret, "q": qs, "vf": r["vf"], "lv60": r["f"]["lv60"]})
                done = True
                break
            if done:
                break
    return out


def summarize(tr: list[dict]) -> dict:
    if len(tr) < 3:
        return {"n": len(tr)}
    v = cell_stats(tr)
    mid = sorted(x["t_close"] for x in tr)[len(tr) // 2]
    h1 = [x["ret"] for x in tr if x["t_close"] < mid]; h2 = [x["ret"] for x in tr if x["t_close"] >= mid]
    v["half1"] = st.mean(h1) if h1 else float("nan"); v["half2"] = st.mean(h2) if h2 else float("nan")
    return v


def fmt(v: dict) -> str:
    if v.get("n", 0) < 3:
        return f"n={v.get('n', 0)}"
    return (f"n={v['n']:5d} ev={v['events']:4d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} "
            f"wo3={v['ret_wo3']:+.1%} h1={v['half1']:+.1%} h2={v['half2']:+.1%}")


def choose_models(log) -> dict:
    """Per family: the level/lambda chosen on inner-test log-loss with the 1-SE complexity rule."""
    train, test = _G["itrain"], _G["itest"]
    fams = sorted({r["fam"] for r in train})
    ok = [f for f in fams if sum(r["fam"] == f for r in train) >= 200 and sum(r["fam"] == f for r in test) >= 50]
    tasks = [("pooled", ("itrain",), None, "M1", LAMBDAS[0])]
    for fam in ok:
        for lev in ("M2", "M3", "M4"):
            for lam in (LAMBDAS if lev != "M2" else LAMBDAS[:1]):
                tasks.append(((fam, lev, lam), ("itrain",), fam, lev, lam))
    M = fit_many(tasks)
    choice = {}
    for fam in fams:
        te = [r for r in test if r["fam"] == fam]
        if fam not in ok:
            choice[fam] = ("M0", None); log(f"  {fam:12s} too few rows: M0"); continue
        p0 = [r["mid"] for r in te]
        cands = {("M1", LAMBDAS[0]): [M["pooled"].p(r) for r in te]}
        for k, m in M.items():
            if k != "pooled" and k[0] == fam:
                cands[(k[1], k[2])] = [m.p(r) for r in te]
        base = sum(ll(p, r["won"]) for p, r in zip(p0, te)) / len(te)
        best = ("M0", None); bestp = p0
        line = [f"  {fam:12s} train {sum(r['fam'] == fam for r in train):6d} test {len(te):5d} M0 ll={base:.4f}"]
        for lev in ("M1", "M2", "M3", "M4"):
            opts = [(k, v) for k, v in cands.items() if k[0] == lev]
            k, v = min(opts, key=lambda kv: sum(ll(p, r["won"]) for p, r in zip(kv[1], te)))
            d, se = ev_mean_diff(te, v, bestp)
            d0, se0 = ev_mean_diff(te, v, p0)
            line.append(f"{lev}(l={k[1]}) vsbest {d:+.4f}+-{se:.4f} vsmkt {d0:+.4f}")
            if d < -se:
                best, bestp = k, v
        choice[fam] = best
        log(" ".join(line) + f" -> {best[0]}")
    return choice


def fit_chosen(segs: tuple, choice: dict) -> dict:
    tasks = [("pooled", segs, None, "M1", LAMBDAS[0])] + [(fam, segs, fam, lev, lam) for fam, (lev, lam) in choice.items() if lev not in ("M0", "M1")]
    M = fit_many(tasks)
    return {fam: (M["pooled"] if lev == "M1" else M[fam]) for fam, (lev, lam) in choice.items() if lev != "M0"}


def predict(rows: list[dict], models: dict) -> tuple[list[dict], list[float]]:
    rr = [r for r in rows if r["fam"] in models]
    return rr, [models[r["fam"]].p(r) for r in rr]


def calib_table(rows: list[dict], probs: list[float]) -> list[dict]:
    out = []
    for lo in [i / 10 for i in range(10)]:
        idx = [i for i, p in enumerate(probs) if lo <= p < lo + 0.1]
        if idx:
            out.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "n": len(idx), "model": st.mean(probs[i] for i in idx),
                        "mid": st.mean(rows[i]["mid"] for i in idx), "realised": st.mean(rows[i]["won"] for i in idx)})
    return out


def main(mode: str = "discover") -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lines = []
    def log(s=""):
        print(s, flush=True); lines.append(s)
    R, icut, cut = load()
    seg = defaultdict(list)
    for r in R:
        seg[r["seg"]].append(r)
    _G.update(seg)
    import datetime as dt
    d = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    log(f"rows {len(R)}: itrain {len(seg['itrain'])} itest {len(seg['itest'])} val {len(seg['val'])}; inner cut {d(icut)} UTC, validation cut {d(cut)} UTC")
    log("\n== DISCOVERY step 1: model level per family (inner-train fit, inner-test log-loss vs market; d<0 = better than market, 1-SE rule)")
    choice = choose_models(log)
    log("\n== DISCOVERY step 2: margin grid on inner-test (inner-train models), one trade per market")
    m_in = fit_chosen(("itrain",), choice)
    rr, pp = predict(seg["itest"], m_in)
    grid = {}
    for rule in [("ret", m) for m in RET_MARGINS] + [("abs", m) for m in ABS_MARGINS]:
        VARIANTS["margins"] += 1
        tr = trade(rr, pp, rule); v = summarize(tr); grid[rule] = (v, tr)
        log(f"  {rule[0]} {rule[1]:.2f}: {fmt(v)}")
        byf = defaultdict(list)
        for x in tr:
            byf[x["fam"]].append(x)
        for fam, xs in sorted(byf.items()):
            vv = summarize(xs); log(f"      {fam:12s} {fmt(vv)}")
    # also: the full M3 model for every family, no selection (does micro-structure help anywhere?), at ret 0.10
    log("\n== DISCOVERY diagnostic: unselected M3 (lambda 3) for every family, inner-test, ret>=0.10")
    m3 = fit_many([(fam, ("itrain",), fam, "M3", 3.0) for fam in sorted({r["fam"] for r in seg["itrain"]})
                   if sum(1 for r in seg["itrain"] if r["fam"] == fam) >= 200])
    rr3, pp3 = predict(seg["itest"], m3)
    tr3 = trade(rr3, pp3, ("ret", 0.10)); VARIANTS["margins"] += 1
    log(f"  all: {fmt(summarize(tr3))}")
    byf = defaultdict(list)
    for x in tr3:
        byf[x["fam"]].append(x)
    for fam, xs in sorted(byf.items()):
        log(f"      {fam:12s} {fmt(summarize(xs))}")
    state = {"choice": {k: list(v) for k, v in choice.items()},
             "grid": {f"{k[0]}:{k[1]}": v for k, (v, _) in grid.items()}, "m3_diag": summarize(tr3), "variants": dict(VARIANTS)}
    (OUT / "discovery.json").write_text(json.dumps(state, indent=1, default=str))
    (OUT / f"report_{mode}.txt").write_text("\n".join(lines) + "\n")
    if mode != "validate":
        return
    # ---------------------------------------------------------------------------------------------------- VALIDATION
    frozen = json.loads((OUT / "frozen.json").read_text())
    log(f"\n== FROZEN CANDIDATES (chosen on discovery, written before validation): {json.dumps(frozen)}")
    models = fit_chosen(("itrain", "itest"), {k: tuple(v) for k, v in frozen["choice"].items()})
    m3full = fit_many([(fam, ("itrain", "itest"), fam, "M3", 3.0) for fam in frozen.get("m3_fams", [])]) if frozen.get("m3_fams") else {}
    val = seg["val"]
    log("\n== VALIDATION: log-loss model vs market (d<0 = model better), event-clustered SE")
    res = {"calibration": {}, "logloss": {}, "candidates": []}
    allm = {**m3full, **models}
    for fam in sorted({r["fam"] for r in val}):
        te = [r for r in val if r["fam"] == fam]
        for name, mm in (("chosen", models.get(fam)), ("M3", m3full.get(fam))):
            if mm is None:
                continue
            pm = [mm.p(r) for r in te]; d0, se = ev_mean_diff(te, pm, [r["mid"] for r in te])
            res["logloss"][f"{fam}:{name}"] = {"n": len(te), "d": d0, "se": se}
            log(f"  {fam:12s} {name:6s} n={len(te):5d} d={d0:+.4f} +- {se:.4f}")
            res["calibration"][f"{fam}:{name}"] = calib_table(te, pm)
    for cand in frozen["candidates"]:
        ms = models if cand["models"] == "chosen" else m3full
        fams = cand.get("fams") or sorted(ms)
        rr = [r for r in val if r["fam"] in fams and r["fam"] in ms]
        pp = [ms[r["fam"]].p(r) for r in rr]
        tr = trade(rr, pp, (cand["kind"], cand["m"]))
        v = summarize(tr)
        days = (max(x["t_close"] for x in tr) - min(x["t_close"] for x in tr)) / 86400 if len(tr) > 1 else float("nan")
        v["trades_per_day"] = len(tr) / days if days and days > 0 else float("nan")
        v["median_vol60_contracts"] = st.median(math.expm1(x["lv60"]) for x in tr) if tr else float("nan")
        v["median_fillminute_vol"] = st.median(x["vf"] for x in tr) if tr else float("nan")
        v["rule"] = cand["name"]
        byf = defaultdict(list)
        for x in tr:
            byf[x["fam"]].append(x)
        v["by_family"] = {f: summarize(xs) for f, xs in sorted(byf.items())}
        res["candidates"].append(v)
        log(f"\n  {cand['name']}: {fmt(v)} trades/day={v['trades_per_day']:.1f} med vol60={v['median_vol60_contracts']:.0f}")
        for f, vv in v["by_family"].items():
            log(f"      {f:12s} {fmt(vv)}")
        (OUT / f"val_trades_{cand['name']}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in tr))
    log("\n== VALIDATION calibration (pooled over traded families, chosen models): bin, n, model, mid, realised")
    rr = [r for r in val if r["fam"] in models]; pp = [models[r["fam"]].p(r) for r in rr]
    for b in calib_table(rr, pp):
        log(f"  {b['bin']} n={b['n']:5d} model={b['model']:.3f} mid={b['mid']:.3f} realised={b['realised']:.3f}")
    res["calibration"]["pooled:chosen"] = calib_table(rr, pp)
    res["variants"] = dict(VARIANTS)
    (OUT / "validation.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / f"report_{mode}.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "discover")
