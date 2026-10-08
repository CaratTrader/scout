"""r2_mentions_baserate: are Kalshi "will <speaker> say <word> during <appearance>" markets mispriced against base rates?

Data (collected by r2_mentions_baserate_data.py, 125 Kalshi calls):
  markets_recent.jsonl  settled mention markets closed 2026-08-01..10-08 (live tier), 22 series, 240 events
  candles.jsonl         hourly candles (yes_ask / yes_bid close) for every market of 229 events (KXTRUMPSAY skipped)
  markets_hist.jsonl    /historical results (settled before 2026-08-08) for 14 series: word base rates only, no prices

Decision rule (no look-ahead):
  * decision hours H = hourly candle closes with open_time + 1 h <= H, the market still open at H, and NO market of the
    same event closed at or before H. Mention markets close early when the word is said, so "some market of the event has
    closed" is visible to a live trader at H; "none has closed" means the appearance has not visibly started. The actual
    start time is never used.
  * quote = the candle at H (yes_ask / yes_bid close = the book at H); a 1-2 min reaction pre-event fills at that quote.
  * taker fee 0.07 p (1-p) per contract, rounded UP to the cent per order of floor($5 / p) contracts.
  * base rates use only markets settled (close_time) at least 1 h before H, from other events, same speaker group.
Split: events ordered by their last close; discovery = first 70%, validation = last 30%. Every parameter is chosen on
discovery. Usage: python -m lab.kalshi.strategies.r2_mentions_baserate [step1|step2|validate|all]"""
from __future__ import annotations
import json, math, random, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/r2_mentions_baserate")
GROUP = {"KXTRUMPMENTIONB": "KXTRUMPMENTION"}           # same speaker, same format
SKIP = {"KXTRUMPSAY"}                                   # weekly accumulators: different format, no candles fetched
STAKE = 5.0
SPLIT = 0.7


def ts(s):
    import datetime as dt
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def wkey(m: dict) -> str:
    w = (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""
    parts = sorted(p.strip() for p in re.sub(r"[^a-z0-9/+() ]", "", w.lower()).split("/") if p.strip())
    return "/".join(parts)


def fee_pc(p: float) -> float:
    """Taker fee per contract for a $5 order at price p (Kalshi rounds the order's fee up to the cent)."""
    n = max(1, int(STAKE / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def load():
    R = [json.loads(l) for l in (OUT / "markets_recent.jsonl").open()]
    Hh = [json.loads(l) for l in (OUT / "markets_hist.jsonl").open()]
    C = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l); C[x["t"]] = x["c"]
    pool = {}
    for m in Hh + R:
        pool[m["ticker"]] = {"g": GROUP.get(m["series"], m["series"]), "w": wkey(m), "close": ts(m["close_time"]), "y": m["result"] == "yes",
                             "e": m["event_ticker"]}
    mk = [m for m in R if m["series"] not in SKIP and m["ticker"] in C]
    ev = defaultdict(list)
    for m in mk:
        m["open_ts"], m["close_ts"] = ts(m["open_time"]), ts(m["close_time"]); m["y"] = m["result"] == "yes"
        m["g"] = GROUP.get(m["series"], m["series"]); m["w"] = wkey(m)
        ev[m["event_ticker"]].append(m)
    events = {}
    for e, x in ev.items():
        events[e] = {"first_close": min(m["close_ts"] for m in x), "last_close": max(m["close_ts"] for m in x), "series": x[0]["series"], "n": len(x)}
    order = sorted(events, key=lambda e: (events[e]["last_close"], e))
    cut = int(len(order) * SPLIT)
    for i, e in enumerate(order):
        events[e]["part"] = "disc" if i < cut else "val"
    return mk, events, C, list(pool.values())


class BaseRates:
    """Word hit counts by (speaker group, word), queried as of time T (only markets closed before T - 1 h)."""

    def __init__(self, pool):
        self.by_w = defaultdict(list); self.by_g = defaultdict(list)
        for p in pool:
            self.by_w[(p["g"], p["w"])].append((p["close"], p["y"], p["e"])); self.by_g[p["g"]].append((p["close"], p["y"], p["e"]))
        for v in list(self.by_w.values()) + list(self.by_g.values()):
            v.sort()

    def word(self, g, w, T, e, last_n=None):
        x = [y for c, y, ee in self.by_w.get((g, w), []) if c < T - 3600 and ee != e]
        if last_n:
            x = x[-last_n:]
        return sum(x), len(x)

    def group(self, g, T, e):
        x = [y for c, y, ee in self.by_g.get(g, []) if c < T - 3600 and ee != e]
        return sum(x), len(x)


def decision_hours(m, ev, C):
    """Eligible (H, ask, bid) for market m: open >= 1 h, market open, no market of the event closed yet."""
    out = []
    fc = ev[m["event_ticker"]]["first_close"]
    for r in C[m["ticker"]]:
        H, ask, bid = r[0], r[1], r[2]
        if H < m["open_ts"] + 3600 or H >= m["close_ts"] or H >= fc:
            continue
        if ask is None or bid is None:
            continue
        out.append((H, ask, bid))
    return out


def ret_yes(ask, y):
    return ((1.0 if y else 0.0) - ask - fee_pc(ask)) / ask


def ret_no(bid, y):
    px = 1 - bid
    return ((0.0 if y else 1.0) - px - fee_pc(px)) / px


def cstats(rows):
    """rows: dicts with e, ret, won, px, tclose. Clustered-by-event t, mean w/o 3 best, halves by event close."""
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; ne = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(ne - 1)) if ne > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    mid = sorted(r["tclose"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["tclose"] < mid]; h2 = [r["ret"] for r in rows if r["tclose"] >= mid]
    return {"n": len(rows), "events": ne, "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2), "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None}


def logit(p):
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def fit_logit(X, y, iters=50, l2=1e-6):
    """Plain Newton-Raphson logistic regression (with intercept column already in X)."""
    k = len(X[0]); b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; Hm = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = sum(a * c for a, c in zip(b, xi)); p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            for i in range(k):
                g[i] += (yi - p) * xi[i]
                for j in range(k):
                    Hm[i][j] += p * (1 - p) * xi[i] * xi[j]
        for i in range(k):
            Hm[i][i] += l2; g[i] -= l2 * b[i]
        step = solve(Hm, g)
        b = [a + s for a, s in zip(b, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return b


def solve(A, v):
    n = len(v); M = [row[:] + [v[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c])); M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[c][c] != 0:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def cluster_boot(obs, fn, reps=200, seed=7):
    """obs: list of (event, x, y). Bootstrap over events; returns (estimate, list of replicate estimates)."""
    by = defaultdict(list)
    for o in obs:
        by[o[0]].append(o)
    keys = list(by); rng = random.Random(seed); est = fn(obs); reps_out = []
    for _ in range(reps):
        s = [o for k in (rng.choice(keys) for _ in keys) for o in by[k]]
        reps_out.append(fn(s))
    return est, reps_out


def ll(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(math.log(p) if y else math.log(1 - p))


# ----------------------------------------------------------------------------------------------------------------- step 1
def snapshots(mk, events, C, which):
    """One quote per market: 'first' eligible hour or 'last' eligible hour (last = diagnostic for calibration only)."""
    out = []
    for m in mk:
        hs = decision_hours(m, events, C)
        hs = [h for h in hs if 0 < h[2] <= h[1] < 1]
        if not hs:
            continue
        H, ask, bid = hs[0] if which == "first" else hs[-1]
        out.append({"m": m, "H": H, "ask": ask, "bid": bid, "mid": (ask + bid) / 2, "e": m["event_ticker"], "part": events[m["event_ticker"]]["part"],
                    "tclose": events[m["event_ticker"]]["last_close"], "hrs_before_first_close": (events[m["event_ticker"]]["first_close"] - H) / 3600})
    return out


BANDS = (0.0, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 1.0)


def band(p):
    for lo, hi in zip(BANDS, BANDS[1:]):
        if lo <= p < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "1.00"


def step1(mk, events, C, part="disc", verbose=True):
    res = {}
    for which in ("first", "last"):
        S = [s for s in snapshots(mk, events, C, which) if s["part"] == part]
        obs = [(s["e"], logit(s["mid"]), s["m"]["y"]) for s in S]

        def slope(o):
            b = fit_logit([[1.0, x] for _, x, _ in o], [1 if y else 0 for _, _, y in o])
            return b
        b, reps = cluster_boot(obs, slope, reps=150)
        sl = sorted(r[1] for r in reps); ic = sorted(r[0] for r in reps)
        tab = defaultdict(list)
        for s in S:
            tab[band(s["mid"])].append(s)
        bands = {}
        for k in sorted(tab):
            v = tab[k]
            yes = [{"e": s["e"], "ret": ret_yes(s["ask"], s["m"]["y"]), "won": s["m"]["y"], "px": s["ask"], "tclose": s["tclose"]} for s in v]
            no = [{"e": s["e"], "ret": ret_no(s["bid"], s["m"]["y"]), "won": not s["m"]["y"], "px": 1 - s["bid"], "tclose": s["tclose"]} for s in v]
            bands[k] = {"n": len(v), "events": len({s["e"] for s in v}), "mean_mid": round(st.mean(s["mid"] for s in v), 3),
                        "yes_rate": round(st.mean(s["m"]["y"] for s in v), 3), "spread": round(st.mean(s["ask"] - s["bid"] for s in v), 3),
                        "taker_YES": cstats(yes), "taker_NO": cstats(no)}
        brier_mid = st.mean((s["mid"] - s["m"]["y"]) ** 2 for s in S)
        res[which] = {"n": len(S), "events": len({s["e"] for s in S}), "intercept": round(b[0], 3), "slope": round(b[1], 3),
                      "slope_ci90": [round(sl[int(0.05 * len(sl))], 3), round(sl[int(0.95 * len(sl)) - 1], 3)],
                      "intercept_ci90": [round(ic[int(0.05 * len(ic))], 3), round(ic[int(0.95 * len(ic)) - 1], 3)],
                      "brier_mid": round(brier_mid, 4), "logloss_mid": round(st.mean(ll(s["mid"], s["m"]["y"]) for s in S), 4),
                      "median_hrs_before_first_close": round(st.median(s["hrs_before_first_close"] for s in S), 2),
                      "median_spread": round(st.median(s["ask"] - s["bid"] for s in S), 3), "bands": bands}
        if verbose:
            r = res[which]
            print(f"\n[{part}] snapshot={which}: n={r['n']} events={r['events']} slope={r['slope']} CI90={r['slope_ci90']} intercept={r['intercept']} "
                  f"CI90={r['intercept_ci90']} brier={r['brier_mid']} median hrs before first close={r['median_hrs_before_first_close']} spread={r['median_spread']}")
            for k, v in bands.items():
                ty, tn = v["taker_YES"], v["taker_NO"]
                print(f"  mid {k}: n={v['n']:4d} ev={v['events']:3d} mid={v['mean_mid']:.3f} yes={v['yes_rate']:.3f} spr={v['spread']:.3f} | "
                      f"YES {ty['ret_per_dollar']:+.3f} t={ty['t']} | NO {tn['ret_per_dollar']:+.3f} t={tn['t']}")
    return res


# ----------------------------------------------------------------------------------------------------------------- step 2
def add_baserates(S, BR, a=3.0, last_n=None):
    for s in S:
        m = s["m"]; k, n = BR.word(m["g"], m["w"], s["H"], m["event_ticker"], last_n); kg, ng = BR.group(m["g"], s["H"], m["event_ticker"])
        p0 = (kg + 1) / (ng + 2)
        s["k"], s["n"], s["p0"] = k, n, p0
        s["pbr"] = (k + a * p0) / (n + a)
    return S


def step2_info(mk, events, C, pool, part="disc"):
    """Does the word base rate carry information the mid lacks? y ~ logit(mid) + logit(p_br) (+ n>=nmin only)."""
    BR = BaseRates(pool); res = {}
    for which in ("first", "last"):
        S = add_baserates([s for s in snapshots(mk, events, C, which) if s["part"] == part], BR)
        for nmin in (0, 3, 10):
            T = [s for s in S if s["n"] >= nmin]
            obs = [(s["e"], (logit(s["mid"]), logit(s["pbr"])), s["m"]["y"]) for s in T]
            fn = lambda o: fit_logit([[1.0, x[0], x[1]] for _, x, _ in o], [1 if y else 0 for _, _, y in o])
            b, reps = cluster_boot(obs, fn, reps=150)
            cb = sorted(r[2] for r in reps)
            se = st.pstdev([r[2] for r in reps])
            brier_br = st.mean((s["pbr"] - s["m"]["y"]) ** 2 for s in T); brier_mid = st.mean((s["mid"] - s["m"]["y"]) ** 2 for s in T)
            r = {"n": len(T), "events": len({s['e'] for s in T}), "b_int": round(b[0], 3), "b_mid": round(b[1], 3), "b_br": round(b[2], 3),
                 "b_br_ci90": [round(cb[int(0.05 * len(cb))], 3), round(cb[int(0.95 * len(cb)) - 1], 3)], "b_br_z": round(b[2] / se, 2) if se else None,
                 "brier_mid": round(brier_mid, 4), "brier_baserate": round(brier_br, 4)}
            res[f"{which}|nmin{nmin}"] = r
            print(f"[{part}] {which} nmin={nmin}: {r}")
    return res


def fit_m1(mk, events, C, BR):
    """M1: y ~ logit(mid) + logit(p_br) on DISCOVERY first-eligible snapshots."""
    S = add_baserates([s for s in snapshots(mk, events, C, "first") if s["part"] == "disc"], BR)
    return fit_logit([[1.0, logit(s["mid"]), logit(s["pbr"])] for s in S], [1 if s["m"]["y"] else 0 for s in S])


def candidates(mk, events, C, BR, part, schedule):
    """Quotes a live bot would evaluate: 'first' = first eligible hour only; 'ff6' = every eligible hour up to open + 6 h."""
    out = []
    for m in mk:
        if events[m["event_ticker"]]["part"] != part:
            continue
        hs = [h for h in decision_hours(m, events, C) if 0 < h[2] <= h[1] < 1]
        if schedule == "first":
            hs = hs[:1]
        elif schedule == "ff6":
            hs = [h for h in hs if h[0] <= m["open_ts"] + 6 * 3600]
        out.append([{"m": m, "H": H, "ask": a, "bid": b, "mid": (a + b) / 2, "e": m["event_ticker"], "tclose": events[m["event_ticker"]]["last_close"]}
                    for H, a, b in hs])
    for seq in out:
        add_baserates(seq, BR)
    return out


def run_rule(cands, model, side, thr, b1=None, nmin=3):
    """First quote per market where the model's expected return per $ (after fee) >= thr. One trade per market."""
    rows = []
    for seq in cands:
        for s in seq:
            if model == "M1":
                p = 1 / (1 + math.exp(-(b1[0] + b1[1] * logit(s["mid"]) + b1[2] * logit(s["pbr"]))))
            else:
                if s["n"] < nmin:
                    continue
                p = s["pbr"]
            y = s["m"]["y"]; fired = None
            if side in ("YES", "both"):
                ev = (p - s["ask"] - fee_pc(s["ask"])) / s["ask"]
                if ev >= thr:
                    fired = ("YES", s["ask"], ret_yes(s["ask"], y), y)
            if fired is None and side in ("NO", "both"):
                px = 1 - s["bid"]; ev = ((1 - p) - px - fee_pc(px)) / px
                if ev >= thr:
                    fired = ("NO", px, ret_no(s["bid"], y), not y)
            if fired:
                rows.append({"e": s["e"], "ret": fired[2], "won": fired[3], "px": fired[1], "tclose": s["tclose"], "side": fired[0], "t": s["m"]["ticker"],
                             "H": s["H"], "p": p, "ask": s["ask"], "bid": s["bid"], "vol_h": None})
                break
    return rows


GRID = [(model, sched, side, thr) for model in ("M1", "M2") for sched in ("first", "ff6") for side in ("YES", "NO", "both") for thr in (0.10, 0.20, 0.30)]


def step2_grid(mk, events, C, pool):
    BR = BaseRates(pool); b1 = fit_m1(mk, events, C, BR)
    print("M1 coefficients (discovery):", [round(x, 3) for x in b1])
    cache = {sch: candidates(mk, events, C, BR, "disc", sch) for sch in ("first", "ff6")}
    res = []
    for model, sched, side, thr in GRID:
        r = cstats(run_rule(cache[sched], model, side, thr, b1))
        r["rule"] = f"{model}|{sched}|{side}|thr{thr:.2f}"; res.append(r)
        print(f"  {r['rule']:24s} n={r.get('n',0):4d} ev={r.get('events',0):3d} win={r.get('win')} px={r.get('avg_px')} ret={r.get('ret_per_dollar')} t={r.get('t')} wo3={r.get('ret_wo3')} h={r.get('half1')}/{r.get('half2')}")
    return {"b1": b1, "grid": res}


def load_full():
    F = {}
    for l in (OUT / "candles_full.jsonl").open():
        x = json.loads(l); F[x["t"]] = x["c"]
    return F


def run_maker(cands, F, events, side, thr, b1, window="B"):
    """Rest a no-fee maker order one tick inside the spread at the first eligible hour on the side M1 favours.
    Filled if a later hourly candle printed a trade strictly through the order price (YES bid b: trade low < b; NO = sell
    YES at s: trade high > s). Window B (primary, conservative) also counts the candle in which the first market of the
    event closed (in-event adverse fills); window A stops at candles ending at or before the first close."""
    rows = []; posted = 0
    for seq in cands:
        if not seq:
            continue
        s = seq[0]; m = s["m"]; y = m["y"]
        p = 1 / (1 + math.exp(-(b1[0] + b1[1] * logit(s["mid"]) + b1[2] * logit(s["pbr"]))))
        order = None
        b = round(s["bid"] + 0.01, 2)
        if side in ("YES", "both") and b < s["ask"] and (p - b) / b >= thr:
            order = ("YES", b)
        sp = round(s["ask"] - 0.01, 2)
        if order is None and side in ("NO", "both") and sp > s["bid"] and ((1 - p) - (1 - sp)) / (1 - sp) >= thr:
            order = ("NO", sp)
        if not order:
            continue
        posted += 1
        fc = events[m["event_ticker"]]["first_close"]; end = fc + 3600 if window == "B" else fc
        filled = False
        for r in F.get(m["ticker"], []):
            if r[0] <= s["H"] or r[0] > end or r[0] > m["close_ts"] + 3600:
                continue
            lo, hi = r[5], r[6]
            if order[0] == "YES" and lo is not None and lo < order[1]:
                filled = True; break
            if order[0] == "NO" and hi is not None and hi > order[1]:
                filled = True; break
        if not filled:
            continue
        if order[0] == "YES":
            px = order[1]; ret = ((1.0 if y else 0.0) - px) / px; won = y
        else:
            px = 1 - order[1]; ret = ((0.0 if y else 1.0) - px) / px; won = not y
        rows.append({"e": s["e"], "ret": ret, "won": won, "px": px, "tclose": s["tclose"], "side": order[0], "t": m["ticker"], "H": s["H"], "p": p})
    return rows, posted


MAKER_GRID = [(side, thr) for side in ("YES", "NO", "both") for thr in (0.10, 0.20, 0.30)]


def step2_maker(mk, events, C, pool):
    BR = BaseRates(pool); b1 = fit_m1(mk, events, C, BR); F = load_full()
    cands = candidates(mk, events, C, BR, "disc", "first"); res = []
    for side, thr in MAKER_GRID:
        for w in ("B", "A"):
            rows, posted = run_maker(cands, F, events, side, thr, b1, w)
            r = cstats(rows); r["rule"] = f"MAKER|M1|first|{side}|thr{thr:.2f}|win{w}"; r["posted"] = posted; res.append(r)
            print(f"  {r['rule']:34s} posted={posted:4d} filled n={r.get('n',0):4d} ev={r.get('events',0):3d} win={r.get('win')} px={r.get('avg_px')} ret={r.get('ret_per_dollar')} t={r.get('t')} wo3={r.get('ret_wo3')} h={r.get('half1')}/{r.get('half2')}")
    return res


# ------------------------------------------------------------------------------------------------- frozen candidates
# Frozen 2026-10-08 on DISCOVERY only (160 events, closes 2026-08-03..2026-09-18 11:39 UTC), before any validation run.
M1_FROZEN = [-0.09174520607273995, 0.9701165898132638, 0.385610892255977]
FROZEN = {
    "C1_maker_no_all": {"side": "NO", "thr": -9.0},     # sell YES at ask-1c at the first eligible hour, every market
    "C2_maker_no_br20": {"side": "NO", "thr": 0.20},    # ... only where M1 says the NO at 1-(ask-1c) has EV >= +20%/$
    "C3_maker_no_br10": {"side": "NO", "thr": 0.10},    # ... EV >= +10%/$
}


def validate(mk, events, C, pool, part="val"):
    BR = BaseRates(pool); F = load_full()
    cands = candidates(mk, events, C, BR, part, "first")
    info = {seq[0]["m"]["ticker"]: seq[0] for seq in cands if seq}
    out = {}
    for name, cfg in FROZEN.items():
        rows, posted = run_maker(cands, F, events, cfg["side"], cfg["thr"], M1_FROZEN, "B")
        r = cstats(rows); r["rule"] = name; r["posted"] = posted
        rA, _ = run_maker(cands, F, events, cfg["side"], cfg["thr"], M1_FROZEN, "A")
        r["windowA_ret"] = cstats(rA).get("ret_per_dollar")
        # capacity proxy: volume of the candle in which the order filled, and orders per day
        vols = []
        for x in rows:
            s0 = info[x["t"]]; sp = round(s0["ask"] - 0.01, 2)
            for c in F.get(x["t"], []):
                if c[0] > s0["H"] and c[6] is not None and c[6] > sp:
                    vols.append(c[4]); break
        r["median_fill_candle_volume"] = round(st.median(vols), 1) if vols else None
        days = (max(x["tclose"] for x in rows) - min(x["tclose"] for x in rows)) / 86400 if rows else 0
        r["trades_per_day"] = round(len(rows) / days, 1) if days else None
        by_s = defaultdict(list)
        for x in rows:
            by_s[x["t"].split("-")[0]].append(x)
        r["by_series"] = {k: {kk: cstats(v)[kk] for kk in ("n", "events", "ret_per_dollar")} for k, v in sorted(by_s.items(), key=lambda kv: -len(kv[1]))}
        out[name] = r
        print(f"[{part}] {name}: posted={posted} " + json.dumps({k: v for k, v in r.items() if k not in ('by_series', 'rule')}))
    return out


def fill_class(s0, m, F, events):
    """'full' if a later candle printed >= 2c through the order with >= 20 contracts traded in it; 'marginal' if only
    1c-through prints (or low volume); None if never through. Trade-print samples (calls 127-193) measured fills:
    full -> 20/20 complete; marginal -> mean fraction 0.74 (19/28 complete) on validation orders."""
    sp = round(s0["ask"] - 0.01, 2); end = events[m["event_ticker"]]["first_close"] + 3600
    cs = [r for r in F.get(m["ticker"], []) if s0["H"] < r[0] <= end and r[0] <= m["close_ts"] + 3600 and r[6] is not None]
    if not any(r[6] > sp + 1e-9 for r in cs):
        return None
    return "full" if any(r[6] >= sp + 0.02 - 1e-9 and r[4] >= 20 for r in cs) else "marginal"


def size_weighted(mk, events, C, pool, part="val"):
    """Equal-$ orders, but each fill weighted by its estimated filled fraction: 1 for 'full', the measured trade-print
    fraction for sampled marginal fills, the sample mean for the other marginal fills."""
    BR = BaseRates(pool); F = load_full()
    meas = {r[0]: r[5] for r in json.loads((OUT / "marginal_fill_fractions.json").read_text())}
    fbar = st.mean(meas.values())
    cands = candidates(mk, events, C, BR, part, "first"); out = {}
    for name, cfg in FROZEN.items():
        rows, _ = run_maker(cands, F, events, cfg["side"], cfg["thr"], M1_FROZEN, "B")
        info = {seq[0]["m"]["ticker"]: seq[0] for seq in cands if seq}
        ev = defaultdict(lambda: [0.0, 0.0]); W = 0.0; S = 0.0; h = defaultdict(lambda: [0.0, 0.0])
        mid = sorted(r["tclose"] for r in rows)[len(rows) // 2]
        for r in rows:
            m = info[r["t"]]["m"]; c = fill_class(info[r["t"]], m, F, events)
            w = 1.0 if c == "full" else meas.get(r["t"], fbar)
            ev[r["e"]][0] += w * r["ret"]; ev[r["e"]][1] += w; W += w; S += w * r["ret"]
            hh = h["h1" if r["tclose"] < mid else "h2"]; hh[0] += w * r["ret"]; hh[1] += w
        em = [a / b for a, b in ev.values() if b > 0]
        t = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em) - 1)) if len(em) > 2 else float("nan")
        out[name] = {"ret_per_dollar_size_weighted": round(S / W, 4), "t_event_means": round(t, 2), "filled_dollar_share": round(W / len(rows), 3),
                     "half1": round(h["h1"][0] / h["h1"][1], 4), "half2": round(h["h2"][0] / h["h2"][1], 4), "marginal_fill_mean_used": round(fbar, 3)}
        print(f"[{part}] {name} size-weighted: {out[name]}")
    return out


if __name__ == "__main__":
    mk, events, C, pool = load()
    what = sys.argv[1] if len(sys.argv) > 1 else "step1"
    n_d = sum(1 for e in events.values() if e["part"] == "disc"); n_v = len(events) - n_d
    print(f"markets {len(mk)}, events {len(events)} (discovery {n_d}, validation {n_v})")
    if what == "step1":
        r = step1(mk, events, C)
        (OUT / "step1_discovery.json").write_text(json.dumps(r, indent=1))
    elif what == "step2grid":
        r = step2_grid(mk, events, C, pool)
        (OUT / "step2_grid_discovery.json").write_text(json.dumps(r, indent=1))
    elif what == "step2maker":
        r = step2_maker(mk, events, C, pool)
        (OUT / "step2_maker_discovery.json").write_text(json.dumps(r, indent=1))
    elif what == "discovery_frozen":
        r = validate(mk, events, C, pool, "disc")
        (OUT / "frozen_on_discovery.json").write_text(json.dumps(r, indent=1))
    elif what == "validate":
        r = validate(mk, events, C, pool, "val")
        (OUT / "validation.json").write_text(json.dumps(r, indent=1))
    elif what == "sizeweighted":
        r = {p: size_weighted(mk, events, C, pool, p) for p in ("disc", "val")}
        (OUT / "size_weighted.json").write_text(json.dumps(r, indent=1))
    elif what == "step2info":
        r = step2_info(mk, events, C, pool)
        (OUT / "step2_info_discovery.json").write_text(json.dumps(r, indent=1))
