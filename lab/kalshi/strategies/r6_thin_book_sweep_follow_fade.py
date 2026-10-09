"""r6_thin_book_sweep_follow_fade: does a taker reacting 2 minutes after a sweep in a thin Kalshi book make money by
following the sweep (informed flow not yet absorbed) or by fading it (narrative overshoot)?

Hypothesis. In books with < 5,000 contracts of prior volume, a minute with volume >= 10x the trailing 6-h median (and
>= 10 contracts) that moves yes_bid or yes_ask by >= 15c is either informed flow the book has not finished absorbing 2 min
later (FOLLOW pays) or a retail overshoot that reverts (FADE pays). We cannot win a latency race from a home Mac polling
REST at ~1 req/s, so the edge, if any, must survive a 2-minute delay and the post-sweep spread.

Steps (0 Kalshi calls unless a cell survives):
  1. lab/kalshi/strategies/r6_thin_book_sweep_follow_fade_data.py detects triggers (frozen SPEC, no outcome).
  2. Here: fill each arm at the quote 2 min after the trigger minute, hold to settlement, $5-stake fee rounding.
     Split events 70/30 by first trigger time. Discovery cells (all counted): arms x {pooled, category, fill-price band,
     move size}, plus secondary universes (refractory all-triggers; event categories without the prior-volume cap).
  3. Kill rule from the round-5 lead on discovery; up to 3 frozen candidates go to validation once.

Run: .venv/bin/python -m lab.kalshi.strategies.r6_thin_book_sweep_follow_fade [discovery|validate]
"""
from __future__ import annotations

import gzip
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

from lab.kalshi.calib import cell_stats
from lab.kalshi.strategies.r6_thin_book_sweep_follow_fade_data import OUT, SPEC

FILL = SPEC["fill"]


def fee_pc(px: float) -> float:
    n = max(1, math.floor(FILL["stake_usd"] / px))
    cents = math.ceil(7.0 * px * (1 - px) * n - 1e-9)
    return cents / 100.0 / n


def quote_at(c: list, t: int, max_age: int | None = FILL["max_quote_age_s"]):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or (max_age is not None and t - best[0] > max_age):
        return None
    return best


def mid(q):
    if q is None or q[1] is None or q[2] is None:
        return None
    return (q[1] + q[2]) / 2


def load(path: Path = OUT / "triggers.jsonl.gz") -> list[dict]:
    with gzip.open(path, "rt") as fh:
        return [json.loads(l) for l in fh]


def trade(x: dict, arm: str, res: dict) -> dict | None:
    d = x["dir"] if arm == "FOLLOW" else -x["dir"]
    tf = x["ts"] + FILL["delay_s"]
    if x.get("close") and tf >= x["close"]:
        return None
    q = quote_at(x["c"], tf)
    if q is None:
        return None
    if d > 0:
        if q[1] is None:
            return None
        px, side = q[1], "YES"
    else:
        if q[2] is None:
            return None
        px, side = 1 - q[2], "NO"
    lo, hi = FILL["px_range"]
    if not (lo <= px <= hi):
        return None
    r = res.get(x["t"])
    if r not in ("yes", "no"):
        return None
    won = (r == "yes") == (side == "YES")
    f = fee_pc(px)
    ret = ((1.0 if won else 0.0) - px - f) / px
    # 30-minute diagnostics
    t30 = x["ts"] + 1800
    if x.get("close"):
        t30 = min(t30, x["close"] - 1)
    q30 = quote_at(x["c"], t30, None)
    m2, m30 = mid(q), mid(q30)
    move = (m30 - m2) * d if (m2 is not None and m30 is not None) else None
    mtm = None
    if q30 is not None:
        ex = q30[2] if side == "YES" else (1 - q30[1] if q30[1] is not None else None)
        if ex is not None:
            mtm = (ex - px - f - (fee_pc(ex) if 0 < ex < 1 else 0.0)) / px
    dmove = max(abs(x["trig"][1] - x["prev"][1]), abs(x["trig"][2] - x["prev"][2]))
    return {"t": x["t"], "e": x["e"], "cat": x["cat"], "series": x["series"], "ts": x["ts"], "arm": arm, "side": side, "px": px,
            "fee": f, "won": won, "ret": ret, "move30": move, "mtm30": mtm, "dmove": dmove, "prior_vol": x["prior_vol"], "vol": x["vol"],
            "spread_fill": (q[1] - q[2]) if (q[1] is not None and q[2] is not None) else None,
            "cap_proxy": sum(r[3] for r in x["c"] if tf < r[0] <= tf + 600)}


def split_events(T: list[dict], frac: float = 0.7):
    first = {}
    for x in T:
        first[x["e"]] = min(first.get(x["e"], 1e18), x["ts"])
    order = sorted(first, key=lambda e: first[e])
    cut = int(len(order) * frac)
    disc = set(order[:cut])
    return disc, (first[order[cut]] if cut < len(order) else None)


def summ(rows: list[dict]) -> dict | None:
    if len(rows) < 3:
        return None
    s = cell_stats(rows)
    mv = [r["move30"] for r in rows if r["move30"] is not None]
    mt = [r["mtm30"] for r in rows if r["mtm30"] is not None]
    s.update({"move30_median": st.median(mv) if mv else None, "move30_mean": st.mean(mv) if mv else None,
              "mtm30_mean": st.mean(mt) if mt else None, "cap_proxy_median": st.median(r["cap_proxy"] for r in rows),
              "spread_median": st.median([r["spread_fill"] for r in rows if r["spread_fill"] is not None] or [float("nan")])})
    s["kill"] = bool((s["move30_median"] is None or s["move30_median"] < 0.03) or s["ret"] < 0.05)
    return s


def band(px):
    return "px<0.30" if px < 0.30 else ("px0.30-0.70" if px <= 0.70 else "px>0.70")


def cells(rows: list[dict]) -> dict:
    out = {}
    for arm in ("FOLLOW", "FADE"):
        a = [r for r in rows if r["arm"] == arm]
        out[f"{arm}|pooled"] = summ(a)
        for cat in sorted({r["cat"] for r in a}):
            out[f"{arm}|cat={cat}"] = summ([r for r in a if r["cat"] == cat])
        for b in ("px<0.30", "px0.30-0.70", "px>0.70"):
            out[f"{arm}|{b}"] = summ([r for r in a if band(r["px"]) == b])
        out[f"{arm}|move15-25c"] = summ([r for r in a if r["dmove"] < 0.25])
        out[f"{arm}|move>=25c"] = summ([r for r in a if r["dmove"] >= 0.25])
        out[f"{arm}|ex_weather"] = summ([r for r in a if r["cat"] != "weather"])
        out[f"{arm}|spread<=4c"] = summ([r for r in a if r["spread_fill"] is not None and r["spread_fill"] <= 0.04])
    return {k: v for k, v in out.items() if v is not None}


def fmt(k, v):
    return (f"  {k:28s} n={v['n']:5d} ev={v['events']:5d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} "
            f"wo3={v['ret_wo3']:+.1%} mv30med={v['move30_median'] if v['move30_median'] is None else round(v['move30_median'], 3)} "
            f"mv30mean={(v['move30_mean'] or 0):+.3f} mtm30={(v['mtm30_mean'] or 0):+.1%} spr={v['spread_median']:.2f} kill={v['kill']}")


def discovery(variant: str = "") -> dict:
    X = load(OUT / (f"triggers_{variant}.jsonl.gz" if variant else "triggers.jsonl.gz"))
    res = json.loads((OUT / "outcomes.json").read_text())
    disc, cut_ts = split_events(X)
    rows = []
    for x in X:
        for arm in ("FOLLOW", "FADE"):
            r = trade(x, arm, res)
            if r:
                r["split"] = "disc" if x["e"] in disc else "val"
                rows.append(r)
    D = [r for r in rows if r["split"] == "disc"]
    C = cells(D)
    print(f"triggers {len(X)}; trades {len(rows)}; discovery trades {len(D)}; cut ts {cut_ts}")
    for k, v in C.items():
        print(fmt(k, v))
    out = {"variant": variant or "A_primary", "n_triggers": len(X), "cut_ts": cut_ts, "discovery_cells": C, "n_cells": len(C)}
    (OUT / (f"discovery_{variant}.json" if variant else "discovery.json")).write_text(json.dumps(out, indent=1, default=str))
    return out




def rows_for(variant: str = ""):
    X = load(OUT / (f"triggers_{variant}.jsonl.gz" if variant else "triggers.jsonl.gz"))
    res = json.loads((OUT / "outcomes.json").read_text())
    disc, cut_ts = split_events(X)
    rows = []
    for x in X:
        for arm in ("FOLLOW", "FADE"):
            r = trade(x, arm, res)
            if r:
                r["split"] = "disc" if x["e"] in disc else "val"
                rows.append(r)
    return X, res, disc, cut_ts, rows


def diag() -> dict:
    """Discovery only: is the sweep informative (pre- vs post-sweep mid vs outcome) and is the post-sweep mid biased?"""
    X, res, disc, cut_ts, _ = rows_for()
    out = {}
    for name, sel in (("all", lambda x: True), ("weather", lambda x: x["cat"] == "weather"), ("ex_weather", lambda x: x["cat"] != "weather")):
        pre_b, post_b, fill_b, gap = [], [], [], []
        for x in X:
            if x["e"] not in disc or not sel(x) or res.get(x["t"]) not in ("yes", "no"):
                continue
            y = 1.0 if res[x["t"]] == "yes" else 0.0
            mp = (x["prev"][1] + x["prev"][2]) / 2; mt = (x["trig"][1] + x["trig"][2]) / 2
            q = quote_at(x["c"], x["ts"] + FILL["delay_s"]); mf = mid(q)
            if mf is None:
                continue
            pre_b.append((mp - y) ** 2); post_b.append((mt - y) ** 2); fill_b.append((mf - y) ** 2)
            # outcome of the followed side minus its fill-time mid
            pf = mf if x["dir"] > 0 else 1 - mf
            yf = y if x["dir"] > 0 else 1 - y
            gap.append(yf - pf)
        if gap:
            se = st.pstdev(gap) / math.sqrt(len(gap))
            out[name] = {"n": len(gap), "brier_pre": st.mean(pre_b), "brier_trigger": st.mean(post_b), "brier_fill": st.mean(fill_b),
                         "follow_side_outcome_minus_fill_mid": st.mean(gap), "se": se}
    print(json.dumps(out, indent=1))
    (OUT / "diag_discovery.json").write_text(json.dumps(out, indent=1))
    return out


FROZEN = OUT / "frozen.json"


def freeze() -> None:
    if FROZEN.exists():
        print("already frozen"); return
    FROZEN.write_text(json.dumps({
        "frozen_at": "2026-10-09, after discovery, before any validation outcome was computed",
        "note": "Every discovery cell failed the round-5 kill rule. These three are evaluated on validation ONLY as a descriptive "
                "check that the discovery verdict holds (the two pre-registered primary arms, and the one cell with n>=40 that met "
                "the 3c continuation half of the kill rule). They are not gate candidates.",
        "cells": ["A|FOLLOW|pooled", "A|FADE|pooled", "A|FADE|cat=rain"],
        "spec": SPEC}, indent=1))
    print("frozen")


def validate() -> dict:
    fz = json.loads(FROZEN.read_text())
    if (OUT / "validation.json").exists():
        print("validation already run once:"); print((OUT / "validation.json").read_text()); return json.loads((OUT / "validation.json").read_text())
    X, res, disc, cut_ts, rows = rows_for()
    V = [r for r in rows if r["split"] == "val"]
    out = {}
    for cell in fz["cells"]:
        _, arm, sel = cell.split("|")
        a = [r for r in V if r["arm"] == arm and (sel == "pooled" or r["cat"] == sel.split("=")[1])]
        s = summ(a)
        if s is None:
            out[cell] = None; continue
        ts = sorted(r["ts"] for r in a); midt = ts[len(ts) // 2]
        h1 = [r["ret"] for r in a if r["ts"] < midt]; h2 = [r["ret"] for r in a if r["ts"] >= midt]
        days = max(1.0, (ts[-1] - ts[0]) / 86400)
        s.update({"half1": st.mean(h1) if h1 else None, "half2": st.mean(h2) if h2 else None, "trades_per_day": len(a) / days,
                  "by_cat": {c: round(st.mean(r["ret"] for r in a if r["cat"] == c), 4) for c in sorted({r["cat"] for r in a})},
                  "n_by_cat": {c: sum(r["cat"] == c for r in a) for c in sorted({r["cat"] for r in a})}})
        out[cell] = s
        print(fmt(cell, s), f"halves {s['half1']:+.1%}/{s['half2']:+.1%}", s["n_by_cat"])
    (OUT / "validation.json").write_text(json.dumps({"cut_ts": cut_ts, "cells": out}, indent=1, default=str))
    return out


if __name__ == "__main__":
    a = sys.argv[1:] or ["discovery"]
    {"discovery": discovery, "diag": diag, "freeze": freeze, "validate": validate}[a[0]](*a[1:])
