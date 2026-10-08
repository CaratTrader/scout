"""r2_weather_daily_maker: resting (maker) YES bids on the favourite bucket of Kalshi daily-high ladders (KXHIGH*,
24 cities), testing round-1 lead D2 (mids ~10 h before close look underconfident: favourites at 0.6-0.8 ~3-5c cheap).
KXHIGH* is fee_type 'quadratic': makers pay no fee. Disk data from r2_weather_daily_maker_data.py.

Order model (decision at local wall-clock minute T, using only candles that END at or before T):
  favourite  = the bucket whose mid (ask+bid)/2 at T is in [MID_LO, MID_HI] (at most one per ladder)
  price      = bid + 1c if the spread is >= 2c, else join the bid (queue behind the visible size)
  placed     = T + 1 min; if the ask at T+1 is already <= our price the order crosses -> taker fill at that ask + fee
  live       = L minutes, cancelled after that; one unit per market (first fill wins)
  fill       = STRICT: a trade printed strictly below our price (price_low < b) or the ask fell strictly below our
               price (ask_low < b) in a candle that ends in (T+1, T+1+L]: every bid at b, ours included, was used up.
               TOUCH (diagnostic only): <= instead of <.
Trade prints / ask lows exist only from 2026-07-19 (the /historical archive has closes only), so fills are judged on
2026-07-19..10-05; the archive is used for the unconditional calibration part only.
RESULT (validation run once, 2026-10-08): dead. 3 frozen rules +0.6% / +2.4% / +1.9% per $ (t <= 0.7), second halves
negative; filled orders win 71% vs 97-100% unfilled (adverse selection), although unconditional favourites at 15:00
win 77.1% vs mid 72.1%. See data/kalshi_lab/strategies/r2_weather_daily_maker/result.json.
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_weather_daily_maker explore|discover|validate"""
from __future__ import annotations
import gzip, json, math, pickle, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r2_weather_daily_maker"
MAX_AGE = 30
STAKE = 5.0


def load():
    with gzip.open(OUT / "days.pkl.gz", "rb") as fh:
        return pickle.load(fh)


def taker_fee_c(p: float, stake: float = STAKE) -> float:
    """Per-contract taker fee (dollars) for one ~$5 order, rounded up to the cent per order."""
    n = max(1, int(stake / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def at(cd, minute):
    """index of the last candle ending at or before `minute` (fresh <= MAX_AGE), else None"""
    lo, hi = 0, len(cd)
    while lo < hi:
        mid = (lo + hi) // 2
        if cd[mid][0] <= minute:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    if i < 0 or minute - cd[i][0] > MAX_AGE:
        return None
    return i


def clustered_t(rows, key="cl"):
    """mean of r['ret'] with a CR1 cluster-robust t (clusters = station-day)."""
    n = len(rows)
    if n < 2:
        return (rows[0]["ret"] if rows else float("nan")), float("nan"), n
    m = sum(r["ret"] for r in rows) / n
    by = defaultdict(float)
    for r in rows:
        by[r[key]] += r["ret"] - m
    g = len(by)
    if g < 2:
        return m, float("nan"), g
    v = sum(s * s for s in by.values()) / n ** 2 * g / (g - 1)
    return m, (m / math.sqrt(v) if v > 0 else float("nan")), g


def stats(rows):
    if not rows:
        return {"n": 0}
    m, t, g = clustered_t(rows)
    rs = sorted((r["ret"] for r in rows), reverse=True)
    days = sorted({r["day"] for r in rows})
    mid = sorted(r["close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["close"] < mid]; h2 = [r["ret"] for r in rows if r["close"] >= mid]
    return {"n": len(rows), "events": g, "win": round(sum(r["won"] for r in rows) / len(rows), 4),
            "avg_px": round(st.mean(r["px"] for r in rows), 4), "ret_per_dollar": round(m, 4),
            "t": round(t, 2) if t == t else None, "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "trades_per_day": round(len(rows) / max(1, len(days)), 2)}


# ------------------------------------------------------------------ order simulation
def favourite(day, T, mid_lo, mid_hi, side="YES"):
    """-> (market, idx, ask, bid) of the favourite at T, or None. side NO: the 'favourite' is the bucket's NO side,
    i.e. a YES mid in [1-mid_hi, 1-mid_lo]; prices are then expressed for the NO contract."""
    best = None
    for b in day["mk"]:
        i = at(b["cd"], T)
        if i is None:
            continue
        ask, bid = b["cd"][i][1], b["cd"][i][2]
        if ask <= 0 or bid <= 0 or ask < bid or ask >= 100:
            continue
        mid = (ask + bid) / 200
        if side == "YES" and mid_lo <= mid <= mid_hi:
            if best is None or mid > best[4]:
                best = (b, i, ask, bid, mid)
        if side == "NO" and 1 - mid_hi <= mid <= 1 - mid_lo:
            if best is None or mid < best[4]:
                best = (b, i, ask, bid, mid)
    return best


def fair_p(fair, T, mid):
    a, b = fair[T]
    x = math.log(mid / (1 - mid))
    return 1 / (1 + math.exp(-(a + b * x)))


def simulate(day, T, L, mid_lo=0.55, mid_hi=0.90, improve=1, side="YES", fill_rule="strict", max_spread=99,
             min_spread=1, fair=None, edge=0.0):
    """One maker order on the favourite of `day` at decision minute T. -> dict or None (no favourite / no quote /
    filtered out). price = bid + min(improve, spread - 1) (never crosses the visible ask)."""
    f = favourite(day, T, mid_lo, mid_hi, side)
    if not f:
        return None
    b, i, ask, bid, mid = f
    cd = b["cd"]
    if side == "NO":   # NO contract: bid_no = 100 - yes_ask, ask_no = 100 - yes_bid
        ask, bid = 100 - bid, 100 - ask
    spread = ask - bid
    if spread > max_spread or spread < min_spread:
        return None
    px = bid + max(0, min(improve, spread - 1))
    if fair is not None:
        fy = fair_p(fair, T, min(0.97, max(0.03, mid)))
        if (fy if side == "YES" else 1 - fy) - px / 100 < edge:
            return None
    won = b["won"] if side == "YES" else not b["won"]
    base = {"stn": day["stn"], "day": day["day"], "cl": (day["stn"], day["day"]), "close": day["close"], "part": day["part"],
            "tk": b["tk"], "T": T, "mid": mid if side == "YES" else 1 - mid, "spread": spread, "px": px / 100,
            "won": won, "vol": b["vol"]}
    # T+1: does the order cross?
    j = at(cd, T + 1)
    if j is not None:
        a1 = cd[j][1] if side == "YES" else 100 - cd[j][2]
        if 0 < a1 <= px:
            p = a1 / 100
            return {**base, "fill": "cross", "px": p, "ret": ((1.0 if won else 0.0) - p - taker_fee_c(p)) / p, "fill_min": T + 1}
    # resting window: candles ending in (T+1, T+1+L]
    fill_min = None; strict = fill_rule == "strict"; tv = 0.0
    k = j + 1 if j is not None else 0
    while k < len(cd) and cd[k][0] <= T + 1:
        k += 1
    while k < len(cd) and cd[k][0] <= T + 1 + L:
        m, ac, bc, alo, bhi, plo, phi, pcl, v = cd[k]
        if side == "YES":
            tlo, alow = plo, alo
        else:   # NO price = 100 - YES price; a NO trade below our NO bid = a YES trade above 100 - px
            tlo = None if phi is None else 100 - phi
            alow = None if bhi is None else 100 - bhi
        hit = False
        if fill_rule == "proxy":   # archive (closes only): ask close below our price, or the bid close fell below
            # both the decision-time bid and our price - 1 (calibrated on 2026-07-19..08-19 prints for improve=1:
            # precision 93%, recall 90% vs the strict print rule)
            a_c, b_c = (ac, bc) if side == "YES" else (100 - bc, 100 - ac)
            hit = a_c < px or b_c < min(px - 1, bid)
        else:
            for x in (tlo, alow):
                if x is not None and (x < px if strict else x <= px):
                    hit = True
        if hit:
            fill_min = m; break
        k += 1
    if fill_min is None:
        return {**base, "fill": "none", "ret": None}
    p = px / 100
    return {**base, "fill": "maker", "ret": ((1.0 if won else 0.0) - p) / p, "fill_min": fill_min}


def run(D, T, L, **kw):
    out = []
    for day in D:
        r = simulate(day, T, L, **kw)
        if r:
            out.append(r)
    return out


def summarize(rows, label=""):
    f = [r for r in rows if r["fill"] in ("maker", "cross")]
    u = [r for r in rows if r["fill"] == "none"]
    wf = sum(r["won"] for r in f) / len(f) if f else float("nan")
    wu = sum(r["won"] for r in u) / len(u) if u else float("nan")
    s = stats(f)
    return {"label": label, "orders": len(rows), "filled": len(f), "fill_rate": round(len(f) / max(1, len(rows)), 3),
            "cross": sum(r["fill"] == "cross" for r in rows), "win_filled": round(wf, 3), "win_unfilled": round(wu, 3),
            "win_all": round(sum(r["won"] for r in rows) / max(1, len(rows)), 3),
            "avg_mid": round(st.mean(r["mid"] for r in rows), 3) if rows else None, **{k: v for k, v in s.items()}}


def explore():
    D = load()
    nvar = 0
    disc = [d for d in D if d["part"] == "disc"]
    arch = [d for d in disc if not d["prints"]]; rec = [d for d in disc if d["prints"]]
    res = {"calibration": {}, "maker": {}}
    # 1. unconditional calibration of the favourite (all disc), as if every order filled at the mid / at bid
    for name, S in (("archive", arch), ("recent_disc", rec)):
        for T in (11 * 60, 13 * 60, 15 * 60, 17 * 60):
            for lo, hi in ((0.55, 0.70), (0.70, 0.80), (0.80, 0.90), (0.55, 0.90)):
                rows = []
                for d in S:
                    f = favourite(d, T, lo, hi)
                    if f:
                        b, i, ask, bid, mid = f
                        rows.append({"won": b["won"], "mid": mid, "bid": bid / 100, "ask": ask / 100, "spr": ask - bid})
                nvar += 1
                if not rows:
                    continue
                w = sum(r["won"] for r in rows) / len(rows); mm = st.mean(r["mid"] for r in rows)
                res["calibration"][f"{name}|{T // 60}h|{lo}-{hi}"] = {
                    "n": len(rows), "win": round(w, 3), "mid": round(mm, 3), "gap_c": round(100 * (w - mm), 1),
                    "se_c": round(100 * math.sqrt(w * (1 - w) / len(rows)), 1),
                    "ret_at_bid": round(st.mean(((1 if r["won"] else 0) - r["bid"]) / r["bid"] for r in rows), 4),
                    "taker_ret": round(st.mean(((1 if r["won"] else 0) - r["ask"] - taker_fee_c(r["ask"])) / r["ask"] for r in rows), 4),
                    "spread_med": st.median(r["spr"] for r in rows), "spread_ge2": round(sum(r["spr"] >= 2 for r in rows) / len(rows), 3)}
    # 2. maker simulation on recent discovery (prints available)
    for T in (11 * 60, 13 * 60, 15 * 60, 17 * 60):
        for L in (60, 120):
            for rule in ("strict", "touch"):
                for lo, hi in ((0.55, 0.90), (0.55, 0.70), (0.70, 0.90)):
                    rows = run(rec, T, L, mid_lo=lo, mid_hi=hi, fill_rule=rule)
                    nvar += 1
                    res["maker"][f"{T // 60}h|L{L}|{rule}|{lo}-{hi}"] = summarize(rows)
    # 3. NO side (longshot NO = favourite of the NO contract), strict, mid 0.55-0.90 of the NO contract
    for T in (13 * 60, 15 * 60):
        for L in (60, 120):
            rows = run(rec, T, L, side="NO", fill_rule="strict")
            nvar += 1
            res["maker"][f"NO|{T // 60}h|L{L}|strict|0.55-0.90"] = summarize(rows)
    res["variants"] = nvar
    (OUT / "explore_discovery.json").write_text(json.dumps(res, indent=1, default=str))
    print("CALIBRATION (discovery)  gap = win - mid in cents")
    for k, v in res["calibration"].items():
        print(f"  {k:32s} n={v['n']:4d} win={v['win']:.3f} mid={v['mid']:.3f} gap={v['gap_c']:+5.1f}c (se {v['se_c']}) "
              f"at_bid={v['ret_at_bid']:+.1%} taker={v['taker_ret']:+.1%} spr_med={v['spread_med']} spr>=2c={v['spread_ge2']:.0%}")
    print("MAKER (recent discovery 2026-07-19..08-19)")
    for k, v in res["maker"].items():
        print(f"  {k:30s} orders={v['orders']:4d} fill={v['fill_rate']:.0%} cross={v['cross']:3d} win f/u={v['win_filled']:.3f}/{v['win_unfilled']:.3f} "
              f"n={v.get('n')} ret={v.get('ret_per_dollar')} t={v.get('t')} wo3={v.get('ret_wo3')} px={v.get('avg_px')}")
    print("variants", nvar)


def fit_logistic(xs, ys, iters=40):
    a, b = 0.0, 1.0
    for _ in range(iters):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys):
            z = max(-30.0, min(30.0, a + b * x)); p = 1 / (1 + math.exp(-z)); w = p * (1 - p)
            ga += y - p; gb += (y - p) * x; haa += w; hab += w * x; hbb += w * x * x
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        da = (hbb * ga - hab * gb) / det; db = (haa * gb - hab * ga) / det
        a += da; b += db
        if abs(da) + abs(db) < 1e-9:
            break
    return a, b


def fit_fair(D, hours):
    """logistic recalibration win ~ a + b logit(mid) of every bucket (mid 0.03-0.97) at each decision minute, on the
    given (discovery) station-days only."""
    out = {}
    for T in hours:
        xs, ys = [], []
        for d in D:
            for b in d["mk"]:
                i = at(b["cd"], T)
                if i is None:
                    continue
                ask, bid = b["cd"][i][1], b["cd"][i][2]
                if not (0 < bid <= ask < 100):
                    continue
                m = (ask + bid) / 200
                if 0.03 <= m <= 0.97:
                    xs.append(math.log(m / (1 - m))); ys.append(1 if b["won"] else 0)
        out[T] = fit_logistic(xs, ys)
    return out


HOURS = (11 * 60, 13 * 60, 15 * 60, 17 * 60)
BANDS = ((0.55, 0.90), (0.55, 0.70), (0.70, 0.90))


def grid():
    for T in HOURS:
        for L in (30, 60, 120):
            for band in BANDS:
                for improve in (0, 1, 2):
                    for min_spread in (1, 3):
                        for edge in (None, 0.02):
                            yield {"T": T, "L": L, "mid_lo": band[0], "mid_hi": band[1], "improve": improve,
                                   "min_spread": min_spread, "edge": edge}


def cell_id(c):
    return f"T{c['T'] // 60}h L{c['L']} mid{c['mid_lo']}-{c['mid_hi']} imp{c['improve']} spr>={c['min_spread']} edge{c['edge']}"


def run_cell(S, c, fair, rule):
    kw = {k: v for k, v in c.items() if k not in ("T", "L", "edge")}
    if c["edge"] is not None:
        kw["fair"] = fair; kw["edge"] = c["edge"]
    return run(S, c["T"], c["L"], fill_rule=rule, **kw)


def discover():
    D = load()
    disc = [d for d in D if d["part"] == "disc"]
    arch = [d for d in disc if not d["prints"]]; rec = [d for d in disc if d["prints"]]
    fair = fit_fair(disc, HOURS)
    print("fair-value logistic (discovery, all buckets):", {T // 60: (round(a, 3), round(b, 3)) for T, (a, b) in fair.items()})
    cells = []
    for c in grid():
        r_rec = summarize(run_cell(rec, c, fair, "strict"))
        r_arc = summarize(run_cell(arch, c, fair, "proxy"))
        kill = r_rec["filled"] > 0 and r_rec["win_filled"] < r_rec["win_unfilled"] - 0.05
        cells.append({"cell": c, "id": cell_id(c), "recent": r_rec, "archive_proxy": r_arc, "kill_adverse": kill})
    nvar = len(cells) * 2 + 84          # grid on two samples + the explore pass
    ok = [x for x in cells if x["recent"]["filled"] >= 80 and (x["archive_proxy"].get("ret_per_dollar") or -1) > 0
          and x["recent"].get("t") is not None]
    ok.sort(key=lambda x: -x["recent"]["t"])
    chosen, seen = [], set()
    for x in ok:
        sig = (x["recent"]["n"], x["recent"]["ret_per_dollar"])
        if sig in seen:
            continue
        seen.add(sig); chosen.append(x)
        if len(chosen) == 3:
            break
    print(f"cells {len(cells)} x 2 samples; eligible (recent fills >= 80, archive-proxy ret > 0): {len(ok)}; "
          f"kill_adverse in {sum(x['kill_adverse'] for x in cells)} of {len(cells)} recent cells")
    print("TOP 15 by recent-discovery clustered t (strict fills)")
    for x in sorted([x for x in cells if x['recent']['filled'] >= 40 and x['recent'].get('t') is not None], key=lambda x: -x['recent']['t'])[:15]:
        r, a = x["recent"], x["archive_proxy"]
        print(f"  {x['id']:48s} rec n={r['n']:4d} fill={r['fill_rate']:.0%} win f/u={r['win_filled']:.2f}/{r['win_unfilled']:.2f} ret={r['ret_per_dollar']:+.1%} t={r['t']} wo3={r['ret_wo3']:+.1%} | "
              f"arch n={a.get('n')} ret={a.get('ret_per_dollar')} t={a.get('t')} kill={x['kill_adverse']}")
    print("CHOSEN (frozen for validation)")
    for x in chosen:
        print("  ", x["id"], x["recent"]["ret_per_dollar"], x["recent"]["t"], x["archive_proxy"].get("ret_per_dollar"))
    # The top 3 by t are three near-copies of one cell (15:00, spread >= 3c, bid + 2c). To make the one validation
    # pass more informative, C2 and C3 are fixed by design rather than by rank (decided on discovery only):
    #   C1 discovery winner; C2 the same pocket with the test plan's own parameters (bid + 1c, 120 min, no fair-value
    #   model); C3 the test plan's unfiltered baseline (bid + 1c on every favourite 0.55-0.90 at 15:00, 120 min),
    #   i.e. the direct out-of-sample test of lead D2.
    C1 = chosen[0]["cell"]
    C2 = {"T": 900, "L": 120, "mid_lo": 0.55, "mid_hi": 0.90, "improve": 1, "min_spread": 3, "edge": None}
    C3 = {"T": 900, "L": 120, "mid_lo": 0.55, "mid_hi": 0.90, "improve": 1, "min_spread": 1, "edge": None}
    frozen = {"written": "2026-10-08, before any validation run", "fair_model": {str(k): v for k, v in fair.items()},
              "candidates": [C1, C2, C3], "ids": [cell_id(c) for c in (C1, C2, C3)], "variants_examined": nvar,
              "top3_by_t": [x["id"] for x in chosen],
              "discovery": {cell_id(c): next(x for x in cells if x["cell"] == c) for c in (C1, C2, C3)},
              "selection": "C1 = best clustered t among recent-discovery cells (2026-07-19..08-19, strict print fills) with >= 80 fills and positive archive-proxy return; C2/C3 fixed by design (see code)"}
    (OUT / "discovery_cells.json").write_text(json.dumps(cells, default=str))
    if not (OUT / "validation.lock").exists():
        (OUT / "frozen.json").write_text(json.dumps(frozen, indent=1, default=str))
    print("variants", nvar)


def validate():
    lock = OUT / "validation.lock"
    if lock.exists():
        print("validation already run once:", lock.read_text()); return
    fz = json.loads((OUT / "frozen.json").read_text())
    fair = {int(k): tuple(v) for k, v in fz["fair_model"].items()}
    D = load(); val = [d for d in D if d["part"] == "val"]
    out = []
    for c, cid in zip(fz["candidates"], fz["ids"]):
        rows = run_cell(val, c, fair, "strict")
        s = summarize(rows, cid)
        s["kill_adverse"] = s["filled"] > 0 and s["win_filled"] < s["win_unfilled"] - 0.05
        f = [r for r in rows if r["fill"] != "none"]
        s["fills_cross_taker"] = sum(r["fill"] == "cross" for r in rows)
        s["first_validation_day"] = min(r["day"] for r in rows) if rows else None
        s["last_validation_day"] = max(r["day"] for r in rows) if rows else None
        s["rows"] = [{k: r[k] for k in ("stn", "day", "tk", "T", "mid", "spread", "px", "won", "fill", "ret")} for r in rows]
        out.append(s)
        print(f"{cid:48s} orders={s['orders']} filled={s['filled']} win f/u={s['win_filled']}/{s['win_unfilled']} n={s.get('n')} ev={s.get('events')} "
              f"ret={s.get('ret_per_dollar')} t={s.get('t')} wo3={s.get('ret_wo3')} halves={s.get('half1')}/{s.get('half2')} kill={s['kill_adverse']}")
    (OUT / "validation.json").write_text(json.dumps(out, indent=1, default=str))
    lock.write_text("run 2026-10-08")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "explore"
    {"explore": explore, "discover": discover, "validate": validate}[cmd]()
