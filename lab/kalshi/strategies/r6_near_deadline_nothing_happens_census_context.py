"""Post-validation context for r6_near_deadline_nothing_happens_census (descriptive only; nothing here was used to
choose or change a candidate). Full sample = discovery + validation decisions from decisions.jsonl.
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_context"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_near_deadline_nothing_happens_census import cell, stats
from lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_data import OUTP

CELLS = [("h72", "80-95", "news"), ("h24", "70-95", "news"), ("h120", "50-97", "all"), ("h72", "70-95", "news"),
         ("h72", "70-95", "launch_release"), ("first_any", "70-95", "news"), ("first_any", "70-95", "launch_release")]


def main() -> dict:
    D = [json.loads(l) for l in open(OUTP / "decisions.jsonl")]
    out = {}
    for e, b, s in CELLS:
        rows = cell(D, e, b, s)
        v = stats(rows)
        nonlaunch = [r for r in rows if r["topic"] != "launch_release"]
        v["non_launch"] = {k: stats(nonlaunch).get(k) for k in ("n", "events", "win", "avg_px", "ret_per_dollar", "t")} if nonlaunch else {}
        losses = [{"t": r["t"], "topic": r["topic"], "px": r["px"], "dec": r["dec"]} for r in rows if not r["won"]]
        v["losses"] = losses
        out[f"{e}|{b}|{s}"] = v
    # loss clustering by topic over every filled decision with NO in 0.70-0.95 (all horizons)
    agg = defaultdict(lambda: [0, 0])
    for d in D:
        if d["fill"] and 0.70 <= d["sig"] <= 0.95:
            agg[d["topic"]][0] += 1; agg[d["topic"]][1] += (not d["won"])
    out["band70_95_all_horizons_by_topic"] = {k: {"decisions": a, "losses": l} for k, (a, l) in agg.items()}
    json.dump(out, open(OUTP / "full_sample_context.json", "w"), indent=1)
    return out


if __name__ == "__main__":
    o = main()
    for k, v in o.items():
        if k.startswith("band"):
            print(k, v); continue
        b = v.get("bounds", {})
        print(f"{k:32s} n={v['n']} ev={v['events']} win={v['win']} px={v['avg_px']} ret={v['ret_per_dollar']} t={v['t']} "
              f"wo3={v['ret_wo3']} LB={b.get('beta_lb_ret')} nonlaunch={v['non_launch']} losses={len(v['losses'])}")
