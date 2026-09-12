from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import os
import time
from typing import Any

from .config import Settings, hex_private_key
from .crypto_lag import parse_window
from .ledger import record_fill
from .math_risk import taker_fee_per_share

_CLIENT: Any = None


class LiveDisabled(RuntimeError):
    pass


class ReconcileRequired(LiveDisabled):
    """An on-chain action was flattened, but local cash must be refreshed."""

    pass


def token_for_side(market: dict[str, Any], side: str) -> str:
    if side == "YES":
        return market.get("yes_token") or ""
    if side == "NO":
        return market.get("no_token") or ""
    return ""


def execute(
    ledger: dict[str, Any],
    candidate: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    mode = "live" if settings.live else "paper"
    reason = f"{candidate['kind']} edge={candidate['edge']}"
    if candidate["side"] == "BOTH":
        raw: Any = {"paper": True, "taker": "pair"}
        shares = float(candidate["shares"])
        stake = round(
            shares
            * (float(candidate.get("yes_ask") or 0) + float(candidate.get("no_ask") or 0)),
            4,
        )
        price = float(candidate["price"])
        side = "BOTH"
        if settings.live:
            raw = _live_buy_both(candidate, settings)
            stake, shares, price, side = _pair_fill(raw, candidate)
            if stake <= 0 or shares <= 0:
                raise LiveDisabled("empty pair fill")
        fee = _pair_fee(raw, candidate, settings)
        return record_fill(
            ledger,
            market=candidate,
            side=side,
            stake=stake,
            price=price,
            shares=shares,
            reason=reason,
            mode=mode,
            settings=settings,
            raw=raw,
            fee=fee,
        )
    raw = {"paper": True, "taker": True}
    stake = float(candidate["stake"])
    price = float(candidate["price"])
    shares = float(candidate["shares"])
    if settings.live:
        raw = _live_taker_buy(candidate, settings)
        stake, shares, price = _actual_fill(raw, candidate)
        if stake <= 0 or shares <= 0:
            raise LiveDisabled("empty fill")
    fee = round(shares * taker_fee_per_share(price, settings.taker_fee_rate), 4)
    return record_fill(
        ledger,
        market=candidate,
        side=candidate["side"],
        stake=stake,
        price=price,
        shares=shares,
        reason=reason,
        mode=mode,
        settings=settings,
        raw=raw,
        fee=fee,
    )


def _validate_crypto_execution(
    candidate: dict[str, Any],
    settings: Settings,
    *,
    now: float | None = None,
) -> None:
    """Fail closed when a short-window signal is no longer the signal we scored."""
    if candidate.get("kind") not in {"crypto_lag", "dip_arb", "completeness"}:
        return
    now = time.time() if now is None else now
    signal_ts = float(candidate.get("signal_ts") or 0)
    if signal_ts <= 0:
        raise LiveDisabled("crypto signal missing timestamp")
    age = now - signal_ts
    if age < -1.0 or age > settings.crypto_signal_ttl_seconds:
        raise LiveDisabled(
            f"stale crypto signal: age={age:.2f}s ttl={settings.crypto_signal_ttl_seconds:.2f}s"
        )
    quoted_at = float(candidate.get("clob_quoted_at") or 0)
    if quoted_at <= 0 or now - quoted_at > settings.crypto_signal_ttl_seconds:
        quote_age = now - quoted_at if quoted_at > 0 else float("inf")
        raise LiveDisabled(f"stale crypto quote: age={quote_age:.2f}s")
    win = parse_window(candidate)
    if not win:
        raise LiveDisabled("crypto market window unavailable")
    remaining = float(win["end"]) - now
    if remaining < settings.crypto_min_seconds_to_expiry:
        raise LiveDisabled(
            f"crypto window closing: remaining={remaining:.2f}s "
            f"minimum={settings.crypto_min_seconds_to_expiry:.2f}s"
        )


def _live_buy_both(candidate: dict[str, Any], settings: Settings) -> Any:
    """Equal shares, dipped/cheap leg first. 50/50 USD split is how you unhedge."""
    client = trading_client(settings)
    _validate_crypto_execution(candidate, settings)
    _refresh_pair_quote(client, candidate, settings)
    shares = float(candidate.get("shares") or 0)
    yes_ask = float(candidate.get("yes_ask") or 0)
    no_ask = float(candidate.get("no_ask") or 0)
    if shares <= 0 or yes_ask <= 0 or no_ask <= 0:
        raise LiveDisabled("pair missing size")
    slip = max(0.01, float(settings.crypto_max_slip))
    yes_usd = round(shares * yes_ask, 4)
    no_usd = round(shares * no_ask, 4)
    yes_max = min(0.99, yes_ask + slip)
    no_max = min(0.99, no_ask + slip)
    order = [
        ("YES", candidate["yes_token"], yes_usd, yes_max),
        ("NO", candidate["no_token"], no_usd, no_max),
    ]
    if candidate.get("first_side") == "NO":
        order.reverse()
    out: dict[str, Any] = {"yes": {}, "no": {}}
    filled_first = False
    first_token = ""
    first_shares = 0.0
    first_key = ""
    for side, token, usd, max_price in order:
        if side != order[0][0] and not filled_first:
            break
        try:
            payload = _order_payload(
                _taker_buy(client, token, usd, max_price=max_price, order_type="FOK")
            )
        except LiveDisabled as exc:
            payload = {"error": str(exc), "making_amount": "0", "taking_amount": "0"}
        key = "yes" if side == "YES" else "no"
        out[key] = payload
        making = float(payload.get("making_amount") or 0)
        if side == order[0][0]:
            filled_first = making > 0
            if filled_first:
                first_token = str(token)
                first_shares = float(payload.get("taking_amount") or 0)
                first_key = key
        elif making <= 0 and first_shares > 0:
            # The venue has no atomic two-leg primitive. Flatten the first FOK leg
            # immediately if the second FOK leg failed; retain it in the ledger only
            # when the emergency unwind itself fails.
            try:
                out["unwind"] = _order_payload(_fok_sell(client, first_token, first_shares))
                raise ReconcileRequired(
                    f"pair second leg failed; unwound {first_shares:.4f} {first_key.upper()} shares"
                )
            except LiveDisabled as unwind_exc:
                if isinstance(unwind_exc, ReconcileRequired):
                    raise
                out["unwind_error"] = str(unwind_exc)
    return out


def _refresh_pair_quote(client: Any, candidate: dict[str, Any], settings: Settings) -> None:
    tokens = [str(candidate.get("yes_token") or ""), str(candidate.get("no_token") or "")]
    if not all(tokens):
        raise LiveDisabled("pair missing token ids")
    try:
        books = client.get_order_books(token_ids=tokens)
    except Exception as exc:
        raise LiveDisabled(f"pair books unavailable: {exc}") from exc
    by_token = {str(book.token_id): book for book in books}
    asks: list[float] = []
    for token in tokens:
        book = by_token.get(token)
        if not book or not getattr(book, "asks", None):
            raise LiveDisabled("pair book missing asks")
        asks.append(min(float(level.price) for level in book.asks))
    yes, no = asks
    fees = taker_fee_per_share(yes, settings.taker_fee_rate) + taker_fee_per_share(
        no, settings.taker_fee_rate
    )
    edge = 1.0 - yes - no - fees
    if yes + no >= settings.pair_sum_target or edge < 0.015:
        raise LiveDisabled(
            f"pair value gone: yes={yes:.3f} no={no:.3f} net_edge={edge:.3f}"
        )
    candidate["yes_ask"] = yes
    candidate["no_ask"] = no
    budget = float(candidate.get("stake") or 0)
    net_cost = yes + no + fees
    if budget <= 0 or net_cost <= 0:
        raise LiveDisabled("pair missing budget")
    candidate["shares"] = round(min(float(candidate.get("shares") or 0), budget / net_cost), 4)
    if float(candidate["shares"]) < settings.min_book_shares:
        raise LiveDisabled("pair size fell below venue minimum")


def _live_taker_buy(candidate: dict[str, Any], settings: Settings) -> dict[str, Any]:
    client = trading_client(settings)
    token = token_for_side(candidate, candidate["side"])
    if not token:
        raise LiveDisabled("missing token id")
    amount = float(candidate["stake"])
    last_error: LiveDisabled | None = None
    # A Gamma quote is discovery data, not an executable quote. Re-read the CLOB
    # immediately before signing and retry once if another taker wins the race.
    for _attempt in range(2):
        _validate_crypto_execution(candidate, settings)
        ceiling, best_ask, tick = _buy_ceiling(client, token, candidate, settings)
        try:
            payload = _order_payload(_taker_buy(client, token, amount, max_price=ceiling))
            payload.update(
                {
                    "quoted_best_ask": str(best_ask),
                    "max_price": str(ceiling),
                    "tick_size": str(tick),
                }
            )
            return payload
        except LiveDisabled as exc:
            last_error = exc
            if "no orders found to match" not in str(exc).lower():
                raise
    raise last_error or LiveDisabled("buy rejected after quote refresh")


def _buy_ceiling(
    client: Any,
    token_id: str,
    candidate: dict[str, Any],
    settings: Settings,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return a marketable, tick-valid ceiling that still preserves required edge."""
    try:
        book = client.get_order_book(token_id=token_id)
    except Exception as exc:
        raise LiveDisabled(f"order book unavailable: {exc}") from exc
    asks = getattr(book, "asks", ()) or ()
    if not asks:
        raise LiveDisabled("no resting asks")
    # SDK books are highest-first; min() also keeps this safe for test doubles.
    def level_price(level: Any) -> Any:
        return level.get("price") if isinstance(level, dict) else level.price

    best_ask = min(Decimal(str(level_price(level))) for level in asks)
    tick = Decimal(str(getattr(book, "tick_size", candidate.get("tick_size") or "0.01")))
    if tick <= 0:
        raise LiveDisabled("invalid tick size")

    snapshot = Decimal(str(candidate.get("limit_price") or candidate["price"]))
    if candidate.get("kind") == "crypto_lag":
        improvement_floor = max(
            tick,
            snapshot - Decimal(str(settings.crypto_max_price_improvement)),
        )
        if best_ask < improvement_floor:
            raise LiveDisabled(
                f"price dislocation: ask={best_ask} snapshot={snapshot} floor={improvement_floor}"
            )
    slip_cap = snapshot + Decimal(str(settings.crypto_max_slip))
    fair = Decimal(str(candidate.get("fair") or 0))
    min_edge = Decimal(
        str(settings.crypto_min_edge if candidate.get("kind") == "crypto_lag" else settings.grok_min_edge)
    )
    # Solve conservatively on the tick grid, including taker fees at the ceiling.
    raw_cap = min(Decimal("1") - tick, slip_cap, fair - min_edge)
    ceiling = (raw_cap / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    fee_rate = Decimal(str(settings.taker_fee_rate))
    while ceiling >= tick and fair - ceiling - fee_rate * ceiling * (Decimal("1") - ceiling) < min_edge:
        ceiling -= tick
    if ceiling < tick or best_ask > ceiling:
        raise LiveDisabled(
            f"book moved/value gone: ask={best_ask} ceiling={ceiling} fair={fair} tick={tick}"
        )
    return ceiling, best_ask, tick


def _actual_fill(raw: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, float, float]:
    making = float(raw.get("making_amount") or 0)
    taking = float(raw.get("taking_amount") or 0)
    if making > 0 and taking > 0:
        return round(making, 4), round(taking, 4), round(making / taking, 4)
    if "making_amount" in raw or "taking_amount" in raw:
        return 0.0, 0.0, 0.0
    return (
        float(candidate["stake"]),
        float(candidate["shares"]),
        float(candidate["price"]),
    )

def _pair_fill(raw: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, float, float, str]:
    yes_s, yes_sh, yes_px = _actual_fill(raw.get("yes") or {}, {
        "stake": float(candidate.get("shares") or 0) * float(candidate.get("yes_ask") or 0),
        "shares": candidate.get("shares") or 0,
        "price": candidate.get("yes_ask") or 0,
    })
    no_s, no_sh, no_px = _actual_fill(raw.get("no") or {}, {
        "stake": float(candidate.get("shares") or 0) * float(candidate.get("no_ask") or 0),
        "shares": candidate.get("shares") or 0,
        "price": candidate.get("no_ask") or 0,
    })
    if yes_sh > 0 and no_sh > 0:
        shares = min(yes_sh, no_sh)
        stake = round(yes_s + no_s, 4)
        price = round(stake / shares, 4) if shares else 0.0
        return stake, shares, price, "BOTH"
    if yes_sh > 0:
        return yes_s, yes_sh, yes_px, "YES"
    if no_sh > 0:
        return no_s, no_sh, no_px, "NO"
    return 0.0, 0.0, 0.0, "BOTH"


def _pair_fee(raw: dict[str, Any], candidate: dict[str, Any], settings: Settings) -> float:
    yes_s, yes_sh, yes_px = _actual_fill(raw.get("yes") or {}, {
        "stake": float(candidate.get("shares") or 0) * float(candidate.get("yes_ask") or 0),
        "shares": candidate.get("shares") or 0,
        "price": candidate.get("yes_ask") or 0,
    })
    no_s, no_sh, no_px = _actual_fill(raw.get("no") or {}, {
        "stake": float(candidate.get("shares") or 0) * float(candidate.get("no_ask") or 0),
        "shares": candidate.get("shares") or 0,
        "price": candidate.get("no_ask") or 0,
    })
    _ = yes_s, no_s
    return round(
        yes_sh * taker_fee_per_share(yes_px, settings.taker_fee_rate)
        + no_sh * taker_fee_per_share(no_px, settings.taker_fee_rate),
        4,
    )


def live_sell(pos: dict[str, Any], price: float, settings: Settings) -> Any:
    if not settings.live:
        return {"paper": True}
    client = trading_client(settings)
    token = pos.get("yes_token") if pos["side"] == "YES" else pos.get("no_token")
    if pos["side"] == "BOTH":
        return {"skipped": "close BOTH by holding to resolution"}
    if not token:
        raise LiveDisabled("missing token id on position")
    _ = price
    return _order_payload(_fok_sell(client, token, float(pos["shares"])))


def rest_at_cap_enabled() -> bool:
    """LOCK_REST_AT_CAP=1: when a lock candidate's offer is gone before our taker order lands,
    rest a post-only GTC bid at the price ceiling instead of giving up. Fee 0; the venue cancels
    it when the market closes; a fill is held to settlement like any lock trade."""
    return (os.getenv("LOCK_REST_AT_CAP") or "0").strip().lower() in {"1", "true", "on", "yes"}


REST_FALLBACK_REASONS = ("no orders found to match", "no resting asks", "book moved/value gone")


def rest_fallback_applies(candidate: dict[str, Any], exc: Exception) -> bool:
    if candidate.get("edge_type") != "twap_lock" or not rest_at_cap_enabled():
        return False
    text = str(exc).lower()
    return any(r in text for r in REST_FALLBACK_REASONS)


def _rest_price(candidate: dict[str, Any], settings: Settings, tick: Decimal) -> Decimal:
    """Highest tick-valid price that still keeps crypto_min_edge after TAKER fees (a resting
    bid pays none, so this is conservative), never above the snapshot plus the slip cap."""
    snapshot = Decimal(str(candidate.get("limit_price") or candidate["price"]))
    fair = Decimal(str(candidate.get("fair") or 0))
    min_edge = Decimal(str(settings.crypto_min_edge))
    raw_cap = min(Decimal("1") - tick, snapshot + Decimal(str(settings.crypto_max_slip)), fair - min_edge)
    ceiling = (raw_cap / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    fee_rate = Decimal(str(settings.taker_fee_rate))
    while ceiling >= tick and fair - ceiling - fee_rate * ceiling * (Decimal("1") - ceiling) < min_edge:
        ceiling -= tick
    return ceiling


def live_rest_buy(candidate: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Rest a post-only GTC bid for the lock candidate at its price ceiling."""
    client = trading_client(settings)
    token = token_for_side(candidate, candidate["side"])
    if not token:
        raise LiveDisabled("missing token id")
    _validate_crypto_execution(candidate, settings)
    tick = Decimal(str(candidate.get("tick_size") or "0.01"))
    price = _rest_price(candidate, settings, tick)
    if price < tick:
        raise LiveDisabled("no resting price with edge")
    # Unproven fill rate: rest at most LOCK_REST_MAX_USD (default $5) until live fills say more.
    try:
        rest_cap = Decimal(str(float(os.getenv("LOCK_REST_MAX_USD") or 5)))
    except ValueError:
        rest_cap = Decimal("5")
    stake = min(Decimal(str(candidate["stake"])), rest_cap)
    size = (stake / price).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    min_size = max(Decimal(str(candidate.get("min_order_size") or 0)), Decimal("5"))
    if size < min_size:
        raise LiveDisabled(f"rest size {size} below venue minimum {min_size}")
    try:
        response = client.place_limit_order(token_id=token, price=price, size=size, side="BUY", post_only=True)
    except Exception as exc:
        raise LiveDisabled(f"rest rejected: {exc}") from exc
    if not getattr(response, "ok", False):
        raise LiveDisabled(f"rest rejected: {getattr(response, 'code', '')} {getattr(response, 'message', response)}")
    payload = _order_payload(response)
    payload.update({"rest_price": str(price), "rest_size": str(size), "tick_size": str(tick)})
    return payload


def live_order_state(order_id: str, settings: Settings) -> tuple[str, float, float]:
    """(status, size_matched, price) of a resting order."""
    try:
        order = trading_client(settings).get_order(order_id=order_id)
    except Exception as exc:
        raise LiveDisabled(f"order status failed: {exc}") from exc
    return (
        str(getattr(order, "status", "") or ""),
        float(getattr(order, "size_matched", 0) or 0),
        float(getattr(order, "price", 0) or 0),
    )


def live_cancel(order_id: str, settings: Settings) -> None:
    try:
        trading_client(settings).cancel_order(order_id=order_id)
    except Exception as exc:
        raise LiveDisabled(f"cancel failed: {exc}") from exc


def live_redeem(pos: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Redeem a resolved winning position into collateral and wait for settlement."""
    if not settings.live:
        return {"paper": True}
    market_id = str(pos.get("market_id") or "")
    if not market_id:
        raise LiveDisabled("missing market id for redemption")
    try:
        handle = trading_client(settings).redeem_positions(market_id=market_id)
        outcome = handle.wait()
    except Exception as exc:
        raise LiveDisabled(f"redeem failed: {exc}") from exc
    return {
        "transaction_hash": str(getattr(outcome, "transaction_hash", "") or ""),
        "transaction_id": str(getattr(outcome, "transaction_id", "") or ""),
    }


def live_position_balance(pos: dict[str, Any], settings: Settings) -> float:
    token = pos.get("yes_token") if pos.get("side") == "YES" else pos.get("no_token")
    if not token:
        raise LiveDisabled("missing token id for balance check")
    try:
        balance = trading_client(settings).get_balance_allowance(
            asset_type="CONDITIONAL", token_id=str(token)
        ).balance
    except Exception as exc:
        raise LiveDisabled(f"position balance unavailable: {exc}") from exc
    return float(balance)


def trading_client(settings: Settings):
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _secure_client(settings)
    return _CLIENT


def reset_trading_client() -> None:
    global _CLIENT
    if _CLIENT is not None:
        try:
            _CLIENT.close()
        except Exception:
            pass
        _CLIENT = None


def _secure_client(settings: Settings):
    try:
        from polymarket import RelayerApiKey, SecureClient
    except ImportError as exc:
        raise LiveDisabled("pip install polymarket-client") from exc
    if not settings.private_key or not settings.funder:
        raise LiveDisabled("LIVE needs POLYMARKET_PRIVATE_KEY and POLYMARKET_FUNDER")
    if not settings.relayer_api_key or not settings.relayer_api_key_address:
        raise LiveDisabled("LIVE needs RELAYER_API_KEY and RELAYER_API_KEY_ADDRESS")
    return SecureClient.create(
        private_key=hex_private_key(settings.private_key),
        wallet=settings.funder,
        api_key=RelayerApiKey(
            key=settings.relayer_api_key,
            address=settings.relayer_api_key_address,
        ),
    )


def probe_live(settings: Settings) -> dict[str, Any]:
    from eth_account import Account
    from eth_utils.address import to_checksum_address

    pk = hex_private_key(settings.private_key)
    signer = Account.from_key(pk).address
    relayer = settings.relayer_api_key_address
    signer_match = bool(relayer) and to_checksum_address(signer) == to_checksum_address(relayer)
    client = trading_client(settings)
    collat = client.get_balance_allowance(asset_type="COLLATERAL")
    keys = client.fetch_api_keys()
    return {
        "signer": signer,
        "wallet": client.wallet,
        "wallet_type": client.wallet_type,
        "relayer_address": relayer,
        "signer_matches_relayer": signer_match,
        "collateral_balance_raw": collat.balance,
        "collateral_pUSD": collat.balance / 1_000_000,
        "allowance_contracts": len(collat.allowances),
        "clob_api_key_count": len(keys),
    }


def _taker_buy(
    client,
    token_id: str,
    amount: float,
    *,
    max_price: float,
    order_type: str = "FAK",
) -> Any:
    try:
        response = client.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount,
            max_price=max_price,
            order_type=order_type,
        )
    except Exception as exc:
        raise LiveDisabled(f"buy rejected: {exc}") from exc
    if not getattr(response, "ok", False):
        raise LiveDisabled(f"buy rejected: {getattr(response, 'code', '')} {getattr(response, 'message', response)}")
    return response


def _fok_sell(client, token_id: str, size: float) -> Any:
    try:
        response = client.place_market_order(
            token_id=token_id,
            side="SELL",
            shares=size,
            order_type="FOK",
        )
    except Exception as exc:
        raise LiveDisabled(f"sell rejected: {exc}") from exc
    if not response.ok:
        raise LiveDisabled(f"sell rejected: {response.code} {response.message}")
    return response


def _order_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    return {"raw": str(response)}
