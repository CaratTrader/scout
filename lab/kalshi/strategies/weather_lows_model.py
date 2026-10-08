"""weather_lows climatology model (ASOS only, no Kalshi prices): for a station and an hour k of the climate day,
the empirical distribution of drop = cur_k - (min of the upper-bound readings after the decision cut-off), from
past climate days. Then P(official min in [lo, hi] | U_k, cur_k) with a measurement term delta (official = U_end - delta).
fit(days_before, months) only uses ASOS days strictly before `days_before` (no look-ahead into the test period)."""
from __future__ import annotations
import bisect, datetime as dt
from collections import defaultdict
from lab.kalshi.strategies import weather_lows_obs as O

LAG = 360        # obs must be valid <= H - 6 min (decision at H - 1 min, fill at the quote at H)


def day_series(obs, d0, d1):
    """per hour k=1..23: (U_k, cur_k, F_k) where F_k = min upper-bound reading after the cut-off (None if none)."""
    out = {}
    ub = [(u, (v if k == "H" else v[1] if k == "5" else None)) for u, k, v in obs if k in ("H", "5")]
    for k in range(1, 24):
        H = d0 + k * 3600
        s = O.state(obs, H - 60, d0, lag=LAG - 60)
        if not s or s["cur_t"] is None or H - s["cur_t"] > 90 * 60:
            continue
        fut = [v for u, v in ub if H - LAG < u <= d1]
        out[k] = (s["U"], s["cur"], min(fut) if fut else None, s)
    return out


def fit(stations: dict, before: str, months=(6, 7, 8, 9, 10, 11)):
    """stations: stn -> (obs_by_day, tz). Returns drops[(stn, k)] sorted list and pooled[k]."""
    drops = defaultdict(list); pooled = defaultdict(list)
    for stn, (obs_by_day, tz) in stations.items():
        for day, obs in obs_by_day.items():
            if day >= before or int(day[5:7]) not in months:
                continue
            d0, d1 = O.day_bounds(day, tz)
            for k, (U, cur, F, s) in day_series(obs, d0, d1).items():
                if F is None:
                    continue
                d = cur - F
                drops[(stn, k)].append(d); pooled[k].append(d)
    for v in list(drops.values()) + list(pooled.values()):
        v.sort()
    return drops, pooled


def p_drop_ge(drops, pooled, stn, k, x, w_pool=30):
    """P(drop >= x), station-specific empirical with a pooled prior of weight w_pool."""
    a = drops.get((stn, k), []); b = pooled.get(k, [])
    pa = (len(a) - bisect.bisect_left(a, x)) / len(a) if a else 0.0
    pb = (len(b) - bisect.bisect_left(b, x)) / len(b) if b else 0.0
    return (len(a) * pa + w_pool * pb) / (len(a) + w_pool) if (a or b) else 0.0


def p_end_le(drops, pooled, stn, k, U, cur, j):
    """P(U_end <= j) given running upper bound U and current reading cur."""
    if U <= j:
        return 1.0
    return p_drop_ge(drops, pooled, stn, k, cur - j)


def p_bucket(drops, pooled, stn, k, U, cur, lo, hi, dl):
    """P(official in [lo, hi]); official = U_end - delta, dl = {delta: prob}."""
    p = 0.0
    for d, q in dl.items():
        a, b = lo + d, hi + d
        pb = (p_end_le(drops, pooled, stn, k, U, cur, b) if b < 1e8 else 1.0) - (p_end_le(drops, pooled, stn, k, U, cur, a - 1) if a > -1e8 else 0.0)
        p += q * pb
    return min(max(p, 0.0), 1.0)
