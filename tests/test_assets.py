"""All seven TWAP-settled Up-or-Down assets and all three window lengths parse, stream and are discovered."""
from __future__ import annotations

from scout.crypto_lag import SUPPORTED_ASSETS, crypto_event_slugs, is_short_updown_window, parse_window, stream_assets


def test_seven_assets_parse_and_stream(monkeypatch):
    assert SUPPORTED_ASSETS == ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
    for a in SUPPORTED_ASSETS:
        win = parse_window({"slug": f"{a}-updown-5m-1789080900", "question": "x"})
        assert win and win["asset"] == a and win["window_s"] == 300
        win15 = parse_window({"slug": f"{a}-updown-15m-1789080300", "question": "x"})
        assert win15 and win15["window_s"] == 900
        win4h = parse_window({"slug": f"{a}-updown-4h-1789070400", "question": "x"})
        assert win4h and win4h["window_s"] == 14400 and win4h["end"] == 1789070400 + 14400
    assert parse_window({"slug": "ada-updown-5m-1789080900", "question": "x"}) is None
    assert parse_window({"slug": "btc-updown-1h-1789080900", "question": "x"}) is None
    monkeypatch.delenv("CRYPTO_STREAM_ASSETS", raising=False)
    assert stream_assets() == list(SUPPORTED_ASSETS)
    monkeypatch.setenv("CRYPTO_STREAM_ASSETS", "btc, xrp,ada")
    assert stream_assets() == ["btc", "xrp"]


def test_event_slugs_follow_enabled_assets_and_windows(monkeypatch):
    now = 1789080900 + 17
    monkeypatch.setenv("CRYPTO_ASSETS", "btc,eth,sol,xrp,doge,bnb,hype")
    monkeypatch.setenv("CRYPTO_WINDOWS_MIN", "5,15")
    slugs = crypto_event_slugs(now)
    assert len(slugs) == 7 * 2 * 3  # asset x window length x (previous, current, next)
    assert "xrp-updown-5m-1789080900" in slugs and "hype-updown-15m-1789080300" in slugs
    assert not any("-4h-" in s for s in slugs)
    monkeypatch.setenv("CRYPTO_WINDOWS_MIN", "5,15,240")
    slugs = crypto_event_slugs(now)
    assert len(slugs) == 7 * 3 * 3
    assert "btc-updown-4h-1789070400" in slugs  # 1789080917 // 14400 * 14400
    assert is_short_updown_window({"asset": "doge", "window_s": 14400}) is True
    monkeypatch.setenv("CRYPTO_ASSETS", "btc")
    monkeypatch.setenv("CRYPTO_WINDOWS_MIN", "5")
    assert crypto_event_slugs(now) == ["btc-updown-5m-1789080600", "btc-updown-5m-1789080900", "btc-updown-5m-1789081200"]
    assert is_short_updown_window({"asset": "btc", "window_s": 14400}) is False
