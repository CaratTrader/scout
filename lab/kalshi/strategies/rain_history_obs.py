"""METAR observation state for the rain_history researcher. Parses IEM ASOS routine+special reports of a settlement
station into per-climate-day report lists and answers 'what was known at time t' questions (never looks past t).
Climate day = local standard time midnight..midnight. A report is usable LAG minutes after its time."""
from __future__ import annotations
import csv, datetime as dt, io, re, zoneinfo
from functools import lru_cache
from pathlib import Path

MET = Path("data/kalshi_lab/strategies/rain_history/metar")
LAG = 5
TZ = {"ABQ": "America/Denver", "ATL": "America/New_York", "AUS": "America/Chicago", "BOS": "America/New_York", "ORD": "America/Chicago",
      "CLL": "America/Chicago", "CMH": "America/New_York", "DFW": "America/Chicago", "DCA": "America/New_York", "DEN": "America/Denver",
      "EWR": "America/New_York", "HOU": "America/Chicago", "LAX": "America/Los_Angeles", "LEX": "America/New_York", "LAS": "America/Los_Angeles",
      "MIA": "America/New_York", "MSP": "America/Chicago", "MKE": "America/Chicago", "MSY": "America/Chicago", "NYC": "America/New_York",
      "OKC": "America/Chicago", "PHL": "America/New_York", "PHX": "America/Phoenix", "PIT": "America/New_York", "PVD": "America/New_York",
      "SAT": "America/Chicago", "SEA": "America/Los_Angeles", "SFO": "America/Los_Angeles", "SGF": "America/Chicago", "TTN": "America/New_York"}
PRECIP_WX = re.compile(r"(?<![A-Z])(?:\+|-)?(?:TS|SH|FZ)?(?:RA|DZ|SN|PL|GR|GS|UP|SG|IC)")
CONV = re.compile(r"\b(?:TS\w*|VCTS|VCSH|\w*CB|\w*TCU|LTG\w*|VIRGA)\b")
BEGEND = re.compile(r"\b(?:RA|DZ|SN|PL|UP|TS|SH)\w*[BE]\d{2}")


def fnum(x: str) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def groups(raw: str) -> tuple[float | None, float | None]:
    """(hourly P group inches, 3/6-hour group inches); 0.0 = trace; None = absent."""
    if " RMK " not in raw:
        return None, None
    toks = raw.split(" RMK ", 1)[1].split()
    p = g6 = None
    for tok in toks:
        m = re.fullmatch(r"P(\d{4})", tok)
        if m:
            p = int(m.group(1)) / 100.0; continue
        m = re.fullmatch(r"6(\d{4})", tok)
        if m and g6 is None:
            g6 = int(m.group(1)) / 100.0
    return p, g6


@lru_cache(maxsize=None)
def reports(stn: str) -> list[dict]:
    rows = {}
    for f in sorted(MET.glob(f"{stn}_*.csv")):
        for r in csv.DictReader(io.StringIO(f.read_text())):
            raw = r.get("metar") or ""
            if not raw or raw == "M":
                continue
            t = int(dt.datetime.fromisoformat(r["valid"]).replace(tzinfo=dt.timezone.utc).timestamp())
            body = raw.split(" RMK ", 1)[0]; rmk = raw.split(" RMK ", 1)[1] if " RMK " in raw else ""
            wx = r.get("wxcodes") or ""; wx = "" if wx == "M" else wx
            p, g6 = groups(raw)
            wxs = wx.split()
            at_stn = any(PRECIP_WX.match(w) and not w.startswith("VC") for w in wxs)
            rows[(t, raw)] = {"ts": t, "tmpf": fnum(r.get("tmpf")), "dwpf": fnum(r.get("dwpf")), "alti": fnum(r.get("alti")), "wx": wx,
                              "precip_wx": at_stn, "p": p, "g6": g6, "begend": bool(BEGEND.search(rmk)),
                              "conv": bool(CONV.search(" " + wx + " " + body + " " + rmk + " ")), "raw": raw}
    return sorted(rows.values(), key=lambda x: x["ts"])


def day_bounds(stn: str, day: dt.date) -> tuple[int, int]:
    """UTC timestamps of the local-standard-time climate day."""
    tz = zoneinfo.ZoneInfo(TZ[stn])
    noon = dt.datetime.combine(day, dt.time(12), tzinfo=tz)
    std = noon.utcoffset() - (noon.dst() or dt.timedelta(0))
    start = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone(std))
    return int(start.timestamp()), int(start.timestamp()) + 86400


def local_ts(stn: str, day: dt.date, hour: int) -> int:
    return int(dt.datetime.combine(day, dt.time(hour), tzinfo=zoneinfo.ZoneInfo(TZ[stn])).timestamp())


def measurable(r: dict, d0: int) -> bool:
    """Report shows >= 0.01 in whose accumulation interval lies inside the climate day starting d0."""
    age = r["ts"] - d0
    if r["p"] is not None and r["p"] >= 0.01 and age >= 70 * 60:
        return True
    if r["g6"] is not None and r["g6"] >= 0.01:
        hr = (dt.datetime.fromtimestamp(r["ts"] + 600, dt.timezone.utc).hour) % 24
        span = 6 if hr % 6 == 0 else 3
        if age >= span * 3600 + 600:
            return True
    return False


def any_precip(r: dict, d0: int) -> bool:
    """Any precipitation at the station (trace counts): weather code at the station, a P/6 group (0000 = trace), or a began/ended remark."""
    if r["precip_wx"] or r["begend"]:
        return True
    return (r["p"] is not None and r["ts"] - d0 >= 70 * 60) or (r["g6"] is not None and measurable({**r, "p": None, "g6": max(r["g6"], 0.01)}, d0))


def day_reports(stn: str, day: dt.date) -> tuple[int, int, list[dict]]:
    d0, d1 = day_bounds(stn, day)
    return d0, d1, [r for r in reports(stn) if d0 <= r["ts"] < d1]


def first_time(stn: str, day: dt.date, kind: str) -> int | None:
    """Usable time (report time + LAG) of the first report of the climate day with measurable ('meas') or any ('any') precip."""
    d0, d1, rs = day_reports(stn, day)
    for r in rs:
        if (measurable(r, d0) if kind == "meas" else any_precip(r, d0)):
            return r["ts"] + LAG * 60
    return None


def state(stn: str, day: dt.date, t: int) -> dict:
    """Everything known at t about the climate day: usable reports only."""
    d0, d1, rs = day_reports(stn, day)
    us = [r for r in rs if r["ts"] + LAG * 60 <= t]
    last3 = [r for r in us if r["ts"] + LAG * 60 > t - 3 * 3600]
    last2 = [r for r in us if r["ts"] + LAG * 60 > t - 2 * 3600]
    last = us[-1] if us else None
    alt_then = None
    for r in us:
        if abs((r["ts"] + LAG * 60) - (t - 3 * 3600)) <= 40 * 60:
            alt_then = r["alti"]
    return {
        "n": len(us), "meas": any(measurable(r, d0) for r in us), "any": any(any_precip(r, d0) for r in us),
        "n3": len(last3), "unsettled3": any(r["precip_wx"] or r["conv"] or r["begend"] or r["p"] is not None for r in last3),
        "n2": len(last2), "wet2": any(r["precip_wx"] or r["begend"] or r["p"] is not None for r in last2),
        "dd": (last["tmpf"] - last["dwpf"]) if last and last["tmpf"] is not None and last["dwpf"] is not None else None,
        "dp3": (last["alti"] - alt_then) if last and last["alti"] is not None and alt_then is not None else None,
        "last_age": (t - last["ts"]) if last else None,
    }
