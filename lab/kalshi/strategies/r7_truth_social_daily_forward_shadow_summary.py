"""Forward-only summary and gate of r7_truth_social_daily_forward_shadow (KXTRUTHSOCIALD daily Truth Social count, rule D1),
against data/kalshi_lab/strategies/r7_truth_social_daily_forward_shadow/preregistration.json. Makes no network calls.

Reads forward.jsonl (passes, signals, Saturday arm) and settled.jsonl (events, settled signals); writes
forward_summary.json next to them (--test: the test_passes/ files).

PRIMARY measure: filled D1 trades (a bracket-side's first signal with >= 1 contract displayed at or better than the
signal price), equal-$ return per trade at the signal price after the ceil-to-cent fee on the paper order;
t clustered by event (day), sample standard deviation.
Forward gate (all must hold; docs/KALSHI_LAB.md rows 1-7 with amendments (d) and (k), forward data only per (l)):
  >= 40 independent events with a filled trade and >= 40 trades; mean >= +10%/$; t >= z(0.05/K) with K as recorded
  on the review day; one-sided 95% exact-binomial lower bound on the per-$ return over unique events > 0; mean > 0
  without the 3 best trades; both halves (by event date) > 0; median displayed size at signal >= $5; no wrong-side
  fill from a data or code error; at least 90 settled forward event-days.
Kill rule (evaluated from 30 settled forward event-days on; any time for a data/code wrong-side fill):
  mean per $ of filled trades < 0; median displayed contracts at signal < 5; more than 95% of eligible passes with
  every in-play bracket's mid within 5c of the model; no signal at all in the first 30 event-days.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_truth_social_daily_forward_shadow_summary [--test]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r7_truth_social_daily_forward_shadow"
K_DEFAULT = 20175          # lab K after round 6 (data/kalshi_lab/strategies/lead_round6/lead_checks.json)
MIN_EVENT_DAYS_REVIEW = 90
KILL_AT_DAYS = 30


def _read(f: Path) -> list[dict]:
    out = []
    if f.exists():
        for line in f.open():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def lab_K() -> int:
    ks = [K_DEFAULT]
    for f in (ROOT / "data/kalshi_lab/strategies").glob("lead_round*/lead_checks.json"):
        try:
            ks.append(int(json.loads(f.read_text())["K"]["K_after"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return max(ks)


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact-binomial (Clopper-Pearson) lower bound on the win rate (as in lead_round6)."""
    if k == 0 or n == 0:
        return 0.0

    def tail(p):
        return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def perf(rows: list[dict]) -> dict:
    """rows: {e, ret, won, px, fee, B}"""
    if not rows:
        return {"n": 0, "events": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r)
    em = {e: st.mean(x["ret"] for x in v) for e, v in ev.items()}
    ne = len(em); vals = list(em.values())
    sd = st.stdev(vals) if ne > 2 else 0.0
    t = st.mean(vals) / (sd / math.sqrt(ne)) if sd > 0 else None
    rs = sorted((r["ret"] for r in rows), reverse=True)
    best_ev = set(sorted(em, key=lambda e: -em[e])[:3])
    keep_ev = [r["ret"] for r in rows if r["e"] not in best_ev]
    order = sorted(ev, key=lambda e: ev[e][0]["B"]); h = len(order) // 2
    h1 = [r["ret"] for e in order[:h] for r in ev[e]]; h2 = [r["ret"] for e in order[h:] for r in ev[e]]
    px = st.mean(r["px"] for r in rows); fe = st.mean(r["fee"] for r in rows)
    k_ev = sum(1 for v in ev.values() if all(x["won"] for x in v))
    lb = cp_lower(k_ev, ne)
    return {"n": len(rows), "events": ne, "losses": sum(not r["won"] for r in rows), "win": round(sum(r["won"] for r in rows) / len(rows), 4),
            "avg_px": round(px, 4), "ret_per_dollar": round(st.mean(rs), 4), "ret_event_mean": round(st.mean(vals), 4),
            "t_clustered": round(t, 3) if t is not None else None, "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "ret_wo3_events": round(st.mean(keep_ev), 4) if keep_ev else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "beta_lb_ret_events": round(lb / px - 1 - fe / px, 4), "event_sd": round(sd, 4)}


def events_needed(mean: float, sd: float, bar: float) -> int | None:
    return math.ceil((bar * sd / mean) ** 2) if mean and mean > 0 and sd > 0 else None


def main(argv: list[str] | None = None) -> dict:
    argv = sys.argv[1:] if argv is None else argv
    test = "--test" in argv
    d = OUT / "test_passes" if test else OUT
    F = _read(d / ("forward_test.jsonl" if test else "forward.jsonl"))
    S = _read(d / ("settled_test.jsonl" if test else "settled.jsonl"))
    passes = [r for r in F if r.get("row") == "pass"]
    sigs = [r for r in F if r.get("row") == "signal"]
    sats = [r for r in F if r.get("row") == "sat_arm"]
    evrows = {r["event"]: r for r in S if r.get("row") == "event"}
    settled = [r for r in S if r.get("row") == "settled"]
    K = lab_K(); bar = NormalDist().inv_cdf(1 - 0.05 / K)
    pass_days = sorted({r["event"] for r in passes if r.get("event")})
    health = {"passes": len(passes), "eligible_passes": sum(bool(r.get("eligible")) for r in passes), "event_days_with_passes": len(pass_days),
              "settled_event_days": sum(1 for e in pass_days if e in evrows),
              "max_kalshi_calls_per_pass": max((r.get("kalshi_calls", 0) for r in passes), default=0),
              "kalshi_calls_in_passes": sum(r.get("kalshi_calls", 0) for r in passes),
              "rollcall_incomplete_passes": sum(1 for r in passes if not (r.get("rollcall") or {}).get("ok")),
              "no_event_passes": sum(1 for r in passes if not r.get("event")),
              "ladder_not_complete_passes": sum(1 for r in passes if r.get("event") and not r.get("ladder_ok")),
              "first": passes[0]["iso"] if passes else None, "last": passes[-1]["iso"] if passes else None}
    # ---- primary: filled trades
    def row(r):
        return {"e": r["event"], "ret": r["ret_per_dollar"], "won": r["won"], "px": r["px"], "fee": r["fee_per_contract"], "B": r["B"],
                "side": r["side"], "t": r["t"]}
    filled = [row(r) for r in settled if r["filled"]]
    first_any = {}
    for r in settled:
        k = (r["event"], r["t"], r["side"])
        if k not in first_any or r["ts"] < first_any[k]["ts"]:
            first_any[k] = r
    unit_all = [row(r) for r in first_any.values()]
    P = perf(filled)
    by_side = {s: perf([r for r in filled if r["side"] == s]) for s in ("YES", "NO")}
    # ---- capacity (all signals with a book read)
    disp = [(s["book"].get("disp_at_px") or 0.0, s["px"]) for s in sigs if (s.get("book") or {}).get("read")]
    cap = {"signals": len(sigs), "signals_with_book": len(disp), "fill_rate": round(sum(1 for c, _ in disp if c >= 1) / len(disp), 3) if disp else None,
           "median_contracts_at_px": st.median(c for c, _ in disp) if disp else None,
           "median_usd_at_px": round(st.median(c * p for c, p in disp), 2) if disp else None}
    # ---- data / code integrity
    mism = sorted(e for e, r in evrows.items() if r.get("rc_bracket_match") is False)
    dead_yes = [r for r in settled if r["filled"] and r["side"] == "YES" and r.get("hi") is not None
                and r.get("seen_at_signal") is not None and r["hi"] < r["seen_at_signal"]]
    wrong_side = [r for r in settled if r["filled"] and not r["won"] and r["event"] in mism] + dead_yes
    integrity = {"events_rollcall_bracket_mismatch": mism, "filled_yes_on_dead_bracket": len(dead_yes),
                 "wrong_side_fills_from_data_or_code": len(wrong_side)}
    # ---- has the market caught up with the model? and model vs mid log loss on settled days (descriptive)
    caught = tot = 0
    ll = defaultdict(lambda: {"d1": 0.0, "week": 0.0, "day": 0.0, "mid": 0.0, "n": 0})
    for r in passes:
        if not r.get("eligible") or not r.get("brackets"):
            continue
        inplay = []
        for b in r["brackets"]:
            tk, sub, lo, hi, bid, ask, bsz, asz, vol, p1, pw, pdy = b
            if bid is None or ask is None or not (0 < bid < ask < 1):
                continue
            mid = (bid + ask) / 2
            if 0.05 <= mid <= 0.95:
                inplay.append((tk, mid, p1, pw, pdy))
        if inplay:
            tot += 1; caught += all(abs(m - p1) <= 0.05 for _, m, p1, _, _ in inplay)
        ev = evrows.get(r["event"])
        if ev and ev.get("winner"):
            for tk, mid, p1, pw, pdy in inplay:
                y = tk == ev["winner"]
                for k, p in (("d1", p1), ("week", pw), ("day", pdy), ("mid", mid)):
                    p = min(max(p, 0.005), 0.995)
                    ll[r["event"]][k] += -math.log(p if y else 1 - p)
                ll[r["event"]]["n"] += 1
    llr = {}
    for k in ("d1", "week", "day"):
        dif = [(v[k] - v["mid"]) / v["n"] for v in ll.values() if v["n"]]
        n_obs = sum(v["n"] for v in ll.values())
        llr[k] = {"obs": n_obs, "events": len(dif), "ll_model": round(sum(v[k] for v in ll.values()) / n_obs, 4) if n_obs else None,
                  "ll_mid": round(sum(v["mid"] for v in ll.values()) / n_obs, 4) if n_obs else None,
                  "t_model_minus_mid_by_event": round(st.mean(dif) / (st.stdev(dif) / math.sqrt(len(dif))), 2)
                  if len(dif) > 2 and st.stdev(dif) > 0 else None}
    market = {"passes_with_inplay": tot, "share_all_inplay_within_5c": round(caught / tot, 4) if tot else None, "log_loss_settled": llr}
    # ---- Saturday arm (descriptive)
    ed = [x for r in sats for x in r.get("edges") or []]
    would = {}
    for r in sats:
        for w in r.get("would_signals") or []:
            would.setdefault((r.get("event"), w["t"], w["side"]), w)
    wr = []
    for (e, tk, side), w in would.items():
        ev = evrows.get(e)
        if ev and ev.get("winner"):
            won = (tk == ev["winner"]) == (side == "YES")
            fee = math.ceil(round(0.07 * w["px"] * (1 - w["px"]) * 1000, 6)) / 1000
            wr.append({"e": e, "ret": ((1.0 if won else 0.0) - w["px"] - fee) / w["px"], "won": won, "px": w["px"], "fee": fee, "B": ev["B"]})
    sat = {"rows": len(sats), "rows_skipped": sum(1 for r in sats if r.get("skip")), "edge_points": len(ed),
           "mean_abs_weekly_minus_daily_mid": round(st.mean(abs(x["S_weekly"] - x["S_daily_mid"]) for x in ed), 4) if ed else None,
           "mean_abs_weekly_minus_model": round(st.mean(abs(x["S_weekly"] - x["S_model"]) for x in ed), 4) if ed else None,
           "would_signals_first": len(would), "would_signals_settled": perf(wr)}
    # ---- power
    sdays = health["settled_event_days"]
    tr_ev = P.get("events", 0)
    power = {"trade_events_per_settled_day": round(tr_ev / sdays, 3) if sdays else None,
             "projected_trade_events_per_year": round(365 * tr_ev / sdays) if sdays else None,
             "events_needed_at_bar_if_true_mean_10pct": events_needed(0.10, P.get("event_sd") or 1.0, bar),
             "events_needed_at_bar_if_true_mean_30pct": events_needed(0.30, P.get("event_sd") or 1.0, bar),
             "events_needed_at_observed_mean": events_needed(P.get("ret_event_mean") or 0, P.get("event_sd") or 0, bar)}
    # ---- kill rule
    kill = {"evaluated": sdays >= KILL_AT_DAYS, "settled_event_days": sdays, "kill_wrong_side_data_or_code": len(wrong_side) > 0}
    if sdays >= KILL_AT_DAYS:
        kill.update({"kill_mean_below_0": P.get("n", 0) > 0 and P["ret_per_dollar"] < 0,
                     "kill_median_size_below_5": cap["median_contracts_at_px"] is not None and cap["median_contracts_at_px"] < 5,
                     "kill_mid_within_5c_over_95pct": market["share_all_inplay_within_5c"] is not None and market["share_all_inplay_within_5c"] > 0.95,
                     "kill_no_signals": len(sigs) == 0})
    kill["verdict"] = "KILL" if any(v is True for k, v in kill.items() if k.startswith("kill_")) else ("continue" if kill["evaluated"] else "too early")
    # ---- gate
    g = {"K": K, "bar_t": round(bar, 3),
         "row1_events_ge_40_and_n_ge_40": P.get("events", 0) >= 40 and P.get("n", 0) >= 40,
         "row2_mean_ge_10pct": (P.get("ret_per_dollar") or -1) >= 0.10,
         "row3_t_ge_bar": (P.get("t_clustered") or 0) >= bar,
         "row3d_beta_lb_gt_0": (P.get("beta_lb_ret_events") if P.get("n") else -1) > 0,
         "row4_wo3_gt_0": (P.get("ret_wo3") or -1) > 0,
         "row5_both_halves_gt_0": P.get("n", 0) > 0 and (P.get("half1") or -1) > 0 and (P.get("half2") or -1) > 0,
         "row6_median_usd_at_px_ge_5": (cap["median_usd_at_px"] or 0) >= 5,
         "row7_no_wrong_side_fills": len(wrong_side) == 0,
         "min_90_settled_event_days": sdays >= MIN_EVENT_DAYS_REVIEW}
    g["PASS"] = all(v for k, v in g.items() if k.startswith(("row", "min_")))
    out = {"written": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "test": test, "health": health,
           "primary_filled_trades": P, "primary_by_side": by_side, "secondary_first_signal_unit_stake": perf(unit_all),
           "capacity": cap, "integrity": integrity, "market_vs_model": market, "saturday_arm": sat, "power": power,
           "kill_rule": kill, "gate": g}
    d.mkdir(parents=True, exist_ok=True)
    (d / "forward_summary.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: out[k] for k in ("health", "primary_filled_trades", "capacity", "kill_rule")}, indent=1, default=str))
    print("gate PASS" if g["PASS"] else "gate not passed", {k: v for k, v in g.items() if k not in ("K", "bar_t", "PASS") and not v})
    return out


if __name__ == "__main__":
    main()
