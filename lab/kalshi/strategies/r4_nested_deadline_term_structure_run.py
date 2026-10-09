"""Runner for r4_nested_deadline_term_structure (disk only, no Kalshi calls).

  disc : discovery report (first 70% of events by latest settled rung close). Writes discovery.json / discovery.txt.
  val  : evaluates the candidates frozen in frozen.json on validation, once. Writes validation.json / validation.txt.
  robust : post-validation context (full sample, without Starship). Writes robustness.txt.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_nested_deadline_term_structure_run [disc|val|robust]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_nested_deadline_term_structure import (load, snapshots, market_snaps, rule_S, rule_B, stats, fmt, split,
                                                                     signal, trade, KAPPAS, HAZARDS, SPLIT, DAY)
from lab.kalshi.strategies.r4_nested_deadline_term_structure_api import OUT

RANGES = ((0.50, 0.97), (0.30, 0.97))    # pre-specified NO range, plus a wider one added after the discovery diagnostic
VARIANTS = [(k, h, rg) for rg in RANGES for h in HAZARDS for k in KAPPAS]


def universe():
    M, E, C = load()
    S = snapshots(E, M, C)
    MS = market_snaps(E, M, C, S)
    ec, _ = split(E, M)
    evs = sorted({r["e"] for r in S if r.get("far") and r["fill_ok"]}, key=lambda e: ec[e])
    cut_i = int(len(evs) * SPLIT)
    disc_e, val_e = set(evs[:cut_i]), set(evs[cut_i:])
    return M, E, C, S, MS, ec, evs, disc_e, val_e


def near_markets(S, evs: set) -> set:
    """Markets that are a near rung with >= 1 far-rung quote and a fill at some snapshot with tau <= 60 d."""
    return {r["t"] for r in S if r["e"] in evs and r.get("far") and r["fill_ok"] and 1 <= r["tau"] <= 60}


def half_stats(rows, ec):
    if not rows:
        return float("nan"), float("nan")
    mid = sorted(ec[x["e"]] for x in rows)[len(rows) // 2]
    h1 = [x["ret"] for x in rows if ec[x["e"]] < mid]; h2 = [x["ret"] for x in rows if ec[x["e"]] >= mid]
    return (st.mean(h1) if h1 else float("nan")), (st.mean(h2) if h2 else float("nan"))


def beta_lo(rows) -> dict:
    """One-sided 95% exact-binomial (Clopper-Pearson) lower bound on the win rate over unique events (event-level win =
    event-mean return > 0), and the per-$ return implied at the average price (gate amendment d)."""
    ev = defaultdict(list)
    for x in rows:
        ev[x["e"]].append(x)
    n = len(ev); k = sum(1 for v in ev.values() if st.mean(y["ret"] for y in v) > 0)
    if n == 0:
        return {}
    lo = 0.0 if k == 0 else _beta_ppf(0.05, k, n - k + 1)
    px = st.mean(x["px"] for x in rows); fee = 0.07 * px * (1 - px)
    return {"events": n, "wins": k, "win_lo95": round(lo, 4), "ret_at_lo95": round((lo - px - fee) / px, 4)}


def _beta_ppf(q, a, b, it=80):
    lo, hi = 0.0, 1.0
    for _ in range(it):
        m = (lo + hi) / 2
        if _betacdf(m, a, b) < q:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def _betacdf(x, a, b):
    # regularized incomplete beta via the binomial identity (a, b integers here)
    n = a + b - 1
    return sum(math.comb(n, j) * x ** j * (1 - x) ** (n - j) for j in range(a, n + 1))


def report(S, MS, evs: set, ec: dict, label: str) -> tuple[list[str], dict]:
    L = []; out = {}
    rows = [r for r in S if r["e"] in evs]
    mk = near_markets(S, evs)
    L.append(f"{label}: events {len(evs)}, near-rung markets {len(mk)}, snapshots {len(rows)}, with far quote {sum(1 for r in rows if r.get('far'))}")
    # diagnostic: is the near rung's YES above the hazard-implied q? (realised YES rate by ratio bucket; one row per
    # market at its first snapshot in the bucket, tau 1..60 d)
    L.append("\nDIAGNOSTIC ratio bid/q_ls of the near rung, first snapshot per market in each bucket (tau 1-60 d)")
    for lo, hi in ((0, 0.8), (0.8, 1.25), (1.25, 1.5), (1.5, 2), (2, 4), (4, 1e9)):
        seen = {}
        for r in sorted(rows, key=lambda r: r["ts"]):
            q = r.get("q_ls")
            if q is None or not (1 <= r["tau"] <= 60) or q <= 0 or r["bid"] <= 0:
                continue
            if lo <= r["bid"] / q < hi:
                seen.setdefault(r["t"], r)
        v = list(seen.values())
        if v:
            L.append(f"  ratio {lo:4.2f}-{hi:7.2f}: markets {len(v):3d} events {len({x['e'] for x in v}):3d} mean bid {st.mean(x['bid'] for x in v):.3f} "
                     f"mean q {st.mean(x['q_ls'] for x in v):.3f} realised YES {st.mean(x['result'] == 'yes' for x in v):.3f}")
    B = rule_B([r for r in MS if r["e"] in evs], mk)
    vB = stats(B); vB["half1"], vB["half2"] = half_stats(B, ec); out["B"] = vB
    L.append("\nBAND B on the same near-rung markets (NO 0.70-0.97 at D in {60,45,30,21,14}, first entry per market)")
    L.append(fmt("B", vB))
    L.append("\nSIGNAL RULES S(kappa, hazard): NO 0.50-0.97, tau 1-60 d, first signal per market")
    for k, h, rg in VARIANTS:
        T = rule_S(rows, k, h, *rg); v = stats(T); v["half1"], v["half2"] = half_stats(T, ec)
        name = f"S k{k} {h} NO{rg[0]:.2f}-{rg[1]:.2f}"
        # encompassing: (i) S minus B (all markets of the set); (ii) paired on markets traded by both
        both = set(x["t"] for x in T) & set(x["t"] for x in B)
        sm = {x["t"]: x["ret"] for x in T}; bm = {x["t"]: x["ret"] for x in B}
        v["incr_vs_B"] = (v.get("ret_per_dollar", float("nan")) - vB.get("ret_per_dollar", float("nan"))) if v.get("n") and vB.get("n") else float("nan")
        v["paired_n"] = len(both)
        v["paired_diff"] = st.mean(sm[t] - bm[t] for t in both) if both else float("nan")
        # (iii) within B entries: the signal state at B's own entry snapshot
        on = [x for x in B if signal(x["row"], k, h)]
        off = [x for x in B if not signal(x["row"], k, h) and not x["row"].get("not_near") and x["row"].get(f"q_{h}") is not None]
        v["B_and_S"] = stats(on); v["B_not_S"] = stats(off)
        v["within_B_diff"] = (v["B_and_S"].get("ret_per_dollar", float("nan")) - v["B_not_S"].get("ret_per_dollar", float("nan"))) if on and off else float("nan")
        out[name] = v
        L.append(fmt(name, v) + f" | incr vs B {v['incr_vs_B']:+.1%}; paired n={len(both)} diff {v['paired_diff']:+.1%}; "
                 f"within B (near rungs only): S-on n={len(on)} {v['B_and_S'].get('ret_per_dollar', float('nan')):+.1%} vs off n={len(off)} "
                 f"{v['B_not_S'].get('ret_per_dollar', float('nan')):+.1%}")
    return L, out


def main(mode: str) -> None:
    M, E, C, S, MS, ec, evs, disc_e, val_e = universe()
    f = lambda x: dt.datetime.utcfromtimestamp(x).strftime("%Y-%m-%d")
    head = [f"events with evaluable near-rung snapshots: {len(evs)} (discovery {len(disc_e)}, validation {len(val_e)}); "
            f"split after event close {f(ec[sorted(disc_e, key=lambda e: ec[e])[-1]])}",
            "discovery events: " + ", ".join(sorted(disc_e, key=lambda e: ec[e]))]
    if mode == "disc":
        L, out = report(S, MS, disc_e, ec, "DISCOVERY")
        L = head + L
        rows = [r for r in S if r["e"] in disc_e]
        L.append("\nALL DISCOVERY TRADES of B and of S k1.5 ls NO0.30-0.97")
        mk = near_markets(S, disc_e)
        for x in sorted(rule_B([r for r in MS if r["e"] in disc_e], mk) + rule_S(rows, 1.5, "ls", 0.30, 0.97), key=lambda x: (x["t"], x["rule"])):
            L.append(f"  {x['rule']:12s} {x['t']:40s} {f(x['ts'])} tau={x['tau']:5.1f} bid={x['bid']:.2f} q={x['q'] if x['q'] is not None else float('nan'):.3f} "
                     f"px={x['px']:.2f} won={x['won']} ret={x['ret']:+.2f}")
        (OUT / "discovery.txt").write_text("\n".join(L) + "\n")
        (OUT / "discovery.json").write_text(json.dumps(out, indent=1, default=str))
        print("\n".join(L))
    elif mode == "val":
        fr = json.loads((OUT / "frozen.json").read_text())
        rows = [r for r in S if r["e"] in val_e]
        mk = near_markets(S, val_e)
        L = head[:1] + [f"VALIDATION events: {len(val_e)}: " + ", ".join(sorted(val_e, key=lambda e: ec[e])), f"near-rung markets {len(mk)}"]
        B = rule_B([r for r in MS if r["e"] in val_e], mk); vB = stats(B); vB["half1"], vB["half2"] = half_stats(B, ec)
        L.append(fmt("B (reference)", vB) + f" halves {vB.get('half1', float('nan')):+.1%}/{vB.get('half2', float('nan')):+.1%}")
        res = {"B": vB, "candidates": []}
        for c in fr["candidates"]:
            if c["rule"] == "B":
                T = B
            elif c["rule"].startswith("B&S"):
                k, h = c["kappa"], c["hazard"]; T = [x for x in B if signal(x["row"], k, h)]
            else:
                T = rule_S(rows, c["kappa"], c["hazard"], c.get("no_lo", 0.50), c.get("no_hi", 0.97))
            v = stats(T); v["half1"], v["half2"] = half_stats(T, ec); v["rule"] = c["rule"]; v["beta"] = beta_lo(T)
            bm = {x["t"]: x["ret"] for x in B}
            v["incr_vs_B"] = v.get("ret_per_dollar", float("nan")) - vB.get("ret_per_dollar", float("nan")) if v.get("n") and vB.get("n") else float("nan")
            res["candidates"].append(v)
            L.append(fmt(c["rule"], v) + f" halves {v.get('half1', float('nan')):+.1%}/{v.get('half2', float('nan')):+.1%} incr vs B {v['incr_vs_B']:+.1%} beta {v['beta']}")
            for x in sorted(T, key=lambda x: x["ts"]):
                L.append(f"      {x['t']:40s} {f(x['ts'])} tau={x['tau']:5.1f} bid={x['bid']:.2f} px={x['px']:.2f} won={x['won']} ret={x['ret']:+.2f}")
        (OUT / "validation.txt").write_text("\n".join(L) + "\n")
        (OUT / "validation.json").write_text(json.dumps(res, indent=1, default=str))
        print("\n".join(L))


if __name__ == "__main__" and (len(sys.argv) < 2 or sys.argv[1] in ("disc", "val")):
    main(sys.argv[1] if len(sys.argv) > 1 else "disc")


def robustness() -> None:
    """Post-validation context only (not used for any choice): full sample, and with SpaceX Starship ladders removed."""
    import statistics as st2
    M, E, C, S, MS, ec, evs, disc_e, val_e = universe()
    L = ["POST-VALIDATION CONTEXT (no choice made from it)"]
    allE = set(evs)
    for lab, es in (("ALL", allE), ("ALL non-Starship", {e for e in allE if "STARSHIP" not in e}),
                    ("DISC non-Starship", {e for e in disc_e if "STARSHIP" not in e}),
                    ("VAL non-Starship", {e for e in val_e if "STARSHIP" not in e})):
        rows = [r for r in S if r["e"] in es]; mk = near_markets(S, es)
        B = rule_B([r for r in MS if r["e"] in es], mk); vB = stats(B)
        L.append(f"{lab}: near-rung markets {len(mk)}"); L.append(fmt("B", vB))
        for k, h, lo in ((1.25, "ls", 0.30), (1.5, "next", 0.30), (1.5, "ls", 0.50)):
            T = rule_S(rows, k, h, lo, 0.97); v = stats(T)
            bm = {x["t"]: x["ret"] for x in B}; both = [x for x in T if x["t"] in bm]
            pd = st2.mean(x["ret"] - bm[x["t"]] for x in both) if both else float("nan")
            inc = v.get("ret_per_dollar", float("nan")) - vB.get("ret_per_dollar", float("nan")) if v.get("n") else float("nan")
            L.append(fmt(f"S k{k} {h} NO{lo:.2f}", v) + f" incr vs B {inc:+.1%}; paired n={len(both)} {pd:+.1%}")
    (OUT / "robustness.txt").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "robust":
    robustness()
