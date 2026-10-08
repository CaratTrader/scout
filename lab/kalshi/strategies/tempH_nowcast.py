"""tempH_nowcast: can a nowcast of the Kalshi weather index beat the hourly temperature markets?

Markets: KXTEMPMIAH / KXTEMPNYCHS / KXTEMPCHIHS / KXTEMPLAXHS, "temperature at H:00 above X" (10 strikes 1F apart, open
H-1:00, close H:00, fee_type quadratic). Settlement = the Kalshi weather index value at the close minute (verified: the
index history equals expiration_value on 1476/1476 events that the history covers). The index is the equal-weight mean
of HF-ASOS 1-minute station readings (whole C converted to F) and is PUBLIC with no key: GET /live_data/weather/{city};
member readings appear (status "pending", detailed=true) ~140-210 s after the minute and the published value ~5-6 min
after the minute. A home Mac polling once a minute therefore knows the index up to t - 4 min (detailed) or t - 6 min.

Protocol (docs/KALSHI_LAB.md): events ordered by close; discovery = first 70%, validation = last 30%. All models are
fitted on discovery only. Decision at t with index <= t - L; fill as taker one minute later at the candle quote (YES at
yes_ask, NO at 1 - yes_bid), quote at most 10 min old; fee 0.07 p (1-p). Stats: equal-$ return, t clustered by event.

Sections
  1. information: does the market already know what the index nowcast knows? (MAE of the market-implied median vs
     persistence / diurnal nowcasts; incremental regression of the market's residual on the latest index)
  2. discovery grid of trading rules (A nowcast-vs-quote 15-60 min; B market-median + index tilt, last 2-10 min;
     C stale-quote rule, last 2-10 min; D inner/outer strikes around the market median)
  3. the 3 best discovery variants (n >= 40) frozen and evaluated once on validation; capacity; result.json
Usage: python -m lab.kalshi.strategies.tempH_nowcast   (data: python -m lab.kalshi.strategies.tempH_nowcast_data)"""
from __future__ import annotations
import bisect, collections, datetime as dt, json, math, statistics as st, sys
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import fee, cell_stats
from lab.kalshi.strategies.tempH_nowcast_data import CITY, OUT, load_index

TZ = {"miami": "America/New_York", "nyc": "America/New_York", "chicago": "America/Chicago", "la-coastal": "America/Los_Angeles"}
MAX_AGE = 10          # minutes a candle quote may be carried forward (stricter than calib's 30)
SPLIT = 0.7
L_DET, L_PUB = 4, 6   # index lag (minutes) with detailed polling / published values only

IDX = {c: load_index(c) for c in CITY.values()}
EV: dict[str, list[dict]] = collections.defaultdict(list)
CD: dict[str, list] = {}
for s in CITY:
    for line in open(f"data/kalshi_lab/markets/{s}.jsonl"):
        m = json.loads(line); EV[m["e"]].append(m)
    for line in open(f"data/kalshi_lab/candles/{s}.jsonl"):
        x = json.loads(line); CD[x["t"]] = x["c"]
for e in EV:
    EV[e].sort(key=lambda m: m["floor"])
EVS = sorted((ms[0]["close"], e) for e, ms in EV.items())
CUT = EVS[int(len(EVS) * SPLIT)][0]


def ia(c: str, t: int, back: int = 5) -> float | None:
    """Latest index value at a minute <= t (at most `back` minutes older)."""
    t -= t % 60
    for k in range(back + 1):
        v = IDX[c].get(t - 60 * k)
        if v is not None:
            return v
    return None


def quote(c: list, t: int, max_age: int = MAX_AGE) -> tuple[float, float] | None:
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > max_age * 60 or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


def implied_median(ms: list[dict], t: int) -> float | None:
    """Strike where the mid survival curve P(Y > K) crosses 0.5 (linear interpolation), quotes known at t
    (carried forward up to 30 min: far out-of-the-money strikes rarely requote)."""
    pts = []
    for m in ms:
        q = quote(CD[m["t"]], t, 30)
        if not q:
            return None
        pts.append((m["floor"], (q[0] + q[1]) / 2))
    for (k1, p1), (k2, p2) in zip(pts, pts[1:]):
        if p1 >= 0.5 >= p2 and p1 != p2:
            return k1 + (p1 - 0.5) / (p1 - p2) * (k2 - k1)
    return None


def local_bin(c: str, t: int) -> int:
    d = dt.datetime.fromtimestamp(t, ZoneInfo(TZ[c])); return (d.hour * 60 + d.minute) // 15


def tstat(v: list[float]) -> float:
    return st.mean(v) / (st.pstdev(v) / math.sqrt(len(v))) if len(v) > 2 and st.pstdev(v) > 0 else float("nan")


def ols(xs: list[float], ys: list[float]) -> tuple[float, float]:
    mx, my = st.mean(xs), st.mean(ys); sxx = sum((a - mx) ** 2 for a in xs)
    b = sum((a - mx) * (y - my) for a, y in zip(xs, ys)) / sxx
    res = [y - my - b * (a - mx) for a, y in zip(xs, ys)]
    return b, b / math.sqrt(sum(r * r for r in res) / (len(xs) - 2) / sxx)


def trade(e: str, c: str, T: int, side: str, px: float, won_yes: bool) -> dict:
    w = won_yes if side == "YES" else not won_yes
    return {"e": e, "c": c, "t_close": T, "side": side, "px": px, "won": w, "ret": ((1.0 if w else 0.0) - px - fee(px)) / px}


# ---------------------------------------------------------------- 1. information
def information() -> dict:
    out = {"mae": {}, "beta": {}}
    print("1. INFORMATION. Mean |Y - forecast| (F) at decision time; market = mid-implied median, persistence = index at t-4m")
    for tau in (5, 15, 30, 45, 57):
        h = tau + L_DET
        acc = collections.defaultdict(list)                     # diurnal mean change by city x local 15-min bin (discovery only)
        for c in CITY.values():
            for T, v in IDX[c].items():
                if T >= CUT or T % 300:
                    continue
                x = IDX[c].get(T - h * 60)
                if x is not None:
                    acc[(c, local_bin(c, T - h * 60))].append(v - x)
        D = {k: st.mean(v) for k, v in acc.items()}
        dmu = lambda c, b: st.mean([D[(c, (b + j) % 96)] for j in (-1, 0, 1) if (c, (b + j) % 96) in D] or [0.0])
        for part in ("disc", "val"):
            E = collections.defaultdict(list)
            for T, e in EVS:
                if (T < CUT) != (part == "disc"):
                    continue
                ms = EV[e]; c = CITY[ms[0]["series"]]; t = T - tau * 60
                y = IDX[c].get(T); x = ia(c, t - L_DET * 60); med = implied_median(ms, t)
                if None in (y, x, med):
                    continue
                E["persistence"].append(abs(y - x)); E["diurnal"].append(abs(y - x - dmu(c, local_bin(c, t - L_DET * 60)))); E["market"].append(abs(y - med))
            row = {k: round(st.mean(v), 3) for k, v in E.items()}; row["n"] = len(E["market"])
            out["mae"][f"{tau}m|{part}"] = row
            print(f"   tau={tau:2d}m {part}: {row}")
    print("   Does the latest index add to the market? OLS of (Y - market median) on (index - market median), L=4m")
    for tau in (2, 3, 5, 10, 15, 30, 45):
        for part in ("disc", "val"):
            zs, rs = [], []
            for T, e in EVS:
                if (T < CUT) != (part == "disc"):
                    continue
                ms = EV[e]; c = CITY[ms[0]["series"]]; t = T - tau * 60
                y = IDX[c].get(T); x = ia(c, t - L_DET * 60); med = implied_median(ms, t)
                if None in (y, x, med):
                    continue
                zs.append(x - med); rs.append(y - med)
            b, tb = ols(zs, rs)
            out["beta"][f"{tau}m|{part}"] = {"n": len(zs), "beta": round(b, 3), "t": round(tb, 1)}
            print(f"   tau={tau:2d}m {part}: n={len(zs)} beta={b:+.3f} t={tb:+.1f}")
    return out


# ---------------------------------------------------------------- 2. trading rules
def fit_slope_model(c: str, h: int) -> tuple[float, float, list[float]]:
    """D = I(T) - I(T-h) ~ a + b * (I(T-h) - I(T-h-30m)); empirical residuals. Discovery anchors only."""
    xs, ys = [], []
    for T in sorted(IDX[c])[::2]:
        if T >= CUT:
            continue
        x = IDX[c].get(T - h * 60); x30 = IDX[c].get(T - h * 60 - 1800)
        if x is None or x30 is None:
            continue
        xs.append(x - x30); ys.append(IDX[c][T] - x)
    mx, my = st.mean(xs), st.mean(ys)
    b = sum((a - mx) * (y - my) for a, y in zip(xs, ys)) / sum((a - mx) ** 2 for a in xs); a0 = my - b * mx
    return a0, b, sorted(y - (a0 + b * x) for x, y in zip(xs, ys))


def rules_A() -> dict:
    """Nowcast (persistence + slope, empirical residuals) vs quote, 15-60 min before the hour."""
    V = {}
    for L in (L_DET, L_PUB):
        for tau in (15, 20, 30, 45, 58):
            models = {c: fit_slope_model(c, tau + L) for c in CITY.values()}
            cand = []
            for T, e in EVS:
                for m in EV[e]:
                    c = CITY[m["series"]]; t = T - tau * 60
                    x = ia(c, t - L * 60); x30 = ia(c, t - L * 60 - 1800); q = quote(CD[m["t"]], t + 60)
                    if None in (x, x30) or not q:
                        continue
                    a0, b, res = models[c]; p = 1 - bisect.bisect_right(res, m["floor"] - x - (a0 + b * (x - x30))) / len(res)
                    ask, bid = q; won = m["result"] == "yes"
                    for side, px, pw in (("YES", ask, p), ("NO", 1 - bid, 1 - p)):
                        if 0.02 <= px <= 0.98:
                            r = trade(e, c, T, side, px, won); r["edge"] = pw - px - fee(px); cand.append(r)
            for th in (0.05, 0.10, 0.20, 0.30):
                V[f"A|L{L}|tau{tau}|edge>{th}"] = [r for r in cand if r["edge"] > th]
    return V


def rules_B() -> dict:
    """Market median tilted toward the latest index: Yhat = med + beta (x - med); empirical residuals; last 2-10 min."""
    V = {}
    for tau in (2, 3, 4, 5, 6, 8, 10):
        data = []
        for T, e in EVS:
            ms = EV[e]; c = CITY[ms[0]["series"]]; t = T - tau * 60
            y = IDX[c].get(T); x = ia(c, t - L_DET * 60); med = implied_median(ms, t)
            if None not in (y, x, med):
                data.append((T, e, c, x, med, y))
        disc = [d for d in data if d[0] < CUT]
        zs = [d[3] - d[4] for d in disc]; rs = [d[5] - d[4] for d in disc]
        beta, _ = ols(zs, rs); res = sorted(r - beta * z for z, r in zip(zs, rs))
        cand = []
        for T, e, c, x, med, y in data:
            yh = med + beta * (x - med)
            for m in EV[e]:
                q = quote(CD[m["t"]], T - tau * 60 + 60)
                if not q:
                    continue
                p = 1 - bisect.bisect_right(res, m["floor"] - yh) / len(res); ask, bid = q; won = m["result"] == "yes"
                for side, px, pw in (("YES", ask, p), ("NO", 1 - bid, 1 - p)):
                    if 0.02 <= px <= 0.98:
                        r = trade(e, c, T, side, px, won); r["edge"] = pw - px - fee(px); cand.append(r)
        for th in (0.03, 0.05, 0.10, 0.15):
            V[f"B|L{L_DET}|tau{tau}|edge>{th}"] = [r for r in cand if r["edge"] > th]
    return V


def rules_C() -> dict:
    """Stale quote: the index (t-4m) is >= d past the strike, buy that side if it costs <= cap."""
    V = {}
    for tau in (2, 3, 5, 10):
        rows = []
        for T, e in EVS:
            c = CITY[EV[e][0]["series"]]; t = T - tau * 60; x = ia(c, t - L_DET * 60)
            if x is None:
                continue
            for m in EV[e]:
                q = quote(CD[m["t"]], t + 60)
                if q:
                    rows.append((e, c, T, x - m["floor"], q[0], q[1], m["result"] == "yes"))
        for d in (0.2, 0.4, 0.6, 1.0):
            for cap in (0.80, 0.95):
                out = []
                for e, c, T, dist, ask, bid, won in rows:
                    if dist >= d and 0.02 <= ask <= cap:
                        out.append(trade(e, c, T, "YES", ask, won))
                    if dist <= -d and 0.02 <= 1 - bid <= cap:
                        out.append(trade(e, c, T, "NO", 1 - bid, won))
                V[f"C|L{L_DET}|tau{tau}|d>={d}|cap{cap}"] = out
    return V


def rules_D() -> dict:
    """Mid-implied distributions are too wide (PIT hump): buy the inner side of strikes 0-1F / 1-2F from the market median."""
    V = {}
    for tau in (5, 10, 15, 30, 45, 57):
        legs = collections.defaultdict(list)
        for T, e in EVS:
            ms = EV[e]; c = CITY[ms[0]["series"]]; t = T - tau * 60; med = implied_median(ms, t)
            if med is None:
                continue
            for m in ms:
                q = quote(CD[m["t"]], t + 60)
                if not q:
                    continue
                ask, bid = q; won = m["result"] == "yes"; off = m["floor"] - med
                if -1.0 < off <= 0 and 0.02 <= ask <= 0.98: legs["YESbelow0-1"].append(trade(e, c, T, "YES", ask, won))
                if -2.0 < off <= -1.0 and 0.02 <= ask <= 0.98: legs["YESbelow1-2"].append(trade(e, c, T, "YES", ask, won))
                if 0 < off < 1.0 and 0.02 <= 1 - bid <= 0.98: legs["NOabove0-1"].append(trade(e, c, T, "NO", 1 - bid, won))
                if 1.0 <= off < 2.0 and 0.02 <= 1 - bid <= 0.98: legs["NOabove1-2"].append(trade(e, c, T, "NO", 1 - bid, won))
        for k in ("YESbelow0-1", "YESbelow1-2", "NOabove0-1", "NOabove1-2"):
            V[f"D|tau{tau}|{k}"] = legs[k]
    return V


def full_stats(rows: list[dict]) -> dict:
    s = cell_stats(rows)
    mid = sorted(r["t_close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
    days = (max(r["t_close"] for r in rows) - min(r["t_close"] for r in rows)) / 86400 or 1
    s.update(half1=st.mean(h1) if h1 else float("nan"), half2=st.mean(h2) if h2 else float("nan"), trades_per_day=len(rows) / days)
    return s


def capacity() -> dict:
    """Top-of-book size at the ask for strikes quoted 0.05-0.95 in one live snapshot (2026-10-08 ~15:30 ET)."""
    snap = json.loads((OUT / "open_markets_sample.json").read_text()); out = {}
    for s, ms in snap.items():
        sz = [float(m["yes_ask_size_fp"]) for m in ms if 0.05 <= float(m["yes_ask_dollars"]) <= 0.95] + \
             [float(m["yes_bid_size_fp"]) for m in ms if 0.05 <= 1 - float(m["yes_bid_dollars"]) <= 0.95]
        out[s] = st.median(sz) if sz else None
    return out


def main() -> None:
    print(f"events {len(EVS)}; discovery < {dt.datetime.utcfromtimestamp(CUT)} UTC ({sum(1 for T, _ in EVS if T < CUT)} events)")
    info = information()
    print("\n2. DISCOVERY GRID (equal-$ return after fees, taker one minute after the decision)")
    V = {**rules_A(), **rules_B(), **rules_C(), **rules_D()}
    disc = {}
    for k, rows in V.items():
        d = [r for r in rows if r["t_close"] < CUT]
        if len(d) >= 10:
            disc[k] = cell_stats(d)
    for k, v in sorted(disc.items(), key=lambda kv: -kv[1]["ret"])[:15]:
        print(f"   {k:34s} n={v['n']:4d} ev={v['events']:3d} win={v['win']:.2f} px={v['px']:.2f} ret={v['ret']:+.3f} t={v['t']:+.2f} wo3={v['ret_wo3']:+.3f}")
    fam = collections.defaultdict(list)
    for k, v in disc.items():
        fam[k[0]].append(v["ret"])
    print("   median discovery return by mechanism:", {f: round(st.median(r), 3) for f, r in fam.items()})
    bar = [k for k, v in disc.items() if v["n"] >= 40 and v["ret"] >= 0.10 and v["t"] >= 2.0]
    print(f"   variants in this grid: {len(V)} (with >= 10 discovery trades: {len(disc)}); meeting the discovery bar (n>=40, ret>=10%, t>=2): {len(bar)}")
    frozen = [k for k, _ in sorted(((k, v) for k, v in disc.items() if v["n"] >= 40), key=lambda kv: -kv[1]["ret"])[:3]]
    print("\n3. VALIDATION of the 3 best discovery variants (n >= 40), frozen, evaluated once")
    val = []
    for k in frozen:
        rows = [r for r in V[k] if r["t_close"] >= CUT]
        s = full_stats(rows) if len(rows) >= 3 else {"n": len(rows)}
        s["rule"] = k; s["discovery"] = disc[k]; val.append(s)
        if len(rows) >= 3:
            print(f"   {k:34s} n={s['n']} ev={s['events']} win={s['win']:.2f} px={s['px']:.2f} ret={s['ret']:+.3f} t={s['t']:+.2f} wo3={s['ret_wo3']:+.3f} halves={s['half1']:+.3f}/{s['half2']:+.3f}")
    cap = capacity()
    print("   capacity: median top-of-book contracts at a 0.05-0.95 price, live snapshot:", cap)
    best_k = max(disc, key=lambda k: disc[k]["ret"] if disc[k]["n"] >= 40 else -9)
    (OUT / "study.json").write_text(json.dumps({"cut": CUT, "information": info, "discovery": disc, "frozen": frozen, "validation": val,
                                                "capacity_top_of_book": cap, "grid_variants": len(V)}, default=str, indent=1))
    print("\nwrote", OUT / "study.json", "| best discovery:", best_k, disc[best_k])


if __name__ == "__main__":
    main()
