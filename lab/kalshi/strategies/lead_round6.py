"""Round-6 lead checks (descriptive; no strategy is chosen here).

1. K update and the power table at the new bar.
2. Launch/release first-entry NO 0.70-0.95 (the post-hoc r6 near-deadline sub-pattern) re-scored three ways:
   builder decisions with the verifier-2 corrections (fill at the decision quote, no quote older than 6 h),
   verifier-1 base and expanded universes, and the expanded-only series that no researcher had picked
   (the closest thing on disk to a fresh sample of the pattern).
3. Truth Social weekly C1: event-level concentration of the validation return.
4. KXTRUTHSOCIALD (daily count, launched 2026-10-01): spreads, volume and event cadence, as grounding for round 7.

Run: .venv/bin/python -m lab.kalshi.strategies.lead_round6 [--live]   (--live spends <= 4 Kalshi calls)
Outputs: data/kalshi_lab/strategies/lead_round6/lead_checks.json
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics as st
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/lead_round6")
S = os.path.join(ROOT, "data/kalshi_lab/strategies")

K_BEFORE = 18565
ROUND6 = {  # builder self-reported variants
    "r6_near_deadline_nothing_happens_census": 593,
    "r6_earnings_transcript_prior_taker": 92,
    "r6_new_series_honeymoon": 185,
    "r6_weather_d1_mos_brackets": 302,
    "r6_rule_boundary_climate_day": 18,
    "r6_truth_social_count_endgame": 240,
    "r6_thin_book_sweep_follow_fade": 87,
}
VERIFIER = {  # cells the verifiers computed (stated or counted from their issue lists)
    "near_deadline_v1": 12, "near_deadline_v2": 10,
    "honeymoon_v1": 21, "honeymoon_v2": 12,
    "truth_social_v1": 15, "truth_social_v2": 13,
}
LEAD = 10  # cells in this file
SPLIT_D = int(dt.datetime(2026, 6, 11, tzinfo=dt.timezone.utc).timestamp())


def zbar(K: int) -> float:
    p = 0.05 / K
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2)) > p:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def k_update() -> dict:
    b, v = sum(ROUND6.values()), sum(VERIFIER.values())
    tot = b + v + LEAD
    return {"builders": ROUND6, "builders_total": b, "verifiers": VERIFIER, "verifiers_total": v, "lead": LEAD,
            "round6_total": tot, "K_before": K_BEFORE, "K_after": K_BEFORE + tot, "bar_t": zbar(K_BEFORE + tot)}


def power(bar: float) -> list:
    rows = []
    for p in (0.92, 0.85, 0.70, 0.57, 0.50, 0.30, 0.18):
        q = p * 1.10 + 0.07 * p * (1 - p)
        if q >= 0.999:
            rows.append({"price": p, "win_needed": None, "events_needed": "impossible"}); continue
        sd = math.sqrt(q * (1 - q)) / p
        rows.append({"price": p, "win_needed": round(q, 3), "sd_per_dollar": round(sd, 2),
                     "events_needed": math.ceil((bar * sd / 0.10) ** 2)})
    return rows


def fee10(p: float) -> float:
    """Taker fee per contract on a 10-lot, rounded up to the cent per order."""
    return math.ceil(round(10 * 0.07 * p * (1 - p) * 100, 6)) / 1000


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact-binomial (Clopper-Pearson) lower bound on the win rate."""
    if k == 0:
        return 0.0
    def tail(p):  # P(X >= k | n, p)
        return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r)
    em = [st.mean(x["ret"] for x in v) for v in ev.values()]
    ne = len(em)
    sd = st.stdev(em) if ne > 2 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne)) if sd > 0 else float("nan")
    ev_best = sorted(ev, key=lambda e: -st.mean(x["ret"] for x in ev[e]))
    keep = [r for r in rows if r["e"] not in set(ev_best[:3])]
    k_ev = sum(1 for v in ev.values() if all(x["won"] for x in v))
    px = st.mean(r["px"] for r in rows)
    fe = st.mean(fee10(r["px"]) for r in rows)
    lb = cp_lower(k_ev, ne)
    return {"n": len(rows), "events": ne, "series": len({r["s"] for r in rows}), "losses": sum(not r["won"] for r in rows),
            "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(px, 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t_event_sample_sd": round(t, 2),
            "ret_wo3_events": round(st.mean(r["ret"] for r in keep), 4) if keep else None,
            "beta_lb_ret_events": round(lb / px - 1 - fe / px, 4)}


def first_entry(path: str, fresh_fill: bool, max_age: float | None) -> list[dict]:
    """First decision per market (largest horizon first) with the NO signal price in [0.70, 0.95]."""
    by = defaultdict(list)
    for line in open(path):
        d = json.loads(line)
        if not d.get("fill") or d.get("sig") is None:
            continue
        age = d.get("sig_age_h", d.get("age_h", 0)) or 0
        if max_age is not None and age > max_age:
            continue
        by[d["t"]].append(d)
    out = []
    for t, ds in by.items():
        ds.sort(key=lambda d: -d["h"])
        for d in ds:
            if 0.70 <= d["sig"] <= 0.95:
                px = d["sig"] if fresh_fill else d["px"]
                f = fee10(px)
                ret = (1 - px - f) / px if d["won"] else -(px + f) / px
                out.append({"t": t, "e": d["e"], "s": d.get("series", d.get("s")), "topic": d["topic"], "D": d["D"],
                            "h": d["h"], "px": px, "won": bool(d["won"]), "ret": ret})
                break
    return out


def launch_release() -> dict:
    b = os.path.join(S, "r6_near_deadline_nothing_happens_census/decisions.jsonl")
    v1b = os.path.join(S, "r6_near_deadline_nothing_happens_census/repro/decisions_base.jsonl")
    v1x = os.path.join(S, "r6_near_deadline_nothing_happens_census/repro/decisions_expanded.jsonl")
    res = {}
    for name, path, fresh, age in [("builder_as_reported", b, False, None),
                                   ("builder_v2_corrected_fill_at_signal_age_le_6h", b, True, 6.0),
                                   ("verifier1_base", v1b, False, None),
                                   ("verifier1_expanded", v1x, False, None)]:
        rows = first_entry(path, fresh, age)
        lr = [r for r in rows if r["topic"] == "launch_release"]
        ot = [r for r in rows if r["topic"] != "launch_release"]
        res[name] = {"launch_release": stats(lr), "other_topics": stats(ot),
                     "launch_release_after_split": stats([r for r in lr if r["D"] >= SPLIT_D]),
                     "launch_release_before_split": stats([r for r in lr if r["D"] < SPLIT_D])}
    # expanded-only series: series in verifier-1 expanded that are absent from both base universes
    base_series = {json.loads(l).get("series") for l in open(b)} | {json.loads(l).get("s") for l in open(v1b)}
    xo = [r for r in first_entry(v1x, False, None) if r["s"] not in base_series]
    res["expanded_only_series"] = {"launch_release": stats([r for r in xo if r["topic"] == "launch_release"]),
                                   "other_topics": stats([r for r in xo if r["topic"] != "launch_release"]),
                                   "series_list": sorted({r["s"] for r in xo}),
                                   "topics": sorted({r["topic"] for r in xo})}
    lr_all = [r for r in first_entry(v1x, False, None) if r["topic"] == "launch_release"]
    months = defaultdict(set)
    for r in lr_all:
        months[dt.datetime.utcfromtimestamp(r["D"]).strftime("%Y-%m")].add(r["e"])
    res["launch_release_events_per_deadline_month"] = {m: len(v) for m, v in sorted(months.items())}
    res["launch_release_losses"] = [r for r in lr_all if not r["won"]]
    return res


def truth_weekly() -> dict:
    v = json.load(open(os.path.join(S, "r6_truth_social_count_endgame/val_result.json")))
    tr = v["candidates"]["C1_model_NO_theta10_sat"]["trades"]
    for r in tr:
        r["s"] = "KXTRUTHSOCIAL"
    ev = defaultdict(list)
    for r in tr:
        ev[r["e"]].append(r["ret"])
    loo = {e: round(st.mean(r["ret"] for r in tr if r["e"] != e), 3) for e in ev}
    first = [r for r in tr if r["h_before_close"] >= 23.9]
    later = [r for r in tr if r["h_before_close"] < 23.9]
    exp = st.mean(r["model"] / r["px"] - 1 - fee10(r["px"]) / r["px"] for r in tr)
    return {"C1_validation": stats(tr), "event_sums": {e: round(sum(x), 3) for e, x in ev.items()},
            "leave_one_event_out_mean": loo, "first_grid_point_sat_0959": stats(first), "later_decisions": stats(later),
            "model_expected_ret_on_same_trades": round(exp, 4),
            "share_of_summed_return_from_top2_events": round(sum(sorted((sum(x) for x in ev.values()), reverse=True)[:2])
                                                              / sum(sum(x) for x in ev.values()), 3)}


def truth_daily(live: bool) -> dict:
    mk = defaultdict(list)
    for line in open(os.path.join(S, "r2_launch_window/markets.jsonl")):
        m = json.loads(line)
        if m.get("series") == "KXTRUTHSOCIALD":
            mk[m["e"]].append(m)
    cand = {}
    for line in open(os.path.join(S, "r2_launch_window/candles_h.jsonl")):
        c = json.loads(line)
        if c["t"].startswith("KXTRUTHSOCIALD"):
            cand[c["t"]] = c["c"]
    out = {"events_on_disk": len(mk), "event_volume": {e: round(sum(m.get("vol") or 0 for m in ms)) for e, ms in sorted(mk.items())},
           "brackets_per_event": sorted({len(ms) for ms in mk.values()})}
    spreads = defaultdict(list)
    for e, ms in mk.items():
        close = ms[0]["close"]
        for hb, lab in [(12, "close-12h"), (6, "close-6h"), (3, "close-3h")]:
            t = close - hb * 3600
            for m in ms:
                c = [x for x in cand.get(m["t"], []) if x[0] <= t]
                if not c or t - c[-1][0] > 6 * 3600:
                    continue
                ask, bid = c[-1][1], c[-1][2]
                mid = (ask + bid) / 2
                if 0.10 <= mid <= 0.90:
                    spreads[lab].append(round(ask - bid, 3))
    out["in_play_spread"] = {k: {"n": len(v), "median": st.median(v) if v else None, "max": max(v) if v else None}
                             for k, v in spreads.items()}
    if live:
        from lab.kalshi.strategies.lead_round6_api import get, calls_used
        st_ = get("/markets?series_ticker=KXTRUTHSOCIALD&status=settled&limit=1000")
        op = get("/markets?series_ticker=KXTRUTHSOCIALD&status=open&limit=200")
        hist = get("/historical/markets?series_ticker=KXTRUTHSOCIALD&limit=1000")
        sm = st_.get("markets", [])
        evs = defaultdict(float)
        for m in sm:
            evs[m["event_ticker"]] += float(m.get("volume_fp") or m.get("volume") or 0)
        om = op.get("markets", [])
        out["live"] = {"settled_markets": len(sm), "settled_events": len(evs),
                       "settled_event_volume": dict(sorted(evs.items())),
                       "historical_markets": len(hist.get("markets", [])),
                       "open_markets": len(om), "open_events": sorted({m["event_ticker"] for m in om})}
        inplay = []
        for m in om:
            try:
                a, b_ = float(m.get("yes_ask_dollars") or 0), float(m.get("yes_bid_dollars") or 0)
            except (TypeError, ValueError):
                continue
            if 0.15 <= (a + b_) / 2 <= 0.85:
                inplay.append((abs((a + b_) / 2 - 0.5), m["ticker"], a, b_))
        if inplay:
            inplay.sort()
            tk = inplay[0][1]
            ob = get(f"/markets/{tk}/orderbook")
            out["live"]["orderbook_sample"] = {"ticker": tk, "yes_ask": inplay[0][2], "yes_bid": inplay[0][3], "book": ob}
        out["live"]["calls_used_total"] = calls_used()
    return out


def main() -> None:
    live = "--live" in sys.argv
    os.makedirs(OUT, exist_ok=True)
    k = k_update()
    res = {"K": k, "power_at_plus10": power(k["bar_t"]), "launch_release": launch_release(),
           "truth_social_weekly_C1": truth_weekly(), "truth_social_daily": truth_daily(live)}
    json.dump(res, open(os.path.join(OUT, "lead_checks.json"), "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str)[:20000])


if __name__ == "__main__":
    main()
