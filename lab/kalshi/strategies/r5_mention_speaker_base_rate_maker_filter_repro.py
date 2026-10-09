"""r5_mention_speaker_base_rate_maker_filter_repro: independent reproduction of the r5 claim (written from the claim's
description only; the claimant's r5_* code is not imported or read).

Claim: in seeded-book mention markets the maker NO (sell YES at ask - 1c) is adversely selected. Keeping only words with a
low prior hit rate (same speaker key and word key, other events, close <= event listing - 1 h; buckets NH n=0, LOW20 k/n<=0.20,
LOW40 k/n<=0.40) should remove informed fills. Validation (pooled EVAL = last 30% of each sample's events by listing time).

Samples (one order per ticker; first sample in the order S1..S4 keeps a duplicate):
  S1  round-2 frozen C1, rebuilt here from the raw round-2 caches: at the first hourly candle H >= open + 1 h with the market
      open, no market of the event closed, 0 < bid <= ask < 1 and s = ask - 0.01 > bid, rest SELL-YES at s. Filled if a later
      hourly candle (H, first close + 1 h] (and <= market close + 1 h) has trade HIGH > s. Rule-consistent entries only
      (drop markets whose open precedes the candle fetch window start, which was set by the event's future last close).
      Size proxy: min(N, volume of the through candles), N = floor(5 / (1 - s)).
  S2  round-3 verifier rows (r3_earnings_call_mentions/repro/rows_base.jsonl): fill = fill_thr, size = min(n_order, vol_proxy).
  S3  round-4 earnings arm A on the 79 fresh archive events: the round-4 sample builder (archive_orders / arm_rows of
      r4_earnings_seeded_book_48h_forward, NOT the claimant's code) gives the through-only filled contracts f.
  S4  round-4 single-appearance orders.jsonl, cell P, sell, through-only (exact prints override candle evidence).
Returns: maker, fee 0 ('quadratic' series), equal-$ per filled order = (NO won - q) / q with q = 1 - s; size-weighted too.
Pool for priors: every settled *MENTION* market in the on-disk round 2-4 caches (jsonl files and api_cache listings) plus
the r5 family's /historical listings (its api_cache). Kalshi calls: 0.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_mention_speaker_base_rate_maker_filter_repro
"""
from __future__ import annotations
import datetime as dt, glob, json, math, random, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
D = ROOT / "data/kalshi_lab/strategies"
OUT = D / "r5_mention_speaker_base_rate_maker_filter/repro"
SPLIT = 0.7
BUCKETS = ("ALL", "NH", "LOW20", "LOW40", "HIGH40")
CANDS = ("LOW20", "LOW40", "NH")


def ts(s):
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(s)
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def wkey(w: str) -> str:
    parts = sorted(p.strip() for p in re.sub(r"[^a-z0-9/+() ]", "", (w or "").lower()).split("/") if p.strip())
    return "/".join(parts)


def spk(series: str) -> str:
    return re.sub(r"MENTIONB$", "MENTION", series)


# ------------------------------------------------------------------------------------------------ settlement pool
def _add(P, t, e, series, op, cl, res, word):
    if res not in ("yes", "no") or not t or "MENTION" not in (series or ""):
        return
    r = P.get(t)
    if r is None:
        P[t] = {"t": t, "e": e, "spk": spk(series), "w": wkey(word), "open": op, "close": cl, "y": res == "yes"}
    else:
        if r["open"] is None and op is not None:
            r["open"] = op
        if r["close"] is None and cl is not None:
            r["close"] = cl


def _api_row(P, m):
    e = m.get("event_ticker") or ""
    series = m.get("series") or e.split("-")[0]
    word = (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""
    _add(P, m.get("ticker"), e, series, ts(m.get("open_time")), ts(m.get("close_time")), m.get("result"), word)


def build_pool(with_forward: bool = False) -> dict:
    P = {}
    jsonl = ["r2_mentions_baserate/markets_hist.jsonl", "r2_mentions_baserate/markets_recent.jsonl",
             "r3_earnings_call_mentions/markets_hist.jsonl", "r3_earnings_call_mentions/markets_live.jsonl",
             "r3_frozen_c1_other_mention_formats/markets.jsonl", "r4_single_appearance_seeded_books/markets.jsonl",
             "r3_mentions_maker_forward/settled.jsonl"]
    for f in jsonl:
        for l in (D / f).open():
            m = json.loads(l)
            if "ticker" in m:
                _api_row(P, m)
            else:   # short format {t, e, series, open, close, result, sub}
                _add(P, m["t"], m["e"], m["series"], m.get("open"), m.get("close"), m.get("result"), m.get("sub") or "")
    for f in sorted(glob.glob(str(D / "*/api_cache/*.json"))):
        if "/repro/" in f:
            continue
        try:
            x = json.load(open(f))
        except Exception:
            continue
        if isinstance(x, dict) and isinstance(x.get("markets"), list):
            for m in x["markets"]:
                if isinstance(m, dict):
                    _api_row(P, m)
    if with_forward:
        for l in (D / "r5_mention_speaker_base_rate_maker_filter/settled_fwd.jsonl").open():
            m = json.loads(l)
            if m["t"] not in P:
                P[m["t"]] = {"t": m["t"], "e": m["e"], "spk": m["spk"], "w": wkey(m["w"]), "open": m.get("open"), "close": m["close"], "y": bool(m["yes"])}
    return P


class Priors:
    def __init__(self, P):
        self.by = defaultdict(list); self.ev_open = defaultdict(lambda: None)
        for r in P.values():
            if r["close"] is not None:
                self.by[(r["spk"], r["w"])].append((r["close"], r["y"], r["e"]))
            if r["open"] is not None:
                o = self.ev_open[r["e"]]
                self.ev_open[r["e"]] = r["open"] if o is None else min(o, r["open"])

    def kn(self, s, w, L, e):
        x = [y for c, y, ee in self.by.get((s, w), ()) if c <= L - 3600 and ee != e]
        return sum(x), len(x)


def bucket_of(k, n):
    out = {"ALL"}
    if n == 0:
        out.add("NH")
    else:
        r = k / n
        if r <= 0.20:
            out.add("LOW20")
        if r <= 0.40:
            out.add("LOW40")
        if r > 0.40:
            out.add("HIGH40")
    return out


# ------------------------------------------------------------------------------------------------ samples
def s1_orders() -> list[dict]:
    """Round-2 C1 rebuilt from raw caches (own implementation of the published frozen rule)."""
    R2 = D / "r2_mentions_baserate"
    C = {}
    for l in (R2 / "candles.jsonl").open():
        x = json.loads(l); C[x["t"]] = x["c"]
    F = {}
    for l in (R2 / "candles_full.jsonl").open():
        x = json.loads(l); F[x["t"]] = x["c"]
    mk = [json.loads(l) for l in (R2 / "markets_recent.jsonl").open()]
    mk = [m for m in mk if m["series"] != "KXTRUMPSAY" and m["ticker"] in C]
    ev = defaultdict(list)
    for m in mk:
        m["o"], m["c"] = ts(m["open_time"]), ts(m["close_time"]); ev[m["event_ticker"]].append(m)
    fc = {e: min(m["c"] for m in x) for e, x in ev.items()}
    lo = {e: (max(min(m["o"] for m in x), max(m["c"] for m in x) - 96 * 3600) // 3600) * 3600 - 3600 for e, x in ev.items()}
    out = []; ntrunc = 0
    for m in mk:
        e = m["event_ticker"]; first = None
        for r in C[m["ticker"]]:
            H, a, b = r[0], r[1], r[2]
            if H < m["o"] + 3600 or H >= m["c"] or H >= fc[e] or a is None or b is None:
                continue
            if not (0 < b <= a < 1):
                continue
            first = (H, a, b); break
        if first is None:
            continue
        H, a, b = first
        s = round(a - 0.01, 2)
        if not s > b:
            continue
        if m["o"] < lo[e]:
            ntrunc += 1; continue
        end = min(fc[e] + 3600, m["c"] + 3600)
        thr = [r for r in F.get(m["ticker"], []) if H < r[0] <= end and r[6] is not None and r[6] > s + 1e-9]
        N = int(5 // (1 - s))
        full = any(r[6] >= s + 0.02 - 1e-9 and r[4] >= 20 for r in thr)
        out.append({"S": "S1", "t": m["ticker"], "e": e, "series": m["series"],
                    "word": (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or "",
                    "open": m["o"], "post": H, "s": s, "q": round(1 - s, 4), "won": m["result"] == "no",
                    "filled": bool(thr), "size": float(min(N, sum(r[4] for r in thr))) if thr else 0.0, "N": N,
                    "full": full, "first_close": fc[e],
                    "fill_time": thr[0][0] if thr else None})
    print(f"S1: {len(out)} rule-consistent posted orders ({ntrunc} truncated dropped), {len({o['e'] for o in out})} events")
    return out


def s2_orders() -> list[dict]:
    out = []
    for l in (D / "r3_earnings_call_mentions/repro/rows_base.jsonl").open():
        r = json.loads(l)
        out.append({"S": "S2", "t": r["t"], "e": r["e"], "series": r["e"].split("-")[0], "word": r["word"], "open": None,
                    "post": r["post"], "s": r["s"], "q": round(1 - r["s"], 4), "won": bool(r["won"]), "filled": bool(r["fill_thr"]),
                    "size": float(min(r["n_order"], r["vol_proxy"] or 0)), "N": r["n_order"], "first_close": r["t_close"],
                    "cancel": r["cancel"]})
    return out


def s3_orders() -> list[dict]:
    from lab.kalshi.strategies import r4_earnings_seeded_book_48h_forward as R4   # the round-4 sample builder
    import os
    cwd = os.getcwd(); os.chdir(ROOT)
    try:
        base, _ = R4.archive_orders(0)
        rows = {r["t"]: r for r in R4.arm_rows(base, None, "through")}
    finally:
        os.chdir(cwd)
    plan = {r["t"]: r for r in json.loads((D / "r4_earnings_seeded_book_48h_forward/plan.json").read_text())["rows"]}
    out = []
    for o in base:
        f = rows[o["t"]]["f"]
        if f is None:
            continue
        out.append({"S": "S3", "t": o["t"], "e": o["e"], "series": o["series"], "word": plan[o["t"]]["word"], "open": o["open"],
                    "post": o["post"], "s": o["s"], "q": o["q"], "won": bool(o["won"]), "filled": f > 0, "size": float(f), "N": o["N"],
                    "first_close": o["tclose"], "cancel": o["cancel_A"]})
    return out


def s4_orders(P) -> list[dict]:
    out = []
    for l in (D / "r4_single_appearance_seeded_books/orders.jsonl").open():
        o = json.loads(l)
        if not o.get("posted"):
            continue
        f = o["P"]; x = o.get("xP")
        if x is not None:
            vol = x["thr"]; hit = vol > 0
        else:
            vol = f["vthr"]; hit = bool(f["thr"])
        word = ""
        if o["t"] in P:
            word = None   # word key taken from the pool row below
        out.append({"S": "S4", "t": o["t"], "e": o["e"], "series": o["series"], "word": word, "open": None, "post": o["H1"],
                    "s": o["s"], "q": round(1 - o["s"], 4), "won": not o["yes"], "filled": hit,
                    "size": float(min(o["N"], vol)) if hit and vol > 0 else 0.0, "N": o["N"], "first_close": o["ev_first_close"],
                    "cancel": o["C"]})
    return out


# ------------------------------------------------------------------------------------------------ statistics
def binom_lo(k, n, a=0.05):
    """One-sided exact (Clopper-Pearson) lower bound: p with P(X >= k | n, p) = a."""
    if k == 0:
        return 0.0
    def tail(p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < a:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def rstats(rows: list[dict]) -> dict:
    """Filled orders: equal-$ return per order, event-clustered t (calib style: t of event means) and a cluster-robust t
    of the order-level mean, mean without the 3 best, halves by listing time, size-weighted, binomial bound (first order
    per event), capacity."""
    if not rows:
        return {"n": 0}
    for r in rows:
        r["ret"] = ((1.0 if r["won"] else 0.0) - r["q"]) / r["q"]
    n = len(rows); mean = st.mean(r["ret"] for r in rows)
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    t_ev = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))) if len(em) > 2 and st.pstdev(em) > 0 else float("nan")
    G = len(ev)
    ss = sum(sum(x - mean for x in v) ** 2 for v in ev.values())
    se = math.sqrt(ss * G / (G - 1)) / n if G > 1 else float("nan")
    t_cr = mean / se if se and se > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: (r["L"], r["t"]))
    h1, h2 = srt[: n // 2], srt[n // 2:]
    sw_cost = sum(r["size"] * r["q"] for r in rows)
    sw = sum(r["size"] * ((1.0 if r["won"] else 0.0) - r["q"]) for r in rows) / sw_cost if sw_cost > 0 else float("nan")
    first = {}
    for r in sorted(rows, key=lambda r: (r["post"], r["t"])):
        first.setdefault(r["e"], r)
    fk = sum(r["won"] for r in first.values()); fn = len(first); fpx = st.mean(r["q"] for r in first.values())
    plo = binom_lo(fk, fn)
    span = (max(r["L"] for r in rows) - min(r["L"] for r in rows)) / 86400
    return {"n": n, "events": G, "win": round(sum(r["won"] for r in rows) / n, 4), "avg_px": round(st.mean(r["q"] for r in rows), 4),
            "ret_per_dollar": round(mean, 4), "t_event_means": round(t_ev, 2), "t_cluster_robust": round(t_cr, 2),
            "ret_wo3": round(st.mean(rs[3:]), 4) if n > 3 else None,
            "half1": round(st.mean(r["ret"] for r in h1), 4) if h1 else None, "half2": round(st.mean(r["ret"] for r in h2), 4) if h2 else None,
            "size_weighted_ret": round(sw, 4), "median_filled_contracts": st.median(r["size"] for r in rows),
            "binom_events": fn, "binom_wins": fk, "binom_first_px": round(fpx, 4), "beta_lo_ret": round(plo / fpx - 1, 4),
            "listing_span_days": round(span, 1), "trades_per_day": round(n / span, 2) if span > 0 else None}


def mh_lor(strata):
    """Mantel-Haenszel log OR of being filled, YES-outcome vs NO-outcome orders; strata = list of order lists."""
    num = den = 0.0
    for rows in strata:
        a = sum(1 for r in rows if not r["won"] and r["filled"]); b = sum(1 for r in rows if not r["won"] and not r["filled"])
        c = sum(1 for r in rows if r["won"] and r["filled"]); d = sum(1 for r in rows if r["won"] and not r["filled"])
        if min(a, b, c, d) == 0:
            a, b, c, d = a + 0.5, b + 0.5, c + 0.5, d + 0.5
        nn = a + b + c + d
        if nn == 0:
            continue
        num += a * d / nn; den += b * c / nn
    return math.log(num / den) if num > 0 and den > 0 else float("nan")


def gap(rows):
    f = [r["won"] for r in rows if r["filled"]]; u = [r["won"] for r in rows if not r["filled"]]
    return (st.mean(u) - st.mean(f)) if f and u else None


def as_stats(orders, bucket):
    """Stratified (by sample) win gap of the bucket vs ALL on the bucket's weights; MH log-OR ratio."""
    by_s = defaultdict(list)
    for o in orders:
        by_s[o["S"]].append(o)
    gb = ga = W = 0.0; sb, sa = [], []
    for S, rows in by_s.items():
        rb = [r for r in rows if bucket in r["B"]]
        g1, g0 = gap(rb), gap(rows)
        if g1 is None or g0 is None:
            continue
        w = len(rb); gb += w * g1; ga += w * g0; W += w; sb.append(rb); sa.append(rows)
    if W == 0:
        return {"R": None}
    lb, la = mh_lor(sb), mh_lor(sa)
    rb_all = [r for r in orders if bucket in r["B"]]
    fl = [r["won"] for r in rb_all if r["filled"]]; uf = [r["won"] for r in rb_all if not r["filled"]]
    return {"gap_bucket": round(gb / W, 4), "gap_all_same_w": round(ga / W, 4), "R": round(gb / ga, 4) if ga else None,
            "lor_bucket": round(lb, 4), "lor_all": round(la, 4), "R_LOR": round(lb / la, 4) if la else None,
            "orders": len(rb_all), "filled": len(fl), "unfilled": len(uf),
            "filled_NO_win": round(st.mean(fl), 4) if fl else None, "unfilled_NO_win": round(st.mean(uf), 4) if uf else None}


def boot(orders, bucket, reps=1000, seed=20261008):
    """Event-cluster bootstrap within sample of R and R_LOR (90% interval)."""
    rnd = random.Random(seed)
    by = defaultdict(lambda: defaultdict(list))
    for o in orders:
        by[o["S"]][o["e"]].append(o)
    Rs, Ls = [], []
    for _ in range(reps):
        res = []
        for S, evs in by.items():
            keys = list(evs)
            for _k in range(len(keys)):
                res.extend(evs[keys[rnd.randrange(len(keys))]])
        a = as_stats(res, bucket)
        if a.get("R") is not None and a.get("R_LOR") is not None and not math.isnan(a["R_LOR"]):
            Rs.append(a["R"]); Ls.append(a["R_LOR"])
    Rs.sort(); Ls.sort()
    q = lambda v, p: v[min(len(v) - 1, int(p * len(v)))] if v else None
    return {"R_90": [q(Rs, 0.05), q(Rs, 0.95)], "R_LOR_90": [q(Ls, 0.05), q(Ls, 0.95)],
            "P_R_le_0.5": round(sum(x <= 0.5 for x in Rs) / len(Rs), 3) if Rs else None}


# ------------------------------------------------------------------------------------------------ main
def assemble(with_forward=False, split_round=False):
    P = build_pool(with_forward)
    PR = Priors(P)
    S = s1_orders() + s2_orders() + s3_orders() + s4_orders(P)
    seen = set(); orders = []
    for o in S:
        if o["t"] in seen:
            continue
        seen.add(o["t"]); orders.append(o)
    miss_word = 0
    for o in orders:
        if not o["word"]:
            pr = P.get(o["t"])
            o["w"] = pr["w"] if pr else ""
            miss_word += pr is None
        else:
            o["w"] = wkey(o["word"])
        o["spk"] = spk(o["series"])
        L = PR.ev_open[o["e"]]
        own = o["open"] if o["open"] is not None else None
        if L is None:
            L = own if own is not None else o["post"] - 3600
        elif own is not None:
            L = min(L, own)
        o["L"] = L
        k, n = PR.kn(o["spk"], o["w"], L, o["e"])
        o["k"], o["nh"] = k, n
        o["B"] = bucket_of(k, n)
    # per-sample split by event listing time
    for Sname in ("S1", "S2", "S3", "S4"):
        evs = sorted({(o["L"], o["e"]) for o in orders if o["S"] == Sname})
        cut = int(round(len(evs) * SPLIT)) if split_round else int(len(evs) * SPLIT)
        conf = {e for _, e in evs[:cut]}
        for o in orders:
            if o["S"] == Sname:
                o["part"] = "CONFIRM" if o["e"] in conf else "EVAL"
    return P, orders, miss_word


def run(with_forward=False, split_round=False, do_boot=True, verbose=True):
    P, orders, miss_word = assemble(with_forward, split_round)
    res = {"pool_settled_mention_markets": len(P), "orders": len(orders), "events": len({o["e"] for o in orders}),
           "orders_by_sample": {s: sum(o["S"] == s for o in orders) for s in ("S1", "S2", "S3", "S4")},
           "events_by_sample": {s: len({o["e"] for o in orders if o["S"] == s}) for s in ("S1", "S2", "S3", "S4")},
           "orders_without_word": miss_word, "parts": {}}
    for part in ("CONFIRM", "EVAL"):
        Op = [o for o in orders if o["part"] == part]
        pr = {"orders": len(Op), "events": len({o["e"] for o in Op})}
        for b in BUCKETS:
            fl = [dict(o) for o in Op if b in o["B"] and o["filled"]]
            a = as_stats(Op, b)
            pr[b] = {"as": a, "returns_filled": rstats(fl)}
            if do_boot and part == "EVAL" and b in CANDS:
                pr[b]["as"].update(boot(Op, b))
        # S1 full fills only (>= 2c through with >= 20 contracts in the candle)
        for b in ("ALL", "LOW20"):
            pr[f"S1_full_only|{b}"] = rstats([dict(o) for o in Op if o["S"] == "S1" and b in o["B"] and o["filled"] and o["full"]])
            pr[f"S1_any|{b}"] = rstats([dict(o) for o in Op if o["S"] == "S1" and b in o["B"] and o["filled"]])
        # pre-cancel samples (S2-S4) by bucket, descriptive
        for b in ("ALL", "LOW40", "LOW20", "NH"):
            sub = [o for o in Op if o["S"] != "S1"]
            pr[f"preS2S4|{b}"] = {"as": as_stats(sub, b), "returns_filled": rstats([dict(o) for o in sub if b in o["B"] and o["filled"]])}
        res["parts"][part] = pr
        if verbose:
            print(f"\n== {part}: {pr['orders']} orders, {pr['events']} events")
            for b in BUCKETS:
                a = pr[b]["as"]; r = pr[b]["returns_filled"]
                print(f"  {b:6s} R={a.get('R')} R_LOR={a.get('R_LOR')} {('90% R ' + str(a.get('R_90')) + ' R_LOR ' + str(a.get('R_LOR_90'))) if 'R_90' in a else ''}"
                      f" | filledNOwin={a.get('filled_NO_win')} unfilled={a.get('unfilled_NO_win')} orders={a.get('orders')}")
                if r.get("n"):
                    print(f"         n={r['n']} ev={r['events']} win={r['win']} px={r['avg_px']} ret={r['ret_per_dollar']:+.4f} t_ev={r['t_event_means']} "
                          f"t_cr={r['t_cluster_robust']} wo3={r['ret_wo3']} h={r['half1']}/{r['half2']} sw={r['size_weighted_ret']} "
                          f"medq={r['median_filled_contracts']} LB={r['beta_lo_ret']} tpd={r['trades_per_day']}")
            for k in ("S1_full_only|ALL", "S1_full_only|LOW20", "S1_any|ALL", "S1_any|LOW20"):
                r = pr[k]
                if r.get("n"):
                    print(f"  {k:20s} n={r['n']} ret={r['ret_per_dollar']:+.4f} t_ev={r['t_event_means']} sw={r['size_weighted_ret']}")
            for b in ("ALL", "LOW40", "LOW20", "NH"):
                r = pr[f"preS2S4|{b}"]["returns_filled"]
                if r.get("n"):
                    print(f"  preS2S4|{b:5s} n={r['n']} ev={r['events']} win={r['win']} px={r['avg_px']} ret={r['ret_per_dollar']:+.4f} sw={r['size_weighted_ret']} LB={r['beta_lo_ret']}")
    return res, orders


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    res, orders = run()
    sens = {}
    for name, kw in (("split_round", {"split_round": True}), ("pool_with_forward_rows", {"with_forward": True})):
        r2, _ = run(do_boot=False, verbose=False, **kw)
        sens[name] = {b: r2["parts"]["EVAL"][b]["returns_filled"] | {"R": r2["parts"]["EVAL"][b]["as"].get("R"),
                                                                       "R_LOR": r2["parts"]["EVAL"][b]["as"].get("R_LOR")} for b in ("ALL",) + CANDS}
    res["sensitivity_EVAL"] = sens
    (OUT / "repro.json").write_text(json.dumps(res, indent=1, default=str))
    with (OUT / "orders_repro.jsonl").open("w") as fh:
        for o in orders:
            fh.write(json.dumps({k: (sorted(v) if isinstance(v, set) else v) for k, v in o.items()}) + "\n")
    print("\nwrote", OUT / "repro.json")


if __name__ == "__main__":
    main()
