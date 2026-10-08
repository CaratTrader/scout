"""r2_daily_commod_maker: rest zero-fee maker orders on the KXGOLDD / KXNATGASD / KXWTI ladders at the futures-model
fair value plus or minus a margin, 6 h to 2 h before the close, and hold to settlement.

Hypothesis (round-1 lead R2-8). commod_model's fair value (Yahoo futures minus the basis implied by the previous
settlement, per-minute vol from the last 2 h, p = Phi((S - K) / (k sigma sqrt(tau)))) carried some information against
the Kalshi mid 2-6 h before the 17:00 ET (gold, natgas) / 14:30 ET (WTI) close, too little to pay spread + fee as a
taker. These series are fee_type 'quadratic': makers pay no fee. A maker resting at fair -/+ margin collects the
model's information without paying the spread, IF the fills are not too adversely selected.

Simulation (no look-ahead)
  decision t every 5 min, tau = minutes to close in [lo, hi] (default 360..125); data known at t only:
    Yahoo 5-minute bar closes complete by t - lag (lag 0 or 10 min: Yahoo CME data may be delayed), 24-bar vol,
    basis = F(previous close of the same series) - previous settlement value (expiration_value);
    Kalshi quote = last 1-minute candle at or before t (<= 30 min old).
  order: YES bid b = min(floor_cent(p - m), ask_t - 0.01) and/or YES ask a = max(ceil_cent(p + m), bid_t + 0.01)
    (= NO bid 1 - a), never marketable; 'touch' variants also require b >= bid_t (a <= ask_t), i.e. at or inside the
    touch; quote only when p is in [pband, 1 - pband].
  live from t + 60 s (latency) until replaced by the next decision's order (t + 360 s): fill ONLY on a trade-through,
    a trade print or ask quote strictly below b (YES bid), a print or bid quote strictly above a (NO bid), in a
    1-minute candle that starts at or after t + 60.  One position per market (the first fill), held to settlement;
    no maker fee ('quadratic'); equal-$ return per trade = pnl / price.
  Adverse selection is measured as the return if EVERY posted order had filled vs the orders that did fill.
Split: events of the three series pooled and ordered by close; DISCOVERY = first 70%, VALIDATION = last 30%.
Every variant examined is counted. At most 3 frozen candidates (FROZEN) go to validation, once.
Results (2026-10-08, 120 events 08-04..10-07, 606 variants): the model loses to the mid on discovery Brier (.166 vs .158;
only WTI ties); best discovery t 1.63 (bar 4.46). Validation: C1 (pre-registered, 2c) -39%/$ t -5.5, C2 (4c) -29%/$
t -3.6, C3 (best discovery) -3%/$ t -0.4. Filled orders win 33% vs 54% unfilled: adverse selection. Verdict dead.
Usage: python -m lab.kalshi.strategies.r2_daily_commod_maker [diag|disc|val]"""
from __future__ import annotations
import bisect, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import cell_stats
from lab.kalshi.strategies.r2_daily_commod_maker_data import OUT, SERIES, Fut5, events, settle_value

N = st.NormalDist()
MAX_AGE = 30 * 60
SPLIT = 0.7


def cents_floor(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100


def cents_ceil(x: float) -> float:
    return math.ceil(x * 100 - 1e-9) / 100


def load_candles(s: str) -> dict[str, list[list]]:
    f = OUT / f"candles_{s}.jsonl"
    return {x["t"]: x["c"] for x in map(json.loads, f.open())} if f.exists() else {}


def quote_at(c: list[list], t: int, i0: int = 0) -> tuple[float | None, float | None] | None:
    """(yes_ask, yes_bid) of the last candle with ts <= t, if <= 30 min old. 0 bid / 1 ask = empty side."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > MAX_AGE:
        return None
    ask = best[1] if best[1] is not None and 0 < best[1] < 1 else None
    bid = best[2] if best[2] is not None and 0 < best[2] < 1 else None
    return ask, bid


def build() -> list[dict]:
    """One record per (series, event) with the decision snapshots of every fetched strike."""
    out = []
    for s, sym in SERIES.items():
        F = Fut5(sym); C = load_candles(s); evs = events(s)
        for i, (e, T, ms) in enumerate(evs):
            if i == 0:
                continue
            sv = settle_value(evs[i - 1][2]); fp = F.px(evs[i - 1][1])
            if sv is None or fp is None:
                continue
            basis = fp - sv
            mk = [m for m in ms if m["t"] in C and C[m["t"]] and m["floor"] is not None]
            if not mk:
                continue
            out.append({"s": s, "e": e, "T": T, "basis": basis, "F": F, "mk": mk, "C": C, "sv": settle_value(ms)})
    out.sort(key=lambda r: r["T"])
    return out


def snapshots(ev: dict, lag: int, taus=range(360, 124, -5)) -> dict:
    """{tau: (S, sigma1)} at each decision time (futures data complete by t - lag)."""
    F = ev["F"]; out = {}
    for tau in taus:
        t = ev["T"] - tau * 60; f = F.px(t, lag=lag); sg = F.sig1(t, lag=lag)
        if f is not None and sg:
            out[tau] = (f - ev["basis"], sg)
    return out


TAUS = tuple(range(360, 124, -5))


def prep(ev: dict) -> None:
    """Per strike and decision minute: the quote known at t and, over the order's life (candles starting at or after
    t + 60 through t + 360), the lowest sell-side through price (trade print or ask quote) and the highest buy-side one."""
    if "_prep" in ev:
        return
    ev["_prep"] = {}
    for mkt in ev["mk"]:
        c = ev["C"][mkt["t"]]; ts = [r[0] for r in c]; rows = {}
        for tau in TAUS:
            t = ev["T"] - tau * 60
            j = bisect.bisect_right(ts, t) - 1
            q = None
            if j >= 0 and t - ts[j] <= MAX_AGE:
                r = c[j]
                q = (r[1] if r[1] is not None and 0 < r[1] < 1 else None, r[2] if r[2] is not None and 0 < r[2] < 1 else None)
            lo_through, hi_through, vol = 9.0, -9.0, 0.0
            j0 = bisect.bisect_left(ts, t + 120)
            for r in c[j0:]:
                if r[0] > t + 360:
                    break
                vol += r[9] or 0.0
                for v in (r[7], r[3]):
                    if v is not None and 0 < v < lo_through:
                        lo_through = v
                for v in (r[8], r[6]):
                    if v is not None and 0 < v < 1 and v > hi_through:
                        hi_through = v
            rows[tau] = (q, lo_through, hi_through, vol)
        ev["_prep"][mkt["t"]] = rows


def simulate(evs: list[dict], k: float, m: float, lag: int, join: str, window: tuple[int, int], pband: float = 0.05,
             series: tuple[str, ...] | None = None, sides: str = "both", posted_log: list | None = None,
             max_dis: float | None = None, w: float = 1.0) -> list[dict]:
    """max_dis: quote only when |p - mid_t| <= max_dis (the market knows the true basis; a wild disagreement is more
    likely a model error than an opportunity). w < 1: quote around mid + w (p - mid) instead of the model value."""
    lo, hi = window; trades = []
    for ev in evs:
        if series and ev["s"] not in series:
            continue
        prep(ev)
        snap = ev.setdefault("_snap", {}).get(lag)
        if snap is None:
            snap = ev["_snap"][lag] = snapshots(ev, lag)
        for mkt in ev["mk"]:
            P = ev["_prep"][mkt["t"]]; K = mkt["floor"]; won_yes = mkt["result"] == "yes"; done = False
            for tau in TAUS:
                if done:
                    break
                if not (lo <= tau <= hi) or tau not in snap:
                    continue
                S, sg = snap[tau]
                p = N.cdf((S - K) / (k * sg * math.sqrt(tau)))
                if not (pband <= p <= 1 - pband):
                    continue
                q, lo_th, hi_th, vol = P[tau]
                if q is None:
                    continue
                ask, bid = q
                if max_dis is not None or w < 1:
                    if ask is None or bid is None or (max_dis is not None and abs(p - (ask + bid) / 2) > max_dis):
                        continue
                    if w < 1:
                        p = (ask + bid) / 2 + w * (p - (ask + bid) / 2)
                orders = []
                if sides in ("both", "yes"):
                    b = cents_floor(p - m)
                    if ask is not None:
                        b = min(b, round(ask - 0.01, 2))
                    if b >= 0.02 and (join == "any" or (bid is not None and b >= bid)):
                        orders.append(("YES", b, lo_th < b - 1e-9))
                if sides in ("both", "no"):
                    a = cents_ceil(p + m)
                    if bid is not None:
                        a = max(a, round(bid + 0.01, 2))
                    if a <= 0.98 and (join == "any" or (ask is not None and a <= ask)):
                        orders.append(("NO", a, hi_th > a + 1e-9))
                for side, px, hit in orders:
                    won = won_yes if side == "YES" else not won_yes
                    cost = px if side == "YES" else round(1 - px, 2)
                    ret = ((1.0 if won else 0.0) - cost) / cost
                    if posted_log is not None:
                        posted_log.append({"e": ev["e"], "t_close": ev["T"], "ret": ret, "won": won, "px": cost, "filled": hit})
                    if hit:
                        trades.append({"e": ev["e"], "s": ev["s"], "t": mkt["t"], "t_close": ev["T"], "tau": tau, "side": side,
                                       "px": cost, "won": won, "ret": ret, "p": p if side == "YES" else 1 - p,
                                       "mid": (ask + bid) / 2 if ask is not None and bid is not None else None,
                                       "vol_life": vol})
                        done = True; break
    return trades


def stats(T: list[dict]) -> dict:
    if len(T) < 2:
        return {"n": len(T)}
    cs = cell_stats(T); ts = sorted(r["t_close"] for r in T); mid = ts[len(ts) // 2]
    h1 = [r["ret"] for r in T if r["t_close"] < mid]; h2 = [r["ret"] for r in T if r["t_close"] >= mid]
    days = max(1.0, (ts[-1] - ts[0]) / 86400)
    cs.update({"half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
               "trades_per_day": len(T) / days, "edge_vs_model": st.mean(r["p"] - r["px"] for r in T)})
    return cs


def fmt(v: dict) -> str:
    if v.get("n", 0) < 2:
        return f"n={v.get('n', 0)}"
    return (f"n={v['n']:4d} ev={v['events']:3d} win={v['win']:.2f} px={v['px']:.3f} ret={v['ret']:+.3f} t={v['t']:+.2f} "
            f"wo3={v['ret_wo3']:+.3f} h={v['half1']:+.3f}/{v['half2']:+.3f} modelEdge={v['edge_vs_model']:+.3f}")


def split_events(evs: list[dict]) -> tuple[list[dict], list[dict]]:
    cut = evs[int(len(evs) * SPLIT)]["T"]
    return [e for e in evs if e["T"] < cut], [e for e in evs if e["T"] >= cut]


# ----------------------------------------------------------------------------------------------------------------
GRID = [dict(k=k, m=m, lag=lag, join=join, window=win, series=ss, w=w) for k in (1.0, 1.5) for m in (0.02, 0.04, 0.08)
        for lag in (0, 600) for join in ("any", "touch") for win in ((125, 360), (240, 360), (125, 240))
        for ss in (None, ("KXGOLDD",), ("KXNATGASD",), ("KXWTI",)) for w in (1.0, 0.5)]
VARIANTS_EXTRA = 4 + 8 + 16   # Brier/log-loss model-vs-mid cells (k x lag), logistic incremental-information fits, the
#                               16 pre-registered-style adverse-selection runs (diag_maker_discovery.txt)

# Frozen 2026-10-08 after discovery, before any validation run. C1/C2 are the round-1 lead's pre-registered spec
# (commod_model's model k = 1.0, T-6h..T-2h, margins 2c / 4c; lag 0 = futures bars used as soon as they close).
# C3 is the best discovery t among the all-series variants (single-series cells have only 26 discovery and 11
# validation events; the overall best, gold-only m 0.08 lag 10 min touch T-4h..T-2h, had t 1.63 vs 1.13).
FROZEN = {
    "C1_prereg_m2c": dict(k=1.0, m=0.02, lag=0, join="any", window=(125, 360), series=None, w=1.0),
    "C2_prereg_m4c": dict(k=1.0, m=0.04, lag=0, join="any", window=(125, 360), series=None, w=1.0),
    "C3_disc_best_all": dict(k=1.0, m=0.08, lag=600, join="touch", window=(125, 240), series=None, w=1.0),
}


def brier(evs: list[dict], k: float, lag: int, taus=range(360, 124, -30)) -> dict:
    """Model p vs Kalshi mid (both known at t) against the outcome, per series, on strikes with mid in [0.05, 0.95]."""
    acc = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
    for ev in evs:
        snap = snapshots(ev, lag, taus)
        for mkt in ev["mk"]:
            c = ev["C"][mkt["t"]]; y = 1.0 if mkt["result"] == "yes" else 0.0
            for tau, (S, sg) in snap.items():
                q = quote_at(c, ev["T"] - tau * 60)
                if not q or q[0] is None or q[1] is None:
                    continue
                mid = (q[0] + q[1]) / 2
                if not (0.05 <= mid <= 0.95):
                    continue
                p = min(max(N.cdf((S - mkt["floor"]) / (k * sg * math.sqrt(tau))), 0.001), 0.999)
                for key in (ev["s"], "all", f"{ev['s']}|tau{tau // 60}h"):
                    a = acc[key]; a[0] += 1; a[1] += (p - y) ** 2; a[2] += (mid - y) ** 2
                    a[3] += -math.log(p if y else 1 - p); a[4] += -math.log(mid if y else 1 - mid)
    return {k_: {"n": a[0], "brier_model": a[1] / a[0], "brier_mid": a[2] / a[0], "ll_model": a[3] / a[0], "ll_mid": a[4] / a[0]}
            for k_, a in sorted(acc.items()) if a[0]}


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "disc"
    EV = build(); D, V = split_events(EV)
    print(f"events {len(EV)} (discovery {len(D)}, validation {len(V)}); markets {sum(len(e['mk']) for e in EV)}")
    if what == "diag":
        for k in (1.0, 1.5):
            for lag in (0, 600):
                print(f"\nBrier/logloss on DISCOVERY, k={k} lag={lag}s")
                for key, v in brier(D, k, lag).items():
                    print(f"  {key:22s} n={v['n']:5d} brier model {v['brier_model']:.4f} mid {v['brier_mid']:.4f} | ll model {v['ll_model']:.4f} mid {v['ll_mid']:.4f}")
    elif what == "disc":
        res = []
        for par in GRID:
            T = simulate(D, **par); res.append((par, stats(T)))
        (OUT / "discovery.json").write_text(json.dumps([[str(p), v] for p, v in res], default=str))
        print("variants", len(res))
        ok = [x for x in res if x[1].get("n", 0) >= 40 and x[1]["t"] == x[1]["t"]]
        for p, v in sorted(ok, key=lambda x: -x[1]["t"])[:25]:
            print(" ", p, fmt(v))
    elif what == "val":
        out = {}
        for name, f in FROZEN.items():
            T = simulate(V, **f); v = stats(T); out[name] = v; print(name, fmt(v))
        (OUT / "validation.json").write_text(json.dumps(out, default=str))
