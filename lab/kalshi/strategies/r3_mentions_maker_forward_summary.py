"""r3_mentions_maker_forward_summary: score the forward paper log of the frozen mention-market maker (no Kalshi calls).

Reads data/kalshi_lab/strategies/r3_mentions_maker_forward/forward.jsonl (written by r3_mentions_maker_forward_logger)
and writes summary.json next to it. For each arm (C1 primary; C2, C3 nested subsets of the same orders) and each fill
measure it reports the size-weighted return per $ (weight = filled contracts / N, i.e. dollars actually deployed):
  any      prints with yes_price >= s (assumes we were first in the queue at s)
  through  prints with yes_price > s  (certain fills: price priority) - the pre-registered maker gate row 2 uses this
  strict   through + prints at s whose taker sold YES (a resting YES bid at s would have crossed our ask)
  queue    probed orders only: strict + YES-taker prints at s beyond the size already resting at s when we posted
P&L per filled contract: +s if the market resolves NO, -(1 - s) if YES; no fee (maker, fee_type 'quadratic').
Statistics: t clustered by event (ratio estimator), t of event means, mean without the 3 best fills, halves by the
event's first close, capacity (filled contracts), fill timing before the event's first close (pre-event vs in-event),
first-hour spread, queue-probe undercutting, a capped book (<= 10 concurrent $5 orders or positions, chosen by C2 EV,
as the live bankroll would allow), and the pre-registered kill and gate checks.
Usage: .venv/bin/python -m lab.kalshi.strategies.r3_mentions_maker_forward_summary [--K 15450] [--quiet]"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist

OUT = Path("data/kalshi_lab/strategies/r3_mentions_maker_forward")
BACKTEST_SW = {"C1": 0.141, "C2": 0.192, "C3": 0.1655}      # r2 validation, size-weighted (backtest reference)
MEASURES = ("any", "through", "strict", "queue")
ARMS = ("C1", "C2", "C3")
CAP_ORDERS = 10
SETTLE_LAG_S = 1800       # a filled position ties up collateral until about first close + 30 min


def load(out: Path = OUT) -> dict:
    rows = [json.loads(l) for l in (out / "forward.jsonl").open()] if (out / "forward.jsonl").exists() else []
    R = defaultdict(list)
    for r in rows:
        R[r["type"]].append(r)
    return R


def orders(R: dict) -> list[dict]:
    """Posted orders with their settlement and fill rows joined (only orders with both are complete)."""
    settle = {r["e"]: r for r in R["settle"]}
    fills = {r["ticker"]: r for r in R["fill"]}
    probes = defaultdict(dict)
    for p in R["probe"]:
        probes[p["ticker"]][p["phase"]] = p
    out = []
    for e in R["entry"]:
        if not e.get("posted"):
            continue
        o = dict(e); o["probes"] = probes.get(e["ticker"], {})
        S = settle.get(e["e"]); F = fills.get(e["ticker"])
        o["complete"] = bool(S and F)
        if S:
            res = (S["markets"].get(e["ticker"]) or [""])[0]
            o["result"] = res; o["first_close"] = S["first_close"]
        if F:
            o["fill"] = F
        out.append(o)
    return out


def ret_no(o: dict) -> float | None:
    """Return per $ of a filled NO at 1 - s."""
    if o.get("result") not in ("yes", "no"):
        return None
    s = o["s_c"] / 100.0
    return s / (1 - s) if o["result"] == "no" else -1.0


def weight(o: dict, measure: str) -> float | None:
    F = o.get("fill") or {}
    if measure == "queue":
        return F.get("f_queue")
    return F.get(f"f_{measure}")


def stats(items: list[tuple[str, float, float, dict]]) -> dict:
    """items: (event, weight, return, order). Size-weighted mean, clustered-by-event t (ratio estimator), and more."""
    it = [x for x in items if x[1] and x[1] > 0]
    if not it:
        return {"n_filled": 0}
    W = sum(w for _, w, _, _ in it); R = sum(w * r for _, w, r, _ in it) / W
    by = defaultdict(list)
    for e, w, r, o in it:
        by[e].append((w, r))
    E = len(by)
    if E > 1:
        var = sum(sum(w * (r - R) for w, r in v) ** 2 for v in by.values()) / W ** 2 * E / (E - 1)
        t = R / math.sqrt(var) if var > 0 else float("nan")
        em = [sum(w * r for w, r in v) / sum(w for w, _ in v) for v in by.values()]
        t_em = st.mean(em) / (st.pstdev(em) / math.sqrt(E - 1)) if E > 2 and st.pstdev(em) > 0 else float("nan")
    else:
        t = t_em = float("nan")
    srt = sorted(it, key=lambda x: -x[2])
    rest = srt[3:]
    wo3 = sum(w * r for _, w, r, _ in rest) / sum(w for _, w, _, _ in rest) if rest else None
    closes = sorted(o["first_close"] for _, _, _, o in it)
    mid = closes[len(closes) // 2]
    h = {"h1": [0.0, 0.0], "h2": [0.0, 0.0]}
    for _, w, r, o in it:
        hh = h["h1" if o["first_close"] < mid else "h2"]; hh[0] += w * r; hh[1] += w
    span_d = (closes[-1] - closes[0]) / 86400 if len(closes) > 1 else 0
    filled_cnt = [w * o["N"] for _, w, _, o in it]
    return {"n_filled": len(it), "events": E, "win": round(sum(1 for _, _, r, _ in it if r > 0) / len(it), 3),
            "avg_no_px": round(sum(w * (1 - o["s_c"] / 100) for _, w, _, o in it) / W, 3),
            "ret_per_dollar_size_weighted": round(R, 4), "t_clustered": round(t, 2) if t == t else None, "t_event_means": round(t_em, 2) if t_em == t_em else None,
            "equal_dollar_ret_filled": round(st.mean(r for _, _, r, _ in it), 4),
            "ret_wo3": round(wo3, 4) if wo3 is not None else None,
            "half1": round(h["h1"][0] / h["h1"][1], 4) if h["h1"][1] else None, "half2": round(h["h2"][0] / h["h2"][1], 4) if h["h2"][1] else None,
            "filled_dollars": round(W * 5, 2), "median_filled_contracts": round(st.median(filled_cnt), 2),
            "fills_per_day": round(len(it) / span_d, 2) if span_d > 0 else None,
            "se": round(abs(R / t), 4) if t and t == t and t != 0 else None}


def capped_book(os_: list[dict], measure: str) -> set:
    """Tickers the live $50 book would have held: <= CAP_ORDERS concurrent orders or positions, best C2 EV first.
    An order occupies a slot from its post until the event's first close (unfilled) or first close + 30 min (filled):
    either way that is the state a live trader would see at a later post time."""
    done = [o for o in os_ if o["complete"]]
    by_post = defaultdict(list)
    for o in done:
        by_post[round(o["ts"] / 60)].append(o)            # orders posted within the same minute compete for slots
    held: list[tuple[float, str]] = []
    keep = set()
    for k in sorted(by_post):
        now = k * 60
        held = [(end, t) for end, t in held if end > now]
        for o in sorted(by_post[k], key=lambda o: -o["ev_c2"]):
            if len(held) >= CAP_ORDERS:
                break
            w = weight(o, measure) or 0
            end = o["first_close"] + (SETTLE_LAG_S if w > 0 else 0)
            held.append((end, o["ticker"])); keep.add(o["ticker"])
    return keep


def summarize(K: int = 15450, out: Path = OUT, quiet: bool = False) -> dict:
    R = load(out)
    os_ = orders(R)
    done = [o for o in os_ if o["complete"]]
    stf = out / "state.json"
    stj = json.loads(stf.read_text()) if stf.exists() else {}
    res: dict = {"started": stj.get("started"), "docs_gate_c_at_start": stj.get("docs_gate_c_at_start"), "counts": {
        "passes": len(R["pass"]), "kalshi_calls_logged": sum(p.get("calls", 0) for p in R["pass"]),
        "listings": len(R["listing"]), "listings_in_scope": sum(1 for r in R["listing"] if r["status"] != "prestart"),
        "missed": dict(sorted(((k, sum(1 for r in R["missed"] if r["reason"] == k)) for k in {r["reason"] for r in R["missed"]}))),
        "entries": len(R["entry"]), "posted": sum(1 for e in R["entry"] if e.get("posted")),
        "skipped": dict(sorted(((k, sum(1 for e in R["entry"] if e.get("reason") == k)) for k in {e.get("reason") for e in R["entry"] if not e.get("posted")}))),
        "events_with_orders": len({o["e"] for o in os_}), "events_settled_with_orders": len({o["e"] for o in done}),
        "orders_complete": len(done), "probes": len(R["probe"]),
        "first_pass": min((p["ts"] for p in R["pass"]), default=None), "last_pass": max((p["ts"] for p in R["pass"]), default=None)}}
    lags = [e["lag_s"] for e in R["entry"]]
    res["entry_lag_s"] = {"median": round(st.median(lags), 1), "max": max(lags)} if lags else None
    sp = [e["ask_c"] - e["bid_c"] for e in R["entry"] if e.get("ask_c") is not None and e.get("bid_c") and 0 < e["bid_c"] <= e["ask_c"] < 100]
    res["first_hour_spread_c"] = {"median": st.median(sp), "n": len(sp)} if sp else None

    # ---- arms x measures
    arms = {}
    for arm in ARMS:
        sel = [o for o in done if arm == "C1" or o.get(arm)]
        for m in MEASURES:
            pool = [o for o in sel if m != "queue" or (o.get("fill") or {}).get("f_queue") is not None]
            items = [(o["e"], weight(o, m), ret_no(o), o) for o in pool if ret_no(o) is not None]
            s = stats(items); s["orders"] = len(pool)
            s["fill_fraction_mean"] = round(st.mean(weight(o, m) or 0 for o in pool), 3) if pool else None
            arms[f"{arm}|{m}"] = s
        keep = capped_book(sel, "any")
        for m in ("any", "through"):
            items = [(o["e"], weight(o, m), ret_no(o), o) for o in sel if o["ticker"] in keep and ret_no(o) is not None]
            arms[f"{arm}|{m}|capped10"] = stats(items)
    res["arms"] = arms

    # ---- fill timing: hours between the first fill print and the event's first close (in-event fills are adverse)
    tim = defaultdict(list)
    for o in done:
        F = o.get("fill") or {}; r = ret_no(o)
        if F.get("t_first_any") and r is not None:
            h = (o["first_close"] - F["t_first_any"]) / 3600
            b = "<1h" if h < 1 else "1-3h" if h < 3 else ">=3h"
            tim[b].append((o["e"], F["f_any"], r, o))
    res["fill_timing_any"] = {b: {k: v for k, v in stats(x).items() if k in ("n_filled", "events", "ret_per_dollar_size_weighted", "t_clustered")}
                              for b, x in sorted(tim.items())}

    # ---- queue probes
    pr = defaultdict(list)
    for p in R["probe"]:
        pr[p["phase"]].append(p)
    res["probes"] = {ph: {"n": len(v), "share_with_size_at_our_price": round(st.mean(1 if p["size_at_our_price"] > 0 else 0 for p in v), 3),
                          "median_size_at_our_price": st.median(p["size_at_our_price"] for p in v),
                          "share_undercut": round(st.mean(1 if p["undercut"] else 0 for p in v), 3),
                          "share_crossed": round(st.mean(1 if p["crossed"] else 0 for p in v), 3),
                          "median_since_post_s": st.median(p["since_post_s"] for p in v)} for ph, v in sorted(pr.items())}

    # ---- pre-registered kill and gate checks (C1 primary)
    ev_done = len({o["e"] for o in done})
    thr = arms["C1|through"]; anyf = arms["C1|any"]
    med_sp = res["first_hour_spread_c"]["median"] if res["first_hour_spread_c"] else None
    kill = {"evaluated": ev_done >= 30, "events_settled": ev_done,
            "through_mean_below_0": (thr.get("ret_per_dollar_size_weighted") or 0) < 0 if thr.get("n_filled") else None,
            "any_mean_below_5pct": (anyf.get("ret_per_dollar_size_weighted") or 0) < 0.05 if anyf.get("n_filled") else None,
            "median_first_hour_spread_below_4c": med_sp < 4 if med_sp is not None else None}
    kill["KILL"] = bool(kill["evaluated"] and (kill["through_mean_below_0"] or kill["any_mean_below_5pct"] or kill["median_first_hour_spread_below_4c"]))
    res["kill"] = kill
    bar = NormalDist().inv_cdf(1 - 0.05 / K)
    gate = {}
    for arm in ARMS:
        g = arms[f"{arm}|through"]
        se = g.get("se")
        gate[arm] = {
            "1_fills>=40": g.get("n_filled", 0) >= 40, "events>=30": g.get("events", 0) >= 30,
            "2_through_size_weighted>=+10%": (g.get("ret_per_dollar_size_weighted") or -9) >= 0.10,
            "3_t>=bar": (g.get("t_clustered") or 0) >= bar, "t": g.get("t_clustered"), "bar": round(bar, 2), "K": K,
            "3b_t>=family_bar_2.39": (g.get("t_clustered") or 0) >= 2.39,
            "4_wo3>0": (g.get("ret_wo3") or -9) > 0,
            "5_halves>0": bool(g.get("half1") is not None and g["half1"] > 0 and g.get("half2") is not None and g["half2"] > 0),
            "6_median_filled_contracts>=5": (g.get("median_filled_contracts") or 0) >= 5,
            "7_not_below_backtest-2SE": (g.get("ret_per_dollar_size_weighted") is not None and se is not None
                                         and g["ret_per_dollar_size_weighted"] >= BACKTEST_SW[arm] - 2 * se)}
        gate[arm]["PASS"] = all(v for k, v in gate[arm].items() if k[0].isdigit() or k == "events>=30") and ev_done >= 60
    res["gate"] = gate
    res["run_length_ok"] = {"events_settled": ev_done, "needs": ">= 60 events and >= 28 days"}
    (out / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    if not quiet:
        c = res["counts"]
        print(f"passes={c['passes']} calls={c['kalshi_calls_logged']} listings={c['listings']} (in scope {c['listings_in_scope']}) "
              f"entries={c['entries']} posted={c['posted']} skipped={c['skipped']} missed={c['missed']} complete={c['orders_complete']} "
              f"events settled w/ orders={c['events_settled_with_orders']} probes={c['probes']}")
        print("entry lag:", res["entry_lag_s"], "| first-hour spread:", res["first_hour_spread_c"])
        for k, v in arms.items():
            if v.get("n_filled"):
                print(f"  {k:22s} filled={v['n_filled']:4d}/{v.get('orders', '?')} ev={v['events']:3d} sw_ret={v['ret_per_dollar_size_weighted']:+.4f} "
                      f"t={v['t_clustered']} wo3={v['ret_wo3']} halves={v['half1']}/{v['half2']} med_cnt={v['median_filled_contracts']}")
        print("fill timing (any):", res["fill_timing_any"])
        print("probes:", res["probes"])
        print("kill:", kill)
        print("gate C1:", gate["C1"])
    return res


if __name__ == "__main__":
    K = int(sys.argv[sys.argv.index("--K") + 1]) if "--K" in sys.argv else 15450
    summarize(K, quiet="--quiet" in sys.argv)
