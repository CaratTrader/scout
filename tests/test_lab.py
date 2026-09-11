"""Strategy lab: decisions on synthetic states, the replay fill model, sizing, ledgers."""
from __future__ import annotations

import math

from lab.arena import Ledger
from lab.replay import maker_filled, next_print, stake_for
from lab.strategies import Fade, LagModel, Lock, Momentum, Order, State, by_name, catalog, to_maker


def make_state(**kw) -> State:
    base = dict(
        asset="btc", mins=5, epoch=1_700_000_000, t=1_700_000_120.0, seconds_left=180.0,
        open_px=100_000.0, spot_raw=100_000.0, spot_twap=100_000.0, sigma=0.0010,
        yes_ask=0.50, yes_bid=0.49, no_ask=0.51, no_bid=0.50, yes_hist=[], hour_utc=15,
    )
    base.update(kw)
    return State(**base)


def test_catalog_has_at_least_twenty_distinct_strategies():
    names = [s.name for s in catalog()]
    assert len(names) >= 20
    assert len(set(names)) == len(names)
    families = {s.family for s in catalog()}
    assert {"lag", "momentum", "fade", "jump", "lock", "maker", "filter", "ensemble", "cross"} <= families


def test_state_z_and_market_p():
    s = make_state(spot_raw=100_100.0)  # +10 bps
    assert s.chg("raw") > 0 and s.chg("twap") == 0
    assert s.z("raw") > 0.5
    assert abs(s.market_p() - 0.495) < 1e-9


def test_momentum_buys_favourite_only_after_a_large_move():
    strat = Momentum("m", 1.0)
    flat = make_state()
    assert strat.decide(flat) is None
    moved = make_state(spot_raw=100_120.0, yes_ask=0.62, yes_bid=0.60, no_ask=0.40, no_bid=0.38)
    order = strat.decide(moved)
    assert order is not None and order.side == "YES" and order.price == 0.62
    pricey = make_state(spot_raw=100_120.0, yes_ask=0.90, yes_bid=0.89, no_ask=0.11, no_bid=0.10)
    assert strat.decide(pricey) is None  # above the ask cap


def test_lag_model_refuses_when_no_edge_and_honours_trust_cap():
    strat = LagModel("l", "raw")
    assert strat.decide(make_state()) is None  # 50/50 book, no move
    # a big move but the book already at 0.95: outside the ask cap -> no trade
    assert strat.decide(make_state(spot_raw=100_300.0, yes_ask=0.95, yes_bid=0.94, no_ask=0.06, no_bid=0.05)) is None


def test_lock_requires_endgame_and_multi_sigma_lead():
    strat = Lock("k", 2.0)
    early = make_state(spot_raw=100_150.0, seconds_left=200.0)
    assert not strat.applies(early)
    late = make_state(spot_raw=100_150.0, seconds_left=60.0, t=1_700_000_240.0, yes_ask=0.80, yes_bid=0.78, no_ask=0.22, no_bid=0.20)
    assert strat.applies(late)
    order = strat.decide(late)
    assert order is not None and order.side == "YES" and order.fair > 0.95


def test_fade_only_when_flat_and_underdog_cheap():
    strat = Fade("f", 0.35)
    s = make_state(yes_ask=0.35, yes_bid=0.33, no_ask=0.67, no_bid=0.65)
    order = strat.decide(s)
    assert order is not None and order.side == "YES"
    assert strat.decide(make_state(spot_raw=100_200.0, yes_ask=0.35, yes_bid=0.33, no_ask=0.67, no_bid=0.65)) is None


def test_maker_conversion_rests_below_the_ask_with_zero_fee_edge():
    s = make_state(yes_ask=0.40, yes_bid=0.38, no_ask=0.62, no_bid=0.60)
    o = to_maker(Order("YES", 0.40, 0.50, 0.08), s)
    assert o is not None and o.kind == "maker" and o.price == 0.39 and abs(o.edge - 0.11) < 1e-9


def test_next_print_and_maker_fill_rules():
    points = [(100, 0.50), (160, 0.55), (220, 0.70)]
    assert next_print(points, 150) == (0.55, 10)
    assert next_print(points, 230) is None
    w = {"points": points}
    # a NO limit fills once the YES print is at or above 1 - limit
    assert maker_filled(Order("NO", 0.45, 0.6, 0.1, kind="maker"), w, t=100, end=400) is True  # 0.55 at t=160
    assert maker_filled(Order("NO", 0.25, 0.6, 0.1, kind="maker"), w, t=100, end=400) is False  # needs 0.75, never printed
    assert maker_filled(Order("YES", 0.50, 0.6, 0.1, kind="maker"), w, t=100, end=400) is False  # never trades below 0.50 after t
    assert maker_filled(Order("YES", 0.55, 0.6, 0.1, kind="maker"), w, t=100, end=400) is True


def test_sizing_rules():
    assert stake_for("fixed5", 50.0, 0.6, 0.5, 0.0) == 5.0
    assert stake_for("fixed5", 3.0, 0.6, 0.5, 0.0) == 3.0
    k = stake_for("kelly", 50.0, 0.60, 0.50, 0.0)
    assert 2.0 <= k <= 5.0
    assert stake_for("qkelly", 50.0, 0.60, 0.50, 0.0) <= k


def test_ledger_stats_from_fills():
    L = Ledger("x", {"bankroll": 55.0, "fills": [
        {"pnl5": 4.0, "ret": 0.8, "won": True, "fair": 0.7, "price": 0.5, "closed_ts": 10.0},
        {"pnl5": -5.0, "ret": -1.0, "won": False, "fair": 0.7, "price": 0.5, "closed_ts": 20.0},
        {"pnl5": 4.0, "ret": 0.8, "won": True, "fair": 0.7, "price": 0.5, "closed_ts": 30.0},
    ]})
    st = L.stats(now=40.0)
    assert st["n"] == 3 and st["wins"] == 2 and abs(st["pnl5"] - 3.0) < 1e-9
    assert st["max_dd5"] == 5.0 and st["pnl_rule"] == 5.0
    assert st["n_24h"] == 3


def test_every_strategy_survives_a_flat_state_without_error():
    s = make_state()
    for strat in catalog():
        if strat.applies(s):
            strat.decide(s)
    assert "lag_twap" in by_name()


def test_arena_rebuilds_settlement_schedule_from_restored_positions(monkeypatch, tmp_path):
    import json
    from lab import arena as arena_module

    ledgers = tmp_path / "ledgers.json"
    ledgers.write_text(json.dumps({"lock_k2": {"bankroll": 45.0, "positions": {"m1": {
        "side": "YES", "price": 0.9, "fair": 0.97, "edge": 0.06, "kind": "taker", "stake": 5.0, "shares": 5.55, "fee": 0.03,
        "cost": 5.03, "opened_ts": 1.0, "question": "q", "asset": "btc", "mins": 5, "epoch": 1_700_000_000}}, "orders": {}, "fills": []}}))
    monkeypatch.setattr(arena_module, "LEDGERS_PATH", ledgers)
    monkeypatch.setattr(arena_module, "TICKS", tmp_path)
    a = arena_module.Arena()
    assert "m1" in a.pending
    assert a.pending["m1"]["end"] == 1_700_000_300
    assert a.ledgers["lock_k2"].bankroll == 45.0


def test_lock_twap_probability_and_persistence():
    from lab.strategies import LockTwap

    strat = LockTwap("lt", p_min=0.95, persist=3)
    epoch = 1_700_000_000
    # 60 s left, oracle sat 10 bps above the open for the whole window, sigma 0.10%/window
    hist = [(float(epoch + i), 100_100.0) for i in range(0, 241)]
    s = make_state(epoch=epoch, t=float(epoch + 240), seconds_left=60.0, spot_raw=100_100.0, sigma=0.0010,
                   yes_ask=0.80, yes_bid=0.78, no_ask=0.22, no_bid=0.20, oracle_hist=hist)
    assert strat.p_up(s) > 0.95
    assert strat.decide(s) is None and strat.decide(s) is None  # persistence: 3 consecutive looks
    order = strat.decide(s)
    assert order is not None and order.side == "YES" and order.price == 0.80
    # flat window: no lock either way, and the streak resets
    flat = make_state(epoch=epoch, t=float(epoch + 240), seconds_left=60.0, spot_raw=100_000.0, sigma=0.0010, oracle_hist=[(float(epoch + i), 100_000.0) for i in range(0, 241)])
    assert 0.3 < strat.p_up(flat) < 0.7 and strat.decide(flat) is None
    # the book disagrees hard (YES at 0.34): abstain even with a lock reading
    cheap = make_state(epoch=epoch, t=float(epoch + 240), seconds_left=60.0, spot_raw=100_100.0, sigma=0.0010, yes_ask=0.34, yes_bid=0.32, no_ask=0.68, no_bid=0.66, oracle_hist=hist)
    for _ in range(4):
        assert strat.decide(cheap) is None


def test_persistent_momentum_needs_consecutive_confirmation():
    strat = Momentum("mp", 1.0, persist=3)
    moved = make_state(spot_raw=100_120.0, yes_ask=0.62, yes_bid=0.60, no_ask=0.40, no_bid=0.38)
    assert strat.decide(moved) is None and strat.decide(moved) is None
    assert strat.decide(moved) is not None


def test_scout_clone_carries_engine_flags():
    s = by_name()["scout_clone"]
    assert s.cooldown_s == 900.0 and s.sample_every_s == 35.0 and s.underdog_only
    assert by_name()["lag_raw"].cooldown_s == 0.0


def test_state_history_accessors_never_see_the_future():
    s = make_state(t=1_700_000_120.0, yes_hist=[(1_700_000_100.0, 0.40), (1_700_000_119.0, 0.45), (1_700_000_200.0, 0.99)])
    assert s.yes_now() == 0.45
    assert s.yes_price_ago(15) == 0.40


def test_market_maker_quotes_both_sides_under_fair():
    from lab.strategies import MakerBoth

    strat = MakerBoth("mm", offset=0.03)
    s = make_state(yes_ask=0.52, yes_bid=0.50, no_ask=0.50, no_bid=0.48)
    q = strat.quotes(s)
    assert {o.side for o in q} == {"YES", "NO"} and all(o.kind == "maker" for o in q)
    for o in q:
        assert o.price <= (s.yes_ask if o.side == "YES" else s.no_ask) - 0.01 + 1e-9
        assert o.edge >= 0.025 - 1e-9  # offset minus rounding to the 1c tick grid
    assert strat.multi_leg


def test_arena_settles_multi_leg_positions(monkeypatch, tmp_path):
    import json
    from lab import arena as arena_module

    ledgers = tmp_path / "ledgers.json"
    pos = lambda side, price: {"market_id": "m9", "side": side, "price": price, "fair": 0.5, "edge": 0.03, "kind": "maker", "stake": 5.0, "shares": round(5.0 / price, 4), "fee": 0.0,
                               "cost": 5.0, "opened_ts": 1.0, "question": "q", "asset": "btc", "mins": 5, "epoch": 1_700_000_000}
    ledgers.write_text(json.dumps({"mm_fair3": {"bankroll": 40.0, "positions": {"m9:YES": pos("YES", 0.47), "m9:NO": pos("NO", 0.49)}, "orders": {}, "fills": []}}))
    monkeypatch.setattr(arena_module, "LEDGERS_PATH", ledgers)
    monkeypatch.setattr(arena_module, "TICKS", tmp_path)
    monkeypatch.setattr(arena_module, "fetch_outcome", lambda mid: 1.0)
    a = arena_module.Arena()
    a.pending["m9"]["due"] = 0.0
    a.settle(now=1_700_000_500.0)
    L = a.ledgers["mm_fair3"]
    assert not L.positions and len(L.fills) == 2
    yes = next(f for f in L.fills if f["side"] == "YES")
    assert yes["won"] and abs(L.bankroll - (40.0 + 5.0 / 0.47)) < 1e-3  # complete set: the YES leg pays $1/share


def test_basis_and_obi_filters_require_coinbase_agreement():
    from lab.strategies import BasisFilter, ObiFilter, Momentum

    base = Momentum("m", 1.0)
    bf = BasisFilter("bf", base, min_bps=0.15)
    ob = ObiFilter("ob", base, min_imb=0.2)
    up = dict(spot_raw=100_120.0, yes_ask=0.62, yes_bid=0.60, no_ask=0.40, no_bid=0.38)
    # no Coinbase data -> abstain
    assert bf.decide(make_state(**up)) is None and ob.decide(make_state(**up)) is None
    # Coinbase above the oracle by 0.5 bp and bids heavier: YES passes both filters
    s = make_state(**up, cb_price=100_125.0, cb_ts=1_700_000_120.0, cb_bid_size=3.0, cb_ask_size=1.0)
    assert abs(s.basis_bps() - 0.499) < 0.01 and s.cb_imbalance() == 0.5
    assert bf.decide(s) is not None and ob.decide(s) is not None
    # Coinbase below the oracle: the YES momentum order is refused
    s2 = make_state(**up, cb_price=100_110.0, cb_ts=1_700_000_120.0, cb_bid_size=1.0, cb_ask_size=3.0)
    assert bf.decide(s2) is None and ob.decide(s2) is None
    # stale Coinbase tick -> treated as missing
    s3 = make_state(**up, cb_price=100_125.0, cb_ts=1_700_000_100.0)
    assert s3.basis_bps() is None and bf.decide(s3) is None
    # coinbase as spot
    assert make_state(spot_raw=100_000.0, cb_price=100_050.0, cb_ts=1_700_000_120.0).chg("coinbase") > 0
