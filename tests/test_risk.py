from datetime import datetime, timezone
import threading
import time

from scout.config import Settings
from scout.math_risk import taker_fee_per_share
from scout.ledger import empty_ledger, record_fill, record_order
from scout.risk import maker_buy_price, veto
from scout.agent import _apply_risk_state, grok_skip_reason
from scout.crypto_lag import crypto_side_ok
import scout.agent as agent_module


def test_maker_buy_never_crosses_ask():
    assert maker_buy_price(0.40, 0.42) == 0.40
    assert maker_buy_price(0.0, 0.42) == 0.41
    assert maker_buy_price(0.42, 0.42) == 0.41
    assert maker_buy_price(0.50, 0.01) == 0.0


def test_taker_fee_peaks_near_half():
    mid = taker_fee_per_share(0.50, 0.05)
    wing = taker_fee_per_share(0.90, 0.05)
    assert mid > wing
    assert mid == 0.0125


def test_veto_max_stake_and_already_in():
    settings = Settings(max_stake_usd=5, max_fraction=0.10, min_trade=1)
    ledger = empty_ledger(settings)
    cand = {
        "id": "1",
        "kind": "grok",
        "side": "YES",
        "stake": 9,
        "price": 0.40,
        "limit_price": 0.40,
        "edge": 0.10,
        "fair": 0.61,
        "confidence": 0.9,
        "already_priced": False,
        "edge_type": "stale_news",
        "volume_24h": 8000,
        "days_to_end": 5,
    }
    assert veto(cand, ledger, settings) == "max_stake"
    cand["stake"] = 4
    record_order(
        ledger,
        market={"id": "1", "question": "Q", "yes_token": "y", "no_token": "n"},
        side="YES",
        stake=4,
        price=0.40,
        shares=10,
        reason="t",
        mode="paper",
        settings=settings,
    )
    assert veto(cand, ledger, settings) == "already_in"


def test_veto_99_trap_and_already_priced():
    settings = Settings()
    ledger = empty_ledger(settings)
    base = {
        "id": "2",
        "kind": "grok",
        "side": "YES",
        "stake": 3,
        "price": 0.99,
        "limit_price": 0.99,
        "edge": 0.01,
        "fair": 0.995,
        "confidence": 0.99,
        "already_priced": False,
        "edge_type": "cheap_convex",
        "volume_24h": 8000,
        "days_to_end": 3,
    }
    assert veto(base, ledger, settings) == "extreme_price"
    base["price"] = 0.40
    base["limit_price"] = 0.40
    base["already_priced"] = True
    assert veto(base, ledger, settings) == "already_priced"


def test_veto_blocks_btc_eth_not_btc_sol():
    settings = Settings(max_crypto_positions=2, max_crypto_stake=40, max_fraction=0.4, min_trade=1)
    ledger = empty_ledger(settings)
    record_fill(
        ledger,
        market={
            "id": "btc1",
            "question": "Bitcoin Up or Down",
            "slug": "btc-updown-5m-1787770000",
            "yes_token": "y",
            "no_token": "n",
            "asset": "btc",
        },
        side="YES",
        stake=8,
        price=0.4,
        shares=20,
        reason="t",
        mode="paper",
        settings=settings,
    )
    eth = {
        "id": "eth1",
        "kind": "crypto_lag",
        "side": "YES",
        "stake": 8,
        "price": 0.78,
        "limit_price": 0.78,
        "asset": "eth",
        "edge": 0.2,
        "fair": 0.92,
        "edge_type": "crypto_lag",
        "slug": "eth-updown-5m-1787770000",
    }
    sol = {**eth, "id": "sol1", "asset": "sol", "slug": "sol-updown-5m-1787770000"}
    assert veto(eth, ledger, settings) == "correlated"
    assert veto(sol, ledger, settings) is None


def test_loss_cooldown_blocks_same_asset():
    settings = Settings(max_crypto_positions=1, max_fraction=0.4, min_trade=1)
    ledger = empty_ledger(settings)
    ledger["fills"] = [
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "side": "CLOSE_YES",
            "pnl": -4.2,
            "question": "Bitcoin Up or Down - August 26, 6:25PM-6:30PM ET",
            "slug": "btc-updown-5m-1787780700",
        }
    ]
    cand = {
        "id": "x",
        "kind": "crypto_lag",
        "side": "YES",
        "stake": 4,
        "price": 0.78,
        "limit_price": 0.78,
        "asset": "btc",
        "edge": 0.2,
        "fair": 0.92,
        "edge_type": "crypto_lag",
        "slug": "btc-updown-5m-1787780300",
    }
    assert veto(cand, ledger, settings) == "loss_cooldown"


def test_veto_blocks_cheap_crypto_tickets():
    settings = Settings(max_crypto_positions=1, max_fraction=0.4, min_trade=1, max_crypto_stake=12)
    ledger = empty_ledger(settings)
    cand = {
        "id": "eth-down",
        "kind": "crypto_lag",
        "side": "NO",
        "stake": 4,
        "price": 0.2469,
        "limit_price": 0.2469,
        "asset": "eth",
        "edge": 0.29,
        "fair": 0.54,
        "edge_type": "crypto_lag",
    }
    assert veto(cand, ledger, settings) == "no_underdog"
    cand["price"] = 0.32
    cand["limit_price"] = 0.32
    cand["side"] = "YES"
    assert veto(cand, ledger, settings) == "no_underdog"
    cand["price"] = 0.48
    cand["limit_price"] = 0.48
    cand["fair"] = 0.65
    # 17c gap with model-fair > ask: a legitimate entry under the EV gate.
    assert veto(cand, ledger, settings) is None
    cand["price"] = 0.78
    cand["limit_price"] = 0.78
    cand["fair"] = 0.92
    cand["slug"] = "eth-updown-5m-1"
    assert veto(cand, ledger, settings) is None


def test_crypto_side_ok_ev_gate():
    # EV gate: refuse lotteries, near-locks, overpaying, and believe-nothing gaps.
    assert crypto_side_ok(0.04, 0.54) is False   # lottery floor
    assert crypto_side_ok(0.93, 0.97) is False   # near-lock, one tick = -100%
    assert crypto_side_ok(0.49, 0.60) is True    # the ask=0.49 fair=0.60 kill, now passes
    assert crypto_side_ok(0.40, 0.52) is True    # the ask=0.40 fair=0.52 kill, now passes
    assert crypto_side_ok(0.32, 0.54) is False   # 22c gap: book knows something we don't
    assert crypto_side_ok(0.62, 0.75) is True
    assert crypto_side_ok(0.62, 0.82) is True
    assert crypto_side_ok(0.88, 0.70) is False   # model says we're overpaying
    assert crypto_side_ok(0.62, 0.63) is True    # barely model-fair still allowed


def test_grok_idle_while_crypto_open():
    settings = Settings(grok_every_seconds=1800)
    ledger = empty_ledger(settings)
    assert grok_skip_reason(ledger, {}, settings, use_grok=True, now=10_000) is None
    ledger["positions"] = [
        {"market_id": "1", "question": "Bitcoin Up or Down", "slug": "btc-updown-5m-1"}
    ]
    assert grok_skip_reason(ledger, {}, settings, use_grok=True, now=10_000) is None
    ledger["positions"] = []
    scores = {"1": {"edge_type": "crypto_lag"}}
    assert grok_skip_reason(ledger, scores, settings, use_grok=True, now=10_000) is None
    ledger["last_grok_ts"] = 9_500
    assert grok_skip_reason(ledger, {}, settings, use_grok=True, now=10_000).startswith("cooldown")


def test_deposit_rebases_session_instead_of_profit_lock():
    settings = Settings(profit_lock_fraction=0.5, max_session_drawdown=6, halt_bankroll=18)
    ledger = {
        "cash": 24.2,
        "positions": [],
        "fills": [],
        "risk_model_version": 2,
        "risk_baseline_cash": 9.17,
        "realized_peak_cash": 9.17,
        "halted": False,
        "halt_reason": "",
    }
    _apply_risk_state(ledger, 24.2, settings)
    assert ledger["risk_baseline_cash"] == 24.2
    assert ledger["halted"] is False
    # A winning clip must not look like another deposit.
    ledger["cash"] = 30.0
    _apply_risk_state(ledger, 30.0, settings)
    assert ledger["risk_baseline_cash"] == 24.2


def test_two_crypto_losses_halt_when_flat():
    settings = Settings(max_consecutive_crypto_losses=2, max_session_drawdown=100, halt_bankroll=1)
    ledger = empty_ledger(settings)
    ledger["risk_model_version"] = 2
    ledger["risk_baseline_cash"] = 24
    ledger["realized_peak_cash"] = 24
    ledger["cash"] = 16
    ledger["session_started_at"] = "2026-08-27T00:00:00+00:00"
    ledger["fills"] = [
        {
            "ts": "2026-08-27T02:00:00+00:00",
            "side": "CLOSE_YES",
            "pnl": -4,
            "question": "Bitcoin Up or Down - August 26, 8:50PM-8:55PM ET",
            "slug": "btc-updown-5m-1",
        },
        {
            "ts": "2026-08-27T02:10:00+00:00",
            "side": "CLOSE_NO",
            "pnl": -4,
            "question": "Ethereum Up or Down - August 26, 8:40PM-8:45PM ET",
            "slug": "eth-updown-5m-2",
        },
    ]
    _apply_risk_state(ledger, 16, settings)
    assert ledger["halted"] is True
    assert "streak" in ledger["halt_reason"]


def test_profit_lock_uses_realized_peak():
    settings = Settings(profit_lock_fraction=0.5, max_session_drawdown=100)
    ledger = {
        "cash": 59,
        "positions": [],
        "risk_model_version": 2,
        "risk_baseline_cash": 50,
        "realized_peak_cash": 70,
        "halted": False,
        "halt_reason": "",
    }
    _apply_risk_state(ledger, 59, settings)
    assert ledger["locked_equity_floor"] == 60
    assert ledger["halted"] is True
    assert ledger["halt_reason"].startswith("profit lock")


def test_profit_lock_ignores_tiny_sync_bump():
    settings = Settings(profit_lock_fraction=0.5, max_session_drawdown=6)
    ledger = {
        "cash": 17.57,
        "positions": [],
        "risk_model_version": 2,
        "risk_baseline_cash": 19.58,
        "realized_peak_cash": 21.71,
        "halted": False,
        "halt_reason": "",
    }
    _apply_risk_state(ledger, 17.57, settings)
    assert ledger["halted"] is False

def test_veto_allows_15m_favorite():
    settings = Settings(max_crypto_positions=2, max_fraction=0.5, min_trade=1, max_crypto_stake=12)
    ledger = empty_ledger(settings)
    cand = {
        "id": "btc15",
        "kind": "crypto_lag",
        "side": "YES",
        "stake": 3,
        "price": 0.78,
        "limit_price": 0.78,
        "asset": "btc",
        "edge": 0.12,
        "fair": 0.92,
        "edge_type": "crypto_lag",
        "slug": "btc-updown-15m-1787770000",
    }
    assert veto(cand, ledger, settings) is None


def test_grok_worker_does_not_block_trading_thread(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_pool(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=1)
        return []

    monkeypatch.setattr(agent_module, "collect_grok_pool", slow_pool)
    monkeypatch.setattr(agent_module, "cached_intel", lambda: {})
    with agent_module._GROK_WORKER_LOCK:
        agent_module._GROK_WORKER = None
        agent_module._GROK_RESULT = None
    started = time.monotonic()
    assert agent_module._start_grok_worker(Settings(), {}) is True
    assert time.monotonic() - started < 0.1
    assert entered.wait(timeout=0.5)
    assert agent_module._grok_worker_running() is True
    with agent_module._GROK_WORKER_LOCK:
        worker = agent_module._GROK_WORKER
    release.set()
    assert worker is not None
    worker.join(timeout=1)
    assert agent_module._take_grok_result() is not None


def test_basis_veto_env(monkeypatch):
    from scout.risk import _basis_veto

    cand = {"kind": "crypto_lag", "side": "YES", "coinbase_basis_bps": 0.5}
    monkeypatch.delenv("CRYPTO_BASIS_MIN_BPS", raising=False)
    assert _basis_veto(cand) is None
    monkeypatch.setenv("CRYPTO_BASIS_MIN_BPS", "0.15")
    assert _basis_veto(cand) is None
    assert _basis_veto({**cand, "coinbase_basis_bps": -0.5}).startswith("basis_disagree")
    assert _basis_veto({**cand, "side": "NO", "coinbase_basis_bps": -0.5}) is None
    assert _basis_veto({**cand, "side": "NO", "coinbase_basis_bps": 0.05}).startswith("basis_disagree")
    assert _basis_veto({**cand, "coinbase_basis_bps": None}) == "basis_unknown"


def test_lessons_source_inherits_another_ledgers_vetoes(monkeypatch, tmp_path):
    import json
    from scout import lessons

    monkeypatch.setattr(lessons, "LESSONS_PATH", tmp_path / "lessons.json")
    monkeypatch.setattr(lessons, "POSTMORTEMS_PATH", tmp_path / "pm.jsonl")
    monkeypatch.setattr(lessons, "LEARN_DIR", tmp_path)
    # a paper record where the 0.40-0.60 band lost 8 of 10 (significant vs the 65% prior)
    fills = []
    for i in range(10):
        won = i < 2
        fills.append({"market_id": f"m{i}", "side": "YES", "price": 0.5, "stake": 5.0, "fee": 0.1, "reason": "crypto_lag edge=0.05"})
        fills.append({"market_id": f"m{i}", "side": "CLOSE_YES", "pnl": 4.9 if won else -5.1})
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({"fills": fills}))
    cand = {"kind": "crypto_lag", "price": 0.45}
    monkeypatch.delenv("LESSONS_SOURCE", raising=False)
    assert lessons.lesson_veto(cand, {"fills": []}) is None  # own empty ledger -> prior allows the band
    monkeypatch.setenv("LESSONS_SOURCE", str(paper))
    lessons._SOURCE_CACHE.update({"path": None, "mtime": None, "ledger": None})
    assert (lessons.lesson_veto(cand, {"fills": []}) or "").startswith("lesson:ask_0.40-0.60")


def test_profit_lock_disabled_when_fraction_is_zero():
    from scout.agent import _apply_risk_state
    from scout.config import Settings

    ledger = {"cash": 40.0, "positions": [], "fills": [], "risk_model_version": 2, "risk_baseline_cash": 40.0, "realized_peak_cash": 60.0, "halted": False}
    _apply_risk_state(ledger, equity=40.0, settings=Settings(profit_lock_fraction=0.0, max_session_drawdown=1000.0))
    assert not ledger["halted"]
    ledger2 = {"cash": 40.0, "positions": [], "fills": [], "risk_model_version": 2, "risk_baseline_cash": 40.0, "realized_peak_cash": 60.0, "halted": False}
    _apply_risk_state(ledger2, equity=40.0, settings=Settings(profit_lock_fraction=0.5, max_session_drawdown=1000.0))
    assert ledger2["halted"] and ledger2["halt_reason"].startswith("profit lock")
