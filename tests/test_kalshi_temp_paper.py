import datetime as dt, zoneinfo
from scout import kalshi_temp_paper as KT
from scout import us_temp_paper as U

def test_synthetic_slugs_map_strikes_to_the_shared_bucket_grammar():
    assert U.bounds(KT.synthetic_slug({"ticker": "X-T62", "strike_type": "less", "cap_strike": 62})) == (-1e9, 61.0)
    assert U.bounds(KT.synthetic_slug({"ticker": "X-B68.5", "strike_type": "between", "floor_strike": 68, "cap_strike": 69})) == (68.0, 69.0)
    assert U.bounds(KT.synthetic_slug({"ticker": "X-T73", "strike_type": "greater", "floor_strike": 73})) == (74.0, 1e9)

def test_market_day_is_the_local_day_before_the_05z_close():
    tz = zoneinfo.ZoneInfo("America/New_York")
    assert KT.market_day({"close_time": "2026-09-29T05:00:00Z"}, tz) == "2026-09-28"

def test_cents_normalises_prices():
    assert KT.cents(45) == 0.45 and KT.cents("0.4500") == 0.45 and KT.cents(None) is None

def test_rules_apply_to_kalshi_ladder(monkeypatch):
    tz = zoneinfo.ZoneInfo("America/New_York")
    ob = {"max": 61.0, "latest": 59.0, "t_max": dt.datetime(2026, 9, 26, 14, 30, tzinfo=tz), "has_00z": True}
    buckets = [
        {"slug": KT.synthetic_slug({"ticker": "N-T62", "strike_type": "less", "cap_strike": 62}), "bid": 0.76, "ask": 0.79, "bid_sz": None, "ask_sz": None},
        {"slug": KT.synthetic_slug({"ticker": "N-B62.5", "strike_type": "between", "floor_strike": 62, "cap_strike": 63}), "bid": 0.21, "ask": 0.23, "bid_sz": None, "ask_sz": None},
        {"slug": KT.synthetic_slug({"ticker": "N-T69", "strike_type": "greater", "floor_strike": 69}), "bid": None, "ask": 0.01, "bid_sz": None, "ask_sz": None},
    ]
    s = U.signals(buckets, ob, dt.datetime(2026, 9, 26, 20, 10, tzinfo=tz), city="nyc")
    assert [(c["rule"], c["side"], c["px"]) for c in s] == [("R1x", "YES", 0.79), ("R1x", "NO", 0.79)]



def test_negative_kalshi_strikes_map_correctly():
    assert U.bounds(KT.synthetic_slug({"ticker": "M-T-3", "strike_type": "less", "cap_strike": -3})) == (-1e9, -4.0)
    assert U.bounds(KT.synthetic_slug({"ticker": "M-B-1.5", "strike_type": "between", "floor_strike": -2, "cap_strike": -1})) == (-2.0, -1.0)
    assert U.bounds(KT.synthetic_slug({"ticker": "M-T-1", "strike_type": "greater", "floor_strike": -1})) == (0.0, 1e9)


def test_book_depth_maps_taker_prices(monkeypatch):
    ob = {"orderbook_fp": {"yes_dollars": [["0.1500", "40.00"], ["0.2200", "14.00"], ["0.0500", "300.00"]], "no_dollars": [["0.7000", "12.00"], ["0.7600", "5.00"]]}}
    monkeypatch.setattr(KT, "kget", lambda url, quiet=False: ob)
    assert KT.book_depth("X", "NO") == [[0.78, 14.0], [0.85, 40.0], [0.95, 300.0]]   # NO taker hits YES bids: 1 - 0.22 first
    assert KT.book_depth("X", "YES") == [[0.24, 5.0], [0.3, 12.0]]
    assert KT.book_size("X", "NO") == 14.0
    monkeypatch.setattr(KT, "kget", lambda url, quiet=False: None)
    assert KT.book_depth("X", "NO") is None and KT.book_size("X", "NO") == 0.0


def test_quotes_distinguishes_failure_from_no_market(monkeypatch):
    tz = zoneinfo.ZoneInfo("America/New_York")
    monkeypatch.setattr(KT, "kget", lambda url, quiet=False: None)
    assert KT.quotes("KXHIGHNY", "2026-10-01", tz) is None
    monkeypatch.setattr(KT, "kget", lambda url, quiet=False: {"markets": []})
    assert KT.quotes("KXHIGHNY", "2026-10-01", tz) == []



def _poll_with(monkeypatch, tmp_path, depth, five=None):
    """One Kalshi poll for NYC with a dead bucket bid at 0.10 (R0 NO at 0.90), network fully mocked."""
    monkeypatch.setattr(U, "JOURNAL", tmp_path / "j.jsonl"); monkeypatch.setattr(U, "LEDGER", tmp_path / "l.json")
    monkeypatch.setattr(KT, "SNAPS", tmp_path / "s.jsonl"); monkeypatch.setattr(KT, "STATE", tmp_path / "st.json")
    monkeypatch.setattr(KT, "SERIES", {"KXHIGHNY": ("nyc", "KNYC", "America/New_York")})
    tz = zoneinfo.ZoneInfo("America/New_York")
    q = [{"slug": KT.synthetic_slug({"ticker": "N-T65", "strike_type": "less", "cap_strike": 65}), "ticker": "N-T65", "bid": 0.10, "ask": 0.12, "bid_sz": None, "ask_sz": None,
          "state": "OPEN", "close_time": "2026-10-02T05:00:00Z", "title": "t"}]
    monkeypatch.setattr(KT, "quotes", lambda series, day, tz_: q)
    monkeypatch.setattr(U, "observed", lambda station, tz_, now, fetch=None, day_start_hour=None: {"max": 70.0, "t_max": dt.datetime(2026, 10, 1, 14, 0, tzinfo=tz), "latest": 68.0, "n": 10,
                        "last_obs": None, "has_00z": False, "readings": [], "max_raw": 70.0, "max_obstime": 70.0})
    monkeypatch.setattr(U, "cli_intraday", lambda city, day, fetch=None: None)
    monkeypatch.setattr(KT, "book_depth", lambda ticker, side, levels_n=5: depth)
    monkeypatch.setattr(KT, "settle", lambda led: [])
    monkeypatch.setattr(U, "five_min_obs", lambda station, tz_, now, day_start_hour=None, fetch=None: five)
    led = U.load_ledger(); now = dt.datetime(2026, 10, 1, 20, 0, tzinfo=dt.timezone.utc)
    for i in range(2):
        KT.poll(led, now + dt.timedelta(seconds=60 * i))
    return led


def test_fill_uses_all_depth_at_the_scored_price(monkeypatch, tmp_path):
    led = _poll_with(monkeypatch, tmp_path, [[0.90, 10.0], [0.905, 8.0], [0.95, 500.0]])
    assert len(led["positions"]) == 1 and led["positions"][0]["shares"] == 18.0 and led["positions"][0]["px"] == 0.9


def test_stale_quote_is_skipped(monkeypatch, tmp_path):
    led = _poll_with(monkeypatch, tmp_path, [[0.97, 500.0]])   # executable NO price worse than the scored 0.90
    assert led["positions"] == []


def test_failed_five_minute_request_does_not_block_the_dead_bucket_rule(monkeypatch, tmp_path):
    led = _poll_with(monkeypatch, tmp_path, [[0.90, 10.0]], five=None)   # R0 never depends on the 5-minute data
    assert len(led["positions"]) == 1


def test_new_cities_are_central_time_without_report_trigger():
    for s_, city in (("KXHIGHTHOU", "hou"), ("KXHIGHTOKC", "okc"), ("KXHIGHTSATX", "sat"), ("KXHIGHTNOLA", "msy")):
        assert KT.SERIES[s_][0] == city and KT.SERIES[s_][2] == "America/Chicago"
        assert U.Z00_LOCAL_HOUR[city] == 19 and city not in U.CFG["cli_cities"]
