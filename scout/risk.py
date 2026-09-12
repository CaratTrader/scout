from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .crypto_lag import crypto_side_ok, is_crypto_updown, parse_window
from .math_risk import taker_fee_per_share

_CORRELATED = frozenset({frozenset({"btc", "eth"})})


def _position_asset(pos: dict[str, Any]) -> str:
    raw = str(pos.get("asset") or "")
    if raw:
        return raw.lower()
    win = parse_window(pos)
    return str(win["asset"]) if win else ""


def _recent_loss_on_asset(ledger: dict[str, Any], asset: str, *, seconds: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    for fill in reversed(ledger.get("fills") or []):
        if not str(fill.get("side") or "").startswith("CLOSE_"):
            continue
        if float(fill.get("pnl") or 0) >= 0:
            continue
        fill_asset = _position_asset(fill)
        if fill_asset != asset:
            continue
        ts = str(fill.get("ts") or "")
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when >= cutoff
    return False


def open_crypto_assets(ledger: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for pos in ledger.get("positions") or []:
        if not is_crypto_updown(pos):
            continue
        asset = _position_asset(pos)
        if asset:
            out.add(asset)
    return out


def utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def looks_round_prob(p: float) -> bool:
    return abs(p * 20 - round(p * 20)) < 1e-9


def maker_buy_price(bid: float, ask: float, tick: float = 0.01) -> float:
    """Post-only buy: rest on the bid, never cross the ask."""
    if ask <= tick:
        return 0.0
    if bid > 0:
        px = round(bid, 2)
    else:
        px = round(ask - tick, 2)
    if px >= ask:
        px = round(ask - tick, 2)
    if px < tick or px >= ask:
        return 0.0
    return px


def occupied_ids(ledger: dict[str, Any]) -> set[str]:
    ids = {p["market_id"] for p in ledger.get("positions") or []}
    ids.update(o["market_id"] for o in ledger.get("orders") or [])
    return ids

def position_age_s(pos: dict[str, Any], now: float | None = None) -> float:
    import time

    now = now if now is not None else time.time()
    ts = str(pos.get("opened_at") or "")
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, now - when.timestamp())


def grok_dump(
    pos: dict[str, Any],
    mark: float,
    settings: Settings,
    *,
    now: float | None = None,
) -> tuple[float, str] | None:
    """KETT: dump Grok losers in about a minute. Crypto complete-sets are not this."""
    if pos.get("side") not in {"YES", "NO"}:
        return None
    if is_crypto_updown(pos):
        return None
    if "grok" not in str(pos.get("reason") or "").lower():
        return None
    entry = float(pos.get("entry_price") or 0)
    if entry <= 0 or mark <= 0:
        return None
    age = position_age_s(pos, now)
    if mark <= entry * (1.0 - settings.fast_dump_pct):
        return mark, "fast_dump"
    if age >= settings.fast_dump_seconds and mark < entry - 0.01:
        return mark, "fast_dump"
    if age >= settings.grok_max_hold_seconds and mark < entry * 1.04:
        return mark, "time_stop"
    return None


def day_realized_pnl(ledger: dict[str, Any], day: str) -> float:
    total = 0.0
    for fill in ledger.get("fills") or []:
        ts = str(fill.get("ts") or "")
        if not ts.startswith(day):
            continue
        if fill.get("side", "").startswith("CLOSE_"):
            total += float(fill.get("pnl") or 0)
    return round(total, 4)


def _basis_veto(candidate: dict[str, Any]) -> str | None:
    """CRYPTO_BASIS_MIN_BPS>0: only take a side Coinbase already leads toward. The
    leading venue moves seconds before the settlement oracle; against-the-basis entries
    lost ~0.1 per $ more than with-the-basis ones in every taker family (lab, 2026-09-08)."""
    import os

    try:
        min_bps = float(os.getenv("CRYPTO_BASIS_MIN_BPS") or 0)
    except ValueError:
        return None
    if min_bps <= 0:
        return None
    b = candidate.get("coinbase_basis_bps")
    if b is None:
        return "basis_unknown"
    side = candidate.get("side")
    if side == "YES" and float(b) < min_bps:
        return f"basis_disagree({float(b):+.2f}bp)"
    if side == "NO" and float(b) > -min_bps:
        return f"basis_disagree({float(b):+.2f}bp)"
    return None


def veto(
    candidate: dict[str, Any],
    ledger: dict[str, Any],
    settings: Settings,
    *,
    occupied: set[str] | None = None,
) -> str | None:
    """Deterministic gate. The model cannot override this."""
    if ledger.get("halted"):
        return "halted"
    import time

    if time.time() < float(ledger.get("entry_pause_until") or 0):
        return "reconciling"
    # resting (unfilled) orders occupy a slot too: a fill later must always be recordable
    if len(ledger.get("positions") or []) + len(ledger.get("orders") or []) >= settings.max_positions:
        return "max_positions"
    held = occupied if occupied is not None else occupied_ids(ledger)
    if candidate["id"] in held:
        return "already_in"
    if candidate.get("kind") == "grok" and any(
        is_crypto_updown(p) for p in ledger.get("positions") or []
    ):
        return "crypto_busy"
    if candidate.get("kind") == "crypto_lag":
        price = float(candidate.get("price") or 0)
        fair = float(candidate.get("fair") or 0)
        if candidate.get("edge_type") == "twap_lock":
            from .twap_lock import ASK_CAP, ASK_FLOOR, MIN_P_LOCK

            if not (ASK_FLOOR <= price <= ASK_CAP and fair >= MIN_P_LOCK):
                return "twap_lock_band"
        elif not crypto_side_ok(price, fair):
            return "no_underdog"
        crypto_open = sum(1 for p in ledger.get("positions") or [] if is_crypto_updown(p))
        crypto_open += sum(1 for o in ledger.get("orders") or [] if is_crypto_updown(o))
        if crypto_open >= settings.max_crypto_positions:
            return "crypto_slot"
        asset = str(candidate.get("asset") or "") or _position_asset(candidate)
        held_assets = open_crypto_assets(ledger)
        if asset and asset in held_assets:
            return "same_asset"
        for held_asset in held_assets:
            if frozenset({held_asset, asset}) in _CORRELATED:
                return "correlated"
        if asset and _recent_loss_on_asset(ledger, asset, seconds=900):
            return "loss_cooldown"
        if candidate.get("edge_type") != "twap_lock":
            basis_veto = _basis_veto(candidate)
            if basis_veto:
                return basis_veto
    if candidate.get("kind") in {"crypto_lag", "dip_arb"}:
        from .lessons import lesson_veto

        lesson = lesson_veto(candidate, ledger)
        if lesson:
            return lesson
    stake = float(candidate.get("stake") or 0)
    if stake < settings.min_trade:
        return "dust"
    if candidate.get("kind") == "crypto_lag":
        if stake > settings.max_crypto_stake + 1e-9:
            return "max_stake"
    elif stake > settings.max_stake_usd + 1e-9:
        return "max_stake"
    cash = float(ledger.get("cash") or 0)
    if stake > cash * settings.max_fraction + 1e-9:
        return "max_fraction"
    if cash + 1e-9 < stake:
        return "no_cash"

    day_start = float(ledger.get("risk_baseline_cash") or ledger.get("starting_bankroll") or settings.starting_bankroll)
    equity_proxy = float(ledger.get("last_equity") or ledger.get("cash") or 0)
    if day_start - equity_proxy >= settings.max_daily_loss:
        return "daily_loss"

    price = float(candidate.get("price") or 0)
    kind = candidate.get("kind")
    side = candidate.get("side")

    if side != "BOTH":
        if candidate.get("edge_type") == "twap_lock":
            pass  # banded above; crypto_side_ok's trust cap does not apply to oracle arithmetic
        elif kind == "crypto_lag" or candidate.get("edge_type") == "crypto_lag":
            if not crypto_side_ok(price, float(candidate.get("fair") or 0)):
                return "cheap_crypto"
        else:
            lo, hi = settings.min_entry_price, settings.max_entry_price
            if price < lo or price > hi:
                return "extreme_price"

    if kind in {"completeness", "dip_arb"}:
        if settings.live and not is_crypto_updown(candidate):
            return "live_non_atomic_pair"
        crypto_open = sum(1 for p in ledger.get("positions") or [] if is_crypto_updown(p))
        if is_crypto_updown(candidate) and crypto_open >= settings.max_crypto_positions:
            return "crypto_slot"
        yes_ask = float(candidate.get("yes_ask") or 0)
        no_ask = float(candidate.get("no_ask") or 0)
        fees = taker_fee_per_share(yes_ask, settings.taker_fee_rate) + taker_fee_per_share(
            no_ask, settings.taker_fee_rate
        )
        if 1.0 - (yes_ask + no_ask + fees) < 0.015:
            return "edge_after_fees"
        if yes_ask + no_ask >= settings.pair_sum_target:
            return "pair_sum"

    if kind == "grok":
        if candidate.get("already_priced"):
            return "already_priced"
        edge_type = candidate.get("edge_type") or "none"
        if edge_type == "none":
            return "no_structural_edge"
        days = candidate.get("days_to_end")
        if days is not None and days <= 0:
            return "horizon"
        sources = candidate.get("sources") or []
        corroborated = bool(candidate.get("kalshi_yes")) or bool(candidate.get("intel_news"))
        if len(sources) < settings.grok_min_sources and not corroborated:
            return "one_source"
    return None
