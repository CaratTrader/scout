"""Descriptive extras for r5_announced_date_slip (after the one-time validation; nothing here selects a rule).

1. Pooled (discovery + validation) hypothesis-core cell 'now | 0.40 | both | NO 0.30-0.60' and its plain-band
   control, by issuer family: is the slip premium only Starship?
2. Fill-staleness sensitivity: carry the last hourly quote forward up to 6 h (instead of requiring a candle in
   [t + 1 h, t + 2 h]); recovers pairs skipped for 'no_fill_quote'.
3. The round-4 hint restated on this data: Starship rungs, YES bid 0.45-0.70 at the fill, D-7 / D-3.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_announced_date_slip_extras"""
from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import lab.kalshi.strategies.r5_announced_date_slip as M  # noqa: E402
from lab.kalshi.strategies.r5_announced_date_slip_common import OUT  # noqa: E402


def by_fam(rows):
    out = {}
    for f in sorted({r["fam"] for r in rows}):
        x = [r for r in rows if r["fam"] == f]
        out[f] = {"n": len(x), "events": len({r["e"] for r in x}), "ret": round(st.mean(r["ret"] for r in x), 3),
                  "win": round(sum(r["won"] for r in x) / len(x), 2), "px": round(st.mean(r["p"] for r in x), 3)}
    return out


def core(base, label):
    res = {}
    for mode in ("now", "ever"):
        rows = [r for r in base if r["flags"][f"{mode}0.40"] and 0.30 <= r["p"] <= 0.60]
        res[f"{mode}|0.40|hboth|no30-60|pooled"] = {**M.stats(rows), "by_family": by_fam(rows)}
    ctrl = [r for r in base if 0.30 <= r["p"] <= 0.60]
    res["control|hboth|no30-60|pooled"] = {**M.stats(ctrl), "by_family": by_fam(ctrl)}
    nont = [r for r in ctrl if not r["flags"]["now0.40"]]
    res["non-target|hboth|no30-60|pooled"] = {**M.stats(nont), "by_family": by_fam(nont)}
    print(f"\n[{label}]")
    for k, v in res.items():
        print(f"  {k:34s} n={v.get('n')} ev={v.get('events')} win={v.get('win')} px={v.get('avg_px')} ret={v.get('ret_per_dollar')} t={v.get('t')} wo3={v.get('ret_wo3')}")
        for f, s in v["by_family"].items():
            print(f"      {f:16s} {s}")
    return res


def main():
    base, meta = M.build()
    out = {"strict_fill": core(base, f"strict fill, base {len(base)} pairs {meta}")}
    orig = M.fill

    def fill6(c, t):
        last = None
        for r in c:
            if r[0] <= t + M.H:
                last = r
            else:
                break
        if last is None or t + M.H - last[0] > 6 * M.H or last[1] is None or last[2] is None:
            return orig(c, t)
        return last[1], last[2], sum(x[4] for x in c if t - 24 * M.H < x[0] <= t)
    M.fill = fill6
    base6, meta6 = M.build()
    out["carry6h_fill"] = core(base6, f"carry-forward <= 6 h, base {len(base6)} pairs {meta6}")
    M.fill = orig
    hint = [r for r in base if r["fam"] == "spacex_starship" and 0.45 <= r["bid"] <= 0.70]
    out["starship_hint_yesbid_45_70"] = {**M.stats(hint), "trades": [(r["t"], r["h"], r["p"], r["won"]) for r in hint]}
    nh = [r for r in base if r["fam"] != "spacex_starship" and 0.45 <= r["bid"] <= 0.70]
    out["non_starship_yesbid_45_70"] = {**M.stats(nh), "trades": [(r["t"], r["h"], r["p"], r["won"]) for r in nh]}
    print("\n[starship hint] ", {k: out["starship_hint_yesbid_45_70"].get(k) for k in ("n", "events", "win", "avg_px", "ret_per_dollar", "t")})
    print("[non-starship]  ", {k: out["non_starship_yesbid_45_70"].get(k) for k in ("n", "events", "win", "avg_px", "ret_per_dollar", "t")})
    for x in out["non_starship_yesbid_45_70"]["trades"]:
        print("     ", x)
    json.dump(out, open(OUT / "extras.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
