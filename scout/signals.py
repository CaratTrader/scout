from __future__ import annotations

from typing import Any

from .config import Settings
from .crypto_lag import crypto_side_ok
from .grok_fair import grok_eligible
from .math_risk import kelly_buy, shares_for_stake, stake_usd
from .math_risk import taker_fee_per_share


def _crypto_side_ok(is_lag: bool, ask: float, fair: float, edge_type: str = "") -> bool:
    """5m books at 1¢ are already decided. Buy the lag in the middle, or the favorite near the end."""
    if not is_lag:
        return True
    if edge_type == "twap_lock":
        # Fair here is settlement-oracle arithmetic, not a model opinion; the
        # trust cap in crypto_side_ok would wrongly refuse a genuine lock.
        from .twap_lock import ASK_CAP, ASK_FLOOR, MIN_P_LOCK

        return ASK_FLOOR <= ask <= ASK_CAP and fair >= MIN_P_LOCK
    return crypto_side_ok(ask, fair)


def build_candidates(
    universe: list[dict[str, Any]],
    settings: Settings,
    grok_scores: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    scores = grok_scores or {}
    candidates: list[dict[str, Any]] = []
    for market in universe:
        ideas: list[dict[str, Any]] = []
        if market["completeness_edge"] >= settings.completeness_min_edge:
            ideas.append(
                {
                    "kind": "completeness",
                    "side": "BOTH",
                    "price": round(market["yes_ask"] + market["no_ask"], 4),
                    "fair": 1.0,
                    "edge": market["completeness_edge"],
                    "confidence": 1.0,
                    "thesis": "Buy both outcomes; asks sum to less than $1.",
                    "sources": [],
                }
            )
        scored = scores.get(market["id"])
        edge_type = (scored or {}).get("edge_type") or "none"
        is_lag = edge_type in {"crypto_lag", "twap_lock"}
        min_edge = settings.crypto_min_edge if is_lag else settings.grok_min_edge
        if (
            scored
            and (is_lag or grok_eligible(market))
            and scored.get("confidence", 0) >= settings.min_confidence
            and not scored.get("already_priced")
            and edge_type != "none"
        ):
            if settings.live and not is_lag:
                if edge_type not in {"stale_news", "cheap_convex", "related_inconsistency"}:
                    continue
            p = scored["p_yes"]
            yes_ask = float(market["yes_ask"])
            no_ask = float(market["no_ask"])
            yes_edge = p - yes_ask - taker_fee_per_share(yes_ask, settings.taker_fee_rate)
            no_fair = 1.0 - p
            no_edge = no_fair - no_ask - taker_fee_per_share(no_ask, settings.taker_fee_rate)
            kind = "crypto_lag" if is_lag else "grok"
            extra = {
                "already_priced": False,
                "edge_type": edge_type,
                "thesis": scored.get("thesis") or "",
                "sources": scored.get("sources") or [],
                "days_to_end": market.get("days_to_end"),
                "asset": scored.get("asset") or market.get("asset") or "",
                "taker": True,
                "signal_ts": scored.get("signal_ts"),
                "window_start": scored.get("window_start"),
                "window_end": scored.get("window_end"),
                "seconds_left_at_signal": scored.get("seconds_left_at_signal"),
                "oracle_open": scored.get("oracle_open"),
                "oracle_spot": scored.get("oracle_spot"),
                "exchange_spot": scored.get("exchange_spot"),
                "exchange_basis_bps": scored.get("exchange_basis_bps"),
                "coinbase_basis_bps": scored.get("coinbase_basis_bps"),
                "raw_model_p_yes": scored.get("raw_model_p_yes"),
                "market_p_yes": scored.get("market_p_yes"),
                "model_weight": scored.get("model_weight"),
                "kalshi_yes": market.get("kalshi_yes"),
                "intel_news": market.get("intel_news"),
            }
            if yes_edge >= min_edge and _crypto_side_ok(is_lag, yes_ask, p, edge_type):
                ideas.append(
                    {
                        "kind": kind,
                        "side": "YES",
                        "price": yes_ask,
                        "limit_price": yes_ask,
                        "fair": p,
                        "edge": round(yes_edge, 4),
                        "confidence": scored["confidence"],
                        **extra,
                    }
                )
            if no_edge >= min_edge and _crypto_side_ok(is_lag, no_ask, no_fair, edge_type):
                ideas.append(
                    {
                        "kind": kind,
                        "side": "NO",
                        "price": no_ask,
                        "limit_price": no_ask,
                        "fair": no_fair,
                        "edge": round(no_edge, 4),
                        "confidence": scored["confidence"],
                        **extra,
                    }
                )
        for idea in ideas:
            row = dict(market)
            row.update(idea)
            candidates.append(row)
    # Crypto first, then Grok. Edge is the tie-break inside each kind.
    candidates.sort(
        key=lambda c: (
            0 if c.get("kind") in {"dip_arb", "completeness"} else 1 if c.get("kind") == "crypto_lag" else 2,
            -float(c.get("edge") or 0),
            -float(c.get("confidence") or 0),
        )
    )
    return candidates


def crypto_clip(
    cash: float,
    edge: float,
    settings: Settings,
    *,
    fair: float,
    price: float,
) -> float:
    """Target-aware fractional Kelly, capped for the $9-to-$100 campaign."""
    target = max(1.0, settings.campaign_target_usd)
    if cash < target * 0.25:
        floor, cap, urgency = settings.min_crypto_stake, min(settings.max_crypto_stake, cash * 0.45), 1.15
    elif cash < target * 0.70:
        floor, cap, urgency = settings.min_crypto_stake, 8.0, 1.0
    elif cash < target * 1.20:
        floor, cap, urgency = settings.min_crypto_stake, 12.0, 0.90
    elif cash < target * 2.0:
        floor, cap, urgency = settings.min_crypto_stake, 20.0, 0.80
    else:
        floor, cap, urgency = settings.min_crypto_stake, min(settings.max_crypto_stake, 30.0), 0.70
    fee = taker_fee_per_share(price, settings.taker_fee_rate)
    effective_price = min(0.999999, price + fee)
    full_kelly = kelly_buy(fair, effective_price)
    fraction = min(
        settings.crypto_bankroll_frac,
        settings.max_fraction,
        full_kelly * settings.crypto_kelly_mult * urgency,
    )
    raw = cash * max(0.0, fraction)
    if edge >= 0.15:
        raw *= 1.08
    return round(
        min(cap, max(floor, raw), settings.max_crypto_stake, cash * settings.max_fraction, cash),
        2,
    )


def attach_sizing(candidates: list[dict[str, Any]], cash: float, settings: Settings) -> list[dict[str, Any]]:
    sized = []
    remaining = cash
    for row in candidates:
        if row["side"] == "BOTH":
            stake = round(min(settings.max_fraction * cash, remaining, cash * 0.5), 2)
        elif row.get("kind") == "crypto_lag":
            stake = min(
                crypto_clip(
                    cash,
                    float(row.get("edge") or 0),
                    settings,
                    fair=float(row.get("fair") or 0),
                    price=float(row.get("price") or 0),
                ),
                remaining,
            )
        else:
            if cash >= 20:
                grok_cap = min(settings.max_grok_stake, cash * settings.grok_bankroll_frac)
            else:
                grok_cap = min(
                    settings.max_grok_stake,
                    max(settings.min_trade, cash * 0.12),
                    cash * 0.15,
                )
            stake = stake_usd(
                cash,
                row["fair"],
                row["price"],
                kelly_mult=settings.kelly_mult,
                cap=min(settings.max_fraction, grok_cap / max(cash, 1.0)),
            )
            stake = min(stake, remaining, grok_cap, settings.max_stake_usd)
            stake = round(stake, 2)
        if stake < settings.min_trade:
            continue
        row = dict(row)
        if row["side"] != "BOTH":
            ask = float(row.get("yes_ask") or 0) if row["side"] == "YES" else float(row.get("no_ask") or 0)
            if ask <= 0:
                continue
            row["limit_price"] = ask
            row["price"] = ask
            row["taker"] = True
        row["stake"] = round(stake, 4)
        if row["side"] == "BOTH":
            pair = float(row.get("yes_ask") or 0) + float(row.get("no_ask") or 0)
            row["price"] = round(pair, 4) if pair > 0 else row.get("price") or 1.0
            row["shares"] = round(stake / pair, 4) if pair > 0 else round(stake, 4)
            row["taker"] = True
        else:
            row["shares"] = shares_for_stake(stake, row["price"])
        remaining = round(remaining - stake, 4)
        sized.append(row)
    return sized
