"""Coinbase ticker feed (public websocket). Coinbase leads the Chainlink oracle by
seconds; the sign of (Coinbase - oracle) separated winners from losers in every taker
strategy on real books (lab, 2026-09-08). Used by the CRYPTO_BASIS_MIN_BPS veto."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime

_LOCK = threading.Lock()
_LAST: dict[str, tuple[float, float]] = {}  # asset -> (price, received_ts)
_THREAD: threading.Thread | None = None
_PRODUCTS = {"BTC-USD": "btc", "ETH-USD": "eth", "SOL-USD": "sol"}


def start() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=lambda: asyncio.run(_run()), name="coinbase-feed", daemon=True)
    _THREAD.start()


async def _run() -> None:
    import websockets

    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com", ping_interval=20, max_size=2**20) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": list(_PRODUCTS), "channels": ["ticker"]}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                    if msg.get("type") != "ticker":
                        continue
                    asset = _PRODUCTS.get(msg.get("product_id"))
                    if asset and msg.get("price"):
                        with _LOCK:
                            _LAST[asset] = (float(msg["price"]), time.time())
        except Exception as exc:
            print("coinbase feed reconnect:", type(exc).__name__, str(exc)[:80])
            await asyncio.sleep(2)


def spot(asset: str, max_age: float = 3.0) -> tuple[float, float] | None:
    with _LOCK:
        row = _LAST.get(asset)
    if not row or time.time() - row[1] > max_age:
        return None
    return row


def basis_bps(asset: str, oracle_px: float) -> float | None:
    """(Coinbase - oracle) / oracle in basis points, or None when the feed is stale."""
    row = spot(asset)
    if not row or oracle_px <= 0:
        return None
    return round((row[0] - oracle_px) / oracle_px * 1e4, 3)
