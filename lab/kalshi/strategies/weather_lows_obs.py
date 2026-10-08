"""weather_lows observations: IEM ASOS rows (hourly/special METAR with T-group, 5-minute HFMETAR rows in whole C,
6-hour minimum remark 2sTTT) per station, grouped by NWS climate day (midnight-midnight local STANDARD time).

For a decision instant t (UTC seconds) state(day_obs, t, lag) uses only rows with valid time <= t - lag and returns
  U   : upper bound (whole F) on the official daily minimum so far (official min <= U): min over hourly T-group readings
        (exact whole F, ASOS stores whole F), 5-minute rows (largest whole F consistent with the whole-C reading) and
        6-hour minimum groups whose window lies inside the climate day;
  L6  : the 6-hour-group minimum (exact) where available (None otherwise);
  cur : latest reading (F, hourly preferred if within 15 min else 5-minute midpoint);
  tmin: UTC time of the reading that set U."""
from __future__ import annotations
import csv, datetime as dt, math, re, zoneinfo
from collections import defaultdict
from pathlib import Path

RAW = Path("data/lab/us/asos_raw")
STD_OFF = {"America/New_York": -5, "America/Chicago": -6, "America/Denver": -7, "America/Phoenix": -7,
           "America/Los_Angeles": -8, "America/Kentucky/Louisville": -5}


def rnd(x: float) -> int:
    return math.floor(x + 0.5)


def c2f(c: float) -> float:
    return c * 9 / 5 + 32


def whole_f_for_c(c: int) -> list[int]:
    """whole-F readings that the ASOS would report as whole C = c (C = round((F-32)*5/9), half away from zero)."""
    out = []
    for f in range(int(c2f(c) - 3), int(c2f(c) + 4)):
        x = (f - 32) * 5 / 9
        r = math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)
        if r == c:
            out.append(f)
    return out


def load(stn: str, tzname: str) -> dict[str, list[tuple]]:
    """climate day (YYYY-MM-DD, LST) -> sorted [(utc_ts, kind, value)], kind 'H' (whole F), '5' ((lo F, hi F) whole),
    '6' (exact whole F of the 6-hour minimum ending at utc_ts)."""
    tz = zoneinfo.ZoneInfo(tzname); off = STD_OFF[tzname] * 3600
    out = defaultdict(list)
    f = RAW / f"{stn}.csv"
    if not f.exists():
        return out
    for r in csv.DictReader(open(f)):
        raw = r.get("metar") or ""
        try:
            lt = dt.datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except Exception:
            continue
        u = int(lt.timestamp()); day = dt.datetime.utcfromtimestamp(u + off).strftime("%Y-%m-%d")
        if "MADISHF" in raw:
            m = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", raw)
            if not m:
                continue
            c = int(m.group(1).replace("M", "-"))
            fs = whole_f_for_c(c)
            if fs:
                out[day].append((u, "5", (min(fs), max(fs))))
            continue
        m = re.search(r"\bT([01])(\d{3})[01]\d{3}\b", raw)
        if m:
            c = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1)
            out[day].append((u, "H", rnd(c2f(c))))
        elif r.get("tmpf") not in (None, "", "M"):
            out[day].append((u, "H", rnd(float(r["tmpf"]))))
        else:
            continue
        if " RMK " in raw:
            toks = raw.split(" RMK ", 1)[1].split()
            for i, tok in enumerate(toks):
                if i > 0 and toks[i - 1] in ("WND", "WSHFT"):
                    continue
                g = re.fullmatch(r"2([01])(\d{3})", tok)
                if g:
                    c = int(g.group(2)) / 10.0 * (-1 if g.group(1) == "1" else 1)
                    out[day].append((u, "6", rnd(c2f(c))))
                    break
    for d in out:
        out[d].sort(key=lambda x: (x[0], x[1]))
    return out


def day_bounds(day: str, tzname: str) -> tuple[int, int]:
    off = STD_OFF[tzname] * 3600
    d0 = int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()) - off
    return d0, d0 + 86400


def state(obs: list[tuple], t: int, day_start: int, lag: int = 600) -> dict | None:
    """Running state from rows with utc <= t - lag (publication latency)."""
    U = None; tmin = None; L6 = None; cur = None; cur_t = None; nH = n5 = 0
    first_h = next((v for u, k, v in obs if k == "H" and u >= day_start), None)
    for u, k, v in obs:
        if u > t - lag:
            break
        if k == "H":
            x = v; nH += 1
        elif k == "5":
            x = v[1]; n5 += 1
        else:
            if u - 6 * 3600 < day_start - 15 * 60:   # window reaches into the previous climate day
                continue
            if u - 6 * 3600 < day_start and not (first_h is not None and v <= first_h - 1):
                continue   # <= 15 min overlap: accept only if the minimum is below the first in-day reading
            L6 = v if L6 is None else min(L6, v); x = v
        if U is None or x < U:
            U, tmin = x, u
        if k in ("H", "5"):
            cv = v if k == "H" else (v[0] + v[1]) / 2
            cur, cur_t = cv, u
    if U is None:
        return None
    return {"U": U, "tmin": tmin, "L6": L6, "cur": cur, "cur_t": cur_t, "nH": nH, "n5": n5}
