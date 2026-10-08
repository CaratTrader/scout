"""r2_infra_timestamp_fill_audit: measurement audit for the Kalshi lab (judges no strategy).

  align     - which seconds a 1-minute candle stamped T covers (trade prints vs candle volume / price / quote close)
  stale     - REST /markets and /orderbook staleness vs exact-time trade prints (live recording)
  fill      - maker fill model: candle proxies vs the print-exact time-priority fill of hypothetical resting orders
              (created level = strictly inside the spread -> first in queue; joined level -> queue from REST sizes)
Datasets (prints + 1-minute candles):
  D1 weather  44 KXHIGH* markets, 2-h windows (cached by r2_weather_daily_maker, read-only)
  D2 rain     64 KXRAIN markets, 30-min windows (cached by microstructure verifier, read-only)
  D3 live     17 markets of KXBTC15M / KXBTCD / KXRAIN / KXHIGHNY recorded live 2026-10-08 ~17:15-17:25 ET
              (REST snapshots every ~5 s, orderbooks, prints, candles; this family's own calls)
  D4 inxu/commod (optional, see _data.py)
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_infra_timestamp_fill_audit align|stale|fill|all"""
from __future__ import annotations
import bisect, glob, json, math, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r2_infra_timestamp_fill_audit_fill as F
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_align import align, norm_prints, norm_candles, window_of
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_api import OUT

ROOT = Path(__file__).resolve().parents[3]


# ----------------------------------------------------------------------------------------------------- datasets
def d1_weather() -> list[dict]:
    meta = json.load(open(ROOT / "data/lab/us/kalshi/markets.json"))
    out = []
    for f in sorted(glob.glob(str(ROOT / "data/kalshi_lab/strategies/r2_weather_daily_maker/trades/*.json"))):
        d = json.load(open(f)); tk = d["order"]["tk"]; cf = ROOT / f"data/lab/us/kalshi/{tk}.json"
        if not cf.exists() or tk not in meta:
            continue
        raw = d["trades"]; lo, hi = d["t0"], d["t1"]
        out.append({"ds": "D1_weather", "tk": tk, "e": meta[tk]["event_ticker"], "series": tk.split("-")[0], "result": meta[tk]["result"],
                    "prints": F.norm_prints(raw), "raw_candles": json.load(open(cf)), "lo": lo, "hi": hi})
    return out


def d2_rain() -> list[dict]:
    A = ROOT / "data/kalshi_lab/strategies/microstructure/repro/api"
    cands = {}; meta = {}
    for line in open(ROOT / "data/kalshi_lab/candles/KXRAIN.jsonl"):
        x = json.loads(line); cands[x["t"]] = x["c"]
    for line in open(ROOT / "data/kalshi_lab/markets/KXRAIN.jsonl"):
        m = json.loads(line); meta[m["t"]] = m
    out = []
    for u in [l.split()[-1] for l in open(A / "calls.log") if "trades?" in l]:
        tk = re.search(r"ticker=([^&]+)", u).group(1); lo = int(re.search(r"min_ts=(\d+)", u).group(1)); hi = int(re.search(r"max_ts=(\d+)", u).group(1))
        f = A / f"trades_{tk}_{lo}.json"
        if not f.exists() or tk not in cands or tk not in meta:
            continue
        raw = json.load(open(f))["trades"]; lo2, hi2 = window_of(raw, lo, hi)
        out.append({"ds": "D2_rain", "tk": tk, "e": meta[tk]["e"], "series": "KXRAIN", "result": meta[tk]["result"], "prints": F.norm_prints(raw),
                    "raw_candles": cands[tk], "lo": lo2, "hi": hi2})
    return out


def d3_live() -> list[dict]:
    p = OUT / "prints_live.json"; c = OUT / "candles_live.json"
    if not (p.exists() and c.exists()):
        return []
    P = json.load(open(p)); C = {m["market_ticker"]: m["candlesticks"] for m in json.load(open(c)).get("markets", [])}
    rest = defaultdict(list)
    for l in (OUT / "live.jsonl").open():
        r = json.loads(l)
        if r["v"] == "M":
            rest[r["tk"]].append(r)
    out = []
    for tk, raw in P["prints"].items():
        lo, hi = P["coverage"][tk]
        lo = F.ts(lo) if isinstance(lo, str) else lo
        out.append({"ds": "D3_live", "tk": tk, "e": tk.rsplit("-", 1)[0], "series": tk.split("-")[0], "result": None, "prints": F.norm_prints(raw),
                    "raw_candles": C.get(tk, []), "lo": lo, "hi": hi, "rest": rest.get(tk, [])})
    return out


# --------------------------------------------------------------------------------------------------- fill model
def hypothetical_orders(m: dict, every: int = 300, H: int = 30, size: float = 10.0) -> list[dict]:
    """Outcome-blind hypothetical orders: at minute-aligned t (every `every` s) read the candle quote at t, go live at
    t + 60, rest until min(t + 60 + H min, end of print coverage). Four orders per t: YES bid improve (bid + 1c, if the
    spread is >= 2c) / join (= bid); YES offer (= NO bid) improve (ask - 1c) / join (= ask)."""
    cs = [F.norm_candle(c) for c in m["raw_candles"]]; cs.sort(key=lambda c: c["T"])
    if not cs:
        return []
    P = m["prints"]; out = []
    t = int(math.ceil((m["lo"] + 60) / 60) * 60)
    while t + 60 + 600 <= m["hi"]:
        q = F.quote_at(cs, t)
        if q and q[0] and q[1] and q[0] - q[1] >= 0.01 - 1e-9 and 0 < q[1] and q[0] < 1:
            ask, bid = q; tl = t + 60; te = min(tl + H * 60, int(m["hi"] // 60 * 60))
            for side, kind, lvl in (("yes", "improve", bid + 0.01), ("yes", "join", bid), ("no", "improve", ask - 0.01), ("no", "join", ask)):
                lvl = round(lvl, 2)
                if kind == "improve" and ask - bid < 0.02 - 1e-9:
                    continue
                if not 0.03 <= lvl <= 0.97:
                    continue
                cf = F.candle_fill(side, lvl, tl, te, cs, touch_rule="strict")
                if cf["status"] == "crossed":
                    continue
                created = cf["created"]
                tr0 = F.fill_from_prints(side, lvl, tl, te, P, size=size, queue_ahead=0.0)
                tr0a = F.fill_from_prints(side, lvl, tl, te, P, size=size, queue_ahead=0.0, count_cross=False)
                trinf = F.fill_from_prints(side, lvl, tl, te, P, size=size, queue_ahead=1e12)
                qx = _quote_cross(side, lvl, tl, te, cs)
                Q = None
                if m.get("rest"):
                    rs = [r for r in m["rest"] if r["t1"] <= tl]
                    if rs:
                        r = rs[-1]; top = float(r["bid"] or 0) if side == "yes" else float(r["ask"] or 0)
                        szq = float((r["bid_sz"] if side == "yes" else r["ask_sz"]) or 0)
                        Q = szq if abs(top - lvl) < 1e-6 else (0.0 if ((side == "yes" and lvl > top) or (side == "no" and lvl < top)) else None)
                trq = F.fill_from_prints(side, lvl, tl, te, P, size=size, queue_ahead=Q) if Q is not None else None
                rules = {}
                for name, kw in (("auto", {"touch_rule": "auto"}), ("strict_new", {"touch_rule": "strict"}), ("touch_always", {"touch_rule": "always"})):
                    rules[name] = F.candle_fill(side, lvl, tl, te, cs, **kw)["fill_ts"]
                rules["px_through_only"] = _px_through(side, lvl, tl, te, cs)
                rules["px_or_quote_strict"] = _old_strict(side, lvl, tl, te, cs)
                rules["quote_plus1c"] = _quote_plus1c(side, lvl, tl, te, cs)
                won = None if m["result"] not in ("yes", "no") else (m["result"] == "yes") == (side == "yes")
                px = lvl if side == "yes" else round(1 - lvl, 2)
                mn = lambda *a: min([x for x in a if x is not None], default=None)
                out.append({"ds": m["ds"], "tk": m["tk"], "e": m["e"], "series": m["series"], "t": t, "tl": tl, "te": te, "side": side, "kind": kind,
                            "created": created, "lvl": lvl, "px": px, "won": won,
                            "truth_q0_first": tr0["first_fill_ts"], "truth_q0_kind": tr0["kind"], "truth_q0_full": tr0["full_fill_ts"],
                            "truth_q0_aggr": tr0a["first_fill_ts"], "truth_q0_any": mn(tr0["first_fill_ts"], qx),
                            "truth_qinf_first": trinf["first_fill_ts"], "truth_qinf_any": mn(trinf["first_fill_ts"], qx), "quote_cross": qx, "Q": Q,
                            "truth_q_first": trq["first_fill_ts"] if trq else None, "truth_q_any": mn(trq["first_fill_ts"], qx) if trq else None,
                            "truth_q_full": trq["full_fill_ts"] if trq else None, **{"r_" + k: v for k, v in rules.items()}})
        t += every
    return out


def _quote_cross(side, lvl, tl, te, cs):
    """Counterfactual cross from the quote: the opposite best quote reached our level (an order rested there)."""
    for c in cs:
        if c["T"] - 60 < tl or c["T"] > te:
            continue
        if side == "yes" and c["ask_lo"] is not None and 0 < c["ask_lo"] <= lvl + 1e-9:
            return c["T"]
        if side == "no" and c["bid_hi"] is not None and 0 < c["bid_hi"] and c["bid_hi"] >= lvl - 1e-9:
            return c["T"]
    return None


def _px_through(side, lvl, tl, te, cs):
    for c in cs:
        if c["T"] - 60 < tl or c["T"] > te:
            continue
        if side == "yes" and c["px_lo"] is not None and c["px_lo"] < lvl - 1e-9:
            return c["T"]
        if side == "no" and c["px_hi"] is not None and c["px_hi"] > lvl + 1e-9:
            return c["T"]
    return None


def _old_strict(side, lvl, tl, te, cs):
    """R2 weather/commod 'strict': print strictly through, or the opposite quote strictly through."""
    for c in cs:
        if c["T"] - 60 < tl or c["T"] > te:
            continue
        if side == "yes" and ((c["px_lo"] is not None and c["px_lo"] < lvl - 1e-9) or (c["ask_lo"] is not None and 0 < c["ask_lo"] < lvl - 1e-9)):
            return c["T"]
        if side == "no" and ((c["px_hi"] is not None and c["px_hi"] > lvl + 1e-9) or (c["bid_hi"] is not None and c["bid_hi"] > lvl + 1e-9)):
            return c["T"]
    return None


def _quote_plus1c(side, lvl, tl, te, cs):
    """Round-1 microstructure proxy: opposite quote >= 1c through our level (bid_high >= offer + 1c)."""
    for c in cs:
        if c["T"] - 60 < tl or c["T"] > te:
            continue
        if side == "yes" and c["ask_lo"] is not None and 0 < c["ask_lo"] <= lvl - 0.01 + 1e-9:
            return c["T"]
        if side == "no" and c["bid_hi"] is not None and c["bid_hi"] >= lvl + 0.01 - 1e-9:
            return c["T"]
    return None


RULES = ("auto", "strict_new", "touch_always", "px_through_only", "px_or_quote_strict", "quote_plus1c")


def compare(rows: list[dict], truth: str) -> dict:
    """Agreement of each candle rule with a print-truth column; timing; adverse selection (win rate / maker return)."""
    rows = [r for r in rows if truth in r and (r[truth] is not None or True)]
    T = [r for r in rows if r[truth] is not None]
    res = {"orders": len(rows), "truth_fill_rate": round(len(T) / max(1, len(rows)), 3)}
    def ret(rs):
        x = [((1.0 if r["won"] else 0.0) - r["px"]) / r["px"] for r in rs if r["won"] is not None]
        return (round(st.mean(x), 4) if x else None, len(x), round(st.mean(1.0 if r["won"] else 0.0 for r in rs if r["won"] is not None), 3) if x else None)
    res["truth_filled_ret_n_win"] = ret(T)
    res["truth_unfilled_ret_n_win"] = ret([r for r in rows if r[truth] is None])
    for k in RULES:
        R = [r for r in rows if r["r_" + k] is not None]
        tp = [r for r in R if r[truth] is not None]
        lag = [r["r_" + k] - r[truth] for r in tp]
        res[k] = {"fill_rate": round(len(R) / max(1, len(rows)), 3), "sensitivity": round(len(tp) / max(1, len(T)), 3),
                  "false_pos": len(R) - len(tp), "false_pos_rate_of_unfilled": round((len(R) - len(tp)) / max(1, len(rows) - len(T)), 3),
                  "rule_minus_truth_s_median": st.median(lag) if lag else None, "rule_early_count": sum(1 for x in lag if x < -60),
                  "ret_n_win": ret(R)}
    return res


def kind_returns(rows):
    """Maker return per $ by the kind of the first print-truth fill (Q = 0) and for quote-only crosses."""
    g = defaultdict(list)
    for r in rows:
        if r["won"] is None:
            continue
        k = r["truth_q0_kind"] or ("quote_cross_only" if r["quote_cross"] else "unfilled")
        g[k].append(((1.0 if r["won"] else 0.0) - r["px"]) / r["px"])
    return {k: {"n": len(v), "ret": round(st.mean(v), 4)} for k, v in g.items()}


def fill(write: bool = True) -> dict:
    data = d1_weather() + d2_rain() + d3_live()
    rows = []
    for m in data:
        rows += hypothetical_orders(m)
    out = {"datasets": {ds: {"markets": sum(1 for m in data if m["ds"] == ds), "prints": sum(len(m["prints"]) for m in data if m["ds"] == ds)}
                        for ds in sorted({m["ds"] for m in data})}}
    for ds in sorted({r["ds"] for r in rows}) + ["ALL"]:
        R = rows if ds == "ALL" else [r for r in rows if r["ds"] == ds]
        out[ds] = {}
        C = [r for r in R if r["created"]]; J = [r for r in R if not r["created"]]
        out[ds]["created_truth_prints_q0"] = compare(C, "truth_q0_first")
        out[ds]["created_truth_prints+quote_q0"] = compare(C, "truth_q0_any")
        out[ds]["created_truth_aggressive_only"] = compare(C, "truth_q0_aggr")
        out[ds]["created_truth_full10"] = compare(C, "truth_q0_full")
        out[ds]["created_fill_kind_returns"] = kind_returns(C)
        out[ds]["joined_upper_q0"] = compare(J, "truth_q0_any")
        out[ds]["joined_lower_qinf"] = compare(J, "truth_qinf_any")
        JQ = [r for r in J if r["Q"] is not None]
        if JQ:
            out[ds]["joined_queue_from_rest"] = compare(JQ, "truth_q_any")
            out[ds]["joined_queue_from_rest_full10"] = compare(JQ, "truth_q_full")
            out[ds]["joined_queue_Q_median"] = st.median(r["Q"] for r in JQ)
    if write:
        (OUT / "fill_rows.json").write_text(json.dumps(rows))
        (OUT / "fill_model.json").write_text(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------------------------------------------- REST staleness
def _cum(P):
    c = 0.0; out = []
    for p in P:
        c += p[3]; out.append(c)
    return out


def rest_staleness(m: dict) -> dict:
    """REST /markets volume_fp vs cumulative print volume: for every snapshot (receive time t1) find the true time tau
    at which the prints add up to the snapshot's volume -> staleness = t1 - tau (an interval between two prints).
    The unknown volume before the print window (base) is fitted over a lag grid; snapshots must lie inside coverage."""
    P = m["prints"]; pt = [p[0] for p in P]; cum = _cum(P)
    C = lambda t: cum[bisect.bisect_right(pt, t) - 1] if bisect.bisect_right(pt, t) > 0 else 0.0
    snaps = [(r["t1"], float(r["vol"] or 0), r) for r in m.get("rest", []) if r.get("vol") is not None and m["lo"] + 130 <= r["t1"] <= m["hi"]]
    if len(snaps) < 5 or len(P) < 2:
        return {"snaps": len(snaps), "prints": len(P)}
    best = None
    for L10 in range(0, 1201, 5):
        L = L10 / 10
        bases = [round(v - C(t - L), 2) for t, v, _ in snaps]
        base = st.median(bases)
        err = sum(1 for t, v, _ in snaps if abs(v - base - C(t - L)) > 0.011 + 1e-7 * v) / len(snaps)
        if best is None or err < best[0] - 1e-9:
            best = (err, L, base)
    err, L, base = best
    iv = []; bad = 0
    for t, v, r in snaps:
        target = v - base
        j = bisect.bisect_right(cum, target + 0.011 + 1e-7 * v) - 1    # prints 0..j included
        if j >= 0 and abs(cum[j] - target) > 0.011 + 1e-7 * v or (j < 0 and abs(target) > 0.011):
            bad += 1; continue
        lo_tau = pt[j] if j >= 0 else m["lo"]
        hi_tau = pt[j + 1] if j + 1 < len(pt) else m["hi"]
        iv.append((t - hi_tau, t - lo_tau, r["tk"]))     # staleness in (t - next print, t - last included print]
    tight = [(a + b) / 2 for a, b, _ in iv if b - a <= 5 and b >= 0]
    return {"snaps": len(snaps), "prints": len(P), "fit_lag_s": L, "fit_mismatch": round(err, 3), "inconsistent": bad,
            "n_tight": len(tight), "staleness_tight_s": sorted(round(x, 2) for x in tight),
            "lower_bounds_s": sorted(round(max(0, a), 1) for a, b, _ in iv)}


def orderbook_staleness(m: dict, obs: list[dict], max_lag: int = 90) -> dict:
    """Orderbook / REST top of book vs prints: a YES-buy print at time p reveals yes_ask(p-) = price; a NO-buy print
    reveals yes_bid(p-). For lag L, compare the snapshot's best ask/bid (taken at receive time t1) with the price of
    the first revealing print after t1 - L (within 3 s). The L with the best match rate ~ the snapshot's age."""
    P = m["prints"]; pt = [p[0] for p in P]
    def reveal(t, side):
        i = bisect.bisect_left(pt, t)
        while i < len(P) and P[i][0] <= t + 3:
            if P[i][2] == side:
                return P[i][1]
            i += 1
        return None
    res = {}
    for name, snaps in (("orderbook", obs), ("rest", m.get("rest", []))):
        rows = []
        for s in snaps:
            if name == "orderbook":
                b = (s.get("book") or {}).get("orderbook_fp") or {}
                yes = [(float(p), float(q)) for p, q in b.get("yes_dollars") or []]; no = [(float(p), float(q)) for p, q in b.get("no_dollars") or []]
                bid = max((p for p, q in yes if q > 0), default=None); nb = max((p for p, q in no if q > 0), default=None)
                ask = round(1 - nb, 4) if nb is not None else None
            else:
                bid = float(s["bid"] or 0) or None; ask = float(s["ask"] or 0) or None
            if bid is None or ask is None or not (m["lo"] + max_lag <= s["t1"] <= m["hi"] - 5):
                continue
            rows.append((s["t1"], ask, bid))
        out = {}
        for L in range(-10, max_lag + 1, 5):
            ok = n = 0
            for t1, ask, bid in rows:
                ra = reveal(t1 - L, "yes"); rb = reveal(t1 - L, "no")
                for r, q in ((ra, ask), (rb, bid)):
                    if r is not None:
                        n += 1; ok += abs(r - q) < 1e-6
            if n:
                out[L] = (round(ok / n, 3), n)
        res[name] = {"snapshots": len(rows), "match_rate_by_lag_s": out,
                     "best_lag_s": max(out, key=lambda L: (out[L][0], -L)) if out else None}
    return res


def stale() -> dict:
    data = d3_live(); obs = defaultdict(list)
    for l in (OUT / "live.jsonl").open():
        r = json.loads(l)
        if r["v"] == "OB":
            obs[r["tk"]].append(r)
    res = {}
    allt = defaultdict(list); allb = defaultdict(list)
    for m in data:
        r = rest_staleness(m)
        if m["tk"] in obs or m["series"] in ("KXBTC15M", "KXBTCD"):
            r["quote_reveal"] = orderbook_staleness(m, obs.get(m["tk"], []))
        res[m["tk"]] = r
        allt[m["series"]] += r.get("staleness_tight_s", []); allb[m["series"]] += r.get("lower_bounds_s", [])
    def q(x, f):
        x = sorted(x); return x[min(len(x) - 1, int(f * len(x)))] if x else None
    res["_summary"] = {s: {"n_tight": len(v), "median": q(v, 0.5), "p10": q(v, 0.1), "p90": q(v, 0.9), "max": max(v) if v else None,
                           "n_lower_bounds": len(allb[s]), "lower_bound_median": q(allb[s], 0.5), "lower_bound_p90": q(allb[s], 0.9)}
                       for s, v in allt.items()}
    allv = [x for v in allt.values() for x in v]
    res["_summary"]["ALL"] = {"n_tight": len(allv), "median": q(allv, 0.5), "p10": q(allv, 0.1), "p90": q(allv, 0.9), "max": max(allv) if allv else None}
    (OUT / "staleness.json").write_text(json.dumps(res, indent=1))
    return res


def align_all() -> dict:
    """Candle alignment on D1 weather, D2 rain, D3 live (and INXU if fetched)."""
    out = {}
    sets = {"D1_weather": d1_weather(), "D2_rain": d2_rain(), "D3_live": d3_live()}
    pi = OUT / "prints_inxu.json"
    if pi.exists():
        x = json.load(open(pi)); cand = {}
        for line in open(ROOT / "data/kalshi_lab/candles/KXINXU.jsonl"):
            y = json.loads(line); cand[y["t"]] = y["c"]
        sets["D5_inxu"] = [{"tk": tk, "prints": F.norm_prints(v["prints"]), "raw_candles": cand.get(tk, []), "lo": v["lo"], "hi": v["hi"]} for tk, v in x.items()]
    for name, ms in sets.items():
        items = []
        for m in ms:
            c = norm_candles(m["raw_candles"])
            if not c:
                continue
            # dense only if the candle file spans the print window (missing minute = zero volume)
            dense = c[0]["T"] <= m["lo"] - 60 and c[-1]["T"] >= m["hi"]
            items.append({"tk": m["tk"], "prints": m["prints"], "lo": m["lo"], "hi": m["hi"], "candles": c, "dense": dense})
        if items:
            out[name] = align(items)
            out[name]["markets"] = len(items)
    (OUT / "alignment.json").write_text(json.dumps(out, indent=1))
    return out


# ------------------------------------------------------------------------------- queue size and time at level
QS = (0, 5, 25, 100, 500, 1e12)
HS = (5, 10, 20, 30, 60)


def candle_queue_fill(side, lvl, tl, te, cs, Q, size=10.0, frac=1.0):
    """Candle-only queue estimate for a JOINED level: certain fills (through / quote cross) as candle_fill(strict);
    otherwise at-level volume ~ frac x candle volume in minutes whose print range touches our level without going
    through; filled once that estimate exceeds Q + size."""
    cum = 0.0
    for c in cs:
        if c["T"] - 60 < tl or c["T"] > te:
            continue
        if side == "yes":
            if (c["px_lo"] is not None and c["px_lo"] < lvl - 1e-9) or (c["ask_lo"] is not None and 0 < c["ask_lo"] <= lvl + 1e-9):
                return c["T"]
            if c["px_lo"] is not None and abs(c["px_lo"] - lvl) < 1e-9:
                cum += frac * (c["vol"] or 0)
        else:
            if (c["px_hi"] is not None and c["px_hi"] > lvl + 1e-9) or (c["bid_hi"] is not None and 0 < c["bid_hi"] and c["bid_hi"] >= lvl - 1e-9):
                return c["T"]
            if c["px_hi"] is not None and abs(c["px_hi"] - lvl) < 1e-9:
                cum += frac * (c["vol"] or 0)
        if cum >= Q + size:
            return c["T"]
    return None


def queue_horizon(write: bool = True) -> dict:
    data = d1_weather() + d2_rain()
    rows = []
    for m in data:
        cs = sorted([F.norm_candle(c) for c in m["raw_candles"]], key=lambda c: c["T"]); P = m["prints"]
        if not cs:
            continue
        t = int(math.ceil((m["lo"] + 60) / 60) * 60)
        while t + 60 + 600 <= m["hi"]:
            q = F.quote_at(cs, t)
            if q and q[0] and q[1] and 0 < q[1] and q[0] < 1 and q[0] - q[1] >= 0.01 - 1e-9:
                ask, bid = q; tl = t + 60; te = min(tl + 60 * 60, int(m["hi"] // 60 * 60))
                for side, lvl in (("yes", bid), ("no", ask), ("yes", bid + 0.01), ("no", ask - 0.01)):
                    lvl = round(lvl, 2)
                    if not 0.03 <= lvl <= 0.97:
                        continue
                    cf = F.candle_fill(side, lvl, tl, te, cs, touch_rule="strict")
                    if cf["status"] == "crossed":
                        continue
                    qx = _quote_cross(side, lvl, tl, te, cs)
                    won = None if m["result"] not in ("yes", "no") else (m["result"] == "yes") == (side == "yes")
                    r = {"ds": m["ds"], "e": m["e"], "side": side, "created": cf["created"], "px": lvl if side == "yes" else round(1 - lvl, 2),
                         "won": won, "tl": tl, "dur": te - tl, "fill": {}, "cfill": {}, "has_px": cs[0]["px_lo"] is not None or any(c["px_lo"] is not None for c in cs[:50])}
                    for Qv in QS:
                        a = F.fill_from_prints(side, lvl, tl, te, P, size=10.0, queue_ahead=Qv)["full_fill_ts"]
                        r["fill"][str(Qv)] = min([x for x in (a, qx) if x is not None], default=None)
                        r["cfill"][str(Qv)] = candle_queue_fill(side, lvl, tl, te, cs, Qv)
                    rows.append(r)
            t += 300
    out = {}
    for ds in ("D1_weather", "D2_rain", "ALL"):
        R = [r for r in rows if ds == "ALL" or r["ds"] == ds]
        o = {}
        for grp, G in (("created", [r for r in R if r["created"]]), ("joined", [r for r in R if not r["created"]])):
            g = {}
            for H in HS:
                G2 = [r for r in G if r["dur"] >= H * 60]
                f = [r for r in G2 if r["fill"]["0"] is not None and r["fill"]["0"] - r["tl"] <= H * 60]
                g[f"H{H}_q0_fill_rate"] = (round(len(f) / max(1, len(G2)), 3), len(G2))
            G30 = [r for r in G if r["dur"] >= 1200]
            for Qv in QS:
                k = str(Qv)
                f = [r for r in G30 if r["fill"][k] is not None and r["fill"][k] - r["tl"] <= 1200]
                cf = [r for r in G30 if r["cfill"][k] is not None and r["cfill"][k] - r["tl"] <= 1200]
                both = [r for r in f if r in cf]
                rr = lambda X: round(st.mean(((1.0 if r["won"] else 0.0) - r["px"]) / r["px"] for r in X if r["won"] is not None), 4) if [r for r in X if r["won"] is not None] else None
                g[f"H20_Q{int(Qv) if Qv < 1e11 else 'inf'}"] = {"orders": len(G30), "fill_rate_prints": round(len(f) / max(1, len(G30)), 3),
                                                                  "fill_rate_candle_est": round(len(cf) / max(1, len(G30)), 3),
                                                                  "agree": round((len(both) + sum(1 for r in G30 if r not in f and r not in cf)) / max(1, len(G30)), 3),
                                                                  "ret_filled_prints": rr(f), "ret_filled_candle_est": rr(cf)}
            o[grp] = g
        out[ds] = o
    if write:
        (OUT / "queue_horizon.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    a = sys.argv[1:] or ["fill"]
    if a[0] == "queue":
        print(json.dumps(queue_horizon(), indent=1))
    if a[0] == "stale":
        print(json.dumps(stale()["_summary"], indent=1))
    if a[0] == "align":
        r = align_all()
        for k, v in r.items():
            print(k, v["markets"], {x: y for x, y in v.items() if x.startswith("best")})
    if a[0] == "fill":
        r = fill()
        for ds, v in r.items():
            if ds == "datasets":
                print(v); continue
            for k, x in v.items():
                if not isinstance(x, dict) or "orders" not in x:
                    print(ds, k, x); continue
                print(f"\n== {ds} {k}: orders={x['orders']} truth_fill={x['truth_fill_rate']} truth_filled(ret,n,win)={x['truth_filled_ret_n_win']} unfilled={x['truth_unfilled_ret_n_win']}")
                for rn in RULES:
                    y = x[rn]
                    print(f"   {rn:20s} fill={y['fill_rate']:.3f} sens={y['sensitivity']:.3f} fp={y['false_pos']:4d} ({y['false_pos_rate_of_unfilled']:.3f}) lag_med={y['rule_minus_truth_s_median']} early={y['rule_early_count']} ret={y['ret_n_win']}")
