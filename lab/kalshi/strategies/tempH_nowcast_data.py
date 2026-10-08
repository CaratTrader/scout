"""Data for the tempH_nowcast study (hourly city-temperature markets KXTEMPMIAH / KXTEMPNYCHS / KXTEMPCHIHS / KXTEMPLAXHS).

Settlement (rules_primary, fetched 2026-10-08): "temperature recorded at <city> ... at H PM as reported by Synoptic Data,
calculated in accordance with the Kalshi Weather Index Methodology". The index is public at minute resolution:
GET /live_data/weather/{city} (no key), an equal-weight mean of HF-ASOS 1-minute station readings:
  miami      KFLL KFXE KMIA KOPF KPMP (5)
  nyc        KCDW KEWR KFRG KHPN KISP KLGA KSMQ KTEB (8)
  chicago    / la-coastal: see stations.json
Readings arrive ~2-3 min after the event minute (received_at_ms), so a live trader sees the index with a few minutes' lag.

This module caches, under data/kalshi_lab/strategies/tempH_nowcast/:
  index_<city>.json      {minute_ts: value} for 2026-09-23..now (3-day windows, one Kalshi call each)
  stations_<city>.json   one detailed sample (member stations, receipt lags)
  settled_values.json    settled markets with expiration_value (fetched once by hand, same endpoint as fetch.py)
Usage: python -m lab.kalshi.strategies.tempH_nowcast_data"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/tempH_nowcast")
CITY = {"KXTEMPMIAH": "miami", "KXTEMPNYCHS": "nyc", "KXTEMPCHIHS": "chicago", "KXTEMPLAXHS": "la-coastal"}
START = 1790121600          # 2026-09-23 00:00 UTC (first tempH close in the lab data is 09-24 00:00)
CHUNK = 3 * 86400


def index_history(city: str, end: int | None = None) -> dict[int, float]:
    f = OUT / f"index_{city}.json"
    have = {int(k): v for k, v in json.loads(f.read_text()).items()} if f.exists() else {}
    end = end or int(time.time()) - 600
    a = (max(have) + 60) if have else START
    calls = 0
    while a < end:
        b = min(a + CHUNK, end)
        txt = fetch(f"{K}/live_data/weather/{city}?from={a * 1000}&to={b * 1000}", pace=1.15); calls += 1
        d = json.loads(txt or "{}")
        for p in d.get("timeseries") or []:
            if p.get("v") is not None:
                have[p["t"] // 1000] = p["v"]
        a = b + 60
    f.write_text(json.dumps({str(k): have[k] for k in sorted(have)}))
    print(city, len(have), "points,", calls, "calls", flush=True)
    return have


def stations(city: str) -> None:
    f = OUT / f"stations_{city}.json"
    if f.exists():
        return
    now = int(time.time())
    txt = fetch(f"{K}/live_data/weather/{city}?from={(now - 1800) * 1000}&to={(now - 600) * 1000}&detailed=true", pace=1.15)
    f.write_text(txt or "{}")


def load_index(city: str) -> dict[int, float]:
    return {int(k): v for k, v in json.loads((OUT / f"index_{city}.json").read_text()).items()}


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for c in CITY.values():
        stations(c)
        index_history(c)
