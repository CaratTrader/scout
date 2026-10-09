"""Summary of the r4_long_dated_maker_no forward paper log (no Kalshi calls).

Reads data/kalshi_lab/strategies/r4_long_dated_maker_no/forward.jsonl and reports, per arm (taker, C1 static maker,
C2 daily-refresh maker, C3 = C2 with signal NO price in [0.80, 0.92)):
  signals in band, orders posted, fills (through-only, size-weighted against N; any-print reported separately),
  filled vs unfilled NO win rate (the adverse-selection check), equal-$ and size-weighted return per $ after fees,
  t clustered by event, mean without the 3 best trades, halves by post time, the exact-binomial (Clopper-Pearson)
  lower bound on the per-$ return over unique events (gate amendment d), and the incremental return of each maker arm
  over the taker arm on the same signals. Gate rows per docs/KALSHI_LAB.md on forward fills only.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_maker_no_summary [--json]"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_maker_no_core import clopper_lo
from lab.kalshi.strategies.r4_long_dated_maker_no_logger import OUT

K_FILE = Path("/Users/roomyhome/Tradeinc/data/kalshi_lab/K.json")


def z_bar() -> tuple[int, float]:
    from statistics import NormalDist
    K = 17180 + 300
    try:
        K = int(json.loads(K_FILE.read_text()).get("K", K))
    except Exception:
        pass
    return K, NormalDist().inv_cdf(1 - 0.05 / max(K, 1))


def load() -> list[dict]:
    f = OUT / "forward.jsonl"
    return [json.loads(l) for l in f.open()] if f.exists() else []


def tstat(rows: list[dict], key: str) -> float | None:
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r[key])
    m = [st.mean(v) for v in ev.values()]
    if len(m) < 3 or st.pstdev(m) == 0:
        return None
    return st.mean(m) / (st.pstdev(m) / math.sqrt(len(m)))


def arm_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["post"]); h = len(srt) // 2
    first = {}
    for r in srt:
        first.setdefault(r["e"], r)
    fe = list(first.values()); k = sum(r["won"] for r in fe); pa = st.mean(r["px"] for r in fe)
    lo = clopper_lo(k, len(fe))
    w = sum(r["f"] for r in rows)
    return {"n": len(rows), "events": len({r["e"] for r in rows}), "win": round(st.mean(r["won"] for r in rows), 4),
            "avg_px": round(st.mean(r["px"] for r in rows), 4), "ret_equal_dollar": round(st.mean(rs), 4),
            "ret_size_weighted": round(sum(r["ret"] * r["f"] for r in rows) / w, 4) if w > 0 else None,
            "t_event": round(tstat(rows, "ret"), 2) if tstat(rows, "ret") is not None else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in srt[:h]), 4) if h else None, "half2": round(st.mean(r["ret"] for r in srt[h:]), 4),
            "uniq_events": len(fe), "uniq_losses": len(fe) - k, "ret_at_win_lo95": round((lo - pa) / pa, 4) if pa else None}


def summarize() -> dict:
    R = load()
    sig = {}
    orders = {}
    fills = {}
    settles = {}
    for r in R:
        if r["type"] == "signal":
            sig[(r["ticker"], r["D"])] = r
        elif r["type"] == "order" and r.get("posted"):
            orders[r["aid"]] = r
        elif r["type"] == "fill":
            fills[r["aid"]] = r
        elif r["type"] == "settle":
            settles[r["aid"]] = r
    passes = [r for r in R if r["type"] == "pass"]
    arms = defaultdict(list)
    unfilled = defaultdict(list)
    for aid, s in settles.items():
        t, D, arm = aid.split("|")
        sg = sig.get((t, int(D)), {})
        base = {"aid": aid, "e": sg.get("e", t), "post": orders.get(aid, {}).get("ts", s["ts"]), "won": s["won"], "px": s["px"], "ret": s["ret_per_dollar"],
                "f": s.get("f_through") or 0.0, "no_signal": sg.get("no_taker")}
        groups = [arm] + (["C3"] if arm == "C2" and s.get("c3") else [])
        for g in groups:
            if arm == "taker" or base["f"] > 0:
                arms[g].append(base)
            else:
                unfilled[g].append(base)
    K, z = z_bar()
    out = {"passes": len(passes), "calls": sum(p.get("calls", 0) for p in passes), "signals_logged": len(sig),
           "signals_in_band": sum(1 for s in sig.values() if s.get("in_band")), "orders_posted": len(orders), "fills_judged": len(fills),
           "settled_arms": len(settles), "K": K, "bar_t": round(z, 2), "arms": {}}
    taker_by = {(a["aid"].split("|")[0], a["aid"].split("|")[1]): a for a in arms.get("taker", [])}
    for g in ("taker", "C1", "C2", "C3"):
        a = arm_stats(arms.get(g, []))
        a["unfilled_settled"] = len(unfilled.get(g, []))
        a["win_unfilled"] = round(st.mean(r["won"] for r in unfilled[g]), 4) if unfilled.get(g) else None
        if g != "taker" and arms.get(g):
            paired = [(r["ret"], taker_by[(r["aid"].split("|")[0], r["aid"].split("|")[1])]["ret"]) for r in arms[g]
                      if (r["aid"].split("|")[0], r["aid"].split("|")[1]) in taker_by]
            a["incremental_vs_taker_same_signals"] = round(st.mean(x - y for x, y in paired), 4) if paired else None
            a["adverse_selection_kill"] = (a["win_unfilled"] is not None and a["win"] <= a["win_unfilled"] - 0.05)
        n = a.get("n", 0)
        a["gate"] = {"n>=40": n >= 40, "ret>=+10%": (a.get("ret_size_weighted") or a.get("ret_equal_dollar") or -1) >= 0.10 if n else False,
                     "t>=bar": (a.get("t_event") or 0) >= z, "binomial_lb>0": (a.get("ret_at_win_lo95") or -1) > 0,
                     "wo3>0": (a.get("ret_wo3") or -1) > 0, "halves>0": n > 1 and min(a.get("half1") or -1, a.get("half2") or -1) > 0}
        out["arms"][g] = a
    return out


def main() -> None:
    s = summarize()
    if "--json" in sys.argv:
        print(json.dumps(s, indent=1)); return
    print(f"passes {s['passes']} calls {s['calls']} signals {s['signals_logged']} in band {s['signals_in_band']} orders {s['orders_posted']} "
          f"fills judged {s['fills_judged']} settled arms {s['settled_arms']} (K={s['K']}, bar t >= {s['bar_t']})")
    for g, a in s["arms"].items():
        print(f"  {g:5} {json.dumps(a)}")
    (OUT / "forward_summary.json").write_text(json.dumps(s, indent=1))


if __name__ == "__main__":
    main()
