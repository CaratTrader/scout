"""weather_walkforward: clean walk-forward test of observation rules on Kalshi daily-high temperature ladders
(KXHIGH*, 24 cities, 2025-07-01..2026-10-07; data built by weather_walkforward_data.py).

Two layers.
 1. COMBOS: every rule-parameter combination is backtested on every month. Hand families:
      DEAD   NO on a bucket wholly below the observed max (R0-like; YES on the open top bucket once the max is in it)
      FADE   NO on buckets >= G degrees above the max after the peak (R2-like)
      HOLD   YES on the bucket holding the rounded max after the peak (R1-like)
      CHASE  YES on the bucket 1-2 degrees above the max while the temperature is still at its max (early afternoon)
    and a MODEL family: an empirical distribution of (official high - rounded METAR max so far) conditioned on hour,
    fall from the max, minutes since the max, 5-minute excess and season, fitted month by month on PAST station-days
    only; trade when the model's expected return at the taker price exceeds a threshold.
    One trade per market per combo (first trigger in time). Fill = quote one minute after the decision, taker,
    fee 0.07 p (1-p) per contract rounded up to the cent per $5 order.
 2. SELECTOR (walk-forward): for each test month, choose the combo(s) with the best record over a training window of
    PAST months only (expanding / rolling 3 / rolling 6 / same season), trade them in the test month, concatenate.
    Meta-parameters of the selector are chosen on the DISCOVERY part (first 70% of station-days by close time);
    at most 3 frozen selectors are run once on VALIDATION (last 30%).
Usage: .venv/bin/python -m lab.kalshi.strategies.weather_walkforward discover|validate|combos"""
from __future__ import annotations
import gzip, json, math, os, pickle, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/weather_walkforward"
STAKE = 5.0
SPLIT = 0.70
MIN_TEST_MONTH = "2025-10"      # walk-forward test months start once 3 months of history exist


def rnd(x: float) -> int:
    return math.floor(x + 0.5)


def fee_c(p: float, stake: float = STAKE) -> float:
    """Taker fee per contract for one order of ~stake dollars, rounded up to the cent per order."""
    n = max(1, int(stake / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def season(day: str) -> str:
    return "warm" if 4 <= int(day[5:7]) <= 9 else "cold"


def load():
    with gzip.open(OUT / f"snap{os.environ.get('WW_TAG', '')}.pkl.gz", "rb") as fh:
        D = pickle.load(fh)
    D.sort(key=lambda r: (r["close"], r["stn"]))
    cut = int(SPLIT * len(D))
    for i, r in enumerate(D):
        r["part"] = "disc" if i < cut else "val"
        r["mon"] = r["day"][:7]
        r["key"] = (r["stn"], r["day"])
    return D


# ---------------------------------------------------------------- state features
def feats(r):
    """Per decision index: dict of observation features (None if no observation yet)."""
    out = []
    for t, s in zip(r["times"], r["S"]):
        if s is None:
            out.append(None); continue
        Mo, tM, Mraw, M5mid, M5hi, M5lo2, T, T5, last_m = s
        rMo = rnd(Mo); rMm = rnd(max(Mo, Mraw, M5mid))
        Tnow = T if T5 is None else max(T, T5) if last_m < t - 30 else T
        out.append({"t": t, "Mo": Mo, "rMo": rMo, "rMm": rMm, "Mraw": Mraw, "M5lo2": M5lo2, "M5mid": M5mid,
                    "fall": Mo - T, "since": t - tM, "T": T, "Tnow": Tnow, "ex5": max(0, min(2, rMm - rMo)),
                    "grid": t % 15 == 0})
    return out


# ---------------------------------------------------------------- hand rule families
def hand_combos():
    C = []
    for src in ("obs", "obs5", "raw"):
        for marg in (1.0, 0.5):
            for cap in (0.90, 0.95, 0.97):
                C.append({"fam": "DEAD", "src": src, "marg": marg, "cap": cap})
    for h0 in (15, 17):
        for F in (1, 2):
            for G in (1, 2, 3):
                for band in ((0.30, 0.85), (0.60, 0.95), (0.30, 0.95)):
                    C.append({"fam": "FADE", "h0": h0, "F": F, "G": G, "band": band})
    for h0 in (15, 17, 19):
        for F in (1, 2):
            for margin in (0, 1):
                for band in ((0.05, 0.60), (0.40, 0.90), (0.05, 0.90)):
                    C.append({"fam": "HOLD", "h0": h0, "F": F, "margin": margin, "band": band})
    for h1 in (14, 15, 16):
        for G in (1, 2):
            for band in ((0.05, 0.35), (0.05, 0.60)):
                C.append({"fam": "CHASE", "h1": h1, "G": G, "band": band})
    # flipped sides of the two families whose YES side loses heavily in discovery (NO pays 1 - bid, not 1 - ask)
    for h0 in (15, 17, 19):
        for F in (1, 2):
            for band in ((0.40, 0.95), (0.60, 0.90)):
                C.append({"fam": "HOLDNO", "h0": h0, "F": F, "band": band})
    for h1 in (14, 15, 16):
        for G in (1, 2):
            for band in ((0.65, 0.95), (0.40, 0.95)):
                C.append({"fam": "CHASENO", "h1": h1, "G": G, "band": band})
    for i, c in enumerate(C):
        c["id"] = f"{c['fam']}:" + ",".join(f"{k}={v}" for k, v in c.items() if k != "fam")
    return C


def hand_signal(c, f, b, ask, bid):
    """-> (side, price) or None. ask/bid in dollars."""
    fam = c["fam"]; lo, hi = b["lo"], b["hi"]; t = f["t"]; no_ask = 1 - bid
    if fam == "DEAD":
        Mx = f["Mo"] if c["src"] == "obs" else max(f["Mo"], f["M5lo2"]) if c["src"] == "obs5" else f["Mraw"]
        if hi < 1e8 and hi + c["marg"] <= Mx and bid > 0 and 0.02 <= no_ask <= c["cap"]:
            return "NO", no_ask
        if hi >= 1e8 and lo > -1e8 and Mx >= lo + (c["marg"] - 0.95) and 0.02 <= ask <= c["cap"]:
            return "YES", ask
        return None
    if fam == "FADE":
        if t >= c["h0"] * 60 and f["fall"] >= c["F"] and f["since"] >= 45 and lo > -1e8 and lo >= f["rMm"] + c["G"] and bid > 0 and c["band"][0] <= no_ask <= c["band"][1]:
            return "NO", no_ask
        return None
    if fam == "HOLD":
        r = f["rMm"]
        if t >= c["h0"] * 60 and f["fall"] >= c["F"] and f["since"] >= 45 and lo <= r <= hi and (c["margin"] == 0 or r + 1 <= hi) and c["band"][0] <= ask <= c["band"][1]:
            return "YES", ask
        return None
    if fam == "CHASE":
        r = f["rMm"]
        if 12 * 60 <= t < c["h1"] * 60 and f["T"] >= f["Mo"] - 0.5 and lo <= r + c["G"] <= hi and lo > r and c["band"][0] <= ask <= c["band"][1]:
            return "YES", ask
        return None
    if fam == "HOLDNO":
        r = f["rMm"]; na = 1 - bid
        if t >= c["h0"] * 60 and f["fall"] >= c["F"] and f["since"] >= 45 and lo <= r <= hi and bid > 0 and c["band"][0] <= na <= c["band"][1]:
            return "NO", na
        return None
    if fam == "CHASENO":
        r = f["rMm"]; na = 1 - bid
        if 12 * 60 <= t < c["h1"] * 60 and f["T"] >= f["Mo"] - 0.5 and lo <= r + c["G"] <= hi and lo > r and bid > 0 and c["band"][0] <= na <= c["band"][1]:
            return "NO", na
        return None
    raise ValueError(fam)


def mk_trade(r, b, f, side, px, q, cid):
    won = b["won"] if side == "YES" else not b["won"]
    pnl = (1.0 if won else 0.0) - px - fee_c(px)
    return {"cid": cid, "stn": r["stn"], "day": r["day"], "mon": r["mon"], "part": r["part"], "tk": b["tk"], "t": f["t"],
            "side": side, "px": round(px, 2), "won": won, "ret": pnl / px, "vol": q[3], "age": q[2], "spread": (q[0] - q[1]) / 100}


def run_hand(D, combos):
    trades = defaultdict(list)
    by_fam = defaultdict(list)
    for c in combos:
        by_fam[c["fam"]].append(c)
    for r in D:
        F = feats(r)
        for b in r["mk"]:
            done = set()
            for i, f in enumerate(F):
                if f is None:
                    continue
                q = b["q"][i]
                if q is None:
                    continue
                ask, bid = q[0] / 100, q[1] / 100
                if ask <= 0 or ask >= 1:
                    ask = 9.0   # no offer: no YES fill
                for fam, cs in by_fam.items():
                    # cheap family prefilters
                    if fam == "DEAD" and not (b["hi"] < f["Mraw"] + 1 or b["hi"] >= 1e8):
                        continue
                    if fam == "FADE" and (f["t"] < 15 * 60 or f["fall"] < 1 or b["lo"] < f["rMm"] + 1):
                        continue
                    if fam == "HOLD" and (f["t"] < 15 * 60 or not (b["lo"] <= f["rMm"] <= b["hi"])):
                        continue
                    if fam in ("CHASE", "CHASENO") and (f["t"] < 12 * 60 or f["t"] >= 16 * 60 or b["lo"] <= f["rMm"]):
                        continue
                    if fam == "HOLDNO" and (f["t"] < 15 * 60 or not (b["lo"] <= f["rMm"] <= b["hi"])):
                        continue
                    for c in cs:
                        if c["id"] in done:
                            continue
                        s = hand_signal(c, f, b, ask, bid)
                        if s:
                            done.add(c["id"]); trades[c["id"]].append(mk_trade(r, b, f, s[0], s[1], q, c["id"]))
    return trades


# ---------------------------------------------------------------- model family
DMIN, DMAX = -3, 15


def hbin(t):
    h = t // 60
    return 11 if h <= 11 else 19 if h in (19, 20) else 21 if h >= 21 else h


def fbin(x):
    return 0 if x < 0.5 else 1 if x < 1.5 else 2 if x < 2.5 else 3 if x < 4.5 else 4


def sbin(x):
    return 0 if x < 30 else 1 if x < 90 else 2 if x < 180 else 3


def cell_keys(f, day):
    hb, fb, sb, eb = hbin(f["t"]), fbin(f["fall"]), sbin(f["since"]), f["ex5"]
    return [(season(day), hb, fb, sb, eb), (hb, fb, sb, eb), (hb, fb, eb), (hb,), ()]


class DeltaModel:
    def __init__(self, k: float = 20.0):
        self.k = k; self.cnt = [defaultdict(lambda: [0.0] * (DMAX - DMIN + 1)) for _ in range(5)]; self.n = [defaultdict(float) for _ in range(5)]

    def add(self, r, F):
        if r["final"] is None:
            return
        for f in F:
            if f is None or not f["grid"]:
                continue
            d = max(DMIN, min(DMAX, int(round(r["final"])) - f["rMo"]))
            for lv, key in enumerate(cell_keys(f, r["day"])):
                self.cnt[lv][key][d - DMIN] += 1; self.n[lv][key] += 1

    def dist(self, f, day):
        keys = cell_keys(f, day)
        p = None
        for lv in range(4, -1, -1):
            key = keys[lv]; n = self.n[lv].get(key, 0.0); c = self.cnt[lv].get(key)
            if p is None:
                p = [x / n for x in c] if n else [1.0 / (DMAX - DMIN + 1)] * (DMAX - DMIN + 1)
            elif n:
                p = [(c[j] + self.k * p[j]) / (n + self.k) for j in range(len(p))]
        return p


def model_combos():
    C = []
    for th in (0.05, 0.10, 0.20, 0.40):
        for side in ("YES", "NO", "BOTH"):
            for pmin in (0.05, 0.15):
                C.append({"fam": "MODEL", "th": th, "side": side, "pmin": pmin, "h": 0})
    for th in (0.10, 0.20, 0.40):          # added after the all-day model lost: observation-only edges can exist only late in the day
        for side in ("YES", "NO", "BOTH"):
            for h in (14, 17):
                C.append({"fam": "MODEL", "th": th, "side": side, "pmin": 0.05, "h": h})
    for c in C:
        c["id"] = "MODEL:" + ",".join(f"{k}={v}" for k, v in c.items() if k != "fam")
    return C


def run_model(D, combos, k: float = 20.0):
    """Month by month: fit on all station-days whose climate day is before the month (expanding), predict the month."""
    trades = defaultdict(list); calib = []
    months = sorted({r["mon"] for r in D})
    Fcache = {id(r): feats(r) for r in D}
    model = DeltaModel(k)
    for m in months:
        rows = [r for r in D if r["mon"] == m]
        if sum(model.n[4].values()) > 0:
            for r in rows:
                F = Fcache[id(r)]
                P = [None if f is None else model.dist(f, r["day"]) for f in F]
                for b in r["mk"]:
                    done = set()
                    for i, f in enumerate(F):
                        if f is None or b["q"][i] is None:
                            continue
                        q = b["q"][i]; ask, bid = q[0] / 100, q[1] / 100
                        p = P[i]
                        lo_d = -10**6 if b["lo"] < -1e8 else int(b["lo"]) - f["rMo"]
                        hi_d = 10**6 if b["hi"] > 1e8 else int(b["hi"]) - f["rMo"]
                        py = sum(p[j] for j in range(len(p)) if lo_d <= j + DMIN <= hi_d)
                        if f["grid"] and f["t"] % 60 == 0:
                            calib.append((r["part"], r["mon"], py, b["won"], (ask + bid) / 2 if 0 < ask < 1 else None))
                        for c in combos:
                            if c["id"] in done or f["t"] < c["h"] * 60:
                                continue
                            if c["side"] in ("YES", "BOTH") and 0 < ask < 1 and c["pmin"] <= ask <= 0.97:
                                e = (py - ask - fee_c(ask)) / ask
                                if e >= c["th"]:
                                    done.add(c["id"]); tr = mk_trade(r, b, f, "YES", ask, q, c["id"]); tr["edge"] = e; tr["p"] = py
                                    trades[c["id"]].append(tr); continue
                            na = 1 - bid
                            if c["side"] in ("NO", "BOTH") and bid > 0 and c["pmin"] <= na <= 0.97:
                                e = ((1 - py) - na - fee_c(na)) / na
                                if e >= c["th"]:
                                    done.add(c["id"]); tr = mk_trade(r, b, f, "NO", na, q, c["id"]); tr["edge"] = e; tr["p"] = py
                                    trades[c["id"]].append(tr)
        for r in rows:
            model.add(r, Fcache[id(r)])
        print("model month", m, "trained days", int(sum(model.n[4].values())), flush=True)
    return trades, calib


# ---------------------------------------------------------------- observation-conditioned price cells
POS_LBL = ("dead", "hold", "+1", "+2", "+3-4", "+5")
PX_EDGES = (0.02, 0.10, 0.25, 0.50, 0.75, 0.90, 0.98)


def cell_of(f, b):
    r = f["rMm"]
    if b["hi"] < r:
        pos = "dead"
    elif b["lo"] <= r <= b["hi"]:
        pos = "hold"
    else:
        g = b["lo"] - r
        pos = "+1" if g <= 1 else "+2" if g <= 2 else "+3-4" if g <= 4 else "+5"
    h = f["t"] // 60
    hb = "12-14" if h <= 14 else "15-16" if h <= 16 else "17-18" if h <= 18 else "19-21" if h <= 21 else "22+"
    return pos, hb


def pxbin(p):
    for a, b_ in zip(PX_EDGES, PX_EDGES[1:]):
        if a <= p < b_:
            return f"{a:.2f}-{b_:.2f}"
    return None


def run_cells(D):
    trades = defaultdict(list)
    for r in D:
        F = feats(r)
        for b in r["mk"]:
            done = set()
            for i, f in enumerate(F):
                if f is None or b["q"][i] is None or f["t"] < 12 * 60:
                    continue
                q = b["q"][i]; ask, bid = q[0] / 100, q[1] / 100
                pos, hb = cell_of(f, b)
                for side, px in (("YES", ask if 0 < ask < 1 else None), ("NO", 1 - bid if bid > 0 else None)):
                    if px is None:
                        continue
                    pb = pxbin(px)
                    if pb is None:
                        continue
                    cid = f"CELL:{pos}|{hb}|{side}|{pb}"
                    if cid not in done:
                        done.add(cid); trades[cid].append(mk_trade(r, b, f, side, px, q, cid))
    return trades


# ---------------------------------------------------------------- statistics
def summ(rows):
    if not rows:
        return {"n": 0}
    ret = [r["ret"] for r in rows]; ev = defaultdict(list)
    for r in rows:
        ev[(r["stn"], r["day"])].append(r["ret"])
    # clustered SE of the trade mean: sum of per-event residual sums
    n = len(ret); mu = st.mean(ret)
    var = sum(sum(x - mu for x in v) ** 2 for v in ev.values()) / n ** 2
    G = len(ev)
    if G > 1:
        var *= G / (G - 1)
    t = mu / math.sqrt(var) if var > 0 else float("nan")
    rs = sorted(ret, reverse=True)
    days = sorted(r["day"] for r in rows); mid = days[len(days) // 2]
    h1 = [r["ret"] for r in rows if r["day"] < mid]; h2 = [r["ret"] for r in rows if r["day"] >= mid]
    return {"n": n, "events": G, "win": sum(r["won"] for r in rows) / n, "avg_px": st.mean(r["px"] for r in rows), "ret": mu, "t": t,
            "se": math.sqrt(var) if var > 0 else float("nan"),
            "wo3": st.mean(rs[3:]) if n > 3 else float("nan"), "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan")}


def fmt(s):
    if not s.get("n"):
        return "n=0"
    return (f"n={s['n']:4d} ev={s['events']:4d} win={s['win']:.0%} px={s['avg_px']:.2f} ret/$={s['ret']:+.3f} t={s['t']:+.2f} "
            f"wo3={s['wo3']:+.3f} h1={s['half1']:+.3f} h2={s['half2']:+.3f}")


# ---------------------------------------------------------------- selector
MONTHS_ALL: list[str] = []


def window_months(test_m, kind):
    prior = [m for m in MONTHS_ALL if m < test_m]
    if kind == "expanding":
        return prior
    if kind == "roll3":
        return prior[-3:]
    if kind == "roll6":
        return prior[-6:]
    if kind == "season":
        return [m for m in prior if season(m + "-01") == season(test_m + "-01")]
    raise ValueError(kind)


def score(s, crit):
    if crit == "mean":
        return s["ret"]
    if crit == "t":
        return s["t"]
    if crit == "lb":
        return s["ret"] - s["se"]
    raise ValueError(crit)


def select_and_trade(trades_by_combo, pool, kind, crit, min_n=30, abstain=0.0, topk=1, test_months=None, log=None):
    """Concatenated walk-forward trades: for each test month choose top-k combos on past months, trade them."""
    by_cm = defaultdict(lambda: defaultdict(list))
    for cid, rows in trades_by_combo.items():
        if pool(cid):
            for r in rows:
                by_cm[cid][r["mon"]].append(r)
    out = []
    for m in test_months:
        W = set(window_months(m, kind))
        cand = []
        for cid, mm in by_cm.items():
            tr = [r for mo in W for r in mm.get(mo, [])]
            if len(tr) < min_n:
                continue
            s = summ(tr)
            if s["ret"] < abstain or not (s["t"] == s["t"]):
                continue
            cand.append((score(s, crit), cid, s))
        cand.sort(key=lambda x: -x[0])
        chosen = [c[1] for c in cand[:topk]]
        if log is not None:
            log.append({"month": m, "chosen": chosen, "train": [(c[1], round(c[2]["ret"], 3), c[2]["n"], round(c[2]["t"], 2)) for c in cand[:topk]]})
        seen = set()
        for cid in chosen:
            for r in sorted(by_cm[cid].get(m, []), key=lambda x: (x["day"], x["t"])):
                if r["tk"] not in seen:
                    seen.add(r["tk"]); out.append({**r, "sel": cid})
    return out


POOLS = {"hand": lambda c: not c.startswith(("MODEL", "CELL")), "model": lambda c: c.startswith("MODEL"), "all": lambda c: True,
         "cell": lambda c: c.startswith("CELL"), "holdno": lambda c: c.startswith("HOLDNO"), "chaseno": lambda c: c.startswith("CHASENO"),
         "dead": lambda c: c.startswith("DEAD"), "fade": lambda c: c.startswith("FADE"), "hold": lambda c: c.startswith("HOLD:"),
         "chase": lambda c: c.startswith("CHASE:")}


def build_all(force=False):
    cache = OUT / f"combo_trades{os.environ.get('WW_TAG', '')}.pkl.gz"
    D = load()
    global MONTHS_ALL
    MONTHS_ALL = sorted({r["mon"] for r in D})
    if cache.exists() and not force:
        with gzip.open(cache, "rb") as fh:
            T, calib = pickle.load(fh)
        return D, T, calib
    T = run_hand(D, hand_combos())
    TM, calib = run_model(D, model_combos())
    T.update(TM)
    T.update(run_cells(D))
    with gzip.open(cache, "wb") as fh:
        pickle.dump((dict(T), calib), fh)
    return D, dict(T), calib


def cmd_combos():
    D, T, calib = build_all("--force" in sys.argv)
    print(f"station-days {len(D)}; discovery {sum(r['part']=='disc' for r in D)} (to {max(r['day'] for r in D if r['part']=='disc')}), validation {sum(r['part']=='val' for r in D)}")
    print(f"combos {len(T)} (hand {sum(not c.startswith('MODEL') for c in T)}, model {sum(c.startswith('MODEL') for c in T)})")
    # in-sample (full discovery, NOT walk-forward) overview per combo, discovery part only
    rows = []
    for cid, tr in T.items():
        d = [r for r in tr if r["part"] == "disc"]
        if len(d) >= 20:
            rows.append((summ(d)["ret"], cid, summ(d)))
    rows.sort(key=lambda x: -x[0])
    print("\nDISCOVERY, full-period combo results (in-sample view; top 25 / bottom 5):")
    for _, cid, s in rows[:25] + rows[-5:]:
        print(f"  {cid:60s} {fmt(s)}")
    # model calibration on discovery
    print("\nMODEL calibration (discovery, hourly snapshots): model p bin -> realised win rate, market mid")
    bins = defaultdict(list)
    for part, mon, py, won, mid in calib:
        if part == "disc":
            bins[min(9, int(py * 10))].append((won, mid))
    for k in sorted(bins):
        v = bins[k]; mids = [x[1] for x in v if x[1] is not None]
        print(f"  p {k/10:.1f}-{(k+1)/10:.1f}: n={len(v):6d} win={sum(x[0] for x in v)/len(v):.3f} mid={st.mean(mids) if mids else float('nan'):.3f}")


META_GRID = [(pool, kind, crit, abst, topk) for pool in ("hand", "model", "cell", "all") for kind in ("expanding", "roll3", "roll6", "season")
             for crit in ("mean", "t", "lb") for abst in (0.0, 0.10) for topk in (1, 3)]


def cmd_discover():
    D, T, _ = build_all()
    tm = [m for m in MONTHS_ALL if m >= MIN_TEST_MONTH]
    res = []
    for pool, kind, crit, abst, topk in META_GRID:
        tr = select_and_trade(T, POOLS[pool], kind, crit, abstain=abst, topk=topk, test_months=tm)
        d = [r for r in tr if r["part"] == "disc"]
        s = summ(d)
        res.append(((pool, kind, crit, abst, topk), s))
    res.sort(key=lambda x: -(x[1].get("ret") if x[1].get("n", 0) >= 40 else -9))
    print(f"DISCOVERY walk-forward (test months {tm[0]}..cut; concatenated out-of-sample trades in the discovery part)")
    for meta, s in res:
        print(f"  {str(meta):48s} {fmt(s)}")
    # per-family walk-forward to see which families carry anything
    print("\nper-family pools (expanding, mean, abstain 0, top1):")
    for pool in ("dead", "fade", "hold", "chase", "holdno", "chaseno", "model", "cell"):
        tr = select_and_trade(T, POOLS[pool], "expanding", "mean", abstain=0.0, topk=1, test_months=tm)
        print(f"  {pool:6s} {fmt(summ([r for r in tr if r['part'] == 'disc']))}")
    (OUT / "discovery_meta.json").write_text(json.dumps([{"meta": list(m), **s} for m, s in res], indent=0))
    print("variants: combos", len(T), "meta", len(META_GRID))


if __name__ == "__main__" and (len(sys.argv) < 2 or sys.argv[1] in ("combos", "discover")):
    cmd = sys.argv[1] if len(sys.argv) > 1 else "combos"
    {"combos": cmd_combos, "discover": cmd_discover}[cmd]()


# ---------------------------------------------------------------- frozen candidates (fixed 2026-10-08 on discovery only, before any validation run)
FROZEN = [
    {"name": "C1 walk-forward selector: cells, rolling 3 months, best clustered t, abstain if train mean < +10%, top 1",
     "kind": "selector", "meta": ("cell", "roll3", "t", 0.10, 1)},
    {"name": "C2 fixed: YES on the bucket holding the rounded max after 19:00 local (fall >= 1F, 45 min since max, room above), ask 0.05-0.90",
     "kind": "fixed", "cid": "HOLD:h0=19,F=1,margin=1,band=(0.05, 0.9)"},
    {"name": "C3 fixed: R0 alone - NO on a bucket >= 1F below the METAR max (YES on the open top bucket), price <= 0.97",
     "kind": "fixed", "cid": "DEAD:src=obs,marg=1.0,cap=0.97"},
]


def cand_trades(c, T):
    if c["kind"] == "fixed":
        return [dict(r, sel=c["cid"]) for r in T[c["cid"]]]
    pool, kind, crit, abst, topk = c["meta"]
    tm = [m for m in MONTHS_ALL if m >= MIN_TEST_MONTH]
    log = []
    tr = select_and_trade(T, POOLS[pool], kind, crit, abstain=abst, topk=topk, test_months=tm, log=log)
    c["log"] = log
    return tr


def gate(s, K):
    z = NormalDist().inv_cdf(1 - 0.05 / max(K, 1))
    return {"K": K, "t_bar": round(z, 2), "n>=40": s.get("n", 0) >= 40, "ret>=10%": s.get("ret", -9) >= 0.10, "t>=bar": s.get("t", -9) >= z,
            "wo3>0": s.get("wo3", -9) > 0, "halves>0": min(s.get("half1", -9), s.get("half2", -9)) > 0}


def cmd_validate():
    lock = OUT / "validation.lock"
    if lock.exists() and "--again" not in sys.argv:
        print("validation already run once:", lock.read_text()); return
    D, T, _ = build_all()
    val_days = sorted({r["day"] for r in D if r["part"] == "val"}); ndays = len(val_days)
    kf = ROOT / "data/kalshi_lab/K.json"; K0 = len(json.loads(kf.read_text()).get("cells", [])) if kf.exists() else 1
    nvar = len(T) + len(META_GRID) + 15   # combos + selector meta-variants + latency sensitivity rows
    K = K0 + nvar
    out = []
    for c in FROZEN:
        tr = cand_trades(c, T)
        d = [r for r in tr if r["part"] == "disc" and r["mon"] >= MIN_TEST_MONTH] if c["kind"] == "selector" else [r for r in tr if r["part"] == "disc"]
        v = [r for r in tr if r["part"] == "val"]
        sd, sv = summ(d), summ(v)
        vols = sorted(r["vol"] for r in v) if v else []
        caps = sorted(r["vol"] for r in v)
        row = {"rule": c["name"], "discovery": sd, "validation": sv, "trades_per_day": len(v) / max(ndays, 1),
               "median_volume_pm30min_contracts": vols[len(vols) // 2] if vols else None, "gate": gate(sv, K),
               "val_trades": [{k: r[k] for k in ("stn", "day", "tk", "t", "side", "px", "won", "ret", "vol", "sel")} for r in v]}
        if c.get("log"):
            row["selection_log"] = c["log"]
        out.append(row)
        print("\n" + c["name"]); print("  discovery :", fmt(sd)); print("  VALIDATION:", fmt(sv), f"trades/day={len(v)/max(ndays,1):.2f}")
        print("  gate:", row["gate"])
        if c.get("log"):
            for L in c["log"]:
                if L["month"] >= "2026-08":
                    print("   ", L["month"], L["train"][:1], "->", fmt(summ([r for r in v if r["mon"] == L["month"]])))
    (OUT / "validation.json").write_text(json.dumps({"K": K, "K_lab": K0, "variants_here": nvar, "validation_days": ndays,
                                                      "first_val_day": val_days[0], "last_val_day": val_days[-1], "candidates": out}, indent=1, default=str))
    import datetime as _dt
    lock.write_text(_dt.datetime.now().isoformat())


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "validate":
    cmd_validate()
