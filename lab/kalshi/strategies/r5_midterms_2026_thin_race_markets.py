"""r5_midterms_2026_thin_race_markets: do thin per-race election markets keep stale quotes on election night?

KILL TEST (descriptive, archive, the market's own path as the trigger; the outcome is used only to count the volume
that traded on the eventual winner):
  2024 general (House 23 races, Senate 19, Governor 9, six lean presidential states) and the 2025 off-year night
  (VA/NJ governor, VA Lt-Gov/AG, VA House of Delegates, NJ Assembly, Charlotte, Seattle, Detroit) plus four AP-call
  timing ladders (KXAPCALL*). 1-minute candles over [17:00 ET, +26 h] of election night (data module).
  Per market (YES-book quotes; NO-side prices are 1 - yes_bid):
    ref      = mid at 17:00 ET (last candle at or before; else first candle)
    t0 (Q)   = first minute the leader's committed quote moved >= 25c: bid >= ref+0.25 (leader YES) or
               ask <= ref-0.25 (leader NO).  t0 (M) = first minute |mid - ref| >= 0.25 (mid trigger, noisier).
    t_ask95  = first minute >= t0 with leader ask >= 0.95 (no offer on the leader below 0.95 left: the taker window
               is closed).  t_mid95 / t_bid95 likewise for the leader mid / bid (full repricing).
    t_last85 = last minute in the window with a leader offer at <= 0.85.
    vol<=.85 = contracts traded at <= 0.85 on the eventual winner after t0: lower bound = minutes whose whole trade
               range is <= 0.85, upper bound = minutes whose low is <= 0.85.
  Stop rule (task): median repricing (t_ask95 - t0) under 3 min -> stop. It is computed for both triggers.
AP-call ladders: the call time is read from the ladder itself (first minute the earliest YES-resolving strike bid
>= 0.90); the matching race market's leader offer at that minute measures staleness after a public call.
Rule simulations (taker, decide at t, fill at the quote one minute later, quote <= 30 min old, fee ceil per order)
are run on discovery (first 70% of events by election night then close time) for at most 3 candidates.
Usage: python -m lab.kalshi.strategies.r5_midterms_2026_thin_race_markets"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_midterms_2026_thin_race_markets_api import OUT, used

ET = ZoneInfo("America/New_York")
MOVE = 0.25
WIN_MIN = 26 * 60
AP_PAIR = {"KXAPCALLVAGOV-25NOV04-800": "GOVPARTYVA-25", "KXAPCALLNJGOV-25NOV04-930": "GOVPARTYNJ-25",
           "KXAPCALLVAAG-25NOV04-1030": "KXATTYGENVA-25", "KXAPCALLSNYC-25NOV04-1000": None}


def et(t: int | None) -> str | None:
    return dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M") if t else None


def load():
    picked = {m["t"]: m for m in json.loads((OUT / "picked.json").read_text())}
    meta = {json.loads(l)["t"]: json.loads(l) for l in (OUT / "markets.jsonl").open()}
    cand = {json.loads(l)["t"]: json.loads(l)["c"] for l in (OUT / "candles.jsonl").open()}
    return picked, meta, cand


def series_q(c: list[list], night: int):
    """Carry-forward quote path: list of (ts, ask, bid, vol, p_hi, p_lo) for candles in the window, ask/bid cleaned."""
    out = []
    for r in c:
        ts, ask, bid, vol, phi, plo = r[0], r[1], r[2], r[5], r[7], r[8]
        ask = 1.0 if ask is None or ask <= 0 else ask
        bid = 0.0 if bid is None else bid
        out.append((ts, ask, bid, vol, phi, plo))
    return out


def ref_mid(q, t_ref: int):
    prev = [x for x in q if x[0] <= t_ref]
    x = prev[-1] if prev else (q[0] if q else None)
    return None if x is None else (x[1] + x[2]) / 2


def leader_px(x, lead_yes: bool):
    """(ask, bid, mid) of the leader side for quote row x."""
    ask, bid = x[1], x[2]
    if lead_yes:
        return ask, bid, (ask + bid) / 2
    return 1 - bid, 1 - ask, 1 - (ask + bid) / 2


def metrics(t: str, c: list[list], night: int, won_yes: bool) -> dict:
    q = series_q(c, night)
    t_ref = night - 3600
    ref = ref_mid(q, t_ref)
    res = {"t": t, "ref": ref, "n_candles": len(q), "vol_window": sum(x[3] for x in q if x[0] > t_ref)}
    if ref is None:
        return res
    for trig in ("Q", "M"):
        t0 = None; lead_yes = None
        for x in q:
            if x[0] <= t_ref:
                continue
            ask, bid = x[1], x[2]; mid = (ask + bid) / 2
            if trig == "Q":
                if bid >= ref + MOVE:
                    t0, lead_yes = x[0], True; break
                if ask <= ref - MOVE:
                    t0, lead_yes = x[0], False; break
            else:
                if abs(mid - ref) >= MOVE:
                    t0, lead_yes = x[0], mid > ref; break
        d = {"t0": t0}
        if t0 is not None:
            d["lead_yes"] = lead_yes; d["leader_won"] = lead_yes == won_yes
            after = [x for x in q if x[0] >= t0]
            def first(f):
                for x in after:
                    if f(leader_px(x, lead_yes)):
                        return x[0]
                return None
            d["t_ask95"] = first(lambda p: p[0] >= 0.95)
            d["t_mid95"] = first(lambda p: p[2] >= 0.95)
            d["t_bid95"] = first(lambda p: p[1] >= 0.95)
            cheap = [x[0] for x in after if leader_px(x, lead_yes)[0] <= 0.85]
            # last minute a leader offer <= 0.85 was visible: the offer is alive until the next candle changes it
            d["t_last85"] = None
            if cheap:
                lc = cheap[-1]; nxt = [x[0] for x in after if x[0] > lc]
                d["t_last85"] = (nxt[0] if nxt else min(night + WIN_MIN * 60, lc + 60))
            d["ask_at_t0"] = leader_px([x for x in after][0], lead_yes)[0]
            # volume on the eventual winner at <= 0.85 after t0
            lo = hi = 0.0
            for x in after:
                v, phi, plo = x[3], x[4], x[5]
                if not v or phi is None:
                    continue
                if won_yes:
                    if phi <= 0.85: lo += v
                    if plo <= 0.85: hi += v
                else:
                    if plo >= 0.15: lo += v
                    if phi >= 0.15: hi += v
            d["vol85_lo"], d["vol85_hi"] = lo, hi
            for k in ("t_ask95", "t_mid95", "t_bid95", "t_last85"):
                d[k.replace("t_", "lag_")] = None if d[k] is None else round((d[k] - t0) / 60, 1)
        res[trig] = d
    return res


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def summarize_kill(rows: list[dict]) -> dict:
    out = {}
    for trig in ("Q", "M"):
        trg = [r for r in rows if r.get(trig, {}).get("t0") is not None]
        lag_ask = [r[trig]["lag_ask95"] for r in trg]
        cens = sum(1 for x in lag_ask if x is None)
        # censored (never reached 0.95 inside the window) counted as longer than any observed lag
        big = 10 ** 6
        lag_sorted = sorted((x if x is not None else big) for x in lag_ask)
        m = lag_sorted[len(lag_sorted) // 2] if lag_sorted else None
        out[trig] = {"markets_triggered": len(trg), "of": len(rows), "censored_no95": cens,
                     "median_lag_ask95_min": (None if m is None else (">window" if m == big else m)),
                     "median_lag_mid95_min": med([r[trig]["lag_mid95"] for r in trg]),
                     "median_lag_bid95_min": med([r[trig]["lag_bid95"] for r in trg]),
                     "median_lag_last85_min": med([r[trig]["lag_last85"] for r in trg]),
                     "share_lag_ask95_lt3": (sum(1 for x in lag_ask if x is not None and x < 3) / len(lag_ask)) if lag_ask else None,
                     "leader_at_t0_won": sum(r[trig]["leader_won"] for r in trg),
                     "vol85_winner_after_t0_lo_median": med([r[trig]["vol85_lo"] for r in trg]),
                     "vol85_winner_after_t0_hi_median": med([r[trig]["vol85_hi"] for r in trg]),
                     "vol85_winner_after_t0_lo_total": sum(r[trig]["vol85_lo"] for r in trg),
                     "vol85_winner_after_t0_hi_total": sum(r[trig]["vol85_hi"] for r in trg)}
    return out


def main() -> None:
    picked, meta, cand = load()
    rows = []
    for t, m in picked.items():
        if t not in cand:
            continue
        r = metrics(t, cand[t], m["night"], meta[t]["result"] == "yes")
        r.update({"cls": m["cls"], "role": m["role"], "night": m["night"], "vol_life": m["vol"], "result": meta[t]["result"],
                  "close": meta[t]["close"], "sub": meta[t]["sub"]})
        rows.append(r)
    (OUT / "per_market.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    races = [r for r in rows if r["role"] == "race"]
    print(f"markets with candles {len(rows)} (races {len(races)}); Kalshi calls used {used()}")
    print("\nPER RACE (Q trigger: committed quote crossed ref +-25c)")
    for r in sorted(races, key=lambda r: (r["night"], r["cls"], r["t"])):
        q = r.get("Q", {}); mq = r.get("M", {})
        print(f"  {r['t']:24s} {r['cls']:6s} ref {r['ref'] if r['ref'] is None else round(r['ref'], 2)!s:5s} "
              f"Q t0 {et(q.get('t0'))!s:11s} lead {'Y' if q.get('lead_yes') else ('N' if q.get('t0') else '-')} "
              f"won {q.get('leader_won')!s:5s} ask95 {q.get('lag_ask95')!s:6s} mid95 {q.get('lag_mid95')!s:6s} bid95 {q.get('lag_bid95')!s:6s} "
              f"last85 {q.get('lag_last85')!s:6s} v85 {q.get('vol85_lo', 0):.0f}-{q.get('vol85_hi', 0):.0f} | M t0 {et(mq.get('t0'))!s:11s} ask95 {mq.get('lag_ask95')!s:6s} "
              f"| vol_win {r['vol_window']:.0f}")
    kill = {"all": summarize_kill(races)}
    for grp in ("house", "senate", "gov", "pres", "y25"):
        kill[grp] = summarize_kill([r for r in races if r["cls"] == grp])
    print("\nKILL TEST SUMMARY")
    for g, v in kill.items():
        for trig, d in v.items():
            print(f"  {g:6s} {trig}: {json.dumps(d)}")
    # AP-call ladders
    ap = {}
    print("\nAP-CALL LADDERS")
    for t, race_e in AP_PAIR.items():
        if t not in cand:
            continue
        q = series_q(cand[t], picked[t]["night"])
        t_call = next((x[0] for x in q if x[2] >= 0.90), None)
        t_first = next((x[0] for x in q if x[2] >= 0.5), None)
        pre = [x for x in q if t_call and x[0] < t_call]
        d = {"t_call_est": t_call, "t_call_et": et(t_call), "ask_before": pre[-1][1] if pre else None,
             "t_first_bid50": et(t_first), "lag_to_ask_ge99": None}
        if t_call:
            a99 = next((x[0] for x in q if x[0] >= t_call and x[1] >= 0.99), None)
            d["lag_to_ask_ge99"] = None if a99 is None else (a99 - t_call) / 60
            # volume traded at <= 0.85 on YES after the call
            d["vol_le85_after_call"] = sum(x[3] for x in q if x[0] >= t_call and x[3] and x[5] is not None and x[5] <= 0.85)
        if race_e and t_call:
            rm = [k for k in cand if k.startswith(race_e + "-")]
            for k in rm:
                rq = series_q(cand[k], picked[k]["night"]); won = meta[k]["result"] == "yes"
                at = [x for x in rq if x[0] <= t_call]
                if at:
                    x = at[-1]
                    d[k] = {"winner_offer_at_call": round(leader_px(x, won)[0], 3), "winner_bid_at_call": round(leader_px(x, won)[1], 3)}
                    nxt = next((y[0] for y in rq if y[0] >= t_call and leader_px(y, won)[0] >= 0.95), None)
                    d[k]["lag_winner_ask95_after_call_min"] = None if nxt is None else (nxt - t_call) / 60
                    d[k]["vol_winner_le85_after_call"] = sum(y[3] for y in rq if y[0] >= t_call and y[3] and y[4] is not None and
                                                             ((y[5] <= 0.85) if won else (y[4] >= 0.15)))
        ap[t] = d
        print(f"  {t}: {json.dumps(d)}")
    res = {"kill": kill, "ap": ap, "n_markets": len(rows), "n_races": len(races)}
    (OUT / "kill_test.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__" and len(sys.argv) == 1:
    main()


# ================================================================ cross-book (sibling) staleness and rule simulation
FEE = 0.07


def fee_order(p: float, n: int) -> float:
    """Kalshi taker fee for an order of n contracts at p, rounded UP to the cent, per contract."""
    return math.ceil(round(FEE * p * (1 - p) * n * 100, 6)) / 100 / n


def qat(q, t: int, max_age: int = 30 * 60):
    """Last quote row at or before t if not older than max_age (protocol: skip quotes older than 30 min)."""
    prev = None
    for x in q:
        if x[0] <= t:
            prev = x
        else:
            break
    if prev is None or t - prev[0] > max_age:
        return None
    return prev


def pairs(picked, meta, cand):
    """2-book races: (event, A ticker, B ticker) with candles for both."""
    ev = defaultdict(list)
    for t, m in picked.items():
        if t in cand and m["role"] in ("race", "sibling"):
            ev[meta[t]["e"]].append(t)
    return {e: sorted(ts) for e, ts in ev.items() if len(ts) == 2}


def cross_book(picked, meta, cand, X: float, cap: float, delay: int = 60, n_contracts: int = 10):
    """Signal at minute t: book A's committed leader quote says decided (A yes_bid >= X -> A's candidate leads;
    A yes_ask <= 1-X -> A's candidate trails). Decide at t, fill at t+delay in the SIBLING book on the leader:
    buy NO on the trailer's YES book at 1 - yes_bid (or YES on the leader's book at yes_ask), whichever is cheaper,
    if <= cap. One trade per race (first fill). Returns trade rows."""
    out = []
    for e, (a, b) in pairs(picked, meta, cand).items():
        night = picked[a]["night"]
        qa, qb = series_q(cand[a], night), series_q(cand[b], night)
        res = {a: meta[a]["result"] == "yes", b: meta[b]["result"] == "yes"}
        times = sorted({x[0] for x in qa} | {x[0] for x in qb})
        done = False
        for t in times:
            if t < night - 3600 or done:
                continue
            for s1, s2, q1, q2 in ((a, b, qa, qb), (b, a, qb, qa)):
                x1 = qat(q1, t)
                if x1 is None:
                    continue
                lead_s1 = x1[2] >= X                 # s1's candidate leads (committed bid)
                trail_s1 = x1[1] <= 1 - X            # s1's candidate trails (committed offer to sell cheap)
                if not (lead_s1 or trail_s1):
                    continue
                x2 = qat(q2, t + delay)
                if x2 is None:
                    continue
                # leader's candidate is s1 (if lead) else s2 (2-candidate race). Buy the leader in book s2:
                if lead_s1:      # s1 wins -> s2 loses -> buy NO on s2 at 1 - bid(s2)
                    px = 1 - x2[2]; side = "NO"; win = not res[s2]
                else:            # s1 loses -> s2 wins -> buy YES on s2 at ask(s2)
                    px = x2[1]; side = "YES"; win = res[s2]
                if not (0.01 <= px <= cap):
                    continue
                f = fee_order(px, n_contracts)
                pnl = (1.0 if win else 0.0) - px - f
                out.append({"e": e, "night": night, "close": meta[s2]["close"], "t_sig": t, "book": s2, "side": side, "px": px,
                            "won": win, "ret": pnl / px, "sig_book": s1, "sig_bid": x1[2], "sig_ask": x1[1]})
                done = True; break
    return out


def leader_offer_rule(rows, picked, meta, cand, cap: float, trig: str = "Q", delay: int = 60, n_contracts: int = 10):
    """Own-book rule: at the first committed 25c move (t0), buy the leader at its offer one minute later if <= cap."""
    out = []
    for r in rows:
        d = r.get(trig) or {}
        if r["role"] != "race" or d.get("t0") is None:
            continue
        q = series_q(cand[r["t"]], r["night"]); x = qat(q, d["t0"] + delay)
        if x is None:
            continue
        px = leader_px(x, d["lead_yes"])[0]
        if not (0.01 <= px <= cap):
            continue
        win = d["leader_won"]; f = fee_order(px, n_contracts)
        out.append({"e": meta[r["t"]]["e"], "night": r["night"], "close": r["close"], "t_sig": d["t0"], "book": r["t"], "px": px,
                    "won": win, "ret": ((1.0 if win else 0.0) - px - f) / px})
    return out


def split(tr, events_order):
    cut = events_order[int(len(events_order) * 0.7)] if events_order else None
    disc = [x for x in tr if (x["night"], x["close"], x["e"]) < cut] if cut else tr
    val = [x for x in tr if (x["night"], x["close"], x["e"]) >= cut] if cut else []
    return disc, val


def stats(tr):
    if not tr:
        return {"n": 0}
    ev = defaultdict(list)
    for x in tr:
        ev[x["e"]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(len(em))) if len(em) > 2 and sd > 0 else float("nan")
    rs = sorted((x["ret"] for x in tr), reverse=True)
    srt = sorted(tr, key=lambda x: x["t_sig"]); h = len(srt) // 2
    return {"n": len(tr), "events": len(em), "win": sum(x["won"] for x in tr) / len(tr), "avg_px": st.mean(x["px"] for x in tr),
            "ret_per_dollar": st.mean(x["ret"] for x in tr), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(x["ret"] for x in srt[:h]) if h else float("nan"), "half2": st.mean(x["ret"] for x in srt[h:]) if srt[h:] else float("nan")}


def rules_main() -> dict:
    picked, meta, cand = load()
    rows = [json.loads(l) for l in (OUT / "per_market.jsonl").open()]
    evs = sorted({(picked[t]["night"], meta[t]["close"], meta[t]["e"]) for t in picked if t in cand and picked[t]["role"] in ("race", "sibling")})
    out = {"variants": {}, "n_event_order": len(evs)}
    # descriptive cross-book: every signal minute (not only the first), how often is the sibling offer <= 0.85 one minute later
    for X in (0.85, 0.90, 0.95):
        for cap in (0.85, 0.90):
            tr = cross_book(picked, meta, cand, X, cap)
            d, v = split(tr, evs)
            out["variants"][f"C1 cross-book X={X} cap={cap}"] = {"all": stats(tr), "disc": stats(d), "val_n": len(v), "trades": tr}
    for cap in (0.85, 0.95):
        for trig in ("Q", "M"):
            tr = leader_offer_rule(rows, picked, meta, cand, cap, trig)
            d, v = split(tr, evs)
            out["variants"][f"C2 own-book leader offer trig={trig} cap={cap}"] = {"all": stats(tr), "disc": stats(d), "val_n": len(v), "trades": tr}
    for k, v in out["variants"].items():
        print(f"  {k:44s} all {json.dumps({a: (round(b, 3) if isinstance(b, float) else b) for a, b in v['all'].items()})}")
        print(f"  {'':44s} disc {json.dumps({a: (round(b, 3) if isinstance(b, float) else b) for a, b in v['disc'].items()})} val_n {v['val_n']}")
    return out


def sibling_desc() -> dict:
    """Descriptive: for each 2-book race, minutes in which one book's committed quote says >= 0.90 while the sibling
    still offers the same leader at <= 0.85 (stale cross-book), the run lengths, and the volume traded in them."""
    picked, meta, cand = load()
    res = {}
    for e, (a, b) in pairs(picked, meta, cand).items():
        night = picked[a]["night"]; qa, qb = series_q(cand[a], night), series_q(cand[b], night)
        times = sorted({x[0] for x in qa} | {x[0] for x in qb})
        stale = []; vol = 0.0
        for t in times:
            if t < night - 3600:
                continue
            xa, xb = qat(qa, t, 10 ** 9), qat(qb, t, 10 ** 9)
            if xa is None or xb is None:
                continue
            # A says A's candidate leads at >= 0.90 and B still offers NO (the same leader) at <= 0.85, or vice versa
            for x1, x2 in ((xa, xb), (xb, xa)):
                if (x1[2] >= 0.90 and 1 - x2[2] <= 0.85) or (x1[1] <= 0.10 and x2[1] <= 0.85):
                    stale.append(t); vol += x2[3] or 0; break
        runs = []
        if stale:
            s0 = prev = stale[0]
            for t in stale[1:]:
                if t - prev > 120:
                    runs.append((s0, prev)); s0 = t
                prev = t
            runs.append((s0, prev))
        res[e] = {"a": a, "b": b, "stale_minutes": len(stale), "runs": [(et(x), et(y), (y - x) / 60 + 1) for x, y in runs],
                  "max_run_min": max(((y - x) / 60 + 1 for x, y in runs), default=0), "vol_in_stale": vol}
        print(f"  {e:18s} stale_min {len(stale):4d} max_run {res[e]['max_run_min']:6.0f} vol {vol:8.0f} runs {res[e]['runs'][:4]}")
    return res


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "rules":
    print("SIBLING-BOOK STALENESS"); sd = sibling_desc()
    print("\nRULES"); rr = rules_main()
    (OUT / "rules.json").write_text(json.dumps({"sibling_desc": sd, "rules": rr}, indent=1, default=str))


def feasible_volume() -> dict:
    """Contracts traded at <= 0.85 on the eventual winner from t0 + 2 min (earliest fill for a 1-2 min reactor that
    saw the move at t0), split by whether the leader at t0 was the eventual winner. Upper bound on what a
    perfect-hindsight 1-2 min taker could have bought in the race's own book."""
    picked, meta, cand = load()
    rows = [json.loads(l) for l in (OUT / "per_market.jsonl").open()]
    out = {}
    for trig in ("Q", "M"):
        acc = {"right": [], "wrong": []}
        for r in rows:
            d = r.get(trig) or {}
            if r["role"] != "race" or d.get("t0") is None:
                continue
            won_yes = meta[r["t"]]["result"] == "yes"
            q = series_q(cand[r["t"]], r["night"]); lo = 0.0
            for x in q:
                if x[0] < d["t0"] + 120 or not x[3] or x[4] is None:
                    continue
                if (won_yes and x[4] <= 0.85) or ((not won_yes) and x[5] >= 0.15):
                    lo += x[3]
            acc["right" if d["leader_won"] else "wrong"].append((r["t"], lo))
        out[trig] = {k: {"n": len(v), "median": med([x[1] for x in v]), "total": sum(x[1] for x in v),
                         "by_market": v} for k, v in acc.items()}
        print(f"  {trig}: leader-right n={out[trig]['right']['n']} median {out[trig]['right']['median']} total {out[trig]['right']['total']:.0f}"
              f" | leader-wrong n={out[trig]['wrong']['n']} median {out[trig]['wrong']['median']} total {out[trig]['wrong']['total']:.0f}")
        print("     right:", [(t, int(v)) for t, v in acc["right"]])
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "feasible":
    (OUT / "feasible_volume.json").write_text(json.dumps(feasible_volume(), indent=1))


def contrarian_rule(rows, picked, meta, cand, cap: float, trig: str = "Q", delay: int = 60, n_contracts: int = 10):
    """C3 (post-hoc idea from the 2024 night, counted as variants): at the first committed 25c move (t0), buy the
    TRAILER (the side the move went against) at its offer one minute later if <= cap (late-count shift)."""
    out = []
    for r in rows:
        d = r.get(trig) or {}
        if r["role"] != "race" or d.get("t0") is None:
            continue
        q = series_q(cand[r["t"]], r["night"]); x = qat(q, d["t0"] + delay)
        if x is None:
            continue
        px = leader_px(x, not d["lead_yes"])[0]
        if not (0.01 <= px <= cap):
            continue
        win = not d["leader_won"]; f = fee_order(px, n_contracts)
        out.append({"e": meta[r["t"]]["e"], "night": r["night"], "close": r["close"], "t_sig": d["t0"], "book": r["t"], "px": px,
                    "won": win, "ret": ((1.0 if win else 0.0) - px - f) / px})
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "contrarian":
    picked, meta, cand = load()
    rows = [json.loads(l) for l in (OUT / "per_market.jsonl").open()]
    evs = sorted({(picked[t]["night"], meta[t]["close"], meta[t]["e"]) for t in picked if t in cand and picked[t]["role"] in ("race", "sibling")})
    res = {}
    for trig in ("Q", "M"):
        for cap in (0.30, 0.50):
            tr = contrarian_rule(rows, picked, meta, cand, cap, trig)
            d, v = split(tr, evs)
            res[f"C3 contrarian trig={trig} cap={cap}"] = {"disc": stats(d), "val": stats(v), "trades": tr}
            print(f"C3 {trig} cap {cap}: disc {json.dumps({a: (round(b, 3) if isinstance(b, float) else b) for a, b in stats(d).items()})}")
            for x in tr:
                print("     ", x["book"], round(x["px"], 3), x["won"], round(x["ret"], 2), "VAL" if x in v else "disc")
    (OUT / "contrarian.json").write_text(json.dumps(res, indent=1, default=str))


def capacity_proxy(tr, cand, picked, minutes: int = 10) -> float | None:
    """Median contracts traded in the fill book over the 10 minutes after the signal (no depth in the archive)."""
    vs = []
    for x in tr:
        q = series_q(cand[x["book"]], picked[x["book"]]["night"])
        vs.append(sum(y[3] or 0 for y in q if x["t_sig"] < y[0] <= x["t_sig"] + minutes * 60))
    return med(vs)


def final() -> dict:
    """The three frozen candidates (picked on discovery, see notes) evaluated ONCE on validation."""
    picked, meta, cand = load()
    rows = [json.loads(l) for l in (OUT / "per_market.jsonl").open()]
    evs = sorted({(picked[t]["night"], meta[t]["close"], meta[t]["e"]) for t in picked if t in cand and picked[t]["role"] in ("race", "sibling")})
    cands = {"C1 cross-book X=0.85 cap=0.90 (best cross-book cell on discovery, -16%)": lambda: cross_book(picked, meta, cand, 0.85, 0.90),
             "C3 contrarian trig=M cap=0.30 (discovery-best by mean, +190%, t 1.19, wo3 -108%)": lambda: contrarian_rule(rows, picked, meta, cand, 0.30, "M"),
             "C2 own-book leader offer trig=Q cap=0.95 (the plan's buy-the-leader rule, market-only trigger)": lambda: leader_offer_rule(rows, picked, meta, cand, 0.95, "Q")}
    out = []
    for name, f in cands.items():
        tr = f(); d, v = split(tr, evs)
        sd, sv = stats(d), stats(v)
        span_days = max(1.0, (max((x["t_sig"] for x in tr), default=0) - min((x["t_sig"] for x in tr), default=0)) / 86400) if tr else 1
        out.append({"rule": name, "disc": sd, "val": sv, "val_trades": [{k: x[k] for k in ("book", "px", "won", "ret")} for x in v],
                    "capacity_proxy_contracts_10min": capacity_proxy(tr, cand, picked), "trades_per_election_night": len(tr) / 2})
        print(name, "\n   disc", json.dumps(sd, default=str), "\n   val ", json.dumps(sv, default=str), "\n   cap", out[-1]["capacity_proxy_contracts_10min"])
    (OUT / "final.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "final":
    final()
