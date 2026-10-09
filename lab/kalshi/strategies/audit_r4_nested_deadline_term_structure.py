"""Bias audit of r4_nested_deadline_term_structure (disk only, no Kalshi calls; does not touch that family's outputs).

1. Reproduces the frozen validation (B, C1-C3) with the family's own code.
2. Shows the event split is outcome-dependent: events are ordered by their latest *settled* rung close, and a ladder
   whose event happened settles all rungs at once (early), so 'X happened' events land in discovery and 'nothing
   happened yet' ladders (still listing new rungs) land in validation.
3. Re-evaluates the same frozen rules under outcome-free splits: calendar split by decision time, and event split by
   first listing time.
4. Point-in-time thinning check (thin() uses rungs listed after the snapshot).
Usage: .venv/bin/python -m lab.kalshi.strategies.audit_r4_nested_deadline_term_structure"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import lab.kalshi.strategies.r4_nested_deadline_term_structure_run as R
from lab.kalshi.strategies.r4_nested_deadline_term_structure import rule_S, rule_B, stats, fmt
from lab.kalshi.strategies.r4_nested_deadline_term_structure_parse import thin

OUT = Path(__file__).resolve().parents[3] / "data/kalshi_lab/strategies/audit_r4_nested_deadline_term_structure"
DAY = 86400
RULES = {"C1": (1.25, "ls", 0.30), "C2": (1.5, "next", 0.30), "C3": (1.5, "ls", 0.50)}


def main() -> None:
    M, E, C, S, MS, ec, evs, disc_e, val_e = R.universe()
    f = lambda x: dt.datetime.utcfromtimestamp(x).strftime("%Y-%m-%d")
    L, res = [], {}
    rows = [r for r in S if r["e"] in val_e]
    B = rule_B([r for r in MS if r["e"] in val_e], R.near_markets(S, val_e))
    res["reproduced_validation"] = {"B": stats(B)}
    L.append("REPRODUCED VALIDATION (family's own split)"); L.append(fmt("B", stats(B)))
    for k, (ka, h, lo) in RULES.items():
        T = rule_S(rows, ka, h, lo, 0.97); v = stats(T); v["beta"] = R.beta_lo(T); res["reproduced_validation"][k] = v
        L.append(fmt(k, v) + f" beta {v['beta']}")
    happened = lambda e: any(M[t]["result"] == "yes" for t in E[e]["rungs"])
    res["split_outcome_dependence"] = {"disc_events": len(disc_e), "disc_events_X_happened": sum(map(happened, disc_e)),
                                       "val_events": len(val_e), "val_events_X_happened": sum(map(happened, val_e))}
    L.append(f"\nSPLIT vs OUTCOME: {res['split_outcome_dependence']}")
    allE = set(evs)
    full = {"B": rule_B([r for r in MS if r["e"] in allE], R.near_markets(S, allE))}
    for k, (ka, h, lo) in RULES.items():
        full[k] = rule_S(S, ka, h, lo, 0.97)
    allts = sorted(x["ts"] for T in full.values() for x in T)
    eo = {e: min(M[t]["open"] for t in E[e]["rungs"]) for e in allE}
    order = sorted(allE, key=lambda e: eo[e]); vE = set(order[int(len(order) * 0.7):])
    splits = {f"calendar_{int(q * 100)}": (lambda x, c=allts[int(len(allts) * q)]: x["ts"] >= c) for q in (0.6, 0.7)}
    splits["event_first_open_70"] = lambda x: x["e"] in vE
    for name, isval in splits.items():
        L.append(f"\nOUTCOME-FREE SPLIT {name}"); res[name] = {}
        for k, T in full.items():
            V = [x for x in T if isval(x)]; D = [x for x in T if not isval(x)]
            res[name][k] = {"disc": stats(D), "val": stats(V), "val_beta": R.beta_lo(V) if V else {}}
            L.append(fmt(k + " disc", stats(D))); L.append(fmt(k + " val", stats(V)))
    lost = set()
    for e, v in E.items():
        allr = [M[t] for t in v["rungs"]]; fullset = {m["t"] for m in thin(allr)}
        if len(fullset) < 2:
            continue
        t0 = min(m["open"] for m in allr); t1 = max(min(m["close"], m["D"]) for m in allr)
        for t in range((t0 // DAY + 1) * DAY + 14 * 3600, t1, DAY):
            vp = sorted([m for m in thin([m for m in allr if m["open"] <= t]) if m["open"] <= t < min(m["close"], m["D"]) and m["D"] - t >= DAY], key=lambda m: m["D"])
            if len(vp) >= 2 and vp[0]["t"] not in fullset and vp[0]["result"] in ("yes", "no"):
                lost.add((e, vp[0]["t"], vp[0]["result"]))
    res["pit_thinning_lost_near_rungs"] = sorted(lost)
    L.append(f"\nNEAR RUNGS DROPPED BY FULL-HISTORY THINNING (would be near rungs point-in-time): {sorted(lost)}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "audit.txt").write_text("\n".join(L) + "\n")
    (OUT / "audit.json").write_text(json.dumps(res, indent=1, default=str))
    print("\n".join(L))


if __name__ == "__main__":
    main()
