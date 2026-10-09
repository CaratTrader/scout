"""r6_rule_boundary_climate_day: data layer (zero Kalshi calls).

ASOS rows (data/lab/us/asos_raw/<STN>.csv, IEM, `valid` is local wall-clock time) are parsed into observations with an
exact UTC timestamp taken from the METAR's DDHHMMZ group. Each observation is assigned
  - its climate day: the date in local STANDARD time (UTC + standard offset), which is what the NWS CLI uses, and
  - its calendar day: the local wall-clock date (the naive "today" of a weather app).
They differ only for readings in the 00:00-00:59 wall-clock hour while daylight saving time is in force: that hour
belongs to the PREVIOUS climate day.

Kalshi daily-high markets (data/lab/us/kalshi/markets.json + one 1-minute candle JSON per ticker, the last 14 h before
close, i.e. ~11:00 local daylight time to the 01:00 close) give the strikes, the result and expiration_value (the CLI
max the market settled on).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import re
import zoneinfo
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ASOS = ROOT / "data/lab/us/asos_raw"
KD = ROOT / "data/lab/us/kalshi"
OUT = ROOT / "data/kalshi_lab/strategies/r6_rule_boundary_climate_day"

SERIES = {"KXHIGHNY": ("NYC", "America/New_York"), "KXHIGHCHI": ("MDW", "America/Chicago"), "KXHIGHMIA": ("MIA", "America/New_York"),
          "KXHIGHLAX": ("LAX", "America/Los_Angeles"), "KXHIGHTSFO": ("SFO", "America/Los_Angeles"), "KXHIGHTBOS": ("BOS", "America/New_York"),
          "KXHIGHTDC": ("DCA", "America/New_York"), "KXHIGHPHIL": ("PHL", "America/New_York"), "KXHIGHTATL": ("ATL", "America/New_York"),
          "KXHIGHDEN": ("DEN", "America/Denver"), "KXHIGHAUS": ("AUS", "America/Chicago"), "KXHIGHTDAL": ("DFW", "America/Chicago"),
          "KXHIGHTMIN": ("MSP", "America/Chicago"), "KXHIGHTPHX": ("PHX", "America/Phoenix"), "KXHIGHTSEA": ("SEA", "America/Los_Angeles"),
          "KXHIGHTLV": ("LAS", "America/Los_Angeles"), "KXHIGHTSAN": ("SAN", "America/Los_Angeles"),
          "KXHIGHTHOU": ("HOU", "America/Chicago"), "KXHIGHTOKC": ("OKC", "America/Chicago"),
          "KXHIGHTSATX": ("SAT", "America/Chicago"), "KXHIGHTNOLA": ("MSY", "America/Chicago"),
          "KXHIGHTEWR": ("EWR", "America/New_York"), "KXHIGHTTTN": ("TTN", "America/New_York"),
          "KXHIGHTSDF": ("SDF", "America/Kentucky/Louisville")}
STD_OFF_H = {"America/New_York": -5, "America/Chicago": -6, "America/Denver": -7, "America/Los_Angeles": -8,
             "America/Phoenix": -7, "America/Kentucky/Louisville": -5}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def rnd(x: float) -> int:
    return math.floor(x + 0.5)


def c2f(c: float) -> float:
    return c * 9 / 5 + 32


def _utc_from_metar(local_naive: dt.datetime, raw: str) -> int | None:
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", raw)
    if not m:
        return None
    dd, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
    for k in (0, 1, -1):
        d = (local_naive + dt.timedelta(days=k)).date()
        if d.day == dd:
            u = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=dt.timezone.utc)
            off = (local_naive.replace(tzinfo=dt.timezone.utc) - u).total_seconds() / 3600
            if -11 <= off <= -3:   # US zones are UTC-4..-10
                return int(u.timestamp())
    return None


def _remarks(raw: str) -> list[str]:
    if " RMK " not in f" {raw} ":
        return []
    return raw.split("RMK", 1)[1].split()


def parse_metar(raw: str) -> dict:
    """{'t': current F (T-group tenths, else whole-C group), 'x6': 6-h max F, 'x24': 24-h max F, 'whole': bool}."""
    out = {"t": None, "x6": None, "x24": None, "whole": False}
    m = re.search(r"\bT([01])(\d{3})[01]\d{3}\b", raw)
    if m:
        out["t"] = c2f(int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1))
    else:
        m = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", raw)
        if m:
            out["t"] = c2f(float(m.group(1).replace("M", "-"))); out["whole"] = True
    toks = _remarks(raw); skip = 0
    for i, tok in enumerate(toks):
        if skip:
            skip -= 1; continue
        if tok == "PK" and i + 1 < len(toks) and toks[i + 1] == "WND":
            skip = 2; continue
        g = re.fullmatch(r"1([01])(\d{3})", tok)
        if g and out["x6"] is None:
            out["x6"] = c2f(int(g.group(2)) / 10.0 * (-1 if g.group(1) == "1" else 1))
        g = re.fullmatch(r"4([01])(\d{3})([01])(\d{3})", tok)
        if g and out["x24"] is None:
            out["x24"] = c2f(int(g.group(2)) / 10.0 * (-1 if g.group(1) == "1" else 1))
    return out


@lru_cache(maxsize=None)
def station_obs(stn: str, tzname: str) -> list[dict]:
    """Sorted observations: {'u': utc ts, 'clim': climate date (LST), 'cal': wall-clock date, 'wmin': wall minute of
    day, 'lstmin': LST minute of day, 'f': F, 'kind': 'hf5' (5-minute, whole C) | 'metar', 'x6', 'x24'}."""
    f = ASOS / f"{stn}.csv"
    if not f.exists():
        return []
    tz = zoneinfo.ZoneInfo(tzname); std = dt.timedelta(hours=STD_OFF_H[tzname])
    rows = []
    for r in csv.DictReader(open(f)):
        raw = r.get("metar") or ""
        try:
            ln = dt.datetime.strptime(r["valid"][:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        u = _utc_from_metar(ln, raw)
        if u is None:
            continue
        p = parse_metar(raw)
        hf = "MADISHF" in raw
        if not hf and r.get("tmpf") not in (None, "", "M") and p["t"] is None:
            p["t"] = float(r["tmpf"])
        if p["t"] is None and p["x6"] is None and p["x24"] is None:
            continue
        ud = dt.datetime.fromtimestamp(u, dt.timezone.utc)
        lst = (ud + std).replace(tzinfo=None); wall = ud.astimezone(tz).replace(tzinfo=None)
        rows.append({"u": u, "clim": lst.date().isoformat(), "cal": wall.date().isoformat(), "wmin": wall.hour * 60 + wall.minute,
                     "lstmin": lst.hour * 60 + lst.minute, "f": p["t"], "kind": "hf5" if hf else "metar",
                     "whole": hf or p["whole"], "x6": None if hf else p["x6"], "x24": None if hf else p["x24"]})
    rows.sort(key=lambda x: x["u"])
    # de-duplicate identical timestamps (IEM sometimes repeats a row)
    out, seen = [], set()
    for x in rows:
        k = (x["u"], x["kind"])
        if k not in seen:
            seen.add(k); out.append(x)
    return out


def is_dst(tzname: str, day: str) -> bool:
    if tzname == "America/Phoenix":
        return False
    d = dt.date.fromisoformat(day)
    return bool(zoneinfo.ZoneInfo(tzname).dst(dt.datetime(d.year, d.month, d.day, 12)))


def event_date(event_ticker: str) -> str:
    s = event_ticker.split("-")[1]   # e.g. 26SEP26
    return dt.date(2000 + int(s[:2]), MON[s[2:5]], int(s[5:])).isoformat()


@lru_cache(maxsize=1)
def markets() -> dict:
    return json.loads((KD / "markets.json").read_text())


def interval(m: dict) -> tuple[float, float]:
    """Integer-degree range of the CLI max for which the market settles YES."""
    t = m.get("strike_type")
    if t == "less":
        return -1e9, float(m["cap_strike"]) - 1
    if t == "greater":
        return float(m["floor_strike"]) + 1, 1e9
    return float(m["floor_strike"]), float(m["cap_strike"])


@lru_cache(maxsize=1)
def events() -> dict:
    """event_ticker -> {'series', 'stn', 'tz', 'day', 'close' (ts), 'cli' (float|None), 'mk': [market dicts]}."""
    ev: dict = {}
    for m in markets().values():
        s = m["series"]
        if s not in SERIES:
            continue
        e = ev.setdefault(m["event_ticker"], {"series": s, "stn": SERIES[s][0], "tz": SERIES[s][1], "day": event_date(m["event_ticker"]),
                                              "close": None, "cli": None, "mk": []})
        e["mk"].append(m)
        c = int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        e["close"] = c if e["close"] is None else max(e["close"], c)
        if m.get("expiration_value") not in (None, ""):
            try:
                e["cli"] = float(m["expiration_value"])
            except ValueError:
                pass
    for e in ev.values():
        e["mk"].sort(key=lambda m: interval(m)[0])
    return ev


def bracket_of(e: dict, v: float | None) -> str | None:
    if v is None:
        return None
    x = rnd(v)
    for m in e["mk"]:
        lo, hi = interval(m)
        if lo <= x <= hi:
            return m["ticker"]
    return None


def yes_ticker(e: dict) -> str | None:
    ys = [m["ticker"] for m in e["mk"] if m.get("result") == "yes"]
    return ys[0] if len(ys) == 1 else None


@lru_cache(maxsize=None)
def candles(ticker: str) -> list[tuple[int, float, float, float]]:
    """[(ts, yes_ask, yes_bid, volume)] 1-minute candles (end_period_ts), only minutes with activity."""
    f = KD / f"{ticker}.json"
    if not f.exists():
        return []
    out = []
    for c in json.loads(f.read_text()):
        try:
            a = float(c["yes_ask"]["close_dollars"]); b = float(c["yes_bid"]["close_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((int(c["end_period_ts"]), a, b, float(c.get("volume_fp") or 0)))
    out.sort()
    return out


def quote(ticker: str, t: int, max_age_s: int = 1800) -> tuple[float, float] | None:
    """(yes_ask, yes_bid) from the last candle with end_period_ts <= t, if not older than max_age_s."""
    best = None
    for r in candles(ticker):
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > max_age_s:
        return None
    return best[1], best[2]


def fee_order(p: float, n: int = 1) -> float:
    """Kalshi taker fee for an n-contract order, rounded up to the cent, per contract."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n
