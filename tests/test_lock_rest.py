"""Resting-bid fallback for lock candidates (LOCK_REST_AT_CAP)."""
from __future__ import annotations

import dataclasses
import time
from decimal import Decimal

from scout import agent, execute
from scout.config import settings_from_env
from scout.execute import LiveDisabled, _rest_price, live_rest_buy, rest_fallback_applies


class _Accepted:
    ok = True
    order_id = "ord-1"
    status = "live"
    making_amount = "0"
    taking_amount = "0"

    def model_dump(self, mode="json"):
        return {"ok": True, "order_id": self.order_id, "status": self.status, "making_amount": "0", "taking_amount": "0"}


class _Client:
    def __init__(self):
        self.calls = []

    def place_limit_order(self, **kw):
        self.calls.append(kw)
        return _Accepted()


def _live_settings(monkeypatch):
    monkeypatch.setenv("CRYPTO_MIN_EDGE", "0.004")
    monkeypatch.setenv("CRYPTO_MAX_SLIP", "0.01")
    return dataclasses.replace(settings_from_env(), live=True)


def _cand(price=0.97, stake=10.0):
    end = int(time.time()) // 300 * 300 + 300
    return {
        "id": "m1", "question": "Bitcoin Up or Down", "slug": f"btc-updown-5m-{end - 300}", "side": "YES",
        "price": price, "limit_price": price, "fair": 0.995, "edge": 0.02, "stake": stake, "shares": stake / price,
        "yes_token": "y1", "no_token": "n1", "tick_size": 0.01, "min_order_size": 5, "edge_type": "twap_lock",
        "kind": "crypto_lag", "signal_ts": time.time(), "clob_quoted_at": time.time(), "asset": "btc", "window_end": end,
    }


def test_rest_price_keeps_edge(monkeypatch):
    s = _live_settings(monkeypatch)
    assert _rest_price(_cand(0.97), s, Decimal("0.01")) == Decimal("0.98")   # snapshot 0.97 + slip 0.01
    assert _rest_price(_cand(0.985), s, Decimal("0.01")) == Decimal("0.99")  # fair 0.995 - 0.004 -> 0.99


def test_rest_fallback_applies_only_to_lock_and_gone_offers(monkeypatch):
    monkeypatch.setenv("LOCK_REST_AT_CAP", "1")
    assert rest_fallback_applies(_cand(), LiveDisabled("buy rejected: no orders found to match with FAK order"))
    assert rest_fallback_applies(_cand(), LiveDisabled("no resting asks"))
    assert not rest_fallback_applies(_cand(), LiveDisabled("Trading restricted in your region"))
    assert not rest_fallback_applies({**_cand(), "edge_type": "crypto_lag"}, LiveDisabled("no resting asks"))
    monkeypatch.setenv("LOCK_REST_AT_CAP", "0")
    assert not rest_fallback_applies(_cand(), LiveDisabled("no resting asks"))


def test_live_rest_buy_posts_post_only_bid(monkeypatch):
    s = _live_settings(monkeypatch)
    client = _Client()
    monkeypatch.setattr(execute, "trading_client", lambda settings: client)
    monkeypatch.setattr(execute, "_validate_crypto_execution", lambda cand, settings, **kw: None)
    raw = live_rest_buy(_cand(0.97, 10.0), s)
    assert raw["order_id"] == "ord-1" and raw["rest_price"] == "0.98"
    call = client.calls[0]
    assert call["token_id"] == "y1" and call["side"] == "BUY" and call["post_only"] is True
    assert call["price"] == Decimal("0.98") and call["size"] == Decimal("5.10")  # capped at LOCK_REST_MAX_USD=$5 -> 5/0.98 floored to cents
    # below the venue minimum -> refused, nothing posted
    try:
        live_rest_buy(_cand(0.97, 3.0), s)
        assert False, "expected LiveDisabled"
    except LiveDisabled as exc:
        assert "below venue minimum" in str(exc)
    assert len(client.calls) == 1


def test_working_live_fills_and_cancels(monkeypatch):
    s = _live_settings(monkeypatch)
    end = int(time.time()) // 300 * 300 + 300
    base = {"ts": "x", "market_id": "m1", "question": "Bitcoin Up or Down", "side": "YES", "price": 0.98, "stake": 9.8,
            "shares": 10.0, "reason": "lock rest", "mode": "live", "yes_token": "y1", "no_token": "n1",
            "slug": f"btc-updown-5m-{end - 300}", "event_slug": f"btc-updown-5m-{end - 300}", "asset": "btc", "edge_type": "twap_lock"}
    # fully matched -> position
    ledger = {"cash": 30.0, "positions": [], "fills": [], "orders": [dict(base, venue_id="o1")], "halted": False}
    monkeypatch.setattr(agent, "live_order_state", lambda oid, settings: ("MATCHED", 10.0, 0.98))
    out = agent._working_live(ledger, ledger["orders"][0], s)
    assert out and ledger["positions"] and not ledger["orders"]
    assert ledger["positions"][0]["shares"] == 10.0 and ledger["positions"][0]["edge_type"] == "twap_lock"
    # still live, nothing matched, window not over -> leave it
    ledger = {"cash": 30.0, "positions": [], "fills": [], "orders": [dict(base, venue_id="o2")], "halted": False}
    monkeypatch.setattr(agent, "live_order_state", lambda oid, settings: ("LIVE", 0.0, 0.98))
    assert agent._working_live(ledger, ledger["orders"][0], s) == [] and ledger["orders"]
    # window over, nothing matched -> cancelled with the reserved cash refunded
    cancelled = []
    monkeypatch.setattr(agent, "live_cancel", lambda oid, settings: cancelled.append(oid))
    ledger = {"cash": 30.0, "positions": [], "fills": [], "orders": [dict(base, venue_id="o3", slug="btc-updown-5m-1789000000", event_slug="btc-updown-5m-1789000000")], "halted": False}
    out = agent._working_live(ledger, ledger["orders"][0], s)
    assert out[0]["cancelled"] == "m1" and cancelled == ["o3"] and not ledger["orders"] and ledger["cash"] == 39.8


def test_execute_batch_rests_when_offer_is_gone(monkeypatch):
    s = _live_settings(monkeypatch)
    monkeypatch.setenv("LOCK_REST_AT_CAP", "1")
    monkeypatch.setattr(agent, "veto", lambda cand, ledger, settings, occupied=None: None)
    monkeypatch.setattr(agent, "execute", lambda ledger, cand, settings: (_ for _ in ()).throw(LiveDisabled("no resting asks")))
    monkeypatch.setattr(agent, "live_rest_buy", lambda cand, settings: {"order_id": "o9", "rest_price": "0.98", "rest_size": "10.2"})
    monkeypatch.setattr(agent, "journal", lambda event: None)
    ledger = {"cash": 40.0, "positions": [], "fills": [], "orders": [], "halted": False}
    held, taken, vetoes, errors = set(), [], [], []
    agent._execute_candidate_batch([_cand()], ledger, s, held=held, taken=taken, vetoes=vetoes, errors=errors)
    assert len(ledger["orders"]) == 1 and ledger["orders"][0]["venue_id"] == "o9" and held == {"m1"}
    assert errors and "no resting asks" in errors[0]
    # a geoblock rejection must NOT rest a bid
    ledger = {"cash": 40.0, "positions": [], "fills": [], "orders": [], "halted": False}
    monkeypatch.setattr(agent, "execute", lambda ledger, cand, settings: (_ for _ in ()).throw(LiveDisabled("buy rejected: Trading restricted in your region")))
    agent._execute_candidate_batch([_cand()], ledger, s, held=set(), taken=[], vetoes=[], errors=[])
    assert ledger["orders"] == []
