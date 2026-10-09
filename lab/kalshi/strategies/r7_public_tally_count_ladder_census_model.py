"""Count models for the r7 forward shadow (stdlib only). Parameters are fitted on the PUBLIC TALLY history only (no Kalshi
prices or outcomes) by `fit()` and frozen to frozen_model.json before any forward data.

Weekly ladders (KXEOWEEK executive orders by Federal Register signing date, KXTRUMPNOMNUM nominations by White House
publication date; week = Sun 00:00 ET .. Sat 23:59 ET) are compound: BATCHES (EO signing days / nomination posts) arrive
as an over-dispersed count and each batch carries an iid size from the empirical batch-size distribution:
    weekly batch level Lambda ~ Gamma(shape phi, rate phi / lam)   (NB(lam, phi) marginal batches/week, MLE on 52 weeks)
    batches over a share f of the week's (day-of-week x hour-of-day) mass ~ Poisson(Lambda f)
    b batches seen over elapsed share f_seen -> remaining batches ~ NB(size phi + b, mean (phi + b) f_rem / (phi/lam + f_seen))
    (the conjugate update; it is the r6 Truth Social level form L = (c + a) / (E + a) with a = phi), R = sum of their sizes.
    KXEOWEEK adds a tally-error term (FR minus White House count per week, from the 52-week audit), floored at the FR count.
Monthly ladder (KXTORNADO, SPC preliminary rough-log count by UTC calendar month):
    convective day (12Z..12Z) level Lambda_d ~ Gamma(phi_m, phi_m / mu_m), mu_m / phi_m fitted per calendar month on
    2015-2025 SPC daily counts; within-day timing by the UTC hour profile. Remaining to the month end = rest of the current
    convective day (gamma-updated on that day's count so far) + later days (prior), convolved.
Final F = seen + R. Strike P(YES) from the Kalshi strike_type (greater: F > floor; greater_or_equal: F >= floor;
between: floor <= F <= cap; less: F < cap; less_or_equal: F <= cap; binary 'any': F >= 1)."""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics as st
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
KMAX = 800


# ---------------------------------------------------------------- negative binomial
def nb_logpmf(k: int, mu: float, phi: float) -> float:
    mu = max(mu, 1e-9); phi = max(phi, 1e-9)
    return (math.lgamma(k + phi) - math.lgamma(phi) - math.lgamma(k + 1) + phi * math.log(phi / (phi + mu)) + k * math.log(mu / (phi + mu)))


def nb_pmf(mu: float, phi: float, kmax: int = KMAX) -> list[float]:
    if mu <= 1e-9:
        return [1.0] + [0.0] * kmax
    out = [math.exp(nb_logpmf(k, mu, phi)) for k in range(kmax + 1)]
    s = sum(out)
    out[-1] += max(0.0, 1.0 - s)          # tail mass into the last cell
    return out


def convolve(a: list[float], b: list[float], kmax: int = KMAX) -> list[float]:
    out = [0.0] * (kmax + 1)
    nz_b = [(j, q) for j, q in enumerate(b) if q > 1e-15]
    for i, p in enumerate(a):
        if p <= 1e-15:
            continue
        for j, q in nz_b:
            if i + j > kmax:
                out[kmax] += p * q
            else:
                out[i + j] += p * q
    return out


def fit_nb(xs: list[int]) -> dict:
    """MLE: mean = sample mean; phi by golden-section on the profile log-likelihood over log(phi) in [-4, 7]."""
    mu = st.mean(xs)
    var = st.pvariance(xs)
    if mu <= 0:
        return {"mu": 0.0, "phi": 1000.0, "var": var, "n": len(xs), "ll": 0.0}
    f = lambda lp: sum(nb_logpmf(x, mu, math.exp(lp)) for x in xs)  # noqa: E731
    lo, hi = -4.0, 7.0; g = (math.sqrt(5) - 1) / 2
    c, d = hi - g * (hi - lo), lo + g * (hi - lo)
    for _ in range(80):
        if f(c) > f(d):
            hi = d
        else:
            lo = c
        c, d = hi - g * (hi - lo), lo + g * (hi - lo)
    lp = (lo + hi) / 2
    return {"mu": mu, "phi": min(math.exp(lp), 1000.0), "var": var, "n": len(xs), "ll": f(lp) / len(xs)}


# ---------------------------------------------------------------- strikes
def p_yes(pmf_final: list[float], stype: str | None, floor, cap) -> float | None:
    """P(YES) for one Kalshi market given the pmf of the final count."""
    def P(pred):
        return sum(p for k, p in enumerate(pmf_final) if pred(k))
    if stype == "greater" and floor is not None:
        return P(lambda k: k > floor)
    if stype == "greater_or_equal" and floor is not None:
        return P(lambda k: k >= floor)
    if stype == "between" and floor is not None and cap is not None:
        return P(lambda k: floor <= k <= cap)
    if stype == "less" and cap is not None:
        return P(lambda k: k < cap)
    if stype == "less_or_equal" and cap is not None:
        return P(lambda k: k <= cap)
    if stype == "binary":
        return P(lambda k: k >= 1)
    return None


def strike_of(m: dict) -> tuple[str | None, float | None, float | None]:
    """(strike_type, floor, cap) from the market fields, falling back to the rules text ('above N', 'at least N',
    'exactly N'); 'any' binary markets -> ('binary', None, None)."""
    import re
    stype, fl, cap = m.get("strike_type"), m.get("floor_strike"), m.get("cap_strike")
    if stype in ("greater", "greater_or_equal") and fl is not None:
        return stype, float(fl), None
    if stype in ("less", "less_or_equal") and cap is not None:
        return stype, None, float(cap)
    if stype == "between" and fl is not None and cap is not None:
        return stype, float(fl), float(cap)
    r = (m.get("rules_primary") or "").lower()
    for pat, st_ in ((r"\babove (\d+(?:\.\d+)?)", "greater"), (r"\bat least (\d+(?:\.\d+)?)", "greater_or_equal"),
                     (r"\bexactly (\d+(?:\.\d+)?)", "between")):
        mm = re.search(pat, r)
        if mm:
            v = float(mm.group(1))
            return (st_, v, v) if st_ == "between" else (st_, v, None)
    if re.search(r"\bany\b", r):
        return "binary", None, None
    return None, None, None


def shift(pmf: list[float], c: int, kmax: int = KMAX) -> list[float]:
    out = [0.0] * (kmax + 1)
    for k, p in enumerate(pmf):
        out[min(k + c, kmax)] += p
    return out


# ---------------------------------------------------------------- weekly mass profile (ET day-of-week x hour)
def week_start(t: int) -> dt.datetime:
    """Sun 00:00 ET of the week containing t (ET)."""
    lt = dt.datetime.fromtimestamp(t, ET)
    sun = (lt - dt.timedelta(days=(lt.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    return sun


class WeekProfile:
    """w[d][h]: share of the weekly count in ET day-of-week d (0 = Sunday) and ET hour h; sum = 1."""

    def __init__(self, dow: list[float], hod: list[float]):
        sd, sh = sum(dow), sum(hod)
        self.dow = [x / sd for x in dow]; self.hod = [x / sh for x in hod]

    def share(self, A_local: dt.datetime, t0: int, t1: int) -> float:
        """Mass share between t0 and t1 (unix), clipped to the week [A, A + 7 d) in local ET wall-clock hours."""
        B_local = A_local + dt.timedelta(days=7)
        a = max(dt.datetime.fromtimestamp(t0, ET), A_local); b = min(dt.datetime.fromtimestamp(t1, ET), B_local)
        if b <= a:
            return 0.0
        tot = 0.0; cur = a
        while cur < b:
            nxt = min((cur.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)), b)
            d = (cur.date() - A_local.date()).days
            if 0 <= d < 7:
                tot += self.dow[d] * self.hod[cur.hour] * (nxt - cur).total_seconds() / 3600.0
            cur = nxt
        return tot

    def export(self) -> dict:
        return {"dow_share_from_Sunday": self.dow, "hour_share_ET": self.hod}


def weekly_remaining(c_seen: int, f_seen: float, f_rem: float, lam: float, phi: float) -> list[float]:
    if f_rem <= 1e-9:
        return [1.0] + [0.0] * KMAX
    size = phi + c_seen; rate = phi / max(lam, 1e-9) + f_seen
    return nb_pmf(size * f_rem / rate, size)


def compound(p_batches: list[float], batch_pmf: list[float], pmax: int = 12) -> list[float]:
    """pmf of R = S_1 + ... + S_P, P ~ p_batches (index = number of batches), S_j iid ~ batch_pmf (index = size)."""
    out = [0.0] * (KMAX + 1); cur = [1.0] + [0.0] * KMAX
    for p, w in enumerate(p_batches[:pmax + 1]):
        if p > 0:
            cur = convolve(cur, batch_pmf)
        if w > 1e-12:
            for k, q in enumerate(cur):
                out[k] += w * q
    tail = 1.0 - sum(out)
    if tail > 0:
        out[-1] += tail
    return out


def apply_kernel(pmf: list[float], kernel: dict, floor: int = 0) -> list[float]:
    """Add a tally-error term E (kernel {offset: prob}) and move any mass below `floor` to `floor`."""
    out = [0.0] * (KMAX + 1)
    for k, p in enumerate(pmf):
        if p <= 0:
            continue
        for off, q in kernel.items():
            j = min(max(k + int(off), floor, 0), KMAX)
            out[j] += p * q
    return out


# ---------------------------------------------------------------- tornado (SPC)
def conv_day_start(t: int) -> dt.datetime:
    u = dt.datetime.fromtimestamp(t, UTC)
    s = u.replace(hour=12, minute=0, second=0, microsecond=0)
    return s if u >= s else s - dt.timedelta(days=1)


def hour_share_utc(hod: list[float], t0: dt.datetime, t1: dt.datetime) -> float:
    tot = 0.0; cur = t0
    while cur < t1:
        nxt = min(cur.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1), t1)
        tot += hod[cur.hour] * (nxt - cur).total_seconds() / 3600.0
        cur = nxt
    return tot


def tornado_remaining(t: int, W_end: int, c_today: int, month_par: dict, hod: list[float]) -> list[float]:
    """Remaining preliminary tornado reports in (t, W_end] (W_end = 00Z on the 1st). month_par: {mu, phi} per convective
    day for the month of each convective day (keyed by month number as str); hod: UTC hour shares (sum 1)."""
    if t >= W_end:
        return [1.0] + [0.0] * KMAX
    pmf = [1.0] + [0.0] * KMAX
    s = conv_day_start(t); end = dt.datetime.fromtimestamp(W_end, UTC); now = dt.datetime.fromtimestamp(t, UTC)
    first = True
    while s < end:
        e = s + dt.timedelta(days=1)
        par = month_par[str(s.month)]
        a = max(now, s); b = min(e, end)
        f_rem = hour_share_utc(hod, a, b)
        if f_rem > 1e-9:
            if first:
                f_seen = hour_share_utc(hod, s, now)
                size = par["phi"] + c_today; rate = par["phi"] / max(par["mu"], 1e-9) + f_seen
                pmf = convolve(pmf, nb_pmf(size * f_rem / rate, size))
            else:
                pmf = convolve(pmf, nb_pmf(par["mu"] * f_rem, par["phi"]))
        first = False
        s = e
    return pmf


# ---------------------------------------------------------------- frozen container
class Frozen:
    def __init__(self, d: dict):
        self.d = d
        self.eo = d["KXEOWEEK"]; self.nom = d["KXTRUMPNOMNUM"]; self.tor = d["KXTORNADO"]
        self.eo_prof = WeekProfile(self.eo["profile"]["dow_share_from_Sunday"], self.eo["profile"]["hour_share_ET"])
        self.nom_prof = WeekProfile(self.nom["profile"]["dow_share_from_Sunday"], self.nom["profile"]["hour_share_ET"])

    @classmethod
    def load(cls, path: Path) -> "Frozen":
        return cls(json.loads(path.read_text()))

    def weekly_pmf(self, series: str, A: int, B: int, t: int, c_seen: int, b_seen: int, fr_floor: int = 0) -> tuple[list[float], dict]:
        """Compound weekly model. c_seen = count seen so far (EOs / nominations); b_seen = batches seen so far (EO signing
        days / nomination posts). Remaining batches ~ gamma-updated NB on b_seen; batch sizes ~ empirical batch_pmf.
        KXEOWEEK adds the White House vs Federal Register tally-error kernel, floored at the FR count (FR is the settlement)."""
        par = self.eo if series == "KXEOWEEK" else self.nom
        prof = self.eo_prof if series == "KXEOWEEK" else self.nom_prof
        A_local = dt.datetime.fromtimestamp(A, ET)
        f_seen = prof.share(A_local, A, min(t, B)); f_rem = prof.share(A_local, min(t, B), B)
        pb = weekly_remaining(b_seen, f_seen, f_rem, par["lam_batches"], par["phi_batches"])
        rem = compound(pb, par["batch_pmf"])
        final = shift(rem, c_seen)
        if series == "KXEOWEEK":
            final = apply_kernel(final, {int(k): v for k, v in par["tally_error_kernel"].items()}, floor=fr_floor)
        mean_rem = sum(k * p for k, p in enumerate(rem)); mean_b = sum(k * p for k, p in enumerate(pb))
        return final, {"f_seen": round(f_seen, 4), "f_rem": round(f_rem, 4), "mu_batches_remaining": round(mean_b, 3),
                       "mu_remaining": round(mean_rem, 3)}

    def tornado_pmf(self, t: int, W_end: int, c_month: int, c_today: int) -> tuple[list[float], dict]:
        rem = tornado_remaining(t, W_end, c_today, self.tor["month_par"], self.tor["hour_share_utc"])
        mean_rem = sum(k * p for k, p in enumerate(rem))
        return shift(rem, c_month), {"mu_remaining": round(mean_rem, 3), "c_today": c_today}
