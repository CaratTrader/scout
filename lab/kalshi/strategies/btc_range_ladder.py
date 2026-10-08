"""btc_range_ladder: hourly crypto RANGE ladders (KXBTC $100 buckets, KXETH $5 buckets) and the 15-minute coin race
(KXCRYPTOLEAD15M). Three questions, all taker-only, decided at minute t, filled at the quote of minute t+1:

  (a) ladder consistency  - sum of bucket YES bids > $1 (buy NO on every listed bucket: pays N-1 at worst) and, for the
                            coin race, sum of the 5 YES asks < $1 (buy all: pays exactly $1) / sum of bids > $1.
  (c) cross-consistency   - bucket [a, a+w) = above(a) - above(a+w) on the KXBTCD/KXETHD ladder of the SAME hour (same
                            CF Benchmarks 60-s average, same close). Exact 3-leg arbs (pay exactly $1 or $2), and the risky
                            "relative value" version: buy the bucket when its ask is below the ladder-implied mid by > theta.
  (b) model fair value    - lognormal from the Coinbase spot at t and realised 1-minute vol (bucket) / Monte-Carlo of the five
                            15-minute returns with realised vols and correlations (coin race) vs the taker price.

Split: events ordered by close; first 70% discovery, last 30% validation (per family). Parameters are chosen on discovery
only; at most 3 frozen candidates are validated once (validate step reads frozen.json written by the select step).
Usage: python -m lab.kalshi.strategies.btc_range_ladder explore|select|validate"""
from __future__ import annotations
import bisect, json, math, random, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote, cell_stats

OUT = Path("data/kalshi_lab/strategies/btc_range_ladder")
N01 = NormalDist()
SPLIT = 0.7
RANGE = {"BTC": ("KXBTC", "KXBTCD", "btc", 100.0), "ETH": ("KXETH", "KXETHD", "eth", 5.0)}
COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")


def fee(p: float, contracts: int = 0, stake: float = 5.0) -> float:
    """Taker fee per contract incl. Kalshi's round-up to the cent per order. Default order: a $5 stake."""
    c = contracts or max(1, round(stake / max(p, 0.01)))
    return math.ceil(round(7 * c * p * (1 - p), 9)) / 100 / c


def load_cb(asset: str) -> dict[int, list]:
    p = OUT / f"cb_{asset}.json"
    return {int(k): v for k, v in json.loads(p.read_text()).items()} if p.exists() else {}


class Spot:
    """Coinbase 1-minute closes: spot(t) = close of the minute ending at or before t; vol(t, W) from the W minutes before t."""
    def __init__(self, asset: str):
        cb = load_cb(asset); self.ts = sorted(cb); self.cl = [cb[k][3] for k in self.ts]; self.cb = cb
        self.lr = [math.log(b / a) if (t2 - t1 == 60) else None for (t1, a), (t2, b) in zip(zip(self.ts, self.cl), zip(self.ts[1:], self.cl[1:]))]

    def spot(self, t: int) -> float | None:
        i = bisect.bisect_right(self.ts, t - 60) - 1     # minute starting at ts closes at ts + 60 <= t
        return self.cl[i] if i >= 0 and t - 60 - self.ts[i] <= 300 else None

    def rets(self, t: int, w: int) -> list[float]:
        i = bisect.bisect_right(self.ts, t - 60) - 1
        return [r for r in self.lr[max(0, i - w):i] if r is not None]

    def vol(self, t: int, w: int = 60) -> float | None:
        r = self.rets(t, w)
        return math.sqrt(sum(x * x for x in r) / len(r)) if len(r) >= w // 2 else None


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    v = cell_stats(rows)
    mid = sorted(r["t_close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
    v["half1"] = st.mean(h1) if h1 else float("nan"); v["half2"] = st.mean(h2) if h2 else float("nan")
    return v


def fmt(v: dict) -> str:
    if not v.get("n"):
        return "n=0"
    return (f"n={v['n']:5d} ev={v['events']:4d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} "
            f"wo3={v['ret_wo3']:+.1%} h1={v['half1']:+.1%} h2={v['half2']:+.1%}")


# ---------------------------------------------------------------- range ladders
def load_range(asset: str) -> list[dict]:
    ser, above, cba, w = RANGE[asset]
    meta = defaultdict(dict)
    for l in open(f"data/kalshi_lab/markets/{above}.jsonl"):
        m = json.loads(l); meta[m["e"]][round(m["floor"], 2)] = m
    up = defaultdict(dict)
    for f in (Path(f"data/kalshi_lab/candles/{above}.jsonl"), Path(f"data/kalshi_lab/strategies/cryptoH_model/candles_{above}.jsonl")):
        if f.exists():
            for l in f.open():
                x = json.loads(l); e = x["t"].rsplit("-", 1)[0]; k = round(float(x["t"].rsplit("-T", 1)[1]), 2)
                if x["c"]:
                    up[e][k] = x["c"]
    evs = {}
    for l in (OUT / f"candles_{ser}.jsonl").open():
        x = json.loads(l); e = x["e"]; ea = e.replace(ser, above)
        if ea not in meta:
            continue
        if e not in evs:
            ms = meta[ea]; any_m = next(iter(ms.values()))
            yes = [k for k, m in ms.items() if m["result"] == "yes"]; no = [k for k, m in ms.items() if m["result"] == "no"]
            lo = max(yes) if yes else None; hi = min(no) if no else None
            evs[e] = {"e": e, "T": any_m["close"], "open": any_m["open"], "settle_lo": lo, "settle_hi": hi, "b": {}, "up": up.get(ea, {}),
                      "up_res": {k: m["result"] == "yes" for k, m in ms.items()}}
        f = x["floor"]; strike = round(f - 0.01, 2)
        won = evs[e]["settle_lo"] is not None and abs(evs[e]["settle_lo"] - strike) < 1e-6
        if x["c"]:
            evs[e]["b"][f] = {"t": x["t"], "c": x["c"], "won": won}
    out = [v for v in evs.values() if v["settle_lo"] is not None and v["settle_hi"] is not None and abs(v["settle_hi"] - v["settle_lo"] - w) < 1e-6]
    return sorted(out, key=lambda v: v["T"])


def split(evs: list[dict]) -> tuple[list[dict], list[dict], int]:
    closes = sorted({v["T"] for v in evs}); cut = closes[int(len(closes) * SPLIT)]
    return [v for v in evs if v["T"] < cut], [v for v in evs if v["T"] >= cut], cut


def q(c, t):
    r = quote(c, t)
    if not r:
        return None
    a, b = r
    return (a if a is not None and 0 < a < 0.995 else None, b if b is not None and b > 0.005 else None)


def p_bucket(S: float, sig1: float, tau_min: float, lo: float, hi: float, basis: float = 0.0, k: float = 1.0) -> float:
    s = k * sig1 * math.sqrt(max(tau_min - 2 / 3, 0.05))
    z = lambda x: math.log(x / (S + basis)) / s
    return N01.cdf(z(hi)) - N01.cdf(z(lo))


def range_rows(asset: str, evs: list[dict], spot: Spot, taus=range(1, 60), W=60, k=1.0, basis=0.0) -> list[dict]:
    """One row per (bucket, decision minute): model prob, ladder-implied mid, decision quote and fill quote one minute later."""
    w = RANGE[asset][3]; rows = []
    for v in evs:
        T = v["T"]; up = v["up"]
        for tau in taus:
            t = T - tau * 60
            S = spot.spot(t); sig = spot.vol(t, W)
            if S is None or not sig:
                continue
            for f, b in v["b"].items():
                q0 = q(b["c"], t); q1 = q(b["c"], t + 60)
                if not q0 or not q1:
                    continue
                strike = round(f - 0.01, 2); u0 = up.get(strike); u1 = up.get(round(strike + w, 2))
                imp = None
                if u0 and u1:
                    a0, b0 = q(u0, t) or (None, None); a1, b1 = q(u1, t) or (None, None)
                    if None not in (a0, b0, a1, b1):
                        imp = (a0 + b0) / 2 - (a1 + b1) / 2
                rows.append({"e": v["e"], "t_close": T, "tau": tau, "f": f, "won": b["won"], "pm": p_bucket(S, sig, tau, f, f + w, basis, k),
                             "imp": imp, "a0": q0[0], "b0": q0[1], "a1": q1[0], "b1": q1[1], "dist": (f + w / 2 - S) / (sig * S * math.sqrt(tau))})
    return rows


def trades_from(rows: list[dict], side: str, sig_key: str, theta: float, tau_lo: int, tau_hi: int, pmin=0.0, pmax=1.0) -> list[dict]:
    """First qualifying minute per bucket (one trade per market). Signal uses the decision quote at t; fill at t+1."""
    out = {}; done = set()
    for r in sorted(rows, key=lambda r: (r["e"], r["f"], -r["tau"])):
        key = (r["e"], r["f"])
        if key in done or not (tau_lo <= r["tau"] <= tau_hi):
            continue
        ref = r[sig_key]
        if ref is None:
            continue
        if side == "YES":
            if r["a0"] is None or r["a1"] is None:
                continue
            edge = ref - r["a0"] - fee(r["a0"]); px = r["a1"]; won = r["won"]
        else:
            if r["b0"] is None or r["b1"] is None:
                continue
            edge = (1 - ref) - (1 - r["b0"]) - fee(1 - r["b0"]); px = 1 - r["b1"]; won = not r["won"]
        if edge < theta or not (pmin <= px <= pmax):
            continue
        done.add(key)
        out[key] = {"e": r["e"], "t_close": r["t_close"], "tau": r["tau"], "px": px, "won": won, "ret": ((1.0 if won else 0.0) - px - fee(px)) / px, "edge": edge}
    return list(out.values())


def ladder_arbs(asset: str, evs: list[dict]) -> dict:
    """(a) sum of bucket bids over the fetched buckets (buy NO on all: worst-case payoff N-1), (c) exact 3-leg arbs with the
    above/below ladder. Counted at decision minute t and re-checked with ALL legs at t+1 (10-contract legs, fee rounded up)."""
    w = RANGE[asset][3]
    res = {"minutes": 0, "sum_ask_med": None, "sum_bid_med": None, "no_all_arb_t": 0, "no_all_arb_t1": 0, "pnl_no_all_t1": 0.0,
           "x1_t": 0, "x1_t1": 0, "x2_t": 0, "x2_t1": 0, "x_pnl_t1": 0.0, "x_events_t1": set(), "x_edges": []}
    sa, sb = [], []
    for v in evs:
        T = v["T"]; up = v["up"]
        for tau in range(1, 59):
            t = T - tau * 60
            qs0 = {f: q(b["c"], t) for f, b in v["b"].items()}; qs1 = {f: q(b["c"], t + 60) for f, b in v["b"].items()}
            asks = [x[0] for x in qs0.values() if x and x[0]]; bids = [x[1] for x in qs0.values() if x and x[1]]
            if not asks:
                continue
            res["minutes"] += 1; sa.append(sum(asks)); sb.append(sum(bids))
            def no_all(qs):
                legs = [x[1] for x in qs.values() if x and x[1]]
                return sum(legs) - 1 - sum(fee(1 - b, 10) for b in legs) if legs else -1
            if no_all(qs0) > 0:
                res["no_all_arb_t"] += 1; e1 = no_all(qs1)
                if e1 > 0:
                    res["no_all_arb_t1"] += 1; res["pnl_no_all_t1"] += e1
            for f in v["b"]:
                s = round(f - 0.01, 2); ua, ub = up.get(s), up.get(round(s + w, 2))
                if not (ua and ub):
                    continue
                for tt, key in ((t, "t"), (t + 60, "t1")):
                    qb = (qs0 if key == "t" else qs1).get(f); qa = q(ua, tt); qu = q(ub, tt)
                    if not (qb and qa and qu):
                        continue
                    # combo 1: YES bucket + YES above(a+w) + NO above(a) pays exactly 1
                    if None not in (qb[0], qu[0], qa[1]):
                        e1 = 1 - (qb[0] + qu[0] + (1 - qa[1])) - fee(qb[0], 10) - fee(qu[0], 10) - fee(1 - qa[1], 10)
                        if key == "t":
                            res["x_edges"].append(e1)
                        if e1 > 0:
                            res["x1_" + key] += 1
                            if key == "t1":
                                res["x_pnl_t1"] += e1; res["x_events_t1"].add(v["e"])
                    # combo 2: NO bucket + NO above(a+w) + YES above(a) pays exactly 2
                    if None not in (qb[1], qu[1], qa[0]):
                        e2 = 2 - ((1 - qb[1]) + (1 - qu[1]) + qa[0]) - fee(1 - qb[1], 10) - fee(1 - qu[1], 10) - fee(qa[0], 10)
                        if key == "t":
                            res["x_edges"].append(e2)
                        if e2 > 0:
                            res["x2_" + key] += 1
                            if key == "t1":
                                res["x_pnl_t1"] += e2; res["x_events_t1"].add(v["e"])
    res["sum_ask_med"] = st.median(sa) if sa else None; res["sum_bid_med"] = st.median(sb) if sb else None
    res["sum_bid_max"] = max(sb) if sb else None; res["sum_ask_min"] = min(sa) if sa else None
    xe = sorted(res.pop("x_edges")); res["x_combos_checked"] = len(xe)
    res["x_edge_p50_p99_max"] = [xe[len(xe) // 2], xe[int(len(xe) * 0.99)], xe[-1]] if xe else None
    res["x_events_t1"] = len(res["x_events_t1"])
    return res


# ---------------------------------------------------------------- coin race
LEAD_MIN_CLOSE = 1791087300   # first close of the coin-race sample used for discovery/validation (fixed before validation)


def load_lead(holdout: bool = False) -> list[dict]:
    """Coin-race events. Main study: closes >= LEAD_MIN_CLOSE. holdout=True: the older events fetched AFTER the validation run
    (2026-10-01..10-04), used once as an extra out-of-sample check of the frozen rule."""
    ms = [json.loads(l) for l in (OUT / "lead_markets.jsonl").open()]
    cd = {}
    if (OUT / "candles_LEAD.jsonl").exists():
        for l in (OUT / "candles_LEAD.jsonl").open():
            x = json.loads(l)
            if x["c"]:
                cd[x["t"]] = x["c"]
    ev = defaultdict(list)
    for m in ms:
        ev[m["e"]].append(m)
    out = []
    for e, v in ev.items():
        if len(v) != 5 or sum(m["result"] == "yes" for m in v) != 1 or not all(m["t"] in cd for m in v):
            continue
        if (v[0]["close"] < LEAD_MIN_CLOSE) != holdout:
            continue
        out.append({"e": e, "T": v[0]["close"], "open": v[0]["open"], "m": {m["coin"]: {"t": m["t"], "c": cd[m["t"]], "won": m["result"] == "yes"} for m in v}})
    return sorted(out, key=lambda x: x["T"])


def chol(a: list[list[float]]) -> list[list[float]]:
    n = len(a); L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            L[i][j] = math.sqrt(max(s, 1e-18)) if i == j else s / L[j][j]
    return L


def lead_probs(spots: dict, t: int, T0: int, T: int, W: int = 60, Wc: int = 240, k: float = 1.0, n: int = 3000, rng=None) -> dict | None:
    cur = {}; sig = {}; R = {}
    for c in COINS:
        s0 = spots[c].spot(T0); s1 = spots[c].spot(t)
        v = spots[c].vol(t, W); r = spots[c].rets(t, Wc)
        if None in (s0, s1) or not v or len(r) < Wc // 2:
            return None
        cur[c] = math.log(s1 / s0); sig[c] = v; R[c] = r
    m = min(len(R[c]) for c in COINS)
    X = {c: R[c][-m:] for c in COINS}
    cov = [[0.0] * 5 for _ in range(5)]
    for i, a in enumerate(COINS):
        for j, b in enumerate(COINS):
            sa = math.sqrt(sum(x * x for x in X[a]) / m); sb = math.sqrt(sum(x * x for x in X[b]) / m)
            rho = sum(x * y for x, y in zip(X[a], X[b])) / m / (sa * sb) if sa > 0 and sb > 0 else (1.0 if i == j else 0.0)
            cov[i][j] = rho * sig[a] * sig[b] * k * k
    L = chol(cov); tau = (T - t) / 60
    if tau <= 0:
        return None
    rng = rng or random.Random(t); wins = [0] * 5; st_ = math.sqrt(tau)
    for _ in range(n):
        z = [rng.gauss(0, 1) for _ in range(5)]
        best = -1e9; bi = 0
        for i in range(5):
            x = cur[COINS[i]] + st_ * sum(L[i][j] * z[j] for j in range(i + 1))
            if x > best:
                best = x; bi = i
        wins[bi] += 1
    return {c: (wins[i] + 0.5) / (n + 2.5) for i, c in enumerate(COINS)}


def lead_rows(evs: list[dict], spots: dict, taus=(13, 11, 9, 7, 5, 4, 3, 2, 1), W=60, k=1.0) -> list[dict]:
    rows = []
    for v in evs:
        T = v["T"]; T0 = v["open"]
        for tau in taus:
            t = T - tau * 60
            p = lead_probs(spots, t, T0, T, W=W, k=k)
            if not p:
                continue
            for c, mk in v["m"].items():
                if c not in p:
                    continue
                q0 = q(mk["c"], t); q1 = q(mk["c"], t + 60)
                if not q0 or not q1:
                    continue
                rows.append({"e": v["e"], "t_close": T, "tau": tau, "f": c, "won": mk["won"], "pm": p[c], "imp": None,
                             "a0": q0[0], "b0": q0[1], "a1": q1[0], "b1": q1[1]})
    return rows


def lead_ladder(evs: list[dict]) -> dict:
    res = {"minutes": 0, "yes_all_t": 0, "yes_all_t1": 0, "no_all_t": 0, "no_all_t1": 0, "pnl_t1": 0.0, "sum_ask": [], "sum_bid": []}
    for v in evs:
        for tau in range(1, 15):
            t = v["T"] - tau * 60
            for tt, key in ((t, "t"), (t + 60, "t1")):
                qs = [q(mk["c"], tt) for mk in v["m"].values()]
                if not all(qs):
                    break
                if key == "t":
                    res["minutes"] += 1
                if all(x[0] for x in qs):
                    a = [x[0] for x in qs]; e = 1 - sum(a) - sum(fee(x, 10) for x in a)
                    if key == "t":
                        res["sum_ask"].append(sum(a))
                    if e > 0:
                        res["yes_all_" + key] += 1; res["pnl_t1"] += e if key == "t1" else 0
                if all(x[1] for x in qs):
                    b = [x[1] for x in qs]; e = sum(b) - 1 - sum(fee(1 - x, 10) for x in b)
                    if key == "t":
                        res["sum_bid"].append(sum(b))
                    if e > 0:
                        res["no_all_" + key] += 1; res["pnl_t1"] += e if key == "t1" else 0
    for k in ("sum_ask", "sum_bid"):
        x = sorted(res[k]); res[k] = {"n": len(x), "min": x[0], "p50": x[len(x) // 2], "max": x[-1]} if x else None
    return res


def calib_table(rows: list[dict], key: str = "pm") -> list[tuple]:
    """Model/market probability vs realised frequency in probability bins (discovery diagnostics)."""
    bins = (0, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 0.9, 1.01); out = []
    for lo, hi in zip(bins, bins[1:]):
        x = [r for r in rows if r[key] is not None and lo <= r[key] < hi]
        if x:
            mid = [((r["a0"] or 0) + (r["b0"] or 0)) / 2 for r in x]
            out.append((f"{lo:.2f}-{hi:.2f}", len(x), round(st.mean(r[key] for r in x), 4), round(st.mean(mid), 4), round(sum(r["won"] for r in x) / len(x), 4)))
    return out


def logloss(rows, f):
    s = 0; n = 0
    for r in rows:
        p = min(max(f(r), 0.005), 0.995); s -= math.log(p if r["won"] else 1 - p); n += 1
    return s / n if n else float("nan")


# ---------------------------------------------------------------- driver
GRID_TAU_R = ((2, 4), (5, 14), (15, 29), (30, 58))
GRID_TAU_L = ((2, 4), (5, 8), (9, 13))
THETAS = (0.03, 0.05, 0.08, 0.12)
KS = (0.8, 1.0, 1.25)


def basis_est(evs: list[dict], spot: Spot, w: float) -> dict:
    """Coinbase average of the last minute vs the settled bucket (BRTI 60-s average)."""
    off = []; inside = 0
    for v in evs:
        c = spot.cb.get(v["T"] - 60)
        if not c:
            continue
        x = (c[2] + c[3]) / 2; lo = v["settle_lo"] + 0.01
        off.append(x - (lo + w / 2)); inside += lo <= x < lo + w
    return {"n": len(off), "inside": inside / len(off) if off else None, "mean_off": st.mean(off) if off else None,
            "median_off": st.median(off) if off else None}


def with_k(rows: list[dict], k: float, w: float, basis: float) -> list[dict]:
    out = []
    for r in rows:
        r2 = dict(r); r2["pm"] = p_bucket(r["S"], r["sig"], r["tau"], r["f"], r["f"] + w, basis, k); out.append(r2)
    return out


def range_rows_fast(asset: str, evs: list[dict], spot: Spot) -> list[dict]:
    rows = range_rows(asset, evs, spot)
    for r in rows:   # keep the inputs so that the vol multiplier k can be varied without recomputing quotes
        T = r["t_close"]; t = T - r["tau"] * 60; r["S"] = spot.spot(t); r["sig"] = spot.vol(t, 60)
    return rows


def grid(fam: str, rows_by_k: dict, taus, use_imp: bool) -> dict:
    res = {}
    for k, rows in rows_by_k.items():
        for side in ("YES", "NO"):
            for th in THETAS:
                for lo, hi in taus:
                    if use_imp:
                        tr = trades_from(rows, side, "imp", th, lo, hi)
                        name = f"{fam}|rv|{side}|th{th}|tau{lo}-{hi}"
                    else:
                        tr = trades_from(rows, side, "pm", th, lo, hi)
                        name = f"{fam}|model|k{k}|{side}|th{th}|tau{lo}-{hi}"
                    if name not in res:
                        res[name] = stats(tr)
        if use_imp:
            break
    return res


def main(step: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {}
    btc = load_range("BTC"); spot_b = Spot("btc")
    d_b, v_b, cut_b = split(btc)
    print(f"BTC range events {len(btc)} (discovery {len(d_b)}, validation {len(v_b)}), buckets/event {st.mean(len(v['b']) for v in btc):.0f}, "
          f"with above/below ladder {sum(bool(v['up']) for v in btc)}")
    lead = load_lead() if (OUT / "candles_LEAD.jsonl").exists() else []
    spots = {c: Spot(c.lower()) for c in COINS}
    if lead:
        d_l, v_l, cut_l = split(lead)
        print(f"coin race events {len(lead)} (discovery {len(d_l)}, validation {len(v_l)})")
    eth = []
    if (OUT / "candles_KXETH.jsonl").exists():
        eth = load_range("ETH"); spot_e = Spot("eth"); d_e, v_e, cut_e = split(eth)
        print(f"ETH range events {len(eth)} (discovery {len(d_e)}, validation {len(v_e)})")

    if step == "explore":
        bb = basis_est(d_b, spot_b, 100.0); print("BTC basis (discovery):", bb); report["btc_basis"] = bb
        rows = range_rows_fast("BTC", d_b, spot_b)
        report["btc_rows_disc"] = len(rows)
        both = [r for r in rows if r["imp"] is not None and r["a0"] and r["b0"]]
        mid = lambda r: (r["a0"] + r["b0"]) / 2
        print("BTC discovery rows", len(rows), "with ladder-implied", len(both))
        print("  logloss model", round(logloss(both, lambda r: r["pm"]), 4), "bucket mid", round(logloss(both, mid), 4),
              "ladder implied", round(logloss(both, lambda r: r["imp"]), 4))
        for tau_lo, tau_hi in GRID_TAU_R:
            x = [r for r in both if tau_lo <= r["tau"] <= tau_hi]
            print(f"  tau {tau_lo}-{tau_hi}: n={len(x)} LL model {logloss(x, lambda r: r['pm']):.4f} mid {logloss(x, mid):.4f} implied {logloss(x, lambda r: r['imp']):.4f}"
                  f" | mean(mid-implied) {st.mean(mid(r) - r['imp'] for r in x):+.4f}")
        print("  calibration (model):", calib_table(rows, "pm"))
        print("  calibration (implied):", calib_table(both, "imp"))
        for name, evs in (("discovery", d_b), ("validation", v_b)):
            a = ladder_arbs("BTC", evs); report[f"btc_arbs_{name}"] = a; print(f"BTC ladder/cross arbs ({name}):", a)
        if lead:
            for name, evs in (("discovery", d_l), ("validation", v_l)):
                a = lead_ladder(evs); report[f"lead_ladder_{name}"] = a; print(f"coin race ladder ({name}):", a)
            lr = lead_rows(d_l, spots)
            print("coin race discovery rows", len(lr), "LL model", round(logloss(lr, lambda r: r["pm"]), 4),
                  "mid", round(logloss([r for r in lr if r["a0"] and r["b0"]], lambda r: (r["a0"] + r["b0"]) / 2), 4))
            print("  calibration (model):", calib_table(lr, "pm"))
            for tl, th_ in GRID_TAU_L:
                x = [r for r in lr if tl <= r["tau"] <= th_ and r["a0"] and r["b0"]]
                if x:
                    print(f"  tau {tl}-{th_}: n={len(x)} LL model {logloss(x, lambda r: r['pm']):.4f} mid {logloss(x, lambda r: (r['a0'] + r['b0']) / 2):.4f}")
        if eth:
            be = basis_est(d_e, spot_e, 5.0); print("ETH basis (discovery):", be)
            for name, evs in (("discovery", d_e), ("validation", v_e)):
                a = ladder_arbs("ETH", evs); report[f"eth_arbs_{name}"] = a; print(f"ETH ladder/cross arbs ({name}):", a)
        (OUT / "explore.json").write_text(json.dumps(report, default=str, indent=1))

    elif step == "select":
        bb = basis_est(d_b, spot_b, 100.0)["median_off"]
        rows = range_rows_fast("BTC", d_b, spot_b)
        res = {}
        res.update(grid("BTC", {k: with_k(rows, k, 100.0, -bb if False else 0.0) for k in KS}, GRID_TAU_R, False))
        res.update(grid("BTC", {1.0: rows}, GRID_TAU_R, True))
        if lead:
            lr = {k: lead_rows(d_l, spots, k=k) for k in KS}
            res.update(grid("LEAD", lr, GRID_TAU_L, False))
        if eth:
            er = range_rows_fast("ETH", d_e, spot_e)
            res.update(grid("ETH", {k: with_k(er, k, 5.0, 0.0) for k in KS}, GRID_TAU_R, False))
            res.update(grid("ETH", {1.0: er}, GRID_TAU_R, True))
        ok = {k: v for k, v in res.items() if v.get("n", 0) >= 30}
        print(f"variants {len(res)}, with n>=30: {len(ok)}")
        for k, v in sorted(ok.items(), key=lambda kv: -kv[1]["ret"])[:25]:
            print(f"  {k:45s} {fmt(v)}")
        # frozen candidates (rule fixed before validation): per family (BTC / ETH / LEAD) the discovery variant with the highest
        # clustered t among n >= 30, mean >= +10% and mean > 0 without the 3 best trades; at most 3 in total. (No variant reached t >= 2.)
        cand = sorted([(k, v) for k, v in ok.items() if v["ret"] >= 0.10 and v["ret_wo3"] > 0], key=lambda kv: -kv[1]["t"])
        frozen = []
        for k, v in cand:
            if all(f["name"].split("|")[0] != k.split("|")[0] for f in frozen):
                frozen.append({"name": k, "disc": v})
            if len(frozen) == 3:
                break
        print("FROZEN:", [f["name"] for f in frozen])
        (OUT / "select.json").write_text(json.dumps({"variants": len(res), "results": res}, default=str))
        (OUT / "frozen.json").write_text(json.dumps({"frozen": frozen, "variants": len(res), "cut_btc": cut_b}, default=str, indent=1))

    elif step == "holdout":
        fz = [f for f in json.loads((OUT / "frozen.json").read_text())["frozen"] if f["name"].startswith("LEAD")]
        hold = load_lead(holdout=True); print("coin-race holdout events", len(hold))
        res = []
        for f in fz:
            parts = f["name"].split("|"); k = float(parts[2][1:]); side = parts[3]; th = float(parts[4][2:]); lo, hi = map(int, parts[5][3:].split("-"))
            tr = trades_from(lead_rows(hold, spots, k=k), side, "pm", th, lo, hi); v = stats(tr)
            print(f"HOLDOUT {f['name']:45s} {fmt(v)}"); res.append({"rule": f["name"], "holdout": v, "events": len(hold)})
        (OUT / "holdout.json").write_text(json.dumps(res, default=str, indent=1))

    elif step == "validate":
        fz = json.loads((OUT / "frozen.json").read_text())["frozen"]
        out = []
        for f in fz:
            parts = f["name"].split("|"); fam = parts[0]
            if parts[1] == "model":
                k = float(parts[2][1:]); side = parts[3]; th = float(parts[4][2:]); lo, hi = map(int, parts[5][3:].split("-"))
                if fam == "LEAD":
                    rows = lead_rows(v_l, spots, k=k)
                else:
                    a, evs, sp, w = ("BTC", v_b, spot_b, 100.0) if fam == "BTC" else ("ETH", v_e, spot_e, 5.0)
                    rows = with_k(range_rows_fast(a, evs, sp), k, w, 0.0)
                tr = trades_from(rows, side, "pm", th, lo, hi)
            else:
                side = parts[2]; th = float(parts[3][2:]); lo, hi = map(int, parts[4][3:].split("-"))
                a, evs, sp = ("BTC", v_b, spot_b) if fam == "BTC" else ("ETH", v_e, spot_e)
                tr = trades_from(range_rows_fast(a, evs, sp), side, "imp", th, lo, hi)
            v = stats(tr); print(f"VALIDATION {f['name']:45s} {fmt(v)}"); out.append({"rule": f["name"], "disc": f["disc"], "val": v, "trades": tr})
        (OUT / "validation.json").write_text(json.dumps(out, default=str, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "explore")
