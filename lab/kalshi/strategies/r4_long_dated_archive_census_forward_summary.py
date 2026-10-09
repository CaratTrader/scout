"""Summary of the r4_long_dated_archive_census forward paper test (reads forward.jsonl only; no Kalshi calls).

Joins paper fills to settle rows, computes per-cell statistics with the census definitions (common.stats: return per $,
loss rate, event-clustered t, CP / Jeffreys lower bounds on unique events) and checks the frozen gate and kill rules of
preregistration.json. Output: data/kalshi_lab/strategies/r4_long_dated_archive_census/forward_summary.json
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_archive_census_forward_summary"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_archive_census_api import OUT  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_archive_census_common import stats  # noqa: E402

FWD = OUT / "forward.jsonl"


def main() -> dict:
    rows = [json.loads(l) for l in FWD.open()] if FWD.exists() else []
    kinds = Counter(r["type"] for r in rows)
    res = {r["ticker"]: r["result"] for r in rows if r["type"] == "settle"}
    fills = [r for r in rows if r["type"] == "fill" and r.get("contracts", 0) > 0]
    settled, open_ = {}, {}
    for f in fills:
        if f["ticker"] in res:
            won = res[f["ticker"]] == "no"
            n = f["contracts"]
            pnl = (1.0 if won else 0.0) - f["px"] - f["fee"] / n
            g = time.gmtime(f["ts"])
            settled.setdefault(f["cell"], []).append({**f, "t": f["ticker"], "won": won, "pnl": pnl, "ret": pnl / f["px"], "fill_ts": f["ts"],
                                                       "quarter": f"{g.tm_year}Q{(g.tm_mon - 1) // 3 + 1}"})
        else:
            open_[f["cell"]] = open_.get(f["cell"], 0) + 1
    out = {"rows": dict(kinds), "passes": kinds.get("pass", 0), "calls": sum(r.get("calls", 0) for r in rows if r["type"] == "pass"),
           "first_pass": min((r["ts"] for r in rows if r["type"] == "pass"), default=None), "open_fills": open_, "cells": {}}
    for c in ("B", "C1", "M"):
        v = stats(settled.get(c, []))
        out["cells"][c] = v
    b = out["cells"]["B"]
    n = b.get("n", 0)
    verdict = "running"
    if n >= 20 and (b["loss_rate"] >= 0.10 or b["ret_per_dollar"] < 0):
        verdict = "KILL (pre-registered kill rule)"
    elif n >= 40:
        ok = (b["ret_per_dollar"] >= 0.10 and b["loss_rate"] < 0.10 and (b["bounds"].get("ret_at_cp_lo95") or -1) > 0
              and (b["ret_wo3"] or -1) > 0 and min(b["half1"] or -1, b["half2"] or -1) > 0)
        verdict = "gate rows 1-5 + (d) met: check the K-adjusted t bar and census-backtest consistency" if ok else "gate not met"
    out["verdict_B"] = verdict
    (OUT / "forward_summary.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: out[k] for k in ("rows", "passes", "calls", "open_fills", "verdict_B")}))
    for c, v in out["cells"].items():
        if v.get("n"):
            print(c, {k: v[k] for k in ("n", "events", "win", "avg_px", "ret_per_dollar", "t", "ret_wo3")}, v["bounds"])
    return out


if __name__ == "__main__":
    main()
