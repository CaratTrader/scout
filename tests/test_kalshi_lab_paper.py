import datetime as dt
import zoneinfo

from scout import kalshi_lab_paper as L


def test_precip_groups():
    assert L.precip_in("KATL 071853Z 18008KT 10SM -RA BKN040 22/18 A3001 RMK AO2 RAB25 SLP160 P0012 T02170183") == 0.12
    assert L.precip_in("KATL 071753Z 18008KT 10SM BKN040 22/18 A3001 RMK AO2 SLP160 60015 T02170183 10233 20200 53010") == 0.15
    assert L.precip_in("KATL 071853Z 18008KT 10SM -RA BKN040 22/18 A3001 RMK AO2 SLP160 P0000 T02170183") == 0.0   # trace
    assert L.precip_in("KDEN 021853Z 36020G35KT 10SM FEW080 29/02 A3001 RMK AO2 PK WND 60035/1832 SLP120 T02890022") is None
    assert L.precip_in("KATL 071853Z 18008KT 10SM BKN040 22/18 A3001") is None


def _row(t, raw):
    return {"obsTime": t.timestamp(), "rawOb": raw}


def test_rain_state_ignores_groups_that_start_before_the_climate_day():
    tz = zoneinfo.ZoneInfo("America/New_York"); utc = dt.timezone.utc
    # climate day 2026-10-07 starts 01:00 EDT = 05:00Z; the 05:53Z P-group covers 04:53-05:53Z, partly the previous day
    rows = [_row(dt.datetime(2026, 10, 7, 5, 53, tzinfo=utc), "KATL 070553Z 18008KT 10SM -RA OVC040 18/17 A3001 RMK AO2 P0004"),
            _row(dt.datetime(2026, 10, 7, 12, 53, tzinfo=utc), "KATL 071253Z 18008KT 10SM BKN040 20/17 A3001 RMK AO2"),
            _row(dt.datetime(2026, 10, 7, 13, 53, tzinfo=utc), "KATL 071353Z 18008KT 10SM BKN040 21/17 A3001 RMK AO2"),
            _row(dt.datetime(2026, 10, 7, 14, 53, tzinfo=utc), "KATL 071453Z 18008KT 10SM BKN040 22/17 A3001 RMK AO2")]
    s = L.rain_state(rows, tz, dt.datetime(2026, 10, 7, 19, 0, tzinfo=utc))
    assert s["day"] == dt.date(2026, 10, 7) and not s["measurable"] and s["recent_wet"] == [False, False, False]
    rows.append(_row(dt.datetime(2026, 10, 7, 15, 53, tzinfo=utc), "KATL 071553Z 18008KT 10SM -SHRA BKN040 22/17 A3001 RMK AO2 P0001"))
    s = L.rain_state(rows, tz, dt.datetime(2026, 10, 7, 19, 0, tzinfo=utc))
    assert s["measurable"] and s["recent_wet"][-1]


def test_stake_is_quarter_kelly_capped():
    led = {"cash": 50.0, "positions": []}
    s = L.stake_for(led, 0.72, 0.81)   # q = 0.765 -> f* = 0.161 -> quarter 0.040 -> $2.01
    assert 1.9 < s < 2.1
    assert L.stake_for(led, 0.50, 0.99) == 5.0   # capped at max_stake
    assert L.stake_for(led, 0.80, 0.75) == 0.0   # no edge, no bet


def test_halts():
    led = {"streak": 3, "fills": []}
    assert "3 losses" in L.halted(led)
    today = dt.date.today().isoformat()
    led = {"streak": 0, "fills": [{"pnl": -6.0, "settled": today + "T12:00"}, {"pnl": -4.5, "settled": today + "T13:00"}]}
    assert "daily loss" in L.halted(led)
    assert L.halted({"streak": 1, "fills": [{"pnl": 2.0, "settled": "2026-10-01T00:00"}]}) is None
