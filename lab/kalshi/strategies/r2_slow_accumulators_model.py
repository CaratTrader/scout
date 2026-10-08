"""Climatology model for monthly precipitation markets: P(month total > strike | month-to-date through yesterday).
Remaining-period precipitation is sampled from the station's own daily history (ACIS = NWS official daily values):
for every earlier year and every calendar shift s in -SHIFT..+SHIFT days, the sum over a window of the same length
starting on the same calendar day (+s). Only years strictly before the event year are used (no look-ahead)."""
from __future__ import annotations
import calendar, datetime as dt, sys
from functools import lru_cache
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_slow_accumulators_data import acis, STATIONS

SHIFT = 7
TZ = {"NYC": "America/New_York", "MIA": "America/New_York", "SPG": "America/New_York", "MDW": "America/Chicago", "ORD": "America/Chicago",
      "AUS": "America/Chicago", "DFW": "America/Chicago", "HOU": "America/Chicago", "DEN": "America/Denver",
      "LAX": "America/Los_Angeles", "SFO": "America/Los_Angeles", "SEA": "America/Los_Angeles"}
MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


@lru_cache(maxsize=None)
def precip(st: str) -> dict[dt.date, float | None]:
    return {dt.date.fromisoformat(k): v for k, v in acis(STATIONS[st], edate="2026-10-07").items()}


def ym(event: str) -> tuple[int, int]:
    e = event.split("-")[1]
    return 2000 + int(e[:2]), MON[e[2:]]


def mtd(st: str, y: int, m: int, day: int) -> float | None:
    """Month-to-date total through day-1 (known at 10:00 local on `day`)."""
    P = precip(st); s = 0.0
    for d in range(1, day):
        v = P.get(dt.date(y, m, d))
        if v is None:
            return None
        s += v
    return s


@lru_cache(maxsize=None)
def samples(st: str, y: int, m: int, day: int) -> tuple[float, ...]:
    """Remaining-period totals (days day..end of month) from years < y, calendar shifts -SHIFT..SHIFT."""
    P = precip(st); last = calendar.monthrange(y, m)[1]; L = last - day + 1
    out = []
    for yy in range(1971, y):
        try:
            base = dt.date(yy, m, min(day, calendar.monthrange(yy, m)[1]))
        except ValueError:
            continue
        for s in range(-SHIFT, SHIFT + 1):
            st0 = base + dt.timedelta(days=s); tot = 0.0; ok = True
            for k in range(L):
                v = P.get(st0 + dt.timedelta(days=k))
                if v is None:
                    ok = False; break
                tot += v
            if ok:
                out.append(tot)
    return tuple(sorted(out))


def p_yes(st: str, y: int, m: int, day: int, strike: float, have: float) -> float | None:
    """P(have + R > strike), R from samples; shrunk slightly away from 0/1 (add-one)."""
    S = samples(st, y, m, day)
    if len(S) < 100:
        return None
    need = strike - have
    k = sum(1 for r in S if r > need + 1e-9)
    return (k + 0.5) / (len(S) + 1.0)
