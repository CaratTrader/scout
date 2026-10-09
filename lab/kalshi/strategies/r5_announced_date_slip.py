"""r5_announced_date_slip: does NO on the rung that holds an issuer's announced release / launch date, bought a few
days before that date, beat the plain NO band?

Hypothesis. Retail traders anchor on company-announced target dates (SpaceX NET dates, album drops, game / model
launches); serial slippers miss them more often than the price says. Other side: fans and headline readers buying
YES "it comes out on the announced day". No speed edge is needed: the decision is days before the date.

Announced date (outcome-free, from prices only): at decision time t the target rung of an event is the earliest-
deadline rung still open at t whose hourly yes_bid close has reached THETA at some candle ending <= t (candle
history limited to 190 d before the rung deadline). Rung deadlines D are parsed from listing text (common.deadline).

Rule. t = D - h days (h in {7, 3}); if the rung is the target at t, buy NO as taker at 1 - yes_bid of the first
hourly candle ending in [t + 1 h, t + 2 h] (else skip: quote too old); 10-contract order fee
ceil(0.07 p (1 - p) * 10 cents) / 10. Return per $ = (win - p - fee) / p. Every tradeable (rung, h) pair with a fill
quote is the 'base set'; the plain-band control is the base set in the same NO band.

Split. Events (ladders) ordered by the latest decision time of their base-set pairs; discovery = first 70%,
validation = last 30%. Parameters chosen on discovery only; at most 3 frozen candidates go to validation.
Statistics: equal-$ mean, t clustered by event, mean without the 3 best, halves of validation, exact one-sided 95%
binomial (Beta) lower bound of the win rate mapped to per-$ return (amendment d).
Run: .venv/bin/python -m lab.kalshi.strategies.r5_announced_date_slip [discovery|validate|all]"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_announced_date_slip_common import DAY, H, OUT, load_candles, tradeable, universe  # noqa: E402
from lab.kalshi.strategies.r5_announced_date_slip_data import load_markets  # noqa: E402

SPLIT = 0.70
THETAS = (0.30, 0.40, 0.50)
HS = ("7", "3", "both")
BANDS = {"no30-60": (0.30, 0.60), "no30-90": (0.30, 0.90), "no05-95": (0.05, 0.95), "no50-97": (0.50, 0.97)}
MODES = ("ever", "now")   # target: bid reached THETA at any time <= t ('ever', the plan's rule) or bid >= THETA at t


def fee_pc(p: float, n: int = 10) -> float:
    return math.ceil(round(100 * 0.07 * p * (1 - p) * n, 6)) / 100 / n


def fill(c: list, t: int):
    """(yes_ask, yes_bid, vol24) of the first candle ending in [t + 1 h, t + 2 h]."""
    for r in c:
        if r[0] < t + H:
            continue
        if r[0] > t + 2 * H:
            return None
        if r[1] is None or r[2] is None:
            return None
        v24 = sum(x[4] for x in c if t - 24 * H < x[0] <= t)
        return r[1], r[2], v24
    return None


def first_reach(c: list, theta: float) -> int | None:
    for r in c:
        if r[2] is not None and r[2] >= theta:
            return r[0]
    return None


def bid_at(c: list, t: int):
    last = None
    for r in c:
        if r[0] <= t:
            last = r
        else:
            break
    if last is None or t - last[0] > 6 * H:
        return None
    return last[2]


def build() -> tuple[list[dict], dict]:
    """Base set: every tradeable (rung, h) pair with a fill quote, annotated with target flags per THETA / mode."""
    U = universe(load_markets())
    C = load_candles()
    by_e = defaultdict(list)
    for m in U:
        by_e[m["e"]].append(m)
    base, skipped = [], defaultdict(int)
    for m in U:
        for h in (7, 3):
            if not tradeable(m, h):
                continue
            if m["t"] not in C:
                skipped["no_candles"] += 1
                continue
            t = m["D"] - h * DAY
            q = fill(C[m["t"]], t)
            if not q:
                skipped["no_fill_quote"] += 1
                continue
            ask, bid, v24 = q
            p = round(1 - bid, 4)
            if not (0.01 <= p <= 0.99):
                skipped["price_out_of_range"] += 1
                continue
            nearer = [o for o in by_e[m["e"]] if o["D"] < m["D"] and o["D"] > t and o["open"] < t and o["close"] > t]
            if any(o["t"] not in C for o in nearer):
                skipped["nearer_rung_missing"] += 1
                continue
            flags = {}
            for th in THETAS:
                own_ever = (first_reach(C[m["t"]], th) or 1e18) <= t
                own_now = (bid_at(C[m["t"]], t) or 0) >= th
                near_ever = any((first_reach(C[o["t"]], th) or 1e18) <= t for o in nearer)
                near_now = any((bid_at(C[o["t"]], t) or 0) >= th for o in nearer)
                flags[f"ever{th:.2f}"] = own_ever and not near_ever
                flags[f"now{th:.2f}"] = own_now and not near_now
            won = m["result"] == "no"
            ret = ((1.0 if won else 0.0) - p - fee_pc(p)) / p
            base.append({"t": m["t"], "e": m["e"], "series": m["series"], "fam": m["fam"], "h": h, "dec": t, "D": m["D"],
                         "p": p, "ask": ask, "bid": bid, "spread": round(ask - bid, 4), "v24": v24, "won": won, "ret": ret,
                         "flags": flags, "n_nearer": len(nearer)})
    # event split
    ev_time = defaultdict(int)
    for r in base:
        ev_time[r["e"]] = max(ev_time[r["e"]], r["dec"])
    order = sorted(ev_time, key=lambda e: ev_time[e])
    k = int(round(len(order) * SPLIT))
    disc_ev = set(order[:k])
    for r in base:
        r["part"] = "disc" if r["e"] in disc_ev else "val"
    meta = {"skipped": dict(skipped), "events": len(order), "disc_events": k, "val_events": len(order) - k,
            "split_after": ev_time[order[k - 1]] if k else None}
    return base, meta


def beta_lower(k: int, n: int, a: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound of a binomial proportion, by bisection on the Beta CDF."""
    if k == 0:
        return 0.0

    def cdf_tail(p):   # P(X >= k | n, p)
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if cdf_tail(mid) < a:
            lo = mid
        else:
            hi = mid
    return lo


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    # amendment (d): per-event (first trade of each event) win-rate lower bound mapped to per-$ return at the mean price
    first = {}
    for r in sorted(rows, key=lambda r: r["dec"]):
        first.setdefault(r["e"], r)
    k = sum(r["won"] for r in first.values()); n1 = len(first)
    pbar = st.mean(r["p"] for r in first.values())
    lb = beta_lower(k, n1)
    mid = sorted(r["dec"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["dec"] < mid]; h2 = [r["ret"] for r in rows if r["dec"] >= mid]
    return {"n": len(rows), "events": n_e, "win": round(sum(r["won"] for r in rows) / len(rows), 3),
            "avg_px": round(st.mean(r["p"] for r in rows), 3), "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4),
            "t": round(t, 2) if t == t else None, "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "beta_lb_ret": round((lb - pbar - fee_pc(pbar)) / pbar, 4),
            "median_v24": st.median(r["v24"] for r in rows), "median_spread": st.median(r["spread"] for r in rows)}


def select(base, part, mode, th, hs, band):
    lo, hi = BANDS[band]
    return [r for r in base if r["part"] == part and r["flags"][f"{mode}{th:.2f}"] and (hs == "both" or str(r["h"]) == hs)
            and lo <= r["p"] <= hi]


def control(base, part, hs, band):
    lo, hi = BANDS[band]
    return [r for r in base if r["part"] == part and (hs == "both" or str(r["h"]) == hs) and lo <= r["p"] <= hi]


def discovery(base, meta) -> dict:
    cells = {}
    for mode in MODES:
        for th in THETAS:
            for hs in HS:
                for band in BANDS:
                    rows = select(base, "disc", mode, th, hs, band)
                    s = stats(rows)
                    if s["n"]:
                        s["fam_means"] = {f: round(st.mean(r["ret"] for r in rows if r["fam"] == f), 4)
                                          for f in sorted({r["fam"] for r in rows})}
                        s["fam_n"] = {f: sum(1 for r in rows if r["fam"] == f) for f in sorted({r["fam"] for r in rows})}
                        s["n_fam_pos"] = sum(1 for v in s["fam_means"].values() if v > 0)
                    cells[f"{mode}|{th:.2f}|h{hs}|{band}"] = s
    ctrl = {f"h{hs}|{band}": stats(control(base, "disc", hs, band)) for hs in HS for band in BANDS}
    return {"cells": cells, "control": ctrl, "variants": len(cells) + len(ctrl)}


def pick_candidates(d: dict) -> list[str]:
    """Frozen BEFORE looking at validation (rule written before the discovery table was printed):
    C1 = the test plan's literal rule: ever | THETA 0.40 | both horizons | NO 0.05-0.95.
    C2 = the hint band: ever | 0.40 | both | NO 0.30-0.60 (target YES bid 0.40-0.70 at the fill).
    C3 = the best other discovery cell by clustered t among cells with n >= 15 and >= 3 issuer families positive."""
    c1, c2 = "ever|0.40|hboth|no05-95", "ever|0.40|hboth|no30-60"
    ok = [(k, s) for k, s in d["cells"].items() if s.get("n", 0) >= 15 and s.get("n_fam_pos", 0) >= 3 and s.get("t") is not None
          and k not in (c1, c2)]
    c3 = max(ok, key=lambda kv: kv[1]["t"])[0] if ok else None
    return [c for c in (c1, c2, c3) if c]


def validate(base: list[dict], cands: list[str]) -> dict:
    out = {}
    for k in cands:
        mode_th, hs, band = k.split("|")[0:2], k.split("|")[2][1:], k.split("|")[3]
        rows = select(base, "val", mode_th[0], float(mode_th[1]), hs, band)
        s = stats(rows)
        s["control_same_band"] = stats(control(base, "val", hs, band))
        s["fam_n"] = {f: sum(1 for r in rows if r["fam"] == f) for f in sorted({r["fam"] for r in rows})}
        s["fam_means"] = {f: round(st.mean(r["ret"] for r in rows if r["fam"] == f), 4) for f in sorted({r["fam"] for r in rows})}
        s["trades"] = [{kk: r[kk] for kk in ("t", "h", "p", "won", "ret", "v24")} for r in sorted(rows, key=lambda r: r["dec"])]
        out[k] = s
    return out


def main(what: str = "all") -> None:
    base, meta = build()
    (OUT / "base.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sorted(base, key=lambda r: r["dec"])))
    print("base pairs", len(base), meta)
    d = discovery(base, meta)
    json.dump({"meta": meta, **d}, open(OUT / "discovery.json", "w"), indent=1, default=str)
    print(f"\nDISCOVERY cells ({d['variants']} incl. controls)")
    for k, s in sorted(d["cells"].items(), key=lambda kv: -(kv[1].get("ret_per_dollar") or -9)):
        if s["n"] >= 5:
            print(f"  {k:28s} n={s['n']:3d} ev={s['events']:3d} win={s['win']:.2f} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.3f} "
                  f"t={s['t']} wo3={s['ret_wo3']} famPos={s['n_fam_pos']}/{len(s['fam_means'])} {s['fam_means']}")
    print("\nDISCOVERY controls (plain band, any rung)")
    for k, s in d["control"].items():
        if s["n"]:
            print(f"  {k:20s} n={s['n']:3d} ev={s['events']:3d} win={s['win']:.2f} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.3f} t={s['t']} wo3={s['ret_wo3']}")
    cands = pick_candidates(d)
    json.dump({"frozen": cands, "rule": pick_candidates.__doc__}, open(OUT / "frozen.json", "w"), indent=1)
    print("\nFROZEN candidates:", cands)
    for k in cands:
        print("  disc", k, {kk: d["cells"][k].get(kk) for kk in ("n", "events", "ret_per_dollar", "t", "ret_wo3", "n_fam_pos", "fam_means")})
    if what != "all":
        return
    v = validate(base, cands)
    json.dump(v, open(OUT / "validation.json", "w"), indent=1, default=str)
    print("\nVALIDATION (judged once)")
    for k, s in v.items():
        c = s["control_same_band"]
        print(f"  {k:28s} n={s.get('n')} ev={s.get('events')} win={s.get('win')} px={s.get('avg_px')} ret={s.get('ret_per_dollar')} t={s.get('t')} "
              f"wo3={s.get('ret_wo3')} halves={s.get('half1')}/{s.get('half2')} betaLB={s.get('beta_lb_ret')} fam={s.get('fam_means')}"
              f" | control n={c.get('n')} ret={c.get('ret_per_dollar')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
