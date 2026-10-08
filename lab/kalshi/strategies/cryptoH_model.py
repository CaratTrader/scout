"""cryptoH_model: hourly crypto above/below ladders (KXBTCD, KXETHD) priced against a spot + realised-vol model.

Settlement: CF Benchmarks real-time index, 60-s average before the hour. Coinbase 1-minute candles proxy it well: the
average of the last minute's open/close lands inside the settled strike bracket in 640/640 hourly events (checked).

Model at decision time t (tau minutes before close T), using only Coinbase candles closed by t:
    S = last close, sigma = per-minute realised vol over W minutes (sqrt of the mean squared 1-min log return),
    X = settlement average, log(X/S) ~ sigma * sqrt(tau - 2/3) * Z  (Z normal or variance-1 Student-t), zero drift.
    p_yes(K) = P(X > K).
Execution (protocol): signal from the quote at t (last 1-min candle closing <= t), fill as taker at the quote one
minute later (YES at yes_ask, NO at 1 - yes_bid), skipped if the quote is older than 30 min; fee 0.07 p (1-p) per
contract (per-order rounding is applied in the capacity / sizing notes, not here). Universe per event: the 50 strikes
nearest the Coinbase spot 70 minutes before close (decision-time information).
Split: events ordered by close; discovery = first 70%, validation = last 30% (looked at once, for frozen rules only).

Usage: python -m lab.kalshi.strategies.cryptoH_model calib        (discovery only: model calibration vs market)
       python -m lab.kalshi.strategies.cryptoH_model discover     (discovery only: the rule grid; writes discovery.json)
       python -m lab.kalshi.strategies.cryptoH_model validate     (the frozen candidates in FROZEN, on validation, once)"""
from __future__ import annotations
import bisect, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote, fee, cell_stats
from lab.kalshi.strategies.cryptoH_model_data import load_cb, OUT

SER = {"KXBTCD": "btc", "KXETHD": "eth"}
TAUS = (5, 10, 15, 20, 30, 45, 60)
WINDOWS = (30, 60, 240, 1440)
N_STRIKES = 50
SPLIT = 0.7
ND = NormalDist()


# ------------------------------------------------------------------ distributions
def t_cdf(x: float, nu: float) -> float:
    """Student-t CDF (nu > 0) via the regularised incomplete beta function (continued fraction)."""
    if x == 0:
        return 0.5
    z = nu / (nu + x * x)
    ib = _betainc(nu / 2, 0.5, z)
    return 1 - 0.5 * ib if x > 0 else 0.5 * ib


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _cf(a, b, x) / a
    return 1 - math.exp(lbeta) * _cf(b, a, 1 - x) / b


def _cf(a: float, b: float, x: float) -> float:
    tiny = 1e-300; qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny); h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c if abs(1 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c if abs(1 + aa / c) > tiny else tiny
        de = d * c; h *= de
        if abs(de - 1) < 1e-10:
            break
    return h


def p_above(S: float, K: float, sig: float, tau: float, nu: float | None) -> float:
    s = sig * math.sqrt(max(tau - 2 / 3, 1 / 3))
    if s <= 0:
        return 1.0 if S > K else 0.0
    x = math.log(K / S) / s
    if nu is None:
        return 1 - ND.cdf(x)
    return 1 - t_cdf(x * math.sqrt(nu / (nu - 2)), nu)


# ------------------------------------------------------------------ data
class Spot:
    def __init__(self, asset: str):
        cb = load_cb(asset)
        self.m = sorted(cb); self.close = [cb[k][3] for k in self.m]
        r2 = [0.0]
        for i in range(1, len(self.m)):
            ok = self.m[i] - self.m[i - 1] == 60 and self.close[i - 1] > 0
            r = math.log(self.close[i] / self.close[i - 1]) if ok else 0.0
            r2.append(r2[-1] + r * r)
        self.r2 = r2

    def idx(self, t: int) -> int | None:
        """Index of the last candle closed by t (candle starting at m closes at m + 60)."""
        i = bisect.bisect_right(self.m, t - 60) - 1
        return i if i >= 0 and t - 60 - self.m[i] <= 30 * 60 else None

    def spot(self, t: int) -> float | None:
        i = self.idx(t)
        return self.close[i] if i is not None else None

    def vol(self, t: int, w: int) -> float | None:
        i = self.idx(t)
        if i is None or i < w:
            return None
        return math.sqrt((self.r2[i] - self.r2[i - w]) / w)


def load_events() -> list[dict]:
    """Events (hourly only) with their 50-strike universe and candles; sorted by close."""
    spots = {a: Spot(a) for a in SER.values()}
    evs = []
    for s, a in SER.items():
        meta = {}
        for l in open(f"data/kalshi_lab/markets/{s}.jsonl"):
            m = json.loads(l)
            if m["close"] - m["open"] == 3600:
                meta[m["t"]] = m
        cand = {}
        for f in (Path(f"data/kalshi_lab/candles/{s}.jsonl"), OUT / f"candles_{s}.jsonl"):
            if f.exists():
                for l in f.open():
                    x = json.loads(l)
                    if x["t"] in meta:
                        cand[x["t"]] = x["c"]
        by_e = defaultdict(list)
        for t, c in cand.items():
            by_e[meta[t]["e"]].append((meta[t], c))
        for e, ms in by_e.items():
            T = ms[0][0]["close"]; S0 = spots[a].spot(T - 70 * 60)
            if S0 is None:
                continue
            ms = sorted(ms, key=lambda mc: abs(mc[0]["floor"] - S0))[:N_STRIKES]
            evs.append({"e": e, "s": s, "a": a, "T": T, "mk": sorted(ms, key=lambda mc: mc[0]["floor"])})
    evs.sort(key=lambda x: (x["T"], x["s"]))
    return evs, spots


def split(evs: list[dict]) -> tuple[list[dict], list[dict], int]:
    closes = sorted({x["T"] for x in evs}); cut = closes[int(len(closes) * SPLIT)]
    return [x for x in evs if x["T"] < cut], [x for x in evs if x["T"] >= cut], cut


def records(evs: list[dict], spots: dict, taus=TAUS, delay: int = 1) -> list[dict]:
    """One row per (event, tau, strike) with the signal quote at t and the fill quote at t + delay."""
    out = []
    for ev in evs:
        sp = spots[ev["a"]]
        for tau in taus:
            t = ev["T"] - tau * 60; S = sp.spot(t)
            if S is None:
                continue
            vols = {w: sp.vol(t, w) for w in WINDOWS}
            if any(v is None or v <= 0 for v in vols.values()):
                continue
            for m, c in ev["mk"]:
                qs = quote(c, t); qf = quote(c, t + delay * 60)
                if not qs or not qf:
                    continue
                out.append({"e": ev["e"], "s": ev["s"], "T": ev["T"], "tau": tau, "K": m["floor"] + 0.01, "S": S, "vol": vols,
                            "ask": qs[0], "bid": qs[1], "askf": qf[0], "bidf": qf[1], "won": m["result"] == "yes"})
    return out


MODELS = {f"N{w}": (w, None) for w in WINDOWS}
MODELS.update({f"T4_{w}": (w, 4.0) for w in (60, 240)})
MODELS["N_blend"] = ("blend", None)
MODELS["T4_blend"] = ("blend", 4.0)
MODELS["FIT_N"] = ("fit", None)    # market-anchored: centre and scale fitted to the event's own near-money strikes at t
MODELS["FIT_T4"] = ("fit", 4.0)
MODELS["FIT_T3"] = ("fit", 3.0)


def z_cdf(x: float, nu: float | None) -> float:
    return ND.cdf(x) if nu is None else t_cdf(x * math.sqrt(nu / (nu - 2)), nu)


def z_inv(p: float, nu: float | None) -> float:
    if nu is None:
        return ND.inv_cdf(p)
    lo, hi = -50.0, 50.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if z_cdf(mid, nu) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def model_p(r: dict, name: str) -> float:
    w, nu = MODELS[name]
    if w == "fit":
        f = (r.get("fit") or {}).get(nu)
        if not f:
            return float("nan")
        mu, sc = f
        return 1 - z_cdf((math.log(r["K"]) - mu) / sc, nu)
    sig = math.sqrt((r["vol"][60] ** 2 + r["vol"][240] ** 2 + r["vol"][1440] ** 2) / 3) if w == "blend" else r["vol"][w]
    return p_above(r["S"], r["K"], sig, r["tau"], nu)


def add_fit(R: list[dict]) -> None:
    """Fit strikes: two-sided book, spread <= 3c, mid in [.15,.85] at t (need >= 2 distinct strikes). Least squares of
    F^-1(1 - mid) on ln K gives the market's own centre and scale; the fitted curve prices the remaining strikes (wings).
    Fit strikes are flagged as anchors and never traded."""
    g = defaultdict(list)
    for r in R:
        g[(r["e"], r["tau"])].append(r)
    for rs in g.values():
        fs = [r for r in rs if r["bid"] > 0 and r["ask"] - r["bid"] <= 0.03 and 0.15 <= (r["ask"] + r["bid"]) / 2 <= 0.85]
        if len({r["K"] for r in fs}) < 2:
            continue
        fit = {}
        for nu in (None, 4.0, 3.0):
            xs = [math.log(r["K"]) for r in fs]; ys = [z_inv(1 - (r["ask"] + r["bid"]) / 2, nu) for r in fs]
            mx, my = st.mean(xs), st.mean(ys); sxx = sum((x - mx) ** 2 for x in xs)
            bsl = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
            if bsl <= 0:
                continue
            fit[nu] = (mx - my / bsl, 1 / bsl)
        for r in rs:
            r["fit"] = fit; r["anchor"] = r in fs


# ------------------------------------------------------------------ evaluation
def trade_rows(R: list[dict], model: str, theta: float, taus: tuple, side: str = "both", pmin: float = 0.03, pmax: float = 0.90,
               slack: float | None = None, best_only: bool = False) -> list[dict]:
    """Taker trades where the model edge at the signal quote exceeds theta (after fee). Fill at the quote one minute later
    (if slack is set: only if the fill price is <= signal price + slack, an IOC limit order)."""
    cand = defaultdict(list)
    for r in R:
        if r["tau"] not in taus or (r.get("anchor") and MODELS[model][0] == "fit"):   # fit strikes are never traded by FIT rules
            continue
        p = model_p(r, model)
        if p != p:
            continue
        for sd, ps, pf, pw, won in (("YES", r["ask"], r["askf"], p, r["won"]), ("NO", 1 - r["bid"], 1 - r["bidf"], 1 - p, not r["won"])):
            if side != "both" and sd != side:
                continue
            if not (pmin <= ps <= pmax) or ps >= 0.995:
                continue
            edge = pw - ps - fee(ps)
            if edge < theta:
                continue
            if slack is not None and pf > ps + slack + 1e-9:
                continue
            if not (0.01 <= pf <= 0.99):
                continue
            ret = ((1.0 if won else 0.0) - pf - fee(pf)) / pf
            cand[(r["e"], r["tau"])].append({"e": r["e"], "T": r["T"], "tau": r["tau"], "side": sd, "px": pf, "won": won, "ret": ret,
                                              "edge": edge, "p": pw, "K": r["K"], "S": r["S"], "s": r["s"]})
    out = []
    for k, v in cand.items():
        out += [max(v, key=lambda x: x["edge"])] if best_only else v
    return out


def summarize(rows: list[dict]) -> dict | None:
    if len(rows) < 3:
        return {"n": len(rows)}
    v = cell_stats(rows)
    mid = sorted(r["T"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["T"] < mid]; h2 = [r["ret"] for r in rows if r["T"] >= mid]
    v["half1"] = st.mean(h1) if h1 else float("nan"); v["half2"] = st.mean(h2) if h2 else float("nan")
    return v


def fmt(v: dict) -> str:
    if v.get("n", 0) < 3:
        return f"n={v.get('n', 0)}"
    return (f"n={v['n']:5d} ev={v['events']:4d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} "
            f"wo3={v['ret_wo3']:+.1%} h={v['half1']:+.1%}/{v['half2']:+.1%}")


def logloss(p: float, y: bool) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -math.log(p if y else 1 - p)


def calib(R: list[dict]) -> None:
    """Discovery only. Which probability is better calibrated: market mid or the model? Only strikes with a real book."""
    rows = [r for r in R if r["bid"] > 0 and r["ask"] < 1 and r["ask"] - r["bid"] <= 0.05 and 0.03 <= (r["ask"] + r["bid"]) / 2 <= 0.97]
    print(f"calibration rows (two-sided book, spread<=5c, mid in [.03,.97]): {len(rows)} in {len({r['e'] for r in rows})} events")
    by_tau = defaultdict(list)
    for r in rows:
        by_tau[r["tau"]].append(r)
    names = list(MODELS)
    print("tau  " + " ".join(f"{n:>9s}" for n in ["market"] + names) + "   (mean log loss; lower = better)")
    for tau in TAUS:
        x = by_tau[tau]
        if not x:
            continue
        x = [r for r in x if r.get("fit") and len(r["fit"]) == 3 and not r.get("anchor")]
        mk = st.mean(logloss((r["ask"] + r["bid"]) / 2, r["won"]) for r in x)
        ms = [st.mean(logloss(model_p(r, n), r["won"]) for r in x) for n in names]
        print(f"{tau:3d}  {mk:9.4f} " + " ".join(f"{m:9.4f}" for m in ms) + f"   n={len(x)}")
    # does model minus market predict the outcome beyond the market? (binned)
    for n in ("N60", "T4_60", "FIT_N", "FIT_T4", "FIT_T3"):
        bins = defaultdict(list)
        for r in rows:
            mid = (r["ask"] + r["bid"]) / 2; d = model_p(r, n) - mid
            b = -0.15 if d < -0.10 else -0.075 if d < -0.05 else -0.035 if d < -0.02 else 0 if d <= 0.02 else 0.035 if d <= 0.05 else 0.075 if d <= 0.10 else 0.15
            bins[b].append((r["won"] - mid, d))
        print(f"  {n}: outcome - mid by (model - mid) bin: " + "  ".join(f"{b:+.3f}:{st.mean(a for a, _ in v):+.3f}(n={len(v)})" for b, v in sorted(bins.items())))
    # favourite-longshot on the wings: realised YES rate vs ask/bid by band
    print("\nwings (all strikes in universe): YES taker and NO taker by price band, discovery")
    for side in ("YES", "NO"):
        bands = ((0.01, 0.03), (0.03, 0.06), (0.06, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.90), (0.90, 0.97))
        for lo, hi in bands:
            x = []
            for r in R:
                if r["tau"] not in (10, 30, 60):
                    continue
                pf = r["askf"] if side == "YES" else 1 - r["bidf"]; won = r["won"] if side == "YES" else not r["won"]
                if lo <= pf < hi:
                    p = model_p(r, "N_blend"); p = p if side == "YES" else 1 - p
                    x.append({"e": r["e"], "T": r["T"], "px": pf, "won": won, "ret": ((1.0 if won else 0.0) - pf - fee(pf)) / pf, "p": p})
            if len(x) >= 20:
                v = summarize(x)
                print(f"  {side:3s} {lo:.2f}-{hi:.2f}: {fmt(v)} model_p={st.mean(r['p'] for r in x):.3f}")


GRID_THETA = (0.02, 0.04, 0.07, 0.10, 0.15)
GRID_TAU = ((5,), (10,), (15,), (20,), (30,), (45,), (60,), (5, 10, 15), (20, 30, 45, 60))


def discover(R: list[dict], model: str) -> list[tuple]:
    res = []
    for taus in GRID_TAU:
        for theta in GRID_THETA:
            for side in ("YES", "NO"):
                rows = trade_rows(R, model, theta, taus, side)
                if len(rows) >= 3:
                    res.append(((model, taus, theta, side), summarize(rows)))
    return res


# Frozen 2026-10-08 after discovery, before any validation result. Selection rule (stated before validating): among all
# protocol-valid discovery cells (decision on the quote at t, fill at t + 1 min) with n >= 30 and mean return >= +10%,
# the 3 with the highest clustered t. No cell met the lab's candidate bar (ret >= 10% AND t >= 2); these are the best
# available and are validated for information.
FROZEN: list[dict] = [
    {"name": "C1_band_NO_.50-.65_tau30", "kind": "band", "side": "NO", "lo": 0.50, "hi": 0.65, "taus": (30,),
     "disc": {"n": 143, "ret": 0.115, "t": 1.50}},
    {"name": "C2_T4_60_edge>=.02_YES_tau20", "kind": "edge", "model": "T4_60", "theta": 0.02, "side": "YES", "taus": (20,),
     "disc": {"n": 129, "ret": 0.204, "t": 1.29}},
    {"name": "C3_T4_60_edge>=.04_YES_tau15", "kind": "edge", "model": "T4_60", "theta": 0.04, "side": "YES", "taus": (15,),
     "disc": {"n": 47, "ret": 0.185, "t": 0.97}},
]


def band_rows(R: list[dict], side: str, lo: float, hi: float, taus: tuple, spread_max: float | None = None) -> list[dict]:
    """Price band on the SIGNAL quote at t; fill at the quote one minute later."""
    out = []
    for r in R:
        if r["tau"] not in taus:
            continue
        if spread_max is not None and not (r["bid"] > 0 and r["ask"] - r["bid"] <= spread_max):
            continue
        ps, pf, won = (r["ask"], r["askf"], r["won"]) if side == "YES" else (1 - r["bid"], 1 - r["bidf"], not r["won"])
        if lo <= ps < hi and 0.01 <= pf <= 0.99:
            out.append({"e": r["e"], "T": r["T"], "tau": r["tau"], "side": side, "px": pf, "won": won, "s": r["s"], "K": r["K"],
                        "ret": ((1.0 if won else 0.0) - pf - fee(pf)) / pf})
    return out


def cand_rows(R: list[dict], c: dict) -> list[dict]:
    if c["kind"] == "band":
        return band_rows(R, c["side"], c["lo"], c["hi"], c["taus"])
    return trade_rows(R, c["model"], c["theta"], c["taus"], c["side"])


def validate(evs_val: list[dict], spots: dict, R_disc: list[dict]) -> list[dict]:
    R = records(evs_val, spots); add_fit(R)
    out = []
    for c in FROZEN:
        d = summarize(cand_rows(R_disc, c)); rows = cand_rows(R, c); v = summarize(rows)
        days = (max(r["T"] for r in rows) - min(r["T"] for r in rows)) / 86400 if len(rows) > 1 else float("nan")
        v["trades_per_day"] = len(rows) / days if days == days and days > 0 else float("nan")
        # capacity proxy: contracts traded on that market in the fill minute (Kalshi candle volume)
        print(f"  {c['name']:32s} DISC {fmt(d)}")
        print(f"  {'':32s} VAL  {fmt(v)}  trades/day={v['trades_per_day']:.1f}")
        out.append({"rule": c["name"], "disc": d, **v})
    print("  drift benchmarks on validation (near-money, spread<=5c, signal band .35-.65, same taus 15/20/30):")
    for side in ("YES", "NO"):
        print(f"    all {side}: {fmt(summarize(band_rows(R, side, 0.35, 0.65, (15, 20, 30), 0.05)))}")
    return out


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "calib"
    evs, spots = load_events(); disc, val, cut = split(evs)
    print(f"events {len(evs)} (discovery {len(disc)}, validation {len(val)}; cut {cut})")
    if cmd == "calib":
        R = records(disc, spots); add_fit(R); calib(R)
    elif cmd == "discover":
        R = records(disc, spots); add_fit(R); model = sys.argv[2] if len(sys.argv) > 2 else "N_blend"
        res = discover(R, model)
        res.sort(key=lambda kv: -(kv[1].get("ret") or -9))
        for k, v in res[:40]:
            print(f"  {k[0]:9s} tau={','.join(map(str, k[1])):12s} th={k[2]:.2f} {k[3]:3s} {fmt(v)}")
        (OUT / f"discovery_{model}.json").write_text(json.dumps([{"key": [k[0], list(k[1]), k[2], k[3]], **v} for k, v in res], default=str))
        print(f"cells: {len(res)}")
    elif cmd == "validate":
        Rd = records(disc, spots); add_fit(Rd)
        res = validate(val, spots, Rd)
        (OUT / "validation.json").write_text(json.dumps(res, default=str, indent=1))


if __name__ == "__main__":
    main()
