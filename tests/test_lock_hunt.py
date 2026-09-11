"""Sub-second lock hunt and one-sided websocket books."""
from __future__ import annotations

import time

from scout import agent, twap_lock
from scout.config import settings_from_env


class _Client:
    def close(self) -> None:
        pass


def _market(now: float) -> dict:
    start = int(now) // 300 * 300
    end = start + 300
    return {
        "id": "m1",
        "slug": f"btc-updown-5m-{start}",
        "event_slug": f"btc-updown-5m-{start}",
        "question": "Bitcoin Up or Down",
        "yes_token": "y1",
        "no_token": "n1",
        "yes_ask": 0.97,
        "no_ask": 0.03,
        "yes_bid": 0.96,
        "no_bid": 0.02,
        "yes_ask_size": 500.0,
        "no_ask_size": 500.0,
        "tick_size": 0.01,
        "completeness_edge": 0.0,
        "volume_24h": 1e6,
        "liquidity": 1e4,
        "spread": 0.01,
        "clob_fresh": True,
        "clob_quoted_at": now,
        "_end": end,
    }


def test_lock_hunt_seconds(monkeypatch):
    monkeypatch.delenv("LOCK_HUNT_SECONDS", raising=False)
    assert agent.lock_hunt_seconds() == 0.0
    monkeypatch.setenv("LOCK_HUNT_SECONDS", "8")
    assert agent.lock_hunt_seconds() == 8.0
    monkeypatch.setenv("LOCK_HUNT_SECONDS", "x")
    assert agent.lock_hunt_seconds() == 0.0


def test_hunt_locks_buys_once_when_offered_in_band(monkeypatch):
    now = time.time()
    m = _market(now)
    end = m["_end"]
    # put the window 30 s from its end so it is inside the lock band
    monkeypatch.setattr(agent, "parse_window", lambda market: {"asset": "btc", "start": end - 300, "end": end, "window_s": 300})
    monkeypatch.setattr(twap_lock, "MIN_SECONDS", 8.0)
    monkeypatch.setattr(twap_lock, "MAX_SECONDS", 55.0)
    monkeypatch.setattr(twap_lock, "ASK_CAP", 0.99)
    monkeypatch.setattr(twap_lock, "ASK_FLOOR", 0.955)
    monkeypatch.setenv("LOCK_HUNT_SECONDS", "1")
    monkeypatch.setenv("CRYPTO_MIN_EDGE", "0.004")
    monkeypatch.setenv("MAX_CRYPTO_STAKE", "20")
    monkeypatch.setattr(agent, "_refresh_crypto_books", lambda markets, client=None: len(markets))

    def fake_scores(markets, settings):
        return {
            "m1": {
                "p_yes": 0.995, "raw_model_p_yes": 0.995, "market_p_yes": None, "model_weight": 1.0, "confidence": 0.95,
                "already_priced": False, "edge_type": "twap_lock", "asset": "btc", "signal_ts": time.time(),
                "window_start": end - 300, "window_end": end, "seconds_left_at_signal": 30.0, "oracle_open": 1.0,
                "oracle_spot": 1.01, "thesis": "test lock", "sources": ["chainlink"],
            }
        }

    monkeypatch.setattr(agent, "score_crypto_windows", fake_scores)
    executed: list[dict] = []

    def fake_batch(cands, ledger, settings, *, held, taken, vetoes, errors):
        for c in cands:
            executed.append(c)
            held.add(c["id"])
            taken.append({"id": c["id"], "price": c["price"], "stake": c["stake"]})

    monkeypatch.setattr(agent, "_execute_candidate_batch", fake_batch)
    settings = settings_from_env()
    ledger = {"cash": 38.0, "positions": [], "fills": [], "halted": False}
    held: set[str] = set()
    taken: list = []
    # seconds_left must be inside the band for eligibility: a fake clock that starts 30 s
    # before the end and advances 0.3 s per call, so the watch loop terminates.
    clock = {"t": end - 30.0}

    def fake_time():
        clock["t"] += 0.3
        return clock["t"]

    monkeypatch.setattr(agent.time, "time", fake_time)
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    ticks = agent.hunt_locks([m], ledger, settings, held=held, taken=taken, vetoes=[], errors=[], client=_Client())
    assert ticks >= 1
    assert len(executed) == 1, "one buy per market while the offer sits in band"
    cand = executed[0]
    assert cand["side"] == "YES" and cand["price"] == 0.97 and cand["edge_type"] == "twap_lock"
    assert 2.0 <= cand["stake"] <= 20.0
    assert held == {"m1"}


def test_hunt_locks_skips_when_no_window_is_near_its_end(monkeypatch):
    now = time.time()
    m = _market(now)
    monkeypatch.setenv("LOCK_HUNT_SECONDS", "5")
    monkeypatch.setattr(agent, "parse_window", lambda market: {"asset": "btc", "start": now, "end": now + 280, "window_s": 300})
    assert agent.hunt_locks([m], {"cash": 38.0, "positions": [], "halted": False}, settings_from_env(), held=set(), taken=[], vetoes=[], errors=[], client=_Client()) == 0


def test_ws_book_with_empty_side_is_still_fresh(monkeypatch):
    from scout import streams

    now = time.time()
    books = {
        "y1": {"ask": 0.0, "bid": 0.99, "ask_size": 0.0, "tick_size": 0.01, "quoted_at": now},
        "n1": {"ask": 0.02, "bid": 0.01, "ask_size": 900.0, "tick_size": 0.01, "quoted_at": now},
    }
    monkeypatch.setattr(streams, "start_streams", lambda: None)
    monkeypatch.setattr(streams, "set_book_tokens", lambda tokens: None)
    monkeypatch.setattr(streams, "book", lambda token, max_age=10.0: books.get(token))
    m = _market(now)
    assert agent._refresh_crypto_books([m]) == 1
    assert m["clob_fresh"] is True and m["yes_ask"] == 0.999 and m["no_ask"] == 0.02
