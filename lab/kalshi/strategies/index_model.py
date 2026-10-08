"""index_model: fair value of the hourly S&P 500 / Nasdaq-100 above/below ladders (KXINXU, KXNASDAQ100U) from the
cash index level and its volatility, traded as a taker against the Kalshi quote when the model says the quote is wrong
by more than the fee plus a margin.

Data (all cached under data/kalshi_lab/strategies/index_model/):
  Kalshi  - KXINXU 2026-08-10..09-23 fetched by index_model_fetch.py (strikes within +-0.5% of the level 66 min before
            the close), plus the lab's on-disk KXINXU / KXNASDAQ100U 2026-09-24..10-07 (same strike band applied).
  Index   - Yahoo 5-minute bars (^GSPC, ^NDX, ^VIX, ^VXN, ES=F, NQ=F); bars are labelled by their start time, so the bar
            starting at t-5min is complete at t. Decision times are on the 5-minute grid.
Model:    P(settle >= floor) = F( ln(S_t / floor) / (k * sigma * sqrt(tau)) ), F = normal or Student-t(4) scaled to
          unit variance; sigma per minute from (a) realised 5-minute returns of the last N bars, or (b) VIX/VXN.
Fill:     taker at the quote one minute after the decision (YES at yes_ask, NO at 1 - yes_bid), quote <= 30 min old;
          fee 0.07 p (1-p) per contract, rounded up to the cent per order of ceil($5 / p) contracts.
Split:    unique close hours sorted; discovery = first 70%, validation = last 30% (never used for choosing).
Usage:    python -m lab.kalshi.strategies.index_model [discovery|validate]"""
from __future__ import annotations
import bisect, datetime as dt, itertools, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote

OUT = Path("data/kalshi_lab/strategies/index_model"); Y5 = OUT / "yahoo5m"; LAB = Path("data/kalshi_lab")
CASH = {"KXINXU": ("GSPC", "ES_F", "VIX"), "KXNASDAQ100U": ("NDX", "NQ_F", "VXN")}
BAND = 0.005; WIN = 66
N01 = NormalDist()
ET = dt.timezone(dt.timedelta(hours=-4))   # all data is in EDT (Aug-Oct 2026)


# ---------------------------------------------------------------- data
def bars(name: str) -> tuple[list[int], list[list[float]]]:
    d = json.loads((Y5 / f"{name}.json").read_text()); k = sorted(int(x) for x in d)
    return k, [d[str(x)] for x in k]


B: dict = {}


def cash_level(series: str, t: int) -> tuple[float, int] | None:
    """(level, index of the bar) from the last 5-minute cash bar complete at t (bar start <= t - 300, not stale)."""
    k, v = B[CASH[series][0]]
    i = bisect.bisect_right(k, t - 300) - 1
    if i < 0 or t - 300 - k[i] > 300:
        return None
    return v[i][3], i


def band_level(series: str, t: int) -> float | None:
    """Level known at t; before the cash open the futures scaled by the last cash/futures ratio (strike band only)."""
    c = cash_level(series, t)
    if c:
        return c[0]
    ck, cv = B[CASH[series][0]]; fk, fv = B[CASH[series][1]]
    i = bisect.bisect_right(ck, t - 300) - 1; j = bisect.bisect_right(fk, t - 300) - 1
    jc = bisect.bisect_right(fk, ck[i]) - 1 if i >= 0 else -1
    if i < 0 or j < 0 or jc < 0:
        return None
    return fv[j][3] * cv[i][3] / fv[jc][3]


def rv_sigma(series: str, t: int, n: int) -> float | None:
    """Per-minute log vol from the last n complete 5-minute cash bars (returns within one session only)."""
    c = cash_level(series, t)
    if not c:
        return None
    k, v = B[CASH[series][0]]; i = c[1]; r = []
    j = i
    while j > 0 and len(r) < n:
        if k[j] - k[j - 1] == 300:
            r.append(math.log(v[j][3] / v[j - 1][3]))
        j -= 1
    if len(r) < max(3, n // 2):
        return None
    return math.sqrt(sum(x * x for x in r) / len(r) / 5)


def vix_sigma(series: str, t: int) -> float | None:
    k, v = B[CASH[series][2]]
    i = bisect.bisect_right(k, t - 300) - 1
    if i < 0 or t - k[i] > 3 * 86400:
        return None
    return v[i][3] / 100 / math.sqrt(252 * 390)


def load_events() -> list[dict]:
    for s, names in CASH.items():
        for n in names:
            B[n] = bars(n)
    evs = []
    srcs = [("KXINXU", OUT / "markets_KXINXU.jsonl", OUT / "candles_KXINXU.jsonl"),
            ("KXINXU", LAB / "markets/KXINXU.jsonl", LAB / "candles/KXINXU.jsonl"),
            ("KXNASDAQ100U", LAB / "markets/KXNASDAQ100U.jsonl", LAB / "candles/KXNASDAQ100U.jsonl"),
            ("KXNASDAQ100U", OUT / "markets_KXNASDAQ100U.jsonl", OUT / "candles_KXNASDAQ100U.jsonl")]
    seen = set()
    for s, mf, cf in srcs:
        if not mf.exists() or not cf.exists():
            continue
        meta = {m["t"]: m for m in map(json.loads, mf.open())}
        by_e = defaultdict(list)
        for line in cf.open():
            try:
                x = json.loads(line)
            except ValueError:
                continue   # a line still being written
            m = meta.get(x["t"])
            if not m or m["t"] in seen or m["floor"] is None:
                continue
            seen.add(m["t"]); by_e[(m["close"], m["e"])].append({**m, "c": x["c"]})
        for (c, e), ms in by_e.items():
            s0 = band_level(s, c - WIN * 60)
            if s0 is None:
                continue
            ms = [m for m in ms if abs(m["floor"] / s0 - 1) <= BAND]
            if ms:
                evs.append({"series": s, "e": e, "close": c, "markets": sorted(ms, key=lambda m: m["floor"])})
    evs.sort(key=lambda x: (x["close"], x["series"]))
    return evs


def split(evs: list[dict], frac: float = 0.7) -> int:
    hours = sorted({x["close"] for x in evs})
    return hours[int(len(hours) * frac)]


# ---------------------------------------------------------------- model + trades
def fee_c(p: float) -> float:
    n = max(1, math.ceil(5 / p))
    return math.ceil(round(0.07 * n * p * (1 - p) * 100, 6)) / 100 / n




def cdf(z: float, dist: str) -> float:
    if dist == "n":
        return N01.cdf(z)
    # Student-t with 4 dof scaled to unit variance: x = z * sqrt(2); closed-form t4 cdf
    x = z * math.sqrt(2); u = x / math.sqrt(4 + x * x)
    return 0.5 + 0.5 * u * (1 + 0.5 * (1 - u * u))


def prob(S: float, K: float, sig: float, tau_min: float, dist: str) -> float:
    sd = sig * math.sqrt(max(tau_min, 0.5))
    return min(max(cdf(math.log(S / K) / sd, dist), 1e-6), 1 - 1e-6)


def sigma(series: str, t: int, vol: str) -> float | None:
    if vol.startswith("rv"):
        return rv_sigma(series, t, int(vol[2:]))
    if vol == "vix":
        return vix_sigma(series, t)
    if vol.startswith("mix"):   # average of the realised (last N bars) and implied estimates
        a, b = rv_sigma(series, t, int(vol[3:])), vix_sigma(series, t)
        return (a + b) / 2 if a and b else None
    raise ValueError(vol)


def candidates(evs: list[dict], tau: int, vol: str, k: float, dist: str, delay: int = 1) -> list[dict]:
    """Every (market, side) at decision time t = close - tau min: the model probability from data <= t, the decision
    quote at t (the side's taker price then) and the fill quote one minute later. No threshold yet."""
    out = []
    for x in evs:
        t = x["close"] - tau * 60
        c = cash_level(x["series"], t)
        sg = sigma(x["series"], t, vol)
        if not c or not sg:
            continue
        S = c[0]
        for m in x["markets"]:
            q0 = quote(m["c"], t); q1 = quote(m["c"], t + delay * 60)
            if not q0 or not q1:
                continue
            p = prob(S, m["floor"], sg * k, tau, dist)
            won = m["result"] == "yes"
            for side, px0, px, pw, w in (("YES", q0[0], q1[0], p, won), ("NO", 1 - q0[1], 1 - q1[1], 1 - p, not won)):
                if not (0.01 <= px0 <= 0.99 and 0.01 <= px <= 0.99):
                    continue
                f = fee_c(px)
                out.append({"s": x["series"], "e": x["e"], "t_close": x["close"], "tau": tau, "side": side, "px0": px0, "px": px, "p": pw,
                            "edge": pw - px0 - fee_c(px0), "won": w, "ret": ((1.0 if w else 0.0) - px - f) / px, "floor": m["floor"], "S": S,
                            "spread": q0[0] - q0[1], "mid0": (q0[0] + q0[1]) / 2 if side == "YES" else 1 - (q0[0] + q0[1]) / 2})
    return out


def stats(rows: list[dict], key: str = "e") -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r[key]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; n_e = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "px": st.mean(r["px"] for r in rows),
            "ret": st.mean(r["ret"] for r in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan")}


def pick(rows: list[dict], theta: float, pmin: float, pmax: float, rel: float = 0.0, one_per_event: bool = False) -> list[dict]:
    sel = [r for r in rows if r["edge"] >= theta and pmin <= r["px0"] <= pmax and r["edge"] / r["px0"] >= rel]
    if one_per_event:   # the single largest edge per event
        best = {}
        for r in sel:
            if r["e"] not in best or r["edge"] > best[r["e"]]["edge"]:
                best[r["e"]] = r
        sel = list(best.values())
    return sel


def brier(rows: list[dict]) -> tuple[float, float]:
    """Model vs market mid (YES side rows only): mean squared error against the outcome."""
    y = [r for r in rows if r["side"] == "YES"]
    return (st.mean((r["p"] - r["won"]) ** 2 for r in y), st.mean((r["mid0"] - r["won"]) ** 2 for r in y)) if y else (float("nan"),) * 2


# ---------------------------------------------------------------- discovery / validation
TAUS = (5, 10, 15, 20, 30, 45, 60)
VOLS = ("rv6", "rv12", "rv24", "rv78", "vix", "mix12")
KS = (0.8, 0.9, 1.0, 1.1, 1.25, 1.5)
DISTS = ("n", "t")
THETAS = (0.02, 0.04, 0.06, 0.08, 0.10)
RANGES = ((0.05, 0.95), (0.20, 0.80))
SIDES = ("both", "YES", "NO")


def logloss(rows: list[dict], key: str) -> float:
    return st.mean(-math.log(min(max(r[key] if r["won"] else 1 - r[key], 1e-4), 1)) for r in rows)


def halves(rows: list[dict]) -> tuple[float, float]:
    if not rows:
        return float("nan"), float("nan")
    mid = sorted(r["t_close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
    return (st.mean(h1) if h1 else float("nan"), st.mean(h2) if h2 else float("nan"))


def fmt(v: dict) -> str:
    if not v.get("n"):
        return "n=0"
    return (f"n={v['n']:4d} ev={v['events']:3d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} wo3={v['ret_wo3']:+.1%}"
            + (f" halves={v['h'][0]:+.1%}/{v['h'][1]:+.1%}" if "h" in v else ""))


def discovery() -> dict:
    evs = load_events(); cut = split(evs)
    disc = [x for x in evs if x["close"] < cut]
    print(f"events {len(evs)} (INX {sum(x['series'] == 'KXINXU' for x in evs)}, NDX {sum(x['series'] == 'KXNASDAQ100U' for x in evs)}); "
          f"cut {dt.datetime.fromtimestamp(cut, ET)}; discovery events {len(disc)}")
    n_var = 0
    # 1. model choice by log loss against the outcome (proper score, not P&L), on sane quotes only
    res = {}
    for vol, k, dist in itertools.product(VOLS, KS, DISTS):
        rows = [r for tau in (10, 15, 20, 30, 45, 60) for r in candidates(disc, tau, vol, k, dist)
                if r["side"] == "YES" and 0.03 < r["mid0"] < 0.97 and r["spread"] <= 0.10]
        n_var += 1
        if rows:
            res[(vol, k, dist)] = (logloss(rows, "p"), logloss(rows, "mid0"), len(rows))
    best = sorted(res.items(), key=lambda kv: kv[1][0])
    print("\nMODEL LOG LOSS on discovery (lower is better); market mid on the same rows")
    for (vol, k, dist), (ll, llm, n) in best[:12]:
        print(f"  {vol:6s} k={k:4.2f} {dist}  model {ll:.4f}  market-mid {llm:.4f}  n={n}")
    model = best[0][0]
    # per-tau comparison for the chosen model
    print("\nchosen model", model, "- log loss by decision time (model / market mid / n)")
    for tau in TAUS:
        rows = [r for r in candidates(disc, tau, *model) if r["side"] == "YES" and 0.03 < r["mid0"] < 0.97 and r["spread"] <= 0.10]
        if rows:
            print(f"  tau {tau:2d}: {logloss(rows, 'p'):.4f} / {logloss(rows, 'mid0'):.4f} / {len(rows)}")
    # 2. trading cells on discovery
    cells = {}
    for tau in TAUS:
        rows = candidates(disc, tau, *model)
        for th, (lo, hi), side in itertools.product(THETAS, RANGES, SIDES):
            sel = [r for r in pick(rows, th, lo, hi) if side == "both" or r["side"] == side]
            n_var += 1
            if len(sel) >= 30:
                v = stats(sel); v["h"] = halves(sel); cells[(tau, th, lo, hi, side)] = v
    print(f"\nDISCOVERY TRADING CELLS (n>=30): {len(cells)}; best by return per $")
    for key, v in sorted(cells.items(), key=lambda kv: -kv[1]["ret"])[:20]:
        print(f"  tau={key[0]:2d} th={key[1]:.2f} px[{key[2]:.2f},{key[3]:.2f}] {key[4]:4s} {fmt(v)}")
    print("\nall-signal baseline (edge>=0.02, px .05-.95, both sides) by tau:")
    for tau in TAUS:
        sel = pick(candidates(disc, tau, *model), 0.02, 0.05, 0.95)
        print(f"  tau {tau:2d} {fmt(stats(sel))}")
    out = {"cut": cut, "model": model, "n_variants": n_var, "loglosses": {"|".join(map(str, k)): v for k, v in res.items()},
           "cells": {"|".join(map(str, k)): v for k, v in cells.items()}}
    (OUT / "discovery.json").write_text(json.dumps(out, default=str, indent=1))
    return out


def evaluate(evs: list[dict], cand: dict) -> list[dict]:
    """Trades of one frozen candidate: {"model": [vol, k, dist], "taus": [...], "theta", "lo", "hi", "side"}.
    With several decision times, one trade per market (the first time it triggers)."""
    done = set(); out = []
    for tau in sorted(cand["taus"], reverse=True):
        for r in pick(candidates(evs, tau, *cand["model"]), cand["theta"], cand["lo"], cand["hi"]):
            if (cand["side"] in ("both", r["side"])) and (r["e"], r["floor"]) not in done:
                done.add((r["e"], r["floor"])); out.append(r)
    return out


def report(rows: list[dict], days: float) -> dict:
    v = stats(rows)
    if v["n"]:
        v["h"] = halves(rows); v["t_hour"] = stats(rows, "t_close")["t"]; v["trades_per_day"] = v["n"] / days
        v["by_series"] = {s: stats([r for r in rows if r["s"] == s]) for s in ("KXINXU", "KXNASDAQ100U")}
    return v


def validate(cands: list[dict]) -> list[dict]:
    evs = load_events(); cut = split(evs)
    val = [x for x in evs if x["close"] >= cut]; disc = [x for x in evs if x["close"] < cut]
    dd = len({dt.datetime.fromtimestamp(x["close"], ET).date() for x in disc}); vd = len({dt.datetime.fromtimestamp(x["close"], ET).date() for x in val})
    print(f"validation events {len(val)} over {vd} days (INX {sum(x['series'] == 'KXINXU' for x in val)}, NDX {sum(x['series'] == 'KXNASDAQ100U' for x in val)})")
    res = []
    for c in cands:
        d = report(evaluate(disc, c), dd); v = report(evaluate(val, c), vd)
        print(f"\n{c['name']}: {c}\n  discovery  {fmt(d)}\n  VALIDATION {fmt(v)}  t(by hour)={v.get('t_hour', float('nan')):.2f}")
        for s, x in (v.get("by_series") or {}).items():
            print(f"     {s:13s} {fmt(x)}")
        res.append({"cand": c, "discovery": d, "validation": v})
    (OUT / "validation.json").write_text(json.dumps(res, default=str, indent=1))
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discovery"
    if cmd == "discovery":
        discovery()
    elif cmd == "validate":
        validate(json.loads((OUT / "frozen.json").read_text()))
