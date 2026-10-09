"""r4_long_dated_archive_census: does the round-3 long-dated NO lead survive a fresh, broad Kalshi archive census?

Pre-registration (before any data): data/kalshi_lab/strategies/r4_long_dated_archive_census/census_preregistration.json
Definitions: r4_long_dated_archive_census_common.py. Data: r4_long_dated_archive_census_data.py (frame, sample,
candles), r4_long_dated_archive_census_poly.py (fresh Polymarket sample). Forward test: _logger.py / _forward_summary.py.

Cells (frozen): C1 = NO [0.80, 0.92) at D in {60, 30, 14}, every (market, D); B = NO [0.70, 0.97) any D, first entry per
market; M = NO [0.50, 0.70) any D, first entry per market (flagged, seen in round 3).
Reports: fresh census overall, protocol split (events by latest close, 70/30), macro / non-macro, category, round-1
markets only (one market per event), S source; t clustered by event / series / quarter; CP and Jeffreys lower bounds
on unique events; kill rules.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_archive_census"""
from __future__ import annotations

import hashlib
import json
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_archive_census_api import OUT, used  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_archive_census_common import CELLS, DAY, select, stats, trades  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_archive_census_data import catalog  # noqa: E402


def load():
    S = [json.loads(l) for l in (OUT / "sample.jsonl").open()]
    C = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l)
        C[x["t"]] = x["c"]
    M = [r for r in S if r["t"] in C]
    by = defaultdict(list)
    for r in S:
        by[r["e"]].append(r)
    first = {e: sorted(v, key=lambda r: hashlib.sha1(("r4census" + r["t"]).encode()).hexdigest())[0]["t"] for e, v in by.items()}
    for r in M:
        r["round1"] = first[r["e"]] == r["t"]
    return S, M, C


def line(k: str, v: dict) -> str:
    if not v.get("n"):
        return f"  {k:40s} n=0"
    b = v["bounds"]
    f = lambda x: "  nan" if x is None else f"{x:+.3f}"
    return (f"  {k:40s} n={v['n']:3d} mk={v['markets']:3d} ev={v['events']:3d} ser={v['series']:2d} win={v['win']:.3f} px={v['avg_px']:.3f} "
            f"ret={v['ret_per_dollar']:+.3f} t_ev={v['t']} t_ser={v['t_series']} t_q={v['t_quarter']} wo3={f(v['ret_wo3'])} "
            f"h={f(v['half1'])}/{f(v['half2'])} | ev n={b['n_events']} L={b['losses']} cp_lo={b['cp_win_lo95']:.3f} ret@cp={b['ret_at_cp_lo95']:+.3f} "
            f"ret@jf={b['ret_at_jeffreys_lo95']:+.3f} p(+10%)={b['p_obs_or_better_if_true_+10pct']}")


def main() -> dict:
    S, M, C = load()
    _, mult = catalog()
    T = trades(M, C, fee_mult=mult)
    evc = defaultdict(int)
    for r in M:
        evc[r["e"]] = max(evc[r["e"]], r["close"])
    evs = sorted(evc, key=lambda e: evc[e])
    cut = evc[evs[int(len(evs) * 0.7)]]
    disc = [r for r in T if evc[r["e"]] < cut]
    val = [r for r in T if evc[r["e"]] >= cut]
    nc = sum(1 for r in M if C[r["t"]])
    lines = [f"census sample {len(S)} markets / {len({r['e'] for r in S})} events; candles fetched {len(M)} markets / {len({r['e'] for r in M})} events "
             f"({nc} with >= 1 candle); trade rows {len(T)}; markets with a row {len({r['t'] for r in T})}; split at event close "
             f"{time.strftime('%Y-%m-%d', time.gmtime(cut))} (disc {len(disc)} rows, val {len(val)} rows)"]
    res = {"cells": {}}
    groups = {"all": T, "discovery": disc, "validation": val,
              "macro": [r for r in T if r["macro"]], "nonmacro": [r for r in T if not r["macro"]],
              "round1_one_market_per_event": [r for r in T if next(m for m in M if m["t"] == r["t"])["round1"]],
              "nonmacro_excl_mentions": [r for r in T if not r["macro"] and r["cat"] != "Mentions"]}
    for g, rows in groups.items():
        lines.append(f"\n== {g}")
        for c in CELLS:
            v = stats(select(rows, c))
            res["cells"][f"{c}|{g}"] = v
            lines.append(line(c, v))
    # by category, cell B
    lines.append("\n== B by category (all)")
    bc = defaultdict(list)
    for r in select(T, "B"):
        bc[r["cat"]].append(r)
    for k, rows in sorted(bc.items(), key=lambda kv: -len(kv[1])):
        v = stats(rows)
        res["cells"][f"B|cat={k}"] = v
        lines.append(line(f"cat={k}", v))
    # validation-period halves explicitly for the protocol table
    lines.append("\n== every B trade (fill time order)")
    for r in select(T, "B"):
        lines.append(f"    {time.strftime('%Y-%m-%d', time.gmtime(r['fill_ts']))} {r['t']:44s} {str(r['cat'])[:12]:12s} D{r['D']:2d} ps={r['ps']:.2f} "
                     f"px={r['px']:.2f} won={r['won']} ret={r['ret']:+.3f}")
    lines.append("\n== every M trade")
    for r in select(T, "M"):
        lines.append(f"    {time.strftime('%Y-%m-%d', time.gmtime(r['fill_ts']))} {r['t']:44s} {str(r['cat'])[:12]:12s} D{r['D']:2d} ps={r['ps']:.2f} "
                     f"px={r['px']:.2f} won={r['won']} ret={r['ret']:+.3f}")
    # context: YES side longshots and the whole NO curve (counted as variants)
    lines.append("\n== context (not candidates): NO price curve, first entry per market, all D")
    for lo, hi in ((0.30, 0.50), (0.50, 0.70), (0.70, 0.80), (0.80, 0.86), (0.86, 0.92), (0.92, 0.97), (0.97, 0.99)):
        x = [r for r in T if r["side"] == "NO" and lo <= r["ps"] < hi]
        best = {}
        for r in sorted(x, key=lambda r: r["fill_ts"]):
            best.setdefault(r["t"], r)
        v = stats(list(best.values()))
        res["cells"][f"curve NO {lo:.2f}-{hi:.2f}"] = v
        lines.append(line(f"NO {lo:.2f}-{hi:.2f} first-entry", v))
    # capacity proxy: hourly volume (contracts) in the candle at the B signal hour and the median over the prior 72 h
    vols = []
    for r in select(T, "B"):
        c = C[r["t"]]
        w = [x[3] for x in c if r["fill_ts"] - 72 * 3600 <= x[0] <= r["fill_ts"]]
        vols.append(sum(w))
    res["capacity_volume_72h_before_fill_median"] = st.median(vols) if vols else None
    # signal rate: B entries per market sampled, and per day over the sample span
    b = select(T, "B")
    span_d = (max(r["fill_ts"] for r in T) - min(r["fill_ts"] for r in T)) / DAY if T else 0
    res["B_entries_per_sampled_market"] = round(len(b) / max(len(M), 1), 3)
    res["span_days"] = round(span_d, 1)
    res["kalshi_calls_census"] = used()
    lines.append(f"\nB entries per sampled market {res['B_entries_per_sampled_market']}; median contracts traded in the 72 h before a B fill "
                 f"{res['capacity_volume_72h_before_fill_median']}; Kalshi calls (census log) {used()}")
    txt = "\n".join(lines)
    print(txt)
    (OUT / "analysis.txt").write_text(txt + "\n")
    (OUT / "cells.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in T))
    return res


if __name__ == "__main__":
    main()
