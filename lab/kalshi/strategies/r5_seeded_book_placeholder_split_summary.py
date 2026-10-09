"""Summary of the r5_seeded_book_placeholder_split forward ledger (no Kalshi calls; run nightly).

Reads data/kalshi_lab/strategies/r5_seeded_book_placeholder_split/state.json (built by the logger from the three source
loggers) and applies the frozen rules of preregistration.json -> forward_test:
  * per order: f = min(N, source through count, audit through count when present) (conservative); orders count once
    their fill window is known and their market settled yes/no (void dropped); maker fee 0; hold to settlement.
  * arms P (placeholder at the logged entry quote), NP, ALL; also per source.
  * statistics: size-weighted through-only return per $ with an event-clustered t, mean without the 3 best EVENTS,
    halves, equal-$ per fill, one-sided 95% exact bound over unique events (amendment d), filled vs unfilled NO win,
    the mirror (buy YES at bid + 1c, prints strictly below b), median filled contracts, probe capacity.
  * kill rule (each of ALL and P, once it has >= 60 filled events): kill if size-weighted mean < +10%/$, or the mean
    without the 3 best events < 0, or filled-NO win rate < unfilled-NO win rate - 20 points. Split rule: once P and NP
    each have >= 60 filled events, the split is dropped if P <= NP. Verdict due at >= 150 filled P events.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_seeded_book_placeholder_split_summary"""
from __future__ import annotations
import json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_stats import size_weighted, equal_dollar, binom_lb

OUT = Path("data/kalshi_lab/strategies/r5_seeded_book_placeholder_split")
KILL_EVENTS = 60
VERDICT_EVENTS = 150


def final_orders(out: Path = OUT) -> list[dict]:
    sf = out / "state.json"
    if not sf.exists():
        return []
    S = json.loads(sf.read_text())
    rows = []
    for t, o in S.get("orders", {}).items():
        fl = o.get("fill") or None
        res = o.get("result")
        x = {"ticker": t, "src": o["src"], "e": o["e"], "arm": o["arm"], "post": o["post"], "q": round(1 - o["s_c"] / 100, 4),
             "N": o["N"], "settled": res in ("yes", "no"), "result": res, "won": res == "no",
             "tclose": o.get("settle_ts") or o["post"], "t_fill": o["post"], "fill_known": fl is not None,
             "probe": o.get("probe"), "audit": o.get("audit")}
        if fl:
            thr = fl.get("thr") or 0.0
            au = o.get("audit") or {}
            if au.get("thr") is not None:
                thr = min(thr, au["thr"])
            x["f"] = float(min(o["N"], thr)); x["f_any"] = float(min(o["N"], fl.get("any") or 0.0))
            x["fallback"] = bool(fl.get("fallback")); x["partial"] = bool(fl.get("partial") or au.get("partial"))
            if o.get("b_c") and fl.get("m_thr") is not None:
                m = fl["m_thr"]
                if au.get("m_thr") is not None:
                    m = min(m, au["m_thr"])
                x["mb"] = o["b_c"] / 100; x["mf"] = float(min(o["Nb"], m))
        rows.append(x)
    return rows


def wo3_events(rows: list[dict]) -> float | None:
    """Size-weighted return without the 3 best events (by event pnl)."""
    ev = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        w = r["f"] * r["q"]; p = r["f"] * (1 - r["q"]) if r["won"] else -w
        ev[r["e"]][0] += p; ev[r["e"]][1] += w
    keep = sorted(ev.values(), key=lambda v: -v[0])[3:]
    W = sum(v[1] for v in keep)
    return round(sum(v[0] for v in keep) / W, 4) if W > 0 else None


def arm_stats(rows: list[dict]) -> dict:
    done = [r for r in rows if r["settled"] and r["fill_known"]]
    filled = [dict(r) for r in done if r["f"] > 0]
    fw = [r["won"] for r in done if r["f"] > 0]; uw = [r["won"] for r in done if r["f"] == 0]
    d = {"orders": len(rows), "open_or_unfilled_unknown": len(rows) - len(done), "resolved": len(done), "filled": len(filled),
         "filled_events": len({r["e"] for r in filled}),
         "sw_through": size_weighted([dict(r) for r in filled]), "eq_through": equal_dollar([dict(r) for r in filled]),
         "binomial_lb": binom_lb([dict(r) for r in filled]), "wo3_events": wo3_events(filled),
         "sw_any": size_weighted([dict(r, f=r["f_any"]) for r in done if r.get("f_any")]),
         "filled_NO_win": round(st.mean(fw), 3) if fw else None, "unfilled_NO_win": round(st.mean(uw), 3) if uw else None,
         "median_filled_contracts": st.median(r["f"] for r in filled) if filled else None,
         "fallback_fills": sum(1 for r in filled if r.get("fallback")), "partial_windows": sum(1 for r in done if r.get("partial"))}
    m = [dict(e=r["e"], f=r["mf"], q=r["mb"], won=not r["won"], tclose=r["tclose"]) for r in done if r.get("mf")]
    d["mirror_sw"] = size_weighted([dict(x) for x in m]); d["mirror_eq"] = equal_dollar([dict(x) for x in m])
    pr = [r["probe"] for r in rows if r.get("probe") and r["probe"].get("ok")]
    if pr:
        d["probe"] = {"n": len(pr), "median_size_at_best_ask": st.median(p["size_at_best_ask"] for p in pr),
                      "median_queue_at_our_price": st.median(p.get("queue_at_our_price", 0) for p in pr),
                      "share_still_placeholder": round(st.mean(bool(p["still_placeholder"]) for p in pr), 3),
                      "share_undercut": round(st.mean((p.get("size_inside_ours") or 0) > 0 for p in pr), 3)}
    d["kill"] = kill(d)
    return d


def kill(d: dict) -> dict:
    n = d["filled_events"]
    if n < KILL_EVENTS:
        return {"evaluated": False, "filled_events": n, "needed": KILL_EVENTS}
    sw = d["sw_through"].get("ret_per_dollar"); w3 = d["wo3_events"]
    fw, uw = d["filled_NO_win"], d["unfilled_NO_win"]
    why = []
    if sw is None or sw < 0.10:
        why.append(f"size-weighted mean {sw} < +0.10")
    if w3 is None or w3 < 0:
        why.append(f"mean without 3 best events {w3} < 0")
    if fw is not None and uw is not None and fw < uw - 0.20:
        why.append(f"filled NO win {fw} < unfilled {uw} - 0.20")
    return {"evaluated": True, "filled_events": n, "kill": bool(why), "reasons": why}


def summarize(out: Path = OUT) -> dict:
    R = final_orders(out)
    res = {"orders": len(R), "arms": {}, "by_source": {}}
    for arm in ("P", "NP", "ALL"):
        res["arms"][arm] = arm_stats([r for r in R if arm == "ALL" or r["arm"] == arm])
    for s in sorted({r["src"] for r in R}):
        res["by_source"][s] = {a: arm_stats([r for r in R if r["src"] == s and r["arm"] == a]) for a in ("P", "NP")}
    P, NP = res["arms"]["P"], res["arms"]["NP"]
    if P["filled_events"] >= KILL_EVENTS and NP["filled_events"] >= KILL_EVENTS:
        p, n = P["sw_through"].get("ret_per_dollar"), NP["sw_through"].get("ret_per_dollar")
        res["split_rule"] = {"evaluated": True, "P": p, "NP": n, "drop_split": p is None or n is None or p <= n}
    else:
        res["split_rule"] = {"evaluated": False, "P_filled_events": P["filled_events"], "NP_filled_events": NP["filled_events"]}
    res["verdict_due"] = P["filled_events"] >= VERDICT_EVENTS
    fj = out / "forward.jsonl"
    if fj.exists():
        rows = [json.loads(l) for l in fj.open() if l.strip()]
        passes = [r for r in rows if r["type"] == "pass"]
        res["passes"] = len(passes); res["calls_total"] = sum(r.get("calls", 0) for r in passes)
        res["census"] = [{k: r.get(k) for k in ("ticker", "src", "best_yes_bid_c", "implied_yes_ask_c", "size_at_best_ask", "still_placeholder")}
                         for r in rows if r["type"] == "census"][-20:]
        ps = [r["ts"] for r in passes]
        res["first_pass"], res["last_pass"] = (min(ps), max(ps)) if ps else (None, None)
    (out / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    r = summarize()
    for a, d in r["arms"].items():
        s = d["sw_through"]
        print(f"{a:3s} orders {d['orders']:4d} resolved {d['resolved']:4d} filled {d['filled']:4d} ev {d['filled_events']:3d} "
              f"sw {s.get('ret_per_dollar')} t {s.get('t')} wo3ev {d['wo3_events']} NOwin f/u {d['filled_NO_win']}/{d['unfilled_NO_win']} "
              f"mirror {d['mirror_sw'].get('ret_per_dollar')} kill {d['kill']}")
    print("split", r["split_rule"], "verdict_due", r["verdict_due"], "passes", r.get("passes"), "calls", r.get("calls_total"))
