"""Late-window lock profile: env knobs, lag off-switch, crypto-only universe, oracle sigma."""
from __future__ import annotations

import math
import random

import pytest

from scout import agent, crypto_lag, twap_lock
from scout.config import settings_from_env


def _lock_fixture(monkeypatch, *, price_above_open: float = 1.01):
    end, now, open_twap = 1000.0, 980.0, 100.0
    spot = open_twap * price_above_open
    ticks = [(float(t), spot) for t in range(930, 981)]
    monkeypatch.setattr(twap_lock.streams, "oracle_spot", lambda asset, max_age=3.0: (spot, now))
    monkeypatch.setattr(twap_lock.streams, "oracle_ticks", lambda asset, since: [t for t in ticks if t[0] >= since])
    monkeypatch.setattr(twap_lock, "MIN_SECONDS", 8.0)
    monkeypatch.setattr(twap_lock, "MAX_SECONDS", 45.0)
    return {"end": end, "window_s": 300}, open_twap, now


def test_lock_signal_inside_late_window_with_min_z(monkeypatch):
    win, open_twap, now = _lock_fixture(monkeypatch)
    monkeypatch.setattr(twap_lock, "MIN_Z", 5.0)
    sig = twap_lock.lock_signal("btc", win, open_twap, 0.002, now=now)
    assert sig is not None
    assert sig["p_up"] >= 0.99
    assert abs(sig["z"]) >= 5.0
    assert sig["seconds_left"] == 20.0


def test_lock_signal_min_z_filters_weak_locks(monkeypatch):
    win, open_twap, now = _lock_fixture(monkeypatch)
    monkeypatch.setattr(twap_lock, "MIN_Z", 1e6)
    assert twap_lock.lock_signal("btc", win, open_twap, 0.002, now=now) is None


def test_env_float_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("TWAP_LOCK_TEST_KNOB", "0.97")
    assert twap_lock._env_float("TWAP_LOCK_TEST_KNOB", 0.5) == 0.97
    monkeypatch.setenv("TWAP_LOCK_TEST_KNOB", "garbage")
    assert twap_lock._env_float("TWAP_LOCK_TEST_KNOB", 0.5) == 0.5
    monkeypatch.delenv("TWAP_LOCK_TEST_KNOB")
    assert twap_lock._env_float("TWAP_LOCK_TEST_KNOB", 0.5) == 0.5


def test_lag_model_switch(monkeypatch):
    monkeypatch.delenv("CRYPTO_LAG_MODEL", raising=False)
    assert crypto_lag._lag_model_enabled() is True
    monkeypatch.setenv("CRYPTO_LAG_MODEL", "off")
    assert crypto_lag._lag_model_enabled() is False


def test_oracle_minute_sigma_uses_oracle_closes(monkeypatch):
    rng = random.Random(7)
    now = 10_000.0
    px, ticks = 100_000.0, []
    for t in range(int(now) - 40 * 60, int(now)):
        px *= math.exp(rng.gauss(0, 0.0004))
        ticks.append((float(t), px))
    from scout import streams

    monkeypatch.setattr(streams, "oracle_ticks", lambda asset, since: [t for t in ticks if t[0] >= since])
    sig = crypto_lag._oracle_minute_sigma("btc", 300, now)
    assert sig is not None
    assert 0.0008 <= sig <= 0.008
    monkeypatch.setattr(streams, "oracle_ticks", lambda asset, since: ticks[-120:])
    assert crypto_lag._oracle_minute_sigma("btc", 300, now) is None


def test_crypto_only_skips_general_scan(monkeypatch):
    monkeypatch.setenv("CRYPTO_ONLY", "1")

    def boom(settings):
        raise AssertionError("general Gamma scan must not run in crypto-only mode")

    monkeypatch.setattr(agent, "iter_markets", boom)
    monkeypatch.setattr(agent, "iter_crypto_events", lambda settings: [])
    assert agent.collect_universe(settings_from_env()) == []
    monkeypatch.delenv("CRYPTO_ONLY")
    assert agent.crypto_only() is False


def test_crypto_event_cache(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, ua, timeout=12):
        calls["n"] += 1
        return [{"id": "e1", "slug": "btc-updown-5m-1", "markets": []}]

    monkeypatch.setattr(crypto_lag, "_get_json", fake_get)
    monkeypatch.setattr(crypto_lag, "crypto_event_slugs", lambda now: ["btc-updown-5m-1"])
    crypto_lag._EVENT_CACHE.clear()
    settings = settings_from_env()
    monkeypatch.setenv("CRYPTO_EVENT_CACHE_SECONDS", "30")
    assert len(list(crypto_lag.iter_crypto_events(settings))) == 1
    assert len(list(crypto_lag.iter_crypto_events(settings))) == 1
    assert calls["n"] == 1
    monkeypatch.setenv("CRYPTO_EVENT_CACHE_SECONDS", "0")
    list(crypto_lag.iter_crypto_events(settings))
    assert calls["n"] == 2


def test_arena_has_live_mirror():
    from lab.strategies import LockTwap, by_name

    s = by_name()["lock_live_mirror"]
    assert isinstance(s, LockTwap)
    assert s.entry_band(5) == (8.0, 55.0)
    assert s.min_z == 3.5 and s.ask_floor == 0.955 and s.ask_cap == 0.99
    assert set(s.assets) == {"btc", "eth", "sol", "xrp", "doge", "bnb", "hype"}
    assert LockTwap("plain").entry_band(5) == (25.0, 90.0)


def test_quiet_cycles(monkeypatch):
    monkeypatch.delenv("QUIET_CYCLES", raising=False)
    assert agent.quiet_cycles() == 1
    monkeypatch.setenv("QUIET_CYCLES", "15")
    assert agent.quiet_cycles() == 15
    monkeypatch.setenv("QUIET_CYCLES", "garbage")
    assert agent.quiet_cycles() == 1
    monkeypatch.setenv("QUIET_CYCLES", "4")
    monkeypatch.setattr(agent, "_CYCLE_NO", 8)
    assert agent._verbose_cycle() is True
    monkeypatch.setattr(agent, "_CYCLE_NO", 9)
    assert agent._verbose_cycle() is False
    quiet = {"fills": [], "posts": [], "exits": [], "vetoes": [], "errors": [], "halt_reason": ""}
    assert agent._cycle_has_activity(quiet) is False
    assert agent._cycle_has_activity({**quiet, "fills": [{"side": "YES"}]}) is True
    assert agent._cycle_has_activity({**quiet, "halt_reason": "streak"}) is True
