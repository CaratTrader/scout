"""r6_near_deadline_nothing_happens_census: does NO on 'will X happen by D' markets pay more than its price in the
last week before the deadline?

Hypothesis: retail YES holders do not decay the hazard fast enough in the final days, so NO bought at 0.70-0.95
three days or one day before the deadline wins more often than its price says (a time-decay premium on the
favourite side; r5's outcome-free control at D-3 went 25/25, r4's months-out census was calibrated or rich).

Universe (r6_near_deadline_nothing_happens_census_data.frame, listing fields only): settled binary hazard markets
(early-close condition 'closes early if the event occurs'), deadline D parsed from rules_primary, weather
accumulators (rain/snow, the r2 family) excluded. Topic families from series/title keywords (fixed list).
Clock: decision t = D - h hours (rounded down to the hour), h in {168, 120, 72, 48, 24}; the market must have opened
>= 1 h before t and be open at t (nothing after t is used to select). Signal: NO ask = 1 - yes_bid of the last
hourly candle at or before t (carried forward <= 48 h; candles are emitted only when the book or a trade changes).
Fill: taker NO at 1 - yes_bid prevailing at t + 1 h, limit = signal + 3c (no fill above it); a market that closes
inside that hour (the event happened) fills at the decision quote and counts; fee
0.07 * multiplier * p * (1 - p) per contract, rounded up to the cent per 10-lot.
Split: 70/30 by deadline time over (event, deadline) groups that have a decision quote (never by outcome).
Statistics: equal-$ return per trade; t clustered by event and by topic; 'without the 3 best' drops the 3 best
EVENTS; one-sided 95% exact-binomial lower bound over events (first trade per event) mapped to a per-$ return.
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_near_deadline_nothing_happens_census [discovery|validation|all]"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_archive_census_common import binom_tail_ge, cp_lower
from lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_data import (DEC_H, H, MAXAGE, OUTP, cached_candles,
                                                                                frame, last_quote)

SPLIT = 0.70
LIMIT = 0.03
BANDS = {"50-70": (0.50, 0.70), "70-80": (0.70, 0.80), "80-90": (0.80, 0.90), "90-97": (0.90, 0.97),
         "70-95": (0.70, 0.95), "80-95": (0.80, 0.95), "50-97": (0.50, 0.97), "70-97": (0.70, 0.97)}
ENTRY = {"h168": (168,), "h120": (120,), "h72": (72,), "h48": (48,), "h24": (24,), "first72_24": (72, 24),
         "first_any": (168, 120, 72, 48, 24)}
ONEOFF = ("one_off", "custom", "annual")


def fee_pc(p: float, mult: float = 1.0) -> float:
    return math.ceil(round(0.07 * mult * 10 * p * (1 - p) * 100, 9)) / 100 / 10


def decisions(rows: list[dict], C: dict) -> list[dict]:
    """Every (market, decision time) with a signal quote; fill fields set when a fill happens (outcome attached for
    scoring only)."""
    out = []
    for r in rows:
        if r.get("accum"):
            continue
        c = C.get(r["t"], [])
        if not c:
            continue
        for h in r["dec_ok"]:
            t = (r["D"] - h * H) // H * H
            q = last_quote(c, t)
            if not q or q[2] is None or q[2] <= 0 or q[2] >= 1:
                continue
            s = round(1 - q[2], 4)
            d = {"t": r["t"], "e": r["e"], "series": r["series"], "topic": r["topic"], "freq": r["freq"], "tier": r["tier"],
                 "h": h, "dec": t, "D": r["D"], "sig": s, "sig_age_h": round((t - q[0]) / H, 1),
                 "vol24": round(sum(x[3] for x in c if t - 24 * H < x[0] <= t), 1),
                 "spread": round(q[1] - q[2], 4) if q[1] is not None else None, "fill": False}
            tf = t + H
            p = None
            if r["close"] <= tf:
                p, tf = s, t          # closed inside the hour: an order sent at t + 1 min fills at the decision quote
            else:
                qf = last_quote(c, tf, MAXAGE + H)
                if qf and qf[2] is not None and 0 < qf[2] < 1:
                    p = round(1 - qf[2], 4)
            if p is not None and p <= s + LIMIT:
                won = r["result"] == "no"
                f = fee_pc(p, r.get("fee_mult") or 1)
                pnl = (1.0 if won else 0.0) - p - f
                d.update({"fill": True, "fill_ts": tf, "px": p, "fee": f, "won": won, "pnl": pnl, "ret": pnl / p})
            out.append(d)
    return out


def split(D: list[dict]) -> int:
    g = sorted({(d["e"], d["D"]) for d in D}, key=lambda x: x[1])
    return g[int(len(g) * SPLIT)][1] if g else 0


def stratum(d: dict, s: str) -> bool:
    if s == "all":
        return True
    if s == "oneoff":
        return d["freq"] in ONEOFF
    if s == "recurring":
        return d["freq"] not in ONEOFF
    if s == "news":
        return d["topic"] != "price_touch"
    if s == "news_oneoff":
        return d["topic"] != "price_touch" and d["freq"] in ONEOFF
    return d["topic"] == s


def cell(D: list[dict], entry: str, band: str, strat: str) -> list[dict]:
    """One trade per market: the earliest decision of the entry set whose SIGNAL price is in band and that filled."""
    lo, hi = BANDS[band]
    hs = ENTRY[entry]
    by = defaultdict(list)
    for d in D:
        if d["h"] in hs and lo <= d["sig"] <= hi and stratum(d, strat):
            by[d["t"]].append(d)
    out = []
    for t, xs in by.items():
        x = min(xs, key=lambda d: d["dec"])
        if x["fill"]:
            out.append(x)
    return out


def clustered_t(rows: list[dict], key: str) -> tuple[float | None, int]:
    g = defaultdict(list)
    for r in rows:
        g[r[key]].append(r["ret"])
    m = [st.mean(v) for v in g.values()]
    if len(m) < 3 or st.pstdev(m) == 0:
        return None, len(m)
    return st.mean(m) / (st.pstdev(m) / math.sqrt(len(m))), len(m)


def bounds(rows: list[dict]) -> dict:
    first = {}
    for r in sorted(rows, key=lambda r: r["fill_ts"]):
        first.setdefault(r["e"], r)
    ev = list(first.values()); n = len(ev)
    if not n:
        return {}
    k = sum(r["won"] for r in ev)
    inv = st.mean(1 / r["px"] for r in ev)
    cost = st.mean((r["px"] + r["fee"]) / r["px"] for r in ev)
    lo = cp_lower(k, n)
    px = st.mean(r["px"] for r in ev); fe = st.mean(r["fee"] for r in ev)
    need10 = 1.10 * px + fe
    return {"events": n, "wins": k, "win": round(k / n, 4), "avg_px": round(px, 4), "cp_win_lo95": round(lo, 4),
            "beta_lb_ret": round(lo * inv - cost, 4), "win_needed_+10": round(need10, 4),
            "p_obs_if_true_+10": round(binom_tail_ge(k, n, min(need10, 1.0)), 4) if need10 < 1 else None,
            "p_obs_if_true_0": round(binom_tail_ge(k, n, px + fe), 4)}


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rs = [r["ret"] for r in rows]
    te, ne = clustered_t(rows, "e"); tt, nt = clustered_t(rows, "topic")
    em = defaultdict(list)
    for r in rows:
        em[r["e"]].append(r["ret"])
    best = sorted(em, key=lambda e: -sum(em[e]))[:3]
    wo3 = [r["ret"] for r in rows if r["e"] not in best]
    srt = sorted(rows, key=lambda r: r["fill_ts"]); hh = len(srt) // 2
    span_d = max(1.0, (srt[-1]["fill_ts"] - srt[0]["fill_ts"]) / 86400)
    tm = Counter(r["topic"] for r in rows)
    by_topic = {k: round(st.mean(r["ret"] for r in rows if r["topic"] == k), 4) for k in tm}
    return {"n": len(rows), "markets": len({r["t"] for r in rows}), "events": ne, "topics": nt, "series": len({r["series"] for r in rows}),
            "win": round(sum(r["won"] for r in rows) / len(rows), 4), "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(st.mean(rs), 4), "t": round(te, 2) if te is not None else None,
            "t_topic": round(tt, 2) if tt is not None else None,
            "ret_wo3": round(st.mean(wo3), 4) if wo3 else None,
            "half1": round(st.mean(r["ret"] for r in srt[:hh]), 4) if hh else None,
            "half2": round(st.mean(r["ret"] for r in srt[hh:]), 4),
            "trades_per_day": round(len(rows) / span_d, 3), "median_vol24": st.median(r["vol24"] for r in rows),
            "topic_n": dict(tm), "topic_ret": by_topic, "bounds": bounds(rows)}


def load_all() -> tuple[list[dict], int]:
    rows = frame(write=True)
    C = cached_candles({r["t"] for r in rows if not r.get("accum")})
    D = decisions(rows, C)
    return D, split(D)


STRATA = ("all", "oneoff", "recurring", "news", "news_oneoff", "price_touch", "launch_release", "shutdown_congress",
          "cabinet_personnel", "trump_actions", "geopolitics", "fed_macro", "culture_other", "weather_nature")


def discovery() -> dict:
    D, cut = load_all()
    disc = [d for d in D if d["D"] < cut]
    cells = {}
    for entry in ENTRY:
        for band in BANDS:
            for s in STRATA:
                rows = cell(disc, entry, band, s)
                if rows:
                    cells[f"{entry}|{band}|{s}"] = stats(rows)
    # descriptive calibration on discovery: NO win rate vs signal price by decision horizon (all filled decisions)
    calib = {}
    for h in DEC_H:
        for lo, hi in ((0.0, 0.5), (0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.97), (0.97, 1.0)):
            x = [d for d in disc if d["h"] == h and d["fill"] and lo <= d["sig"] < hi and d["topic"] != "price_touch"]
            if x:
                calib[f"h{h}|{lo:.2f}-{hi:.2f}"] = {"n": len(x), "events": len({d['e'] for d in x}),
                                                   "no_win": round(sum(d["won"] for d in x) / len(x), 3),
                                                   "avg_px": round(st.mean(d["px"] for d in x), 3),
                                                   "ret": round(st.mean(d["ret"] for d in x), 4)}
    meta = {"cut_D": cut, "cut_date": dt.datetime.utcfromtimestamp(cut).isoformat() if cut else None,
            "decisions": len(D), "disc_decisions": len(disc), "fills": sum(d["fill"] for d in D),
            "groups": len({(d['e'], d['D']) for d in D}), "disc_groups": len({(d['e'], d['D']) for d in disc}),
            "events": len({d['e'] for d in D}), "disc_events": len({d['e'] for d in disc}),
            "val_events": len({d['e'] for d in D if d['D'] >= cut}), "cells_examined": len(cells),
            "tier": dict(Counter(d["tier"] for d in D)), "topic_decisions": dict(Counter(d["topic"] for d in D))}
    out = {"meta": meta, "cells": cells, "calibration": calib}
    json.dump(out, open(OUTP / "discovery.json", "w"), indent=1)
    with open(OUTP / "decisions.jsonl", "w") as g:
        for d in D:
            g.write(json.dumps(d) + "\n")
    return out


def validation() -> dict:
    fz = json.load(open(OUTP / "frozen.json"))
    D, cut = load_all()
    assert cut == fz["cut_D"], "split moved since freeze"
    val = [d for d in D if d["D"] >= cut]
    res = {}
    for c in fz["candidates"]:
        rows = cell(val, c["entry"], c["band"], c["stratum"])
        res[c["name"]] = {"rule": c, **stats(rows)}
    out = {"cut_D": cut, "val_decisions": len(val), "val_events": len({d['e'] for d in val}), "candidates": res}
    json.dump(out, open(OUTP / "validation.json", "w"), indent=1)
    return out


# Candidate rule, written before discovery was run: C1 and C2 are the lead plan's cells; C3 is the discovery cell with
# the highest 'without the 3 best events' mean among entry x band x stratum in {all, news, oneoff, news_oneoff} with
# >= 15 discovery events and a discovery Beta bound > 0 (if none qualifies, the highest mean with >= 15 events).
PRESET = [{"name": "C1_h72_NO80-95_news", "entry": "h72", "band": "80-95", "stratum": "news"},
          {"name": "C2_h24_NO70-95_news", "entry": "h24", "band": "70-95", "stratum": "news"}]
KILL = "discovery mean < +5%, discovery Beta bound <= 0, or the effect sits in a single topic family"


def freeze() -> dict:
    disc = json.load(open(OUTP / "discovery.json"))
    cells = disc["cells"]
    pool = {k: v for k, v in cells.items() if k.split("|")[2] in ("all", "news", "oneoff", "news_oneoff") and v["events"] >= 15}
    ok = {k: v for k, v in pool.items() if (v.get("bounds") or {}).get("beta_lb_ret", -1) > 0}
    src = ok or pool
    best = max(src, key=lambda k: (src[k]["ret_wo3"] if src[k]["ret_wo3"] is not None else -9)) if src else None
    cands = list(PRESET)
    if best:
        e, b, s_ = best.split("|")
        if not any(c["entry"] == e and c["band"] == b and c["stratum"] == s_ for c in cands):
            cands.append({"name": f"C3_{e}_NO{b}_{s_}", "entry": e, "band": b, "stratum": s_})
    for c in cands:
        k = f"{c['entry']}|{c['band']}|{c['stratum']}"
        c["discovery"] = {x: cells.get(k, {}).get(x) for x in ("n", "events", "win", "avg_px", "ret_per_dollar", "t", "ret_wo3", "bounds", "topic_ret")}
    out = {"written": dt.datetime.utcnow().isoformat() + "Z", "cut_D": disc["meta"]["cut_D"], "candidates": cands,
           "c3_rule": "max ret_wo3 among entry x band x {all,news,oneoff,news_oneoff} with >= 15 discovery events and Beta bound > 0",
           "kill_rule": KILL, "cells_examined": len(cells)}
    json.dump(out, open(OUTP / "frozen.json", "w"), indent=1)
    return out


def show(cells: dict, keys: list[str]) -> None:
    for k in keys:
        v = cells[k]; b = v.get("bounds", {})
        print(f"{k:34s} n={v['n']:3d} ev={v['events']:3d} top={v['topics']} win={v['win']:.3f} px={v['avg_px']:.3f} "
              f"ret={v['ret_per_dollar']:+.3f} t={v['t']} tT={v['t_topic']} wo3={v['ret_wo3']} h={v['half1']}/{v['half2']} "
              f"LB={b.get('beta_lb_ret')} ({b.get('wins')}/{b.get('events')})")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "discovery"
    if what == "discovery":
        out = discovery()
        print(json.dumps(out["meta"], indent=1))
        cells = out["cells"]
        main = [k for k in cells if k.split("|")[2] in ("all", "news", "oneoff", "news_oneoff", "recurring", "price_touch")]
        show(cells, sorted(main, key=lambda k: (k.split("|")[2], k.split("|")[0], k.split("|")[1])))
        print("\nCALIBRATION (discovery, news topics, filled decisions)")
        for k, v in out["calibration"].items():
            print(f"  {k:18s} n={v['n']:3d} ev={v['events']:3d} no_win={v['no_win']:.3f} px={v['avg_px']:.3f} ret={v['ret']:+.3f}")
    elif what == "freeze":
        print(json.dumps(freeze(), indent=1))
    elif what == "validation":
        out = validation()
        for k, v in out["candidates"].items():
            print(k, json.dumps({x: y for x, y in v.items() if x != "rule"}))
