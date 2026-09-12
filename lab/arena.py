"""Forward paper arena: every lab strategy trades the live streams at once.

Same State/Strategy objects as lab/replay.py, real CLOB books (websocket + REST
fallback), real Gamma resolutions, one simulated ledger per strategy. Nothing here
touches the production bot or data/ledger.json.

Files (data/lab/):
  ledgers.json        per-strategy bankroll, open positions, fills, resting orders
  leaderboard.json    per-strategy stats, refreshed every 10 s (dashboard reads it)
  ticks/YYYY-MM-DD.jsonl   one row per second per active window (gzipped at rollover)
  arena.log           stdout (via launchd)

Env: LAB_ASSETS=btc,eth,sol  LAB_WINDOWS=5,15  LAB_TICKS=1
Run: .venv/bin/python -X utf8 -u -m lab.arena
"""
from __future__ import annotations

import gzip
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.replay import stake_for  # noqa: E402
from lab.strategies import FEE_RATE, Order, State, Strategy, catalog  # noqa: E402
from scout.crypto_lag import _binance_open, realized_window_sigma  # noqa: E402
from scout.math_risk import taker_fee_per_share  # noqa: E402

LAB = ROOT / "data" / "lab"
LAB.mkdir(parents=True, exist_ok=True)
TICKS = LAB / "ticks"
TICKS.mkdir(exist_ok=True)
LEDGERS_PATH = LAB / "ledgers.json"
LEADERBOARD_PATH = LAB / "leaderboard.json"
UA = "scout-lab-arena/0.1"
START_BANKROLL = 50.0
FIXED_STAKE = 5.0
SETTLE_DELAY = 90.0
MAKER_CANCEL_BEFORE_END = 20.0


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_outcome(market_id: str) -> float | None:
    """1.0 if Up won, 0.0 if Down won, None if not resolved yet."""
    row = get_json(f"https://gamma-api.polymarket.com/markets/{market_id}")
    row = row[0] if isinstance(row, list) and row else row
    if not isinstance(row, dict) or not row.get("closed"):
        return None
    prices = row.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            return None
    if not prices or len(prices) < 2:
        return None
    yes = float(prices[0])
    return yes if yes in (0.0, 1.0) else None


def atomic_write(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


# ---------------------------------------------------------------- coinbase feed
_CB_LOCK = __import__("threading").Lock()
_CB: dict[str, dict[str, float]] = {}
_CB_THREAD = None
_CB_PRODUCTS = {"BTC-USD": "btc", "ETH-USD": "eth", "SOL-USD": "sol"}


def start_coinbase_feed() -> None:
    """Coinbase ticker websocket (public): last trade, best bid/ask and their sizes, ~10/s."""
    global _CB_THREAD
    import threading

    if _CB_THREAD and _CB_THREAD.is_alive():
        return

    async def run() -> None:
        import asyncio

        import websockets

        while True:
            try:
                async with websockets.connect("wss://ws-feed.exchange.coinbase.com", ping_interval=20, max_size=2**20) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": list(_CB_PRODUCTS), "channels": ["ticker"]}))
                    while True:
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                        if msg.get("type") != "ticker":
                            continue
                        asset = _CB_PRODUCTS.get(msg.get("product_id"))
                        if not asset:
                            continue
                        try:
                            ts = datetime.fromisoformat(str(msg["time"]).replace("Z", "+00:00")).timestamp()
                        except Exception:
                            ts = time.time()
                        with _CB_LOCK:
                            _CB[asset] = {"price": float(msg["price"]), "bid": float(msg.get("best_bid") or 0), "ask": float(msg.get("best_ask") or 0),
                                          "bid_size": float(msg.get("best_bid_size") or 0), "ask_size": float(msg.get("best_ask_size") or 0), "ts": ts, "recv": time.time()}
            except Exception as exc:
                print(f"{now_iso()} coinbase feed reconnect: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                await __import__("asyncio").sleep(2)

    _CB_THREAD = threading.Thread(target=lambda: __import__("asyncio").run(run()), name="coinbase-feed", daemon=True)
    _CB_THREAD.start()


def coinbase(asset: str, max_age: float = 3.0) -> dict[str, float] | None:
    with _CB_LOCK:
        row = _CB.get(asset)
    if not row or time.time() - row["recv"] > max_age:
        return None
    return row


# ---------------------------------------------------------------- ledgers
class Ledger:
    def __init__(self, name: str, raw: dict[str, Any] | None = None):
        raw = raw or {}
        self.name = name
        self.bankroll = float(raw.get("bankroll", START_BANKROLL))
        self.positions: dict[str, dict[str, Any]] = raw.get("positions", {})  # market_id -> position
        self.orders: dict[str, dict[str, Any]] = raw.get("orders", {})  # market_id -> resting maker order
        self.fills: list[dict[str, Any]] = raw.get("fills", [])  # closed trades
        self.unfilled = int(raw.get("unfilled", 0))
        self.skipped_size = int(raw.get("skipped_size", 0))
        self.last_loss: dict[str, float] = raw.get("last_loss", {})

    def to_json(self) -> dict[str, Any]:
        return {"bankroll": round(self.bankroll, 4), "positions": self.positions, "orders": self.orders,
                "fills": self.fills[-2000:], "unfilled": self.unfilled, "skipped_size": self.skipped_size, "last_loss": self.last_loss}

    def stats(self, now: float) -> dict[str, Any]:
        tr = self.fills
        n = len(tr)
        rets = [t["ret"] for t in tr]

        def tstat(xs):
            if len(xs) < 3:
                return None
            m, sd = statistics.fmean(xs), statistics.stdev(xs)
            return round(m / (sd / math.sqrt(len(xs))), 2) if sd > 0 else None

        cum = peak = dd = 0.0
        for t in tr:
            cum += t["pnl5"]
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        last24 = [t for t in tr if t["closed_ts"] >= now - 86400]
        wins = sum(1 for t in tr if t["won"])
        return {
            "name": self.name, "n": n, "wins": wins, "win_rate": round(wins / n, 4) if n else None,
            "ret_mean": round(statistics.fmean(rets), 4) if rets else None, "t": tstat(rets),
            "pnl5": round(sum(t["pnl5"] for t in tr), 2), "max_dd5": round(dd, 2),
            "bankroll": round(self.bankroll, 2), "pnl_rule": round(self.bankroll - START_BANKROLL, 2),
            "brier": round(statistics.fmean((t["fair"] - (1.0 if t["won"] else 0.0)) ** 2 for t in tr), 4) if tr else None,
            "avg_price": round(statistics.fmean(t["price"] for t in tr), 3) if tr else None,
            "open": len(self.positions), "resting": len(self.orders), "unfilled": self.unfilled, "skipped_size": self.skipped_size,
            "n_24h": len(last24), "pnl5_24h": round(sum(t["pnl5"] for t in last24), 2),
            "last_fill_ts": tr[-1]["closed_ts"] if tr else None,
        }


# ---------------------------------------------------------------- arena
class Arena:
    def __init__(self) -> None:
        self.assets = [a for a in (os.getenv("LAB_ASSETS") or "btc,eth,sol").split(",") if a]
        self.wins = [int(w) for w in (os.getenv("LAB_WINDOWS") or "5,15").split(",") if w]
        self.record_ticks = (os.getenv("LAB_TICKS") or "1") not in {"0", "false", "off"}
        self.strategies: list[Strategy] = catalog()
        raw = {}
        if LEDGERS_PATH.exists():
            try:
                raw = json.loads(LEDGERS_PATH.read_text())
            except Exception:
                raw = {}
        self.ledgers = {s.name: Ledger(s.name, raw.get(s.name)) for s in self.strategies}
        self.markets: dict[str, dict[str, Any]] = {}  # market_id -> market (with win)
        self.markets_ts = 0.0
        self.minute_closes: dict[str, dict[int, float]] = defaultdict(dict)  # asset -> minute -> last raw tick
        self.sigma: dict[tuple[str, int], tuple[float, float]] = {}  # (asset, mins) -> (sigma, ts)
        self.yes_hist: dict[str, deque[tuple[float, float]]] = defaultdict(lambda: deque(maxlen=1200))
        self.open_ref: dict[str, tuple[float, str]] = {}  # market_id -> (open, source)
        self.rest_books: dict[str, dict[str, Any]] = {}
        self.rest_books_ts = 0.0
        self._rest_client: Any = None
        self.pending: dict[str, dict[str, Any]] = {}  # market_id -> {end, due, attempts, asset, mins, epoch}
        self.tick_fh = None
        self.tick_day = ""
        self.last_save = 0.0
        self.last_board = 0.0
        self.cycle = 0
        self.started_at = time.time()
        self._rebuild_pending()

    def _rebuild_pending(self) -> None:
        """Positions restored from disk must still settle: rebuild their schedule."""
        for L in self.ledgers.values():
            for key, pos in L.positions.items():
                mid = pos.get("market_id") or key.split(":")[0]
                if mid in self.pending:
                    continue
                end = int(pos["epoch"]) + int(pos["mins"]) * 60
                self.pending[mid] = {"end": end, "due": max(time.time(), end + SETTLE_DELAY), "attempts": 0,
                                     "asset": pos["asset"], "mins": pos["mins"], "epoch": pos["epoch"]}

    # ---- market universe
    def refresh_markets(self, settings) -> None:
        from scout import crypto_lag, streams
        from scout.gamma import normalize_market

        now = int(time.time())
        found: dict[str, dict[str, Any]] = {}
        for ev in crypto_lag.iter_crypto_events(settings, now):
            for raw in ev.get("markets") or []:
                raw = dict(raw)
                raw.setdefault("events", [{"id": ev.get("id"), "slug": ev.get("slug")}])
                m = normalize_market(raw)
                if not m:
                    continue
                m["event_slug"] = ev.get("slug") or ""
                m["slug"] = m.get("slug") or ev.get("slug") or ""
                win = crypto_lag.parse_window(m)
                if not win or win["asset"] not in self.assets or win["window_s"] // 60 not in self.wins:
                    continue
                m["win"] = win
                found[m["id"]] = m
        if found:
            keep_ids = set(found) | set(self.pending) | {mid for L in self.ledgers.values() for mid in L.positions}
            self.markets = {mid: m for mid, m in {**self.markets, **found}.items() if mid in keep_ids or m["win"]["end"] > now - 600}
            streams.set_book_tokens([t for m in found.values() for t in (m["yes_token"], m["no_token"]) if t])
        self.markets_ts = time.time()

    # ---- data helpers
    def _book(self, token: str) -> dict[str, Any] | None:
        from scout import streams

        row = streams.book(token)
        return row or self.rest_books.get(token)

    def _refresh_rest_books(self, tokens: list[str]) -> None:
        if not tokens:
            return
        try:
            if self._rest_client is None:
                from polymarket import PublicClient

                self._rest_client = PublicClient()
            books = self._rest_client.get_order_books(token_ids=tokens[:40])
        except Exception:
            self._rest_client = None
            return
        now = time.time()
        for bk in books:
            asks = [(float(lv.price), float(lv.size)) for lv in (bk.asks or [])]
            bids = [(float(lv.price), float(lv.size)) for lv in (bk.bids or [])]
            if not asks:
                continue
            ask = min(px for px, _ in asks)
            bid = max((px for px, _ in bids), default=0.0)
            self.rest_books[str(bk.token_id)] = {"bid": bid, "ask": ask, "ask_size": sum(sz for px, sz in asks if px <= ask + 0.02), "tick_size": float(bk.tick_size or 0.01), "event_ts": now, "quoted_at": now, "rest": True}
        self.rest_books_ts = now

    def _twap_rows(self, asset: str) -> list[tuple[int, float]]:
        from scout import crypto_lag

        with crypto_lag._CHAINLINK_LOCK:
            return [(int(ts), float(v)) for ts, v in (crypto_lag._CHAINLINK_VALUES.get(asset) or ())]

    def _open_ref(self, m: dict[str, Any]) -> tuple[float, str] | None:
        mid = m["id"]
        if mid in self.open_ref:
            return self.open_ref[mid]
        start = m["win"]["start"]
        rows = self._twap_rows(m["win"]["asset"])
        if rows:
            best = min(rows, key=lambda r: abs(r[0] - start))
            if abs(best[0] - start) <= 8:
                self.open_ref[mid] = (best[1], "twap")
                return self.open_ref[mid]
        if time.time() - start < 20:
            return None  # give the stream a moment before falling back
        try:
            px = float(_binance_open({"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}[m["win"]["asset"]], start))
        except Exception:
            px = 0.0
        if px > 0:
            self.open_ref[mid] = (px, "binance")
            return self.open_ref[mid]
        return None

    def _sigma(self, asset: str, mins: int) -> float:
        key = (asset, mins)
        cached = self.sigma.get(key)
        if cached and time.time() - cached[1] < 60:
            return cached[0]
        closes = [px for _, px in sorted(self.minute_closes[asset].items())[-31:-1]]
        if len(closes) >= 6:
            sig = realized_window_sigma(closes, mins * 60)
        else:
            try:
                from scout.crypto_lag import _binance_recent_closes

                sig = realized_window_sigma(_binance_recent_closes({"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}[asset]), mins * 60)
            except Exception:
                sig = 0.0024 if mins == 5 else 0.0040
        self.sigma[key] = (sig, time.time())
        return sig

    def _record_minute_closes(self) -> None:
        from scout import streams

        for asset in self.assets:
            row = streams.oracle_spot(asset, max_age=5.0)
            if row:
                self.minute_closes[asset][int(row[1]) // 60 * 60] = row[0]
                if len(self.minute_closes[asset]) > 200:
                    for k in sorted(self.minute_closes[asset])[:-120]:
                        self.minute_closes[asset].pop(k, None)

    # ---- state
    def build_state(self, m: dict[str, Any], now: float, btc_z: float | None) -> State | None:
        from scout import streams

        win = m["win"]
        asset, mins = win["asset"], win["window_s"] // 60
        raw = streams.oracle_spot(asset, max_age=3.0)
        if raw is None:
            return None
        ref = self._open_ref(m)
        if ref is None:
            return None
        rows = self._twap_rows(asset)
        twap = rows[-1][1] if rows and now - rows[-1][0] <= 8 else raw[0]
        yb, nb = self._book(m["yes_token"]), self._book(m["no_token"])
        if not yb or not nb or not (0 < yb["ask"] < 1 and 0 < nb["ask"] < 1):
            return None
        state = State(
            asset=asset, mins=mins, epoch=win["start"], t=now, seconds_left=win["end"] - now,
            open_px=ref[0], spot_raw=raw[0], spot_twap=twap, sigma=self._sigma(asset, mins),
            yes_ask=float(yb["ask"]), yes_bid=float(yb.get("bid") or 0.0), no_ask=float(nb["ask"]), no_bid=float(nb.get("bid") or 0.0),
            quote_age=now - float(yb.get("event_ts") or now), yes_ask_size=float(yb.get("ask_size") or 0.0),
            yes_hist=list(self.yes_hist[m["id"]]), hour_utc=datetime.fromtimestamp(now, timezone.utc).hour,
            btc_z=btc_z, tick=float(yb.get("tick_size") or 0.01),
            oracle_hist=streams.oracle_ticks(asset, win["start"] - 10),
        )
        cb = coinbase(asset)
        if cb:
            state.cb_price, state.cb_bid, state.cb_ask = cb["price"], cb["bid"], cb["ask"]
            state.cb_bid_size, state.cb_ask_size, state.cb_ts = cb["bid_size"], cb["ask_size"], min(cb["recv"], now)
        self.yes_hist[m["id"]].append((now, state.market_p()))
        return state

    # ---- execution
    @staticmethod
    def _key(mid: str, side: str, multi: bool) -> str:
        return f"{mid}:{side}" if multi else mid

    def try_enter(self, strat: Strategy, L: Ledger, m: dict[str, Any], s: State, order: Order) -> None:
        mid = m["id"]
        key = self._key(mid, order.side, strat.multi_leg)
        if order.kind == "maker":
            if key in L.orders:
                return
            stake = stake_for(strat.sizing, L.bankroll, order.fair, order.price, 0.0) if L.bankroll >= 2 else 0.0
            L.orders[key] = {"side": order.side, "limit": order.price, "fair": order.fair, "stake": stake, "placed_ts": s.t, "market_id": mid,
                             "question": m["question"], "asset": s.asset, "mins": s.mins, "epoch": s.epoch, "note": order.note, "z": round(s.z("raw"), 3)}
            return
        size = s.yes_ask_size if order.side == "YES" else None
        nb = self._book(m["no_token"])
        if order.side == "NO" and nb:
            size = float(nb.get("ask_size") or 0.0)
        fee = taker_fee_per_share(order.price, FEE_RATE)
        stake = stake_for(strat.sizing, L.bankroll, order.fair, order.price, fee) if L.bankroll >= 2 else 0.0
        shares5 = FIXED_STAKE / order.price
        if size is not None and size < shares5:
            L.skipped_size += 1
            return
        self._open_position(L, m, s, order, stake, fee, key=key)

    def _open_position(self, L: Ledger, m: dict[str, Any], s: State, order: Order, stake: float, fee: float, key: str | None = None) -> None:
        shares = stake / order.price if stake > 0 else 0.0
        cost = stake + shares * fee
        L.bankroll -= cost
        L.positions[key or m["id"]] = {"market_id": m["id"],
            "side": order.side, "price": order.price, "fair": order.fair, "edge": order.edge, "kind": order.kind,
            "stake": round(stake, 4), "shares": round(shares, 4), "fee": round(shares * fee, 4), "cost": round(cost, 4),
            "opened_ts": s.t, "question": m["question"], "asset": s.asset, "mins": s.mins, "epoch": s.epoch,
            "seconds_left": round(s.seconds_left, 1), "z": round(s.z("raw"), 3), "market_p": round(s.market_p(), 4), "note": order.note,
        }
        if m["id"] not in self.pending:
            self.pending[m["id"]] = {"end": m["win"]["end"], "due": m["win"]["end"] + SETTLE_DELAY, "attempts": 0, "asset": s.asset, "mins": s.mins, "epoch": s.epoch}
        print(f"{now_iso()} {L.name:18s} {order.kind:5s} {order.side} @ {order.price:.2f} fair {order.fair:.3f} stake ${stake:.2f} {s.asset}{s.mins}m left {s.seconds_left:.0f}s {order.note}", flush=True)

    def check_maker_orders(self, L: Ledger, now: float) -> None:
        for key, o in list(L.orders.items()):
            mid = o.get("market_id") or key.split(":")[0]
            m = self.markets.get(mid)
            if not m:
                L.orders.pop(key, None)
                continue
            end = m["win"]["end"]
            if now >= end - MAKER_CANCEL_BEFORE_END:
                L.orders.pop(key, None)
                L.unfilled += 1
                continue
            bk = self._book(m["yes_token"] if o["side"] == "YES" else m["no_token"])
            if bk and 0 < bk["ask"] <= o["limit"] + 1e-9:
                s = State(asset=o["asset"], mins=o["mins"], epoch=o["epoch"], t=now, seconds_left=end - now, open_px=1.0, spot_raw=1.0, spot_twap=1.0, sigma=0.0024,
                          yes_ask=bk["ask"], yes_bid=bk.get("bid") or 0.0, no_ask=bk["ask"], no_bid=bk.get("bid") or 0.0)
                order = Order(o["side"], o["limit"], o["fair"], o["fair"] - o["limit"], kind="maker", note=o.get("note", ""))
                L.orders.pop(key, None)
                self._open_position(L, m, s, order, float(o.get("stake") or 0.0), 0.0, key=key)

    # ---- settlement
    def settle(self, now: float) -> None:
        for mid, p in list(self.pending.items()):
            if now < p["due"]:
                continue
            p["attempts"] += 1
            p["due"] = now + 60
            outcome = None
            try:
                outcome = fetch_outcome(mid)
            except Exception as exc:
                print(f"{now_iso()} settle {mid} error {type(exc).__name__}", flush=True)
            if outcome is None and p["attempts"] >= 12:
                # proxy: TWAP at end vs the open reference we used
                rows = self._twap_rows(p["asset"])
                ref = self.open_ref.get(mid)
                end_row = min(rows, key=lambda r: abs(r[0] - p["end"])) if rows else None
                if ref and end_row and abs(end_row[0] - p["end"]) <= 5:
                    outcome = 1.0 if end_row[1] >= ref[0] else 0.0
                    print(f"{now_iso()} settle {mid} via oracle proxy -> {outcome}", flush=True)
                else:
                    print(f"{now_iso()} settle {mid} unresolved after {p['attempts']} attempts; dropping", flush=True)
                    for L in self.ledgers.values():
                        for key in [k for k in L.positions if k == mid or k.startswith(mid + ":")]:
                            pos = L.positions.pop(key, None)
                            if pos:
                                L.bankroll += pos["cost"]  # refund, no result
                    self.pending.pop(mid, None)
                    continue
            if outcome is None:
                continue
            up = outcome >= 0.5
            for L in self.ledgers.values():
              for key in [k for k in L.positions if k == mid or k.startswith(mid + ":")]:
                pos = L.positions.pop(key, None)
                if not pos:
                    continue
                won = up if pos["side"] == "YES" else (not up)
                payout = pos["shares"] if won else 0.0
                L.bankroll += payout
                pnl = payout - pos["cost"]
                fee5 = (FIXED_STAKE / pos["price"]) * (0.0 if pos["kind"] == "maker" else taker_fee_per_share(pos["price"], FEE_RATE))
                cost5 = FIXED_STAKE + fee5
                pnl5 = ((FIXED_STAKE / pos["price"]) - cost5) if won else -cost5
                L.fills.append({**pos, "won": won, "pnl": round(pnl, 4), "pnl5": round(pnl5, 4), "ret": round(pnl5 / cost5, 4), "closed_ts": now, "outcome_up": up})
                if not won:
                    L.last_loss[str(pos.get("asset") or "")] = now
            self.pending.pop(mid, None)
            print(f"{now_iso()} settled {mid} {p['asset']}{p['mins']}m epoch {p['epoch']} -> {'UP' if up else 'DOWN'}", flush=True)

    # ---- persistence
    def save(self, now: float, force: bool = False) -> None:
        if force or now - self.last_save >= 10:
            atomic_write(LEDGERS_PATH, {name: L.to_json() for name, L in self.ledgers.items()})
            self.last_save = now
        if force or now - self.last_board >= 10:
            rows = [L.stats(now) for L in self.ledgers.values()]
            meta = {s.name: {"family": s.family, "desc": s.desc, "assets": list(s.assets), "windows": list(s.windows), "sizing": s.sizing} for s in self.strategies}
            for r in rows:
                r.update(meta.get(r["name"], {}))
            rows.sort(key=lambda r: (r["t"] if r["t"] is not None else -99, r["pnl5"]), reverse=True)
            atomic_write(LEADERBOARD_PATH, {
                "generated_at": now, "started_at": self.started_at, "cycle": self.cycle, "assets": self.assets, "windows": self.wins,
                "active_markets": len([m for m in self.markets.values() if m["win"]["start"] <= now < m["win"]["end"]]),
                "pending_settlements": len(self.pending), "strategies": rows,
                "open_positions": [{"strategy": name, **pos, "market_id": pos.get("market_id") or key.split(":")[0]} for name, L in self.ledgers.items() for key, pos in L.positions.items()],
            })
            self.last_board = now

    def record_tick(self, now: float, m: dict[str, Any], s: State) -> None:
        if not self.record_ticks:
            return
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if day != self.tick_day:
            if self.tick_fh:
                self.tick_fh.close()
                old = TICKS / f"{self.tick_day}.jsonl"
                try:
                    with old.open("rb") as src, gzip.open(str(old) + ".gz", "wb") as dst:
                        dst.writelines(src)
                    old.unlink()
                except Exception:
                    pass
            self.tick_fh = (TICKS / f"{day}.jsonl").open("a")
            self.tick_day = day
        # full precision for prices: 2 decimals made DOGE (~$0.08) and XRP (~$1.4) ticks meaningless
        self.tick_fh.write(json.dumps([round(now, 1), s.asset, s.mins, s.epoch, float(f"{s.spot_raw:.8g}"), float(f"{s.spot_twap:.8g}"), float(f"{s.open_px:.8g}"), s.yes_ask, s.yes_bid, s.no_ask, s.no_bid, round(s.yes_ask_size or 0, 1), round(s.sigma, 6),
                                        round(s.cb_price, 2) if s.cb_fresh() else None, round(s.cb_bid_size or 0, 4) if s.cb_fresh() else None, round(s.cb_ask_size or 0, 4) if s.cb_fresh() else None]) + "\n")

    # ---- main loop
    def run(self) -> None:
        from scout import crypto_lag, streams
        from scout.config import settings_from_env

        settings = settings_from_env()
        crypto_lag.start_chainlink_stream()
        streams.start_streams()
        start_coinbase_feed()
        self.started_at = time.time()
        print(f"{now_iso()} arena: {len(self.strategies)} strategies · assets {self.assets} · windows {self.wins} · ledgers {'restored' if LEDGERS_PATH.exists() else 'fresh'}", flush=True)
        while True:
            try:
                self.step(settings)
            except Exception as exc:
                print(f"{now_iso()} step error {type(exc).__name__}: {str(exc)[:200]}", flush=True)
                time.sleep(2)
            time.sleep(1.0)

    def step(self, settings) -> None:
        from scout import streams

        now = time.time()
        self.cycle += 1
        if now - self.markets_ts > 15:
            try:
                self.refresh_markets(settings)
            except Exception as exc:
                print(f"{now_iso()} markets error {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        self._record_minute_closes()
        active = [m for m in self.markets.values() if m["win"]["start"] <= now < m["win"]["end"]]
        missing = [t for m in active for t in (m["yes_token"], m["no_token"]) if not streams.book(t)]
        if missing and now - self.rest_books_ts > 3:
            self._refresh_rest_books(missing)
        states: dict[str, State] = {}
        btc_z: dict[int, float] = {}
        for m in sorted(active, key=lambda x: 0 if x["win"]["asset"] == "btc" else 1):
            s = self.build_state(m, now, btc_z.get(m["win"]["window_s"]) if m["win"]["asset"] != "btc" else None)
            if s is None:
                continue
            if s.asset == "btc":
                btc_z[s.window_s] = s.z("raw")
            states[m["id"]] = s
            self.record_tick(now, m, s)
        for strat in self.strategies:
            L = self.ledgers[strat.name]
            self.check_maker_orders(L, now)
            for mid, s in states.items():
                if not strat.applies(s):
                    continue
                if not strat.multi_leg and (mid in L.positions or mid in L.orders):
                    continue
                if strat.cooldown_s and now < L.last_loss.get(s.asset, 0.0) + strat.cooldown_s:
                    continue
                if strat.sample_every_s and int(now - s.epoch) % int(strat.sample_every_s) != 0:
                    continue
                try:
                    orders = strat.quotes(s)
                except Exception as exc:
                    print(f"{now_iso()} {strat.name} decide error {type(exc).__name__}: {str(exc)[:100]}", flush=True)
                    continue
                for order in orders:
                    key = self._key(mid, order.side, strat.multi_leg)
                    if key in L.positions or key in L.orders:
                        continue
                    self.try_enter(strat, L, self.markets[mid], s, order)
        self.settle(now)
        self.save(now)


if __name__ == "__main__":
    Arena().run()
