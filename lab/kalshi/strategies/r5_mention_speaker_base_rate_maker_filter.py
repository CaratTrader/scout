"""r5_mention_speaker_base_rate_maker_filter: does a low prior hit rate (same speaker/format, same word, Kalshi's own
settled history strictly before the event's listing) remove the adverse selection of the seeded-book maker NO?

Pre-registration (written before any bucket outcome was computed):
data/kalshi_lab/strategies/r5_mention_speaker_base_rate_maker_filter/preregistration_backtest.json

Samples (order files of rounds 2-4, normalised in r5_mention_speaker_base_rate_maker_filter_data): S1 r2 C1, S2 r3 C1E,
S3 r4 arm A, S4 r4 V1. Buckets: NH (no history), LOW20 / LOW40 (k/n <= 0.20 / 0.40, n >= 1), HIGH40, ALL.
Split: per sample, events by listing time, CONFIRM = first 70%, EVAL = last 30% (direction on CONFIRM only).
Primary diagnostic: filled-vs-unfilled NO-win gap, sample-stratified, and the Mantel-Haenszel log odds ratio of being
filled for YES vs NO outcomes (guards against a ceiling-driven shrink). Kill unless R <= 0.5 and R_LOR <= 0.5 on EVAL.
No Kalshi calls here (the data module's fetch_missing made 17 /historical listing calls once).

Usage: .venv/bin/python -m lab.kalshi.strategies.r5_mention_speaker_base_rate_maker_filter [run]
Output: data/kalshi_lab/strategies/r5_mention_speaker_base_rate_maker_filter/backtest.json (+ result.json via result())
"""
from __future__ import annotations

import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r5_mention_speaker_base_rate_maker_filter_data as RD

OUT = RD.OUT
SPLIT = 0.7
SEED = 20261008
BOOT = 2000
MIN_CELL = 15
SAMPLES = ("S1_r2_C1", "S2_r3_C1E", "S3_r4_armA", "S4_r4_V1")


def bucket_pred(name: str, nmin: int = 1):
    if name == "ALL":
        return lambda o: True
    if name == "NH":
        return lambda o: o["n"] == 0
    if name == "LOW20":
        return lambda o: o["n"] >= nmin and o["k"] / o["n"] <= 0.20 + 1e-12
    if name == "LOW40":
        return lambda o: o["n"] >= nmin and o["k"] / o["n"] <= 0.40 + 1e-12
    if name == "HIGH40":
        return lambda o: o["n"] >= nmin and o["k"] / o["n"] > 0.40 + 1e-12
    raise ValueError(name)


# ------------------------------------------------------------------------------------------------ data
def build() -> list[dict]:
    pool = RD.load_pool()
    O = [o for o in RD.all_orders(pool) if o["rule_consistent"]]
    RD.annotate(O, RD.History(pool))
    for smp in SAMPLES:
        X = [o for o in O if o["sample"] == smp]
        evs = sorted({(o["listing"], o["e"]) for o in X})
        cut = int(len(evs) * SPLIT)
        part = {e: ("CONFIRM" if i < cut else "EVAL") for i, (_, e) in enumerate(evs)}
        for o in X:
            o["part"] = part[o["e"]]
    evs = sorted({(o["listing"], o["e"]) for o in O})
    cut = int(len(evs) * SPLIT)
    gpart = {e: ("CONFIRM" if i < cut else "EVAL") for i, (_, e) in enumerate(evs)}
    for o in O:
        o["gpart"] = gpart[o["e"]]
    return O


# ------------------------------------------------------------------------------------------------ statistics
def gap_of(X: list[dict]) -> tuple[float | None, int, int]:
    f = [o["no_won"] for o in X if o["filled"]]; u = [o["no_won"] for o in X if not o["filled"]]
    if not f or not u:
        return None, len(f), len(u)
    return st.mean(u) - st.mean(f), len(f), len(u)


def cells(X: list[dict]) -> tuple[float, float, float, float]:
    a = sum(1 for o in X if o["filled"] and not o["no_won"])      # filled, YES outcome
    b = sum(1 for o in X if not o["filled"] and not o["no_won"])  # unfilled, YES
    c = sum(1 for o in X if o["filled"] and o["no_won"])          # filled, NO outcome
    d = sum(1 for o in X if not o["filled"] and o["no_won"])      # unfilled, NO
    return a, b, c, d


def mh_lor(strata: list[list[dict]]) -> float | None:
    num = den = 0.0
    for X in strata:
        if not X:
            continue
        a, b, c, d = cells(X)
        if min(a, b, c, d) == 0:
            a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        n = a + b + c + d
        num += a * d / n; den += b * c / n
    if num <= 0 or den <= 0:
        return None
    return math.log(num / den)


def stratified(bysmp: dict, pred) -> dict:
    """bysmp: sample -> orders of one slice. Gap and LOR of the bucket vs ALL on the bucket's sample weights."""
    gb = ga = wsum = 0.0
    used = []
    for smp, X in bysmp.items():
        B = [o for o in X if pred(o)]
        g_b, nf, nu = gap_of(B); g_a, _, _ = gap_of(X)
        if g_b is None or g_a is None:
            continue
        w = len(B); gb += w * g_b; ga += w * g_a; wsum += w; used.append(smp)
    lb = mh_lor([[o for o in X if pred(o)] for X in bysmp.values()])
    la = mh_lor([X for smp, X in bysmp.items() if any(pred(o) for o in X)])
    out = {"gap_bucket": round(gb / wsum, 4) if wsum else None, "gap_all_same_weights": round(ga / wsum, 4) if wsum else None,
           "samples_used": used, "lor_bucket": round(lb, 4) if lb is not None else None, "lor_all": round(la, 4) if la is not None else None}
    out["R"] = round(out["gap_bucket"] / out["gap_all_same_weights"], 4) if wsum and ga / wsum > 0 else None
    out["R_LOR"] = round(lb / la, 4) if (lb is not None and la is not None and la > 0) else None
    allB = [o for X in bysmp.values() for o in X if pred(o)]
    g, nf, nu = gap_of(allB)
    out.update({"orders": len(allB), "filled": nf, "unfilled": nu, "pooled_gap_raw": round(g, 4) if g is not None else None,
                "filled_NO_win": round(st.mean(o["no_won"] for o in allB if o["filled"]), 4) if nf else None,
                "unfilled_NO_win": round(st.mean(o["no_won"] for o in allB if not o["filled"]), 4) if nu else None,
                "fill_rate": round(nf / len(allB), 4) if allB else None,
                "events": len({o["e"] for o in allB})})
    return out


def boot(bysmp: dict, pred, reps: int = BOOT, seed: int = SEED) -> dict:
    rng = random.Random(seed)
    ev = {smp: defaultdict(list) for smp in bysmp}
    for smp, X in bysmp.items():
        for o in X:
            ev[smp][o["e"]].append(o)
    keys = {smp: list(v) for smp, v in ev.items()}
    Rs, Ls = [], []
    for _ in range(reps):
        res = {}
        for smp in bysmp:
            ks = keys[smp]
            res[smp] = [o for _ in ks for o in ev[smp][rng.choice(ks)]] if ks else []
        s = stratified(res, pred)
        if s["R"] is not None:
            Rs.append(s["R"])
        if s["R_LOR"] is not None:
            Ls.append(s["R_LOR"])

    def q(v, p):
        if not v:
            return None
        v = sorted(v); return round(v[min(len(v) - 1, int(p * len(v)))], 3)
    return {"R_90": [q(Rs, 0.05), q(Rs, 0.95)], "R_LOR_90": [q(Ls, 0.05), q(Ls, 0.95)], "reps_R": len(Rs), "reps_LOR": len(Ls),
            "P(R<=0.5)": round(sum(r <= 0.5 for r in Rs) / len(Rs), 3) if Rs else None,
            "P(R_LOR<=0.5)": round(sum(r <= 0.5 for r in Ls) / len(Ls), 3) if Ls else None}


def binom_lo(k: int, n: int, a: float = 0.05) -> float:
    if n == 0 or k == 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        tail = sum(math.comb(n, i) * mid ** i * (1 - mid) ** (n - i) for i in range(k, n + 1))
        lo, hi = (mid, hi) if tail < a else (lo, mid)
    return lo


def returns(X: list[dict]) -> dict:
    F = [o for o in X if o["filled"]]
    if not F:
        return {"n": 0}
    ev = defaultdict(list)
    for o in F:
        ev[o["e"]].append(o["ret"])
    em = [st.mean(v) for v in ev.values()]; ne = len(em)
    sd = st.pstdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne - 1)) if ne > 2 and sd > 0 else None
    rs = sorted((o["ret"] for o in F), reverse=True)
    cost = sum(o["q"] * o["px"] for o in F); pnl = sum(o["q"] * ((1.0 if o["no_won"] else 0.0) - o["px"]) for o in F)
    srt = sorted(F, key=lambda o: o["listing"]); mid = srt[len(srt) // 2]["listing"]
    h1 = [o["ret"] for o in srt if o["listing"] < mid]; h2 = [o["ret"] for o in srt if o["listing"] >= mid]
    first = {}
    for o in sorted(F, key=lambda o: (o["post"], o["t"])):
        first.setdefault(o["e"], o)
    fe = list(first.values()); k = sum(o["no_won"] for o in fe); pxe = st.mean(o["px"] for o in fe)
    wlo = binom_lo(k, len(fe))
    qs = sorted(o["q"] for o in F)
    return {"n": len(F), "events": ne, "win": round(st.mean(o["no_won"] for o in F), 4), "avg_px": round(st.mean(o["px"] for o in F), 4),
            "ret_per_dollar": round(st.mean(o["ret"] for o in F), 4), "t": round(t, 2) if t is not None else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "size_weighted_ret": round(pnl / cost, 4) if cost > 0 else None, "median_filled_contracts": qs[len(qs) // 2],
            "beta_lo_ret": round((wlo - pxe) / pxe, 4), "event_first_px": round(pxe, 4)}


def evaluate(O: list[dict], partkey: str, part: str, nmin: int = 1, do_boot: bool = True) -> dict:
    bysmp = {smp: [o for o in O if o["sample"] == smp and o[partkey] == part] for smp in SAMPLES}
    res = {}
    for b in ("ALL", "NH", "LOW20", "LOW40", "HIGH40"):
        pred = bucket_pred(b, nmin)
        r = stratified(bysmp, pred) if b != "ALL" else {**stratified(bysmp, pred)}
        r["returns_filled"] = returns([o for X in bysmp.values() for o in X if pred(o)])
        r["by_sample"] = {}
        for smp, X in bysmp.items():
            B = [o for o in X if pred(o)]
            g, nf, nu = gap_of(B)
            r["by_sample"][smp] = {"orders": len(B), "filled": nf, "unfilled": nu, "gap": round(g, 4) if g is not None else None,
                                   "filled_NO_win": round(st.mean(o["no_won"] for o in B if o["filled"]), 3) if nf else None,
                                   "unfilled_NO_win": round(st.mean(o["no_won"] for o in B if not o["filled"]), 3) if nu else None,
                                   "lor": (lambda v: round(v, 3) if v is not None else None)(mh_lor([B])) if B else None,
                                   "ret_filled": returns(B).get("ret_per_dollar"), "sw_ret_filled": returns(B).get("size_weighted_ret")}
        if do_boot and b != "ALL":
            r["bootstrap"] = boot(bysmp, pred)
        res[b] = r
    return res


def decide(conf: dict, ev: dict) -> dict:
    out = {}
    for c in ("LOW20", "LOW40", "NH"):
        rc = conf[c]["R"]; re_ = ev[c]["R"]; rl = ev[c]["R_LOR"]
        direction = rc is not None and rc < 1
        thin = ev[c]["filled"] < MIN_CELL or ev[c]["unfilled"] < MIN_CELL
        if not direction:
            v = "killed on CONFIRM (direction not confirmed)"
        elif thin:
            v = "thin on EVAL (not surviving)"
        elif re_ is not None and re_ <= 0.5 and rl is not None and rl <= 0.5:
            v = "SURVIVES"
        elif re_ is not None and re_ <= 0.5:
            v = "killed: win gap halves but the log odds ratio does not (ceiling shrink)"
        else:
            v = "killed: gap does not shrink by half"
        out[c] = {"R_confirm": rc, "R_eval": re_, "R_LOR_eval": rl, "verdict": v}
    out["family"] = "SURVIVES" if any(x["verdict"] == "SURVIVES" for x in out.values() if isinstance(x, dict)) else "KILL"
    return out


def run() -> dict:
    O = build()
    res = {"orders": len(O), "by_sample": {}, "variants_examined": 14}
    for smp in SAMPLES:
        X = [o for o in O if o["sample"] == smp]
        res["by_sample"][smp] = {"orders": len(X), "events": len({o["e"] for o in X}), "confirm_orders": sum(o["part"] == "CONFIRM" for o in X),
                                 "eval_orders": sum(o["part"] == "EVAL" for o in X), "filled": sum(o["filled"] for o in X)}
    res["CONFIRM"] = evaluate(O, "part", "CONFIRM")
    res["EVAL"] = evaluate(O, "part", "EVAL")
    res["decision"] = decide(res["CONFIRM"], res["EVAL"])
    res["secondary_nmin3"] = {"CONFIRM": evaluate(O, "part", "CONFIRM", 3, False), "EVAL": evaluate(O, "part", "EVAL", 3, False)}
    res["secondary_global_split"] = {"CONFIRM": evaluate(O, "gpart", "CONFIRM", 1, False), "EVAL": evaluate(O, "gpart", "EVAL", 1, False)}
    for o in O:
        o["_all"] = "ALL"
    res["all_periods"] = evaluate(O, "_all", "ALL", 1, False)
    res["extras_descriptive"] = extras(O)
    res["variants_examined"] = 14 + 15
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "backtest.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "orders_annotated.jsonl").write_text("".join(json.dumps({k: v for k, v in o.items() if not k.startswith("_")}) + "\n" for o in O))
    return res


def extras(O: list[dict]) -> dict:
    """Descriptive diagnostics run AFTER the pre-registered decision (not decisive; counted in variants_examined).
    1. Samples whose rule cancels before the appearance (S2, S3, S4): is the pre-event adverse selection base-rate driven?
    2. S1 fill timing: S1 rests until the event's first close + 1 h. Orders 'filled_pre' = first through-print candle ends
       >= 1 h before the first close (uses the future first-close time: descriptive only). Where does S1's gap come from?
    3. S1 full fills only (>= 2c through with >= 20 lots, round-2 class): returns by bucket on EVAL."""
    out = {}
    for part in ("CONFIRM", "EVAL", "both"):
        by = {smp: [o for o in O if o["sample"] == smp and (part == "both" or o["part"] == part)] for smp in SAMPLES[1:]}
        out[f"pre_cancel_S2_S4|{part}"] = {b: {**{k: v for k, v in stratified(by, bucket_pred(b)).items() if k != "samples_used"},
                                               "returns_filled": returns([o for X in by.values() for o in X if bucket_pred(b)(o)])}
                                           for b in ("ALL", "NH", "LOW20", "LOW40", "HIGH40")}
    S1 = [o for o in O if o["sample"] == SAMPLES[0]]
    timing = {}
    for b in ("ALL", "NH", "LOW20", "LOW40", "HIGH40"):
        X = [o for o in S1 if bucket_pred(b)(o)]
        F = [o for o in X if o["filled"]]
        pre = [o for o in F if o["fill_ts"] is not None and o["fill_ts"] <= o["first_close"] - 3600]
        Xp = [dict(o, filled=(o in pre)) for o in X]
        g, nf, nu = gap_of(Xp)
        timing[b] = {"orders": len(X), "filled": len(F), "filled_pre_1h": len(pre),
                     "share_of_YES_outcome_orders_filled": round(st.mean(o["filled"] for o in X if not o["no_won"]), 3) if any(not o["no_won"] for o in X) else None,
                     "share_of_YES_outcome_orders_filled_pre": round(st.mean(o in pre for o in X if not o["no_won"]), 3) if any(not o["no_won"] for o in X) else None,
                     "share_of_NO_outcome_orders_filled": round(st.mean(o["filled"] for o in X if o["no_won"]), 3) if any(o["no_won"] for o in X) else None,
                     "share_of_NO_outcome_orders_filled_pre": round(st.mean(o in pre for o in X if o["no_won"]), 3) if any(o["no_won"] for o in X) else None,
                     "gap_if_cancel_1h_before_first_close": round(g, 4) if g is not None else None,
                     "lor_if_cancel_1h_before_first_close": (lambda v: round(v, 3) if v is not None else None)(mh_lor([Xp])),
                     "lor_as_run": (lambda v: round(v, 3) if v is not None else None)(mh_lor([X])),
                     "ret_filled_pre": returns([o for o in Xp if o["filled"]]).get("ret_per_dollar"),
                     "ret_filled_pre_EVAL": returns([o for o in Xp if o["filled"] and o["part"] == "EVAL"]).get("ret_per_dollar")}
    out["S1_fill_timing_all_periods"] = timing
    out["S1_full_fills_EVAL"] = {b: returns([o for o in S1 if o["part"] == "EVAL" and bucket_pred(b)(o) and (not o["filled"] or o["fill_class"] == "full")])
                                 for b in ("ALL", "NH", "LOW20", "LOW40", "HIGH40")}
    return out


def show(res: dict) -> None:
    for part in ("CONFIRM", "EVAL"):
        print(f"== {part}")
        for b, r in res[part].items():
            rf = r["returns_filled"]
            print(f"  {b:6s} orders {r['orders']:4d} ev {r['events']:3d} filled {r['filled']:4d} unf {r['unfilled']:4d} fillrate {r['fill_rate']} "
                  f"NOwin f/u {r['filled_NO_win']}/{r['unfilled_NO_win']} gap_raw {r['pooled_gap_raw']} | strat gap {r['gap_bucket']} vs all {r['gap_all_same_weights']} "
                  f"R {r['R']} | LOR {r['lor_bucket']} vs {r['lor_all']} R_LOR {r['R_LOR']} | boot {r.get('bootstrap', {}).get('R_90')} {r.get('bootstrap', {}).get('R_LOR_90')}")
            print(f"         filled ret/$ {rf.get('ret_per_dollar')} sw {rf.get('size_weighted_ret')} t {rf.get('t')} wo3 {rf.get('ret_wo3')} "
                  f"h {rf.get('half1')}/{rf.get('half2')} px {rf.get('avg_px')} betaLB {rf.get('beta_lo_ret')} medq {rf.get('median_filled_contracts')}")
            for smp, s in r["by_sample"].items():
                print(f"           {smp:11s} {s}")
    print("DECISION", json.dumps(res["decision"], indent=1))
    ex = res["extras_descriptive"]
    for k, v in ex.items():
        print("==", k)
        for b, r in v.items():
            print(f"  {b:6s} {json.dumps(r)[:420]}")


def result() -> dict:
    """Compose result.json from backtest.json (no recomputation)."""
    r = json.loads((OUT / "backtest.json").read_text())
    calls = sum(1 for _ in (OUT / "calls.log").open()) if (OUT / "calls.log").exists() else 0
    ev, cf = r["EVAL"], r["CONFIRM"]
    tpd = {"LOW20": 7.46, "LOW40": 17.7, "NH": 10.79}      # EVAL fills / day over the S1 EVAL span (20.0 days), S1 dominates

    def vrow(b):
        x = ev[b]["returns_filled"]
        return {"rule": f"{b}: seeded-book maker NO orders of S1-S4 whose prior hit rate bucket is {b}; primary = AS-gap shrink "
                        f"R {ev[b]['R']} (90% {ev[b]['bootstrap']['R_90']}), R_LOR {ev[b]['R_LOR']} (90% {ev[b]['bootstrap']['R_LOR_90']}); "
                        f"filled NO win {ev[b]['filled_NO_win']} vs unfilled {ev[b]['unfilled_NO_win']} (ALL: {ev['ALL']['filled_NO_win']} vs {ev['ALL']['unfilled_NO_win']}); "
                        f"verdict {r['decision'][b]['verdict']}; returns are maker through-only, fee 0, size-weighted {x['size_weighted_ret']}",
                "n": x["n"], "events": x["events"], "win": x["win"], "avg_px": x["avg_px"], "ret_per_dollar": x["ret_per_dollar"], "t": x["t"],
                "ret_wo3": x["ret_wo3"], "half1": x["half1"], "half2": x["half2"], "median_capacity_contracts": x["median_filled_contracts"],
                "trades_per_day": tpd[b]}
    out = {
        "name": "r5_mention_speaker_base_rate_maker_filter",
        "verdict": "dead",
        "kalshi_calls_used": calls,
        "variants_examined": r["variants_examined"],
        "decision": r["decision"],
        "validation": [vrow(b) for b in ("LOW20", "LOW40", "NH")],
        "discovery_best": {"rule": "LOW20 on CONFIRM (first 70% of each sample's events by listing time), filled orders", "n": cf["LOW20"]["returns_filled"]["n"],
                           "ret_per_dollar": cf["LOW20"]["returns_filled"]["ret_per_dollar"], "t": cf["LOW20"]["returns_filled"]["t"],
                           "R": cf["LOW20"]["R"], "R_LOR": cf["LOW20"]["R_LOR"]},
        "reference_ALL_eval": ev["ALL"]["returns_filled"],
        "extras_descriptive": {k: v for k, v in r["extras_descriptive"].items()},
    }
    (OUT / "result_core.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    if "result" in sys.argv:
        print(json.dumps(result(), indent=1)[:3000])
    else:
        r = run()
        show(r)
