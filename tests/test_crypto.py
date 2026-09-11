from scout.crypto_lag import (
    _CHAINLINK_LOCK,
    _CHAINLINK_VALUES,
    chainlink_window_context,
    in_entry_window,
    is_crypto_updown,
    p_up_from_move,
    parse_window,
    parse_window_from_question,
    score_crypto_windows,
)
from scout.execute import (
    _actual_fill,
    _buy_ceiling,
    _refresh_pair_quote,
    _validate_crypto_execution,
    LiveDisabled,
    ReconcileRequired,
)
from scout.config import Settings
from scout.agent import _refresh_crypto_books
import scout.agent as agent_module
import scout.execute as execute_module
from decimal import Decimal
from types import SimpleNamespace
import pytest


def test_parse_btc_5m_slug():
    row = parse_window({"slug": "btc-updown-5m-1787727900", "question": "Bitcoin Up or Down"})
    assert row is not None
    assert row["asset"] == "btc"
    assert row["window_s"] == 300
    assert row["start"] == 1787727900
    assert is_crypto_updown({"slug": "btc-updown-5m-1787727900", "question": "Bitcoin Up or Down"})

def test_crypto_event_slugs_include_15m_not_swallowed_by_5m():
    from scout.crypto_lag import crypto_event_slugs

    now = 1787857200
    slugs = crypto_event_slugs(now)
    assert "btc-updown-5m-1787857200" in slugs
    assert "btc-updown-15m-1787857200" in slugs
    assert "sol-updown-15m-1787856300" in slugs
    assert any("-15m-" in slug for slug in slugs)


def test_parse_question_sol_window():
    win = parse_window_from_question(
        "Solana Up or Down - August 26, 3:10AM-3:15AM ET",
        "2026-08-26T07:14:24+00:00",
    )
    assert win is not None
    assert win["asset"] == "sol"
    assert win["window_s"] == 300
    assert win["end"] - win["start"] == 300


def test_actual_fill_uses_venue_amounts():
    cand = {"stake": 19.99, "shares": 39.98, "price": 0.5}
    stake, shares, price = _actual_fill(
        {"making_amount": "19.99", "taking_amount": "20.61"},
        cand,
    )
    assert stake == 19.99
    assert shares == 20.61
    assert price == round(19.99 / 20.61, 4)
    assert price > 0.9


def test_actual_fill_does_not_invent_delayed_fill():
    cand = {"stake": 8, "shares": 16, "price": 0.5}
    assert _actual_fill({"making_amount": "0", "taking_amount": "0"}, cand) == (0.0, 0.0, 0.0)


class _BookClient:
    def __init__(self, ask: str, tick: str):
        self.book = SimpleNamespace(
            asks=(SimpleNamespace(price=Decimal(ask), size=Decimal("100")),),
            tick_size=Decimal(tick),
        )

    def get_order_book(self, *, token_id):
        return self.book


def test_buy_ceiling_is_marketable_and_on_tick():
    candidate = {"price": 0.50, "limit_price": 0.50, "fair": 0.75, "kind": "crypto_lag"}
    ceiling, ask, tick = _buy_ceiling(_BookClient("0.501", "0.001"), "token", candidate, Settings())
    assert ask <= ceiling <= Decimal("0.520")
    assert ceiling % tick == 0


def test_buy_ceiling_rejects_moved_book():
    candidate = {"price": 0.50, "limit_price": 0.50, "fair": 0.56, "kind": "crypto_lag"}
    with pytest.raises(LiveDisabled, match="value gone"):
        _buy_ceiling(_BookClient("0.52", "0.01"), "token", candidate, Settings())


def test_buy_ceiling_rejects_resolution_price_collapse():
    candidate = {"price": 0.65, "limit_price": 0.65, "fair": 0.75, "kind": "crypto_lag"}
    with pytest.raises(LiveDisabled, match="price dislocation"):
        _buy_ceiling(_BookClient("0.01", "0.01"), "token", candidate, Settings())


def test_crypto_execution_rejects_stale_signal_and_closing_window():
    candidate = {
        "kind": "crypto_lag",
        "slug": "btc-updown-5m-1000000000",
        "signal_ts": 1000000098.0,
        "clob_quoted_at": 1000000099.0,
    }
    _validate_crypto_execution(candidate, Settings(), now=1000000100.0)
    with pytest.raises(LiveDisabled, match="stale crypto signal"):
        _validate_crypto_execution(candidate, Settings(), now=1000000102.0)
    candidate.update({"signal_ts": 1000000275.0, "clob_quoted_at": 1000000275.0})
    with pytest.raises(LiveDisabled, match="window closing"):
        _validate_crypto_execution(candidate, Settings(), now=1000000275.0)


def test_pair_preflight_reprices_and_resizes_to_budget():
    def book(token, ask):
        return SimpleNamespace(
            token_id=token,
            asks=(SimpleNamespace(price=Decimal(ask), size=Decimal("100")),),
        )

    client = SimpleNamespace(
        get_order_books=lambda **_: (book("yes", "0.45"), book("no", "0.45"))
    )
    candidate = {
        "yes_token": "yes",
        "no_token": "no",
        "yes_ask": 0.40,
        "no_ask": 0.40,
        "shares": 10,
        "stake": 8,
    }
    _refresh_pair_quote(client, candidate, Settings())
    assert candidate["yes_ask"] == 0.45
    assert 5 <= candidate["shares"] < 10


def test_pair_second_leg_failure_unwinds_and_requires_reconcile(monkeypatch):
    now = 1_000_000_100.0
    candidate = {
        "kind": "dip_arb",
        "side": "BOTH",
        "slug": "btc-updown-5m-1000000000",
        "signal_ts": now,
        "clob_quoted_at": now,
        "yes_token": "yes",
        "no_token": "no",
        "yes_ask": 0.45,
        "no_ask": 0.45,
        "shares": 8,
        "stake": 8,
        "first_side": "YES",
    }
    calls = []

    def buy(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return {"making_amount": "3.6", "taking_amount": "8"}
        raise LiveDisabled("second leg gone")

    unwinds = []
    monkeypatch.setattr(execute_module.time, "time", lambda: now)
    monkeypatch.setattr(execute_module, "trading_client", lambda _settings: object())
    monkeypatch.setattr(execute_module, "_refresh_pair_quote", lambda *a, **k: None)
    monkeypatch.setattr(execute_module, "_taker_buy", buy)
    monkeypatch.setattr(execute_module, "_order_payload", lambda row: row)
    monkeypatch.setattr(
        execute_module,
        "_fok_sell",
        lambda client, token, shares: unwinds.append((token, shares)) or {"ok": True},
    )
    with pytest.raises(ReconcileRequired, match="unwound"):
        execute_module._live_buy_both(candidate, Settings(live=True))
    assert unwinds == [("yes", 8.0)]


def test_refresh_crypto_books_replaces_gamma_quotes():
    def book(token, ask, bid):
        return SimpleNamespace(
            token_id=token,
            asks=(SimpleNamespace(price=Decimal(ask), size=Decimal("10")),),
            bids=(SimpleNamespace(price=Decimal(bid), size=Decimal("10")),),
            tick_size=Decimal("0.01"),
        )

    client = SimpleNamespace(
        get_order_books=lambda **_: (book("up", "0.24", "0.22"), book("down", "0.78", "0.76"))
    )
    market = {
        "slug": "btc-updown-5m-1787727900",
        "question": "Bitcoin Up or Down",
        "yes_token": "up",
        "no_token": "down",
        "yes_ask": 0.50,
        "no_ask": 0.52,
    }
    assert _refresh_crypto_books([market], client) == 1
    assert market["yes_ask"] == 0.24
    assert market["no_ask"] == 0.78
    assert market["clob_fresh"] is True


def test_expired_unresolved_crypto_waits_instead_of_selling(monkeypatch):
    pos = {
        "market_id": "old",
        "side": "YES",
        "entry_price": 0.5,
        "shares": 10,
        "stake": 5,
        "slug": "btc-updown-5m-1000000000",
        "question": "Bitcoin Up or Down",
    }
    ledger = {"positions": [pos], "cash": 10, "fills": []}
    market = {**pos, "yes_price": 0.99, "no_price": 0.01}
    monkeypatch.setattr(agent_module, "crypto_settle_price", lambda *a, **k: None)
    monkeypatch.setattr(
        agent_module,
        "live_sell",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not sell expired book")),
    )
    assert agent_module._exits(ledger, {"old": market}, Settings(live=True)) == []
    assert ledger["positions"] == [pos]

def test_survival_dumps_slow_grok_politics(monkeypatch):
    pos = {
        "market_id": "pol",
        "question": "Will Patrick Roath be the Democratic nominee for MA-08?",
        "side": "YES",
        "entry_price": 0.27,
        "shares": 4.22,
        "stake": 1.14,
        "reason": "grok edge=0.1162",
        "yes_token": "y",
        "no_token": "n",
        "slug": "will-patrick-roath-be-the-democratic-nominee-for-ma-08",
    }
    ledger = {"positions": [pos], "cash": 7.6, "fills": []}
    market = {**pos, "yes_price": 0.26, "no_price": 0.74, "days_to_end": 40}
    sold = []
    monkeypatch.setattr(agent_module, "live_sell", lambda p, px, s: sold.append((p, px)) or {"ok": True})
    out = agent_module._exits(ledger, {"pol": market}, Settings(live=True))
    assert out and out[0].get("reason") == "survival_dump"
    assert sold


def test_p_up_endgame_and_mid():
    assert p_up_from_move(0.003, 10, 300) == 0.985
    assert p_up_from_move(-0.003, 10, 300) == 0.015
    mid = p_up_from_move(0.0, 150, 300)
    assert 0.45 < mid < 0.55
    up = p_up_from_move(0.004, 60, 300)
    assert up > 0.7


def test_chainlink_context_requires_boundary_and_fresh_latest():
    with _CHAINLINK_LOCK:
        _CHAINLINK_VALUES["btc"].clear()
        _CHAINLINK_VALUES["btc"].extend([(1000, Decimal("65000")), (1058, Decimal("65010"))])
    assert chainlink_window_context("btc", 1000, 1060) == (65000.0, 65010.0)
    assert chainlink_window_context("btc", 1000, 1070) is None

def test_entry_window_skips_open_and_lock():
    assert in_entry_window(220, 300, now=1000, start=800) is True
    assert in_entry_window(40, 300, now=1000, start=800) is False
    assert in_entry_window(260, 300, now=1000, start=800) is False
    assert in_entry_window(400, 900, now=2000, start=1300) is True
    assert in_entry_window(60, 900, now=2000, start=1300) is False


def test_score_hits_cheap_favorite_early_in_window(monkeypatch):
    start = 1_700_000_000
    now = start + 80
    market = {
        "id": "btc-early",
        "slug": f"btc-updown-5m-{start}",
        "question": "Bitcoin Up or Down",
        "yes_ask": 0.72,
        "no_ask": 0.30,
        "clob_fresh": True,
        "description": "Chainlink 60s-stream TWAP",
    }
    monkeypatch.setattr("scout.crypto_lag.time.time", lambda: now)
    monkeypatch.setattr("scout.crypto_lag.start_chainlink_stream", lambda: None)
    monkeypatch.setattr("scout.crypto_lag.chainlink_window_context", lambda *a, **k: (100.0, 100.35))
    monkeypatch.setattr("scout.crypto_lag._binance_spot", lambda s: 100.40)
    monkeypatch.setattr("scout.crypto_lag._binance_open", lambda s, t: 100.0)
    monkeypatch.setattr(
        "scout.crypto_lag._binance_recent_closes",
        lambda *a, **k: [100.0, 100.05, 99.97, 100.1, 100.02, 100.08],
    )
    out = score_crypto_windows([market], Settings(crypto_min_edge=0.03))
    assert "btc-early" in out
    assert out["btc-early"]["p_yes"] >= 0.80


def test_score_skips_priced_in_favorite(monkeypatch):
    start = 1_700_000_300
    now = start + 200
    market = {
        "id": "btc-late",
        "slug": f"btc-updown-5m-{start}",
        "question": "Bitcoin Up or Down",
        "yes_ask": 0.98,
        "no_ask": 0.03,
        "clob_fresh": True,
        "description": "Chainlink 60s-stream TWAP",
    }
    monkeypatch.setattr("scout.crypto_lag.time.time", lambda: now)
    monkeypatch.setattr("scout.crypto_lag.start_chainlink_stream", lambda: None)
    monkeypatch.setattr("scout.crypto_lag.chainlink_window_context", lambda *a, **k: (100.0, 100.20))
    monkeypatch.setattr("scout.crypto_lag._binance_spot", lambda s: 100.22)
    monkeypatch.setattr("scout.crypto_lag._binance_open", lambda s, t: 100.0)
    monkeypatch.setattr(
        "scout.crypto_lag._binance_recent_closes",
        lambda *a, **k: [100.0, 100.05, 99.97, 100.1, 100.02, 100.08],
    )
    out = score_crypto_windows([market], Settings(crypto_min_edge=0.03))
    assert out == {}


def test_score_hits_15m_favorite(monkeypatch):
    start = 1_700_001_000
    now = start + 240
    market = {
        "id": "sol-15",
        "slug": f"sol-updown-15m-{start}",
        "question": "Solana Up or Down",
        "yes_ask": 0.70,
        "no_ask": 0.32,
        "clob_fresh": True,
        "description": "Chainlink 60s-stream TWAP",
    }
    monkeypatch.setattr("scout.crypto_lag.time.time", lambda: now)
    monkeypatch.setattr("scout.crypto_lag.start_chainlink_stream", lambda: None)
    monkeypatch.setattr("scout.crypto_lag.chainlink_window_context", lambda *a, **k: (150.0, 151.2))
    monkeypatch.setattr("scout.crypto_lag._binance_spot", lambda s: 151.4)
    monkeypatch.setattr("scout.crypto_lag._binance_open", lambda s, t: 150.0)
    monkeypatch.setattr(
        "scout.crypto_lag._binance_recent_closes",
        lambda *a, **k: [150.0, 150.2, 149.8, 150.4, 150.1, 150.6],
    )
    out = score_crypto_windows([market], Settings(crypto_min_edge=0.03))
    assert "sol-15" in out
    row = out["sol-15"]
    assert row["raw_model_p_yes"] > row["p_yes"] > row["market_p_yes"]
    assert row["p_yes"] >= 0.75


def test_score_spot_source_switch_uses_raw_oracle_tick(monkeypatch):
    """CRYPTO_SPOT_SOURCE=raw scores with the fresh raw tick; default keeps the TWAP."""
    start = 1_700_000_000
    now = start + 100  # 200s left: inside the entry window
    market = {
        "id": "btc-raw",
        "slug": f"btc-updown-5m-{start}",
        "question": "Bitcoin Up or Down",
        "yes_ask": 0.80,
        "no_ask": 0.21,
        "clob_fresh": True,
        "description": "Chainlink 60s-stream TWAP",
    }
    monkeypatch.setattr("scout.crypto_lag.time.time", lambda: now)
    monkeypatch.setattr("scout.crypto_lag.start_chainlink_stream", lambda: None)
    monkeypatch.setattr("scout.streams.start_streams", lambda: None)
    monkeypatch.setattr("scout.streams.exchange_spot", lambda *a, **k: None)
    # TWAP has barely moved (+2 bps) while the raw oracle already printed +40 bps.
    monkeypatch.setattr("scout.crypto_lag.chainlink_window_context", lambda *a, **k: (100.0, 100.02))
    monkeypatch.setattr("scout.streams.oracle_spot", lambda *a, **k: (100.40, float(now)))
    monkeypatch.setattr("scout.crypto_lag._binance_spot", lambda s: 100.40)
    monkeypatch.setattr("scout.crypto_lag._binance_open", lambda s, t: 100.0)
    monkeypatch.setattr(
        "scout.crypto_lag._binance_recent_closes",
        lambda *a, **k: [100.0, 100.05, 99.97, 100.1, 100.02, 100.08],
    )
    monkeypatch.setenv("CRYPTO_SPOT_SOURCE", "raw")
    out = score_crypto_windows([market], Settings(crypto_min_edge=0.03))
    assert "btc-raw" in out
    row = out["btc-raw"]
    assert row["oracle_spot"] == 100.40
    assert "chainlink_raw" in row["thesis"]
    assert row["p_yes"] >= 0.80

    monkeypatch.setenv("CRYPTO_SPOT_SOURCE", "twap")
    out2 = score_crypto_windows([market], Settings(crypto_min_edge=0.03))
    twap_row = out2.get("btc-raw")
    assert twap_row is None or (twap_row["oracle_spot"] == 100.02 and twap_row["p_yes"] < 0.70)
