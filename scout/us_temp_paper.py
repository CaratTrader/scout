"""Paper trader for Polymarket US daily-high-temperature ladders (observed-max lock, US-legal venue).

Rules (see docs/US_RESEARCH.md 2b, backtested on 553 city-days):
  R0  a bucket whose top is below the observed running max is dead -> buy NO; the open top bucket is certain once
      the running max reaches its floor -> buy YES. Certain modulo sensor errors (46/46 in the backtest).
  R2  after the afternoon peak (>= PEAK_HOUR local, temperature >= PEAK_FALL below the max for >= PEAK_MIN_SINCE
      minutes) buckets whose floor is >= max + R2_MARGIN are faded (buy NO) while their YES bid is still >= R2_MIN_BID.
      Margin 3F covers the gap between hourly METAR and the official report (0/1/2F on 35/55/10% of days).
  R1  (off by default) buy the bucket that contains the max and max+1 after the peak.
Observed max = today's METAR temperatures (T-group tenths, plus the 6-hourly maximum remark group) from 01:00 local,
because the NWS climate day is midnight-to-midnight local standard time. Paper fills use the catchable rule: a quote
must be present on two consecutive polls; the fill takes the later price and no more than the displayed size.
Paper only: no API keys, reads public endpoints. Settlement from /markets/{slug}/settlement.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zoneinfo
from pathlib import Path
from typing import Any

US = "https://gateway.polymarket.us/v1"
AWC = "https://aviationweather.gov/api/data/metar"
CITIES = {"sfo": ("KSFO", "America/Los_Angeles"), "lax": ("KLAX", "America/Los_Angeles"), "mdw": ("KMDW", "America/Chicago"),
          "nyc": ("KNYC", "America/New_York"), "mia": ("KMIA", "America/New_York")}
LEDGER = Path(os.getenv("USTEMP_LEDGER") or "data/ledger_us_temp.json")
JOURNAL = Path(os.getenv("USTEMP_JOURNAL") or "data/us_temp_journal.jsonl")
STATE = Path(os.getenv("USTEMP_STATE") or "data/us_temp_state.json")  # decision snapshot for the detail dashboard
SNAPS = Path(os.getenv("USTEMP_SNAPS") or "data/us_temp_snapshots.jsonl")  # one compact line per city per poll (post-mortems)
_SEEN: set[str] = set()
FEE = 0.0695


def env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


CFG = {
    "poll_s": env_f("USTEMP_POLL_S", 60), "stake": env_f("USTEMP_STAKE_USD", 25), "cash": env_f("USTEMP_CASH", 500),
    "r0_max_ask": env_f("USTEMP_MAX_ASK_R0", 0.97), "r2_margin": env_f("USTEMP_R2_MARGIN", 3), "r2_min_bid": env_f("USTEMP_R2_MIN_BID", 0.15),
    "peak_hour": env_f("USTEMP_PEAK_HOUR", 15), "peak_fall": env_f("USTEMP_PEAK_FALL", 1), "peak_min_since": env_f("USTEMP_PEAK_MIN_SINCE", 45),
    "confirm": int(env_f("USTEMP_CONFIRM_POLLS", 2)), "r1": int(env_f("USTEMP_R1", 0)), "r1_max_ask": env_f("USTEMP_R1_MAX_ASK", 0.80),
    "r1x": int(env_f("USTEMP_R1X", 1)), "r1x_max_ask": env_f("USTEMP_R1X_MAX_ASK", 0.90), "r1x_min_bid": env_f("USTEMP_R1X_MIN_BID", 0.10),
}
Z00_LOCAL_HOUR = {"sfo": 17, "lax": 17, "sea": 17, "las": 17, "san": 17, "phx": 17, "den": 18, "mdw": 19, "aus": 19, "dfw": 19, "msp": 19,
                  "nyc": 20, "mia": 20, "bos": 20, "dca": 20, "phl": 20, "atl": 20}  # local hour of the 00Z report (daylight saving; Phoenix has none)
NWS = "https://api.weather.gov/products"
CFG["r2"] = int(env_f("USTEMP_R2", 1))                    # fade rule on/off
CFG["r1x_00z"] = int(env_f("USTEMP_R1X_00Z", 1))          # let the 00Z METAR maximum open the R1x window (off on Kalshi: no edge by then)
CFG["cli_obs"] = int(env_f("USTEMP_CLI_OBS", 1))          # use the NWS intraday climate report's "today maximum" as a trusted observation
CFG["cli_trigger"] = int(env_f("USTEMP_CLI_TRIGGER", 0))  # let it open the R1x window before 00Z when the peak has passed (off until backtested)
CFG["cli_fall"] = env_f("USTEMP_CLI_FALL", 2); CFG["cli_min_since"] = env_f("USTEMP_CLI_MIN_SINCE", 60)
# cities whose office issues an afternoon (16:00-17:00 local) climate report; Denver, Austin and Phoenix only issue the
# previous day's final at 06:00-07:00, and SF / LA issue after 00Z, so the report cannot open an early window there
CFG["cli_cities"] = set((os.getenv("USTEMP_CLI_CITIES") or "nyc,mia,mdw,dca,phl,bos,atl,dfw,msp").split(","))
# off-box dead-man switch: a healthchecks.io-style URL pinged after every completed poll (empty = disabled). The
# monitor, not this machine, raises the alarm when pings stop - an alert sent from here cannot fire while offline.
CFG["heartbeat_url"] = (os.getenv("USTEMP_HEARTBEAT_URL") or "").strip()
_MON = {m: i for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}
_CLI_CACHE: dict[str, tuple[float, dict[str, Any] | None]] = {}


_NET: dict[str, dict[str, Any]] = {}   # per host: {"fails", "first_fail", "down_since", "suppressed", "first_err"}
OUTAGE_AFTER = 3                        # consecutive connectivity failures on one host before it counts as an outage


def is_network_error(exc: BaseException) -> bool:
    """Connectivity failures (no route, DNS, refused, timeouts, captive-portal resets), as opposed to an HTTP error answer."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, ConnectionError, OSError))


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or url[:40]
    except Exception:
        return url[:40]


def net_down(url: str, exc: BaseException) -> None:
    """Per host: isolated failures are ordinary errors; after OUTAGE_AFTER consecutive failures one outage_start is
    journaled and further failures are only counted (the 15.6 h captive-portal outage of 2026-09-27/28 had written
    9,136 identical error lines)."""
    h = _host(url); st = _NET.setdefault(h, {"fails": 0, "first_fail": 0.0, "down_since": None, "suppressed": 0, "first_err": ""})
    st["fails"] += 1
    if st["fails"] == 1:
        st["first_fail"] = time.time(); st["first_err"] = str(exc)[:120]
    if st["down_since"] is not None:
        st["suppressed"] += 1
    elif st["fails"] >= OUTAGE_AFTER:
        st["down_since"] = st["first_fail"]; st["suppressed"] = 0
        journal({"event": "outage_start", "host": h, "since": st["first_fail"], "consecutive_failures": st["fails"], "err": st["first_err"]})
    else:
        journal({"event": "error", "url": url[-80:], "err": str(exc)[:120]})


def net_up(url: str) -> None:
    h = _host(url); st = _NET.get(h)
    if not st:
        return
    if st["down_since"] is not None:
        journal({"event": "outage_end", "host": h, "seconds": round(time.time() - st["down_since"]), "suppressed_errors": st["suppressed"], "first_err": st["first_err"]})
    _NET.pop(h, None)


def close_stale_outages(now: float | None = None) -> None:
    """On startup: an outage_start left open by a previous process (restart during an outage) gets an outage_end
    marked process_restart, so start/end pairs in the journal stay consistent."""
    now = now or time.time(); open_: dict[str, dict[str, Any]] = {}
    try:
        lines = JOURNAL.read_text().splitlines()[-5000:] if JOURNAL.exists() else []
    except Exception:
        return
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("event") == "outage_start":
            open_[e.get("host") or e.get("url", "?")] = e
        elif e.get("event") == "outage_end":
            open_.pop(e.get("host") or e.get("url", "?"), None)
    for h, e in open_.items():
        journal({"event": "outage_end", "host": h, "reason": "process_restart", "seconds": round(now - float(e.get("since") or e.get("ts") or now))})


def get(url: str, timeout: float = 20, quiet: bool = False) -> Any:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-us-temp-paper"}), timeout=timeout) as r:
            data = json.loads(r.read())
        net_up(url)
        return data
    except Exception as exc:  # network blips are routine; the next poll retries
        if is_network_error(exc):
            net_down(url, exc)
        elif not quiet:
            journal({"event": "error", "url": url[-80:], "err": str(exc)[:120]})
        return None


def heartbeat(ok: bool = True) -> None:
    """Ping the off-box monitor (healthchecks.io convention: <url> on success, <url>/fail on failure). Never raises."""
    url = CFG.get("heartbeat_url")
    if not url:
        return
    try:
        with urllib.request.urlopen(urllib.request.Request(url if ok else url.rstrip("/") + "/fail", headers={"User-Agent": "scout-temp-paper"}), timeout=10) as r:
            r.read()
    except Exception:
        pass


def journal(ev: dict[str, Any]) -> None:
    ev = {"ts": time.time(), **ev}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps(ev) + "\n")


def fee(p: float, shares: float) -> float:
    return round(FEE * p * (1 - p) * shares, 4)


# ------------------------------------------------------------------ buckets
_BUCKET = re.compile(r"(?:gte(-?\d+))?(?:lt(-?\d+))?f$")


def bounds(slug: str) -> tuple[float, float]:
    """Venue convention: gte68lt69f = 68-69F inclusive, lt68f = 67F or below, gte76f = 76F or more. Negative strikes
    (winter: lt-3f, gte-2lt-1f, gte-2f) are supported; only the bucket suffix at the end of the slug is parsed, so
    nothing in the ticker part can be mistaken for a strike."""
    m = _BUCKET.search(slug)
    lo = m.group(1) if m else None; hi = m.group(2) if m else None
    if lo is not None and hi is not None:
        return float(lo), float(hi)
    if hi is not None:
        return -1e9, float(hi) - 1
    return (float(lo) if lo is not None else -1e9), 1e9


def round_f(x: float) -> int:
    """Round half up to a whole degree like the climate report (int(x + 0.5) is wrong below -0.5F)."""
    return math.floor(x + 0.5)


# ------------------------------------------------------------------ METAR
def metar_temps_f(raw: str) -> list[float]:
    """[current reading F, optional 6-hour maximum F]. Current = T-group tenths (fallback TT/TD group); the 6-hour
    maximum remark (1sTTT, reported at 00/06/12/18Z) is the true max of the *previous six hours* from continuous
    sensing. Callers must check that those six hours lie inside the climate day before using it."""
    out: list[float] = []
    m = re.search(r"\bT([01])(\d{3})[01]\d{3}\b", raw)
    if m:
        c = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1); out.append(c * 9 / 5 + 32)
    else:
        m = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", raw)
        if m:
            c = float(m.group(1).replace("M", "-")); out.append(c * 9 / 5 + 32)
    if out and " RMK " in f" {raw} ":
        toks = raw.split(" RMK ", 1)[1].split() if " RMK " in raw else raw.split("RMK", 1)[1].split()
        skip = 0
        for i, tok in enumerate(toks):
            if skip:
                skip -= 1; continue
            if tok == "PK" and i + 1 < len(toks) and toks[i + 1] == "WND":
                skip = 2; continue  # "PK WND dddff(f)/(hh)mm" is peak wind, never a temperature (KDEN 2026-09-02)
            g = re.fullmatch(r"1([01])(\d{3})", tok)
            if g:
                c = int(g.group(2)) / 10.0 * (-1 if g.group(1) == "1" else 1); out.append(c * 9 / 5 + 32)
                break
    return out


IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def iem_url(station: str, now_utc: dt.datetime) -> str:
    """IEM archive request for the last ~2 UTC days through now. The end date is exclusive and in UTC, so it must be
    tomorrow's UTC date (the old local "today.day" end returned only yesterday on the 28th-31st and after 00Z)."""
    start = (now_utc - dt.timedelta(days=2)).date(); end = (now_utc + dt.timedelta(days=1)).date()
    return (f"{IEM}?station={station[1:]}&data=metar&year1={start.year}&month1={start.month}&day1={start.day}&year2={end.year}&month2={end.month}&day2={end.day}"
            f"&tz=Etc/UTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=3&report_type=4")


def fetch_metars(station: str, tz: zoneinfo.ZoneInfo) -> list[dict[str, Any]]:
    """Last 30 h of METARs as [{reportTime, rawOb}]. aviationweather.gov first; when it times out or returns
    502/504 (it does, several times a day) fall back to the Iowa State ASOS archive for the last two local days."""
    rows = get(f"{AWC}?ids={station}&format=json&hours=30", timeout=40)
    if rows:
        return rows
    import csv, io
    url = iem_url(station, dt.datetime.now(dt.timezone.utc))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-us-temp-paper"}), timeout=40) as r:
            text = r.read().decode()
        net_up(url)
    except Exception as exc:
        if is_network_error(exc):
            net_down(url, exc)
        else:
            journal({"event": "error", "url": "iem-fallback", "err": str(exc)[:120]})
        return []
    out = []
    for rec in csv.DictReader(io.StringIO(text)):
        if rec.get("metar"):
            out.append({"reportTime": rec["valid"].replace(" ", "T") + ":00Z", "rawOb": rec["metar"]})
    journal({"event": "fallback", "station": station, "rows": len(out)})
    return out


def parse_cli(text: str) -> dict[str, Any] | None:
    """NWS Daily Climate Report: '...THE MIAMI CLIMATE SUMMARY FOR AUGUST 15 2026...', 'VALID TODAY AS OF 0400 PM
    LOCAL TIME.', then under TEMPERATURE (F) / TODAY: 'MAXIMUM  93  2:57 PM ...'. Returns day, as-of minute, max."""
    m = re.search(r"SUMMARY FOR (\w+) (\d{1,2}) (\d{4})", text)
    v = re.search(r"VALID (TODAY|YESTERDAY)? ?AS OF (\d{3,4}) (AM|PM)", text)
    mx = re.search(r"\n\s*MAXIMUM\s+(-?\d+)R?\s+(\d{1,2}:\d{2} [AP]M)?", text)
    if not (m and mx and v and v.group(1) == "TODAY"):
        return None
    try:
        day = dt.date(int(m.group(3)), _MON[m.group(1).upper()], int(m.group(2)))
    except (KeyError, ValueError):
        return None
    hhmm = v.group(2).zfill(4); h = int(hhmm[:2]) % 12 + (12 if v.group(3) == "PM" else 0)
    return {"day": day.isoformat(), "asof_min": h * 60 + int(hhmm[2:]), "max": float(mx.group(1)), "max_time": mx.group(2)}


def cli_intraday(city: str, day: str, fetch=None) -> dict[str, Any] | None:
    """Today's intraday NWS climate report for the city (cached 10 min). Location codes on api.weather.gov are the
    station ids (MIA, NYC, MDW, SFO, LAX)."""
    if city not in CFG["cli_cities"]:
        return None  # no afternoon report office: never fetch (saved ~1,400-2,900 wasted api.weather.gov calls/day)
    key = f"{city}:{day}"; hit = _CLI_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    def _fetch():
        loc = city.upper()
        lst = get(f"{NWS}?type=CLI&location={loc}&limit=6") or {}
        for item in lst.get("@graph") or []:
            prod = get(f"{NWS}/{item.get('id')}") or {}
            rep = parse_cli(prod.get("productText") or "")
            if rep and rep["day"] == day:
                return rep
        return None
    rep = (fetch or _fetch)()
    if rep and (city not in CFG["cli_cities"] or rep["asof_min"] < 12 * 60):
        rep = None  # not an afternoon max-so-far report: never use it
    _CLI_CACHE[key] = (time.time(), rep)
    return rep


def _row_time(r: dict[str, Any], use_obs_time: bool) -> dt.datetime | None:
    """aviationweather.gov rows carry reportTime (rounded UP to the next hour for routine METARs) and obsTime (the
    actual observation, epoch seconds); IEM fallback rows carry only reportTime (actual time)."""
    t = r.get("obsTime") if use_obs_time and r.get("obsTime") is not None else (r.get("reportTime") or r.get("obsTime"))
    try:
        ts = dt.datetime.fromtimestamp(float(t), dt.timezone.utc) if isinstance(t, (int, float)) else dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except Exception:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _collect(rows: list[dict[str, Any]], tz: zoneinfo.ZoneInfo, now: dt.datetime, day_start: dt.datetime, use_obs_time: bool):
    """(readings [(local time, F, trusted)], has_00z, rejected group values). Hourly readings first, then 6-hour
    groups checked against ALL hourly readings in their window (aviationweather.gov returns newest-first)."""
    obs: list[tuple[dt.datetime, float, bool]] = []; groups = []; has_00z = False; rejected: list[float] = []
    for r in rows:
        ts = _row_time(r, use_obs_time)
        if ts is None:
            continue
        lt = ts.astimezone(tz)
        if lt < day_start or lt > now.astimezone(tz):
            continue
        temps = metar_temps_f(r.get("rawOb") or "")
        if temps:
            obs.append((lt, temps[0], False))
        if len(temps) > 1 and lt - dt.timedelta(hours=6) >= day_start:  # 6-hour window entirely inside today's climate day
            groups.append((lt, temps[1], ts))
    for lt, gmax, ts in groups:
        hourly = [(lt2, f) for lt2, f, tr in obs if not tr and lt - dt.timedelta(hours=6) <= lt2 <= lt]
        good = [f for lt2, f in hourly if (lambda neigh: not neigh or max(neigh) >= f - 2.5)([g for lt3, g in hourly if lt3 != lt2 and abs((lt3 - lt2).total_seconds()) <= 5400])]
        if good and gmax > max(good) + 3.0:  # far above every corroborated hourly reading in its window: sensor artefact (KNYC 2026-08-27)
            rejected.append(gmax); continue
        obs.append((lt, gmax, True))
        if ts.astimezone(dt.timezone.utc).hour in (0, 23):  # the 00Z report (23:5x-00:0xZ) carries the afternoon maximum
            has_00z = True
    obs.sort()
    return obs, has_00z, rejected


def _robust(obs: list[tuple[dt.datetime, float, bool]]) -> list[tuple[dt.datetime, float]]:
    """Corroborated readings: an hourly reading counts only if another reading within 90 minutes is >= it - 2.5F
    (lone sensor spikes such as KNYC 2026-08-27, 80F in heavy rain with the maintenance flag, are ignored)."""
    def ok(lt, f, trusted):
        if trusted:
            return True
        neigh = [f2 for lt2, f2, _ in obs if lt2 != lt and abs((lt2 - lt).total_seconds()) <= 5400]
        return not neigh or max(neigh) >= f - 2.5
    return [(lt, f) for lt, f, tr in obs if ok(lt, f, tr)] or [(obs[-1][0], obs[-1][1])]


def observed(station: str, tz: zoneinfo.ZoneInfo, now: dt.datetime, fetch=None, day_start_hour: int | None = None) -> dict[str, Any] | None:
    """Today's (climate-day) running max, latest temperature and time of the max, from METARs. The climate day is
    midnight-to-midnight local *standard* time: 01:00 local wherever daylight saving is in force, 00:00 where it is
    not (Phoenix). Also returns shadow values for review: max_raw (no spike filter) and max_obstime (rows keyed by
    the actual observation time instead of the hour-rounded reportTime)."""
    fetch = fetch or (lambda: fetch_metars(station, tz))
    rows = fetch() or []
    if day_start_hour is None:
        day_start_hour = 1 if now.astimezone(tz).dst() else 0
    day_start = now.astimezone(tz).replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    obs, has_00z, rejected = _collect(rows, tz, now, day_start, use_obs_time=False)
    if not obs:
        return None
    good = _robust(obs)
    mx = max(f for _, f in good); t_max = max(lt for lt, f in good if f == mx)
    latest = [f for lt, f, tr in obs if lt == obs[-1][0] and not tr] or [obs[-1][1]]
    readings = [{"time": lt.strftime("%H:%M"), "f": round(f, 1), "src": "6hr max" if tr else "hourly", "used": (lt, f) in good} for lt, f, tr in obs]
    max_raw = max([f for _, f, _ in obs] + rejected)
    try:
        obs2, _, _ = _collect(rows, tz, now, day_start, use_obs_time=True)
        max_obstime = max(f for _, f in _robust(obs2)) if obs2 else None
    except Exception:
        max_obstime = None
    return {"max": mx, "t_max": t_max, "latest": min(latest), "n": len(obs), "last_obs": obs[-1][0], "has_00z": has_00z, "readings": readings,
            "max_raw": max_raw, "max_obstime": max_obstime}


# ------------------------------------------------------------------ signals
def evaluate(buckets: list[dict[str, Any]], ob: dict[str, Any], now_local: dt.datetime, cfg=CFG, city: str = "") -> dict[str, Any]:
    """Explain every bucket: its position relative to the observed max, which rule applies, and what blocks it.
    Returns {"flags": {...}, "rows": [...]}; rows with candidate=True are the trades (see signals()).
    R1x: once the 00Z six-hour maximum has been received (ob["has_00z"]), the day's high is known on ~98% of days
    (backtest: 722/735 station-days exact); buy the bucket holding it and fade every other bucket."""
    M = ob["max"]; T = ob["latest"]; r_m = round_f(M)  # the official report rounds to whole F
    r_raw = round_f(max(M, ob.get("max_raw", M)))  # unfiltered max (spike filter off): shadow only, see r2_shadow
    M_obs = ob.get("max_obs", M)  # METAR-only max: the certain rule must not lean on a preliminary report value
    since = (now_local - ob["t_max"]).total_seconds() / 60
    fall = M - T
    hour_ok = now_local.hour >= cfg["peak_hour"]; fall_ok = fall >= cfg["peak_fall"]; since_ok = since >= cfg["peak_min_since"]
    peak_passed = hour_ok and fall_ok and since_ok
    z00 = Z00_LOCAL_HOUR.get(city, 20)
    rep = ob.get("cli")
    cli_ok = bool(cfg.get("cli_trigger")) and bool(rep) and now_local.hour >= cfg["peak_hour"] and fall >= cfg["cli_fall"] and since >= cfg["cli_min_since"]
    after_00z = (bool(cfg.get("r1x_00z", 1)) and bool(ob.get("has_00z")) and now_local.hour >= z00) or cli_ok
    flags = {"max": round(M, 1), "r_max": r_m, "latest": round(T, 1), "fall_f": round(fall, 1), "since_max_min": round(since), "peak_passed": peak_passed,
             "peak_hour_ok": hour_ok, "fall_ok": fall_ok, "since_ok": since_ok, "has_00z": bool(ob.get("has_00z")), "after_00z": after_00z, "z00_local_hour": z00,
             "cli_report": (f"{rep['max']:.0f}F as of {rep['asof_min']//60:02d}:{rep['asof_min']%60:02d}" if rep else None), "cli_window": cli_ok}
    rows = []
    for b in buckets:
        lo, hi = bounds(b["slug"]); bid, ask = b.get("bid"), b.get("ask")
        no_ask = (1 - bid) if bid else None
        label = f"<= {hi:.0f}" if lo < -1e8 else f">= {lo:.0f}" if hi > 1e8 else f"{lo:.0f}-{hi:.0f}"
        row = {"slug": b["slug"], "label": label, "lo": None if lo < -1e8 else lo, "hi": None if hi > 1e8 else hi, "bid": bid, "ask": ask,
               "bid_sz": b.get("bid_sz"), "ask_sz": b.get("ask_sz"), "candidate": False, "rule": None, "side": None, "px": None, "size": 0, "why": "", "blocker": ""}
        holds = lo <= r_m <= hi
        dead = hi + 1.0 <= M_obs           # a full degree above the bucket top (KDFW 2026-09-20: 96.8F observed, official 96)
        top_certain = hi >= 1e8 and M_obs >= lo + 0.05
        row["status"] = "dead (top below observed max)" if dead else "certain YES (max reached its floor)" if top_certain else "holds the max" if holds else f"above the max by {lo - r_m:.0f}F" if lo > r_m else "below the max"
        def cand(rule, side, px, size, why):
            row.update({"candidate": True, "rule": rule, "side": side, "px": round(px, 3), "size": size or 0, "why": why, "blocker": ""})
        if cfg["r1x"] and after_00z and holds:
            if ask and 0.02 <= ask <= cfg["r1x_max_ask"]:
                cand("R1x", "YES", ask, b.get("ask_sz"), f"00Z max {r_m} in bucket")
            else:
                row["blocker"] = f"R1x YES: ask {ask if ask is not None else 'none'} not within 0.02-{cfg['r1x_max_ask']:.2f}"
        elif cfg["r1x"] and after_00z and not holds:
            if bid and bid >= cfg["r1x_min_bid"] and no_ask and 0.02 <= no_ask <= cfg["r0_max_ask"]:
                cand("R1x", "NO", no_ask, b.get("bid_sz"), f"00Z max {r_m} outside bucket")
            else:
                row["blocker"] = f"R1x NO: bid {bid if bid is not None else 'none'} < {cfg['r1x_min_bid']:.2f} (nothing worth selling into)"
        elif dead:
            if no_ask and 0.02 <= no_ask <= cfg["r0_max_ask"]:
                cand("R0", "NO", no_ask, b.get("bid_sz"), f"top {hi:.0f} < observed max {M_obs:.1f}")
            else:
                row["blocker"] = "R0: no bid to sell into" if not bid else f"R0: NO ask {no_ask:.2f} > cap {cfg['r0_max_ask']:.2f}"
        elif top_certain:
            if ask and 0.02 <= ask <= cfg["r0_max_ask"]:
                cand("R0", "YES", ask, b.get("ask_sz"), f"observed max {M_obs:.1f} >= floor {lo:.0f}")
            else:
                row["blocker"] = f"R0 YES: ask {ask if ask is not None else 'none'} > cap {cfg['r0_max_ask']:.2f}"
        elif lo >= r_m + cfg["r2_margin"] and cfg.get("r2", 1):
            if not peak_passed:
                why_not = [] if hour_ok else [f"before {cfg['peak_hour']:.0f}:00 local"]
                if not fall_ok: why_not.append(f"fall {fall:.1f}F < {cfg['peak_fall']:.0f}F")
                if not since_ok: why_not.append(f"{since:.0f} min since max < {cfg['peak_min_since']:.0f}")
                row["blocker"] = "R2 waits for the peak: " + ", ".join(why_not)
            elif bid and bid >= cfg["r2_min_bid"] and no_ask and no_ask >= 0.02:
                cand("R2", "NO", no_ask, b.get("bid_sz"), f"floor {lo:.0f} >= max {r_m}+{cfg['r2_margin']:.0f}, peak passed")
            else:
                row["blocker"] = f"R2: bid {bid if bid is not None else 'none'} < {cfg['r2_min_bid']:.2f}"
        elif holds:
            triggers = (["the 00Z report (%d:00 local)" % z00] if cfg.get("r1x_00z", 1) else []) + (["the afternoon climate report + peak"] if cfg.get("cli_trigger") else [])
            if cfg["r1"]:
                row["blocker"] = "holds the max; R1 waits for the peak" if not peak_passed else f"holds the max; R1 needs ask 0.02-{cfg['r1_max_ask']:.2f} and the max+1 inside the bucket"
            elif not cfg["r1x"] or not triggers:
                row["blocker"] = "holds the max; no active rule buys it (R1x off)" if not cfg["r1x"] else "holds the max; no active rule buys it (00Z and report triggers off)"
            else:
                row["blocker"] = "holds the max; R1x waits for " + " or ".join(triggers) if not after_00z else ""
            if cfg["r1"] and peak_passed and r_m + 1 <= hi and ask and 0.02 <= ask <= cfg["r1_max_ask"]:
                cand("R1", "YES", ask, b.get("ask_sz"), f"bucket holds max {r_m} and {r_m+1}, peak passed")
        elif lo > r_m:
            row["blocker"] = (f"above the max by {lo - r_m:.0f}F; R2 off" if lo >= r_m + cfg["r2_margin"] else
                              f"above the max by only {lo - r_m:.0f}F (< R2 margin {cfg['r2_margin']:.0f}F)")
        else:
            row["blocker"] = "below the max but not yet dead by a full degree"
        # shadow: would R2 still fade this bucket if r_m came from the unfiltered max (spike filter off)? The filter
        # rejected the true high on 32 of 1,203 backtest days (e.g. KMDW 2026-09-08: bot 84.0, official 87).
        row["r2_raw"] = bool(row["rule"] == "R2" and lo >= r_raw + cfg["r2_margin"])
        rows.append(row)
    flags["r_max_raw"] = r_raw
    flags["r2_shadow_diff"] = [r["label"] for r in rows if r["rule"] == "R2" and not r["r2_raw"]]
    return {"flags": flags, "rows": rows}


def signals(buckets: list[dict[str, Any]], ob: dict[str, Any], now_local: dt.datetime, cfg=CFG, city: str = "") -> list[dict[str, Any]]:
    """Candidate trades: [{rule, slug, side, px, size, why}] (see evaluate() for the full reasoning)."""
    return [{k: r[k] for k in ("rule", "slug", "side", "px", "size", "why")} for r in evaluate(buckets, ob, now_local, cfg, city)["rows"] if r["candidate"]]


# ------------------------------------------------------------------ ledger
def load_ledger() -> dict[str, Any]:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {"cash": CFG["cash"], "start_cash": CFG["cash"], "positions": [], "fills": [], "pending": {}, "created": dt.datetime.now(dt.timezone.utc).isoformat()}


def save_ledger(led: dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp"); tmp.write_text(json.dumps(led, indent=1)); tmp.replace(LEDGER)


def start_poll(led: dict[str, Any], now: float | None = None) -> int:
    """Advance the poll counter; confirmations count only sightings on consecutive polls. After a gap longer than
    3 poll intervals (restart, reboot waiting at the FileVault login, outage) every pending confirmation is dropped:
    the poll before the gap and the poll after it are not consecutive in any useful sense."""
    now = time.time() if now is None else now
    led.setdefault("pending", {})
    if led.get("poll_ts") and now - float(led["poll_ts"]) > 3 * CFG["poll_s"]:
        led["pending"] = {}
    led["poll_seq"] = int(led.get("poll_seq", 0)) + 1; led["poll_ts"] = now
    return led["poll_seq"]


def finish_poll(led: dict[str, Any]) -> None:
    """Drop every pending confirmation that was not seen on this poll (once per poll, including polls with no
    candidates). Before 2026-10-01 pruning happened inside each per-candidate call, which deleted the other
    candidates' entries (two simultaneous candidates could never fill) and never ran on empty polls (a candidate seen
    once could fill on any later sighting)."""
    seq = int(led.get("poll_seq", 0))
    for key in list(led.get("pending", {}).keys()):
        if led["pending"][key].get("last") != seq:
            del led["pending"][key]


def confirm_and_fill(led: dict[str, Any], cands: list[dict[str, Any]], meta: dict[str, Any], cfg=CFG) -> list[dict[str, Any]]:
    """Catchable rule: the same (slug, side) must be seen on `confirm` consecutive polls at a price no worse than the
    first sighting; fill at the latest price, size capped by the displayed size. Safe to call once per candidate:
    it never touches other candidates' entries (pruning is finish_poll's job). meta is this candidate's own."""
    seq = int(led.get("poll_seq", 0)); led.setdefault("pending", {})
    fills = []
    held = {(p["slug"], p["side"]) for p in led["positions"]}
    for c in cands:
        key = c["slug"] + "|" + c["side"]
        if (c["slug"], c["side"]) in held:
            continue
        p = led["pending"].get(key)
        if p and p.get("last") == seq:
            continue  # already counted on this poll
        now = time.time()
        consecutive = bool(p) and p.get("last") == seq - 1 and now - float(p.get("ts", 0)) <= 2.5 * cfg["poll_s"]
        if not consecutive or c["px"] > p["px"] + 0.005:  # new, interrupted, too old, or got worse: restart the confirmation
            p = led["pending"][key] = {"n": 1, "px": c["px"], "rule": c["rule"], "last": seq, "ts": now, "day": meta.get("day")}
        else:
            p["n"] += 1; p["last"] = seq; p["ts"] = now
        if p["n"] < cfg["confirm"]:
            continue
        if c.get("size") is None:  # the order book could not be read this poll: keep the confirmation, retry next poll
            journal({"event": "skip", "reason": "no_book", **{k: v for k, v in c.items() if k != "depth"}, **meta}); continue
        shares = round(min(cfg["stake"] / c["px"], float(c["size"] or 0)), 2)
        if shares < 1 or led["cash"] < shares * c["px"] + fee(c["px"], shares):
            journal({"event": "skip", "reason": "size" if shares < 1 else "cash", **{k: v for k, v in c.items() if k != "depth"}, **meta}); del led["pending"][key]; continue
        stake = round(shares * c["px"], 4); f = fee(c["px"], shares)
        pos = {"slug": c["slug"], "side": c["side"], "px": c["px"], "shares": shares, "stake": stake, "fee": f, "rule": c["rule"], "why": c["why"],
               "opened": dt.datetime.now(dt.timezone.utc).isoformat(), **meta}
        led["cash"] = round(led["cash"] - stake - f, 4); led["positions"].append(pos); fills.append(pos); del led["pending"][key]
        journal({"event": "fill", **pos})
    return fills


_SETTLE_NEXT: dict[str, float] = {}


def settle(led: dict[str, Any]) -> list[dict[str, Any]]:
    done = []
    for pos in list(led["positions"]):
        end = pos.get("end_date")
        if end and dt.datetime.fromisoformat(end.replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc):
            continue
        if time.time() < _SETTLE_NEXT.get(pos["slug"], 0):
            continue
        _SETTLE_NEXT[pos["slug"]] = time.time() + 600  # the venue posts the settlement hours after close; 404 until then
        s = get(f"{US}/markets/{pos['slug']}/settlement", quiet=True)
        val = s.get("settlement") if isinstance(s, dict) else None
        if val is None:
            continue
        settle_px = float(val)  # YES value at settlement (1 or 0)
        payoff = pos["shares"] * (settle_px if pos["side"] == "YES" else 1 - settle_px)
        pnl = round(payoff - pos["stake"] - pos["fee"], 4)
        led["cash"] = round(led["cash"] + payoff, 4); led["positions"].remove(pos)
        fill = {**pos, "settled": dt.datetime.now(dt.timezone.utc).isoformat(), "settle_yes": settle_px, "pnl": pnl, "won": pnl > 0}
        led["fills"].append(fill); done.append(fill); journal({"event": "settle", **fill})
    return done


# ------------------------------------------------------------------ loop
def bbo(slug: str) -> dict[str, Any] | None:
    d = get(f"{US}/markets/{slug}/bbo")
    md = (d or {}).get("marketData") if isinstance(d, dict) else None
    if not md:
        return None
    def v(x):
        try:
            return float(x["value"]) if isinstance(x, dict) else (float(x) if x not in (None, "") else None)
        except (TypeError, ValueError, KeyError):
            return None
    def sz(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0
    return {"slug": slug, "bid": v(md.get("bestBid")), "ask": v(md.get("bestAsk")), "bid_sz": sz(md.get("bidShares")), "ask_sz": sz(md.get("askShares")), "state": md.get("state")}


_LADDERS: dict[str, list[dict[str, Any]]] = {}


def ladder(city: str, day: str) -> list[dict[str, Any]]:
    key = f"{city}:{day}"
    if key not in _LADDERS:
        ev = (get(f"{US}/events?slug=temp-{city}high-{day}") or {}).get("events") or []
        _LADDERS[key] = [{"slug": m["slug"], "end_date": m.get("endDate")} for m in (ev[0].get("markets") if ev else [])]
    return _LADDERS[key]


def poll(led: dict[str, Any], now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    n_c = n_f = 0; notes = []; start_poll(led)
    snap: dict[str, Any] = {"ts": now.timestamp(), "updated": now.isoformat(), "config": {**CFG, "z00_local_hour": Z00_LOCAL_HOUR}, "cities": {}}
    for city, (station, tzname) in CITIES.items():
        tz = zoneinfo.ZoneInfo(tzname); now_local = now.astimezone(tz); day = now_local.date().isoformat()
        cs = snap["cities"][city] = {"station": station, "tz": tzname, "local_time": now_local.strftime("%H:%M"), "day": day, "active": False, "note": ""}
        if now_local.hour < 9:
            cs["note"] = "waits until 09:00 local"; continue
        mk = ladder(city, day)
        if not mk:
            cs["note"] = "no ladder listed for today"; continue
        ob = observed(station, tz, now)
        if not ob:
            cs["note"] = "no METAR observations yet today"; continue
        rep = cli_intraday(city, day) if CFG["cli_obs"] and now_local.hour >= 15 else None
        ob["cli"] = rep; ob["max_obs"] = ob["max"]
        if rep and rep["max"] >= ob["max"] - 0.5:  # the official max-so-far is a floor for the day's high; treat like a 6-hour group
            if rep["max"] > ob["max"]:
                ob["max"] = rep["max"]; ob["t_max"] = now_local.replace(hour=rep["asof_min"] // 60, minute=rep["asof_min"] % 60, second=0, microsecond=0)
            ob["readings"].append({"time": f"{rep['asof_min']//60:02d}:{rep['asof_min']%60:02d}", "f": rep["max"], "src": "NWS climate report", "used": True})
        buckets = []
        for m in mk:
            q = bbo(m["slug"])
            if q and q.get("state", "").endswith("OPEN"):
                buckets.append(q)
        ev = evaluate(buckets, ob, now_local, city=city)
        cs.update({"active": True, "observed": {**ev["flags"], "t_max": ob["t_max"].strftime("%H:%M"), "n_readings": ob["n"]}, "readings": ob.get("readings", []), "buckets": ev["rows"],
                   "n_buckets_open": len(buckets), "candidates": sum(1 for r in ev["rows"] if r["candidate"])})
        fl = ev["flags"]
        for tag, cond in (("cli_report", bool(fl.get("cli_report"))), ("early_window", bool(fl.get("cli_window"))), ("z00_window", bool(ob.get("has_00z")))):
            k = f"{city}:{day}:{tag}"
            if cond and k not in _SEEN:
                _SEEN.add(k); journal({"event": tag, "city": city, "day": day, "max": fl["max"], "latest": fl["latest"], "report": fl.get("cli_report")})
        holds = next((r for r in ev["rows"] if r["status"] == "holds the max"), None)
        try:
            with SNAPS.open("a") as fh:
                fh.write(json.dumps({"ts": round(now.timestamp()), "city": city, "lt": now_local.strftime("%H:%M"), "max": fl["max"], "latest": fl["latest"], "fall": fl["fall_f"], "since": fl["since_max_min"],
                                     "peak": fl["peak_passed"], "z00": bool(ob.get("has_00z")), "cli": fl.get("cli_report"), "early": fl.get("cli_window"), "cands": cs["candidates"],
                                     "holds": holds and {"b": holds["label"], "bid": holds["bid"], "ask": holds["ask"]},
                                     "book": [(r["label"], r["bid"], r["ask"]) for r in ev["rows"] if (r["bid"] or 0) >= 0.05]}) + "\n")
        except Exception:
            pass
        cands = signals(buckets, ob, now_local, city=city)
        held_keys = {(pth["slug"], pth["side"]) for pth in led["positions"]}
        for c in cands:
            if (c["slug"], c["side"]) in held_keys:
                continue  # already holding it; do not re-log the same signal every poll
            journal({"event": "signal", "city": city, **c, "max": round(ob["max"], 1), "latest": round(ob["latest"], 1)})
        meta = {"city": city, "day": day, "end_date": next((m["end_date"] for m in mk if m["slug"] == (cands[0]["slug"] if cands else "")), None)}
        for c in cands:
            c_meta = dict(meta, end_date=next((m["end_date"] for m in mk if m["slug"] == c["slug"]), None))
            n_f += len(confirm_and_fill(led, [c], c_meta))
        n_c += len(cands)
        notes.append(f"{city} max={ob['max']:.0f} now={ob['latest']:.0f} cands={len(cands)}")
    finish_poll(led)
    settled = settle(led)
    led["updated"] = now.isoformat(); save_ledger(led)
    snap.update({"cash": led["cash"], "positions": led["positions"], "pending": led.get("pending", {}), "n_fills": len(led.get("fills", []))})
    try:
        tmp = STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(snap, default=str)); tmp.replace(STATE)
    except Exception as exc:
        journal({"event": "error", "url": "state-file", "err": str(exc)[:120]})
    return f"us-temp paper cash={led['cash']:.2f} pos={len(led['positions'])} cands={n_c} fills={n_f} settled={len(settled)} | " + " ".join(notes)


def main() -> None:
    once = "--once" in sys.argv
    led = load_ledger()
    print(f"US temperature paper trader: stake ${CFG['stake']:.0f}, poll {CFG['poll_s']:.0f}s, R0<= {CFG['r0_max_ask']}, R2 margin {CFG['r2_margin']:.0f}F, R1 {'on' if CFG['r1'] else 'off'}", flush=True)
    close_stale_outages()
    while True:
        t0 = time.monotonic(); ok = True
        try:
            print(poll(led), flush=True)
        except Exception as exc:
            ok = False; print(f"cycle error: {type(exc).__name__}: {exc}", flush=True); journal({"event": "cycle_error", "err": str(exc)[:200]})
        heartbeat(ok)
        if once:
            break
        time.sleep(max(0.0, t0 + CFG["poll_s"] - time.monotonic()))  # fixed-rate, immune to wall-clock steps


if __name__ == "__main__":
    main()
