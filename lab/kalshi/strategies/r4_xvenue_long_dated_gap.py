"""r4_xvenue_long_dated_gap: buy Kalshi NO when the Kalshi YES bid sits >= X above the Polymarket price of the same
question (long-dated markets, checked daily at 14:00 UTC, held to settlement).

Inputs (all frozen before prices were read, see mapping_freeze.json):
  mapping.json        same-question pairs (Kalshi market <-> Polymarket market), ex-ante decision window [w0, w1]
  fetch_plan.json     price-blind selection (r3-cached pairs + seeded random, <= 3 per Kalshi event)
  kalshi_candles.jsonl, poly_hourly.jsonl  (r4_xvenue_long_dated_gap_data)
Execution: signal at t = 14:00 UTC from the last Kalshi hourly candle <= t (age <= 24 h) and the last Polymarket hourly
point <= t (age <= 3 h); taker fill at the Kalshi quote of the last candle <= t + 1 h (NO = 1 - yes_bid, YES = yes_ask),
market still open at t + 1 h; fee ceil-to-cent of 0.07*10*p*(1-p) per 10-contract order; one trade per market (first
signal day); return per $ = (payout - px - fee) / px. Split: Kalshi events by last close, first 70% discovery.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_xvenue_long_dated_gap [analyze]"""
from __future__ import annotations
import bisect, datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_api import OUT, used
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_map import MAP, DAY, kalshi_raw
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_data import PLAN, KC, PH

H = 3600
XS = (0.04, 0.06, 0.08)
BANDS = {"all": (0.0, 1.0), "NO<0.90": (0.0, 0.90), "NO>=0.90": (0.90, 1.0)}
VARIANTS = []          # every cell examined (for variants_examined)


def fee_pc(p: float, n: int = 10) -> float:
    return math.ceil(round(0.07 * n * p * (1 - p) * 100, 9)) / 100 / n


def last_le(ts: list[int], rows: list, t: int, maxage: int):
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > maxage:
        return None
    return rows[i]


# ---------------------------------------------------------------------------------------------- beta lower bound
def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / d if abs(d) > 1e-30 else 1e30
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / d if abs(d) > 1e-30 else 1e30
        c = 1 + aa / c if abs(c) > 1e-30 else 1e30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / d if abs(d) > 1e-30 else 1e30
        c = 1 + aa / c if abs(c) > 1e-30 else 1e30
        de = d * c; h *= de
        if abs(de - 1) < 1e-12:
            break
    return h


def betainc(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1 - math.exp(lb) * _betacf(b, a, 1 - x) / b


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound of a binomial proportion."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(80):
        mid = (lo + hi) / 2
        if 1 - betainc(k, n - k + 1, mid) > alpha:   # P(X >= k | p=mid) > alpha -> mid still plausible
            hi = mid
        else:
            lo = mid
    return lo


# ---------------------------------------------------------------------------------------------- data
def load():
    M = {r["k"]: r for r in json.loads(MAP.read_text())["pairs"]}
    pl = json.loads(PLAN.read_text())
    KCd = {json.loads(l)["t"]: json.loads(l) for l in KC.open()}
    PHd = {json.loads(l)["t"]: json.loads(l) for l in PH.open()}
    raw = kalshi_raw()
    sel = []
    for t in pl["pairs"]:
        r = dict(M[t]); m = raw.get(t)
        if not m or m.get("result") not in ("yes", "no"):
            continue
        r["result"] = m["result"]
        c = sorted(KCd.get(t, {}).get("c", []))
        h = sorted(PHd.get(t, {}).get("h", []))
        r["kc"], r["kts"] = c, [x[0] for x in c]
        r["ph"], r["pts"] = h, [x[0] for x in h]
        sel.append(r)
    evclose = defaultdict(int)
    for r in sel:
        evclose[r["e"]] = max(evclose[r["e"]], r["k_close"] or 0)
    order = sorted(evclose, key=lambda e: (evclose[e], e))
    cut_i = int(len(order) * 0.7)
    disc_ev = set(order[:cut_i])
    for r in sel:
        r["t_close"] = evclose[r["e"]]
        r["part"] = "disc" if r["e"] in disc_ev else "val"
    cut = evclose[order[cut_i]] if cut_i < len(order) else None
    return sel, cut, order


def days(r):
    d0 = dt.datetime.utcfromtimestamp(r["w0"]).replace(hour=14, minute=0, second=0, microsecond=0)
    t = int(d0.timestamp())
    if t < r["w0"]:
        t += DAY
    while t <= r["w1"]:
        yield t
        t += DAY


ALIGN = {"lag": 60}   # decision at 14:00:00 + lag. Polymarket hourly points are stamped hh:00:02-18, Kalshi candles hh:00:00:
                      # lag=0 (the frozen plan as literally written) compares a fresh Kalshi quote with a 1-hour-old
                      # Polymarket price; lag=60 s compares the two venues at the same instant (bug fix, see notes).


def panel(r, kage=24 * H, page=3 * H):
    """Daily rows: t, ask, bid, poly, next-hour ask/bid (fill), hourly volume over the prior 24 h."""
    out = []
    for t0 in days(r):
        t = t0 + ALIGN["lag"]
        if not (r["k_open"] or 0) <= t < (r["k_close"] or 0):
            continue
        q = last_le(r["kts"], r["kc"], t, kage)
        p = last_le(r["pts"], r["ph"], t, page)
        if not q or not p:
            continue
        f = last_le(r["kts"], r["kc"], t0 + H, kage) if (r["k_close"] or 0) > t0 + H else None
        i0 = bisect.bisect_right(r["kts"], t - DAY); i1 = bisect.bisect_right(r["kts"], t)
        vol24 = sum(x[3] for x in r["kc"][i0:i1])
        out.append({"t": t, "ask": q[1], "bid": q[2], "poly": p[1], "fill": f, "vol24": vol24, "oi": q[4] if len(q) > 4 else None})
    return out


def trades(sel, X, side="NO", band="all", tier="all", mode="first", kage=24 * H, fill_delay=True):
    lo, hi = BANDS[band]
    out = []
    for r in sel:
        if tier == "A" and r["tier"] != "A":
            continue
        for row in panel(r, kage=kage):
            # side NO  : Kalshi rich vs Polymarket -> buy Kalshi NO (the hypothesis)
            # side YES : Kalshi cheap vs Polymarket -> buy Kalshi YES (mirror)
            # FOLLOW_YES: Kalshi rich -> buy Kalshi YES; FOLLOW_NO: Kalshi cheap -> buy Kalshi NO (bet that Kalshi leads)
            if side in ("NO", "FOLLOW_YES"):
                sig = row["bid"] - row["poly"] >= X - 1e-9
            else:
                sig = row["poly"] - row["ask"] >= X - 1e-9
            buy = "NO" if side in ("NO", "FOLLOW_NO") else "YES"
            ps = (1 - row["bid"]) if buy == "NO" else row["ask"]
            if not sig or not (lo <= ps < hi if buy == "NO" else True):
                continue
            f = row["fill"] if fill_delay else [row["t"], row["ask"], row["bid"]]
            if not f:
                continue
            px = (1 - f[2]) if buy == "NO" else f[1]
            if not 0.01 <= px <= 0.99:
                continue
            won = (r["result"] == "no") if buy == "NO" else (r["result"] == "yes")
            fe = fee_pc(px)
            out.append({"k": r["k"], "e": r["e"], "fam": r["fam"], "tier": r["tier"], "part": r["part"], "t": row["t"],
                        "t_close": r["t_close"], "ps": ps, "px": px, "fee": fe, "won": won, "ret": ((1.0 if won else 0.0) - px - fe) / px,
                        "gap": (row["bid"] - row["poly"]) if side in ("NO", "FOLLOW_YES") else (row["poly"] - row["ask"]),
                        "kbid": row["bid"], "kask": row["ask"], "poly": row["poly"], "vol24": row["vol24"], "oi": row["oi"],
                        "days_to_S": (r["S"] - row["t"]) / DAY})
            if mode == "first":
                break
    return out


def stats(rows, key="e"):
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for x in rows:
        ev[x[key]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((x["ret"] for x in rows), reverse=True)
    ts = sorted(x["t_close"] for x in rows)
    mid = ts[len(ts) // 2]
    h1 = [x["ret"] for x in rows if x["t_close"] < mid]; h2 = [x["ret"] for x in rows if x["t_close"] >= mid]
    # amendment (d): exact-binomial lower bound over unique events (one outcome per event: the event's mean win rate)
    evw = defaultdict(list)
    for x in rows:
        evw[x["e"]].append(1.0 if x["won"] else 0.0)
    k_ev = sum(1 for v in evw.values() if st.mean(v) >= 0.5)
    lb = cp_lower(k_ev, len(evw))
    apx = st.mean(x["px"] for x in rows); afee = st.mean(x["fee"] for x in rows)
    return {"n": len(rows), "events": n_e, "win": sum(x["won"] for x in rows) / len(rows), "avg_px": apx,
            "ret_per_dollar": st.mean(x["ret"] for x in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
            "ev_mean": st.mean(em), "binom_lb_ret": (lb - apx - afee) / apx,
            "median_vol24": sorted(x["vol24"] for x in rows)[len(rows) // 2],
            "families": len({x["fam"] for x in rows})}


def fmt(name, s):
    if not s.get("n"):
        return f"  {name:44s} n=0"
    return (f"  {name:44s} n={s['n']:3d} ev={s['events']:3d} win={s['win']:.0%} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.1%} "
            f"t={s['t']:5.2f} wo3={s['ret_wo3']:+.1%} h={s['half1']:+.1%}/{s['half2']:+.1%} lb={s['binom_lb_ret']:+.1%} vol24={s['median_vol24']:.0f}")


if __name__ == "__main__":
    print("see r4_xvenue_long_dated_gap_analysis.py")
