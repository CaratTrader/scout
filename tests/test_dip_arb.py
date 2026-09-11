from scout.config import Settings
from scout.dip_arb import QuoteTape, pair_net_edge, size_complete_set
from scout.ledger import empty_ledger
from scout.risk import veto


def _mkt(**overrides):
    start = 1_700_000_000
    row = {
        "id": "btc1",
        "slug": f"btc-updown-5m-{start}",
        "question": "Bitcoin Up or Down",
        "yes_ask": 0.70,
        "no_ask": 0.22,
        "yes_ask_size": 40.0,
        "no_ask_size": 40.0,
        "clob_fresh": True,
        "yes_token": "y",
        "no_token": "n",
        "_cash": 7.6,
    }
    row.update(overrides)
    return row


def test_pair_net_edge_survives_fees():
    edge = pair_net_edge(0.45, 0.45, 0.07)
    assert 0.05 < edge < 0.08


def test_dip_15pct_in_3s_with_cheap_pair_fires():
    tape = QuoteTape()
    start = 1_700_000_000
    now0 = start + 60
    tape.observe(_mkt(yes_ask=0.82, no_ask=0.20), now0)
    now = now0 + 3.1
    current = _mkt(yes_ask=0.68, no_ask=0.22)
    tape.observe(current, now)
    sig = tape.signal(current, now, Settings(dip_threshold=0.12, dip_window_ms=3000))
    assert sig is not None
    assert sig["kind"] == "dip_arb"
    assert sig["first_side"] == "YES"
    assert sig["side"] == "BOTH"
    assert sig["edge"] >= 0.015


def test_dip_without_cheap_pair_is_a_thin_print_not_a_trade():
    tape = QuoteTape()
    start = 1_700_000_000
    now0 = start + 60
    tape.observe(_mkt(yes_ask=0.80, no_ask=0.22), now0)
    now = now0 + 3.1
    current = _mkt(yes_ask=0.66, no_ask=0.40)
    tape.observe(current, now)
    sig = tape.signal(current, now, Settings(dip_threshold=0.12, pair_sum_target=0.97))
    assert sig is None


def test_thin_book_is_skipped():
    tape = QuoteTape()
    start = 1_700_000_000
    now = start + 80
    current = _mkt(yes_ask=0.45, no_ask=0.45, yes_ask_size=1.0, no_ask_size=1.0)
    tape.observe(current, now)
    sig = tape.signal(current, now, Settings(min_book_shares=8, completeness_min_edge=0.02))
    assert sig is None


def test_complete_set_without_dip_still_fires():
    tape = QuoteTape()
    start = 1_700_000_000
    now = start + 80
    current = _mkt(yes_ask=0.46, no_ask=0.46)
    tape.observe(current, now)
    sig = tape.signal(current, now, Settings(completeness_min_edge=0.02, dip_threshold=0.15))
    assert sig is not None
    assert sig["kind"] == "completeness"
    assert sig["side"] == "BOTH"


def test_size_complete_set_fits_tiny_book():
    shares, stake = size_complete_set(7.6, 0.46, 0.46, Settings(max_fraction=0.5, max_crypto_stake=8))
    assert shares >= 3
    assert 2.5 <= stake <= 4.2


def test_live_crypto_pair_is_not_vetoed():
    settings = Settings(live=True, max_crypto_positions=2, max_fraction=0.5, min_trade=1, max_crypto_stake=8)
    ledger = empty_ledger(settings)
    ledger["cash"] = 7.6
    ledger["risk_baseline_cash"] = 7.6
    ledger["last_equity"] = 7.6
    ledger["starting_bankroll"] = 7.6
    cand = {
        "id": "btc1",
        "kind": "dip_arb",
        "side": "BOTH",
        "stake": 3.2,
        "price": 0.90,
        "yes_ask": 0.68,
        "no_ask": 0.22,
        "edge": 0.07,
        "fair": 1.0,
        "slug": "btc-updown-5m-1700000000",
        "question": "Bitcoin Up or Down",
        "shares": 3.5,
    }
    assert veto(cand, ledger, settings) is None
