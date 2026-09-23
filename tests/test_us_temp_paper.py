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
    assert U.confirm_and_fill(led, [c], meta) == []                      # first sighting only pends
    fills = U.confirm_and_fill(led, [dict(c, px=0.89)], meta)            # second sighting, not worse -> fill at 0.89
    assert len(fills) == 1 and fills[0]["shares"] == 20 and abs(led["cash"] - (500 - 17.8 - U.fee(0.89, 20))) < 1e-6
    monkeypatch.setattr(U, "get", lambda url, timeout=20: {"settlement": 0})   # YES settled 0 -> NO pays 1
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
