"""weather_walkforward: snapshot builder for the Kalshi daily-high temperature ladders (KXHIGH*, 24 cities,
2025-07-01..2026-10-07). Frozen copies of the observation helpers of lab/us/temp_backtest.py and
lab/us/kalshi_backtest.py (so later edits there cannot change these results), with three realism fixes:
  * candle minutes are counted from the climate day's local midnight (post-midnight candles are 1440+, never mixed
    with the afternoon - the original quote_at could pick a post-midnight quote for a late decision);
  * routine/special METARs are usable LAG_METAR minutes after their observation time, 5-minute HFMETAR rows LAG_HF
    minutes after (api.weather.gov serves them ~18 min late);
  * quotes are carried forward at most MAX_AGE (30) minutes.
Decision times: a 15-minute grid 10:00..23:45 local plus every METAR availability minute (obs + LAG_METAR) in that
range. For each decision time t: observation state (only data usable at t) and, for every bucket, the quote at
t + FILL_DELAY (taker fill one minute later).
Output: data/kalshi_lab/strategies/weather_walkforward/snap.pkl.gz
Usage: .venv/bin/python -m lab.kalshi.strategies.weather_walkforward_data"""
from __future__ import annotations
import bisect, csv, datetime as dt, gzip, json, math, os, pickle, re, sys, zoneinfo
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KD = ROOT / "data/lab/us/kalshi"
RAW = ROOT / "data/lab/us/asos_raw"
OUT = ROOT / "data/kalshi_lab/strategies/weather_walkforward"
SERIES = {"KXHIGHNY": ("NYC", "KNYC", "America/New_York"), "KXHIGHCHI": ("MDW", "KMDW", "America/Chicago"), "KXHIGHMIA": ("MIA", "KMIA", "America/New_York"),
          "KXHIGHLAX": ("LAX", "KLAX", "America/Los_Angeles"), "KXHIGHTSFO": ("SFO", "KSFO", "America/Los_Angeles"), "KXHIGHTBOS": ("BOS", "KBOS", "America/New_York"),
          "KXHIGHTDC": ("DCA", "KDCA", "America/New_York"), "KXHIGHPHIL": ("PHL", "KPHL", "America/New_York"), "KXHIGHTATL": ("ATL", "KATL", "America/New_York"),
          "KXHIGHDEN": ("DEN", "KDEN", "America/Denver"), "KXHIGHAUS": ("AUS", "KAUS", "America/Chicago"), "KXHIGHTDAL": ("DFW", "KDFW", "America/Chicago"),
          "KXHIGHTMIN": ("MSP", "KMSP", "America/Chicago"), "KXHIGHTPHX": ("PHX", "KPHX", "America/Phoenix"), "KXHIGHTSEA": ("SEA", "KSEA", "America/Los_Angeles"),
          "KXHIGHTLV": ("LAS", "KLAS", "America/Los_Angeles"), "KXHIGHTSAN": ("SAN", "KSAN", "America/Los_Angeles"),
          "KXHIGHTHOU": ("HOU", "KHOU", "America/Chicago"), "KXHIGHTOKC": ("OKC", "KOKC", "America/Chicago"),
          "KXHIGHTSATX": ("SAT", "KSAT", "America/Chicago"), "KXHIGHTNOLA": ("MSY", "KMSY", "America/Chicago"),
          "KXHIGHTEWR": ("EWR", "KEWR", "America/New_York"), "KXHIGHTTTN": ("TTN", "KTTN", "America/New_York"),
          "KXHIGHTSDF": ("SDF", "KSDF", "America/Kentucky/Louisville")}
NO_LIVE_HF = {"NYC", "SAT"}     # api.weather.gov serves no 5-minute rows for these stations: never use them
LAG_METAR = int(os.environ.get("WW_LAG_METAR", 5))   # minutes from METAR observation time to usable (aviationweather/IEM latency + polling)
LAG_HF = 20                     # 5-minute rows
FILL_DELAY = 1                  # taker fill at the quote one minute after the decision
MAX_AGE = 30                    # carry a candle quote forward at most this many minutes
GRID = list(range(10 * 60, 23 * 60 + 46, 15))
T_LO, T_HI = 10 * 60, 23 * 60 + 45


def rnd(x: float) -> int:
    return math.floor(x + 0.5)


def day_start_minutes(stn: str, tzname: str):
    tz = zoneinfo.ZoneInfo(tzname); cache: dict[str, int] = {}
    def f(day: str) -> int:
        if day not in cache:
            d = dt.date.fromisoformat(day)
            cache[day] = 0 if stn == "PHX" or not tz.dst(dt.datetime(d.year, d.month, d.day, 12)) else 60
        return cache[day]
    return f


def six_hour_max_f(raw: str):
    raw = re.sub(r"\bPK WND \S+", "", raw or "")
    m = re.search(r"\bRMK\b.*?\b1([01])(\d{3})\b", raw)
    if not m:
        return None
    c = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1)
    return rnd(c * 9 / 5 + 32)


def robust_max(past, tol: float = 2.5):
    """Frozen copy of lab/us/temp_backtest.robust_max: highest reading that is a 6-hour max group or corroborated by
    a neighbour within 90 min at >= reading - tol."""
    for row in sorted(past, key=lambda r: (-r[1], -r[0])):
        m, x = row[0], row[1]; trusted = len(row) > 2 and row[2]
        neigh = [r[1] for r in past if r[0] != m and abs(r[0] - m) <= 90]
        if trusted or not neigh or max(neigh) >= x - tol:
            return x, m
    row = past[-1]
    return row[1], row[0]


def load_station(stn: str, tzn: str):
    """-> (obs: day -> sorted [(minute, F, is_group)] as lab/us/temp_backtest.metar (artefact filter on 6-hour groups),
           raw: day -> [(minute, F)] every reading incl. unfiltered 6-hour groups,
           hf: day -> [(minute, whole C)] 5-minute rows)."""
    obs = defaultdict(list); raw_ = defaultdict(list); hf = defaultdict(list)
    f = RAW / f"{stn}.csv"
    if not f.exists():
        return obs, raw_, hf
    start = day_start_minutes(stn, tzn)
    for r in csv.DictReader(open(f)):
        day, hm = r["valid"][:10], r["valid"][11:16]
        minute = int(hm[:2]) * 60 + int(hm[3:]); ds = start(day)
        if minute < ds:
            continue
        text = r.get("metar") or ""
        if "MADISHF" in text:
            if stn in NO_LIVE_HF:
                continue
            m = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", text)
            if m:
                c = float(m.group(1).replace("M", "-"))
            else:
                g = re.search(r"\bT([01])(\d{3})", text)
                if not g:
                    continue
                c = round(int(g.group(2)) / 10.0 * (-1 if g.group(1) == "1" else 1))
            hf[day].append((minute, c)); continue
        if r.get("tmpf") in (None, "M", ""):
            continue
        x = float(r["tmpf"])
        obs[day].append((minute, x, False)); raw_[day].append((minute, x))
        mx = six_hour_max_f(text)
        if mx is not None and minute >= ds + 6 * 60:
            raw_[day].append((minute, float(mx)))
            if mx >= x - 1:
                hourly = [(m_, y) for m_, y, tr in obs[day] if not tr and minute - 360 <= m_ <= minute]
                good = [y for m_, y in hourly if (lambda neigh: not neigh or max(neigh) >= y - 2.5)([z for n_, z in hourly if n_ != m_ and abs(n_ - m_) <= 90])]
                if good and mx > max(good) + 3.0:
                    continue
                obs[day].append((minute, float(mx), True))
    for d in (obs, raw_, hf):
        for k in d:
            d[k].sort()
    return obs, raw_, hf


def interval(m: dict):
    t = m.get("strike_type")
    if t == "less":
        return -1e9, float(m["cap_strike"]) - 1
    if t == "greater":
        return float(m["floor_strike"]) + 1, 1e9
    return float(m["floor_strike"]), float(m["cap_strike"])


def load_candles(ticker: str, midnight_ts: int):
    """-> sorted [(minute since the climate day's local midnight, ask_cents, bid_cents, volume)]"""
    f = KD / f"{ticker}.json"
    if not f.exists():
        return []
    out = []
    for c in json.loads(f.read_text()):
        try:
            ask = float(c["yes_ask"]["close_dollars"]); bid = float(c["yes_bid"]["close_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            v = float(c.get("volume_fp") or 0)
        except ValueError:
            v = 0.0
        out.append(((int(c["end_period_ts"]) - midnight_ts) // 60, int(round(ask * 100)), int(round(bid * 100)), v))
    out.sort()
    return out


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build():
    M = json.load(open(KD / "markets.json"))
    cli = json.load(open(ROOT / "data/lab/us/asos/cli_high.json"))
    days = defaultdict(list)
    for t, m in M.items():
        if m.get("result") not in ("yes", "no") or m["series"] not in SERIES:
            continue
        stn, icao, tzn = SERIES[m["series"]]
        close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        day = (close.astimezone(zoneinfo.ZoneInfo(tzn)) - dt.timedelta(hours=6)).date().isoformat()
        days[(stn, day)].append(m)
    print(f"station-days {len(days)}", flush=True)
    by_stn = defaultdict(list)
    for (stn, day) in days:
        by_stn[stn].append(day)
    out = []; skipped = defaultdict(int); fin_src = defaultdict(int); fin_diff = defaultdict(int)
    for stn in sorted(by_stn):
        icao, tzn = next((v[1], v[2]) for v in SERIES.values() if v[0] == stn)
        tz = zoneinfo.ZoneInfo(tzn)
        obs, raw_, hf = load_station(stn, tzn)
        start = day_start_minutes(stn, tzn)
        for day in sorted(by_stn[stn]):
            mk = days[(stn, day)]
            ob = obs.get(day) or []
            if len(ob) < 12:
                skipped["no_metar"] += 1; continue
            d = dt.date.fromisoformat(day)
            midnight = int(dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
            ev = next((num(m.get("expiration_value")) for m in mk if num(m.get("expiration_value")) is not None), None)
            H = cli.get(icao, {}).get(day)
            if ev is not None and H is not None:
                fin_diff[int(round(ev - H))] += 1
            final = ev if ev is not None else H
            fin_src["kalshi" if ev is not None else ("cli" if H is not None else "none")] += 1
            mrows = []
            for m in mk:
                cd = load_candles(m["ticker"], midnight)
                if not cd:
                    continue
                lo, hi = interval(m)
                mrows.append({"tk": m["ticker"], "lo": lo, "hi": hi, "won": m["result"] == "yes", "cd": cd,
                              "vol": num(m.get("volume_fp")) or 0.0, "close": int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())})
            if not mrows:
                skipped["no_candles"] += 1; continue
            # winning interval must be consistent with the final value
            win = [b for b in mrows if b["won"]]
            if final is not None and win and not (win[0]["lo"] <= final <= win[0]["hi"]):
                skipped["final_inconsistent"] += 1; final = None
            ds = start(day)
            evt = {m_ + LAG_METAR for m_, _, _ in ob if T_LO <= m_ + LAG_METAR <= T_HI}
            times = sorted(set(GRID) | evt)
            hfd = hf.get(day) or []; rawd = raw_.get(day) or []
            hf_m = [x[0] for x in hfd]; raw_m = [x[0] for x in rawd]; ob_m = [x[0] for x in ob]
            states = []
            for t in times:
                k = bisect.bisect_right(ob_m, t - LAG_METAR)
                past = ob[:k]
                if not past:
                    states.append(None); continue
                Mo, tM = robust_max(past)
                last_m = past[-1][0]
                Tcur = min(r[1] for r in past if r[0] == last_m)
                Mraw = max(x for _, x in rawd[:bisect.bisect_right(raw_m, t - LAG_METAR)]) if raw_m and raw_m[0] <= t - LAG_METAR else Mo
                h5 = hfd[:bisect.bisect_right(hf_m, t - LAG_HF)]
                M5mid = max((c * 1.8 + 32 for _, c in h5), default=-999.0)
                M5hi = max(((c + 0.5) * 1.8 + 32 for _, c in h5), default=-999.0)
                M5lo2 = max((((min(a[1], b_[1]) - 0.5) * 1.8 + 32) for a, b_ in zip(h5, h5[1:]) if b_[0] - a[0] <= 10), default=-999.0)
                T5 = h5[-1][1] * 1.8 + 32 if h5 and t - LAG_HF - h5[-1][0] <= 15 else None
                states.append((round(Mo, 2), tM, round(Mraw, 2), round(M5mid, 2), round(M5hi, 2), round(M5lo2, 2), round(Tcur, 2),
                               None if T5 is None else round(T5, 2), last_m))
            for b in mrows:
                cd = b.pop("cd"); cm = [x[0] for x in cd]; q = []
                for t in times:
                    tf = t + FILL_DELAY
                    i = bisect.bisect_right(cm, tf) - 1
                    if i < 0 or tf - cd[i][0] > MAX_AGE:
                        q.append(None); continue
                    lo_i = bisect.bisect_left(cm, t - 30); hi_i = bisect.bisect_right(cm, t + 30)
                    vol = sum(x[3] for x in cd[lo_i:hi_i])
                    q.append((cd[i][1], cd[i][2], tf - cd[i][0], round(vol, 1)))
                b["q"] = q
            out.append({"stn": stn, "day": day, "close": max(b["close"] for b in mrows), "final": final, "ds": ds,
                        "times": times, "S": states, "mk": mrows})
        print(stn, "days", sum(1 for r in out if r["stn"] == stn), flush=True)
    out.sort(key=lambda r: (r["close"], r["stn"]))
    OUT.mkdir(parents=True, exist_ok=True)
    tag = os.environ.get("WW_TAG", "")
    with gzip.open(OUT / f"snap{tag}.pkl.gz", "wb") as fh:
        pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
    meta = {"station_days": len(out), "skipped": dict(skipped), "final_source": dict(fin_src), "kalshi_minus_cli": dict(sorted(fin_diff.items())),
            "lag_metar": LAG_METAR, "lag_hf": LAG_HF, "fill_delay": FILL_DELAY, "max_age": MAX_AGE}
    (OUT / f"snap{tag}_meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta))


if __name__ == "__main__":
    build()
