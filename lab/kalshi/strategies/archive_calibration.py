"""archive_calibration: are Kalshi game-winner prices (MLB; NFL + NCAAF) mis-calibrated in the hours before the scheduled
start, over a longer history than the 30-day lab sample?

Pre-registered rules (data/kalshi_lab/strategies/archive_calibration/prereg.json, written before any price-vs-outcome row
was seen): R1_FAV (favourite in [0.75, 0.95) at S-2h), R2_DOG (underdog in [0.05, 0.30) at S-2h), R3_STEAM (mid up >= 4c
from S-6h to S-1h, buy that team at S-1h).
Clock: S = scheduled start. Decision instant D = last hourly boundary <= S - k h; the signal uses the quote at D (hourly
candle ending at D, carried forward at most MAXCF minutes); fill = taker at the quote of the next boundary D + 1 h.
Split: per family, games ordered by S, discovery = first 70%, validation = last 30%.
Usage: python -m lab.kalshi.strategies.archive_calibration [explore|validate|all]"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/archive_calibration")
FAM = {"KXMLBGAME": "mlb", "KXNFLGAME": "football", "KXNCAAFGAME": "football"}
SPLIT = 0.7
MAXCF = 60          # minutes an hourly close may be carried forward
H = 3600


def fee(p: float, n: int = 10) -> float:
    """Per-contract taker fee of an n-contract order, Kalshi rounds the order fee up to the cent."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def load():
    mk = {json.loads(l)["t"]: json.loads(l) for l in (OUT / "markets.jsonl").open()}
    games = []
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l); m = mk.get(x["t"])
        if not m or m["result"] not in ("yes", "no") or not m["S"]:
            continue
        c = sorted(r for r in x["c"] if r[1] is not None and r[2] is not None)
        games.append({**m, "fam": FAM[m["series"]], "c": c, "won": m["result"] == "yes"})
    return games


def q(c: list, t: int, maxcf: int = MAXCF):
    """(ask, bid, vol, oi) of the last hourly candle ending at or before t, if not older than maxcf minutes."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > maxcf * 60:
        return None
    a, b = best[1], best[2]
    if not (0 < a <= 1 and 0 <= b < 1) or a < b:
        return None
    return a, b, best[3], best[4]


def D(S: int, k: float) -> int:
    return int((S - k * H) // H * H)


def take(g: dict, side: str, t_fill: int):
    """Taker fill at t_fill on 'yes' (this market's team) or 'no' (the other team). -> (px, won, vol, oi) or None."""
    if g["close"] and t_fill >= g["close"]:
        return None
    f = q(g["c"], t_fill)
    if not f:
        return None
    px = f[0] if side == "yes" else 1 - f[1]
    if not (0.01 <= px <= 0.99):
        return None
    won = g["won"] if side == "yes" else not g["won"]
    return px, won, f[2], f[3]


def trade(g, rule, side, t_fill, sig_px):
    x = take(g, side, t_fill)
    if not x:
        return None
    px, won, vol, oi = x
    pnl = (1.0 if won else 0.0) - px - fee(px)
    return {"rule": rule, "fam": g["fam"], "series": g["series"], "src": g["src"], "e": g["e"], "S": g["S"], "px": px, "sig_px": sig_px,
            "won": won, "ret": pnl / px, "ret_half_fee": ((1.0 if won else 0.0) - px - fee(px) / 2) / px, "vol": vol, "oi": oi}


# ---------- rules ----------
def band_rule(g, k, who, lo, hi, name):
    """At D = S - k h: who = 'fav' / 'dog'; buy it if its taker price at D is in [lo, hi); fill at D + 1 h."""
    d = D(g["S"], k); s = q(g["c"], d)
    if not s:
        return None
    a, b = s[0], s[1]; mid = (a + b) / 2
    fav_side = "yes" if mid >= 0.5 else "no"
    side = fav_side if who == "fav" else ("no" if fav_side == "yes" else "yes")
    p = a if side == "yes" else 1 - b
    if not (lo <= p < hi):
        return None
    return trade(g, name, side, d + H, p)


def steam_rule(g, k_from, k_to, thr, mode, name):
    """mid(D_to) - mid(D_from) >= thr: follow = buy the riser, fade = buy the other team; fill at D_to + 1 h."""
    d0, d1 = D(g["S"], k_from), D(g["S"], k_to)
    s0, s1 = q(g["c"], d0), q(g["c"], d1)
    if not (s0 and s1):
        return None
    mv = (s1[0] + s1[1]) / 2 - (s0[0] + s0[1]) / 2
    if abs(mv) < thr:
        return None
    riser = "yes" if mv > 0 else "no"
    side = riser if mode == "follow" else ("no" if riser == "yes" else "yes")
    return trade(g, name, side, d1 + H, None)


PREREG = {
    "R1_FAV": lambda g: band_rule(g, 2, "fav", 0.75, 0.95, "R1_FAV"),
    "R2_DOG": lambda g: band_rule(g, 2, "dog", 0.05, 0.30, "R2_DOG"),
    "R3_STEAM": lambda g: steam_rule(g, 6, 1, 0.04, "follow", "R3_STEAM"),
}


def variants():
    """Every variant explored on discovery (counted in K)."""
    V = dict(PREREG)
    bands = [(0.05, 0.15), (0.15, 0.30), (0.30, 0.45), (0.45, 0.55), (0.55, 0.70), (0.70, 0.85), (0.85, 0.95)]
    for k in (1, 2, 3, 4, 6):
        for lo, hi in bands:
            who = "fav" if lo >= 0.45 else "dog"
            nm = f"band_k{k}_{who}_{lo:.2f}-{hi:.2f}"
            V[nm] = (lambda k, who, lo, hi, nm: lambda g: band_rule(g, k, who, lo, hi, nm))(k, who, lo, hi, nm)
    for kf, kt in ((6, 1), (6, 2), (4, 1), (3, 1)):
        for thr in (0.02, 0.04, 0.06, 0.08):
            for mode in ("follow", "fade"):
                nm = f"steam_{kf}to{kt}_{thr:.2f}_{mode}"
                V[nm] = (lambda kf, kt, thr, mode, nm: lambda g: steam_rule(g, kf, kt, thr, mode, nm))(kf, kt, thr, mode, nm)
    return V


# ---------- stats ----------
def stats(rows):
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; n_e = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    rows = sorted(rows, key=lambda r: r["S"]); mid = len(rows) // 2
    h1 = [r["ret"] for r in rows[:mid]]; h2 = [r["ret"] for r in rows[mid:]]
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "avg_px": st.mean(r["px"] for r in rows),
            "ret": st.mean(r["ret"] for r in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
            "ret_half_fee": st.mean(r["ret_half_fee"] for r in rows),
            "median_hour_vol": st.median(r["vol"] for r in rows), "median_oi": st.median(r["oi"] for r in rows)}


def fmt(nm, s):
    if not s.get("n"):
        return f"  {nm:34s} n=0"
    return (f"  {nm:34s} n={s['n']:4d} win={s['win']:.0%} px={s['avg_px']:.3f} ret={s['ret']:+.1%} t={s['t']:5.2f} wo3={s['ret_wo3']:+.1%} "
            f"h={s['half1']:+.1%}/{s['half2']:+.1%} hourvol={s['median_hour_vol']:.0f}")


def split(games):
    out = {}
    for fam in sorted({g["fam"] for g in games}):
        gs = sorted((g for g in games if g["fam"] == fam), key=lambda g: g["S"])
        cut = gs[int(len(gs) * SPLIT)]["S"]
        out[fam] = ([g for g in gs if g["S"] < cut], [g for g in gs if g["S"] >= cut], cut)
    return out


def run(V, gs):
    res = defaultdict(list)
    for g in gs:
        for nm, f in V.items():
            r = f(g)
            if r:
                res[nm].append(r)
    return res


def calib_table(gs, k=2):
    """Reliability of the favourite's taker price at S - k h (discovery only): bins of price -> win rate."""
    bins = defaultdict(list)
    for g in gs:
        r = band_rule(g, k, "fav", 0.5, 0.99, "x")
        if r:
            bins[min(int(r["sig_px"] * 20) / 20, 0.95)].append(r)
    return {f"{b:.2f}": {"n": len(v), "avg_px": round(st.mean(x["px"] for x in v), 3), "win": round(sum(x["won"] for x in v) / len(v), 3),
                         "ret": round(st.mean(x["ret"] for x in v), 4)} for b, v in sorted(bins.items())}


def explore():
    games = load(); sp = split(games); V = variants()
    print(f"games {len(games)}: " + ", ".join(f"{f} disc {len(d)} val {len(v)} (cut {cut})" for f, (d, v, cut) in sp.items()))
    srcs = defaultdict(int)
    for g in games:
        srcs[(g["fam"], g["src"])] += 1
    print("by source", dict(srcs))
    disc = {}
    for fam, (d, v, cut) in sp.items():
        res = run(V, d)
        print(f"\nDISCOVERY {fam}: calibration of the favourite at S-2h:", json.dumps(calib_table(d)))
        for nm in V:
            s = stats(res.get(nm, []))
            disc[f"{fam}|{nm}"] = s
        for nm in sorted(V, key=lambda nm: -(disc[f"{fam}|{nm}"].get("ret") or -9) if disc[f"{fam}|{nm}"].get("n", 0) >= 30 else 9)[:12]:
            print(fmt(nm, disc[f"{fam}|{nm}"]))
        print("  -- pre-registered:")
        for nm in PREREG:
            print(fmt(nm, disc[f"{fam}|{nm}"]))
    K = sum(1 for s in disc.values() if s.get("n", 0) >= 30)
    (OUT / "discovery.json").write_text(json.dumps({"K_cells_n30": K, "variants_total": len(disc), "cells": disc}, default=str))
    print(f"\nvariants examined {len(disc)} (cells with n>=30: {K})")
    return disc


def validate(cands: list[tuple[str, str]]):
    games = load(); sp = split(games); V = variants(); out = []
    import json as _j
    k_lab = _j.loads(Path("data/kalshi_lab/K.json").read_text()) if Path("data/kalshi_lab/K.json").exists() else {}
    for fam, nm in cands:
        d, v, cut = sp[fam]
        rows = run({nm: V[nm]}, v).get(nm, [])
        s = stats(rows); s["rule"] = f"{fam}|{nm}"
        s["by_src"] = {src: stats([r for r in rows if r["src"] == src]).get("ret") for src in ("live", "arch")}
        out.append(s); print("VALIDATION", fmt(f"{fam}|{nm}", s))
    (OUT / "validation.json").write_text(json.dumps({"candidates": cands, "results": out, "lab_K": k_lab}, default=str))
    return out


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "explore"
    if what == "explore":
        explore()
    elif what == "validate":
        validate([tuple(x.split("|")) for x in sys.argv[2:]])
