"""Fixes from the 2026-09-12 deep review: slots count resting orders, venue-confirmed fills are
always booked, failed cancels are not forgotten, geoblock pauses entries, lock exposure is capped,
sigma clamps scale with the window, empty ask sides are recorded, lab lock needs tick coverage."""
from __future__ import annotations

import dataclasses
import math
import time

from scout import agent, risk
from scout.config import settings_from_env
from scout.crypto_lag import realized_window_sigma
from scout.execute import LiveDisabled
from scout.ledger import record_fill


def _live(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    return dataclasses.replace(settings_from_env(), live=True)


def _order(end, **kw):
    base = {"ts": "x", "market_id": "m1", "question": "Bitcoin Up or Down", "side": "YES", "price": 0.98, "stake": 4.9,
            "shares": 5.0, "reason": "lock rest", "mode": "live", "yes_token": "y1", "no_token": "n1", "venue_id": "o1",
            "slug": f"btc-updown-5m-{end - 300}", "event_slug": f"btc-updown-5m-{end - 300}", "asset": "btc", "edge_type": "twap_lock"}
    base.update(kw)
    return base


def test_veto_counts_resting_orders_as_slots(monkeypatch):
    s = _live(monkeypatch, MAX_POSITIONS=2, MAX_CRYPTO_POSITIONS=2)
    end = int(time.time()) // 300 * 300 + 300
    ledger = {"cash": 40.0, "positions": [{"market_id": "p1", "question": "Ethereum Up or Down", "side": "YES", "stake": 5, "asset": "eth", "slug": "eth-updown-5m-1"}],
              "orders": [_order(end, market_id="o-sol", question="Solana Up or Down", asset="sol", slug="sol-updown-5m-1")], "fills": [], "halted": False}
    cand = {"id": "m2", "kind": "crypto_lag", "edge_type": "twap_lock", "side": "YES", "price": 0.97, "fair": 0.995, "stake": 5.0,
            "asset": "xrp", "question": "XRP Up or Down", "slug": "xrp-updown-5m-1"}
    assert risk.veto(cand, ledger, s) == "max_positions"


def test_record_fill_force_books_venue_confirmed_fill(monkeypatch):
    s = _live(monkeypatch, MAX_POSITIONS=1)
    ledger = {"cash": 1.0, "positions": [{"market_id": "p1"}], "fills": [], "orders": [], "halted": False}
    market = {"id": "m1", "question": "q", "yes_token": "y", "no_token": "n"}
    try:
        record_fill(ledger, market=market, side="YES", stake=4.9, price=0.98, shares=5.0, reason="r", mode="live", settings=s)
        assert False, "expected refusal without force"
    except (RuntimeError, ValueError):
        pass
    fill = record_fill(ledger, market=market, side="YES", stake=4.9, price=0.98, shares=5.0, reason="r", mode="live", settings=s, force=True)
    assert fill["price"] == 0.98 and len(ledger["positions"]) == 2


def test_working_live_keeps_order_when_cancel_fails_then_drops_after_teardown(monkeypatch):
    s = _live(monkeypatch)
    monkeypatch.setattr(agent, "save_ledger", lambda ledger, path=None: None)
    monkeypatch.setattr(agent, "live_order_state", lambda oid, settings: ("LIVE", 0.0, 0.98))

    def failing_cancel(oid, settings):
        raise LiveDisabled("cancel failed: timeout")

    monkeypatch.setattr(agent, "live_cancel", failing_cancel)
    # window ended 30 s ago, cancel fails -> keep the order
    end = int(time.time()) - 30
    ledger = {"cash": 30.0, "positions": [], "fills": [], "orders": [_order(end)], "halted": False}
    assert agent._working_live(ledger, ledger["orders"][0], s) == []
    assert ledger["orders"] and ledger["orders"][0].get("cancel_failed_at")
    # window ended 20 minutes ago -> the venue tore the book down; drop it and refund
    end = int(time.time()) - 1200
    ledger = {"cash": 30.0, "positions": [], "fills": [], "orders": [_order(end)], "halted": False}
    out = agent._working_live(ledger, ledger["orders"][0], s)
    assert out and out[0]["cancelled"] == "m1" and not ledger["orders"] and ledger["cash"] == 34.9


def test_working_live_books_fill_even_when_slots_are_full(monkeypatch):
    s = _live(monkeypatch, MAX_POSITIONS=1)
    monkeypatch.setattr(agent, "save_ledger", lambda ledger, path=None: None)
    monkeypatch.setattr(agent, "live_order_state", lambda oid, settings: ("MATCHED", 5.0, 0.98))
    end = int(time.time()) // 300 * 300 + 300
    ledger = {"cash": 30.0, "positions": [{"market_id": "p1"}], "fills": [], "orders": [_order(end)], "halted": False}
    out = agent._working_live(ledger, ledger["orders"][0], s)
    assert out and len(ledger["positions"]) == 2 and not ledger["orders"]


def test_geoblock_pauses_entries_and_hunt(monkeypatch):
    s = _live(monkeypatch)
    monkeypatch.setattr(agent, "veto", lambda cand, ledger, settings, occupied=None: None)
    monkeypatch.setattr(agent, "journal", lambda event: None)
    monkeypatch.setattr(agent, "execute", lambda ledger, cand, settings: (_ for _ in ()).throw(LiveDisabled("buy rejected: Trading restricted in your region")))
    ledger = {"cash": 40.0, "positions": [], "fills": [], "orders": [], "halted": False}
    cand = {"id": "m1", "kind": "crypto_lag", "edge_type": "twap_lock", "side": "YES", "price": 0.97, "fair": 0.995, "stake": 5.0, "signal_ts": time.time(), "question": "q"}
    vetoes, errors = [], []
    agent._execute_candidate_batch([cand], ledger, s, held=set(), taken=[], vetoes=vetoes, errors=errors)
    assert ledger["entry_pause_until"] > time.time() and errors
    agent._execute_candidate_batch([dict(cand, id="m2")], ledger, s, held=set(), taken=[], vetoes=vetoes, errors=errors)
    assert vetoes and vetoes[-1]["reason"] == "entry_pause"
    assert agent._entry_paused(ledger)


def test_lock_exposure_cap_shrinks_and_vetoes(monkeypatch):
    s = _live(monkeypatch, LOCK_MAX_EXPOSURE_FRAC=0.30, MIN_CRYPTO_STAKE=2)
    ledger = {"cash": 40.0, "positions": [{"edge_type": "twap_lock", "stake": 8.0}], "orders": [{"edge_type": "twap_lock", "stake": 2.0}]}
    cand = {"edge_type": "twap_lock", "stake": 10.0, "price": 0.97}
    assert agent._cap_lock_stake(cand, ledger, s) is None
    assert cand["stake"] == 2.0 and abs(cand["shares"] - 2.0 / 0.97) < 1e-3  # 12 - 10 already at risk
    ledger["orders"].append({"edge_type": "twap_lock", "stake": 1.5})
    assert agent._cap_lock_stake({"edge_type": "twap_lock", "stake": 5.0, "price": 0.97}, ledger, s) == "lock_exposure"
    assert agent._cap_lock_stake({"edge_type": "crypto_lag", "stake": 5.0, "price": 0.5}, ledger, s) is None


def test_sigma_clamps_scale_with_window():
    flat = [100.0] * 40
    assert abs(realized_window_sigma(flat, 300) - 0.0008) < 1e-5      # floor ~ the old 5m value
    assert abs(realized_window_sigma(flat, 900) - 0.0014) < 1e-5      # ~ the old 15m value
    assert abs(realized_window_sigma(flat, 14400) - 0.00036 * math.sqrt(240)) < 1e-6
    wild = [100.0 * (1.05 ** (i % 2)) for i in range(40)]
    assert abs(realized_window_sigma(wild, 300) - 0.008) < 1e-4       # ceiling ~ the old 5m value
    assert abs(realized_window_sigma(wild, 14400) - 0.0036 * math.sqrt(240)) < 1e-6
    assert abs(realized_window_sigma([1.0, 2.0], 300) - 0.0024) < 1e-4  # default when too few closes


def test_ws_book_snapshot_with_empty_ask_side_is_recorded(monkeypatch):
    from scout import streams

    class Lv:
        def __init__(self, price, size):
            self.price, self.size = price, size

    class P:
        token_id = "tok-empty"
        asks = []
        bids = [Lv("0.99", "500")]
        tick_size = "0.01"

    class Ev:
        type = "book"
        payload = P()

    streams._apply_market_event(Ev())
    row = streams._BOOKS.get("tok-empty")
    assert row and row["ask"] == 0.0 and row["bid"] == 0.99 and row["ask_size"] == 0.0


def test_lab_lock_returns_none_on_recording_gap():
    from lab.strategies import LockTwap, State

    end = 1000
    s = State(asset="btc", mins=5, epoch=end - 300, t=end - 20.0, seconds_left=20.0, open_px=100.0, spot_raw=101.0, spot_twap=101.0,
              sigma=0.002, yes_ask=0.97, yes_bid=0.96, no_ask=0.03, no_bid=0.02, yes_ask_size=100.0)
    # oracle history stops 30 s before now: stale carry-forward would fake a lock
    s.oracle_hist = [(float(t), 101.0) for t in range(end - 70, end - 50)]
    assert LockTwap("x").p_up(s) is None
    s.oracle_hist = [(float(t), 101.0) for t in range(end - 70, end - 19)]
    assert LockTwap("x").p_up(s) is not None
