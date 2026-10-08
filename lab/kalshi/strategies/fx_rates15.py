"""fx_rates15: are Kalshi 15-minute FX up/down markets (KXEURUSD15M, KXUSDJPY15M, KXGBPUSD15M, KXAUDUSD15M,
KXUSDCAD15M) slower or less well calibrated than a simple spot + realised-vol model, enough to pay a taker?

(The Treasury-yield 15-minute and hourly series named in the brief list no markets at all on 2026-10-08.)

Market: YES iff the Pyth price at close >= the Pyth price at open (floor_strike), 5 decimals (3 for USD/JPY); ties go to YES.
Model at decision time t = close - tau minutes, using only data with timestamps <= t:
  x   = ln(S_t / S_open) from Yahoo 1-minute bars (bar starting at T-60 closes at ~T; this lag-0 alignment matches Pyth
        best, R^2 0.95-0.99 on the 15-minute change), sign-flipped for the futures quoted the other way round;
  var = k_vol * sigma_1m^2 * tau + s_b^2, sigma_1m from the last W minutes of 1-minute bars, s_b the Yahoo-vs-Pyth noise
        of a 15-minute change (estimated on discovery only);
  p   = Phi((x + half_tick) / sqrt(var))   (ties resolve YES, so the threshold sits half a tick below the strike).
Trade: taker one minute after t at the quote then (YES at yes_ask, NO at 1 - yes_bid, quote <= 30 min old),
fee 0.07 p (1-p) rounded up to the cent on a 10-contract order; buy the side whose model edge net of fee exceeds theta.
Split: events ordered by close; discovery = first 70%, validation = last 30%. Every variant examined is counted.
Spot sources: "spot" = Yahoo FX spot (EURUSD=X etc., what a home Mac could poll live); "fut" = CME futures bars
(6E=F etc.), tighter tracking but delayed ~10 min on Yahoo in real time, so only an upper bound on what is learnable.
Usage: python -m lab.kalshi.strategies.fx_rates15"""
from __future__ import annotations
import json, math, statistics as st, sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote, cell_stats

D = Path("data/kalshi_lab/strategies/fx_rates15")
SERIES = ("KXEURUSD15M", "KXUSDJPY15M", "KXGBPUSD15M", "KXAUDUSD15M", "KXUSDCAD15M")
SRC = {"KXEURUSD15M": {"spot": ("EURUSD_X", 1), "fut": ("6E_F", 1)},
       "KXUSDJPY15M": {"spot": ("JPY_X", 1), "fut": ("6J_F", -1)},
       "KXGBPUSD15M": {"spot": ("GBPUSD_X", 1), "fut": ("6B_F", 1)},
       "KXAUDUSD15M": {"spot": ("AUDUSD_X", 1), "fut": ("6A_F", 1)},
       "KXUSDCAD15M": {"spot": ("CAD_X", 1), "fut": ("6C_F", -1)}}
TAUS = (1, 2, 3, 5, 7, 10)
N = NormalDist()


def fee10(p: float) -> float:
    """Per-contract fee for a 10-contract order: ceil(0.07 * p * (1-p) * 10 contracts, to the cent) / 10."""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


class Bars:
    def __init__(self, name: str):
        f = D / "yahoo" / f"{name}.json"
        raw = {int(k): v for k, v in json.loads(f.read_text()).items()} if f.exists() else {}
        self.ts = sorted(raw); self.c = [raw[t][3] for t in self.ts]

    def at(self, T: int, stale: int = 120) -> float | None:
        """Close of the last bar starting at or before T-60 (i.e. a price known at T), if not older than `stale` s."""
        i = bisect_right(self.ts, T - 60) - 1
        if i < 0 or T - 60 - self.ts[i] > stale:
            return None
        return self.c[i]

    def var1m(self, T: int, W: int) -> float | None:
        """Realised variance per minute of log price over bars starting in [T-60-W*60, T-60] (known at T)."""
        lo = bisect_right(self.ts, T - 60 - W * 60); hi = bisect_right(self.ts, T - 60)
        if hi - lo < max(5, W // 4):
            return None
        ss = sum(math.log(self.c[i] / self.c[i - 1]) ** 2 for i in range(max(lo, 1), hi))
        span = (self.ts[hi - 1] - self.ts[max(lo - 1, 0)]) / 60
        return ss / span if span > 0 else None


def load() -> tuple[list[dict], dict]:
    mk, cd = [], {}
    for s in SERIES:
        f = D / f"markets_{s}.jsonl"
        if f.exists():
            mk += [json.loads(l) for l in f.open()]
        f = D / f"candles_{s}.jsonl"
        if f.exists():
            for l in f.open():
                x = json.loads(l); cd[x["t"]] = x["c"]
    return mk, cd


def decimals(s: str) -> int:
    return 3 if "JPY" in s else 5


def build_rows() -> list[dict]:
    """One row per (market, tau, source) with the model inputs, market quotes and outcome. No parameters chosen here."""
    mk, cd = load(); rows = []
    bars = {s: {k: (Bars(n), sg) for k, (n, sg) in SRC[s].items()} for s in SERIES}
    by_s = defaultdict(list)
    for m in mk:
        by_s[m["series"]].append(m)
    for s, ms in by_s.items():
        ms.sort(key=lambda m: m["close"])
        for i, m in enumerate(ms):
            c = cd.get(m["t"])
            if not c or m["floor"] is None:
                continue
            prev = [p for p in ms[max(0, i - 12):i] if p["xv"] and p["floor"] and p["close"] <= m["open"]]
            pv15 = st.mean(math.log(p["xv"] / p["floor"]) ** 2 for p in prev[-8:]) / 15 if len(prev) >= 4 else None
            ht = 0.5 * 10 ** -decimals(s) / m["floor"]
            for tau in TAUS:
                t = m["close"] - tau * 60
                q_now = quote(c, t); q_fill = quote(c, t + 60)
                if not q_fill:
                    continue
                for src, (b, sg) in bars[s].items():
                    s0, st_ = b.at(m["open"]), b.at(t)
                    if not s0 or not st_:
                        continue
                    rows.append({"s": s, "e": m["e"], "t": m["t"], "close": m["close"], "tau": tau, "src": src,
                                 "x": sg * math.log(st_ / s0), "ht": ht, "v30": b.var1m(t, 30), "v60": b.var1m(t, 60),
                                 "v120": b.var1m(t, 120), "pv15": pv15,
                                 "mid": (q_now[0] + q_now[1]) / 2 if q_now else None,
                                 "ask": q_fill[0], "bid": q_fill[1], "won": m["result"] == "yes",
                                 "r15": math.log(m["xv"] / m["floor"]) if m["xv"] else None})
    return rows


def split_cut(rows: list[dict], frac: float = 0.7) -> int:
    ev = sorted({(r["close"], r["e"]) for r in rows})
    return ev[int(len(ev) * frac)][0]


def prob(r: dict, vk: str, kv: float, sb2: float) -> float | None:
    v = r[vk]
    if v is None:
        return None
    var = kv * v * r["tau"] + sb2
    return N.cdf((r["x"] + r["ht"]) / math.sqrt(var)) if var > 0 else None


def logloss(rows, vk, kv, sb2):
    ll = [];
    for r in rows:
        p = prob(r, vk, kv, sb2)
        if p is None:
            continue
        p = min(max(p, 1e-4), 1 - 1e-4); ll.append(-math.log(p if r["won"] else 1 - p))
    return st.mean(ll) if ll else float("inf"), len(ll)


def trades(rows, vk, kv, sb2, theta, tau, src, pmin=0.02, pmax=0.98):
    out = []
    for r in rows:
        if r["tau"] != tau or r["src"] != src:
            continue
        p = prob(r, vk, kv, sb2)
        if p is None:
            continue
        best = None
        for side, px, pw in (("YES", r["ask"], p), ("NO", 1 - r["bid"], 1 - p)):
            if not (pmin <= px <= pmax):
                continue
            edge = pw - px - fee10(px)
            if edge > theta and (best is None or edge > best[3]):
                best = (side, px, pw, edge)
        if best:
            side, px, pw, edge = best
            w = r["won"] if side == "YES" else not r["won"]
            out.append({"e": r["e"], "s": r["s"], "close": r["close"], "side": side, "px": px, "pm": pw, "edge": edge, "won": w,
                        "ret": ((1.0 if w else 0.0) - px - fee10(px)) / px})
    return out


def sig(z: float) -> float:
    return 1 / (1 + math.exp(-max(min(z, 40), -40)))


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4); return math.log(p / (1 - p))


def fit_logit(X: list[tuple], y: list[bool], iters: int = 30, ridge: float = 1e-3) -> list[float]:
    """Logistic regression with intercept by Newton-Raphson (standard library only)."""
    k = len(X[0]) + 1; w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; H = [[ridge if i == j else 0.0 for j in range(k)] for i in range(k)]
        for x, yy in zip(X, y):
            xx = (1.0,) + tuple(x); p = sig(sum(a * b for a, b in zip(w, xx))); r = (1.0 if yy else 0.0) - p; q = p * (1 - p)
            for i in range(k):
                g[i] += r * xx[i]
                for j in range(k):
                    H[i][j] += q * xx[i] * xx[j]
        # solve H d = g (Gaussian elimination)
        A = [H[i][:] + [g[i]] for i in range(k)]
        for i in range(k):
            piv = max(range(i, k), key=lambda r_: abs(A[r_][i])); A[i], A[piv] = A[piv], A[i]
            for r_ in range(k):
                if r_ != i and A[i][i]:
                    f = A[r_][i] / A[i][i]; A[r_] = [a - f * b for a, b in zip(A[r_], A[i])]
        d = [A[i][k] / A[i][i] if A[i][i] else 0.0 for i in range(k)]
        w = [a + b for a, b in zip(w, d)]
        if max(abs(x) for x in d) < 1e-8:
            break
    return w


def capacity(tr: list[dict], cd: dict, mk: list[dict]) -> float | None:
    """Volume-based capacity proxy: contracts traded in the 5 minutes up to the fill minute of each trade (median)."""
    ev2t = {m["e"]: m["t"] for m in mk}; vols = []
    for r in tr:
        c = cd.get(ev2t.get(r["e"]))
        if c:
            vols.append(sum(x[5] for x in c if r["close"] - 16 * 60 <= x[0] <= r["close"]) / 3)   # ~5 of the 15 minutes
    return st.median(vols) if vols else None


def summarize(tr: list[dict]) -> dict:
    if len(tr) < 3:
        return {"n": len(tr)}
    v = cell_stats(tr)
    tr = sorted(tr, key=lambda r: r["close"]); mid = tr[len(tr) // 2]["close"]
    h1 = [r["ret"] for r in tr if r["close"] < mid]; h2 = [r["ret"] for r in tr if r["close"] >= mid]
    v["half1"] = st.mean(h1) if h1 else float("nan"); v["half2"] = st.mean(h2) if h2 else float("nan")
    # cluster by 15-minute window across pairs as well (EUR/GBP/AUD share the USD leg)
    w = defaultdict(list)
    for r in tr:
        w[r["close"]].append(r["ret"])
    wm = [st.mean(x) for x in w.values()]
    v["t_window"] = st.mean(wm) / (st.pstdev(wm) / math.sqrt(len(wm))) if len(wm) > 2 and st.pstdev(wm) > 0 else float("nan")
    return v


def main() -> None:
    rows = build_rows()
    cut = split_cut(rows)
    disc = [r for r in rows if r["close"] < cut]; val = [r for r in rows if r["close"] >= cut]
    print(f"rows {len(rows)}  discovery {len(disc)}  validation {len(val)}  cut {cut}")
    res = {"cut": cut, "n_rows": len(rows)}
    # 1) Yahoo-vs-Pyth noise of a 15-minute change, per source (discovery only)
    sb2 = {}
    mk, _ = load(); by_s = defaultdict(list)
    for m in mk:
        by_s[m["series"]].append(m)
    for src in ("spot", "fut"):
        resid = []
        for s, ms in by_s.items():
            b, sg = Bars(SRC[s][src][0]), SRC[s][src][1]
            for m in ms:
                if m["close"] >= cut or not m["xv"]:
                    continue
                a0, a1 = b.at(m["open"]), b.at(m["close"])
                if a0 and a1:
                    resid.append(math.log(m["xv"] / m["floor"]) - sg * math.log(a1 / a0))
        sb2[src] = st.pvariance(resid); print(f"basis noise {src}: n={len(resid)} sd={math.sqrt(sb2[src]):.2e}")
    res["sb"] = {k: math.sqrt(v) for k, v in sb2.items()}
    # 2) volatility estimator and scale, by discovery log-loss (variants counted)
    variants = 0; fits = {}
    for src in ("spot", "fut"):
        d = [r for r in disc if r["src"] == src]
        best = None
        for vk in ("v30", "v60", "v120", "pv15"):
            for kv in (0.5, 0.75, 1.0, 1.5, 2.0):
                variants += 1
                ll, n = logloss([r for r in d if r[vk] is not None and r["v60"] is not None and r["pv15"] is not None], vk, kv, sb2[src])
                if best is None or ll < best[0]:
                    best = (ll, vk, kv, n)
        fits[src] = best; print(f"model fit {src}: logloss {best[0]:.4f} vol={best[1]} k={best[2]} n={best[3]}")
    res["fits"] = fits
    # 3) market vs model: log-loss of the market mid and the model on the same rows (discovery)
    for src in ("spot", "fut"):
        _, vk, kv, _ = fits[src]
        for tau in TAUS:
            d = [r for r in disc if r["src"] == src and r["tau"] == tau and r["mid"] is not None and r[vk] is not None]
            if not d:
                continue
            ll = lambda p, w: -math.log(min(max(p if w else 1 - p, 1e-3), 1))
            llm = st.mean(ll(r["mid"], r["won"]) for r in d)
            llp = st.mean(ll(prob(r, vk, kv, sb2[src]), r["won"]) for r in d)
            # blend: does the model add information beyond the mid?
            print(f"  {src} tau={tau:2d} n={len(d):5d} logloss market_mid={llm:.4f} model={llp:.4f}")
    # 4) trading rules on discovery
    grid = []
    for src in ("spot", "fut"):
        _, vk, kv, _ = fits[src]
        for tau in (2, 3, 5, 10):
            for theta in (0.0, 0.03, 0.06, 0.10):
                variants += 1
                tr = trades(disc, vk, kv, sb2[src], theta, tau, src)
                v = summarize(tr); v.update(src=src, tau=tau, theta=theta); grid.append(v)
    grid.sort(key=lambda v: -(v.get("ret") or -9))
    print("\nDISCOVERY grid (best first)")
    for v in grid:
        if v["n"] >= 3:
            print(f"  {v['src']:4s} tau={v['tau']:2d} th={v['theta']:.2f} n={v['n']:4d} ev={v['events']:4d} win={v['win']:.0%} px={v['px']:.2f} "
                  f"ret={v['ret']:+.1%} t={v['t']:.2f} tw={v['t_window']:.2f} wo3={v['ret_wo3']:+.1%} h={v['half1']:+.1%}/{v['half2']:+.1%}")
        else:
            print(f"  {v['src']:4s} tau={v['tau']:2d} th={v['theta']:.2f} n={v['n']}")
    res["discovery_grid"] = grid
    # 5) does the model add information beyond the market mid? logistic blend fitted on discovery, scored on validation
    res["blend"] = {}
    for src in ("spot", "fut"):
        _, vk, kv, _ = fits[src]
        for tau in (1, 2, 3, 5, 10):
            variants += 1
            sel = lambda R: [(logit(r["mid"]), logit(prob(r, vk, kv, sb2[src])), r["won"]) for r in R
                             if r["src"] == src and r["tau"] == tau and r["mid"] is not None and 0.005 < r["mid"] < 0.995 and r[vk] is not None]
            dtr, vte = sel(disc), sel(val)
            w = fit_logit([(a, b) for a, b, _ in dtr], [y for _, _, y in dtr])
            w0 = fit_logit([(a,) for a, _, _ in dtr], [y for _, _, y in dtr])
            sc = lambda W, X: st.mean(-math.log(min(max(sig(W[0] + sum(wi * xi for wi, xi in zip(W[1:], x[:-1]))) if x[-1] else
                                                            1 - sig(W[0] + sum(wi * xi for wi, xi in zip(W[1:], x[:-1]))), 1e-6), 1)) for x in X)
            out = {"n_disc": len(dtr), "n_val": len(vte), "w_blend": w, "w_mid": w0,
                   "val_ll_mid_only": sc(w0, [(a, y) for a, _, y in vte]), "val_ll_blend": sc(w, vte)}
            res["blend"][f"{src}_tau{tau}"] = out
            print(f"blend {src} tau={tau:2d}: coef mid={w[1]:+.2f} model={w[2]:+.2f} | validation logloss mid-only {out['val_ll_mid_only']:.4f} blend {out['val_ll_blend']:.4f}")
    # 6) market calibration: taker return by decision time, side and price band (no model), discovery
    bands = ((0.02, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 0.75), (0.75, 0.90), (0.90, 0.98))
    mk, cd = load(); cells = []
    def band_trades(ms, tau, side, lo, hi):
        out = []
        for m in ms:
            c = cd.get(m["t"])
            if not c:
                continue
            t = m["close"] - tau * 60; q = quote(c, t + 60)
            if not q:
                continue
            px = q[0] if side == "YES" else 1 - q[1]
            if not (lo <= px < hi):
                continue
            w = (m["result"] == "yes") == (side == "YES")
            out.append({"e": m["e"], "close": m["close"], "px": px, "won": w, "ret": ((1.0 if w else 0.0) - px - fee10(px)) / px})
        return out
    mdisc = [m for m in mk if m["close"] < cut]; mval = [m for m in mk if m["close"] >= cut]
    for tau in (1, 2, 3, 5, 10, 14):
        for side in ("YES", "NO"):
            for lo, hi in bands:
                tr = band_trades(mdisc, tau, side, lo, hi)
                if len(tr) >= 30:
                    variants += 1; v = summarize(tr); v.update(rule=f"band tau={tau} {side} {lo:.2f}-{hi:.2f}", kind="band", tau=tau, side=side, lo=lo, hi=hi); cells.append(v)
    cells.sort(key=lambda v: -v["ret"])
    print("\nBAND cells (discovery), best 12 of", len(cells))
    for v in cells[:12]:
        print(f"  {v['rule']:28s} n={v['n']:4d} win={v['win']:.0%} px={v['px']:.2f} ret={v['ret']:+.1%} t={v['t']:.2f} wo3={v['ret_wo3']:+.1%}")
    allb = [r for lo, hi in bands for tau in (1, 2, 3, 5, 10, 14) for side in ("YES", "NO") for r in band_trades(mdisc, tau, side, lo, hi)]
    print(f"  all band trades pooled: n={len(allb)} ret={st.mean(r['ret'] for r in allb):+.1%}")
    res["band_cells"] = cells
    # 7) window-to-window momentum / reversal of the Pyth 15-minute change, entered at open+1 min (fill open+2 min)
    by_s = defaultdict(list)
    for m in mk:
        by_s[m["series"]].append(m)
    auto = []
    for ms in by_s.values():
        ms.sort(key=lambda m: m["close"])
        for a, b in zip(ms, ms[1:]):
            if a["close"] == b["open"] and a["xv"] and a["floor"] and a["xv"] != a["floor"]:
                auto.append((b, 1 if a["xv"] > a["floor"] else -1, math.log(a["xv"] / a["floor"])))
    ac_rows = [(d, b["result"] == "yes") for b, d, _ in auto if b["close"] < cut]
    print(f"\nautocorr (discovery): P(up | prev up) = {st.mean(y for d, y in ac_rows if d > 0):.3f} n={sum(1 for d, _ in ac_rows if d > 0)}; "
          f"P(up | prev down) = {st.mean(y for d, y in ac_rows if d < 0):.3f} n={sum(1 for d, _ in ac_rows if d < 0)}")
    mom = []
    for mode in ("follow", "fade"):
        for delay_min in (1, 3):
            tr = []
            for b, d, _ in auto:
                c = cd.get(b["t"])
                if not c or b["close"] >= cut:
                    continue
                q = quote(c, b["open"] + (delay_min + 1) * 60)
                if not q:
                    continue
                up = (d > 0) == (mode == "follow"); px = q[0] if up else 1 - q[1]
                if not (0.02 <= px <= 0.98):
                    continue
                w = (b["result"] == "yes") == up
                tr.append({"e": b["e"], "close": b["close"], "px": px, "won": w, "ret": ((1.0 if w else 0.0) - px - fee10(px)) / px})
            variants += 1; v = summarize(tr); v.update(rule=f"{mode} prev window, enter open+{delay_min}m", kind="auto", mode=mode, delay=delay_min); mom.append(v)
            print(f"  {v['rule']:34s} n={v['n']:4d} win={v['win']:.0%} px={v['px']:.2f} ret={v['ret']:+.1%} t={v['t']:.2f}")
    res["autocorr"] = mom
    # 8) maker (resting) bids: quadratic series charge makers no fee. A bid for a side at price L, placed at open+1 min and
    #    left until close-2 min, fills only if that side's ask later trades through it (ask low < L in a 1-minute candle
    #    after placement); adverse selection is therefore in the sample by construction.
    mkr = []
    def maker(ms, mode, L):
        out = []
        for b, d, _ in ms:
            c = cd.get(b["t"])
            if not c:
                continue
            t0 = b["open"] + 60; t1 = b["close"] - 120
            sides = ("YES", "NO") if mode == "both" else (("YES" if (d < 0) == (mode == "fade") else "NO"),)
            for side in sides:
                filled = None
                for r in c:
                    if r[0] <= t0 + 60 or r[0] > t1:
                        continue   # candle ending at r[0] covers (r[0]-60, r[0]]; first eligible minute starts after placement
                    lo_ask = r[3] if side == "YES" else (1 - r[4] if r[4] is not None else None)
                    if lo_ask is not None and lo_ask < L - 1e-9:
                        filled = r[0]; break
                if filled:
                    w = (b["result"] == "yes") == (side == "YES")
                    out.append({"e": b["e"], "close": b["close"], "px": L, "won": w, "ret": ((1.0 if w else 0.0) - L) / L, "side": side})
        return out
    adisc = [a for a in auto if a[0]["close"] < cut]
    for mode in ("fade", "follow", "both"):
        for L in (0.35, 0.40, 0.45, 0.48):
            variants += 1; tr = maker(adisc, mode, L); v = summarize(tr)
            v.update(rule=f"maker {mode} bid {L:.2f} open+1..close-2", kind="maker", mode=mode, L=L); mkr.append(v)
            if v["n"] >= 3:
                print(f"  {v['rule']:36s} n={v['n']:4d} win={v['win']:.0%} ret={v['ret']:+.1%} t={v['t']:.2f} wo3={v['ret_wo3']:+.1%}")
    res["maker"] = mkr
    # 9) freeze: the 3 best discovery rules (n >= 40) across all kinds, then judge them once on validation
    pool = [dict(v, kind="model") for v in grid if v.get("n", 0) >= 40] + [v for v in cells + mom + mkr if v.get("n", 0) >= 40]
    pool.sort(key=lambda v: -v["ret"]); frozen = pool[:3]
    print("\nFROZEN (discovery) -> VALIDATION")
    vres = []
    vauto = [a for a in auto if a[0]["close"] >= cut]
    for v in frozen:
        if v["kind"] == "model":
            _, vk, kv, _ = fits[v["src"]]; tr = trades(val, vk, kv, sb2[v["src"]], v["theta"], v["tau"], v["src"])
            rule = f"model {v['src']} tau={v['tau']} theta={v['theta']}"
        elif v["kind"] == "band":
            tr = band_trades(mval, v["tau"], v["side"], v["lo"], v["hi"]); rule = v["rule"]
        elif v["kind"] == "auto":
            tr = []
            for b, d, _ in vauto:
                c = cd.get(b["t"]); q = quote(c, b["open"] + (v["delay"] + 1) * 60) if c else None
                if not q:
                    continue
                up = (d > 0) == (v["mode"] == "follow"); px = q[0] if up else 1 - q[1]
                if 0.02 <= px <= 0.98:
                    w = (b["result"] == "yes") == up
                    tr.append({"e": b["e"], "close": b["close"], "px": px, "won": w, "ret": ((1.0 if w else 0.0) - px - fee10(px)) / px})
            rule = v["rule"]
        else:
            tr = maker(vauto, v["mode"], v["L"]); rule = v["rule"]
        x = summarize(tr); x["rule"] = rule; x["disc"] = {k: v.get(k) for k in ("n", "ret", "t", "win", "px")}
        x["median_capacity_contracts"] = capacity(tr, cd, mk)
        x["trades_per_day"] = len(tr) / max(1e-9, (max(r["close"] for r in tr) - min(r["close"] for r in tr)) / 86400) if len(tr) > 1 else 0
        vres.append(x)
        if x["n"] >= 3:
            print(f"  {rule:36s} disc ret={v['ret']:+.1%} n={v['n']} | VAL n={x['n']} ev={x['events']} win={x['win']:.0%} px={x['px']:.2f} ret={x['ret']:+.1%} "
                  f"t={x['t']:.2f} tw={x['t_window']:.2f} wo3={x['ret_wo3']:+.1%} halves={x['half1']:+.1%}/{x['half2']:+.1%} per_day={x['trades_per_day']:.1f}")
        else:
            print(f"  {rule}: validation n={x['n']}")
    res["frozen_validation"] = vres
    res["variants_examined"] = variants
    print("variants examined", variants)
    (D / "analysis.json").write_text(json.dumps(res, default=str, indent=1))


if __name__ == "__main__":
    main()
