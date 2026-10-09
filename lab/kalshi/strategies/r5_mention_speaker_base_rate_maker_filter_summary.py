"""Summary of the r5_mention_speaker_base_rate_maker_filter forward overlay (no Kalshi calls).

Reads data/kalshi_lab/strategies/r5_mention_speaker_base_rate_maker_filter/forward.jsonl (tag / fill / settle rows),
joins them per ticker and reports the pre-registered arms (preregistration.json):
  F_LOW40_PRE (primary, exploratory), F_LOW20_PRE, F_NH_PRE, F_HIGH40_PRE, F_ALL_PRE: r4e (frozen series) + r4s orders
  with pool_complete tags;  R3M_<bucket>: r3 mention-maker orders (reported only).
An order counts once its fill is known and its market settled yes/no (void dropped). Fills are through-only contracts
(gate amendment c); maker fee 0. The kill rules are evaluated only at the decision point (30 filled events in
F_LOW40_PRE, or 2027-01-31); before that the status is 'running' whatever the numbers show.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_mention_speaker_base_rate_maker_filter_summary
Output: summary.json next to forward.jsonl"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_mention_speaker_base_rate_maker_filter import binom_lo, mh_lor
from lab.kalshi.strategies.r5_mention_speaker_base_rate_maker_filter_logger import OUT

DECISION_EVENTS = 30
DECISION_DATE = 1801371600      # 2027-01-31 00:00 ET
BUCKETS = ("LOW40", "LOW20", "NH", "HIGH40")


def load() -> list[dict]:
    f = OUT / "forward.jsonl"
    if not f.exists():
        return []
    tags, fills, res = {}, {}, {}
    for line in f.open():
        r = json.loads(line)
        if r["type"] == "tag":
            tags.setdefault(r["ticker"], r)
        elif r["type"] == "fill":
            fills.setdefault(r["ticker"], r)
        elif r["type"] == "settle":
            res.setdefault(r["ticker"], r["result"])
    out = []
    for t, g in tags.items():
        o = {**g, "fill_known": t in fills, "result": res.get(t)}
        o["q"] = fills[t]["through"] if t in fills else None
        o["q_any"] = fills[t]["any"] if t in fills else None
        o["filled"] = bool(o["q"]) and o["q"] > 0
        o["px"] = g["no_px"]
        o["no_won"] = res.get(t) == "no"
        o["ret"] = ((1.0 if o["no_won"] else 0.0) - o["px"]) / o["px"] if o["px"] else None
        out.append(o)
    return out


def arm_stats(X: list[dict]) -> dict:
    R = [o for o in X if o["fill_known"] and o["result"] in ("yes", "no")]
    F = [o for o in R if o["filled"]]
    U = [o for o in R if not o["filled"]]
    out = {"tagged": len(X), "resolved": len(R), "filled": len(F), "filled_events": len({o["e"] for o in F}),
           "filled_NO_win": round(st.mean(o["no_won"] for o in F), 4) if F else None,
           "unfilled_NO_win": round(st.mean(o["no_won"] for o in U), 4) if U else None}
    out["gap"] = round(out["unfilled_NO_win"] - out["filled_NO_win"], 4) if F and U else None
    by_src = defaultdict(list)
    for o in R:
        by_src[o["src"]].append(o)
    lor = mh_lor(list(by_src.values())) if R else None
    out["mh_lor"] = round(lor, 4) if lor is not None else None
    if not F:
        return out
    cost = sum(o["q"] * o["px"] for o in F); pnl = sum(o["q"] * ((1.0 if o["no_won"] else 0.0) - o["px"]) for o in F)
    ev = defaultdict(lambda: [0.0, 0.0])
    for o in F:
        ev[o["e"]][0] += o["q"] * ((1.0 if o["no_won"] else 0.0) - o["px"]); ev[o["e"]][1] += o["q"] * o["px"]
    em = sorted((p / c for p, c in ev.values() if c > 0), reverse=True)
    ne = len(em); sd = st.pstdev(em) if ne > 1 else 0.0
    first = {}
    for o in sorted(F, key=lambda o: (o["post"], o["ticker"])):
        first.setdefault(o["e"], o)
    fe = list(first.values()); k = sum(o["no_won"] for o in fe); pxe = st.mean(o["px"] for o in fe)
    wlo = binom_lo(k, len(fe))
    srt = sorted(F, key=lambda o: o["listing"]); mid = srt[len(srt) // 2]["listing"]

    def sw(rows):
        c = sum(o["q"] * o["px"] for o in rows)
        return round(sum(o["q"] * ((1.0 if o["no_won"] else 0.0) - o["px"]) for o in rows) / c, 4) if c > 0 else None
    qs = sorted(o["q"] for o in F)
    out.update({"size_weighted_ret": round(pnl / cost, 4) if cost > 0 else None, "staked": round(cost, 2), "pnl": round(pnl, 2),
                "equal_dollar_ret": round(st.mean(o["ret"] for o in F), 4), "avg_px": round(st.mean(o["px"] for o in F), 4),
                "event_t": round(st.mean(em) / (sd / math.sqrt(ne - 1)), 2) if ne > 2 and sd > 0 else None,
                "event_mean_wo3": round(st.mean(em[3:]), 4) if ne > 3 else None,
                "half1_sw": sw([o for o in srt if o["listing"] < mid]), "half2_sw": sw([o for o in srt if o["listing"] >= mid]),
                "beta_lo_ret": round((wlo - pxe) / pxe, 4), "median_filled_contracts": qs[len(qs) // 2]})
    return out


def main() -> dict:
    O = load()
    now = time.time()
    pre = [o for o in O if o.get("pre_cancel_arm") and o.get("pool_complete")]
    arms = {"F_ALL_PRE": arm_stats(pre)}
    for b in BUCKETS:
        sel = [o for o in pre if (o["bucket"] in ("LOW20", "LOW40") if b == "LOW40" else o["bucket"] == b)]
        arms[f"F_{b}_PRE"] = arm_stats(sel)
    r3 = [o for o in O if o["src"] == "r3m"]
    for b in ("ALL",) + BUCKETS:
        sel = r3 if b == "ALL" else [o for o in r3 if (o["bucket"] in ("LOW20", "LOW40") if b == "LOW40" else o["bucket"] == b)]
        arms[f"R3M_{b}"] = arm_stats(sel)
    p = arms["F_LOW40_PRE"]; a = arms["F_ALL_PRE"]
    due = (p.get("filled_events") or 0) >= DECISION_EVENTS or now >= DECISION_DATE
    status = "running"
    if due:
        fails = []
        if (p.get("size_weighted_ret") is None) or p["size_weighted_ret"] < 0.10:
            fails.append("size-weighted < +10%")
        if (p.get("event_mean_wo3") is None) or p["event_mean_wo3"] < 0:
            fails.append("mean without 3 best events < 0")
        if (p.get("beta_lo_ret") is None) or p["beta_lo_ret"] <= 0:
            fails.append("binomial bound <= 0")
        if p.get("filled_NO_win") is None or a.get("filled_NO_win") is None or p["filled_NO_win"] - a["filled_NO_win"] < 0.15:
            fails.append("filled NO-win not >= 15 pts above F_ALL_PRE")
        status = "KILL: " + "; ".join(fails) if fails else "passes the forward kill rules (not a gate pass)"
    out = {"written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "tags": len(O),
           "tags_incomplete_pool": sum(1 for o in O if not o.get("pool_complete")),
           "by_bucket": {b: sum(1 for o in O if o["bucket"] == b) for b in ("NH", "LOW20", "LOW40", "HIGH40")},
           "by_src": {s: sum(1 for o in O if o["src"] == s) for s in ("r4e", "r4s", "r3m")},
           "decision_due": due, "status": status, "arms": arms}
    (OUT / "summary.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    r = main()
    print(json.dumps({k: v for k, v in r.items() if k != "arms"}))
    for k, v in r["arms"].items():
        print(f"  {k:14s} {json.dumps(v)}")
