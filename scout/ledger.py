from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import LEDGER_PATH, LIVE_LEDGER_PATH, Settings


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_ledger(settings: Settings) -> dict[str, Any]:
    cash = settings.starting_bankroll
    return {
        "mode": "live" if settings.live else "paper",
        "starting_bankroll": cash,
        "cash": cash,
        "halted": False,
        "halt_reason": "",
        "positions": [],
        "orders": [],
        "fills": [],
        "updated_at": utc_now(),
    }


def ledger_path(settings: Settings) -> Path:
    return LIVE_LEDGER_PATH if settings.live else LEDGER_PATH


def load_ledger(settings: Settings, path: Path | None = None) -> dict[str, Any]:
    path = path or ledger_path(settings)
    if not path.exists():
        ledger = empty_ledger(settings)
        save_ledger(ledger, path)
        return ledger
    ledger = json.loads(path.read_text())
    ledger.setdefault("orders", [])
    return ledger


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        live = ledger.get("mode") == "live"
        path = LIVE_LEDGER_PATH if live else LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at"] = utc_now()
    path.write_text(json.dumps(ledger, indent=2) + "\n")


def mark_price(pos: dict[str, Any], market: dict[str, Any] | None) -> float:
    if not market:
        return float(pos["entry_price"])
    quoted_at = float(market.get("clob_quoted_at") or 0)
    opened_at = str(pos.get("opened_at") or "")
    if quoted_at and opened_at:
        opened_ts = float(pos.get("opened_ts") or 0)
        if not opened_ts:
            try:
                opened_ts = datetime.fromisoformat(opened_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                opened_ts = 0.0
        # Never value a new fill from a quote captured before the fill existed.
        if opened_ts and quoted_at < opened_ts:
            return float(pos["entry_price"])
    side = pos["side"]
    if side == "YES":
        if market.get("clob_fresh") and float(market.get("yes_bid") or 0) > 0:
            return float(market["yes_bid"])
        return float(market.get("yes_price") or pos["entry_price"])
    if side == "NO":
        if market.get("clob_fresh") and float(market.get("no_bid") or 0) > 0:
            return float(market["no_bid"])
        return float(market.get("no_price") or pos["entry_price"])
    return 1.0


def mark_to_market(ledger: dict[str, Any], universe_by_id: dict[str, dict[str, Any]]) -> float:
    equity = float(ledger["cash"])
    for pos in ledger["positions"]:
        px = mark_price(pos, universe_by_id.get(pos["market_id"]))
        equity += float(pos["shares"]) * px
    return round(equity, 4)


def open_ids(ledger: dict[str, Any]) -> set[str]:
    return {p["market_id"] for p in ledger["positions"]}


def record_order(
    ledger: dict[str, Any],
    *,
    market: dict[str, Any],
    side: str,
    stake: float,
    price: float,
    shares: float,
    reason: str,
    mode: str,
    settings: Settings,
    raw: Any = None,
) -> dict[str, Any]:
    if ledger.get("halted"):
        raise RuntimeError("ledger is halted")
    if stake <= 0 or shares <= 0 or price <= 0:
        raise ValueError("invalid order")
    if stake > float(ledger["cash"]) + 1e-9:
        raise ValueError("insufficient cash")
    ledger["cash"] = round(float(ledger["cash"]) - stake, 4)
    order = {
        "ts": utc_now(),
        "market_id": market["id"],
        "question": market["question"],
        "side": side,
        "price": price,
        "stake": stake,
        "shares": shares,
        "reason": reason,
        "mode": mode,
        "yes_token": market.get("yes_token", ""),
        "no_token": market.get("no_token", ""),
        "slug": market.get("slug") or market.get("event_slug") or "",
        "event_slug": market.get("event_slug") or market.get("slug") or "",
        "asset": market.get("asset") or "",
        "edge_type": str(market.get("edge_type") or ""),
        "window_end": market.get("window_end"),
        "venue_id": "",
    }
    if isinstance(raw, dict):
        order["venue_id"] = str(raw.get("order_id") or "")
        order["venue"] = str(raw)[:500]
    elif raw is not None:
        order["venue"] = str(raw)[:500]
    ledger.setdefault("orders", []).append(order)
    return order


def fill_order(
    ledger: dict[str, Any],
    order: dict[str, Any],
    *,
    fill_price: float,
    mode: str,
    settings: Settings,
    raw: Any = None,
) -> dict[str, Any]:
    shares = float(order["shares"])
    reserved = float(order["stake"])
    actual = round(shares * fill_price, 4)
    if actual < reserved:
        ledger["cash"] = round(float(ledger["cash"]) + (reserved - actual), 4)
        stake = actual
    else:
        stake = reserved
    market = {
        "id": order["market_id"],
        "question": order["question"],
        "yes_token": order.get("yes_token", ""),
        "no_token": order.get("no_token", ""),
        "slug": order.get("slug", ""),
        "event_slug": order.get("event_slug", ""),
        "asset": order.get("asset", ""),
        "edge_type": order.get("edge_type", ""),
    }
    ledger["orders"] = [o for o in ledger.get("orders") or [] if o is not order]
    ledger["cash"] = round(float(ledger["cash"]) + stake, 4)
    return record_fill(
        ledger,
        market=market,
        side=order["side"],
        stake=stake,
        price=fill_price,
        shares=shares,
        reason=f"fill {order.get('reason', '')}",
        mode=mode,
        settings=settings,
        raw=raw,
        force=(mode == "live"),  # the venue already matched it; never refuse to book it
    )


def cancel_order(ledger: dict[str, Any], order: dict[str, Any], *, reason: str) -> dict[str, Any]:
    ledger["cash"] = round(float(ledger["cash"]) + float(order["stake"]), 4)
    ledger["orders"] = [o for o in ledger.get("orders") or [] if o is not order]
    return {"cancelled": order["market_id"], "reason": reason, "refund": order["stake"]}


def paper_maker_fillable(order: dict[str, Any], market: dict[str, Any]) -> bool:
    limit = float(order["price"])
    if order["side"] == "YES":
        return float(market.get("yes_ask") or 99) <= limit + 1e-9
    if order["side"] == "NO":
        return float(market.get("no_ask") or 99) <= limit + 1e-9
    return False


def record_fill(
    ledger: dict[str, Any],
    *,
    market: dict[str, Any],
    side: str,
    stake: float,
    price: float,
    shares: float,
    reason: str,
    mode: str,
    settings: Settings,
    raw: Any = None,
    fee: float = 0.0,
    force: bool = False,
) -> dict[str, Any]:
    """Book a fill. force=True is for venue-confirmed fills (a resting order that matched):
    those must be recorded even if the slot or cash checks would refuse a new entry."""
    if ledger.get("halted") and not force:
        raise RuntimeError("ledger is halted")
    if stake <= 0 or shares <= 0 or price <= 0:
        raise ValueError("invalid fill")
    fee = round(max(0.0, float(fee)), 4)
    cost_basis = round(stake + fee, 4)
    if not force:
        if cost_basis > float(ledger["cash"]) + 1e-9:
            raise ValueError("insufficient cash")
        if len(ledger["positions"]) >= settings.max_positions:
            raise RuntimeError("max positions reached")

    ledger["cash"] = round(float(ledger["cash"]) - cost_basis, 4)
    opened_ts = time.time()
    fill = {
        "ts": utc_now(),
        "epoch_ts": opened_ts,
        "market_id": market["id"],
        "question": market["question"],
        "side": side,
        "price": price,
        "stake": stake,
        "fee": fee,
        "cost_basis": cost_basis,
        "shares": shares,
        "reason": reason,
        "mode": mode,
        "edge_type": str(market.get("edge_type") or ""),
    }
    if raw is not None:
        fill["venue"] = str(raw)[:500]
    ledger["fills"].append(fill)
    ledger["positions"].append(
        {
            "market_id": market["id"],
            "question": market["question"],
            "side": side,
            "entry_price": price,
            "shares": shares,
            "stake": stake,
            "fee": fee,
            "cost_basis": cost_basis,
            "opened_at": fill["ts"],
            "opened_ts": opened_ts,
            "reason": reason,
            "yes_token": market.get("yes_token", ""),
            "no_token": market.get("no_token", ""),
            "slug": market.get("slug") or market.get("event_slug") or "",
            "event_slug": market.get("event_slug") or market.get("slug") or "",
            "asset": market.get("asset") or "",
            "edge_type": str(market.get("edge_type") or ""),
        }
    )
    return fill


def close_position(
    ledger: dict[str, Any],
    pos: dict[str, Any],
    *,
    price: float,
    reason: str,
    mode: str,
    credit_cash: bool = True,
) -> dict[str, Any]:
    proceeds = round(float(pos["shares"]) * price, 4)
    if credit_cash:
        ledger["cash"] = round(float(ledger["cash"]) + proceeds, 4)
    cost_basis = float(pos.get("cost_basis") or pos["stake"])
    pnl = round(proceeds - cost_basis, 4)
    fill = {
        "ts": utc_now(),
        "market_id": pos["market_id"],
        "question": pos["question"],
        "side": f"CLOSE_{pos['side']}",
        "price": price,
        "stake": proceeds,
        "shares": pos["shares"],
        "entry_fee": float(pos.get("fee") or 0),
        "reason": reason,
        "mode": mode,
        "pnl": pnl,
    }
    ledger["fills"].append(fill)
    ledger["positions"] = [p for p in ledger["positions"] if p is not pos]
    return fill


def maybe_halt(ledger: dict[str, Any], equity: float, settings: Settings) -> None:
    if (
        settings.campaign_target_usd > 0
        and float(ledger.get("cash") or 0) >= settings.campaign_target_usd
        and not ledger.get("positions")
    ):
        ledger["halted"] = True
        ledger["halt_reason"] = f"campaign target reached: cash={float(ledger['cash']):.2f}"
        return
    base = float(ledger.get("risk_baseline_cash") or settings.starting_bankroll)
    floor = base * (1.0 - settings.max_drawdown)
    if equity <= floor:
        ledger["halted"] = True
        ledger["halt_reason"] = f"drawdown equity={equity} floor={floor}"
    # Session DD in dollars, not "35% of a $9 book". A $24 night with HALT_BANKROLL=18
    # stops near $18; a $9 book is allowed one $4 clip then the $8 floor.
    session_floor = base - settings.max_session_drawdown
    if base >= settings.halt_bankroll:
        cash_floor = max(settings.halt_bankroll, session_floor)
    elif base >= 20:
        cash_floor = max(8.0, session_floor)
    else:
        # Sub-$20 remainder: $8 floor would freeze the whole stack.
        cash_floor = max(2.5, session_floor)
    ledger["cash_floor"] = round(cash_floor, 4)
    if float(ledger["cash"]) < cash_floor and not ledger["positions"]:
        ledger["halted"] = True
        ledger["halt_reason"] = f"cash below halt ({cash_floor:.2f}) with no positions"
