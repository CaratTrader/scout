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
