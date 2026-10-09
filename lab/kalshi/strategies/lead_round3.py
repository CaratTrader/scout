"""Lead check, round 3 (no Kalshi calls; reads cached outputs of r3_long_dated_longshot_no only).

Question: is the long-dated favourite-NO result a cherry-picked band, or does the whole NO price curve on
long-dated Kalshi markets sit below the realised win rate? And how much of the evidence survives when the
cluster is the SERIES (one regime, e.g. GOVSHUT 2021-24) or the MARKET (D60/D30/D14 entries in one market
share one outcome)?

Data: the adversarial reproducer's anchor (repro/trades_maxage72.jsonl = the original 70-market sample,
deadline parser fixed) plus repro/expansion_trades.jsonl (148 markets never sampled by the original
researcher). Polymarket: poly/cells.json is not re-derived here; numbers are quoted from poly/analysis.txt.

Run: .venv/bin/python -m lab.kalshi.strategies.lead_round3
Output: data/kalshi_lab/strategies/lead_round3/long_dated_check.json
"""
from __future__ import annotations

import json
import math
import os
import statistics as st
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SRC = os.path.join(ROOT, "data/kalshi_lab/strategies/r3_long_dated_longshot_no/repro")
OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/lead_round3")
MACRO = ("GDP", "FED", "RATECUT", "RECSS", "FEDDECISION")


def load() -> list[dict]:
    rows, seen = [], set()
    for f in ("trades_maxage72.jsonl", "expansion_trades.jsonl"):
        for line in open(os.path.join(SRC, f)):
            r = json.loads(line)
            if r["side"] != "NO":
                continue
            k = (r["t"], r["D"])
            if k in seen:
                continue
            seen.add(k)
            s = r["series"].removeprefix("KX")
            r["series_n"] = s
            r["macro"] = any(s.startswith(m) for m in MACRO)
            rows.append(r)
    return rows


def clustered(rows: list[dict], key: str) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[r[key]].append(r["ret"])
    m = [st.mean(v) for v in g.values()]
    sd = st.pstdev(m) if len(m) > 1 else 0.0
    t = st.mean(m) / (sd / math.sqrt(len(m))) if len(m) > 2 and sd > 0 else float("nan")
    return {"clusters": len(m), "cluster_mean": round(st.mean(m), 4), "t": round(t, 2)}


def wilson_lo(k: int, n: int, z: float = 1.645) -> float:
    if n == 0:
        return float("nan")
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - h) / d


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    first = {}
    for r in sorted(rows, key=lambda r: r["fill_ts"]):
        first.setdefault(r["t"], r)
    fm = list(first.values())
    k = sum(r["won"] for r in fm)
    px = st.mean(r["px"] for r in fm)
    lo = wilson_lo(k, len(fm))
    # per-$ return implied by the 95% one-sided lower bound of the win rate at the average price (fee ~0.07p(1-p))
    fee = 0.07 * px * (1 - px)
    return {
        "n": len(rows), "markets": len(fm), "events": len({r["e"] for r in rows}), "series": len({r["series_n"] for r in rows}),
        "win": round(st.mean(r["won"] for r in rows), 4), "px": round(st.mean(r["px"] for r in rows), 4),
        "ret": round(st.mean(r["ret"] for r in rows), 4),
        "by_event": clustered(rows, "e"), "by_market": clustered(rows, "t"), "by_series": clustered(rows, "series_n"),
        "first_entry_per_market": {"n": len(fm), "win": round(k / len(fm), 4), "px": round(px, 4),
                                   "ret": round(st.mean(r["ret"] for r in fm), 4),
                                   "win_lo95": round(lo, 4), "ret_at_win_lo95": round((lo - px - fee) / (px + fee), 4)},
    }


def main() -> None:
    rows = load()
    bands = [(0.50, 0.70), (0.70, 0.80), (0.80, 0.86), (0.86, 0.92), (0.92, 0.97), (0.80, 0.92), (0.70, 0.97)]
    out = {"source": SRC, "note": "NO side only, one row per (market, D), D in {60,45,30,21,14} days before the scheduled deadline", "cells": {}}
    for lo, hi in bands:
        sel = [r for r in rows if lo <= r["ps"] < hi]
        out["cells"][f"NO {lo:.2f}-{hi:.2f} allD"] = summarize(sel)
        out["cells"][f"NO {lo:.2f}-{hi:.2f} allD macro"] = summarize([r for r in sel if r["macro"]])
        out["cells"][f"NO {lo:.2f}-{hi:.2f} allD nonmacro"] = summarize([r for r in sel if not r["macro"]])
    c1 = [r for r in rows if 0.80 <= r["ps"] < 0.92 and r["D"] in (60, 30, 14)]
    out["cells"]["C1 NO 0.80-0.92 D60/30/14"] = summarize(c1)
    # contribution by series for C1
    g = defaultdict(list)
    for r in c1:
        g[r["series_n"]].append(r)
    out["C1_by_series"] = {s: {"n": len(v), "losses": sum(not r["won"] for r in v), "ret": round(st.mean(r["ret"] for r in v), 4)} for s, v in sorted(g.items(), key=lambda kv: -len(kv[1]))}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "long_dated_check.json"), "w") as f:
        json.dump(out, f, indent=1)
    for k, v in out["cells"].items():
        if v.get("n"):
            fe = v["first_entry_per_market"]
            print(f"{k:34s} n={v['n']:3d} mk={v['markets']:3d} ev={v['events']:3d} ser={v['series']:2d} win={v['win']:.3f} px={v['px']:.3f} ret={v['ret']:+.3f} "
                  f"t_ev={v['by_event']['t']:6} t_ser={v['by_series']['t']:6} | first/mkt n={fe['n']} win={fe['win']:.3f} ret={fe['ret']:+.3f} win_lo95={fe['win_lo95']:.3f} ret@lo95={fe['ret_at_win_lo95']:+.3f}")
    print(json.dumps(out["C1_by_series"]))


if __name__ == "__main__":
    main()
