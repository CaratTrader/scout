"""r3_frozen_c1_other_mention_formats: out-of-sample test of the FROZEN round-2 C1 maker rule on mention formats round 2
never saw. Nothing is fitted; K rises by 3 (one cell per group).

Frozen C1 (data/kalshi_lab/strategies/r2_mentions_baserate/preregistration.json), applied unchanged:
  at the first top-of-hour H with H >= open + 1 h, the market open, NO market of its event closed yet, and the hourly
  candle quote at H satisfying 0 < yes_bid, yes_ask < 1, yes_ask - yes_bid >= 2c: rest a SELL-YES (= buy NO) order at
  s = yes_ask - 0.01 for N = floor($5 / (1 - s)) contracts, live from H + 120 s; cancel when any market of the event
  closes (or the market itself closes). No fee (every series here is fee_type 'quadratic': makers pay nothing).
  Hold to settlement: return per $ = (1{NO} - (1 - s)) / (1 - s).
Mechanism check (maker-YES mirror, same H and same eligibility): rest a BUY-YES at b = yes_bid + 0.01; return per $
  = (1{YES} - b) / b. If narrative YES buyers overpay at listing, the mirror must lose.

Groups (series lists declared before any candle was fetched):
  sports       KXMLBMENTION, KXNBAMENTION, KXNHLMENTION, KXWNBAMENTION (archive tier only: none listed after 2026-08-02;
               KXNASCARMENTION has no settled markets). 56 seeded events, one market each. These four were chosen because
               their events have several close times, taken (wrongly, found after fetching) to mean words close when said;
               in fact every sports series settles words in post-game batches (as KXNFL/KXNCAA/KXSNF visibly do), so the
               frozen cancel trigger fires only after the game. Variant V_sched (cancel at the ESPN scheduled start) is
               scored next to the literal rule.
  trump_accum  KXTRUMPSAYMONTH, KXTRUMPSAYCOUNTRY, KXTRUMPSAYCOMPANY, KXTRUMPSAY (weekly; round 2 skipped it); complete
               live-tier events (events straddling the archive cutoff excluded; KXTRUMPSAYNICKNAME's only live event
               straddles it).
  post_ladder  KXTRUTHSOCIAL (weekly), KXTRUTHSOCIALD (daily, substitute for KXELONTWEETS which has no market since
               2025-04); complete live-tier events.

Fill model (hourly candles [T, ask_c, bid_c, ask_lo, ask_hi, bid_lo, bid_hi, px_lo, px_hi, vol]; candle T covers prints
in (T-3600, T]; the candle T = H+3600 straddles posting at H+120 s):
  touch / queue = 0   (we created the level s, first in queue): a candle after H with a print >= s, or yes_bid high >= s
  through / queue = inf: a print > s or yes_bid high >= s in a candle wholly after posting, or a print >= s + 2c in the
                      straddling candle (the level was traded through: full fill whatever the queue)
  fills count up to the end of the hour that contains the cancel time (the bot learns of the first close at its next poll)
  exact (trade prints, /markets/trades): lab.kalshi.strategies.r2_infra_timestamp_fill_audit_fill.fill_from_prints with
                      queue_ahead 0 (touch) or 1e12 (through), live from H+120 s to the exact cancel time; size N.
Size-weighted (counterfactual): our order was not in the real book, so a print at or through s proves only its own
volume: filled fraction = min(1, contracts printed at >= s (queue 0) or > s (queue inf) after posting / N), exact from
/markets/trades where audited, else the volume of the hourly candles whose print high reached the level.

Split per group: events ordered by last close; discovery = first 70%, validation = last 30% (nothing is chosen on
discovery; both are reported, and the full sample, since the rule is frozen).
Usage: .venv/bin/python -m lab.kalshi.strategies.r3_frozen_c1_other_mention_formats [run|audit_list|fetch_trades N|diag|result]
  (result writes scores.json, diagnostics.json and result_auto.json; result.json = result_auto.json + the written notes)"""
from __future__ import annotations
import json, math, random, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_data import load_markets, events_of, OUT, ts
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_fill import fill_from_prints

EPS = 1e-9
STAKE = 5.0
SPLIT = 0.7
POST_DELAY = 120
GROUP_SERIES = {
    "sports": ("KXMLBMENTION", "KXNBAMENTION", "KXNHLMENTION", "KXWNBAMENTION"),
    "trump_accum": ("KXTRUMPSAYMONTH", "KXTRUMPSAYCOUNTRY", "KXTRUMPSAYCOMPANY", "KXTRUMPSAYNICKNAME", "KXTRUMPSAY"),
    "post_ladder": ("KXTRUTHSOCIAL", "KXTRUTHSOCIALD", "KXELONTWEETS"),
}


def load_candles() -> dict:
    C = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l)
        C[x["t"]] = sorted(x["c"], key=lambda r: r[0])
    return C


def load_trades() -> dict:
    f = OUT / "trades.jsonl"
    T = {}
    if f.exists():
        for l in f.open():
            x = json.loads(l)
            T[x["t"]] = x
    return T


def ceil_h(t: int) -> int:
    return ((t + 3599) // 3600) * 3600


def build_orders(mk, E, C, cancel_at: dict | None = None, window: str = "B") -> list[dict]:
    """One C1 order (and its mirror) per market with candles, at the first eligible hour.
    cancel_at: {event: ts} extra cancel time (variant V_sched: the game's scheduled start); events missing from it are
    skipped. window 'B': fills count to the end of the hour containing the cancel time (conservative: up to 1 h of
    activity after the cancel trigger); 'A': only candles ending at or before the cancel time."""
    out = []
    for m in mk:
        cs = C.get(m["t"])
        ev = E[m["e"]]
        if cs is None or not ev["complete"]:
            continue
        stop = min(ev["first_close"], m["close"])
        if cancel_at is not None:
            if not cancel_at.get(m["e"]):
                continue
            stop = min(stop, cancel_at[m["e"]])
        got = None
        for r in cs:
            H = r[0]
            if H % 3600 or H < m["open"] + 3600 or H >= stop:
                continue
            ask, bid = r[1], r[2]
            if ask is None or bid is None or not (bid > 0 and ask < 1 and ask - bid >= 0.02 - EPS):
                continue
            got = r; break
        if not got:
            continue
        H, ask, bid = got[0], got[1], got[2]
        s = round(ask - 0.01, 2); b = round(bid + 0.01, 2)
        stop_h = ceil_h(stop) if window == "B" else stop
        after = [r for r in cs if H < r[0] <= stop_h]
        yes = m["result"] == "yes"
        o = {"t": m["t"], "e": m["e"], "series": m["series"], "group": m["group"], "H": H, "ask": ask, "bid": bid, "s": s, "b": b,
             "px": round(1 - s, 2), "N": max(1, math.floor(STAKE / (1 - s))), "yes": yes, "stop": stop, "stop_h": stop_h,
             "ev_last_close": ev["last_close"], "spread": round(ask - bid, 2), "hours_live": round((stop - H) / 3600, 2)}
        # --- sell-YES at s (C1) ---
        tch = thr = None; maxhi = None; fill_vol = None; vt_proxy = vth_proxy = 0.0
        for r in after:
            T, px_hi, bid_hi = r[0], r[8], r[6]
            straddle = T - 3600 < H + POST_DELAY
            if px_hi is not None:
                maxhi = px_hi if maxhi is None else max(maxhi, px_hi)
            hit_touch = (px_hi is not None and px_hi >= s - EPS) or (bid_hi is not None and bid_hi > 0 and bid_hi >= s - EPS)
            if straddle:
                hit_thr = px_hi is not None and px_hi >= s + 0.02 - EPS
            else:
                hit_thr = (px_hi is not None and px_hi > s + EPS) or (bid_hi is not None and bid_hi > 0 and bid_hi >= s - EPS)
            if hit_touch and tch is None:
                tch = T; fill_vol = r[9]
            if hit_thr and thr is None:
                thr = T
            # counterfactual size: a taker who paid >= s after we posted would have traded with us first, up to the
            # taker's size (our order was NOT in the real book, so a print 'through' s proves only its own volume)
            if px_hi is not None and px_hi >= s - EPS:
                vt_proxy += r[9] or 0
            if px_hi is not None and ((not straddle and px_hi > s + EPS) or (straddle and px_hi >= s + 0.02 - EPS)):
                vth_proxy += r[9] or 0
        o.update({"fill_touch_ts": tch, "fill_thr_ts": thr, "max_px_after": maxhi, "fill_candle_vol": fill_vol,
                  "marginal": tch is not None and thr is None, "vol_touch_proxy": round(vt_proxy, 2), "vol_thr_proxy": round(vth_proxy, 2)})
        o["ret_no"] = ((0.0 if yes else 1.0) - o["px"]) / o["px"]
        # --- mirror: buy-YES at b ---
        mt = mth = None; mv = 0.0
        for r in after:
            T, px_lo, ask_lo = r[0], r[7], r[3]
            straddle = T - 3600 < H + POST_DELAY
            ht = (px_lo is not None and px_lo <= b + EPS) or (ask_lo is not None and 0 < ask_lo <= b + EPS)
            hth = (px_lo is not None and px_lo <= b - 0.02 + EPS) if straddle else \
                  ((px_lo is not None and px_lo < b - EPS) or (ask_lo is not None and 0 < ask_lo <= b + EPS))
            if ht and mt is None:
                mt = T
            if hth and mth is None:
                mth = T
            if px_lo is not None and px_lo <= b + EPS:
                mv += r[9] or 0
        o.update({"mirror_touch_ts": mt, "mirror_thr_ts": mth, "ret_yes": ((1.0 if yes else 0.0) - b) / b,
                  "N_yes": max(1, math.floor(STAKE / b)), "mirror_vol_proxy": round(mv, 2)})
        out.append(o)
    return out


def split_parts(orders) -> dict:
    """Per group: events by last close, first 70% discovery."""
    part = {}
    by_g = defaultdict(dict)
    for o in orders:
        by_g[o["group"]][o["e"]] = o["ev_last_close"]
    for g, ev in by_g.items():
        order = sorted(ev, key=lambda e: (ev[e], e)); cut = int(len(order) * SPLIT)
        for i, e in enumerate(order):
            part[e] = "disc" if i < cut else "val"
    return part


def wstats(rows: list[dict]) -> dict:
    """rows: {e, ret, w, won, px, ts}. Weighted mean per $; cluster-robust t (by event) for the pooled weighted mean;
    calib-style t of event means; mean w/o 3 best (by ret); halves by fill/post time."""
    rows = [r for r in rows if r["w"] > 0]
    if not rows:
        return {"n": 0}
    W = sum(r["w"] for r in rows); mu = sum(r["w"] * r["ret"] for r in rows) / W
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r)
    ne = len(ev)
    sc = [sum(r["w"] * (r["ret"] - mu) for r in v) for v in ev.values()]
    se = math.sqrt(sum(x * x for x in sc) * ne / max(ne - 1, 1)) / W
    em = [sum(r["w"] * r["ret"] for r in v) / sum(r["w"] for r in v) for v in ev.values()]
    t_em = st.mean(em) / (st.pstdev(em) / math.sqrt(ne)) if ne > 2 and st.pstdev(em) > 0 else None
    rs = sorted(rows, key=lambda r: -r["ret"])[3:]
    wo3 = sum(r["w"] * r["ret"] for r in rs) / sum(r["w"] for r in rs) if rs else None
    srt = sorted(rows, key=lambda r: r["ts"]); h = len(srt) // 2
    hm = [sum(r["w"] * r["ret"] for r in p) / sum(r["w"] for r in p) if p else None for p in (srt[:h], srt[h:])]
    return {"n": len(rows), "events": ne, "weight": round(W, 2), "win": round(sum(r["won"] for r in rows) / len(rows), 3),
            "avg_px": round(st.mean(r["px"] for r in rows), 3), "ret_per_dollar": round(mu, 4), "t": round(mu / se, 2) if se > 0 else None,
            "t_event_means": round(t_em, 2) if t_em is not None else None, "ret_wo3": round(wo3, 4) if wo3 is not None else None,
            "half1": round(hm[0], 4) if hm[0] is not None else None, "half2": round(hm[1], 4) if hm[1] is not None else None}


def exact_fill(o: dict, tr: dict, queue: float) -> dict:
    prints = []
    for t_, p, c, sd in tr["trades"]:
        prints.append((ts(t_), p, sd, c))
    prints.sort()
    return fill_from_prints("no", o["s"], o["H"] + POST_DELAY, o["stop"] - 1e-6, prints, size=o["N"], queue_ahead=queue)


def exact_vol(o: dict, tr: dict) -> tuple[float, float]:
    """Contracts printed at >= s and > s from posting (H + 120 s) to the exact cancel time (counterfactual fill sizes)."""
    post = o["H"] + POST_DELAY; vt = vth = 0.0
    for t_, p, c, sd in tr["trades"]:
        tt = ts(t_)
        if p is None or not (post <= tt < o["stop"]):
            continue
        if p >= o["s"] - EPS:
            vt += c
        if p > o["s"] + EPS:
            vth += c
    return vt, vth


def score(orders, T, part) -> dict:
    """Every fill model x part x group.
    *_equal: an order counts once if it fills at all; *_size: weighted by the counterfactual filled fraction
    min(1, contracts printed at >= s (touch, queue 0) or > s (through, queue inf) after posting / N), exact from
    /markets/trades where audited (every marginal live fill + a random 30 of the others), else the candle proxy
    (volume of the hourly candles whose print high reached the level; checked on the audited random sample)."""
    res = {}
    groups = sorted({o["group"] for o in orders}) + ["ALL"]
    for g in groups:
        for p in ("disc", "val", "all"):
            sel = [o for o in orders if (g == "ALL" or o["group"] == g) and (p == "all" or part[o["e"]] == p)]
            M = defaultdict(list)
            for o in sel:
                base = {"e": o["e"], "ret": o["ret_no"], "won": not o["yes"], "px": o["px"]}
                x = T.get(o["t"])
                if o["fill_touch_ts"] is not None:
                    M["candle_touch_equal"].append({**base, "w": 1.0, "ts": o["fill_touch_ts"]})
                if o["fill_thr_ts"] is not None:
                    M["candle_through_equal"].append({**base, "w": 1.0, "ts": o["fill_thr_ts"]})
                if x is not None:
                    vt, vth = exact_vol(o, x)
                    tch, thr = vt > 0, vth > 0
                elif o["marginal"]:
                    M["_unaudited_marginal"].append({**base, "w": 1.0, "ts": o["fill_touch_ts"]})
                    vt, vth = o["vol_touch_proxy"], 0.0
                    tch, thr = o["fill_touch_ts"] is not None, False
                else:
                    vt, vth = o["vol_touch_proxy"], o["vol_thr_proxy"]
                    tch, thr = o["fill_touch_ts"] is not None, o["fill_thr_ts"] is not None
                t0 = o["fill_touch_ts"] or o["H"]
                if tch:
                    M["touch_equal"].append({**base, "w": 1.0, "ts": t0})
                    M["touch_size"].append({**base, "w": min(1.0, vt / o["N"]), "ts": t0})
                if thr:
                    M["through_equal"].append({**base, "w": 1.0, "ts": t0})
                    M["through_size"].append({**base, "w": min(1.0, vth / o["N"]), "ts": t0})
                M["posted_all_filled"].append({**base, "w": 1.0, "ts": o["H"]})
                mb = {"e": o["e"], "ret": o["ret_yes"], "won": o["yes"], "px": o["b"]}
                if o["mirror_touch_ts"] is not None:
                    M["mirror_touch_equal"].append({**mb, "w": 1.0, "ts": o["mirror_touch_ts"]})
                    M["mirror_touch_size"].append({**mb, "w": min(1.0, o["mirror_vol_proxy"] / o["N_yes"]), "ts": o["mirror_touch_ts"]})
                if o["mirror_thr_ts"] is not None:
                    M["mirror_through_equal"].append({**mb, "w": 1.0, "ts": o["mirror_thr_ts"]})
            r = {k: wstats(v) for k, v in M.items()}
            r["posted"] = len(sel)
            r["posted_events"] = len({o["e"] for o in sel})
            res[f"{g}|{p}"] = r
    return res


def audit_list(orders, n_random=30, seed=20261009) -> list:
    """Every marginal (one-tick / at-level) live-tier fill, plus a seeded random sample of other live-tier fills."""
    from lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_data import load_markets as lm
    tier = {m["t"]: m["tier"] for m in lm()}
    live = [o for o in orders if tier[o["t"]] == "live" and o["fill_touch_ts"] is not None]
    marg = [o for o in live if o["marginal"]]
    other = [o for o in live if not o["marginal"]]
    rnd = random.Random(seed).sample(other, min(n_random, len(other)))
    return [(o["t"], o["H"], o["stop"], "marginal") for o in marg] + [(o["t"], o["H"], o["stop"], "random_certain") for o in rnd]


def main():
    mk = load_markets(); E = events_of(mk); C = load_candles(); T = load_trades()
    orders = build_orders(mk, E, C)
    part = split_parts(orders)
    for o in orders:
        o["part"] = part[o["e"]]
    with (OUT / "orders.jsonl").open("w") as f:
        for o in orders:
            f.write(json.dumps(o) + "\n")
    res = score(orders, T, part)
    sf = OUT / "sports_starts.json"
    if sf.exists():
        starts = {e: v["start"] for e, v in json.loads(sf.read_text()).items()}
        sp = [m for m in mk if m["group"] == "sports"]
        for w in ("B", "A"):
            ov = build_orders(sp, E, C, cancel_at=starts, window=w)
            pv = split_parts(ov)
            for o in ov:
                o["part"] = pv[o["e"]]
            if w == "B":
                with (OUT / "orders_sports_vsched.jsonl").open("w") as f:
                    for o in ov:
                        f.write(json.dumps(o) + "\n")
            for k, v in score(ov, {}, pv).items():
                if k.startswith("sports|"):
                    res[f"VSCHED_{w}|{k}"] = v
    return orders, res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    orders, res = main()
    if cmd in ("audit_list", "fetch_trades"):
        a = audit_list(orders)
        print(len(a), "to audit;", sum(1 for x in a if x[3] == "marginal"), "marginal")
        (OUT / "audit_list.json").write_text(json.dumps(a, indent=0))
        if cmd == "fetch_trades":
            from lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_data import trades
            trades([(t, H, stop) for t, H, stop, _ in a], max_calls=int(sys.argv[2]) if len(sys.argv) > 2 else 75)
    else:
        for k, v in res.items():
            print("==", k, "posted", v["posted"], "events", v["posted_events"])
            for kk in ("candle_touch_equal", "candle_through_equal", "touch_equal", "touch_size", "through_equal", "through_size",
                       "_unaudited_marginal", "posted_all_filled", "mirror_touch_equal", "mirror_touch_size", "mirror_through_equal"):
                x = v.get(kk)
                if x and x.get("n"):
                    print(f"   {kk:22s} n={x['n']:4d} ev={x['events']:3d} w={x['weight']:7.1f} win={x['win']:.3f} px={x['avg_px']:.3f} "
                          f"ret={x['ret_per_dollar']:+.4f} t={x['t']} tEM={x['t_event_means']} wo3={x['ret_wo3']} h={x['half1']}/{x['half2']}")


def diagnostics(orders, T) -> dict:
    """Per series / per event tables, adverse-selection fill rates, trigger-functional split, capacity."""
    mk = load_markets(); E = events_of(mk)
    by_e = defaultdict(list)
    for m in mk:
        by_e[m["e"]].append(m)

    def functional(e):
        # did any word of the event close before the event's batch (modal) close? (ex post; diagnostic only)
        x = by_e[e]; cl = defaultdict(int)
        for m in x:
            cl[m["close"]] += 1
        modal = max(cl, key=lambda k: (cl[k], k))
        return any(m["close"] < modal - 300 and m["result"] == "yes" for m in x)

    def through_size_rows(sel):
        rows = []
        for o in sel:
            base = {"e": o["e"], "ret": o["ret_no"], "won": not o["yes"], "px": o["px"]}
            x = T.get(o["t"])
            if x is not None:
                vth = exact_vol(o, x)[1]
            elif o["marginal"] or o["fill_thr_ts"] is None:
                continue
            else:
                vth = o["vol_thr_proxy"]
            if vth > 0:
                rows.append({**base, "w": min(1.0, vth / o["N"]), "ts": o["fill_touch_ts"] or o["H"]})
        return rows

    out = {"by_series": {}, "by_event": {}, "fill_rate_by_outcome": {}, "trigger_split": {}, "capacity": {}}
    for s in sorted({o["series"] for o in orders}):
        sel = [o for o in orders if o["series"] == s]
        out["by_series"][s] = {"posted": len(sel), "events": len({o["e"] for o in sel}), "through_size": wstats(through_size_rows(sel)),
                               "mirror_touch": wstats([{"e": o["e"], "ret": o["ret_yes"], "won": o["yes"], "px": o["b"], "w": 1.0, "ts": o["mirror_touch_ts"]}
                                                       for o in sel if o["mirror_touch_ts"] is not None])}
    for e in sorted({o["e"] for o in orders}):
        sel = [o for o in orders if o["e"] == e]
        r = wstats(through_size_rows(sel))
        out["by_event"][e] = {"group": sel[0]["group"], "posted": len(sel), "filled": r.get("n", 0), "ret": r.get("ret_per_dollar"),
                              "median_spread": st.median(o["spread"] for o in sel), "hours_live_median": st.median(o["hours_live"] for o in sel)}
    for g in sorted({o["group"] for o in orders}):
        sel = [o for o in orders if o["group"] == g]
        d = {}
        for y in (True, False):
            xs = [o for o in sel if o["yes"] == y]
            d["outcome_" + ("YES" if y else "NO")] = {"posted": len(xs), "c1_fill_rate_touch": round(sum(1 for o in xs if o["fill_touch_ts"]) / max(1, len(xs)), 3),
                                                      "c1_fill_rate_through": round(sum(1 for o in xs if o["fill_thr_ts"]) / max(1, len(xs)), 3),
                                                      "mirror_fill_rate_touch": round(sum(1 for o in xs if o["mirror_touch_ts"]) / max(1, len(xs)), 3)}
        out["fill_rate_by_outcome"][g] = d
        if g == "trump_accum":
            for lab_, f in (("trigger_fired_pre_batch", True), ("no_early_close_rested_to_end", False)):
                ss = [o for o in sel if functional(o["e"]) == f]
                out["trigger_split"][lab_] = {"events": len({o["e"] for o in ss}), "posted": len(ss), "through_size": wstats(through_size_rows(ss))}
        fills = [o for o in sel if o["fill_touch_ts"] is not None]
        days = (max(o["H"] for o in sel) - min(o["H"] for o in sel)) / 86400 if sel else 0
        aud = [o for o in fills if o["t"] in T]
        fr = [min(1.0, exact_vol(o, T[o["t"]])[0] / o["N"]) for o in aud]
        out["capacity"][g] = {"median_fill_candle_volume": st.median(o["fill_candle_vol"] or 0 for o in fills) if fills else None,
                              "median_order_contracts_N": st.median(o["N"] for o in sel), "fills_per_day": round(len(fills) / days, 2) if days else None,
                              "audited_orders": len(aud), "audited_mean_fill_fraction_queue0": round(st.mean(fr), 3) if fr else None,
                              "median_decision_spread": st.median(o["spread"] for o in sel)}
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "diag":
    orders = [json.loads(l) for l in (OUT / "orders.jsonl").open()]
    d = diagnostics(orders, load_trades())
    (OUT / "diagnostics.json").write_text(json.dumps(d, indent=1))
    for k in ("by_series", "fill_rate_by_outcome", "trigger_split", "capacity"):
        print("==", k)
        for kk, v in d[k].items():
            print("  ", kk, json.dumps(v)[:400])
    print("== by_event")
    for kk, v in d["by_event"].items():
        if v["group"] != "sports":
            print("  ", kk, v)


def write_result() -> dict:
    """scores.json (every cell) + result.json (the structured answer)."""
    orders, res = main()
    T = load_trades()
    d = diagnostics(orders, T)
    (OUT / "scores.json").write_text(json.dumps(res, indent=1))
    (OUT / "diagnostics.json").write_text(json.dumps(d, indent=1))
    cap = d["capacity"]
    names = {"sports": "C1 frozen, sports announcer mentions (MLB/NBA/NHL/WNBA, archive; literal cancel trigger)",
             "trump_accum": "C1 frozen, Trump accumulators (KXTRUMPSAY weekly, MONTH, COMPANY, COUNTRY)",
             "post_ladder": "C1 frozen, post-count ladders (KXTRUTHSOCIAL weekly, KXTRUTHSOCIALD daily)"}
    val = []
    for g in ("sports", "trump_accum", "post_ladder"):
        r = res[f"{g}|val"]; v = r["through_size"]
        val.append({"rule": names[g] + " - validation, through-only (queue inf), counterfactual size-weighted", "n": v["n"], "events": v["events"],
                    "win": v["win"], "avg_px": v["avg_px"], "ret_per_dollar": v["ret_per_dollar"], "t": v["t"], "ret_wo3": v["ret_wo3"],
                    "half1": v["half1"], "half2": v["half2"], "median_capacity_contracts": cap[g]["median_fill_candle_volume"],
                    "trades_per_day": cap[g]["fills_per_day"], "posted": r["posted"], "posted_events": r["posted_events"],
                    "equal_dollar_touch_ret": r["touch_equal"].get("ret_per_dollar"), "size_weighted_touch_ret": r["touch_size"].get("ret_per_dollar"),
                    "through_equal_ret": r["through_equal"].get("ret_per_dollar"), "mirror_touch_equal_ret": r["mirror_touch_equal"].get("ret_per_dollar"),
                    "median_order_contracts": cap[g]["median_order_contracts_N"], "audited_fill_fraction_queue0": cap[g]["audited_mean_fill_fraction_queue0"]})
    full = {}
    for g in ("sports", "trump_accum", "post_ladder", "ALL"):
        r = res[f"{g}|all"]
        full[g] = {k: r[k] for k in ("touch_equal", "touch_size", "through_equal", "through_size", "mirror_touch_equal", "mirror_touch_size",
                                     "mirror_through_equal", "posted_all_filled") if k in r}
        full[g]["posted"] = r["posted"]; full[g]["posted_events"] = r["posted_events"]
    vs = {w: {k: res[f"VSCHED_{w}|sports|all"][k] for k in ("touch_equal", "touch_size", "through_size", "mirror_touch_equal")} for w in ("B", "A")}
    thr5 = {g: full[g]["through_size"]["ret_per_dollar"] for g in ("sports", "trump_accum", "post_ladder")}
    thr5v = {g: res[f"{g}|val"]["through_size"]["ret_per_dollar"] for g in ("sports", "trump_accum", "post_ladder")}
    mir = full["ALL"]["mirror_touch_equal"]["ret_per_dollar"]
    kill = {"A_through_size_below_5pct_in_2_of_3_groups": {"full_sample": thr5, "validation": thr5v,
                                                          "groups_below_full": sum(v < 0.05 for v in thr5.values()),
                                                          "groups_below_val": sum(v < 0.05 for v in thr5v.values()), "triggered": False},
            "B_mirror_not_negative": {"pooled_mirror_touch_equal_full": mir, "t": full["ALL"]["mirror_touch_equal"]["t"],
                                      "pooled_mirror_through_equal_full": full["ALL"]["mirror_through_equal"]["ret_per_dollar"],
                                      "by_group_touch_equal": {g: full[g]["mirror_touch_equal"]["ret_per_dollar"] for g in ("sports", "trump_accum", "post_ladder")},
                                      "triggered": mir >= 0},
            "verdict_rule": "kill if A or B (declared in the round-3 test plan; B operationalised before scoring as the pooled touch / equal-$ mirror mean >= 0)"}
    disc = res["post_ladder|disc"]["through_size"]
    out = {
        "name": "r3_frozen_c1_other_mention_formats",
        "hypothesis": "If narrative YES demand at listing is a mechanism (not a 20-day artefact of round 2's political mention markets), the frozen round-2 C1 rule - at the first top-of-hour >= open + 1 h with no market of the event closed and spread >= 2c, rest a sell-YES (= buy NO) at yes_ask - 1c for $5, cancel when any market of the event closes, hold to settlement, no maker fee - earns >= +10% per $ on mention-type series round 2 never saw: (1) sports announcer mentions, (2) Trump accumulators, (3) post-count ladders. Mechanism check: the mirror (rest a buy-YES at yes_bid + 1c) must lose.",
        "why_edge_could_exist": "Mention / count markets are fee_type 'quadratic' (makers pay nothing), listed by Kalshi with wide placeholder books and traded by retail who buy YES on narrative. A patient home-Mac maker posting once per hour at listing needs no speed; the edge would be the spread plus the YES premium, if the fills are not adversely selected. The counter-hypothesis (adverse selection): a resting offer is lifted mainly when YES becomes likely, by takers watching the speech, the game or the post count in real time - and in accumulators, ladders and sports the order rests for days through continuous informed flow, unlike round 2's single-appearance markets.",
        "data_used": "Kalshi public API, 175 calls (budget 200; calls.log): settled lists (live tier + /historical) for 16 series; hourly candles (yes_ask/yes_bid OHLC, trade-price high/low, volume) for 630 live-tier markets of 30 complete events via 16 batched /markets/candlesticks calls (window from each market's own open to the hour after the event's first close, no 96 h truncation), and for 56 archive sports markets (one seeded market per sampled event, /historical/markets/{t}/candlesticks); /markets/trades prints for all 35 marginal (at-level / one-tick) live fills and a seeded random 30 of the other live fills (71 calls). Read-only reuse of round 2's cached KXTRUMPSAY live list and its empty sports lists. ESPN public scoreboards for the scheduled start of 51 of the 54 sampled games (variant V_sched). Series facts: no sports announcer market settled after 2026-08-02 (MLB/WNBA), 2026-06 (NBA/NHL), 2026-04 (NFL); KXNASCARMENTION none; KXELONTWEETS none since 2025-04 (KXTRUTHSOCIALD, 7 days, used instead); KXTRUMPSAYNICKNAME's only complete live event straddles the archive cutoff (excluded with KXTRUMPSAY-26AUG03).",
        "code_path": "lab/kalshi/strategies/r3_frozen_c1_other_mention_formats.py (orders, fill models, scoring, diagnostics, result), _data.py (lists, plan, candles, trades), _api.py (counted cached Kalshi GET), _espn.py (scheduled starts)",
        "variants_examined": 8,
        "discovery_best": {"rule": "post-count ladders, discovery 70% (nothing fitted: the 3 frozen cells' discovery parts are reported only for completeness), through-only size-weighted", "n": disc["n"], "ret_per_dollar": disc["ret_per_dollar"], "t": disc["t"], "events": disc["events"]},
        "validation": val,
        "full_sample_frozen_rule": full,
        "sports_variant_cancel_at_scheduled_start": vs,
        "kill_checks": kill,
        "diagnostics": {"fill_rate_by_outcome": d["fill_rate_by_outcome"], "trump_trigger_split": d["trigger_split"], "capacity": cap,
                        "by_series_through_size": {s: v["through_size"] for s, v in d["by_series"].items()}},
        "verdict": "dead",
        "kalshi_calls_used": 175,
    }
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "result":
    out = write_result()
    (OUT / "result_auto.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("validation", "kill_checks", "discovery_best")}, indent=1)[:6000])
