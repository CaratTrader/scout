"""Summary of the r5_scheduled_release_poller forward log (paper) against its preregistration.

Reads data/kalshi_lab/strategies/r5_scheduled_release_poller/forward.jsonl (or test_passes/forward_test.jsonl with
--test) and writes forward_summary.json next to it.
  * health: passes, Kalshi calls per pass (max must be <= 10), errors, source polls;
  * B200 / HOUSE fires: first serve vs publication (date_gmt / XML action-time), the decided side's repricing time in
    Kalshi snapshots relative to our first serve (first snapshot with the decided taker price >= 0.97), trades,
    settlements, mean return per $ (equal-$), t clustered by fire, mean without the 3 best;
  * pre-registered kill rule (amended 2026-10-08 before any fire): counted over UNCERTAIN decided events (at least one
    decided side priced < 0.97 in the last snapshot at or before our first serve). After 15 of them: KILL if in more
    than half the market had repriced every uncertain side to >= 0.97 by our fill snapshot (first serve + 60 s), or if
    the mean equal-$ return per settled trade after fees is < +10%.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_scheduled_release_poller_summary [--test]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r5_scheduled_release_poller"


def px(m: list, side: str) -> float | None:
    # compact market row: [ticker, event, sub, yes_bid, yes_ask, last, vol, status, open, close]
    try:
        b, a = float(m[3]) if m[3] is not None else None, float(m[4]) if m[4] is not None else None
    except Exception:
        return None
    if side == "yes":
        return a if a is not None and 0 < a <= 1 else None
    return (1 - b) if b is not None and 0 <= b < 1 else None


def main() -> None:
    test = "--test" in sys.argv
    d = OUT / "test_passes" if test else OUT
    f = d / ("forward_test.jsonl" if test else "forward.jsonl")
    rows = [json.loads(l) for l in f.open()] if f.exists() else []
    passes = [r for r in rows if r.get("row") == "pass"]
    health = {"passes": len(passes), "max_calls_per_pass": max((r["kalshi_calls"] for r in passes), default=0),
              "kalshi_calls": sum(r["kalshi_calls"] for r in passes), "errors": [r["err"] for r in rows if r.get("row") == "error"][:10],
              "first": passes[0]["et"] if passes else None, "last": passes[-1]["et"] if passes else None}
    fires = {r["fire"]: r for r in rows if r.get("row") in ("fire", "house_roll")}
    decisions = defaultdict(list)
    for r in rows:
        if r.get("row") == "decision":
            decisions[r["fire"]].append(r)
    snaps = [r for r in rows if r.get("row") == "snap"]
    trades = [r for r in rows if r.get("row") == "trade"]
    settles = {(r["fire"], r["t"]): r for r in rows if r.get("row") == "settle"}
    ev = []
    for fid, fr in fires.items():
        t0 = fr.get("first_serve")
        dec = [x for x in decisions.get(fid, []) if x.get("sides")]
        if not dec or t0 is None:
            continue
        sides = dec[-1]["sides"]
        leg = fr.get("leg", "house")
        ss = sorted((x for x in snaps if x.get("leg") == leg and x.get("tag") != "fill_debut"), key=lambda x: x.get("t_rcv") or 0)
        def prices(snap):
            ms = {m[0]: m for m in snap.get("markets") or []}
            return {t: px(ms[t], sd) for t, sd in sides.items() if t in ms}
        before = [x for x in ss if (x.get("t_rcv") or 0) <= t0]
        after = [x for x in ss if (x.get("t_rcv") or 0) > t0]
        pre = before[-1] if before else (after[0] if after else None)
        if pre is None:
            continue
        p0 = prices(pre)
        unc = [t for t, v in p0.items() if v is not None and v < 0.97]
        fill = next((x for x in after if (x.get("t_rcv") or 0) >= t0 + 60), None)
        rep_fill = None
        if unc and fill is not None:
            pf = prices(fill)
            rep_fill = all((pf.get(t) or 0) >= 0.97 for t in unc)
        reprice = None
        for x in after:
            pv = prices(x)
            if unc and all((pv.get(t) or 0) >= 0.97 for t in unc):
                reprice = (x.get("t_rcv") or 0) - t0
                break
        pub = None
        if fr.get("date_gmt"):
            pub = dt.datetime.fromisoformat(fr["date_gmt"]).replace(tzinfo=dt.timezone.utc).timestamp()
        ev.append({"fire": fid, "leg": leg, "first_serve": t0, "serve_minus_pub_s": (t0 - pub) if pub else None,
                   "uncertain_sides_at_serve": len(unc), "repriced_before_fill": rep_fill,
                   "reprice_after_serve_s": reprice, "n_sides": len(sides), "simulated": fr.get("simulated", False)})
    tr = []
    for t in trades:
        s = settles.get((t["fire"], t["t"]))
        if s:
            tr.append({"e": t["fire"], "ret": s["ret_per_dollar"], "won": s["won"], "px": t["px"]})
    def tstat(xs):
        g = defaultdict(list)
        for x in xs:
            g[x["e"]].append(x["ret"])
        m = [st.mean(v) for v in g.values()]
        return (st.mean(m) / (st.pstdev(m) / math.sqrt(len(m)))) if len(m) > 2 and st.pstdev(m) > 0 else None
    rs = sorted((x["ret"] for x in tr), reverse=True)
    perf = {"n_trades": len(trades), "n_settled": len(tr), "events": len({x["e"] for x in tr}),
            "mean_ret_per_dollar": st.mean(rs) if rs else None, "t_clustered": tstat(tr),
            "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else None, "win": (sum(x["won"] for x in tr) / len(tr)) if tr else None}
    real = [e for e in ev if not e["simulated"] and e["uncertain_sides_at_serve"] > 0]
    kill = None
    if len(real) >= 15:
        rb = [e["repriced_before_fill"] for e in real if e["repriced_before_fill"] is not None]
        share = (sum(rb) / len(rb)) if rb else None
        kill = {"uncertain_decided_events": len(real), "share_repriced_before_fill": share,
                "median_reprice_after_serve_s": st.median([e["reprice_after_serve_s"] for e in real if e["reprice_after_serve_s"] is not None] or [float("nan")]),
                "kill_reprice_before_fill": share is not None and share > 0.5,
                "kill_mean_below_10pct": perf["mean_ret_per_dollar"] is None or perf["mean_ret_per_dollar"] < 0.10}
        kill["verdict"] = "KILL" if (kill["kill_reprice_before_fill"] or kill["kill_mean_below_10pct"]) else "continue"
    out = {"written": dt.datetime.now(dt.timezone.utc).isoformat(), "test": test, "health": health,
           "sources": {"house_rolls": sum(1 for r in rows if r.get("row") == "house_roll"),
                       "scotus_opinions": sum(1 for r in rows if r.get("row") == "scotus_opinion"),
                       "b200_fires": sum(1 for r in rows if r.get("row") == "fire"),
                       "watch_rows": sum(1 for r in rows if r.get("row") == "watch"),
                       "structure": [r for r in rows if r.get("row") == "structure"][-2:]},
           "decided_events": ev, "performance": perf, "kill_rule": kill or f"not evaluated: {len(real)} of 15 uncertain decided events"}
    (d / "forward_summary.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: out[k] for k in ("health", "sources", "performance", "kill_rule")}, indent=1, default=str))


if __name__ == "__main__":
    main()
