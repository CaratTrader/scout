"""Weather observed-max lock: bucket parsing, unit rounding, day filtering, decision rule, event parsing."""
from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scout import weather_lock as w


def test_parse_bucket_and_unit():
    assert w.parse_bucket("77°F or below") == (-math.inf, 77.0)
    assert w.parse_bucket("78-79°F") == (78.0, 79.0)
    assert w.parse_bucket("33°C") == (33.0, 33.0)
    assert w.parse_bucket("35°C or higher") == (35.0, math.inf)
    assert w.parse_bucket("") is None
    assert w.bucket_unit(["77°F or below", "78-79°F"]) == "F"
    assert w.bucket_unit(["28°C or below", "29°C"]) == "C"


def test_reading_in_unit_rounds_like_noaa():
    assert w.reading_in_unit(25.6, "F") == 78      # 78.08
    assert w.reading_in_unit(28.9, "F") == 84      # 84.02
    assert w.reading_in_unit(29.72, "F") == 85     # 85.5 -> half up
    assert w.reading_in_unit(29.0, "C") == 29
    assert w.reading_in_unit(29.5, "C") == 30


def test_day_readings_filters_to_local_date():
    tz = ZoneInfo("America/New_York")
    obs = [
        {"reportTime": "2026-09-12T21:00:00.000Z", "temp": 28.9},  # 17:00 local Sep 12
        {"reportTime": "2026-09-13T02:00:00.000Z", "temp": 25.6},  # 22:00 local Sep 12
        {"reportTime": "2026-09-13T05:00:00.000Z", "temp": 24.0},  # 01:00 local Sep 13 -> excluded
    ]
    rows = w.day_readings(obs, tz, date(2026, 9, 12), "F")
    assert [v for _, v in rows] == [84, 78]
    assert rows[0][0].hour == 17


def test_decide_rule():
    tz = ZoneInfo("America/New_York")
    d = lambda h, m=0: datetime(2026, 9, 12, h, m, tzinfo=tz)
    readings = [(d(13), 82), (d(14), 84), (d(15), 85), (d(16), 84), (d(17), 82)]
    # before lock hour
    assert w.decide(readings, d(16, 30), lock_hour=17, margin=2, min_gap_min=60)["buy"] is None
    # locked: 17:30, latest 82 <= 85-2, max at 15:00 is 150 min old
    v = w.decide(readings, d(17, 30), lock_hour=17, margin=2, min_gap_min=60)
    assert v["buy"] == 85 and v["why"] == "locked" and v["t_max"] == "15:00"
    # still hot: latest within the margin
    hot = readings + [(d(17, 30), 84)]
    assert "within" in w.decide(hot, d(17, 45), lock_hour=17, margin=2, min_gap_min=60)["why"]
    # max too recent
    fresh = [(d(16, 50), 86), (d(17, 20), 83)]
    assert w.decide(fresh, d(17, 30), lock_hour=17, margin=2, min_gap_min=60)["why"] == "max too recent"
    # stale observations
    assert w.decide(readings, d(20, 0), lock_hour=17, margin=2, min_gap_min=60)["why"] == "stale observations"
    assert w.decide([], d(18), lock_hour=17, margin=2, min_gap_min=60)["buy"] is None


def test_market_for_value_and_parse_event():
    markets = [
        {"id": "1", "groupItemTitle": "77°F or below", "description": "x", "closed": False},
        {"id": "2", "groupItemTitle": "78-79°F", "description": "x", "closed": False},
        {"id": "3", "groupItemTitle": "80-81°F", "description": "resolution source ... https://www.weather.gov/wrh/timeseries?site=katl ...", "closed": False},
    ]
    assert w.market_for_value(markets, 79)["id"] == "2"
    assert w.market_for_value(markets, 70)["id"] == "1"
    assert w.market_for_value(markets, 99) is None
    ev = {"slug": "highest-temperature-in-atlanta-on-september-12-2026", "id": "e1", "markets": [markets[2]]}
    info = w.parse_event(ev)
    assert info and info["city"] == "atlanta" and info["station"] == "KATL" and info["unit"] == "F" and info["date"] == date(2026, 9, 12)
    # Hong Kong resolves on the HK Observatory, not NOAA -> not tradeable with the METAR feed
    hk = {"slug": "highest-temperature-in-hong-kong-on-september-12-2026", "markets": [{"id": "9", "groupItemTitle": "33°C", "description": "Hong Kong Observatory daily extract", "closed": False}]}
    assert w.parse_event(hk) is None
    assert w.parse_event({"slug": "lowest-temperature-in-nyc-on-september-12-2026", "markets": markets}) is None
