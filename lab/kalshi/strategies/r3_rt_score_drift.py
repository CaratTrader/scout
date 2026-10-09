"""r3_rt_score_drift: do KXRT (Rotten Tomatoes) ladders misprice the post-embargo drift of the Tomatometer?

Hypothesis. After the review embargo lifts, the Tomatometer moves as more reviews arrive (on average down), while fans
anchor on the score shown. A beta-binomial model of the final score, given the current liked/total counts and the time
left, should then beat the Kalshi mid days before the Monday 10:00 ET settlement.

Data (all cached under data/kalshi_lab/strategies/r3_rt_score_drift/, see r3_rt_score_drift_data.py):
  * 76 KXRT events (Jan-Oct 2026), settled markets from /historical + the live list.
  * RT score paths: Wayback Machine captures of each film's RT page (criticsScore likedCount/notLikedCount), i.e. the
    page as a home poller would have seen it at the capture instant.
  * Kalshi hourly candles: event candles for the live-listed events (all strikes); /historical per-market candles for
    up to 3 strikes per archived event, picked by distance to the RT score at the decision captures (known at t).

Clock discipline. A decision is made at a capture instant t_c (the only instants the score is known). The model uses
captures with t <= t_c. The Kalshi quote used for the mid and for fills is the market state at t_c + 60 min (hourly
candles; the last candle ending <= t_c + 3600), so the market always has at least as much information as the model.
The fee is 0.07 p (1-p) per contract, rounded up to the cent per 10-lot order.

Protocol. Events sorted by close; discovery = first 70%, validation = last 30%. The drift model (review growth and
future positive rate) is fitted on discovery films only. Kill test (pre-registered in the task): on discovery, the model
must beat the mid's Brier by >= 0.02 at embargo+24h and embargo+72h (contested strikes, mid in [0.05, 0.95]).
Usage: python lab/kalshi/strategies/r3_rt_score_drift.py"""
from __future__ import annotations
import json, math, random, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/r3_rt_score_drift")
CAND = OUT / "candles"
SPLIT = 0.7
H = 3600
STALE_Q = 24 * H          # hourly candles only exist when the quote or volume changed: carry forward <= 24 h
FEE = 0.07


def fee(p: float) -> float:
    return FEE * p * (1 - p)


def fee_order(p: float, n: int = 10) -> float:
    """Per-contract fee for an n-lot order, rounded up to the cent per order (Kalshi)."""
    return math.ceil(round(FEE * p * (1 - p) * n * 100, 6)) / 100 / n


def rt_round(x: float) -> int:
    return int(math.floor(x + 0.5 + 1e-9))


# ------------------------------------------------------------------ loading
def load():
    fl = json.loads((OUT / "films.json").read_text())
    ms = [json.loads(l) for l in (OUT / "markets.jsonl").open()]
    mk = defaultdict(list)
    for m in ms:
        mk[m["e"]].append(m)
    caps = defaultdict(dict)
    for l in (OUT / "captures.jsonl").open():
        x = json.loads(l)
        if x.get("n") is not None and x.get("http") == 200 and x.get("ts"):
            caps[x["e"]][x["ts"]] = x
    paths = {}
    for e, r in fl.items():
        by_dig = {}
        for ts, _, dig, _ in r.get("cdx") or []:
            if ts in caps[e]:
                by_dig[dig] = caps[e][ts]
        pts = {}
        for ts, x in caps[e].items():
            pts[ts] = x
        for ts, _, dig, _ in r.get("cdx") or []:
            if ts not in pts and dig in by_dig:      # identical digest = identical page: same counts at a later instant
                pts[ts] = by_dig[dig]
        rows = [{"t": int(_ts(ts)), "n": x["n"], "liked": x["liked"], "score": x["score"]} for ts, x in pts.items()]
        rows.sort(key=lambda z: z["t"])
        paths[e] = rows
    cand = {}
    for f in CAND.glob("*.json"):
        cand[f.stem] = {tk: sorted(v) for tk, v in json.loads(f.read_text()).items()}
    return fl, mk, paths, cand


def _ts(ts: str) -> float:
    import calendar, time
    return calendar.timegm(time.strptime(ts, "%Y%m%d%H%M%S"))


def final_score(r: dict) -> int | None:
    lo, hi = r["final_bounds"]
    if lo is not None and hi is not None and lo == hi:
        return lo
    for v in r["value_raw"]:
        try:
            v = int(v)
        except Exception:
            continue
        if (lo is None or v >= lo) and (hi is None or v <= hi):
            return v
    return None


def embargo(path: list[dict]) -> int | None:
    for c in path:
        if c["n"] >= 5 and c["score"] is not None:
            return c["t"]
    return None


def final_counts(path: list[dict], close: int, s_final: int | None):
    """Counts at settlement: the capture nearest to close within +-36 h whose score matches the settled score if known."""
    best = None
    for c in path:
        if abs(c["t"] - close) <= 36 * H and c["n"] >= 5:
            ok = s_final is None or c["score"] == s_final
            key = (not ok, abs(c["t"] - close))
            if best is None or key < best[0]:
                best = (key, c)
    return best[1] if best else None


def quote_at(c: list[list], t: int):
    """(ask, bid) of the last hourly candle ending <= t, if it is no older than STALE_Q."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > STALE_Q or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


# ------------------------------------------------------------------ model
def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


GH = [(-2.0201828704560856, 0.019953242059045913), (-0.9585724646138185, 0.39361932315224116), (0.0, 0.9453087204829419),
      (0.9585724646138185, 0.39361932315224116), (2.0201828704560856, 0.019953242059045913)]   # Gauss-Hermite (5 nodes, weight e^-x^2)
GH_N = [(x * math.sqrt(2), w / math.sqrt(math.pi)) for x, w in GH]                        # for a standard normal


def hbucket(h_close: float) -> int:
    for i, b in enumerate((24, 48, 72, 120, 1e9)):
        if h_close < b:
            return i
    return 4


class DriftModel:
    """Final score = round(100 (L + X) / (N + M)); M = N (e^g - 1) with g drawn from the discovery distribution of
    log(N_final / N_t) in the same hours-to-close bucket; X ~ Binomial(M, q) with logit q ~ N(a + b logit p_t, s^2)."""

    def __init__(self, obs: list[dict], shrink_n: float = 0.0):
        self.g = defaultdict(list)
        for o in obs:
            self.g[hbucket(o["h_close"])].append(math.log(o["Nf"] / o["N"]))
        for k in list(self.g):
            v = sorted(self.g[k]); qs = [v[min(len(v) - 1, int(len(v) * (i + 0.5) / 15))] for i in range(15)]
            self.g[k] = qs
        # future positive rate: weighted least squares on film-level points (each future block weighted by its size, capped)
        pts = [(logit((o["L"] + 0.5) / (o["N"] + 1)), logit((o["Lf"] - o["L"] + 0.5) / (o["Nf"] - o["N"] + 1)), min(o["Nf"] - o["N"], 60), o["Nf"] - o["N"])
               for o in obs if o["Nf"] - o["N"] >= 5]
        sw = sum(w for _, _, w, _ in pts)
        mx = sum(w * x for x, _, w, _ in pts) / sw; my = sum(w * y for _, y, w, _ in pts) / sw
        sxx = sum(w * (x - mx) ** 2 for x, _, w, _ in pts); sxy = sum(w * (x - mx) * (y - my) for x, y, w, _ in pts)
        self.b = sxy / sxx if sxx > 0 else 1.0; self.a = my - self.b * mx
        # between-film spread of the future rate: residual variance minus the binomial sampling part 1/(M q (1-q))
        ex = []
        for x, y, w, M in pts:
            qf = expit(self.a + self.b * x); r = y - self.a - self.b * x
            ex.append(r * r - 1 / (M * qf * (1 - qf)))
        self.s = math.sqrt(max(st.mean(ex), 0.01)) if ex else 0.3
        self.s_raw = math.sqrt(st.mean((y - self.a - self.b * x) ** 2 for x, y, _, _ in pts)); self.npts = len(pts)

    def variant(self, lam: float, s: float) -> "DriftModel":
        """Same growth distribution; drift scaled by lam (0 = future reviews keep the current rate) and spread s."""
        import copy
        m = copy.copy(self); m.a = self.a * lam; m.b = 1 + (self.b - 1) * lam; m.s = s; m.lam = lam
        return m

    def dist(self, L: int, N: int, h_close: float) -> list[float]:
        """pmf of the final score 0..100."""
        gs = self.g.get(hbucket(h_close)) or self.g[max(self.g)]
        pmf = [0.0] * 101
        mu = self.a + self.b * logit((L + 0.5) / (N + 1))
        wg = 1 / len(gs)
        for g in gs:
            M = max(0, int(round(N * (math.exp(g) - 1))))
            for z, w in GH_N:
                q = expit(mu + self.s * z)
                if M == 0:
                    pmf[rt_round(100 * L / N)] += wg * w; continue
                # binomial pmf over X = 0..M (log space for stability)
                lq, l1q = math.log(max(q, 1e-12)), math.log(max(1 - q, 1e-12))
                lc = 0.0
                for x in range(M + 1):
                    if x > 0:
                        lc += math.log((M - x + 1) / x)
                    p = math.exp(lc + x * lq + (M - x) * l1q)
                    if p > 1e-12:
                        pmf[rt_round(100 * (L + x) / (N + M))] += wg * w * p
        s = sum(pmf)
        return [p / s for p in pmf]


def p_above(pmf: list[float], k: float) -> float:
    return sum(p for s, p in enumerate(pmf) if s > k)


# ------------------------------------------------------------------ dataset
def build(fl, mk, paths, cand):
    evs = sorted(fl, key=lambda e: (fl[e]["close"], e))
    closes = sorted(fl[e]["close"] for e in evs)
    cut = closes[int(len(closes) * SPLIT)]
    info = {}
    for e in evs:
        r = fl[e]; p = paths.get(e) or []
        sf = final_score(r); E = embargo(p); fc = final_counts(p, r["close"], sf)
        info[e] = {"close": r["close"], "disc": r["close"] < cut, "S": sf, "E": E, "fc": fc, "path": p, "open": min(m["open"] for m in mk[e])}
    return evs, cut, info


def model_obs(info, evs, disc_only=True):
    """(N, L, h_close, Nf, Lf) at every capture after the embargo and >= 1 h before close, one film-capture per row."""
    obs = []
    for e in evs:
        x = info[e]
        if (disc_only and not x["disc"]) or x["E"] is None or x["fc"] is None:
            continue
        Nf, Lf = x["fc"]["n"], x["fc"]["liked"]
        for c in x["path"]:
            if c["t"] < x["E"] or c["t"] > x["close"] - H or c["n"] < 5:
                continue
            if Nf < c["n"] or Lf < c["liked"] or Nf - c["n"] < Lf - c["liked"]:
                continue          # reviews removed or re-graded between the two captures: skip the pair
            obs.append({"e": e, "N": c["n"], "L": c["liked"], "h_close": (x["close"] - c["t"]) / H, "Nf": Nf, "Lf": Lf})
    return obs


def capture_at(path, t, max_age=18 * H):
    best = None
    for c in path:
        if c["t"] <= t:
            best = c
    if best and t - best["t"] <= max_age and best["n"] >= 5:
        return best
    return None


def first_capture_after(path, t, within=24 * H):
    for c in path:
        if t <= c["t"] <= t + within and c["n"] >= 5:
            return c
    return None


def decision_rows(info, mk, cand, model, e, c, tag):
    """Model vs mid for every strike of event e at capture c (decision at c.t, quote at c.t + 1 h)."""
    x = info[e]; out = []
    h_close = (x["close"] - c["t"]) / H
    if h_close < 1:
        return out
    pmf = model.dist(c["liked"], c["n"], h_close) if model else None
    tq = c["t"] + H
    for m in mk[e]:
        if m["type"] != "greater" or m["result"] not in ("yes", "no") or m["open"] > c["t"]:
            continue
        cs = (cand.get(e) or {}).get(m["t"])
        if not cs:
            continue
        q = quote_at(cs, tq)
        if not q:
            continue
        ask, bid = q
        if not (0 < bid <= ask < 1) and not (bid == 0 and 0 < ask) and not (ask == 1 and bid > 0):
            continue
        mid = (ask + bid) / 2
        y = 1.0 if m["result"] == "yes" else 0.0
        pm = p_above(pmf, m["floor"]) if pmf else None
        naive = 1.0 if rt_round(100 * c["liked"] / c["n"]) > m["floor"] else 0.0
        out.append({"e": e, "t": m["t"], "k": m["floor"], "tag": tag, "tc": c["t"], "h_close": h_close, "N": c["n"], "S_c": c["score"],
                    "ask": ask, "bid": bid, "mid": mid, "y": y, "pm": pm, "naive": naive, "disc": x["disc"]})
    return out


def brier(rows, key):
    return st.mean((r[key] - r["y"]) ** 2 for r in rows) if rows else float("nan")


def cluster_t(vals_by_e: dict) -> float:
    em = [st.mean(v) for v in vals_by_e.values()]
    if len(em) < 3 or st.pstdev(em) == 0:
        return float("nan")
    return st.mean(em) / (st.pstdev(em) / math.sqrt(len(em)))


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "events": 0}
    ev = defaultdict(list)
    for r in trades:
        ev[r["e"]].append(r["ret"])
    rs = sorted((r["ret"] for r in trades), reverse=True)
    ts = sorted(r["tc"] for r in trades); mid = ts[len(ts) // 2]
    h1 = [r["ret"] for r in trades if r["tc"] < mid]; h2 = [r["ret"] for r in trades if r["tc"] >= mid]
    return {"n": len(trades), "events": len(ev), "win": st.mean(r["won"] for r in trades), "avg_px": st.mean(r["px"] for r in trades),
            "ret_per_dollar": st.mean(rs), "t": cluster_t(ev), "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan")}


def rt_only_rows(info, mk, evs, which="disc"):
    """Every (capture after the anchor, >= 1 h before close) x (every 'greater' strike) of the chosen films, with the
    Kalshi result as outcome. Needs no Kalshi prices: used to tune the model's drift scale and spread on discovery."""
    out = []
    for e in evs:
        x = info[e]
        if x["E"] is None or (which == "disc") != x["disc"]:
            continue
        A = max(x["E"], x["open"])
        ks = [(m["floor"], 1.0 if m["result"] == "yes" else 0.0) for m in mk[e] if m["type"] == "greater" and m["result"] in ("yes", "no")]
        for c in x["path"]:
            if c["t"] < A or c["n"] < 5 or c["t"] > x["close"] - H:
                continue
            out.append({"e": e, "L": c["liked"], "N": c["n"], "h": (x["close"] - c["t"]) / H, "ks": ks})
    return out


def tune(model, rows, lams=(0.0, 0.5, 1.0, 1.5), ss=(0.1, 0.2, 0.3, 0.45, 0.6)):
    """Log loss of P(final > K) over rt_only_rows for each (drift scale, spread); film-weighted (each film counts once)."""
    res = {}
    for lam in lams:
        for s_ in ss:
            m = model.variant(lam, s_); by_e = defaultdict(list)
            for r in rows:
                pmf = m.dist(r["L"], r["N"], r["h"])
                for k, y in r["ks"]:
                    p = min(max(p_above(pmf, k), 1e-4), 1 - 1e-4)
                    by_e[r["e"]].append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
            res[(lam, s_)] = st.mean(st.mean(v) for v in by_e.values())
    return res


def kill_rows(info, mk, cand, model, evs, which="disc", offsets=(24, 72)):
    """Model vs mid at anchor + 24 h and anchor + 72 h (anchor = max(first score capture, first market open)); the
    decision capture is the first capture in [anchor + off, anchor + off + 18 h]."""
    rows = []
    for e in evs:
        x = info[e]
        if x["E"] is None or (which == "disc") != x["disc"]:
            continue
        A = max(x["E"], x["open"])
        for off in offsets:
            c = first_capture_after(x["path"], A + off * H)
            if c and c["t"] < x["close"] - H:
                rows += decision_rows(info, mk, cand, model, e, c, f"A+{off}h")
    return rows


def brier_report(rows, label, lo=0.05, hi=0.95):
    out = {}
    for tag in sorted({r["tag"] for r in rows}):
        rr = [r for r in rows if r["tag"] == tag and lo <= r["mid"] <= hi]
        if not rr:
            continue
        bm, bp, bn = brier(rr, "mid"), brier(rr, "pm"), brier(rr, "naive")
        # clustered SE of the Brier difference by event
        ev = defaultdict(list)
        for r in rr:
            ev[r["e"]].append((r["mid"] - r["y"]) ** 2 - (r["pm"] - r["y"]) ** 2)
        em = [st.mean(v) for v in ev.values()]
        se = st.pstdev(em) / math.sqrt(len(em)) if len(em) > 1 else float("nan")
        out[tag] = {"n": len(rr), "events": len(ev), "brier_mid": bm, "brier_model": bp, "brier_naive_current": bn, "gain": bm - bp, "gain_se": se,
                    "mean_y": st.mean(r["y"] for r in rr), "mean_mid": st.mean(r["mid"] for r in rr), "mean_model": st.mean(r["pm"] for r in rr)}
        print(f"  {label} {tag:7s} n={len(rr):4d} ev={len(ev):3d} Brier mid={bm:.4f} model={bp:.4f} naive={bn:.4f} gain={bm - bp:+.4f} (se {se:.4f}) "
              f"mean y={out[tag]['mean_y']:.3f} mid={out[tag]['mean_mid']:.3f} model={out[tag]['mean_model']:.3f}")
    return out


def decision_captures(x: dict, schedule) -> list[dict]:
    """Decision captures of one film. schedule: a tuple of hour offsets from the anchor (first capture in
    [A + off, A + off + 24 h]) or "all" (every scored capture from the anchor to close - 1 h)."""
    A = max(x["E"], x["open"])
    if schedule == "all":
        cs = [c for c in x["path"] if c["t"] >= A and c["n"] >= 5]
    else:
        cs = []
        for off in schedule:
            c = first_capture_after(x["path"], A + off * H)
            if c and c not in cs:
                cs.append(c)
    return [c for c in sorted(cs, key=lambda c: c["t"]) if c["t"] < x["close"] - H]


def trade_rule(info, mk, cand, model, evs, which, schedule=(24, 48, 72), edge=0.12, side_filter=None, delay_h=1):
    """At each decision capture take the side whose model edge after the fee is >= `edge`, at the quote delay_h hours
    after the capture, once per market (first trigger)."""
    trades = []; taken = set()
    for e in evs:
        x = info[e]
        if x["E"] is None or (which == "disc") != x["disc"]:
            continue
        for c in decision_captures(x, schedule):
            h_close = (x["close"] - c["t"]) / H
            pmf = None
            for m in mk[e]:
                if m["t"] in taken or m["type"] != "greater" or m["result"] not in ("yes", "no") or m["open"] > c["t"]:
                    continue
                cs = (cand.get(e) or {}).get(m["t"])
                if not cs:
                    continue
                q = quote_at(cs, c["t"] + delay_h * H)
                if not q:
                    continue
                ask, bid = q
                if pmf is None:
                    pmf = model.dist(c["liked"], c["n"], h_close)
                pm = p_above(pmf, m["floor"])
                y = m["result"] == "yes"
                for side, px, pw, won in (("YES", ask, pm, y), ("NO", 1 - bid, 1 - pm, not y)):
                    if side_filter and side != side_filter:
                        continue
                    if not (0.02 <= px <= 0.98):
                        continue
                    if pw - px - fee_order(px) >= edge:
                        pnl = (1.0 if won else 0.0) - px - fee_order(px)
                        trades.append({"e": e, "t": m["t"], "k": m["floor"], "side": side, "px": px, "pm": pw, "won": won, "ret": pnl / px,
                                       "tc": c["t"], "h_close": h_close, "S_c": c["score"], "N": c["n"]})
                        taken.add(m["t"]); break
    return trades


SCHEDULES = {"A0": (0,), "A24": (24,), "A24-72": (24, 48, 72), "A0-72": (0, 24, 48, 72), "all": "all"}
EDGES = (0.08, 0.12, 0.16, 0.20)
SIDES = (None, "YES", "NO")


def main(validate: bool = True):
    fl, mk, paths, cand = load()
    evs, cut, info = build(fl, mk, paths, cand)
    disc = [e for e in evs if info[e]["disc"]]; val = [e for e in evs if not info[e]["disc"]]
    res = {"split": {"events": len(evs), "discovery": len(disc), "validation": len(val), "cut_close_ts": cut}}
    print(f"{len(evs)} events; discovery {len(disc)} (close < {cut}), validation {len(val)}")
    cov = {k: {"score_path": sum(1 for e in g if info[e]["E"]), "final_counts": sum(1 for e in g if info[e]["fc"]), "candles": sum(1 for e in g if cand.get(e)),
               "captures": sum(len(info[e]["path"]) for e in g)} for k, g in (("disc", disc), ("val", val))}
    res["coverage"] = cov; print("coverage", cov)
    variants = 0
    # ---- drift model, fitted on discovery films only
    obs = model_obs(info, evs, disc_only=True)
    base = DriftModel(obs)
    print(f"drift model on {len({o['e'] for o in obs})} discovery films, {len(obs)} capture rows: a={base.a:+.3f} b={base.b:.3f} s={base.s:.3f} (raw {base.s_raw:.3f})")
    dd = defaultdict(list)
    for o in obs:
        dd[hbucket(o["h_close"])].append(rt_round(100 * o["Lf"] / o["Nf"]) - rt_round(100 * o["L"] / o["N"]))
    res["drift_by_hours_to_close"] = {}
    for k, lab in enumerate(("<24h", "24-48h", "48-72h", "72-120h", ">120h")):
        v = dd.get(k, [])
        if v:
            res["drift_by_hours_to_close"][lab] = {"n": len(v), "mean": st.mean(v), "median": st.median(v), "sd": st.pstdev(v),
                                                   "review_growth_x": math.exp(base.g[k][len(base.g[k]) // 2]) if k in base.g else None}
            print(f"   {lab:8s} final - current score: mean {st.mean(v):+.2f} median {st.median(v):+.1f} sd {st.pstdev(v):.2f} n={len(v)}")
    rows = rt_only_rows(info, mk, evs, "disc")
    tun = tune(base, rows); variants += len(tun)
    (lam, s_), ll = min(tun.items(), key=lambda kv: kv[1])
    model = base.variant(lam, s_)
    res["tuning"] = {"grid": {f"lam={k[0]},s={k[1]}": v for k, v in tun.items()}, "chosen": {"lam": lam, "s": s_, "logloss": ll}}
    print(f"tuned on discovery RT-only rows ({len(rows)} captures): drift scale {lam}, spread {s_}, log loss {ll:.4f} (no-drift best "
          f"{min(v for k, v in tun.items() if k[0] == 0):.4f})")
    # ---- kill test (pre-registered: A+24h and A+72h), plus A+0h / A+48h diagnostics
    print("\nKILL TEST (discovery): Brier on contested strikes (mid 0.05-0.95)")
    kr = kill_rows(info, mk, cand, model, evs, "disc", offsets=(0, 24, 48, 72)); variants += 4
    kt = brier_report(kr, "disc"); print("  all quoted strikes:"); kta = brier_report(kr, "disc-all", 0.0, 1.0)
    krb = kill_rows(info, mk, cand, base, evs, "disc", offsets=(24, 72)); print("  fitted (untuned) model:"); ktb = brier_report(krb, "disc-fit")
    kill_pass = all(kt.get(t, {}).get("gain", -1) >= 0.02 for t in ("A+24h", "A+72h"))
    res["kill_test"] = {"tuned": kt, "tuned_all_strikes": kta, "fitted_untuned": ktb, "pass": kill_pass,
                        "bar": "Brier(mid) - Brier(model) >= 0.02 at A+24h and A+72h, contested strikes, discovery"}
    print("KILL TEST", "PASS" if kill_pass else "FAIL")
    # ---- trade-rule grid on discovery (reported whether or not the kill test passed)
    grid = {}
    for sn, sch in SCHEDULES.items():
        for ed in EDGES:
            for sd in SIDES:
                tr = trade_rule(info, mk, cand, model, evs, "disc", sch, ed, sd); variants += 1
                grid[(sn, ed, sd or "both")] = (stats(tr), tr)
    print("\nDISCOVERY trade-rule grid (taker at the quote 1 h after the capture, fee rounded per 10-lot)")
    for k, (v, _) in sorted(grid.items(), key=lambda kv: -(kv[1][0].get("t") or -9) if kv[1][0].get("n", 0) >= 15 else 9):
        print(fmt(f"{k[0]} edge>={k[1]:.2f} {k[2]}", v))
    res["discovery_grid"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, (v, _) in grid.items()}
    # ---- freeze at most 3 candidates: C1 = the task's pre-specified rule; C2, C3 = best discovery t with n >= 15
    c1 = ("A24-72", 0.12, "both")
    ok = [(k, v) for k, (v, _) in grid.items() if v.get("n", 0) >= 15 and v.get("t") == v.get("t") and k != c1]
    ok.sort(key=lambda kv: -kv[1]["t"])
    cands = [c1]
    for k, v in ok:
        if len(cands) >= 3:
            break
        if all(k[0] != c[0] or k[2] != c[2] for c in cands):
            cands.append(k)
    res["frozen_candidates"] = [f"{k[0]} edge>={k[1]} side={k[2]}" for k in cands]
    disc_best = max(((k, v) for k, (v, _) in grid.items() if v.get("n", 0) >= 15), key=lambda kv: kv[1]["t"], default=(None, {}))
    res["discovery_best"] = {"rule": f"{disc_best[0][0]} edge>={disc_best[0][1]} side={disc_best[0][2]}" if disc_best[0] else None, **disc_best[1]}
    res["variants_examined"] = variants
    (OUT / "frozen.json").write_text(json.dumps({"model": {"a": model.a, "b": model.b, "s": model.s, "lam": lam, "growth_quantiles": model.g},
                                                 "candidates": res["frozen_candidates"], "written": "before any validation trade was computed"}, indent=1))
    if not validate:
        return res
    # ---- validation, once
    print("\nVALIDATION (frozen model and candidates, evaluated once)")
    vk = kill_rows(info, mk, cand, model, evs, "val", offsets=(0, 24, 48, 72))
    res["validation_brier"] = brier_report(vk, "val")
    res["validation"] = []
    for k in cands:
        tr = trade_rule(info, mk, cand, model, evs, "val", SCHEDULES[k[0]], k[1], None if k[2] == "both" else k[2])
        v = stats(tr)
        days = max(1.0, (max(info[e]["close"] for e in val) - min(info[e]["close"] for e in val)) / 86400)
        v.update({"rule": f"{k[0]} edge>={k[1]} side={k[2]}", "trades_per_day": v.get("n", 0) / days, "median_capacity_contracts": capacity(tr, cand)})
        res["validation"].append(v); print(fmt(v["rule"], v), f"cap~{v['median_capacity_contracts']:.0f}")
        (OUT / f"val_trades_{k[0]}_{k[1]}_{k[2]}.json").write_text(json.dumps(tr))
    return res


def capacity(trades, cand):
    """Median contracts traded in the signal market over the 24 h after the decision (hourly candle volume): a
    volume-based proxy for what a taker could have filled near the signal price."""
    out = []
    for r in trades:
        cs = (cand.get(r["e"]) or {}).get(r["t"]) or []
        out.append(sum(c[3] for c in cs if r["tc"] < c[0] <= r["tc"] + 24 * H))
    return st.median(out) if out else 0.0


def fmt(label, s):
    if not s.get("n"):
        return f"  {label:48s} n=0"
    return (f"  {label:48s} n={s['n']:4d} ev={s['events']:3d} win={s['win']:.0%} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.1%} "
            f"t={s['t']:5.2f} wo3={s['ret_wo3']:+.1%} halves={s['half1']:+.1%}/{s['half2']:+.1%}")


if __name__ == "__main__":
    if "--no-val" not in sys.argv and (OUT / "frozen.json").exists():
        before = json.loads((OUT / "frozen.json").read_text())["candidates"]
    r = main(validate="--no-val" not in sys.argv)
    if "--no-val" not in sys.argv and "before" in dir():
        assert before == r["frozen_candidates"], ("frozen candidates changed", before, r["frozen_candidates"])
    (OUT / ("result_core.json" if "--no-val" not in sys.argv else "discovery_only.json")).write_text(json.dumps(r, indent=1, default=str))
