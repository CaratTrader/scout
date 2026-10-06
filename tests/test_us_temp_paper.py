import datetime as dt, zoneinfo
from scout import us_temp_paper as U

TZ = zoneinfo.ZoneInfo("America/Los_Angeles")

def test_bounds_follow_venue_convention():
    assert U.bounds("tc-temp-sfohigh-2026-09-18-gte68lt69f") == (68.0, 69.0)
    assert U.bounds("tc-temp-sfohigh-2026-09-18-lt68f") == (-1e9, 67.0)
    assert U.bounds("tc-temp-sfohigh-2026-09-18-gte76f") == (76.0, 1e9)

def test_metar_parsing_uses_tenths_and_six_hour_max():
    raw = "METAR KSFO 222356Z 28010KT 10SM FEW008 20/12 A2998 RMK AO2 SLP151 T02000117 10211 20144 53012"
    f = U.metar_temps_f(raw)
    assert round(f[0], 1) == 68.0 and round(f[1], 1) == 70.0   # 20.0C now, 6-hr max 21.1C

def test_observed_drops_pre_one_am_and_tracks_max(monkeypatch):
    now = dt.datetime(2026, 9, 22, 22, 0, tzinfo=dt.timezone.utc)  # 15:00 PDT
    rows = [
        {"reportTime": "2026-09-22T07:53:00Z", "rawOb": "KSFO 220753Z 00000KT 10SM 25/10 A3000 RMK AO2 T02500100"},  # 00:53 PDT -> previous climate day
        {"reportTime": "2026-09-22T19:56:00Z", "rawOb": "KSFO 221956Z 00000KT 10SM 20/10 A3000 RMK AO2 T02000100"},  # 12:56 -> 68.0F
        {"reportTime": "2026-09-22T21:56:00Z", "rawOb": "KSFO 222156Z 00000KT 10SM 18/10 A3000 RMK AO2 T01830100"},  # 14:56 -> 64.9F
    ]
    ob = U.observed("KSFO", TZ, now, fetch=lambda: rows)
    assert round(ob["max"], 1) == 68.0 and round(ob["latest"], 1) == 64.9 and ob["t_max"].hour == 12

def test_signals_r0_and_r2():
    ob = {"max": 68.0, "latest": 64.9, "t_max": dt.datetime(2026, 9, 22, 12, 56, tzinfo=TZ), "latest_ts": None}
    now_local = dt.datetime(2026, 9, 22, 15, 30, tzinfo=TZ)
    buckets = [
        {"slug": "x-lt68f", "bid": 0.10, "ask": 0.12, "bid_sz": 500, "ask_sz": 500},          # dead (<=67 < 68) -> NO at 0.90
        {"slug": "x-gte68lt69f", "bid": 0.40, "ask": 0.45, "bid_sz": 500, "ask_sz": 500},     # winner candidate (R1 off)
        {"slug": "x-gte70lt71f", "bid": 0.30, "ask": 0.35, "bid_sz": 500, "ask_sz": 500},     # floor 70 < 68+3 -> no fade
        {"slug": "x-gte72lt73f", "bid": 0.20, "ask": 0.25, "bid_sz": 400, "ask_sz": 500},     # floor 72 >= 71 -> fade NO at 0.80
        {"slug": "x-gte76f", "bid": 0.01, "ask": 0.02, "bid_sz": 10, "ask_sz": 500},           # bid < 0.15 -> nothing
    ]
    s = U.signals(buckets, ob, now_local)
    assert [(c["rule"], c["slug"], c["side"], c["px"]) for c in s] == [("R0", "x-lt68f", "NO", 0.9), ("R2", "x-gte72lt73f", "NO", 0.8)]
    # before the peak window nothing but R0
    s2 = U.signals(buckets, ob, now_local.replace(hour=14))
    assert [c["rule"] for c in s2] == ["R0"]
    # top bucket certain once the max reaches its floor
    ob2 = dict(ob, max=76.2)
    assert any(c["rule"] == "R0" and c["side"] == "YES" and c["slug"] == "x-gte76f" for c in U.signals([{"slug": "x-gte76f", "bid": 0.9, "ask": 0.95, "bid_sz": 100, "ask_sz": 100}], ob2, now_local))

def test_confirmation_and_fill_and_settlement(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "JOURNAL", tmp_path / "j.jsonl"); monkeypatch.setattr(U, "LEDGER", tmp_path / "l.json")
    led = U.load_ledger(); c = {"rule": "R0", "slug": "x-lt68f", "side": "NO", "px": 0.90, "size": 20, "why": "t"}
    meta = {"city": "sfo", "day": "2026-09-22", "end_date": "2026-09-23T12:00:00Z"}
    U.start_poll(led)
    assert U.confirm_and_fill(led, [c], meta) == []                      # first sighting only pends
    U.finish_poll(led); U.start_poll(led)
    fills = U.confirm_and_fill(led, [dict(c, px=0.89)], meta)            # second consecutive sighting, not worse -> fill at 0.89
    assert len(fills) == 1 and fills[0]["shares"] == 20 and abs(led["cash"] - (500 - 17.8 - U.fee(0.89, 20))) < 1e-6
    monkeypatch.setattr(U, "get", lambda url, timeout=20, **kw: {"settlement": 0})   # YES settled 0 -> NO pays 1
    monkeypatch.setattr(U.dt, "datetime", type("D", (dt.datetime,), {"now": classmethod(lambda cls, tz=None: dt.datetime(2026, 9, 24, tzinfo=tz))}))
    done = U.settle(led)
    assert len(done) == 1 and done[0]["won"] and abs(done[0]["pnl"] - (20 - 17.8 - U.fee(0.89, 20))) < 1e-6 and not led["positions"]


def test_six_hour_max_before_seven_local_is_ignored():
    # 06Z group at 01:00 CDT covers 19:00-01:00 of the previous climate day -> must not set today's max
    now = dt.datetime(2026, 9, 22, 17, 0, tzinfo=dt.timezone.utc)  # 12:00 CDT
    tz = zoneinfo.ZoneInfo("America/Chicago")
    rows = [
        {"reportTime": "2026-09-22T06:00:00Z", "rawOb": "KMDW 220600Z 00000KT 10SM 16/10 A3000 RMK AO2 T01560100 10178 20150"},  # 01:00 CDT, 6-hr max 64.0F
        {"reportTime": "2026-09-22T12:00:00Z", "rawOb": "KMDW 221200Z 00000KT 10SM 14/10 A3000 RMK AO2 T01390100 10156 20139"},  # 07:00 CDT, 6-hr max 60.1F (01:00-07:00: inside)
        {"reportTime": "2026-09-22T15:00:00Z", "rawOb": "KMDW 221500Z 00000KT 10SM 15/10 A3000 RMK AO2 T01500100"},
    ]
    ob = U.observed("KMDW", tz, now, fetch=lambda: rows)
    assert round(ob["max"], 1) == 60.1 and round(ob["latest"], 1) == 59.0


def test_r1x_after_00z_buys_the_max_bucket_and_fades_the_rest():
    tz = zoneinfo.ZoneInfo("America/Los_Angeles")
    ob = {"max": 68.0, "latest": 64.0, "t_max": dt.datetime(2026, 9, 22, 14, 56, tzinfo=tz), "has_00z": True}
    buckets = [
        {"slug": "x-gte68lt69f", "bid": 0.40, "ask": 0.55, "bid_sz": 900, "ask_sz": 700},   # holds 68 -> YES at 0.55
        {"slug": "x-gte70lt71f", "bid": 0.45, "ask": 0.50, "bid_sz": 800, "ask_sz": 800},   # outside -> NO at 0.55
        {"slug": "x-gte72lt73f", "bid": 0.05, "ask": 0.08, "bid_sz": 800, "ask_sz": 800},   # bid < 0.10 -> nothing
    ]
    before = U.signals(buckets, ob, dt.datetime(2026, 9, 22, 16, 30, tzinfo=tz), city="sfo")   # before 17:00 local: no R1x
    assert not [c for c in before if c["rule"] == "R1x"]
    after = U.signals(buckets, ob, dt.datetime(2026, 9, 22, 17, 5, tzinfo=tz), city="sfo")
    assert [(c["rule"], c["slug"], c["side"], c["px"]) for c in after] == [("R1x", "x-gte68lt69f", "YES", 0.55), ("R1x", "x-gte70lt71f", "NO", 0.55)]
    ob2 = dict(ob, has_00z=False)   # the 00Z report has not arrived yet -> no R1x even after 17:00
    assert not [c for c in U.signals(buckets, ob2, dt.datetime(2026, 9, 22, 17, 5, tzinfo=tz), city="sfo") if c["rule"] == "R1x"]


def test_lone_spike_does_not_set_the_max():
    tz = zoneinfo.ZoneInfo("America/New_York"); now = dt.datetime(2026, 8, 27, 20, 30, tzinfo=dt.timezone.utc)
    rows = [{"reportTime": f"2026-08-27T{h:02d}:51:00Z", "rawOb": f"KNYC 27{h:02d}51Z AUTO 10SM 24/20 A3000 RMK AO2 T0{c}0200"} for h, c in ((17, "244"), (18, "250"), (20, "244"))]
    rows.append({"reportTime": "2026-08-27T19:51:00Z", "rawOb": "KNYC 271951Z AUTO 1/2SM +RA 27/21 A3003 RMK AO2 T02670211 $"})   # 80.1F spike
    ob = U.observed("KNYC", tz, now, fetch=lambda: rows)
    assert round(ob["max"], 1) == 77.0


def test_six_hour_group_far_above_hourly_readings_is_ignored():
    tz = zoneinfo.ZoneInfo("America/New_York"); now = dt.datetime(2026, 8, 28, 0, 30, tzinfo=dt.timezone.utc)  # 20:30 EDT
    rows = [{"reportTime": f"2026-08-27T{h:02d}:51:00Z", "rawOb": f"KNYC 27{h:02d}51Z AUTO 10SM 24/20 A3000 RMK AO2 T02500200"} for h in (18, 19, 20, 21, 22)]  # 77.0F all afternoon
    rows.append({"reportTime": "2026-08-27T23:51:00Z", "rawOb": "KNYC 272351Z AUTO 6SM BR 22/22 A2997 RMK AO2 T02220217 10272 20211 $"})  # 00Z group 81F: artefact
    ob = U.observed("KNYC", tz, now, fetch=lambda: rows)
    assert round(ob["max"], 1) == 77.0 and ob["has_00z"] is False


def test_evaluate_explains_every_bucket():
    tz = zoneinfo.ZoneInfo("America/Los_Angeles")
    ob = {"max": 68.0, "latest": 64.9, "t_max": dt.datetime(2026, 9, 22, 12, 56, tzinfo=tz), "has_00z": False}
    buckets = [
        {"slug": "x-lt68f", "bid": None, "ask": 0.01, "bid_sz": 0, "ask_sz": 900},
        {"slug": "x-gte68lt69f", "bid": 0.40, "ask": 0.55, "bid_sz": 900, "ask_sz": 700},
        {"slug": "x-gte70lt71f", "bid": 0.30, "ask": 0.35, "bid_sz": 500, "ask_sz": 500},
        {"slug": "x-gte72lt73f", "bid": 0.05, "ask": 0.08, "bid_sz": 400, "ask_sz": 500},
    ]
    ev = U.evaluate(buckets, ob, dt.datetime(2026, 9, 22, 15, 30, tzinfo=tz), city="sfo")
    by = {r["slug"]: r for r in ev["rows"]}
    assert ev["flags"]["peak_passed"] and ev["flags"]["after_00z"] is False
    assert by["x-lt68f"]["status"].startswith("dead") and by["x-lt68f"]["blocker"] == "R0: no bid to sell into"
    assert by["x-gte68lt69f"]["status"] == "holds the max" and "00Z" in by["x-gte68lt69f"]["blocker"]
    assert by["x-gte70lt71f"]["blocker"].startswith("above the max by only 2F")
    assert by["x-gte72lt73f"]["status"] == "above the max by 4F" and by["x-gte72lt73f"]["blocker"].startswith("R2: bid 0.05")
    assert not any(r["candidate"] for r in ev["rows"])


def test_iem_fallback_when_awc_fails(monkeypatch):
    tz = zoneinfo.ZoneInfo("America/Chicago")
    monkeypatch.setattr(U, "get", lambda url, timeout=20: None)   # aviationweather.gov down
    csv_text = "station,valid,metar\nMDW,2026-09-23 14:53,KMDW 231453Z 05013KT 10SM 17/09 A3027 RMK AO2 T01720094\n"
    class R:
        def __init__(self, t): self.t = t
        def read(self): return self.t.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(U.urllib.request, "urlopen", lambda req, timeout=40: R(csv_text))
    monkeypatch.setattr(U, "journal", lambda ev: None)
    rows = U.fetch_metars("KMDW", tz)
    assert rows == [{"reportTime": "2026-09-23T14:53:00Z", "rawOb": "KMDW 231453Z 05013KT 10SM 17/09 A3027 RMK AO2 T01720094"}]


CLI_TEXT = """CLIMATE REPORT
NATIONAL WEATHER SERVICE MIAMI,FL
428 PM EDT SAT AUG 15 2026

...THE MIAMI CLIMATE SUMMARY FOR AUGUST 15 2026...
VALID TODAY AS OF 0400 PM LOCAL TIME.

TEMPERATURE (F)
 TODAY
  MAXIMUM         93   2:57 PM  98    2024  91      2       93
  MINIMUM         80  12:32 AM  69    1920  78      2       81
"""


def test_parse_cli_intraday_report():
    r = U.parse_cli(CLI_TEXT)
    assert r == {"day": "2026-08-15", "asof_min": 16 * 60, "max": 93.0, "max_time": "2:57 PM"}
    assert U.parse_cli(CLI_TEXT.replace("VALID TODAY", "VALID YESTERDAY")) is None   # the final report is not intraday


def test_cli_report_raises_the_observed_max_and_can_open_the_window(monkeypatch):
    tz = zoneinfo.ZoneInfo("America/New_York")
    ob = {"max": 91.0, "latest": 88.0, "t_max": dt.datetime(2026, 8, 15, 14, 0, tzinfo=tz), "has_00z": False, "cli": {"day": "2026-08-15", "asof_min": 960, "max": 93.0}}
    buckets = [{"slug": "x-gte93lt94f", "bid": 0.30, "ask": 0.35, "bid_sz": 500, "ask_sz": 500}, {"slug": "x-gte91lt92f", "bid": 0.55, "ask": 0.60, "bid_sz": 500, "ask_sz": 500}]
    now_local = dt.datetime(2026, 8, 15, 16, 30, tzinfo=tz)
    off = dict(U.CFG, cli_trigger=0); on = dict(U.CFG, cli_trigger=1)
    assert not [c for c in U.signals(buckets, ob, now_local, cfg=off, city="mia") if c["rule"] == "R1x"]
    ob2 = dict(ob, max=93.0)   # poll() lifts the max to the report's value before evaluate()
    s = U.signals(buckets, ob2, now_local, cfg=on, city="mia")
    assert [(c["rule"], c["slug"], c["side"]) for c in s] == [("R1x", "x-gte93lt94f", "YES"), ("R1x", "x-gte91lt92f", "NO")]


def test_climate_day_starts_at_midnight_where_there_is_no_daylight_saving():
    tz = zoneinfo.ZoneInfo("America/Phoenix"); now = dt.datetime(2026, 9, 27, 20, 0, tzinfo=dt.timezone.utc)  # 13:00 MST
    rows = [{"reportTime": "2026-09-27T07:30:00Z", "rawOb": "KPHX 270730Z 00000KT 10SM 33/10 A2990 RMK AO2 T03300100"},   # 00:30 MST: inside the Phoenix climate day
            {"reportTime": "2026-09-27T18:51:00Z", "rawOb": "KPHX 271851Z 00000KT 10SM 31/10 A2990 RMK AO2 T03110100"}]
    ob = U.observed("KPHX", tz, now, fetch=lambda: rows)
    assert round(ob["max"], 1) == 91.4   # the 00:30 reading counts in Phoenix
    rows_cdt = [{"reportTime": "2026-09-27T05:30:00Z", "rawOb": "KMDW 270530Z 00000KT 10SM 33/10 A2990 RMK AO2 T03300100"},   # 00:30 CDT: previous climate day
                {"reportTime": "2026-09-27T18:51:00Z", "rawOb": "KMDW 271851Z 00000KT 10SM 31/10 A2990 RMK AO2 T03110100"}]
    ob2 = U.observed("KMDW", zoneinfo.ZoneInfo("America/Chicago"), now, fetch=lambda: rows_cdt)  # a DST zone: the 00:30 reading is excluded
    assert round(ob2["max"], 1) == 88.0


def test_certain_rule_uses_metar_only_max_with_a_full_degree_margin():
    tz = zoneinfo.ZoneInfo("America/Chicago")
    ob = {"max": 97.0, "max_obs": 96.8, "latest": 95.0, "t_max": dt.datetime(2026, 9, 20, 15, 47, tzinfo=tz), "has_00z": False}
    buckets = [{"slug": "x-lt97f", "bid": 0.06, "ask": 0.08, "bid_sz": 500, "ask_sz": 500}]   # <= 96 bucket
    assert not [c for c in U.signals(buckets, ob, dt.datetime(2026, 9, 20, 16, 0, tzinfo=tz), city="dfw") if c["rule"] == "R0"]   # 96.8 is not a full degree above 96
    ob2 = dict(ob, max_obs=97.2)
    assert [c["rule"] for c in U.signals(buckets, ob2, dt.datetime(2026, 9, 20, 16, 0, tzinfo=tz), city="dfw")] == ["R0"]


def test_report_only_counts_for_allowed_cities_and_afternoon_issuances(monkeypatch):
    U._CLI_CACHE.clear()
    rep = {"day": "2026-09-27", "asof_min": 16 * 60, "max": 90.0, "max_time": None}
    assert U.cli_intraday("nyc", "2026-09-27", fetch=lambda: rep) == rep
    U._CLI_CACHE.clear(); assert U.cli_intraday("den", "2026-09-27", fetch=lambda: rep) is None            # no afternoon report office
    U._CLI_CACHE.clear(); assert U.cli_intraday("nyc", "2026-09-27", fetch=lambda: dict(rep, asof_min=6 * 60)) is None   # a morning issuance
    U._CLI_CACHE.clear()


def test_six_hour_group_kept_when_reports_arrive_newest_first():
    # aviationweather.gov order: newest first. The 00Z group (73.9F) must be checked against the whole afternoon.
    tz = zoneinfo.ZoneInfo("America/New_York"); now = dt.datetime(2026, 10, 1, 3, 20, tzinfo=dt.timezone.utc)
    rows = [{"reportTime": "2026-10-01T00:00:00.000Z", "rawOb": "METAR KNYC 302351Z AUTO 00000KT 10SM CLR 19/16 A3012 RMK AO2 SLP189 T01890161 10233 20189 53013"}]
    for h, t in ((23, "02000161"), (22, "02110161"), (21, "02280161"), (20, "02220156"), (19, "02220156"), (18, "02220150")):
        rows.append({"reportTime": f"2026-09-30T{h:02d}:00:00.000Z", "rawOb": f"METAR KNYC 30{h-1:02d}51Z AUTO 10SM CLR 22/16 A3012 RMK AO2 T{t}"})
    ob = U.observed("KNYC", tz, now, fetch=lambda: rows)
    assert round(ob["max"], 1) == 73.9 and ob["has_00z"] is True



# ---------------------------------------------------------------- 2026-10-01 review fixes
def _led(monkeypatch, tmp_path):
    monkeypatch.setattr(U, "JOURNAL", tmp_path / "j.jsonl"); monkeypatch.setattr(U, "LEDGER", tmp_path / "l.json")
    return U.load_ledger()


def test_two_simultaneous_candidates_both_fill(monkeypatch, tmp_path):
    led = _led(monkeypatch, tmp_path)
    a = {"rule": "R2", "slug": "x-gte72lt73f", "side": "NO", "px": 0.80, "size": 100, "why": "a"}
    b = {"rule": "R0", "slug": "y-lt68f", "side": "NO", "px": 0.90, "size": 100, "why": "b"}
    for _ in range(2):
        U.start_poll(led)
        fills = U.confirm_and_fill(led, [a], {"city": "atl", "day": "d"}) + U.confirm_and_fill(led, [b], {"city": "den", "day": "d"})
        U.finish_poll(led)
    assert len(fills) == 2 and {f["city"] for f in fills} == {"atl", "den"}


def test_gap_restarts_the_confirmation(monkeypatch, tmp_path):
    led = _led(monkeypatch, tmp_path)
    c = {"rule": "R2", "slug": "x-gte72lt73f", "side": "NO", "px": 0.80, "size": 100, "why": "a"}
    U.start_poll(led); U.confirm_and_fill(led, [c], {"day": "d"}); U.finish_poll(led)   # seen
    U.start_poll(led); U.finish_poll(led)                                                 # absent: pending dropped
    assert led["pending"] == {}
    U.start_poll(led); assert U.confirm_and_fill(led, [c], {"day": "d"}) == []; U.finish_poll(led)   # seen again: counts as first
    U.start_poll(led); assert len(U.confirm_and_fill(led, [c], {"day": "d"})) == 1


def test_legacy_pending_without_poll_id_is_pruned(monkeypatch, tmp_path):
    led = _led(monkeypatch, tmp_path); led["pending"] = {"x-gte87lt88f|NO": {"n": 1, "px": 0.08, "rule": "R1x"}}
    U.start_poll(led); U.finish_poll(led)
    assert led["pending"] == {}


def test_single_poll_confirmation_and_unreadable_book(monkeypatch, tmp_path):
    led = _led(monkeypatch, tmp_path)
    c = {"rule": "R0", "slug": "y-lt68f", "side": "NO", "px": 0.90, "size": 100, "why": "b"}
    U.start_poll(led)
    assert len(U.confirm_and_fill(led, [c], {"day": "d"}, cfg=dict(U.CFG, confirm=1))) == 1
    led2 = {"cash": 500.0, "positions": [], "fills": [], "pending": {}}
    U.start_poll(led2); U.confirm_and_fill(led2, [dict(c, size=None)], {"day": "d"}); U.finish_poll(led2)
    U.start_poll(led2); assert U.confirm_and_fill(led2, [dict(c, size=None)], {"day": "d"}) == []   # book unreadable: no fill
    assert led2["pending"]["y-lt68f|NO"]["n"] == 2                                              # confirmation kept for next poll


def test_negative_strikes_and_rounding():
    assert U.bounds("k-KXHIGHTMIN-27JAN15-T-3-lt-3f") == (-1e9, -4.0)
    assert U.bounds("k-KXHIGHTMIN-27JAN15-B-1.5-gte-2lt-1f") == (-2.0, -1.0)
    assert U.bounds("k-KXHIGHTMIN-27JAN15-T4-gte5f") == (5.0, 1e9)
    assert U.bounds("k-KXHIGHTMIN-27JAN15-T-1-gte0f") == (0.0, 1e9)
    assert U.bounds("tc-temp-sfohigh-2026-09-18-gte68lt69f") == (68.0, 69.0)
    assert U.round_f(-0.6) == -1 and U.round_f(-0.5) == 0 and U.round_f(72.5) == 73 and U.round_f(72.4) == 72


def test_iem_fallback_url_covers_now_on_month_ends():
    import re as _re
    for now in (dt.datetime(2026, 9, 28, 23, 0, tzinfo=dt.timezone.utc), dt.datetime(2026, 10, 31, 2, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 12, 31, 23, 30, tzinfo=dt.timezone.utc), dt.datetime(2027, 2, 28, 3, 0, tzinfo=dt.timezone.utc)):
        u = U.iem_url("KSFO", now)
        y2, m2, d2 = (int(_re.search(k + r"=(\d+)", u).group(1)) for k in ("year2", "month2", "day2"))
        y1, m1, d1 = (int(_re.search(k + r"=(\d+)", u).group(1)) for k in ("year1", "month1", "day1"))
        assert dt.date(y2, m2, d2) == (now + dt.timedelta(days=1)).date() and dt.date(y1, m1, d1) <= (now - dt.timedelta(hours=30)).date()


def test_peak_wind_group_is_not_a_temperature():
    raw = "KDEN 022353Z 31018G30KT 10SM FEW120 31/02 A3001 RMK AO2 PK WND 10037/2258 SLP110 T03110022 10311 20278 58010"
    f = U.metar_temps_f(raw)
    assert round(f[0], 1) == 88.0 and round(f[1], 1) == 88.0   # the 10311 group, not the PK WND 10037 token


def test_outage_needs_consecutive_failures_and_is_per_host(monkeypatch):
    ev = []; monkeypatch.setattr(U, "journal", lambda e: ev.append(e)); U._NET.clear()
    err = U.urllib.error.URLError(OSError(51, "Network is unreachable"))
    U.net_down("https://aviationweather.gov/a", err); U.net_up("https://aviationweather.gov/a")   # isolated blip
    assert [e["event"] for e in ev] == ["error"]
    ev.clear()
    for _ in range(6):
        U.net_down("https://aviationweather.gov/a", err)
    U.net_up("https://api.elections.kalshi.com/x")          # another host succeeding does not end the AWC outage
    U.net_up("https://aviationweather.gov/b")
    assert [e["event"] for e in ev] == ["error", "error", "outage_start", "outage_end"]
    assert ev[2]["host"] == "aviationweather.gov" and ev[3]["suppressed_errors"] == 3
    U._NET.clear()


def test_stale_outage_closed_on_restart(monkeypatch, tmp_path):
    j = tmp_path / "j.jsonl"; j.write_text('{"ts": 100, "event": "outage_start", "host": "aviationweather.gov", "since": 100}\n')
    monkeypatch.setattr(U, "JOURNAL", j)
    U.close_stale_outages(now=400)
    last = [l for l in j.read_text().splitlines()][-1]
    assert '"outage_end"' in last and '"process_restart"' in last and '"seconds": 300' in last


def test_confirmation_does_not_survive_a_long_gap(monkeypatch, tmp_path):
    led = _led(monkeypatch, tmp_path)
    c = {"rule": "R0", "slug": "y-lt68f", "side": "NO", "px": 0.90, "size": 100, "why": "b"}
    U.start_poll(led, now=1000.0); U.confirm_and_fill(led, [c], {"day": "d"}); U.finish_poll(led)
    U.start_poll(led, now=1000.0 + 5 * 3600)                  # 5 h later (reboot at the FileVault login)
    assert led["pending"] == {}
    assert U.confirm_and_fill(led, [c], {"day": "d"}) == []  # the first sighting after the gap only pends


def test_cash_check_includes_the_fee(monkeypatch, tmp_path):
    led = _led(monkeypatch, tmp_path); led["cash"] = 25.0
    c = {"rule": "R2", "slug": "x-gte72lt73f", "side": "NO", "px": 0.50, "size": 500, "why": "a"}
    U.start_poll(led); U.confirm_and_fill(led, [c], {"day": "d"}, cfg=dict(U.CFG, confirm=1))
    assert led["positions"] == [] and led["cash"] == 25.0


def test_blocker_texts_follow_the_configuration():
    tz = zoneinfo.ZoneInfo("America/New_York")
    ob = {"max": 70.0, "latest": 68.0, "t_max": dt.datetime(2026, 10, 1, 14, 0, tzinfo=tz), "has_00z": False}
    b = [{"slug": "x-gte70lt71f", "bid": 0.5, "ask": 0.6, "bid_sz": 10, "ask_sz": 10}, {"slug": "x-gte74lt75f", "bid": 0.3, "ask": 0.35, "bid_sz": 10, "ask_sz": 10}]
    now = dt.datetime(2026, 10, 1, 16, 0, tzinfo=tz)
    live = dict(U.CFG, r1x=1, r1x_00z=0, cli_trigger=0, r1=0, r2=0)
    rows = {r["slug"]: r for r in U.evaluate(b, ob, now, cfg=live, city="nyc")["rows"]}
    assert "00Z and report triggers off" in rows["x-gte70lt71f"]["blocker"] and rows["x-gte74lt75f"]["blocker"] == "above the max by 4F; R2 off"
    rows = {r["slug"]: r for r in U.evaluate(b, ob, now, cfg=dict(live, r1x=0), city="nyc")["rows"]}
    assert rows["x-gte70lt71f"]["blocker"].endswith("(R1x off)")
    rows = {r["slug"]: r for r in U.evaluate(b, ob, now, cfg=dict(live, r1x_00z=1), city="nyc")["rows"]}
    assert "00Z report (20:00 local)" in rows["x-gte70lt71f"]["blocker"]


def test_r2_measures_its_margin_from_the_unfiltered_max():
    tz = zoneinfo.ZoneInfo("America/Chicago")
    ob = {"max": 84.0, "max_raw": 87.1, "latest": 80.0, "t_max": dt.datetime(2026, 9, 8, 13, 0, tzinfo=tz), "has_00z": False}
    buckets = [{"slug": "x-gte87lt88f", "bid": 0.30, "ask": 0.35, "bid_sz": 100, "ask_sz": 100}]
    now = dt.datetime(2026, 9, 8, 16, 0, tzinfo=tz)
    assert U.evaluate(buckets, ob, now, cfg=dict(U.CFG, r2_max="metar"), city="mdw")["rows"][0]["rule"] == "R2"   # the old rule faded it
    ev = U.evaluate(buckets, ob, now, cfg=dict(U.CFG, r2_max="raw"), city="mdw")
    assert ev["rows"][0]["rule"] is None and "unfiltered max 87F" in ev["rows"][0]["blocker"] and ev["flags"]["r2_blocked"] == ["87-88"]


def _feat(ts, c, raw=None, qc="V"):
    return {"properties": {"timestamp": ts, "temperature": {"value": c, "qualityControl": qc}, "rawMessage": raw}}


def test_five_min_summary_reads_whole_c_rows_only():
    tz = zoneinfo.ZoneInfo("America/Los_Angeles")
    day_start = dt.datetime(2026, 10, 2, 1, 0, tzinfo=tz); now = dt.datetime(2026, 10, 2, 15, 0, tzinfo=tz)
    feats = [_feat("2026-10-02T19:53:00+00:00", 28.9, raw="KLAX 021953Z ..."),   # hourly METAR (tenths, rawMessage): skipped
             _feat("2026-10-02T20:10:00+00:00", 29), _feat("2026-10-02T20:15:00+00:00", 30), _feat("2026-10-02T20:45:00+00:00", 29),
             _feat("2026-10-02T21:05:00+00:00", 30), _feat("2026-10-02T21:10:00+00:00", 30),
             _feat("2026-10-02T21:20:00+00:00", 41, qc="X"),                      # rejected by NWS quality control
             _feat("2026-10-02T23:30:00+00:00", 35),                              # after `now`
             _feat("2026-10-02T07:30:00+00:00", 33)]                              # 00:30 local, before the climate day
    s = U.five_min_summary(feats, tz, now, day_start)
    assert s["n"] == 5 and s["max_mid"] == 86.0 and round(s["max_hi"], 1) == 86.9
    assert round(s["max_lo2"], 1) == 85.1                                       # 30C twice in a row (21:05, 21:10)
    assert s["t_max"] == dt.datetime(2026, 10, 2, 14, 10, tzinfo=tz)
    assert U.five_min_summary([_feat("2026-10-02T20:15:00+00:00", 30)], tz, now, day_start)["max_lo2"] is None   # one sample: no lower bound
    assert U.five_min_summary([], tz, now, day_start) == {"n": 0}


def test_lax_2026_10_02_fade_is_blocked_by_5_minute_data():
    """Hourly max 84.0 (raw 84.9 from the rejected 18Z group); 5-minute 30C. Official high 87: the 87-88 fade lost."""
    tz = zoneinfo.ZoneInfo("America/Los_Angeles")
    now = dt.datetime(2026, 10, 2, 15, 3, tzinfo=tz)
    ob = {"max": 84.0, "max_raw": 84.9, "latest": 82.0, "t_max": dt.datetime(2026, 10, 2, 12, 53, tzinfo=tz), "has_00z": False,
          "five": {"n": 60, "max_mid": 86.0, "max_hi": 86.9, "max_lo2": 85.1}}
    buckets = [{"slug": "x-gte87lt88f", "bid": 0.97, "ask": 0.98, "bid_sz": 100, "ask_sz": 100},
               {"slug": "x-gte90lt91f", "bid": 0.20, "ask": 0.25, "bid_sz": 100, "ask_sz": 100}]
    old = {r["slug"]: r for r in U.evaluate(buckets, ob, now, cfg=dict(U.CFG, r2_max="metar"), city="lax")["rows"]}
    assert old["x-gte87lt88f"]["rule"] == "R2"
    ev = U.evaluate(buckets, ob, now, cfg=dict(U.CFG, r2_max="raw5m"), city="lax")   # the default since 2026-10-06
    rows = {r["slug"]: r for r in ev["rows"]}
    assert rows["x-gte87lt88f"]["rule"] is None and "5-minute max 86F" in rows["x-gte87lt88f"]["blocker"]
    assert rows["x-gte90lt91f"]["rule"] == "R2" and ev["flags"]["r_max_r2"] == 86 and ev["flags"]["r2_blocked"] == ["87-88"]
    ev = U.evaluate(buckets, ob, now, cfg=dict(U.CFG, r2_max="raw5u"), city="lax")   # upper bound: 87 (90-91 still exactly 3F above)
    assert ev["flags"]["r_max_r2"] == 87 and [r["rule"] for r in ev["rows"]] == [None, "R2"]
    assert U.CFG["r2_max"] == "raw5m"


def test_r2_waits_when_the_5_minute_request_failed_but_not_where_none_exist():
    tz = zoneinfo.ZoneInfo("America/Chicago")
    ob = {"max": 80.0, "max_raw": 80.0, "latest": 78.0, "t_max": dt.datetime(2026, 9, 8, 13, 0, tzinfo=tz), "has_00z": False, "five": None}
    b = [{"slug": "x-gte84lt85f", "bid": 0.30, "ask": 0.35, "bid_sz": 100, "ask_sz": 100}]
    now = dt.datetime(2026, 9, 8, 16, 0, tzinfo=tz); cfg = dict(U.CFG, r2_max="raw5u", five_min=1)
    r = U.evaluate(b, ob, now, cfg=cfg, city="mdw")["rows"][0]
    assert r["rule"] is None and "waits for 5-minute data (request failed" in r["blocker"] and not r.get("r2_blocked")
    assert U.evaluate(b, dict(ob, five={"n": 0}), now, cfg=cfg, city="nyc")["rows"][0]["rule"] == "R2"   # KNYC / KSAT publish none
    r = U.evaluate(b, dict(ob, five={"n": 0}), now, cfg=cfg, city="mdw")["rows"][0]                    # KMDW does: an empty answer is blind
    assert r["rule"] is None and "no rows today" in r["blocker"]
    stale = {"n": 50, "max_mid": 77.0, "max_hi": 77.9, "max_lo2": 75.1, "last": now - dt.timedelta(minutes=50)}
    assert "min old" in U.evaluate(b, dict(ob, five=stale), now, cfg=cfg, city="mdw")["rows"][0]["blocker"]
    assert U.evaluate(b, dict(ob, five={"n": 1, "max_mid": 66.2, "max_hi": 67.1, "max_lo2": None, "last": now - dt.timedelta(hours=8)}), now, cfg=cfg,
                      city="sat")["rows"][0]["rule"] == "R2"   # a stray KSAT row is ignored, not read as a stale feed
    fresh = dict(stale, last=now - dt.timedelta(minutes=25))
    assert U.evaluate(b, dict(ob, five=fresh), now, cfg=cfg, city="mdw")["rows"][0]["rule"] == "R2"


def test_r0_5min_lower_bound_is_shadow_until_enabled():
    tz = zoneinfo.ZoneInfo("America/Chicago")
    ob = {"max": 84.0, "max_obs": 84.0, "max_raw": 84.0, "latest": 84.0, "t_max": dt.datetime(2026, 9, 8, 13, 0, tzinfo=tz), "has_00z": False,
          "five": {"n": 40, "max_mid": 87.8, "max_hi": 88.7, "max_lo2": 86.9}}
    b = [{"slug": "x-gte84lt85f", "bid": 0.40, "ask": 0.45, "bid_sz": 100, "ask_sz": 100}]    # 84-85: dead only on the 5-minute bound
    now = dt.datetime(2026, 9, 8, 13, 30, tzinfo=tz)
    ev = U.evaluate(b, ob, now, cfg=dict(U.CFG, r0_5min=0), city="mdw")
    assert ev["rows"][0]["rule"] is None and ev["flags"]["r0_5min_shadow"] == ["84-85"]
    ev = U.evaluate(b, ob, now, cfg=dict(U.CFG, r0_5min=1), city="mdw")
    assert ev["rows"][0]["rule"] == "R0" and ev["rows"][0]["side"] == "NO" and ev["flags"]["r0_5min_shadow"] == []


def test_five_min_obs_caches_success_and_retries_failure(monkeypatch):
    tz = zoneinfo.ZoneInfo("America/Chicago"); now = dt.datetime(2026, 9, 8, 13, 0, tzinfo=tz)
    U._FIVE_CACHE.clear(); calls = []
    def ok():
        calls.append(1); return {"features": [_feat("2026-09-08T17:00:00+00:00", 25), _feat("2026-09-08T17:05:00+00:00", 26)]}
    assert U.five_min_obs("KMDW", tz, now, fetch=ok)["n"] == 2 and U.five_min_obs("KMDW", tz, now, fetch=ok)["n"] == 2 and len(calls) == 1
    key = next(iter(U._FIVE_CACHE)); U._FIVE_CACHE[key] = (U._FIVE_CACHE[key][0] - U.CFG["five_min_ttl"] - 1, U._FIVE_CACHE[key][1])   # expire
    assert U.five_min_obs("KMDW", tz, now, fetch=lambda: None)["n"] == 2   # failed refresh keeps the last good summary
    U._FIVE_CACHE.clear()
    assert U.five_min_obs("KMDW", tz, now, fetch=lambda: None) is None
    assert U.five_min_obs("KMDW", tz, now, fetch=ok) is None             # a failure is retried after FIVE_RETRY_S, not every poll
    U._FIVE_CACHE[key] = (U._FIVE_CACHE[key][0] - U.FIVE_RETRY_S - 1, None)
    assert U.five_min_obs("KMDW", tz, now, fetch=ok)["n"] == 2
    U._FIVE_CACHE.clear()


def test_five_min_breaker_limits_requests_when_the_host_fails(monkeypatch):
    tz = zoneinfo.ZoneInfo("America/Chicago"); now = dt.datetime(2026, 9, 8, 13, 0, tzinfo=tz)
    U._FIVE_CACHE.clear(); U._FIVE_FAIL.update(until=0.0, open=False); calls = []; logged = []
    monkeypatch.setattr(U, "_get_json_gz", lambda url, timeout: calls.append(url))
    monkeypatch.setattr(U, "journal", lambda ev: logged.append(ev))
    for st in ("KMDW", "KDFW", "KHOU", "KOKC"):
        assert U.five_min_obs(st, tz, now) is None
    assert len(calls) == 1 and len(logged) == 1   # one request and one journal line, then the breaker holds for FIVE_RETRY_S
    U._FIVE_CACHE.clear(); U._FIVE_FAIL.update(until=0.0, open=False)


def test_climate_day_starts_at_local_standard_midnight_on_clock_change_days():
    chi = zoneinfo.ZoneInfo("America/Chicago"); phx = zoneinfo.ZoneInfo("America/Phoenix"); utc = dt.timezone.utc
    for now in (dt.datetime(2026, 11, 1, 16, 0, tzinfo=utc), dt.datetime(2027, 3, 14, 16, 0, tzinfo=utc), dt.datetime(2026, 10, 6, 16, 0, tzinfo=utc),
                dt.datetime(2026, 12, 6, 16, 0, tzinfo=utc)):
        ds = U.climate_day_start(now, chi)
        assert ds.astimezone(utc).hour == 6 and ds.astimezone(utc).date() == now.date()
    assert U.climate_day_start(dt.datetime(2026, 10, 6, 16, 0, tzinfo=utc), chi).hour == 1       # 01:00 CDT
    assert U.climate_day_start(dt.datetime(2026, 12, 6, 16, 0, tzinfo=utc), chi).hour == 0       # 00:00 CST
    assert U.climate_day_start(dt.datetime(2026, 7, 6, 16, 0, tzinfo=utc), phx).hour == 0        # Phoenix: no daylight saving
    assert U.climate_day_start(dt.datetime(2026, 10, 7, 5, 30, tzinfo=utc), chi).date() == dt.date(2026, 10, 6)   # 00:30 CDT is still Oct 6
