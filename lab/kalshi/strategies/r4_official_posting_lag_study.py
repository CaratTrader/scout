"""r4_official_posting_lag_study: how fast does Kalshi reprice after an official, timestamped resolving record?

Measurement study (no trading rule unless the lag gate passes). Events: Senate roll calls (senate.gov XML vote_date =
official vote time; modify_date = latest XML edit, an upper bound on the XML's first posting), House roll calls
(clerk.house.gov XML action-time) and Supreme Court opinions (posted on supremecourt.gov from 10:00 ET in an
unannounced order). Kalshi 1-minute candles (yes ask/bid close, trade price, volume) for the markets those records
resolve, [t_evt - 3 h, min(close + 10 min, t_evt + 8 h)].

Per market, on the winning side's quotes (YES quotes if YES won, else 1 - bid / 1 - ask):
  m0     = winner mid 30 min before the official time (quote carried <= 30 min; older -> flagged)
  thr    = m0 + 0.9 * (0.99 - m0)          (90% of the remaining repricing; 0.99 is the highest tradeable price)
  t90    = first candle from which the winner mid stays >= thr until the end of the window ("sustained")
  lag90  = t90 - official time, and t90 - posting time
Gate to continue to a taker rule (task): median lag90 after the *posting* > 10 min AND depth >= 10 contracts.
Posting-informed taker (reported regardless): buy the winner at its ask 1 min after the posting (quote <= 30 min
old), fee 0.07 p (1-p) rounded up per 10-contract order; return per $ = (1 - ask - fee) / ask.
Usage: python -m lab.kalshi.strategies.r4_official_posting_lag_study"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_official_posting_lag_study_api import OUT, used

MAXAGE = 30 * 60
CHECK_MIN = (-30, -10, 0, 1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240)


def fee_per_contract(p: float, n: int = 10) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def winner_quotes(c: list[list], yes_won: bool) -> list[tuple]:
    """[(ts, w_ask, w_bid, vol)] with None where that side of the book is empty."""
    out = []
    for r in c:
        ts, a, b, vol = r[0], r[1], r[2], r[5]
        ya = a if (a is not None and 0 < a < 1) else None      # YES ask (None: no sellers)
        yb = b if (b is not None and 0 < b < 1) else None      # YES bid (None: no buyers)
        if yes_won:
            wa, wb = ya, yb
        else:
            wa = (1 - yb) if yb is not None else None
            wb = (1 - ya) if ya is not None else None
        out.append((ts, wa, wb, vol))
    return out


def mid(wa, wb):
    if wa is None and wb is None:
        return None
    if wa is None:
        return (wb + 1.0) / 2        # no one sells the winner: the book sits at the top
    if wb is None:
        return wa / 2
    return (wa + wb) / 2


def at(q: list[tuple], t: int, maxage: int = MAXAGE):
    best = None
    for r in q:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > maxage:
        return None
    return best


POST_MEDIAN_MIN = 59    # median senate.gov modify_date - vote_date over the 37 roll calls fetched (29..92 min when un-revised)


def post_time(e: dict) -> int:
    """Posting time: Senate = XML modify_date when it is within 2 h of the vote (else the XML was revised later and
    the first posting is unknown: vote_date + the 59-min median); House/SCOTUS = the official time itself
    (clerk XML / opinion PDF assumed posted at once, the most favourable case for a poller)."""
    if e["kind"] == "senate" and e.get("t_post"):
        return e["t_post"] if e["t_post"] - e["t_evt"] <= 7200 else e["t_evt"] + POST_MEDIAN_MIN * 60
    return e["t_evt"]


def analyse_market(e: dict, m: dict, c: list[list]) -> dict:
    yes = m["result"] == "yes"
    q = winner_quotes(c, yes)
    te, tp = e["t_evt"], post_time(e)
    r0 = at(q, te - 1800, 3 * 3600)
    res = {"ev": e["id"], "kind": e["kind"], "t": m["t"], "sub": m["sub"], "result": m["result"], "vol": m["vol"],
           "n_candles": len(c), "post_min": round((tp - te) / 60, 1)}
    if not r0 or mid(r0[1], r0[2]) is None:
        res["status"] = "no pre-quote"; return res
    m0 = mid(r0[1], r0[2]); res["m0"] = round(m0, 3); res["m0_age_min"] = round((te - 1800 - r0[0]) / 60, 1)
    gap = 0.99 - m0; res["gap"] = round(gap, 3)
    thr = m0 + 0.9 * gap; thr50 = m0 + 0.5 * gap
    mids = [(r[0], mid(r[1], r[2])) for r in q if r[0] >= te - 1800 and mid(r[1], r[2]) is not None]
    t90 = None
    for i, (ts, mm) in enumerate(mids):
        if mm >= thr - 1e-9 and all(x >= thr - 1e-9 for _, x in mids[i:]):
            t90 = ts; break
    t50 = next((ts for ts, mm in mids if mm >= thr50 - 1e-9), None)
    res["surprise"] = gap >= 0.10
    res["t90_lag_evt_min"] = round((t90 - te) / 60, 1) if t90 else None
    res["t90_lag_post_min"] = round((t90 - tp) / 60, 1) if t90 else None
    res["t50_lag_evt_min"] = round((t50 - te) / 60, 1) if t50 else None
    path = {}
    for k in CHECK_MIN:
        r = at(q, te + k * 60)
        path[str(k)] = None if not r else [r[1], r[2]]
    res["path_evt"] = path
    # posting-informed taker: decide at the posting, fill at the winner ask one minute later
    r = at(q, tp + 60)
    if r and r[1] is not None:
        p = r[1]; f = fee_per_contract(p)
        res["post_fill_ask"] = p; res["post_ret"] = round((1 - p - f) / p, 4)
    else:
        res["post_fill_ask"] = None; res["post_ret"] = None
    # what a taker could buy after the posting: the cheapest winner ask quoted in [post, post + 60 min], and the
    # first time from which the winner's offer is >= 0.97 or absent for good ("taker window closed")
    asks = [(x[0], x[1]) for x in q if tp <= x[0] < tp + 3600 and x[1] is not None]
    res["post_min_ask_60m"] = min((a for _, a in asks), default=None)
    seq = [(x[0], x[1]) for x in q if x[0] >= te - 1800]
    tclose = None
    for i, (ts, a) in enumerate(seq):
        if all(b is None or b >= 0.97 for _, b in seq[i:]):
            tclose = ts; break
    res["ask97_lag_evt_min"] = round((tclose - te) / 60, 1) if tclose else None
    res["ask97_lag_post_min"] = round((tclose - tp) / 60, 1) if tclose else None
    for k in (5, 10, 15, 30):
        r = at(q, tp + k * 60)
        res[f"post_ask_{k}m"] = None if not r else r[1]
    res["vol_after_post_30m"] = sum(x[3] for x in q if tp <= x[0] < tp + 1800)
    res["vol_evt_to_post"] = sum(x[3] for x in q if te <= x[0] < tp)
    return res


def clustered_t(vals: list[tuple[str, float]]) -> float:
    """Mean / SE with event clusters (sum of residuals per event)."""
    n = len(vals)
    if n < 2:
        return float("nan")
    mu = sum(v for _, v in vals) / n
    by = defaultdict(float)
    for e, v in vals:
        by[e] += v - mu
    g = len(by)
    if g < 2:
        return float("nan")
    var = sum(x * x for x in by.values()) * g / (g - 1) / (n * n)
    return mu / math.sqrt(var) if var > 0 else float("inf")


def stats(tr: list[dict]) -> dict:
    if not tr:
        return {"n": 0}
    v = [(x["ev"], x["ret"]) for x in tr]
    srt = sorted(x["ret"] for x in tr)
    tr_t = sorted(tr, key=lambda x: x["ts"])
    h = len(tr_t) // 2
    m = lambda xs: round(sum(x["ret"] for x in xs) / len(xs), 4) if xs else None
    return {"n": len(tr), "events": len({x["ev"] for x in tr}), "win": round(sum(x["ret"] > 0 for x in tr) / len(tr), 3),
            "avg_px": round(sum(x["px"] for x in tr) / len(tr), 3), "ret_per_dollar": m(tr), "t": round(clustered_t(v), 2),
            "ret_wo3": round(sum(srt[:-3]) / len(srt[:-3]), 4) if len(srt) > 3 else None,
            "half1": m(tr_t[:h]), "half2": m(tr_t[h:])}


def release_estimate(e: dict, cand: dict) -> int | None:
    """SCOTUS: the opinion's posting minute is not archived. Estimate it as the start of the first minute after
    10:00 ET in which any of the event's markets moved >= 25% of its gap toward the result."""
    best = None
    for m in e["markets"]:
        c = cand.get(m["t"])
        if not c:
            continue
        q = winner_quotes(c, m["result"] == "yes")
        r0 = at(q, e["t_evt"] - 60, 3 * 3600)
        if not r0 or mid(r0[1], r0[2]) is None:
            continue
        m0 = mid(r0[1], r0[2]); gap = 0.99 - m0
        if gap < 0.04:
            continue
        for ts, wa, wb, _ in q:
            if ts > e["t_evt"] and mid(wa, wb) is not None and mid(wa, wb) - m0 >= 0.25 * gap:
                best = ts - 60 if best is None else min(best, ts - 60); break
    if best is not None and best - e["t_evt"] > 45 * 60:
        return None                      # no move within 45 min of 10:00: not a surprise, release unknown
    return best


def taker(ev: dict, cand: dict, delay_min: int, senate_post: str) -> list[dict]:
    """Decide at posting + delay (the posting reveals the winner), fill at the winner ask one minute later
    (quote <= 30 min old, offer must exist), 10-contract order fee rounding."""
    out = []
    for e in ev.values():
        if e["kind"] == "senate":
            if senate_post == "strict" and e["t_post"] - e["t_evt"] > 7200:
                continue                 # XML revised later: first posting time unknown
            tp = post_time(e)
        elif e["kind"] == "scotus":
            tp = release_estimate(e, cand)
            if tp is None or tp - e["t_evt"] > 45 * 60:
                tp = e["t_evt"]          # no move within 45 min: no surprise, posting taken as 10:00
        else:
            tp = e["t_evt"]
        for m in e["markets"]:
            c = cand.get(m["t"])
            if not c:
                continue
            q = winner_quotes(c, m["result"] == "yes")
            r = at(q, tp + (delay_min + 1) * 60)
            if not r or r[1] is None or r[1] >= 0.995:
                continue
            p = r[1]
            out.append({"ev": e["id"], "kind": e["kind"], "t": m["t"], "ts": tp, "px": p, "delay": delay_min,
                        "ret": (1 - p - fee_per_contract(p)) / p,
                        "vol_next5": sum(x[3] for x in q if tp + (delay_min) * 60 <= x[0] < tp + (delay_min + 5) * 60)})
    return out


def summary(rows: list[dict], ev: dict, cand: dict) -> dict:
    """Event-level lag table (one market per event: the largest gap) and the posting-taker variants."""
    prim = {}
    for r in rows:
        if r.get("gap") is None:
            continue
        if r["ev"] not in prim or r["gap"] > prim[r["ev"]]["gap"]:
            prim[r["ev"]] = r
    lag = {}
    for kind in ("senate", "house", "scotus", "all"):
        ps = [r for r in prim.values() if (kind == "all" or r["kind"] == kind) and r["gap"] >= 0.10]
        a = [r["ask97_lag_post_min"] for r in ps if r.get("ask97_lag_post_min") is not None]
        b = [r["t90_lag_post_min"] for r in ps if r.get("t90_lag_post_min") is not None]
        lag[kind] = {"surprise_events": len(ps), "median_ask97_lag_after_post_min": st.median(a) if a else None,
                     "median_mid90_lag_after_post_min": st.median(b) if b else None,
                     "share_ask97_lag_gt10": round(sum(x > 10 for x in a) / len(a), 3) if a else None,
                     "events_ask97_lag_gt10": [r["ev"] for r in ps if (r.get("ask97_lag_post_min") or -999) > 10]}
    # SCOTUS: lag from the estimated release instead of 10:00
    sc = []
    for r in prim.values():
        if r["kind"] == "scotus":
            rel = release_estimate(ev[r["ev"]], cand)
            r["release_est_min_after_10"] = None if rel is None else round((rel - ev[r["ev"]]["t_evt"]) / 60, 1)
            if rel is not None and r.get("ask97_lag_evt_min") is not None:
                r["ask97_lag_after_release_min"] = round(r["ask97_lag_evt_min"] - r["release_est_min_after_10"], 1)
                sc.append(r["ask97_lag_after_release_min"])
    lag["scotus"]["median_ask97_lag_after_release_est_min"] = st.median(sc) if sc else None
    lag["scotus"]["ask97_lag_after_release_est_min"] = sc
    ah = [r["ask97_lag_post_min"] for r in prim.values() if r["kind"] != "scotus" and r["gap"] >= 0.10 and r.get("ask97_lag_post_min") is not None]
    lag["all_release_based"] = {"n": len(ah) + len(sc), "median_ask97_lag_min": st.median(ah + sc) if ah + sc else None,
                                "share_gt10": round(sum(x > 10 for x in ah + sc) / len(ah + sc), 3) if ah + sc else None}
    # time split (event order by close): discovery = first 70% of events, validation = last 30%
    order = sorted(ev.values(), key=lambda e: max(m["close"] for m in e["markets"]) if e["markets"] else e["t_evt"])
    k = int(round(0.7 * len(order)))
    disc = {e["id"] for e in order[:k]}; val = {e["id"] for e in order[k:]}
    split = {}
    for d in (0, 1, 2, 5, 10):
        tr = taker(ev, cand, d, "modify")
        split[f"post+{d}m"] = {"discovery": stats([x for x in tr if x["ev"] in disc]), "validation": stats([x for x in tr if x["ev"] in val])}
    variants = {}
    for d in (0, 1, 2, 5, 10):
        for sp in ("modify", "strict"):
            tr = taker(ev, cand, d, sp)
            key = f"post+{d}m_senate-{sp}"
            variants[key] = {"all": stats(tr), **{k: stats([x for x in tr if x["kind"] == k]) for k in ("senate", "house", "scotus")},
                             "median_vol_next5": st.median([x["vol_next5"] for x in tr]) if tr else None}
    trades0 = taker(ev, cand, 0, "modify")
    return {"primary": prim, "lag": lag, "taker": variants, "trades_post0": trades0, "split": split,
            "discovery_events": sorted(disc), "validation_events": sorted(val)}


def main() -> None:
    ev = {e["id"]: e for e in json.loads((OUT / "events.json").read_text())}
    cand = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l); cand[x["t"]] = x["c"]
    rows = []
    for e in ev.values():
        for m in e["markets"]:
            if m["t"] in cand:
                rows.append(analyse_market(e, m, cand[m["t"]]))
    (OUT / "per_market.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    for r in rows:
        print(f"{r['ev']:15s} {r['t'][-16:]:16s} {r['result']:3s} m0={r.get('m0')} gap={r.get('gap')} "
              f"lag90 evt={r.get('t90_lag_evt_min')} post={r.get('t90_lag_post_min')} (post at +{r['post_min']}m) "
              f"fill={r.get('post_fill_ask')} ret={r.get('post_ret')} v30={r.get('vol_after_post_30m')}")
    sm = summary(rows, ev, cand)
    (OUT / "summary.json").write_text(json.dumps(sm, indent=1))
    print(json.dumps(sm["lag"], indent=1))
    for k, v in sm["taker"].items():
        print(k, "ALL", v["all"], "| house", v["house"].get("ret_per_dollar"), v["house"].get("n"), "| scotus", v["scotus"].get("ret_per_dollar"), v["scotus"].get("n"), "| senate", v["senate"].get("ret_per_dollar"), v["senate"].get("n"), "| vol5", v["median_vol_next5"])
    for k, v in sm["split"].items():
        print("SPLIT", k, "disc", v["discovery"], "\n      val ", v["validation"])
    for r in sm["primary"].values():
        if r["kind"] == "scotus":
            print(r["ev"], "release est (min after 10:00)", r.get("release_est_min_after_10"))
    print("Kalshi calls used", used())


if __name__ == "__main__":
    main()
