"""Resting a bid when the lock is decisive but the favourite is not offered at all."""
from __future__ import annotations

import dataclasses
import time

from scout import agent, twap_lock
from scout.config import settings_from_env


def _settings(monkeypatch, rest="1"):
    monkeypatch.setenv("LOCK_REST_AT_CAP", rest)
    monkeypatch.setenv("CRYPTO_MIN_EDGE", "0.012")
    monkeypatch.setenv("MAX_CRYPTO_STAKE", "20")
    monkeypatch.setattr(twap_lock, "ASK_CAP", 0.98)
    monkeypatch.setattr(twap_lock, "ASK_FLOOR", 0.955)
    return dataclasses.replace(settings_from_env(), live=True)


def _market(yes_ask=0.999, yes_bid=0.99):
    end = int(time.time()) // 300 * 300 + 300
    return {"id": "m1", "question": "Bitcoin Up or Down", "slug": f"btc-updown-5m-{end - 300}", "yes_token": "y1", "no_token": "n1",
            "yes_ask": yes_ask, "no_ask": 0.01, "yes_bid": yes_bid, "no_bid": 0.001, "tick_size": 0.01, "min_order_size": 5,
            "clob_quoted_at": time.time(), "clob_fresh": True, "asset": "btc"}


def _score(end):
    return {"p_yes": 0.995, "edge_type": "twap_lock", "asset": "btc", "signal_ts": time.time(), "window_start": end - 300, "window_end": end, "thesis": "lock"}


def _wire(monkeypatch):
    posted = []
    monkeypatch.setattr(agent, "veto", lambda cand, ledger, settings, occupied=None: None)
    monkeypatch.setattr(agent, "journal", lambda event: None)
    monkeypatch.setattr(agent, "save_ledger", lambda ledger, path=None: None)
    monkeypatch.setattr(agent, "live_rest_buy", lambda cand, settings: posted.append(cand) or {"order_id": "o1", "rest_price": "0.98", "rest_size": "5.1"})
    return posted


def test_rests_when_favourite_has_no_offer(monkeypatch):
    s = _settings(monkeypatch)
    posted = _wire(monkeypatch)
    m = _market()
    ledger = {"cash": 44.0, "positions": [], "fills": [], "orders": [], "halted": False}
    held, vetoes, errors = set(), [], []
    assert agent._rest_on_no_offer(m, _score(m["slug"] and int(m["slug"].split("-")[-1]) + 300), ledger, s, held=held, vetoes=vetoes, errors=errors) is True
    assert posted and posted[0]["side"] == "YES" and posted[0]["price"] == 0.98 and posted[0]["fair"] == 0.995
    assert len(ledger["orders"]) == 1 and ledger["orders"][0]["venue_id"] == "o1" and held == {"m1"}
    # second call for the same market does nothing (already held / already resting)
    assert agent._rest_on_no_offer(m, _score(0), ledger, s, held=held, vetoes=vetoes, errors=errors) is False
    assert len(posted) == 1


def test_does_not_rest_when_offer_exists_or_book_disagrees_or_disabled(monkeypatch):
    s = _settings(monkeypatch)
    posted = _wire(monkeypatch)
    ledger = {"cash": 44.0, "positions": [], "fills": [], "orders": [], "halted": False}
    # a real offer inside the band -> taker path, no rest
    assert agent._rest_on_no_offer(_market(yes_ask=0.97), _score(0), ledger, s, held=set(), vetoes=[], errors=[]) is False
    # favourite bid below the band floor -> the book disagrees, no rest
    assert agent._rest_on_no_offer(_market(yes_bid=0.90), _score(0), ledger, s, held=set(), vetoes=[], errors=[]) is False
    assert posted == []
    # NO-side lock rests on the NO token
    m = _market(); m.update({"yes_ask": 0.01, "no_ask": 0.999, "yes_bid": 0.001, "no_bid": 0.99})
    score = dict(_score(0), p_yes=0.005)
    assert agent._rest_on_no_offer(m, score, ledger, s, held=set(), vetoes=[], errors=[]) is True
    assert posted[-1]["side"] == "NO" and abs(posted[-1]["fair"] - 0.995) < 1e-9
    # switch off -> nothing
    s_off = _settings(monkeypatch, rest="0")
    assert agent._rest_on_no_offer(_market(), _score(0), {"cash": 44.0, "positions": [], "orders": []}, s_off, held=set(), vetoes=[], errors=[]) is False


def test_vetoed_rest_is_reported_not_posted(monkeypatch):
    s = _settings(monkeypatch)
    posted = _wire(monkeypatch)
    monkeypatch.setattr(agent, "veto", lambda cand, ledger, settings, occupied=None: "loss_cooldown")
    vetoes = []
    assert agent._rest_on_no_offer(_market(), _score(0), {"cash": 44.0, "positions": [], "orders": []}, s, held=set(), vetoes=vetoes, errors=[]) is False
    assert vetoes and vetoes[0]["reason"] == "loss_cooldown" and posted == []


def test_rest_skips_below_venue_minimum_and_backs_off(monkeypatch):
    s = _settings(monkeypatch)
    posted = _wire(monkeypatch)
    agent._REST_BACKOFF.clear()
    monkeypatch.setenv("LOCK_MAX_EXPOSURE_FRAC", "0.30")
    # 30% of $45 = $13.5, $10 already resting -> $3.5 left -> 3.57 shares at 0.98: below the 5-share minimum
    ledger = {"cash": 45.0, "positions": [], "fills": [], "orders": [{"market_id": "other", "edge_type": "twap_lock", "stake": 10.0}], "halted": False}
    m = _market()
    assert agent._rest_on_no_offer(m, _score(0), ledger, s, held=set(), vetoes=[], errors=[]) is False
    assert posted == [] and agent._REST_BACKOFF.get("m1", 0) > time.time()
    # while backed off, nothing is attempted even if the budget is now there
    ledger["orders"].clear()
    assert agent._rest_on_no_offer(m, _score(0), ledger, s, held=set(), vetoes=[], errors=[]) is False
    assert posted == []
    agent._REST_BACKOFF.clear()
    assert agent._rest_on_no_offer(m, _score(0), ledger, s, held=set(), vetoes=[], errors=[]) is True
    assert len(posted) == 1
