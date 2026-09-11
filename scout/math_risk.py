from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def parse_json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def completeness_edge(yes_ask: float, no_ask: float) -> float:
    """Guaranteed edge if both asks can be bought for less than $1."""
    if yes_ask <= 0 or no_ask <= 0:
        return 0.0
    return round(max(0.0, 1.0 - (yes_ask + no_ask)), 10)


def kelly_buy(fair: float, price: float) -> float:
    """Full Kelly fraction for buying a binary share at `price` with true p=`fair`."""
    if not (0.0 < price < 1.0) or not (0.0 < fair < 1.0):
        return 0.0
    b = (1.0 - price) / price
    f = (b * fair - (1.0 - fair)) / b
    return round(max(0.0, f), 10)


def stake_usd(
    bankroll: float,
    fair: float,
    price: float,
    *,
    kelly_mult: float = 0.5,
    cap: float = 0.06,
) -> float:
    if bankroll <= 0:
        return 0.0
    fraction = min(cap, kelly_buy(fair, price) * kelly_mult)
    if fraction <= 0:
        return 0.0
    return round(bankroll * fraction, 4)


def shares_for_stake(stake: float, price: float) -> float:
    if price <= 0 or stake <= 0:
        return 0.0
    return round(stake / price, 4)


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Polymarket taker fee is feeRate * p * (1-p) per share. Makers pay 0."""
    if not (0.0 < price < 1.0) or fee_rate <= 0:
        return 0.0
    return round(fee_rate * price * (1.0 - price), 6)


def days_to_end(end_date: str, *, now: Any = None) -> float | None:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    stamp = now or datetime.now(timezone.utc)
    return round((dt - stamp).total_seconds() / 86400.0, 3)
