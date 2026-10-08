"""Research steps for crypto15_spot (see crypto15_spot.py). Discovery-only until `validate`."""
from __future__ import annotations
import json, math, statistics as st
from collections import defaultdict
from lab.kalshi.strategies.crypto15_spot import load_rows, split_cut, p_model, stats, fee_order, OUT

clip = lambda p: min(max(p, 1e-4), 1 - 1e-4)
logit = lambda p: math.log(clip(p) / (1 - clip(p)))


def mid(q):
    return None if not q else (q[0] + q[1]) / 2


def disc_rows():
    R = load_rows(); cut = split_cut(R)
    return [r for r in R if r["close"] < cut], [r for r in R if r["close"] >= cut], cut


def logreg(X, y, iters=30, l2=1e-6):
    """Newton-Raphson logistic regression; X rows include the intercept column."""
    k = len(X[0]); b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = sum(bj * xj for bj, xj in zip(b, xi)); p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            w = p * (1 - p)
            for i in range(k):
                g[i] += (yi - p) * xi[i]
                for j in range(k):
                    H[i][j] += w * xi[i] * xi[j]
        for i in range(k):
            H[i][i] += l2
        # solve H d = g (Gaussian elimination)
        A = [H[i][:] + [g[i]] for i in range(k)]
        for i in range(k):
            piv = max(range(i, k), key=lambda r: abs(A[r][i])); A[i], A[piv] = A[piv], A[i]
            for r in range(k):
                if r != i:
                    f = A[r][i] / A[i][i]
                    A[r] = [a - f * c for a, c in zip(A[r], A[i])]
        d = [A[i][k] / A[i][i] for i in range(k)]
        b = [bi + di for bi, di in zip(b, d)]
        if max(abs(x) for x in d) < 1e-8:
            break
    # standard errors from the inverse Hessian diagonal (naive, not clustered)
    return b


def ll(ps, ys):
    return -st.mean(y * math.log(clip(p)) + (1 - y) * math.log(1 - clip(p)) for p, y in zip(ps, ys))


def explore():
    D, V, cut = disc_rows()
    print(f"discovery rows {len(D)}  validation rows {len(V)}  cut {cut}")
    rows = [r for r in D if r["q0"] and r["q1"] and 0 < mid(r["q0"]) < 1]
    print("usable discovery rows", len(rows))
    # 1. calibration / log-loss by tau group: market mid at t, model variants, market mid at t+60 (benchmark only)
    groups = {"tau2-4": (2, 4), "tau5-8": (5, 8), "tau9-14": (9, 14)}
    variants = {"n_s60": dict(sig="s60"), "n_s15": dict(sig="s15"), "n_blend": dict(sig="blend"), "t4_s60": dict(sig="s60", dist="t"),
                "n_s60_k0.8": dict(sig="s60", k=0.8), "n_s60_k1.2": dict(sig="s60", k=1.2), "n_nobasis": dict(sig="s60", basis=False)}
    for g, (lo, hi) in groups.items():
        x = [r for r in rows if lo <= r["tau"] <= hi]; y = [r["res"] for r in x]
        line = f"{g:8s} n={len(x):6d} LL mkt_t={ll([mid(r['q0']) for r in x], y):.4f} mkt_t+1={ll([mid(r['q1']) for r in x], y):.4f}"
        for name, kw in variants.items():
            line += f" {name}={ll([p_model(r, **kw) for r in x], y):.4f}"
        print(line)
    # 2. does the model add information beyond the market price at t? logit(res) ~ logit(mid_t) + (logit(model) - logit(mid_t))
    for g, (lo, hi) in groups.items():
        x = [r for r in rows if lo <= r["tau"] <= hi and 0.03 < mid(r["q0"]) < 0.97]
        X = [[1.0, logit(mid(r["q0"])), logit(p_model(r)) - logit(mid(r["q0"]))] for r in x]
        b = logreg(X, [r["res"] for r in x])
        print(f"{g} logit regression (0.03<mid<0.97, n={len(x)}): const {b[0]:+.3f}  mkt {b[1]:.3f}  model-gap {b[2]:+.3f}")
    # 3. binned gap = model - mid_t; realised res - mid_t; taker return following the model at the t+60 quote
    print("\nGAP BINS (model n_s60 minus mid_t), discovery, one row per (market, tau)")
    bins = [-1, -0.3, -0.2, -0.12, -0.08, -0.05, -0.03, 0.03, 0.05, 0.08, 0.12, 0.2, 0.3, 1]
    for lo, hi in zip(bins, bins[1:]):
        x = [r for r in rows if lo <= p_model(r) - mid(r["q0"]) < hi]
        if not x:
            continue
        res_minus = st.mean(r["res"] - mid(r["q0"]) for r in x)
        tr = []
        for r in x:
            side_yes = p_model(r) > mid(r["q0"])
            px = r["q1"][0] if side_yes else 1 - r["q1"][1]
            if not 0.01 <= px <= 0.99:
                continue
            won = r["res"] == (1 if side_yes else 0)
            tr.append({"e": r["e"], "ret": ((1.0 if won else 0.0) - px - fee_order(px)) / px, "won": won, "px": px})
        s = stats(tr)
        print(f"  gap [{lo:+.2f},{hi:+.2f}) n={len(x):6d} res-mid={res_minus:+.4f}  follow-model taker: n={s.get('n')} win={s.get('win')} px={s.get('avg_px')} ret={s.get('ret')} t={s.get('t')}")


def trade(r, side_yes, q, px_cap=None):
    """Taker fill on quote q (yes_ask, yes_bid); None if outside (0.01, 0.99) or above an optional limit."""
    if not q:
        return None
    px = q[0] if side_yes else 1 - q[1]
    if not 0.01 <= px <= 0.99 or (px_cap is not None and px > px_cap):
        return None
    won = r["res"] == (1 if side_yes else 0)
    return {"e": r["e"], "close": r["close"], "tk": r["tk"], "ret": ((1.0 if won else 0.0) - px - fee_order(px)) / px, "won": won, "px": px,
            "a": r["a"], "tau": r["tau"]}


def explore2():
    """Edge at decision time (model - ask_t - fee) by side type and tau group, filled at t+60 (rule) and at t (zero-delay
    upper bound, NOT achievable). Discovery only."""
    D, V, cut = disc_rows()
    rows = [r for r in D if r["q0"] and r["q1"]]
    print("LL by asset (all tau): market_t vs model n_s60 / t4_s60")
    for a in ("btc", "eth", "sol", "xrp", "doge"):
        x = [r for r in rows if r["a"] == a and 0 < mid(r["q0"]) < 1]; y = [r["res"] for r in x]
        sp = st.mean(r["q0"][0] - r["q0"][1] for r in x)
        print(f"  {a:5s} n={len(x):6d} spread={sp:.4f} LL mkt={ll([mid(r['q0']) for r in x], y):.4f} n={ll([p_model(r) for r in x], y):.4f} t4={ll([p_model(r, dist='t') for r in x], y):.4f}")
    edges = [0.0, 0.03, 0.05, 0.08, 0.12, 0.2, 1.0]
    for dist in ("n", "t"):
        print(f"\nEDGE BINS dist={dist}: e = p_model(side) - ask_t(side) - fee; one row per (market, tau, side with e>0)")
        for kind in ("fav", "long"):
            for tg, (lo_t, hi_t) in {"2-4": (2, 4), "5-8": (5, 8), "9-14": (9, 14)}.items():
                for lo, hi in zip(edges, edges[1:]):
                    tr1, tr0 = [], []
                    for r in rows:
                        if not lo_t <= r["tau"] <= hi_t:
                            continue
                        p = p_model(r, dist=dist)
                        for side_yes in (True, False):
                            ask_t = r["q0"][0] if side_yes else 1 - r["q0"][1]
                            if not 0.01 <= ask_t <= 0.99 or (kind == "fav") != (ask_t >= 0.5):
                                continue
                            ps = p if side_yes else 1 - p
                            e = ps - ask_t - fee_order(ask_t)
                            if lo <= e < hi:
                                a1 = trade(r, side_yes, r["q1"]); a0 = trade(r, side_yes, r["q0"])
                                if a1: tr1.append(a1)
                                if a0: tr0.append(a0)
                    s1, s0 = stats(tr1), stats(tr0)
                    if s1["n"]:
                        print(f"  {kind:4s} tau {tg:4s} e[{lo:.2f},{hi:.2f}) fill t+60: n={s1['n']:5d} win={s1['win']:.3f} px={s1['avg_px']:.3f} ret={s1['ret']:+.4f} t={s1['t']:+.2f} | fill t (upper bound): ret={s0.get('ret', 0):+.4f} t={s0.get('t', 0):+.2f}")


# ------------------------------------------------------------------ formal grid (family A: model gap at decision time)
GRID = {"dist": ("n", "t"), "sig": ("s15", "s60", "blend"), "tg": ("2-4", "5-8", "9-14", "2-14"), "kind": ("fav", "long", "any"),
        "E": (0.03, 0.05, 0.08, 0.12, 0.20), "slip": (0.02, None)}
TG = {"2-4": (2, 4), "5-8": (5, 8), "9-14": (9, 14), "2-14": (2, 14)}


def run_rule(rows, dist, sig, tg, kind, E, slip):
    """One trade per market: the earliest decision time (largest tau) in the group where e = p_model(side) - ask_t - fee >= E.
    The order arrives one minute later as a limit at ask_t + slip (None = market order) and fills at that minute's ask."""
    lo, hi = TG[tg]; bym = defaultdict(list)
    for r in rows:
        if lo <= r["tau"] <= hi and r["q0"]:
            bym[r["tk"]].append(r)
    out = []
    for tk, rs in bym.items():
        for r in sorted(rs, key=lambda r: -r["tau"]):
            p = p_model(r, sig=sig, dist=dist); hit = None
            for side_yes in (True, False):
                ask_t = r["q0"][0] if side_yes else 1 - r["q0"][1]
                if not 0.01 <= ask_t <= 0.99:
                    continue
                if kind != "any" and (kind == "fav") != (ask_t >= 0.5):
                    continue
                if (p if side_yes else 1 - p) - ask_t - fee_order(ask_t) >= E:
                    hit = (side_yes, ask_t)
            if hit:
                tr = trade(r, hit[0], r["q1"], None if slip is None else hit[1] + slip)
                if tr:
                    out.append(tr)
                break   # one decision per market: if the limit did not fill, no chasing
    return out


def halves(tr):
    if not tr:
        return (float("nan"), float("nan"))
    mid_t = sorted(x["close"] for x in tr)[len(tr) // 2]
    h1 = [x["ret"] for x in tr if x["close"] < mid_t]; h2 = [x["ret"] for x in tr if x["close"] >= mid_t]
    return (round(st.mean(h1), 4) if h1 else float("nan"), round(st.mean(h2), 4) if h2 else float("nan"))


def discover():
    import itertools
    D, V, cut = disc_rows()
    res = []
    for combo in itertools.product(*GRID.values()):
        kw = dict(zip(GRID, combo)); tr = run_rule(D, **kw); s = stats(tr)
        if s["n"]:
            s.update(kw); s["halves"] = halves(tr); s["t_window"] = stats(tr, key="close")["t"]; res.append(s)
    K = len(list(itertools.product(*GRID.values())))
    ok = [s for s in res if s["n"] >= 60 and s["ret"] >= 0.10 and s["t"] >= 2 and s["ret_wo3"] > 0 and min(s["halves"]) > 0]
    print(f"variants {K}; with trades {len(res)}; discovery candidates (n>=60, ret>=10%, t>=2, wo3>0, halves>0): {len(ok)}")
    for s in sorted(res, key=lambda s: -s["t"] if s["n"] >= 60 else 0)[:15]:
        print(f"  {s['dist']} {s['sig']:5s} tau {s['tg']:4s} {s['kind']:4s} E>={s['E']:.2f} slip={s['slip']} n={s['n']:5d} ev={s['events']:5d} win={s['win']:.3f} px={s['avg_px']:.3f} "
              f"ret={s['ret']:+.4f} t={s['t']:+.2f} t_win={s['t_window']:+.2f} wo3={s['ret_wo3']:+.4f} halves={s['halves']}")
    print("best by mean return (n>=60):")
    for s in sorted([s for s in res if s["n"] >= 60], key=lambda s: -s["ret"])[:8]:
        print(f"  {s['dist']} {s['sig']:5s} tau {s['tg']:4s} {s['kind']:4s} E>={s['E']:.2f} slip={s['slip']} n={s['n']:5d} win={s['win']:.3f} px={s['avg_px']:.3f} ret={s['ret']:+.4f} t={s['t']:+.2f} wo3={s['ret_wo3']:+.4f} halves={s['halves']}")
    pos = sum(1 for s in res if s["n"] >= 60 and s["ret"] > 0); n60 = sum(1 for s in res if s["n"] >= 60)
    print(f"share of variants (n>=60) with positive mean: {pos}/{n60}")
    (OUT / "discovery.json").write_text(json.dumps({"K": K, "cut": cut, "results": res}, default=str))


# ------------------------------------------------------------------ family B: resting (maker) bids at model fair - margin
MGRID = {"dist": ("n", "t"), "sig": ("s60", "blend"), "tg": ("2-4", "5-8", "9-14", "2-14"), "m": (0.02, 0.05, 0.08), "H": (2, 5, 99)}


def load_candles():
    from lab.kalshi.strategies.crypto15_spot_data import SERIES
    cd = {}
    for s in SERIES.values():
        for line in open(f"data/kalshi_lab/candles/{s}.jsonl"):
            x = json.loads(line); cd[x["t"]] = x["c"]
    return cd


def run_maker(rows, cd, dist, sig, tg, m, H):
    """At the earliest decision time in the group, the model's preferred side (fair > market mid) gets a resting bid at
    floor((fair - m) * 100) / 100, arriving one minute later and live for H minutes (99 = until close; cancels are as slow as
    orders). Filled only if the side's ask traded strictly below the bid inside the live window (trade-through: a resting
    ask below our bid cannot exist while our bid rests). quadratic series: no maker fee."""
    lo, hi = TG[tg]; bym = defaultdict(list)
    for r in rows:
        if lo <= r["tau"] <= hi and r["q0"] and r["q1"]:
            bym[r["tk"]].append(r)
    out = []; posted = 0
    for tk, rs in bym.items():
        r = max(rs, key=lambda r: r["tau"])
        p = p_model(r, sig=sig, dist=dist); mid_yes = mid(r["q0"])
        side_yes = p > mid_yes; fair = p if side_yes else 1 - p
        L = math.floor((fair - m) * 100 + 1e-9) / 100
        ask_arr = r["q1"][0] if side_yes else 1 - r["q1"][1]
        if not 0.02 <= L <= 0.98 or L >= ask_arr:
            continue
        posted += 1
        t_arr = r["close"] - r["tau"] * 60 + 60; t_end = min(r["close"], t_arr + H * 60)
        filled = False
        for c in cd.get(tk, []):
            if t_arr < c[0] <= t_end:
                low = c[3] if side_yes else (1 - c[4] if c[4] is not None else None)
                if low is not None and low < L - 1e-9:
                    filled = True; break
        if filled:
            won = r["res"] == (1 if side_yes else 0)
            out.append({"e": r["e"], "close": r["close"], "tk": tk, "ret": ((1.0 if won else 0.0) - L) / L, "won": won, "px": L, "a": r["a"], "tau": r["tau"]})
    return out, posted


def discover_maker():
    import itertools
    D, V, cut = disc_rows(); cd = load_candles(); res = []
    for combo in itertools.product(*MGRID.values()):
        kw = dict(zip(MGRID, combo)); tr, posted = run_maker(D, cd, **kw); s = stats(tr)
        if s["n"]:
            s.update(kw); s["posted"] = posted; s["fill_rate"] = round(s["n"] / posted, 3); s["halves"] = halves(tr); s["t_window"] = stats(tr, key="close")["t"]; res.append(s)
    K = len(list(itertools.product(*MGRID.values())))
    ok = [s for s in res if s["n"] >= 60 and s["ret"] >= 0.10 and s["t"] >= 2 and s["ret_wo3"] > 0 and min(s["halves"]) > 0]
    print(f"maker variants {K}; discovery candidates: {len(ok)}")
    for s in sorted(res, key=lambda s: -s["t"] if s["n"] >= 60 else 0)[:15]:
        print(f"  {s['dist']} {s['sig']:5s} tau {s['tg']:4s} m={s['m']:.2f} H={s['H']:2d} posted={s['posted']:5d} fill={s['fill_rate']:.2f} n={s['n']:5d} win={s['win']:.3f} px={s['avg_px']:.3f} "
              f"ret={s['ret']:+.4f} t={s['t']:+.2f} t_win={s['t_window']:+.2f} wo3={s['ret_wo3']:+.4f} halves={s['halves']}")
    pos = sum(1 for s in res if s["n"] >= 60 and s["ret"] > 0); n60 = sum(1 for s in res if s["n"] >= 60)
    print(f"share of maker variants (n>=60) with positive mean: {pos}/{n60}")
    (OUT / "discovery_maker.json").write_text(json.dumps({"K": K, "cut": cut, "results": res}, default=str))


# ------------------------------------------------------------------ frozen candidates (chosen on discovery only, 2026-10-08)
# No variant met the discovery bar (n>=60, ret>=10%, t>=2, wo3>0, both halves>0): 0/720 taker, 0/144 maker.
# The three frozen for a single validation look are the discovery leaders of each kind, to show what they do out of sample.
FROZEN = [
    {"name": "A1_taker_best_t", "kind": "taker", "kw": {"dist": "t", "sig": "s15", "tg": "5-8", "kind": "fav", "E": 0.05, "slip": 0.02}},
    {"name": "A2_taker_best_ret_longshot_gap", "kind": "taker", "kw": {"dist": "t", "sig": "s60", "tg": "2-4", "kind": "long", "E": 0.20, "slip": None}},
    {"name": "B1_maker_best_t", "kind": "maker", "kw": {"dist": "n", "sig": "blend", "tg": "9-14", "m": 0.02, "H": 5}},
]


def validate():
    D, V, cut = disc_rows(); cd = load_candles()
    days = (max(r["close"] for r in V) - min(r["close"] for r in V)) / 86400
    out = []
    for f in FROZEN:
        res = {}
        for lab_, rows in (("discovery", D), ("validation", V)):
            tr = run_rule(rows, **f["kw"]) if f["kind"] == "taker" else run_maker(rows, cd, **f["kw"])[0]
            s = stats(tr); s["halves"] = halves(tr); s["t_window"] = stats(tr, key="close")["t"] if tr else None
            s["trades_per_day"] = round(s["n"] / days, 2) if lab_ == "validation" else None
            s["by_asset"] = {a: stats([x for x in tr if x["a"] == a]).get("ret") for a in ("btc", "eth", "sol", "xrp", "doge")}
            res[lab_] = s
        print(f["name"], json.dumps(f["kw"]))
        for k, s in res.items():
            print(f"   {k:10s} n={s['n']} ev={s.get('events')} win={s.get('win')} px={s.get('avg_px')} ret={s.get('ret')} t={s.get('t')} t_window={s.get('t_window')} wo3={s.get('ret_wo3')} halves={s['halves']} per_day={s.get('trades_per_day')}")
            print(f"              by asset: {s['by_asset']}")
        out.append({**f, **res})
    (OUT / "validation.json").write_text(json.dumps({"cut": cut, "validation_days": days, "candidates": out}, default=str, indent=1))
