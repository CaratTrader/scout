from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .config import Settings
from .math_risk import as_float, completeness_edge, days_to_end, parse_json_field


def resolved_outcome_prices(slug: str, user_agent: str) -> tuple[float, float] | None:
    """YES/NO prices after close. (0,1) means Down won; (1,0) means Up won."""
    if not slug:
        return None
    try:
        payload = _get_json_any(
            "https://gamma-api.polymarket.com/events?slug=" + urllib.parse.quote(slug),
            user_agent,
            timeout=12,
        )
    except Exception:
        return None
    events = payload if isinstance(payload, list) else []
    if not events:
        return None
    market = (events[0].get("markets") or [None])[0]
    if not market or not market.get("closed"):
        return None
    prices = parse_json_field(market.get("outcomePrices")) or []
    if len(prices) < 2:
        return None
    return as_float(prices[0]), as_float(prices[1])


def _fetch_json(url: str, user_agent: str, timeout: int, attempts: int = 3) -> Any:
    """Proxies (AV TLS scanners included) truncate chunked bodies; retry, don't die."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
    raise last or RuntimeError(f"fetch failed {url}")


def _get_json(url: str, user_agent: str, timeout: int = 30) -> dict[str, Any]:
    return _fetch_json(url, user_agent, timeout)


def iter_near_term_markets(settings: Settings, *, days: int = 21) -> Iterator[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    lo = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    offset = 0
    fetched = 0
    while fetched < 400:
        params = {
            "closed": "false",
            "active": "true",
            "limit": "100",
            "offset": str(offset),
            "end_date_min": lo,
            "end_date_max": hi,
        }
        url = "https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode(params)
        payload = _get_json_any(url, settings.user_agent)
        rows = payload if isinstance(payload, list) else (payload.get("markets") or [])
        if not rows:
            break
        for market in rows:
            yield market
            fetched += 1
        if len(rows) < 100:
            break
        offset += len(rows)


def _get_json_any(url: str, user_agent: str, timeout: int = 30) -> Any:
    return _fetch_json(url, user_agent, timeout)


def iter_markets(settings: Settings) -> Iterator[dict[str, Any]]:
    cursor = None
    fetched = 0
    page = min(100, settings.scan_limit)
    while fetched < settings.scan_limit:
        params: dict[str, Any] = {"closed": "false", "limit": str(page)}
        if cursor:
            params["next_cursor"] = cursor
        url = settings.gamma_markets + "?" + urllib.parse.urlencode(params)
        payload = _get_json_any(url, settings.user_agent)
        if not isinstance(payload, dict):
            break
        markets = payload.get("markets") or []
        if not markets:
            break
        for market in markets:
            yield market
            fetched += 1
            if fetched >= settings.scan_limit:
                break
        cursor = payload.get("next_cursor")
        if not cursor:
            break


def normalize_market(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not raw.get("enableOrderBook") or not raw.get("active") or raw.get("closed"):
        return None
    outcomes = parse_json_field(raw.get("outcomes")) or []
    prices = parse_json_field(raw.get("outcomePrices")) or []
    tokens = parse_json_field(raw.get("clobTokenIds")) or []
    if len(outcomes) < 2 or len(prices) < 2:
        return None
    try:
        yes_px = as_float(prices[0])
        no_px = as_float(prices[1])
    except (TypeError, ValueError, IndexError):
        return None
    best_bid = as_float(raw.get("bestBid"))
    best_ask = as_float(raw.get("bestAsk"), yes_px)
    spread = as_float(raw.get("spread"))
    if spread <= 0 and best_bid and best_ask:
        spread = max(0.0, best_ask - best_bid)
    yes_ask = best_ask if best_ask > 0 else yes_px
    no_ask = max(0.0, 1.0 - best_bid) if best_bid > 0 else no_px
    yes_bid = best_bid if best_bid > 0 else max(0.0, yes_ask - spread)
    no_bid = max(0.0, 1.0 - yes_ask) if yes_ask > 0 else 0.0
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    event_slug = (events[0] or {}).get("slug") if events else ""
    return {
        "id": str(raw.get("id")),
        "question": raw.get("question") or "",
        "description": raw.get("description") or "",
        "resolution_source": raw.get("resolutionSource") or "",
        "slug": raw.get("slug") or "",
        "event_id": str((events[0] or {}).get("id") or "") if events else "",
        "url": f"https://polymarket.com/event/{event_slug or raw.get('slug') or ''}",
        "yes": outcomes[0],
        "no": outcomes[1],
        "yes_price": yes_px,
        "no_price": no_px,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "tick_size": as_float(raw.get("orderPriceMinTickSize"), 0.01),
        "min_order_size": as_float(raw.get("orderMinSize"), 1.0),
        "liquidity": as_float(raw.get("liquidityNum"), as_float(raw.get("liquidity"))),
        "volume_24h": as_float(raw.get("volume24hr")),
        "end_date": raw.get("endDate") or "",
        "days_to_end": days_to_end(str(raw.get("endDate") or "")),
        "yes_token": str(tokens[0]) if len(tokens) > 0 else "",
        "no_token": str(tokens[1]) if len(tokens) > 1 else "",
        "completeness_edge": round(completeness_edge(yes_ask, no_ask), 4),
    }


def liquid_enough(market: dict[str, Any], settings: Settings, *, crypto: bool = False) -> bool:
    if not (0 < market["yes_ask"] < 1 and 0 < market["no_ask"] < 1):
        return False
    if crypto:
        return 0 < market["spread"] <= 0.20 and market["liquidity"] >= 200
    return (
        market["liquidity"] >= settings.min_liquidity
        and market["volume_24h"] >= settings.min_volume_24h
        and 0 < market["spread"] <= settings.max_spread
    )
