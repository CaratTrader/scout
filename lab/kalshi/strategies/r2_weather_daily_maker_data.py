"""r2_weather_daily_maker data: per station-day ladders of the Kalshi daily-high series (KXHIGH*, 24 cities) with
1-minute candles in local wall-clock minutes 09:00..24:00, built from data/lab/us/kalshi (markets.json + one candle
JSON per ticker). Disk only, no Kalshi calls.

Candle row (minute = END of the 1-minute period, minutes since the climate day's local midnight, tz-aware):
  (minute, ask_close, bid_close, ask_low, bid_high, trade_low, trade_high, trade_close, volume)  - prices in cents,
  None where the archive (2025-07..2026-04, /historical) has no OHLC or no trade that minute.
The discovery / validation label is copied from weather_walkforward (sorted by (close, station), first 70% = disc;
the cut falls inside climate day 2026-08-19, key (1787202000, 'NYC')).
Output: data/kalshi_lab/strategies/r2_weather_daily_maker/days.pkl.gz
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_weather_daily_maker_data"""
from __future__ import annotations
import datetime as dt, gzip, json, pickle, sys, zoneinfo
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.weather_walkforward_data import SERIES, interval, num

KD = ROOT / "data/lab/us/kalshi"
OUT = ROOT / "data/kalshi_lab/strategies/r2_weather_daily_maker"
CUT_KEY = (1787202000, "NYC")      # last discovery station-day of weather_walkforward (inclusive)
M_LO, M_HI = 9 * 60, 24 * 60 + 30


def cents(d: dict | None, k: str):
    if not d or d.get(k) in (None, ""):
        return None
    try:
        return int(round(float(d[k]) * 100))
    except (TypeError, ValueError):
        return None


def load(ticker: str, midnight: int):
    f = KD / f"{ticker}.json"
    if not f.exists():
        return None
    try:
        raw = json.loads(f.read_text())
    except Exception:
        return None
    out = []
    for c in raw:
        m = (int(c["end_period_ts"]) - midnight) // 60
        if not (M_LO <= m <= M_HI):
            continue
        a, b = c.get("yes_ask") or {}, c.get("yes_bid") or {}
        p = c.get("price") or {}
        ac, bc = cents(a, "close_dollars"), cents(b, "close_dollars")
        if ac is None or bc is None:
            continue
        v = num(c.get("volume_fp")) or 0.0
        out.append((m, ac, bc, cents(a, "low_dollars"), cents(b, "high_dollars"), cents(p, "low_dollars"),
                    cents(p, "high_dollars"), cents(p, "close_dollars"), round(v, 2)))
    out.sort()
    return out


def build():
    M = json.load(open(KD / "markets.json"))
    days = defaultdict(list)
    for t, m in M.items():
        if m.get("result") not in ("yes", "no") or m["series"] not in SERIES:
            continue
        stn, icao, tzn = SERIES[m["series"]]
        close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        day = (close.astimezone(zoneinfo.ZoneInfo(tzn)) - dt.timedelta(hours=6)).date().isoformat()
        days[(stn, day)].append(m)
    out = []
    for (stn, day), mk in sorted(days.items()):
        tzn = next(v[2] for v in SERIES.values() if v[0] == stn)
        tz = zoneinfo.ZoneInfo(tzn); d = dt.date.fromisoformat(day)
        midnight = int(dt.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())
        rows = []
        for m in mk:
            cd = load(m["ticker"], midnight)
            if not cd:
                continue
            lo, hi = interval(m)
            rows.append({"tk": m["ticker"], "lo": lo, "hi": hi, "won": m["result"] == "yes", "cd": cd,
                         "vol": num(m.get("volume_fp")) or 0.0,
                         "close": int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())})
        if not rows or sum(r["won"] for r in rows) != 1:
            continue
        close = max(r["close"] for r in rows)
        out.append({"stn": stn, "series": mk[0]["series"], "day": day, "close": close, "mk": rows,
                    "part": "disc" if (close, stn) <= CUT_KEY else "val",
                    "prints": any(x[5] is not None for r in rows for x in r["cd"]) or any(x[3] is not None for r in rows for x in r["cd"])})
    out.sort(key=lambda r: (r["close"], r["stn"]))
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / "days.pkl.gz", "wb") as fh:
        pickle.dump(out, fh, protocol=pickle.HIGHEST_PROTOCOL)
    summ = defaultdict(int)
    for r in out:
        summ[(r["part"], r["day"][:7], r["prints"])] += 1
    print(len(out), "station-days")
    for k in sorted(summ):
        print(k, summ[k])


if __name__ == "__main__":
    build()
