from scout.config import Settings
from scout.ledger import close_position, empty_ledger, mark_to_market, maybe_halt, record_fill


def test_fill_and_close_roundtrip():
    settings = Settings()
    ledger = empty_ledger(settings)
    market = {"id": "9", "question": "Q?", "yes_token": "y", "no_token": "n"}
    record_fill(
        ledger,
        market=market,
        side="YES",
        stake=3.0,
        price=0.5,
        shares=6.0,
        reason="test",
        mode="paper",
        settings=settings,
    )
    assert ledger["cash"] == 47.0
    assert len(ledger["positions"]) == 1
    pos = ledger["positions"][0]
    close_position(ledger, pos, price=0.6, reason="take", mode="paper")
    assert ledger["positions"] == []
    assert ledger["cash"] == 50.6


def test_drawdown_halt():
    settings = Settings(starting_bankroll=50, max_drawdown=0.25)
    ledger = empty_ledger(settings)
    ledger["cash"] = 30
    ledger["risk_baseline_cash"] = 50
    maybe_halt(ledger, equity=30, settings=settings)
    assert ledger["halted"] is True


def test_drawdown_halt_uses_session_baseline_not_original_50():
    settings = Settings(starting_bankroll=50, max_drawdown=0.55, halt_bankroll=18)
    ledger = empty_ledger(settings)
    ledger["cash"] = 19.58
    ledger["risk_baseline_cash"] = 19.58
    maybe_halt(ledger, equity=19.58, settings=settings)
    assert ledger["halted"] is False


def test_cash_halt_on_funded_book_uses_halt_bankroll():
    settings = Settings(max_session_drawdown=6, halt_bankroll=18, max_drawdown=0.55)
    ledger = empty_ledger(settings)
    ledger["cash"] = 17.0
    ledger["risk_baseline_cash"] = 24.0
    maybe_halt(ledger, equity=17.0, settings=settings)
    assert ledger["halted"] is True
    assert ledger["cash_floor"] == 18.0


def test_tiny_book_is_allowed_clips_down_to_two_fifty():
    settings = Settings(max_session_drawdown=6, halt_bankroll=18, max_drawdown=0.55)
    ledger = empty_ledger(settings)
    ledger["cash"] = 9.17
    ledger["risk_baseline_cash"] = 9.17
    maybe_halt(ledger, equity=9.17, settings=settings)
    assert ledger["halted"] is False
    ledger["cash"] = 5.0
    maybe_halt(ledger, equity=5.0, settings=settings)
    assert ledger["halted"] is False
    ledger["cash"] = 2.0
    maybe_halt(ledger, equity=2.0, settings=settings)
    assert ledger["halted"] is True


def test_mark_yes_position():
    ledger = {
        "cash": 47.0,
        "positions": [{"market_id": "1", "side": "YES", "shares": 6.0, "entry_price": 0.5}],
    }
    equity = mark_to_market(ledger, {"1": {"yes_price": 0.6, "no_price": 0.4}})
    assert equity == 50.6


def test_mark_does_not_use_quote_older_than_fill():
    ledger = {
        "cash": 9.9,
        "positions": [
            {
                "market_id": "1",
                "side": "NO",
                "shares": 660.0,
                "entry_price": 0.01,
                "opened_at": "2026-08-28T00:25:05+00:00",
            }
        ],
    }
    stale = {
        "no_price": 0.505,
        "no_bid": 0.50,
        "clob_fresh": True,
        "clob_quoted_at": 1787876505.0,
    }
    assert mark_to_market(ledger, {"1": stale}) == 16.5


def test_fill_fee_is_cash_cost_and_realized_pnl():
    settings = Settings(starting_bankroll=20)
    ledger = empty_ledger(settings)
    market = {"id": "fee", "question": "Q?", "yes_token": "y", "no_token": "n"}
    record_fill(
        ledger,
        market=market,
        side="NO",
        stake=6.6,
        fee=0.4574,
        price=0.01,
        shares=660,
        reason="test",
        mode="paper",
        settings=settings,
    )
    assert ledger["cash"] == 12.9426
    closed = close_position(ledger, ledger["positions"][0], price=0, reason="resolved", mode="paper")
    assert closed["pnl"] == -7.0574


def test_campaign_stops_when_flat_at_target():
    settings = Settings(campaign_target_usd=100)
    ledger = empty_ledger(settings)
    ledger["cash"] = 100.01
    maybe_halt(ledger, equity=100.01, settings=settings)
    assert ledger["halted"] is True
    assert ledger["halt_reason"].startswith("campaign target reached")
