"""Summary of the r5_midterms_2026_thin_race_markets forward paper log (0 Kalshi calls).

Reads forward.jsonl / forward_state.json and writes forward_summary.json: passes by mode, universe, baseline spreads
and probed depth, night triggers and repricing lags (same definitions as the archive kill test), stale cross-book
passes, and per paper rule (F1/F2/F3) fills, settled results, mean return per $, clustered t (descriptive: one
cluster), mean without the 3 best and the exact one-sided 95% binomial lower bound, against the pre-registered kill /
promote rules. Usage: .venv/bin/python -m lab.kalshi.strategies.r5_midterms_2026_thin_race_markets_summary"""
from __future__ import annotations
import json, math, os, statistics as st, sys
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(os.environ.get("R5MID_OUT", "data/kalshi_lab/strategies/r5_midterms_2026_thin_race_markets"))


def beta_lower(k: int, n: int, a: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound on a binomial rate, by bisection on the binomial tail."""
    if k == 0:
        return 0.0
    def tail(p):   # P(X >= k | n, p)
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < a:
            lo = mid
        else:
            hi = mid
    return lo


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def rule_stats(fills: list[dict]) -> dict:
    s = [f for f in fills if f.get("settled")]
    out = {"fills": len(fills), "settled": len(s), "races": len({f["e"] for f in fills}),
           "avg_px": st.mean(f["px"] for f in fills) if fills else None}
    if not s:
        return out
    rets = [f["settled"]["ret"] for f in s]; wins = sum(f["settled"]["win"] for f in s)
    ev = defaultdict(list)
    for f in s:
        ev[f["e"]].append(f["settled"]["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0.0
    srt = sorted(rets, reverse=True)
    avg_px = st.mean(f["px"] + f["fee"] for f in s)
    p_lo = beta_lower(wins, len(s))
    out.update({"win": wins / len(s), "ret_per_dollar": st.mean(rets), "t_clustered_descriptive": (st.mean(em) / (sd / math.sqrt(len(em)))) if len(em) > 2 and sd > 0 else None,
                "ret_wo3": st.mean(srt[3:]) if len(srt) > 3 else None,
                "binomial_lb_ret_per_dollar": p_lo / avg_px - 1})
    out["kill"] = len(s) >= 10 and (out["ret_per_dollar"] < 0 or out["binomial_lb_ret_per_dollar"] < -0.10)
    return out


def main() -> None:
    f = OUT / "forward.jsonl"
    if not f.exists():
        print("no forward data"); return
    rows = [json.loads(l) for l in f.open()]
    stt = json.loads((OUT / "forward_state.json").read_text()) if (OUT / "forward_state.json").exists() else {}
    passes = [r for r in rows if r["type"] == "pass"]
    last = stt.get("last", {}); uni = stt.get("universe", {})
    spreads = [v[1] - v[0] for t, v in last.items() if uni.get(t, {}).get("kind") == "race" and v[0] is not None and v[1] is not None and 0 < v[1] <= 1 and (v[4] or "") not in ("finalized", "settled", "closed")]
    probes = [r for r in rows if r["type"] == "probe"]
    books = stt.get("books", {})
    lags = {}
    for trig in ("Q", "M"):
        d = [b[trig] for b in books.values() if trig in b]
        lags[trig] = {"triggered_books": len(d)}
        for k in ("ask95", "mid95", "bid95"):
            xs = [(x[k] - x["t0"]) / 60 for x in d if x.get(k)]
            lags[trig][f"median_lag_{k}_min"] = med(xs)
            lags[trig][f"never_{k}"] = sum(1 for x in d if not x.get(k))
        xs = [(x["ask95"] - x["t0"]) / 60 for x in d if x.get("ask95")]
        lags[trig]["share_ask95_lt3min"] = (sum(1 for x in xs if x < 3) / len(d)) if d else None
    fills = list(stt.get("fills", {}).values())
    by_rule = {r: rule_stats([x for x in fills if x["rule"] == r]) for r in ("F1", "F2", "F3")}
    f1 = by_rule["F1"]
    depth = [p.get("no_offered_le90", 0) + p.get("yes_offered_le90", 0) for p in probes if p.get("why") == "signal"]
    promote = bool(f1.get("settled", 0) >= 20 and (f1.get("ret_per_dollar") or -1) >= 0.10 and (f1.get("ret_wo3") or -1) > 0
                   and (f1.get("binomial_lb_ret_per_dollar") or -1) > 0 and (med(depth) or 0) >= 10)
    out = {"passes": len(passes), "passes_by_mode": dict(Counter(p["mode"] for p in passes)),
           "calls_total": sum(p["calls"] for p in passes), "max_calls_in_a_pass": max((p["calls"] for p in passes), default=0),
           "first_pass": passes[0]["ts"] if passes else None, "last_pass": passes[-1]["ts"] if passes else None,
           "universe": len(uni), "universe_races": sum(1 for v in uni.values() if v["kind"] == "race"),
           "race_events_2book": len({v["e"] for t, v in uni.items() if v["kind"] == "race"}),
           "median_race_spread_last_snapshot": med(spreads),
           "probes": len(probes), "median_probe_depth_le95_either_side": med([max(p.get("yes_offered_le95", 0), p.get("no_offered_le95", 0)) for p in probes]),
           "triggers": lags, "stale_cross_book_passes": sum(len(v) for v in stt.get("stale", {}).values()),
           "stale_races": len(stt.get("stale", {})), "signals": Counter(r["rule"] for r in rows if r["type"] == "signal"),
           "nofills": Counter((r["rule"], r["why"]) for r in rows if r["type"] == "nofill").most_common(),
           "rules": by_rule, "F1_promote_to_2028_monitor": promote,
           "errors": [e for p in passes for e in p.get("errors", [])][-20:]}
    out["signals"] = dict(out["signals"]); out["nofills"] = [[f"{a}:{b}", n] for (a, b), n in out["nofills"]]
    (OUT / "forward_summary.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
