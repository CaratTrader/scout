"""Forecast-shifted model (model B) for monthly precipitation: remaining = A + R_after, where
  A       = precipitation over the local days covered by the latest NBM (NWS National Blend, extended text 'NBE',
            IEM archive) QPF run issued >= 3 h before the decision, sampled from the empirical distribution of
            actual/forecast in the calibration pairs (same Q bin and horizon bin);
  R_after = climatology samples (r2_slow_accumulators_model) for the days after the NBM horizon.
Calibration pairs use only months before the calibration cut (discovery months for every prediction)."""
from __future__ import annotations
import bisect, calendar, datetime as dt, sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_slow_accumulators_data import nbe_month
from lab.kalshi.strategies.r2_slow_accumulators_model import precip, samples, TZ

QB = (0.0, 0.05, 0.25, 0.5, 1.0, 2.0, 99.0)
LB = (0, 3, 7, 99)
UTC = dt.timezone.utc


def _ts(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp())


@lru_cache(maxsize=None)
def runs(st: str, y: int, mo: int) -> dict[int, list[tuple[int, float]]]:
    out = defaultdict(list)
    for r in nbe_month(st, y, mo):
        if r["q12"] is None:
            continue
        out[_ts(r["runtime"])].append((_ts(r["ftime"]), r["q12"] / 100.0))
    return {k: sorted(v) for k, v in out.items()}


def local_midnight(st: str, d: dt.date) -> int:
    """Start of the climate day (local STANDARD time midnight) in UTC seconds."""
    tz = ZoneInfo(TZ[st]); off = dt.datetime(d.year, 1, 15, tzinfo=tz).utcoffset()   # standard-time offset
    return int((dt.datetime(d.year, d.month, d.day, tzinfo=UTC) - off).timestamp())


@lru_cache(maxsize=None)
def fcst(st: str, y: int, mo: int, day: int, tdec: int) -> tuple[float, int, int] | None:
    """(Q inches over the covered window, number of covered local days n_cov, first uncovered day) for the run used at
    tdec. Covered local days: day .. day+n_cov-1, the days whose climate-day end lies within the run's QPF periods."""
    R = runs(st, y, mo)
    cands = [k for k in R if k <= tdec - 3 * 3600]
    if not cands:
        return None
    rt = max(cands)
    if tdec - rt > 30 * 3600:
        return None
    per = [(f, q) for f, q in R[rt] if f > tdec]
    if not per:
        return None
    last = calendar.monthrange(y, mo)[1]
    end_month = local_midnight(st, dt.date(y, mo, last) + dt.timedelta(days=1))
    hz = per[-1][0]                              # end of the last QPF period
    n_cov = 0
    for d in range(day, last + 1):
        if local_midnight(st, dt.date(y, mo, d) + dt.timedelta(days=1)) <= hz + 6 * 3600:
            n_cov += 1
        else:
            break
    if n_cov == 0:
        return None
    cov_end = local_midnight(st, dt.date(y, mo, day) + dt.timedelta(days=n_cov))
    Q = sum(q for f, q in per if f - 6 * 3600 <= min(cov_end, end_month))
    return Q, n_cov, day + n_cov


def qbin(Q: float) -> int:
    return max(i for i, b in enumerate(QB[:-1]) if Q >= b)


def lbin(n: int) -> int:
    return max(i for i, b in enumerate(LB[:-1]) if n > b)


def actual(st: str, y: int, mo: int, day: int, n: int) -> float | None:
    P = precip(st); s = 0.0
    for d in range(day, day + n):
        v = P.get(dt.date(y, mo, d))
        if v is None:
            return None
        s += v
    return s


@lru_cache(maxsize=None)
def calib(cut: tuple[int, int], dec_hour: int = 10, exclude: tuple[int, int] | None = None) -> dict[tuple[int, int], tuple[float, ...]]:
    """Empirical 'ratio' samples per (Q bin, horizon bin) from station-months strictly before `cut`:
    for Q bin 0 the samples are actual totals; otherwise actual / Q."""
    from lab.kalshi.strategies.r2_slow_accumulators_data import load_markets, station_of
    from lab.kalshi.strategies.r2_slow_accumulators_model import ym
    sm = sorted({(station_of(m), ym(m["e"])) for m in load_markets().values() if station_of(m) and not m["e"].endswith("26OCT")})
    out = defaultdict(list)
    for st, (y, mo) in sm:
        if (y, mo) >= cut or (y, mo) == exclude:
            continue
        tz = ZoneInfo(TZ[st])
        for day in range(1, calendar.monthrange(y, mo)[1] + 1):
            tdec = int(dt.datetime(y, mo, day, dec_hour, tzinfo=tz).timestamp())
            f = fcst(st, y, mo, day, tdec)
            if not f:
                continue
            Q, n, _ = f; A = actual(st, y, mo, day, n)
            if A is None:
                continue
            b = (qbin(Q), lbin(n))
            out[b].append(A if b[0] == 0 else A / Q)
    return {k: tuple(sorted(v)) for k, v in out.items()}


def p_yes_nbm(st: str, y: int, mo: int, day: int, tdec: int, strike: float, have: float, cut: tuple[int, int], loo: bool = False) -> float | None:
    """loo=True: leave the event's own calendar month out of the calibration pairs (honest in-discovery check)."""
    f = fcst(st, y, mo, day, tdec)
    if not f:
        return None
    Q, n, nxt = f
    C = calib(cut, exclude=(y, mo) if loo and (y, mo) < cut else None)
    b = (qbin(Q), lbin(n))
    S = C.get(b) or ()
    if len(S) < 30:                      # pool horizon bins when thin
        S = tuple(sorted(sum((list(v) for k, v in C.items() if k[0] == b[0]), [])))
    if len(S) < 30:
        return None
    A = S if b[0] == 0 else tuple(r * Q for r in S)
    last = calendar.monthrange(y, mo)[1]
    Rs = samples(st, y, mo, nxt) if nxt <= last else (0.0,)
    need = strike - have
    k = 0
    for a in A:
        k += len(Rs) - bisect.bisect_right(Rs, need - a + 1e-9)
    tot = len(A) * len(Rs)
    return (k + 0.5) / (tot + 1.0)
