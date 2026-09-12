"""Low-latency market data: websocket caches replacing REST polling.

Three subscriptions on Polymarket's public RTDS/CLOB websockets (no auth):
  - raw Chainlink oracle ticks  -> spot + the integral for TWAP lock-in math
  - Binance spot relay          -> exchange spot without REST round trips
  - CLOB order books            -> live best bid/ask + near-ask depth per token

REST remains the fallback everywhere; a dead websocket degrades to the old
behavior, never to a crash. Measured motivation: REST polling gave a median
signal age of 4.4s; the edge decays in seconds.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from typing import Any

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None

# asset -> deque[(ts, price)] of raw oracle ticks (~5 min retained)
_ORACLE: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=2400))
# asset -> (ts, price) latest exchange spot
_EXCH: dict[str, tuple[float, float]] = {}
# token -> {"bid","ask","ask_size","tick_size","event_ts"}
_BOOKS: dict[str, dict[str, Any]] = {}
_WANTED: set[str] = set()
_WANTED_VERSION = 0
_MARKET_ALIVE_TS = 0.0  # last time the market subscription saw any event/was healthy


def start_streams() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(
        target=lambda: asyncio.run(_run_forever()), name="market-streams", daemon=True
    )
    _THREAD.start()


def set_book_tokens(tokens: list[str]) -> None:
    """Declare which CLOB tokens the loop currently cares about."""
    global _WANTED_VERSION
    wanted = {str(t) for t in tokens if t}
    with _LOCK:
        if wanted != _WANTED:
            _WANTED.clear()
            _WANTED.update(wanted)
            _WANTED_VERSION += 1


def book(token: str, max_age: float = 10.0) -> dict[str, Any] | None:
    """Current book for a token. A quiet book is still current while the
    subscription is healthy, so freshness keys off connection liveness."""
    now = time.time()
    with _LOCK:
        row = _BOOKS.get(str(token))
        alive = now - _MARKET_ALIVE_TS < max_age
    if not row or not alive:
        return None
    return dict(row, quoted_at=now if alive else row["event_ts"])


def exchange_spot(asset: str, max_age: float = 3.0) -> tuple[float, float] | None:
    with _LOCK:
        row = _EXCH.get(asset)
    if not row or time.time() - row[0] > max_age:
        return None
    return row[1], row[0]


def oracle_spot(asset: str, max_age: float = 3.0) -> tuple[float, float] | None:
    with _LOCK:
        ticks = _ORACLE.get(asset)
        row = ticks[-1] if ticks else None
    if not row or time.time() - row[0] > max_age:
        return None
    return row[1], row[0]


def oracle_ticks(asset: str, since: float) -> list[tuple[float, float]]:
    with _LOCK:
        return [t for t in _ORACLE.get(asset) or () if t[0] >= since]


def _touch_alive() -> None:
    global _MARKET_ALIVE_TS
    _MARKET_ALIVE_TS = time.time()


def _near_ask_size(levels: list[Any], ask: float, cap_off: float = 0.02) -> float:
    return sum(
        float(lv.size) for lv in levels or [] if float(lv.price) <= ask + cap_off
    )


async def _price_task(topic: str, store: str) -> None:
    from polymarket import AsyncPublicClient
    from polymarket.streams import CryptoPricesSpec

    while True:
        try:
            async with AsyncPublicClient() as client:
                from .crypto_lag import stream_assets

                spec = CryptoPricesSpec(topic=topic, symbols=[f"{a}/usd" for a in stream_assets()])
                async with await client.subscribe(spec) as stream:
                    async for event in stream:
                        p = event.payload
                        asset = str(p.symbol).split("/", 1)[0].lower()
                        ts = float(p.timestamp) / 1000.0
                        px = float(p.value)
                        with _LOCK:
                            if store == "oracle":
                                _ORACLE[asset].append((ts, px))
                            else:
                                _EXCH[asset] = (ts, px)
        except Exception as exc:
            print(f"stream {topic} reconnect:", type(exc).__name__, str(exc)[:100])
            await asyncio.sleep(1)


async def _books_task() -> None:
    from polymarket import AsyncPublicClient
    from polymarket.streams import MarketSpec

    while True:
        with _LOCK:
            tokens = sorted(_WANTED)
            version = _WANTED_VERSION
        if not tokens:
            await asyncio.sleep(1)
            continue
        try:
            async with AsyncPublicClient() as client:
                spec = MarketSpec(token_ids=tokens)
                async with await client.subscribe(spec) as stream:
                    _touch_alive()
                    async for event in stream:
                        _touch_alive()
                        _apply_market_event(event)
                        with _LOCK:
                            changed = _WANTED_VERSION != version
                        if changed:
                            break  # resubscribe with the new token set
        except Exception as exc:
            print("stream books reconnect:", type(exc).__name__, str(exc)[:100])
            await asyncio.sleep(1)


def _apply_market_event(event: Any) -> None:
    etype = getattr(event, "type", "")
    p = getattr(event, "payload", None)
    if p is None:
        return
    now = time.time()
    if etype == "book":
        asks = sorted((float(lv.price), lv) for lv in p.asks or [])
        bids = sorted((float(lv.price), lv) for lv in p.bids or [])
        # An empty ask side is real information (decided window: nobody offers the loser /
        # the favourite has been swept). Record ask=0 instead of keeping the last offer alive.
        ask = asks[0][0] if asks else 0.0
        bid = bids[-1][0] if bids else 0.0
        with _LOCK:
            _BOOKS[str(p.token_id)] = {
                "bid": bid,
                "ask": ask,
                "ask_size": _near_ask_size(p.asks, ask) if asks else 0.0,
                "tick_size": float(p.tick_size or 0.01),
                "event_ts": now,
            }
    elif etype == "price_change":
        with _LOCK:
            for ch in p.price_changes or []:
                row = _BOOKS.get(str(ch.token_id))
                if not row:
                    continue
                if ch.best_ask is not None and float(ch.best_ask) > 0:
                    row["ask"] = float(ch.best_ask)
                if ch.best_bid is not None and float(ch.best_bid) > 0:
                    row["bid"] = float(ch.best_bid)
                row["event_ts"] = now
    elif etype == "best_bid_ask":
        with _LOCK:
            row = _BOOKS.setdefault(
                str(p.token_id),
                {"bid": 0.0, "ask": 0.0, "ask_size": 0.0, "tick_size": 0.01, "event_ts": now},
            )
            if p.best_ask is not None and float(p.best_ask) > 0:
                row["ask"] = float(p.best_ask)
            if p.best_bid is not None and float(p.best_bid) > 0:
                row["bid"] = float(p.best_bid)
            row["event_ts"] = now


async def _run_forever() -> None:
    await asyncio.gather(
        _price_task("prices.crypto.chainlink", "oracle"),
        _price_task("prices.crypto.binance", "exch"),
        _books_task(),
    )
