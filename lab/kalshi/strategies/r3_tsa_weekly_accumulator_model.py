"""YoY projection model for the KXTSAW weekly average of TSA screenings.

Week = Monday..Sunday; the market settles on the mean of the 7 daily numbers on tsa.gov.
At an information state "days known through date `last`" (some days of the target week may be known), each unknown
day d is projected as g * x(d - lag(d)), where g = sum of the last L known days / sum of their own base days, and
lag(d) = 364 days (same weekday a year earlier), or, near a moving holiday with a fixed weekday (MLK, Presidents,
Easter, Memorial, Labor, Columbus, Thanksgiving), the lag that maps this year's holiday onto last year's.
The probability that the weekly mean exceeds a strike uses the empirical distribution of log(actual remaining sum /
projected remaining sum) for the same number of unknown days, from weeks that were complete before the decision
(expanding window, no look-ahead), smoothed with a normal kernel."""
from __future__ import annotations
import bisect, datetime as dt, math
from statistics import NormalDist

D = dt.timedelta
ND = NormalDist()


def _nth_weekday(y: int, m: int, wd: int, n: int) -> dt.date:
    d = dt.date(y, m, 1)
    d += D((wd - d.weekday()) % 7)
    return d + D(7 * (n - 1))


def _last_weekday(y: int, m: int, wd: int) -> dt.date:
    d = dt.date(y, m + 1, 1) - D(1)
    return d - D((d.weekday() - wd) % 7)


def _easter(y: int) -> dt.date:
    a = y % 19; b = y // 100; c = y % 100; d = b // 4; e = b % 4; f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451; mo = (h + l - 7 * m + 114) // 31; da = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(y, mo, da)


def holidays(y: int) -> dict[str, dt.date]:
    return {"mlk": _nth_weekday(y, 1, 0, 3), "pres": _nth_weekday(y, 2, 0, 3), "easter": _easter(y),
            "memorial": _last_weekday(y, 5, 0), "labor": _nth_weekday(y, 9, 0, 1), "columbus": _nth_weekday(y, 10, 0, 2),
            "thanks": _nth_weekday(y, 11, 3, 4)}


_HCACHE: dict[int, dict] = {}


def lag(d: dt.date, anchor: bool = True, win: int = 9) -> int:
    if not anchor:
        return 364
    best = None
    for y in (d.year, d.year + 1, d.year - 1):
        H = _HCACHE.setdefault(y, holidays(y)); Hp = _HCACHE.setdefault(y - 1, holidays(y - 1))
        for k, h in H.items():
            off = (d - h).days
            if abs(off) <= win and (best is None or abs(off) < best[0]):
                best = (abs(off), (h - Hp[k]).days)
    return best[1] if best else 364


class Model:
    def __init__(self, X: dict[dt.date, int], L: int = 14, anchor: bool = True, bw: float = 0.004, min_hist: int = 26):
        self.X, self.L, self.anchor, self.bw, self.min_hist = X, L, anchor, bw, min_hist
        self._res: dict[int, list[tuple[dt.date, float]]] = {}

    def base(self, d: dt.date) -> int:
        return self.X[d - D(lag(d, self.anchor))]

    def project(self, mon: dt.date, last: dt.date) -> tuple[float, float, int]:
        """(known sum of the target week, projected remaining sum, number of unknown days) given data through `last`."""
        known = sum(self.X[mon + D(i)] for i in range(7) if mon + D(i) <= last)
        unk = [mon + D(i) for i in range(7) if mon + D(i) > last]
        days = [last - D(j) for j in range(self.L)]
        g = sum(self.X[d] for d in days) / sum(self.base(d) for d in days)
        return known, sum(g * self.base(d) for d in unk), len(unk)

    def residuals(self, n_unk: int) -> list[tuple[dt.date, float]]:
        """(week Monday, log(actual remaining / projected)) for every complete week, info = n_unk days still unknown."""
        if n_unk not in self._res:
            out = []
            mon = dt.date(2023, 1, 2)
            while mon + D(6) in self.X:
                last = mon + D(6 - n_unk)
                try:
                    kn, rem, _ = self.project(mon, last)
                    act = sum(self.X[mon + D(i)] for i in range(7 - n_unk, 7))
                    out.append((mon, math.log(act / rem)))
                except KeyError:
                    pass
                mon += D(7)
            self._res[n_unk] = out
        return self._res[n_unk]

    def prob_above(self, mon: dt.date, last: dt.date, strikes: list[float], asof: dt.date) -> dict[float, float]:
        """P(weekly mean > strike) for each strike, using only weeks whose Sunday is before `asof`."""
        kn, rem, n_unk = self.project(mon, last)
        if n_unk == 0:
            avg = kn / 7
            return {s: 1.0 if avg > s else 0.0 for s in strikes}
        res = sorted(r for w, r in self.residuals(n_unk) if w + D(6) < asof)
        if len(res) < self.min_hist:
            return {}
        out = {}
        for s in strikes:
            need = 7 * s - kn
            if need <= 0:
                out[s] = 1.0; continue
            z = math.log(need / rem)       # YES iff residual > z
            out[s] = sum(1 - ND.cdf((z - r) / self.bw) for r in res) / len(res)
        return out

    def mean_pred(self, mon: dt.date, last: dt.date) -> float:
        kn, rem, _ = self.project(mon, last)
        return (kn + rem) / 7
