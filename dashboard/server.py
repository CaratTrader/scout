"""Scout dashboard v2 — analytics, live signal engine, indicators, settlement audit.

Stdlib HTTP server on 127.0.0.1:8787 (override with DASH_PORT / DASH_HOST). Read-only toward the
bot: it never writes ledger/journal. Its own files live in data/:
  windows_audit.jsonl  per-window oracle boundary values + resolved outcome
  features.jsonl       external indicator snapshots (for later backtests)
  resolutions.json     cache of resolved outcomes for every candidate market

Endpoints:
  /            dashboard page
  /api/state   full analytics (cached, recomputed when ledger/journal change)
  /api/live    1-second live view: window, signal, books, positions, indicators
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
HERE = Path(__file__).resolve().parent
PORT = int(os.getenv("DASH_PORT") or 8787)
# 127.0.0.1 = this Mac only (default). DASH_HOST=0.0.0.0 also serves phones/laptops on the same
# network at http://<this Mac's LAN IP>:8787 — there is no login, so only do that on a private LAN.
HOST = os.getenv("DASH_HOST") or "127.0.0.1"
ET = ZoneInfo("America/New_York")
UA = "scout-dashboard/2.0"
AUDIT_PATH = DATA / "windows_audit.jsonl"
FEATURES_PATH = DATA / "features.jsonl"
RESOLUTIONS_PATH = DATA / "resolutions.json"
GATE = {"fills": 200, "t_stat": 2.0, "positive_days": 7}

try:  # LIVE=1 in .env switches the dashboard to the real-money ledger (the bot still needs --live to trade)
    from scout.config import load_env_file as _load_env_file

    _load_env_file()
except Exception:
    pass


import threading as _threading

_VIEW = _threading.local()


def set_view(mode: str | None) -> None:
    _VIEW.mode = mode if mode in {"live", "paper"} else None


def default_view() -> str:
    """Live when LIVE=1 in .env, or when the live ledger was written in the last 10 minutes."""
    if (os.getenv("LIVE") or "0").strip().lower() in {"1", "true", "yes", "on"}:
        return "live"
    p = DATA / "ledger_live.json"
    try:
        if time.time() - p.stat().st_mtime < 600:
            return "live"
    except FileNotFoundError:
        pass
    return "paper"


def is_live() -> bool:
    return (getattr(_VIEW, "mode", None) or default_view()) == "live"


def ledger_file() -> Path:
    return DATA / ("ledger_live.json" if is_live() else "ledger.json")
BANDS = ((0.05, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.92))


# --------------------------------------------------------------------------- utils
def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


_LAST_GOOD: dict[str, Any] = {}


def read_json_stable(path: Path, default: Any) -> Any:
    """The bot rewrites ledger.json / lessons.json in place (not atomically); a read
    that lands mid-write must not blank the dashboard. Keep the last good parse."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return _LAST_GOOD.get(str(path), default)
    if isinstance(data, dict) and data:
        _LAST_GOOD[str(path)] = data
    return data


def tail_lines(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = min(size, 200_000)
            fh.seek(size - block)
            text = fh.read().decode("utf-8", errors="replace")
        return text.splitlines()[-n:]
    except Exception:
        return []


def parse_ts(ts: Any) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def get_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean(obj: Any) -> Any:
    """JSON-safe: non-finite floats become null."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def et_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).date().isoformat()


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def band_of(price: float) -> str:
    for lo, hi in BANDS:
        if lo <= price < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "out"


def taker_fee(price: float, rate: float = 0.07) -> float:
    if not (0 < price < 1) or rate <= 0:
        return 0.0
    return rate * price * (1.0 - price)


def ret_per_dollar(price: float, won: bool, fee_rate: float = 0.07) -> float:
    """Return per $1 staked for a taker buy at `price` (fee-inclusive cost)."""
    if not (0 < price < 1):
        return 0.0
    shares = 1.0 / price
    cost = 1.0 + shares * taker_fee(price, fee_rate)
    payoff = shares if won else 0.0
    return (payoff - cost) / cost


def mean_sd(xs: list[float]) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    if len(xs) < 2:
        return xs[0], 0.0
    return statistics.fmean(xs), statistics.stdev(xs)


def t_stat(xs: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    m, sd = mean_sd(xs)
    if sd <= 0:
        return None
    return m / (sd / math.sqrt(len(xs)))


def bootstrap_ci(xs: list[float], n: int = 1000, seed: int = 7) -> tuple[float, float] | None:
    if len(xs) < 5:
        return None
    rng = random.Random(seed)
    means = []
    k = len(xs)
    for _ in range(n):
        means.append(sum(rng.choice(xs) for _ in range(k)) / k)
    means.sort()
    return means[int(0.05 * n)], means[int(0.95 * n) - 1]


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


# --------------------------------------------------------------------------- journal
class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.mtime = -1.0
        self.size = -1
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def load(self) -> list[dict[str, Any]]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return []
        with self.lock:
            if st.st_mtime == self.mtime and st.st_size == self.size:
                return self.events
            events: list[dict[str, Any]] = []
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
            self.events = events
            self.mtime, self.size = st.st_mtime, st.st_size
            return events


JOURNAL = Journal(DATA / "journal.jsonl")

_THESIS_RE = {
    "chg_pct": re.compile(r"chg=([-+]?\d+(?:\.\d+)?)%"),
    "left_s": re.compile(r"left=(\d+)s"),
    "sigma_pct": re.compile(r"sigma=(\d+(?:\.\d+)?)%"),
    "source": re.compile(r"\b(chainlink_raw|chainlink|binance)\b"),
}


def parse_thesis(thesis: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not thesis:
        return out
    for key, rx in _THESIS_RE.items():
        m = rx.search(thesis)
        if not m:
            continue
        out[key] = m.group(1) if key == "source" else float(m.group(1))
    if "chg_pct" in out and "sigma_pct" in out and "left_s" in out and out["sigma_pct"] > 0:
        t = max(out["left_s"] / 300.0, 0.02)
        out["z"] = out["chg_pct"] / (out["sigma_pct"] * math.sqrt(t))
    return out


# --------------------------------------------------------------------------- trades
def pair_trades(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trades: list[dict[str, Any]] = []
    for f in ledger.get("fills") or []:
        side = str(f.get("side") or "")
        mid = str(f.get("market_id") or "")
        if side.startswith("CLOSE_"):
            entry = queues[mid].pop(0) if queues[mid] else {}
            stake = fnum(entry.get("stake"))
            cost = fnum(entry.get("cost_basis"), stake)
            pnl = fnum(f.get("pnl"))
            price = fnum(entry.get("price"))
            trades.append(
                {
                    "market_id": mid,
                    "question": f.get("question") or entry.get("question") or "",
                    "side": side.replace("CLOSE_", ""),
                    "entry_price": price,
                    "stake": stake,
                    "fee": fnum(entry.get("fee"), fnum(f.get("entry_fee"))),
                    "cost": cost,
                    "shares": fnum(f.get("shares")),
                    "close_price": fnum(f.get("price")),
                    "pnl": pnl,
                    "ret": (pnl / cost) if cost else 0.0,
                    "won": pnl > 0,
                    "entry_ts": fnum(entry.get("epoch_ts")) or parse_ts(entry.get("ts")),
                    "close_ts": parse_ts(f.get("ts")),
                    "close_reason": f.get("reason") or "",
                    "reason": entry.get("reason") or "",
                    "strategy": str(entry.get("reason") or "unknown").split(" ", 1)[0],
                    "band": band_of(price),
                }
            )
        elif side in {"YES", "NO", "BOTH"}:
            queues[mid].append(f)
    return trades


def enrich_trades(trades: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    submits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.get("event") == "submit" and e.get("id"):
            submits[str(e["id"])].append(e)
    for t in trades:
        rows = submits.get(t["market_id"]) or []
        best = None
        for e in rows:
            if fnum(e.get("ts")) <= t["entry_ts"] + 5:
                best = e
        if not best and rows:
            best = rows[-1]
        if not best:
            continue
        th = parse_thesis(str(best.get("thesis") or ""))
        t.update(
            {
                "fair": best.get("fair"),
                "edge": best.get("edge"),
                "raw_model_p": best.get("raw_model_p_yes"),
                "market_p": best.get("market_p_yes"),
                "signal_age_ms": best.get("signal_age_ms"),
                "basis_bps": best.get("exchange_basis_bps"),
                "oracle_open": best.get("oracle_open"),
                "oracle_spot": best.get("oracle_spot"),
                "window_end": best.get("window_end"),
                "left_s": th.get("left_s"),
                "chg_pct": th.get("chg_pct"),
                "sigma_pct": th.get("sigma_pct"),
                "z": th.get("z"),
                "source": th.get("source"),
                "edge_type": best.get("kind"),
            }
        )
        # p of OUR side (fair is already side-relative in the journal)
        mp = best.get("market_p_yes")
        if mp is not None:
            t["market_p_side"] = float(mp) if t["side"] == "YES" else 1.0 - float(mp)


# --------------------------------------------------------------------------- analytics
def breakdown(trades: list[dict[str, Any]], key_fn, order: list[Any] | None = None) -> list[dict[str, Any]]:
    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        groups[k].append(t)
    keys = order if order is not None else sorted(groups)
    out = []
    for k in keys:
        rows = groups.get(k) or []
        if not rows and order is None:
            continue
        n = len(rows)
        pnl = sum(r["pnl"] for r in rows)
        cost = sum(r["cost"] for r in rows)
        edges = [float(r["edge"]) for r in rows if r.get("edge") is not None]
        out.append(
            {
                "key": k,
                "n": n,
                "wins": sum(1 for r in rows if r["won"]),
                "win_rate": (sum(1 for r in rows if r["won"]) / n) if n else None,
                "pnl": round(pnl, 2),
                "ret_per_dollar": round(pnl / cost, 4) if cost else None,
                "avg_claimed_edge": round(statistics.fmean(edges), 4) if edges else None,
            }
        )
    return out


def reliability(pairs: list[tuple[float, float]], edges=(0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01)) -> list[dict[str, Any]]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        rows = [(p, o) for p, o in pairs if lo <= p < hi]
        if not rows:
            continue
        out.append(
            {
                "bucket": f"{lo:.1f}-{min(hi, 1.0):.1f}",
                "n": len(rows),
                "mean_forecast": round(statistics.fmean(p for p, _ in rows), 4),
                "realized": round(statistics.fmean(o for _, o in rows), 4),
            }
        )
    return out


def brier(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return round(statistics.fmean((p - o) ** 2 for p, o in pairs), 4)


def monte_carlo(pnls: list[float], start_equity: float, target: float, floor: float, steps: int = 200, paths: int = 2000) -> dict[str, Any]:
    if len(pnls) < 10:
        return {"n_source": len(pnls), "steps": steps, "fan": [], "p_target": None, "p_floor": None}
    rng = random.Random(11)
    ends: list[float] = []
    hit_target = 0
    hit_floor = 0
    checkpoints = list(range(0, steps + 1, 5))
    series: dict[int, list[float]] = {c: [] for c in checkpoints}
    for _ in range(paths):
        eq = start_equity
        touched_t = touched_f = False
        for i in range(1, steps + 1):
            eq += rng.choice(pnls)
            if eq >= target:
                touched_t = True
            if eq <= floor:
                touched_f = True
            if i % 5 == 0:
                series[i].append(eq)
        series[0].append(start_equity)
        ends.append(eq)
        hit_target += touched_t
        hit_floor += touched_f
    fan = []
    for c in checkpoints:
        xs = series[c]
        fan.append(
            {
                "step": c,
                "p05": round(percentile(xs, 0.05), 2),
                "p25": round(percentile(xs, 0.25), 2),
                "p50": round(percentile(xs, 0.50), 2),
                "p75": round(percentile(xs, 0.75), 2),
                "p95": round(percentile(xs, 0.95), 2),
            }
        )
    return {
        "n_source": len(pnls),
        "steps": steps,
        "paths": paths,
        "fan": fan,
        "p_target": round(hit_target / paths, 4),
        "p_floor": round(hit_floor / paths, 4),
        "p_end_positive": round(sum(1 for e in ends if e > start_equity) / paths, 4),
        "median_end": round(percentile(ends, 0.5), 2),
        "target": target,
        "floor": floor,
    }


# --------------------------------------------------------------------------- resolver
class Resolver(threading.Thread):
    """Resolve outcomes for every candidate market the bot ever scored (taken or vetoed)."""

    def __init__(self):
        super().__init__(name="dash-resolver", daemon=True)
        self.lock = threading.Lock()
        self.cache: dict[str, dict[str, Any]] = read_json(RESOLUTIONS_PATH, {}) or {}
        self.failed: dict[str, float] = {}
        self.stats = {"resolved": len(self.cache), "pending": 0, "last_error": ""}

    def get(self, market_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.cache.get(str(market_id))

    def _save(self) -> None:
        tmp = RESOLUTIONS_PATH.with_suffix(".tmp")
        with self.lock:
            tmp.write_text(json.dumps(self.cache))
        tmp.replace(RESOLUTIONS_PATH)

    @staticmethod
    def fetch_outcome(market_id: str = "", slug: str = "") -> dict[str, Any] | None:
        url = (
            f"https://gamma-api.polymarket.com/markets/{market_id}"
            if market_id
            else f"https://gamma-api.polymarket.com/markets?slug={slug}"
        )
        payload = get_json(url, timeout=10)
        row = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(row, dict) or not row.get("closed"):
            return None
        prices = row.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = []
        if not prices or len(prices) < 2:
            return None
        yes = fnum(prices[0])
        no = fnum(prices[1])
        if yes not in (0.0, 1.0) or no not in (0.0, 1.0):
            return None
        return {"yes": yes, "slug": row.get("slug") or slug, "resolved_at": time.time()}

    def run(self) -> None:
        time.sleep(20)
        while True:
            try:
                self._pass()
            except Exception as exc:  # never die
                self.stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            time.sleep(30)

    def _pass(self) -> None:
        now = time.time()
        wanted: dict[str, float] = {}
        for e in JOURNAL.load():
            if e.get("event") not in {"veto", "submit", "execution_reject"}:
                continue
            mid = str(e.get("id") or "")
            end = fnum(e.get("window_end"))
            if not mid or end <= 0 or end > now - 120:
                continue
            wanted[mid] = end
        with self.lock:
            todo = [m for m in wanted if m not in self.cache and self.failed.get(m, 0) < now - 1800]
        self.stats["pending"] = len(todo)
        done = 0
        for mid in sorted(todo, key=lambda m: -wanted[m]):
            try:
                res = self.fetch_outcome(market_id=mid)
            except Exception as exc:
                self.stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                self.failed[mid] = time.time()
                time.sleep(3)
                continue
            if res is None:
                self.failed[mid] = time.time()
            else:
                with self.lock:
                    self.cache[mid] = res
                done += 1
            time.sleep(1.5)
            if done and done % 20 == 0:
                self._save()
            if done >= 400:
                break
        if done:
            self._save()
        with self.lock:
            self.stats["resolved"] = len(self.cache)
        self.stats["pending"] = max(0, len(todo) - done)


RESOLVER = Resolver()


# --------------------------------------------------------------------------- live engine
class LiveEngine(threading.Thread):
    """Runs the bot's own signal math on the same streams, once per second, plus a
    per-window settlement audit (oracle boundary values vs resolved outcome)."""

    def __init__(self):
        super().__init__(name="dash-live", daemon=True)
        self.lock = threading.Lock()
        self.snapshot: dict[str, Any] = {"ok": False, "error": "starting"}
        self.series: deque[dict[str, Any]] = deque(maxlen=1500)
        self.markets: list[dict[str, Any]] = []
        self.markets_ts = 0.0
        self.sigma = 0.0024
        self.sigma_ts = 0.0
        self.sigma_src = "default"
        self.audit_rows: deque[dict[str, Any]] = deque(maxlen=600)
        self.pending_end: dict[str, Any] | None = None
        self.pending_res: list[dict[str, Any]] = []
        self.current_start = 0
        self.reconnects = 0
        self.binance_open: dict[int, float] = {}
        self.rest_books: dict[str, dict[str, Any]] = {}
        self.rest_books_ts = 0.0
        self._rest_client: Any = None
        self._load_audit()

    # ---- audit persistence
    def _load_audit(self) -> None:
        for line in tail_lines(AUDIT_PATH, 600):
            try:
                row = json.loads(line)
            except Exception:
                continue
            self.audit_rows.append(row)
            if row.get("outcome") is None:
                self.pending_res.append(row)

    def _append_audit(self, row: dict[str, Any]) -> None:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_PATH.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def _rewrite_audit(self) -> None:
        rows = list(self.audit_rows)
        tmp = AUDIT_PATH.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(AUDIT_PATH)

    # ---- helpers
    @staticmethod
    def _twap_rows(asset: str) -> list[tuple[int, float]]:
        from scout import crypto_lag

        with crypto_lag._CHAINLINK_LOCK:
            return [(int(ts), float(v)) for ts, v in (crypto_lag._CHAINLINK_VALUES.get(asset) or ())]

    @staticmethod
    def _nearest(rows: list[tuple[float, float]], at: float, tol: float) -> tuple[float, float] | None:
        if not rows:
            return None
        best = min(rows, key=lambda r: abs(r[0] - at))
        return best if abs(best[0] - at) <= tol else None

    def _refresh_markets(self, settings) -> None:
        from scout import crypto_lag
        from scout.gamma import normalize_market

        now = int(time.time())
        out = []
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
                if not win or win["asset"] != "btc" or win["window_s"] != 300:
                    continue
                m["win"] = win
                out.append(m)
        if out:
            from scout import streams

            streams.set_book_tokens([t for m in out for t in (m["yes_token"], m["no_token"])])
            with self.lock:
                self.markets = out
        self.markets_ts = time.time()

    def _binance_open_for(self, start: int) -> float | None:
        """Mirror the bot's fallback: Binance 1m kline open at the window start."""
        if start in self.binance_open:
            return self.binance_open[start] or None
        from scout import crypto_lag

        try:
            px = float(crypto_lag._binance_open("BTCUSDT", start))
        except Exception:
            px = 0.0
        self.binance_open[start] = px
        for k in [k for k in self.binance_open if k < start - 3600]:
            self.binance_open.pop(k, None)
        return px or None

    def _book(self, token: str) -> dict[str, Any] | None:
        """Websocket book first; REST snapshot (<=3 s old) when the stream is quiet."""
        from scout import streams

        row = streams.book(token)
        if row:
            return row
        return self.rest_books.get(token)

    def _refresh_rest_books(self, markets: list[dict[str, Any]]) -> None:
        tokens = [t for m in markets for t in (m["yes_token"], m["no_token"]) if t]
        if not tokens:
            return
        try:
            if self._rest_client is None:
                from polymarket import PublicClient

                self._rest_client = PublicClient()
            books = self._rest_client.get_order_books(token_ids=tokens)
        except Exception:
            self._rest_client = None
            return
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        for bk in books:
            asks = [(float(lv.price), float(lv.size)) for lv in (bk.asks or [])]
            bids = [(float(lv.price), float(lv.size)) for lv in (bk.bids or [])]
            if not asks:
                continue
            ask = min(px for px, _ in asks)
            bid = max((px for px, _ in bids), default=0.0)
            out[str(bk.token_id)] = {
                "bid": bid, "ask": ask, "ask_size": sum(sz for px, sz in asks if px <= ask + 0.02),
                "tick_size": float(bk.tick_size or 0.01), "event_ts": now, "quoted_at": now, "rest": True,
            }
        self.rest_books = out
        self.rest_books_ts = now

    def _refresh_sigma(self) -> None:
        from scout import crypto_lag

        try:
            closes = crypto_lag._binance_recent_closes("BTCUSDT")
            self.sigma = crypto_lag.realized_window_sigma(closes, 300)
            self.sigma_src = "binance 1m x30"
        except Exception as exc:
            self.sigma_src = f"default ({type(exc).__name__})"
        self.sigma_ts = time.time()

    # ---- main loop
    def run(self) -> None:
        try:
            from scout import crypto_lag, streams
            from scout.config import settings_from_env

            settings = settings_from_env()
            crypto_lag.start_chainlink_stream()
            streams.start_streams()
        except Exception as exc:
            with self.lock:
                self.snapshot = {"ok": False, "error": f"stream init failed: {type(exc).__name__}: {exc}"}
            return
        while True:
            try:
                now = time.time()
                if now - self.markets_ts > 15:
                    try:
                        self._refresh_markets(settings)
                    except Exception as exc:
                        self.snapshot["markets_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                if now - self.sigma_ts > 60:
                    self._refresh_sigma()
                self._tick(settings)
                self._audit_step()
            except Exception as exc:
                with self.lock:
                    self.snapshot = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            time.sleep(1.0)

    def _tick(self, settings) -> None:
        from scout import crypto_lag, streams
        from scout.twap_lock import MIN_P_LOCK, lock_signal

        now = time.time()
        inow = int(now)
        start = inow - (inow % 300)
        end = start + 300
        seconds_left = end - now
        with self.lock:
            markets = list(self.markets)
        cur = next((m for m in markets if m["win"]["start"] == start), None)
        nxt = next((m for m in markets if m["win"]["start"] == end), None)

        twap_rows = self._twap_rows("btc")
        raw_ticks = streams.oracle_ticks("btc", start - 10)
        twap_latest = twap_rows[-1] if twap_rows else None
        twap_open = self._nearest(twap_rows, start, 8)
        raw_open = self._nearest(raw_ticks, start, 3)
        open_source = "chainlink"
        if twap_open is None:
            bo = self._binance_open_for(start)
            if bo:
                twap_open = (float(start), bo)
                open_source = "binance"
        if cur and (not streams.book(cur["yes_token"]) or not streams.book(cur["no_token"])) and now - self.rest_books_ts > 3:
            self._refresh_rest_books([m for m in (cur, nxt) if m])
        raw_spot = streams.oracle_spot("btc", max_age=5)
        exch = streams.exchange_spot("btc", max_age=5)

        sigma = self.sigma
        t = max(seconds_left / 300.0, 0.02)
        sigma_t = math.sqrt((sigma * math.sqrt(t)) ** 2 + 0.00008**2)

        def model_p(chg: float | None) -> float | None:
            if chg is None:
                return None
            return crypto_lag.p_up_from_move(chg, seconds_left, 300, sigma_full=sigma, oracle_noise=0.00008, calibrated=True)

        chg_twap = chg_raw = chg_raw_raw = None
        if twap_open and twap_latest and twap_open[1] > 0:
            chg_twap = (twap_latest[1] - twap_open[1]) / twap_open[1]
        if twap_open and raw_spot and twap_open[1] > 0:
            chg_raw = (raw_spot[0] - twap_open[1]) / twap_open[1]
        if raw_open and raw_spot and raw_open[1] > 0:
            chg_raw_raw = (raw_spot[0] - raw_open[1]) / raw_open[1]
        p_model = model_p(chg_twap)
        p_model_raw = model_p(chg_raw)

        books = {}
        yes_ask = no_ask = yes_bid = no_bid = None
        if cur:
            yb = self._book(cur["yes_token"])
            nb = self._book(cur["no_token"])
            if yb and nb and 0 < yb["ask"] < 1 and 0 < nb["ask"] < 1:
                yes_ask, no_ask, yes_bid, no_bid = yb["ask"], nb["ask"], yb["bid"], nb["bid"]
                books = {
                    "yes_ask": yes_ask, "yes_bid": yes_bid, "yes_ask_size": yb.get("ask_size"),
                    "no_ask": no_ask, "no_bid": no_bid, "no_ask_size": nb.get("ask_size"),
                    "quoted_at": yb.get("quoted_at"), "tick": yb.get("tick_size"), "via": "rest" if yb.get("rest") else "ws",
                }
        nxt_books = {}
        if nxt:
            yb = self._book(nxt["yes_token"])
            nb = self._book(nxt["no_token"])
            if yb and nb:
                nxt_books = {"yes_ask": yb["ask"], "yes_bid": yb["bid"], "no_ask": nb["ask"], "no_bid": nb["bid"]}

        market_p = None
        if yes_ask is not None:
            yb_ = yes_bid if yes_bid and yes_bid > 0 else max(0.0, 1.0 - no_ask)
            market_p = min(0.99, max(0.01, (yes_ask + yb_) / 2.0))
        w = min(1.0, max(0.0, settings.crypto_model_weight))
        p_blend = None
        if p_model is not None and market_p is not None:
            p_blend = w * p_model + (1 - w) * market_p
        elif p_model is not None:
            p_blend = p_model

        up_edge = down_edge = None
        yes_ok = no_ok = None
        if p_blend is not None and yes_ask is not None:
            fy = taker_fee(yes_ask, settings.taker_fee_rate)
            fn = taker_fee(no_ask, settings.taker_fee_rate)
            up_edge = p_blend - yes_ask - fy
            down_edge = (1 - p_blend) - no_ask - fn
            yes_ok = crypto_lag.crypto_side_ok(yes_ask, p_blend)
            no_ok = crypto_lag.crypto_side_ok(no_ask, 1 - p_blend)
        in_entry = crypto_lag.in_entry_window(seconds_left, 300, now=inow, start=start)
        phase = "pre" if seconds_left > 250 else "entry" if seconds_left >= 50 else "endgame" if seconds_left >= 0 else "settling"

        verdict = "no data"
        best_side = None
        if p_blend is not None and yes_ask is not None:
            min_edge = settings.crypto_min_edge
            cands = []
            if yes_ok and up_edge is not None and up_edge >= min_edge:
                cands.append(("YES", yes_ask, up_edge))
            if no_ok and down_edge is not None and down_edge >= min_edge:
                cands.append(("NO", no_ask, down_edge))
            if not in_entry:
                verdict = "outside entry window"
            elif cands:
                best_side = max(cands, key=lambda c: c[2])
                verdict = f"{best_side[0]} candidate @ {best_side[1]:.2f} edge {best_side[2]:+.3f}"
            else:
                be = max(up_edge or -1, down_edge or -1)
                verdict = "no side passes filters" if not (yes_ok or no_ok) else f"edge too thin ({be:+.3f} < {min_edge:.2f})"

        lock = None
        if twap_open and 30 <= seconds_left <= 90:
            try:
                sig = lock_signal("btc", {"end": end, "window_s": 300}, twap_open[1], sigma, now=now)
                if sig:
                    lock = {**sig, "min_p_lock": MIN_P_LOCK, "armed": sig["p_up"] >= MIN_P_LOCK or sig["p_up"] <= 1 - MIN_P_LOCK}
            except Exception:
                lock = None

        z = (chg_twap / sigma_t) if chg_twap is not None else None
        snap = {
            "ok": True,
            "now": now,
            "window": {
                "start": start, "end": end, "seconds_left": round(seconds_left, 1), "phase": phase,
                "in_entry_window": in_entry,
                "market_id": cur["id"] if cur else None, "question": cur["question"] if cur else None,
                "slug": cur["slug"] if cur else None, "liquidity": cur["liquidity"] if cur else None,
                "volume": cur.get("volume_24h") if cur else None,
                "next_market_id": nxt["id"] if nxt else None, "next_question": nxt["question"] if nxt else None,
            },
            "oracle": {
                "raw": raw_spot[0] if raw_spot else None, "raw_ts": raw_spot[1] if raw_spot else None,
                "raw_age_s": round(now - raw_spot[1], 1) if raw_spot else None,
                "twap": twap_latest[1] if twap_latest else None, "twap_ts": twap_latest[0] if twap_latest else None,
                "twap_age_s": round(now - twap_latest[0], 1) if twap_latest else None,
                "twap_open": twap_open[1] if twap_open else None, "raw_open": raw_open[1] if raw_open else None,
                "open_source": open_source if twap_open else None,
                "exchange": exch[0] if exch else None, "exchange_age_s": round(now - exch[1], 1) if exch else None,
                "twap_lag_bps": round((raw_spot[0] - twap_latest[1]) / twap_latest[1] * 1e4, 2) if raw_spot and twap_latest else None,
                "n_twap_rows": len(twap_rows), "n_raw_ticks": len(raw_ticks),
            },
            "signal": {
                "chg_twap_bps": round(chg_twap * 1e4, 2) if chg_twap is not None else None,
                "chg_raw_bps": round(chg_raw * 1e4, 2) if chg_raw is not None else None,
                "chg_rawraw_bps": round(chg_raw_raw * 1e4, 2) if chg_raw_raw is not None else None,
                "sigma_full_pct": round(sigma * 100, 4), "sigma_t_pct": round(sigma_t * 100, 4), "sigma_src": self.sigma_src,
                "z": round(z, 3) if z is not None else None,
                "p_model": round(p_model, 4) if p_model is not None else None,
                "p_model_raw_spot": round(p_model_raw, 4) if p_model_raw is not None else None,
                "market_p": round(market_p, 4) if market_p is not None else None,
                "p_blend": round(p_blend, 4) if p_blend is not None else None,
                "model_weight": w,
                "up_edge": round(up_edge, 4) if up_edge is not None else None,
                "down_edge": round(down_edge, 4) if down_edge is not None else None,
                "yes_ok": yes_ok, "no_ok": no_ok, "min_edge": settings.crypto_min_edge,
                "verdict": verdict, "best_side": best_side[0] if best_side else None,
                "min_ask": float(os.getenv("CRYPTO_MIN_ASK") or 0.25),
            },
            "books": books,
            "next_books": nxt_books,
            "lock": lock,
            "markets_n": len(markets),
        }
        with self.lock:
            self.snapshot = snap
            self.series.append(
                {
                    "t": round(now, 1),
                    "raw": raw_spot[0] if raw_spot else None,
                    "twap": twap_latest[1] if twap_latest else None,
                    "exch": exch[0] if exch else None,
                    "yes_ask": yes_ask, "no_ask": no_ask,
                    "p_model": round(p_model, 4) if p_model is not None else None,
                    "p_blend": round(p_blend, 4) if p_blend is not None else None,
                    "market_p": round(market_p, 4) if market_p is not None else None,
                    "w": start,
                }
            )
        # ---- audit bookkeeping
        if start != self.current_start:
            if self.current_start and self.pending_end is None:
                self.pending_end = {"start": self.current_start, "end": self.current_start + 300, "market_id": None}
                prev = next((m for m in markets if m["win"]["start"] == self.current_start), None)
                if prev:
                    self.pending_end["market_id"] = prev["id"]
            self.current_start = start

    def _audit_step(self) -> None:
        from scout import streams

        now = time.time()
        pe = self.pending_end
        if pe and now >= pe["end"] + 4:
            start, end = pe["start"], pe["end"]
            twap_rows = self._twap_rows("btc")
            raw_ticks = streams.oracle_ticks("btc", start - 10)
            t_open = self._nearest(twap_rows, start, 8)
            t_end = self._nearest(twap_rows, end, 3)
            r_open = self._nearest(raw_ticks, start, 3)
            r_end = self._nearest(raw_ticks, end, 3)
            in_win = [px for ts, px in raw_ticks if start <= ts <= end]
            row = {
                "start": start, "end": end, "slug": f"btc-updown-5m-{start}", "market_id": pe.get("market_id"),
                "twap_open": t_open[1] if t_open else None, "twap_end": t_end[1] if t_end else None,
                "raw_open": r_open[1] if r_open else None, "raw_end": r_end[1] if r_end else None,
                "raw_min": min(in_win) if in_win else None, "raw_max": max(in_win) if in_win else None,
                "raw_ticks": len(in_win), "outcome": None, "attempts": 0, "due": end + 95,
            }
            self.audit_rows.append(row)
            self.pending_res.append(row)
            self._append_audit(row)
            self.pending_end = None
        # resolve outcomes
        changed = False
        for row in list(self.pending_res):
            if now < row.get("due", 0):
                continue
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row["due"] = now + 60
            try:
                res = RESOLVER.fetch_outcome(slug=row["slug"]) if not row.get("market_id") else RESOLVER.fetch_outcome(market_id=str(row["market_id"]))
            except Exception:
                res = None
            if res is not None:
                row["outcome"] = res["yes"]
                self.pending_res.remove(row)
                changed = True
            elif row["attempts"] >= 12:
                self.pending_res.remove(row)
                changed = True
            time.sleep(0.3)
        if changed:
            self._rewrite_audit()

    def audit_summary(self) -> dict[str, Any]:
        rows = [r for r in self.audit_rows if r.get("outcome") is not None]
        stats = {"windows_recorded": len(self.audit_rows), "resolved": len(rows), "pending": len(self.pending_res)}
        agree_tt = agree_rt = agree_rr = 0
        n_tt = n_rt = n_rr = 0
        disc = []
        for r in rows:
            up = r["outcome"] >= 0.5
            te = r.get("twap_end")
            if te is None:
                continue
            if r.get("twap_open") is not None:
                n_tt += 1
                if (te >= r["twap_open"]) == up:
                    agree_tt += 1
            if r.get("raw_open") is not None:
                n_rt += 1
                if (te >= r["raw_open"]) == up:
                    agree_rt += 1
            if r.get("raw_open") is not None and r.get("raw_end") is not None:
                n_rr += 1
                if (r["raw_end"] >= r["raw_open"]) == up:
                    agree_rr += 1
            if r.get("twap_open") is not None and r.get("raw_open") is not None:
                s1 = te >= r["twap_open"]
                s2 = te >= r["raw_open"]
                if s1 != s2:
                    disc.append({"start": r["start"], "outcome_up": up, "twap_open_says_up": s1, "raw_open_says_up": s2})
        stats.update(
            {
                "agree_twap_open": {"n": n_tt, "agree": agree_tt},
                "agree_raw_open": {"n": n_rt, "agree": agree_rt},
                "agree_raw_both": {"n": n_rr, "agree": agree_rr},
                "discriminating": disc[-20:],
                "recent": [
                    {k: r.get(k) for k in ("start", "twap_open", "raw_open", "twap_end", "raw_end", "outcome")}
                    for r in list(self.audit_rows)[-12:]
                ],
            }
        )
        return stats


ENGINE = LiveEngine()


# --------------------------------------------------------------------------- indicators
class Indicators(threading.Thread):
    """External context: multi-venue spot, perp funding/OI/positioning, liquidations,
    implied vol, sentiment. Every poll is appended to data/features.jsonl."""

    def __init__(self):
        super().__init__(name="dash-indicators", daemon=True)
        self.lock = threading.Lock()
        self.latest: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.fng: dict[str, Any] = {}
        self.fng_ts = 0.0
        self.oi_hist: deque[tuple[float, float]] = deque(maxlen=400)
        self.rows_written = 0

    def _safe(self, key: str, fn):
        try:
            val = fn()
            self.errors.pop(key, None)
            return val
        except Exception as exc:
            self.errors[key] = f"{type(exc).__name__}: {str(exc)[:80]}"
            return None

    def run(self) -> None:
        time.sleep(3)
        while True:
            try:
                self.poll()
            except Exception as exc:
                self.errors["poll"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            time.sleep(15)

    def poll(self) -> None:
        now = time.time()
        out: dict[str, Any] = {"ts": round(now, 1)}
        cb = self._safe("coinbase", lambda: get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker"))
        if cb:
            out["coinbase"] = fnum(cb.get("price"))
            out["coinbase_bid"] = fnum(cb.get("bid"))
            out["coinbase_ask"] = fnum(cb.get("ask"))
        kr = self._safe("kraken", lambda: get_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD"))
        if kr:
            res = (kr.get("result") or {}).get("XXBTZUSD") or {}
            out["kraken"] = fnum((res.get("c") or [0])[0])
        okt = self._safe("okx_ticker", lambda: get_json("https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"))
        if okt and okt.get("data"):
            d = okt["data"][0]
            out["okx_perp"] = fnum(d.get("last"))
            out["okx_vol24h_btc"] = fnum(d.get("volCcy24h"))
            out["okx_open24h"] = fnum(d.get("open24h"))
        okf = self._safe("okx_funding", lambda: get_json("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"))
        if okf and okf.get("data"):
            d = okf["data"][0]
            out["funding_rate"] = fnum(d.get("fundingRate"))
            out["funding_premium"] = fnum(d.get("premium"))
            out["funding_next_ts"] = fnum(d.get("fundingTime")) / 1000
        oko = self._safe("okx_oi", lambda: get_json("https://www.okx.com/api/v5/public/open-interest?instId=BTC-USDT-SWAP"))
        if oko and oko.get("data"):
            oi = fnum(oko["data"][0].get("oiUsd"))
            out["oi_usd"] = oi
            self.oi_hist.append((now, oi))
            past = [v for t, v in self.oi_hist if t <= now - 3600]
            if past:
                out["oi_change_1h_pct"] = round((oi - past[-1]) / past[-1] * 100, 3) if past[-1] else None
        okl = self._safe("okx_ls", lambda: get_json("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=5m"))
        if okl and okl.get("data"):
            out["long_short_ratio"] = fnum(okl["data"][0][1])
        okv = self._safe("okx_taker", lambda: get_json("https://www.okx.com/api/v5/rubik/stat/taker-volume?ccy=BTC&instType=CONTRACTS&period=5m"))
        if okv and okv.get("data"):
            row = okv["data"][0]
            sell, buy = fnum(row[1]), fnum(row[2])
            out["taker_buy_usd_5m"] = buy
            out["taker_sell_usd_5m"] = sell
            out["taker_buy_ratio_5m"] = round(buy / (buy + sell), 4) if (buy + sell) > 0 else None
        okq = self._safe("okx_liq", lambda: get_json("https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&instFamily=BTC-USDT&state=filled&limit=100"))
        if okq and okq.get("data"):
            long_5m = short_5m = long_1h = short_1h = 0.0
            for d in okq["data"]:
                for det in d.get("details") or []:
                    ts = fnum(det.get("ts")) / 1000
                    notional = fnum(det.get("sz")) * 0.01 * fnum(det.get("bkPx"))
                    is_long = det.get("posSide") == "long"
                    if ts >= now - 300:
                        if is_long:
                            long_5m += notional
                        else:
                            short_5m += notional
                    if ts >= now - 3600:
                        if is_long:
                            long_1h += notional
                        else:
                            short_1h += notional
            out.update({"liq_long_5m": round(long_5m), "liq_short_5m": round(short_5m), "liq_long_1h": round(long_1h), "liq_short_1h": round(short_1h)})
        ms = int(now * 1000)
        dv = self._safe("deribit_dvol", lambda: get_json(f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={ms - 4 * 3600 * 1000}&end_timestamp={ms}"))
        if dv and (dv.get("result") or {}).get("data"):
            out["dvol"] = fnum(dv["result"]["data"][-1][4])
            out["implied_sigma_5m_pct"] = round(out["dvol"] / 100 * math.sqrt(300 / (365 * 86400)) * 100, 4)
        di = self._safe("deribit_index", lambda: get_json("https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd"))
        if di and di.get("result"):
            out["deribit_index"] = fnum(di["result"].get("index_price"))
        df = self._safe("deribit_funding", lambda: get_json(f"https://www.deribit.com/api/v2/public/get_funding_rate_value?instrument_name=BTC-PERPETUAL&start_timestamp={ms - 8 * 3600 * 1000}&end_timestamp={ms}"))
        if df and df.get("result") is not None:
            out["deribit_funding_8h"] = fnum(df.get("result"))
        cc = self._safe("coinbase_candles", lambda: get_json("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"))
        if cc and isinstance(cc, list) and len(cc) > 10:
            closes = [fnum(c[4]) for c in sorted(cc, key=lambda c: c[0])[-60:]]
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
            if len(rets) > 5:
                m = statistics.fmean(rets)
                var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
                out["realized_sigma_5m_pct_1h"] = round(math.sqrt(var) * math.sqrt(5) * 100, 4)
                out["ret_15m_bps"] = round((closes[-1] / closes[-16] - 1) * 1e4, 2) if len(closes) >= 16 else None
                out["ret_60m_bps"] = round((closes[-1] / closes[0] - 1) * 1e4, 2)
        if now - self.fng_ts > 600:
            fg = self._safe("fear_greed", lambda: get_json("https://api.alternative.me/fng/?limit=1", timeout=10))
            if fg and fg.get("data"):
                self.fng = {"value": fnum(fg["data"][0].get("value")), "label": fg["data"][0].get("value_classification")}
            self.fng_ts = now
        if self.fng:
            out["fear_greed"] = self.fng.get("value")
            out["fear_greed_label"] = self.fng.get("label")
        # derived vs the oracle the market settles on
        snap = ENGINE.snapshot if ENGINE.snapshot.get("ok") else {}
        raw = (snap.get("oracle") or {}).get("raw")
        if raw and out.get("coinbase"):
            out["coinbase_vs_oracle_bps"] = round((out["coinbase"] - raw) / raw * 1e4, 2)
        if out.get("okx_perp") and out.get("coinbase"):
            out["perp_premium_bps"] = round((out["okx_perp"] - out["coinbase"]) / out["coinbase"] * 1e4, 2)
        if snap:
            sig = snap.get("signal") or {}
            out["bot_sigma_5m_pct"] = sig.get("sigma_full_pct")
            out["bot_chg_bps"] = sig.get("chg_twap_bps")
            out["bot_z"] = sig.get("z")
            out["bot_p_model"] = sig.get("p_model")
            out["yes_ask"] = (snap.get("books") or {}).get("yes_ask")
            out["no_ask"] = (snap.get("books") or {}).get("no_ask")
            out["seconds_left"] = (snap.get("window") or {}).get("seconds_left")
            out["oracle_raw"] = raw
            out["oracle_twap"] = (snap.get("oracle") or {}).get("twap")
        with self.lock:
            self.latest = out
        self._log(out)

    def _log(self, row: dict[str, Any]) -> None:
        try:
            FEATURES_PATH.parent.mkdir(parents=True, exist_ok=True)
            if FEATURES_PATH.exists() and FEATURES_PATH.stat().st_size > 40_000_000:
                lines = FEATURES_PATH.read_text().splitlines()
                FEATURES_PATH.write_text("\n".join(lines[len(lines) // 2:]) + "\n")
            with FEATURES_PATH.open("a") as fh:
                fh.write(json.dumps(clean(row)) + "\n")
            self.rows_written += 1
        except Exception as exc:
            self.errors["features_log"] = f"{type(exc).__name__}: {str(exc)[:80]}"


INDICATORS = Indicators()


# --------------------------------------------------------------------------- health
_HEALTH_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}


def launchd_status() -> dict[str, Any]:
    now = time.time()
    if now - _HEALTH_CACHE["ts"] < 30:
        return _HEALTH_CACHE["data"]
    out: dict[str, Any] = {}
    uid = os.getuid()
    for label in ("com.tradeinc.scout", "com.tradeinc.dashboard", "com.tradeinc.lab"):
        info: dict[str, Any] = {"loaded": False}
        try:
            proc = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True, timeout=5)
            if proc.returncode == 0:
                info["loaded"] = True
                for key, rx in (("state", r"^\s*state = (\S+)"), ("pid", r"^\s*pid = (\d+)"), ("runs", r"^\s*runs = (\d+)"), ("last_exit", r"last exit code = (.+)$")):
                    m = re.search(rx, proc.stdout, re.M)
                    if m:
                        info[key] = m.group(1).strip()
        except Exception as exc:
            info["error"] = f"{type(exc).__name__}"
        out[label] = info
    _HEALTH_CACHE.update({"ts": now, "data": out})
    return out


def parse_funnel(log_lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ln in reversed(log_lines):
        if "5m skip" in ln and "skip5" not in out:
            out["skip5"] = dict(re.findall(r"(\w+)=(\d+)", ln))
        elif ln.startswith("crypto 5m/15m") and "universe" not in out:
            m = re.search(r"crypto 5m/15m (\d+) clob_fresh=(\d+) fav_hits=(\d+)", ln)
            if m:
                out["universe"] = {"markets": int(m.group(1)), "clob_fresh": int(m.group(2)), "fav_hits": int(m.group(3))}
        elif ln.strip().startswith("closest") and "closest" not in out:
            out["closest"] = ln.strip()
        elif ln.startswith("paper scanned=") and "cycle" not in out:
            out["cycle"] = dict((k, v) for k, v in re.findall(r"(\w+)=([\w.]+)", ln))
        if len(out) >= 4:
            break
    return out


# --------------------------------------------------------------------------- state
_STATE_CACHE: dict[str, Any] = {"key": None, "ts": 0.0, "data": None}
_STATE_LOCK = threading.Lock()


def build_state() -> dict[str, Any]:
    now = time.time()
    ledger_path = ledger_file()
    try:
        key = (ledger_path.stat().st_mtime, (DATA / "journal.jsonl").stat().st_mtime, RESOLVER.stats.get("resolved"))
    except FileNotFoundError:
        key = None
    key = (key, is_live())
    with _STATE_LOCK:
        if _STATE_CACHE["data"] is not None and _STATE_CACHE["key"] == key and now - _STATE_CACHE["ts"] < 10:
            return _STATE_CACHE["data"]
        data = _compute_state(now)
        _STATE_CACHE.update({"key": key, "ts": now, "data": data})
        return data


def _compute_state(now: float) -> dict[str, Any]:
    ledger = read_json_stable(ledger_file(), {})
    events = JOURNAL.load()
    trades = pair_trades(ledger)
    enrich_trades(trades, events)
    fills = ledger.get("fills") or []
    entries = [f for f in fills if not str(f.get("side", "")).startswith("CLOSE_")]
    start_bank = fnum(ledger.get("starting_bankroll"), 50.0)
    cash = fnum(ledger.get("cash"))
    equity = fnum(ledger.get("last_equity"), cash)
    session_start = str(ledger.get("session_started_at") or "")
    session_start_ts = parse_ts(session_start) if session_start else 0.0

    pnls = [t["pnl"] for t in trades]
    rets = [t["ret"] for t in trades]
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    total_cost = sum(t["cost"] for t in trades)
    fees = sum(fnum(f.get("fee")) for f in entries)
    realized = sum(pnls)

    # equity curve + drawdown (realized bankroll after each close)
    curve = [{"t": trades[0]["entry_ts"] if trades else now, "v": start_bank}]
    bank = start_bank
    peak = start_bank
    max_dd = 0.0
    dd_series = []
    for t in trades:
        bank += t["pnl"]
        peak = max(peak, bank)
        dd = peak - bank
        max_dd = max(max_dd, dd)
        curve.append({"t": t["close_ts"], "v": round(bank, 4)})
        dd_series.append({"t": t["close_ts"], "v": round(-dd, 4)})
    current_dd = round(peak - bank, 2)

    # daily (ET)
    daily: dict[str, dict[str, Any]] = {}
    for t in trades:
        d = et_date(t["close_ts"])
        row = daily.setdefault(d, {"date": d, "pnl": 0.0, "n": 0, "wins": 0, "fees": 0.0})
        row["pnl"] += t["pnl"]
        row["n"] += 1
        row["wins"] += 1 if t["won"] else 0
        row["fees"] += t["fee"]
    daily_rows = [dict(r, pnl=round(r["pnl"], 2), fees=round(r["fees"], 2)) for r in daily.values()]
    daily_rows.sort(key=lambda r: r["date"])
    today = et_date(now)
    today_row = daily.get(today) or {"pnl": 0.0, "n": 0, "wins": 0}
    streak = 0
    for r in reversed(daily_rows):
        if r["pnl"] > 0:
            streak += 1
        else:
            break
    session_trades = [t for t in trades if t["close_ts"] >= session_start_ts] if session_start_ts else []

    # rolling (20-trade)
    rolling = []
    win_flags = [1 if t["won"] else 0 for t in trades]
    for i in range(len(trades)):
        lo = max(0, i - 19)
        w = win_flags[lo : i + 1]
        r = rets[lo : i + 1]
        rolling.append({"i": i + 1, "t": trades[i]["close_ts"], "win_rate": round(sum(w) / len(w), 4), "ret": round(statistics.fmean(r), 4), "n": len(w)})

    # stats
    tstat = t_stat(pnls)
    tstat_ret = t_stat(rets)
    ci = bootstrap_ci(pnls)
    mean_pnl, sd_pnl = mean_sd(pnls)

    # calibration (taken trades): model vs market vs outcome, side-relative
    cal_model = [(float(t["fair"]), 1.0 if t["won"] else 0.0) for t in trades if t.get("fair") is not None]
    cal_market = [(float(t["market_p_side"]), 1.0 if t["won"] else 0.0) for t in trades if t.get("market_p_side") is not None]
    # calibration over ALL candidates (taken + vetoed) using resolved outcomes
    cand_pairs_model: list[tuple[float, float]] = []
    cand_pairs_market: list[tuple[float, float]] = []
    veto_rows: list[dict[str, Any]] = []
    veto_counts: Counter[str] = Counter()
    veto_counts_24h: Counter[str] = Counter()
    cf_by_reason: dict[str, dict[str, Any]] = {}
    seen_taken: set[str] = set()
    for e in events:
        kind = e.get("event")
        if kind not in {"veto", "submit"}:
            continue
        mid = str(e.get("id") or "")
        res = RESOLVER.get(mid)
        side = e.get("side")
        price = fnum(e.get("price"))
        fair = e.get("fair")
        mp = e.get("market_p_yes")
        won = None
        if res is not None and side in {"YES", "NO"}:
            won = (res["yes"] >= 0.5) if side == "YES" else (res["yes"] < 0.5)
        if kind == "submit":
            if won is not None and fair is not None and mid not in seen_taken:
                seen_taken.add(mid)
                cand_pairs_model.append((float(fair), 1.0 if won else 0.0))
                if mp is not None:
                    cand_pairs_market.append((float(mp) if side == "YES" else 1 - float(mp), 1.0 if won else 0.0))
            continue
        reason = str(e.get("reason") or "unknown")
        rkey = reason.split("(")[0][:40]
        veto_counts[rkey] += 1
        if fnum(e.get("ts")) >= now - 86400:
            veto_counts_24h[rkey] += 1
        row = {
            "ts": e.get("ts"), "market_id": mid, "reason": rkey, "side": side, "price": price,
            "fair": fair, "edge": e.get("edge"), "window_end": e.get("window_end"),
            "outcome_won": won, "ret_if_taken": round(ret_per_dollar(price, won), 4) if won is not None else None,
        }
        veto_rows.append(row)
        if won is not None:
            agg = cf_by_reason.setdefault(rkey, {"reason": rkey, "n_resolved": 0, "wins": 0, "ret_sum": 0.0})
            agg["n_resolved"] += 1
            agg["wins"] += 1 if won else 0
            agg["ret_sum"] += row["ret_if_taken"] or 0.0
            if fair is not None:
                cand_pairs_model.append((float(fair), 1.0 if won else 0.0))
                if mp is not None:
                    cand_pairs_market.append((float(mp) if side == "YES" else 1 - float(mp), 1.0 if won else 0.0))
    cf_rows = []
    for agg in cf_by_reason.values():
        n = agg["n_resolved"]
        cf_rows.append(
            {
                "reason": agg["reason"], "n_resolved": n, "n_total": veto_counts.get(agg["reason"], n),
                "win_rate": round(agg["wins"] / n, 4) if n else None,
                "ret_per_dollar": round(agg["ret_sum"] / n, 4) if n else None,
                "pnl_at_5": round(agg["ret_sum"] * 5.0, 2),
            }
        )
    cf_rows.sort(key=lambda r: -r["n_total"])
    # unique vetoed markets per reason (a market is vetoed on many cycles)
    uniq: dict[str, set[str]] = defaultdict(set)
    for v in veto_rows:
        uniq[v["reason"]].add(v["market_id"])
    for r in cf_rows:
        r["unique_markets"] = len(uniq.get(r["reason"], ()))
    # de-duplicate counterfactual rows to one per market (first veto of that market)
    cf_unique: dict[str, dict[str, Any]] = {}
    for v in veto_rows:
        if v["outcome_won"] is None:
            continue
        cf_unique.setdefault(v["reason"] + "|" + v["market_id"], v)
    cf_unique_rows: dict[str, dict[str, Any]] = {}
    for v in cf_unique.values():
        agg = cf_unique_rows.setdefault(v["reason"], {"reason": v["reason"], "markets": 0, "wins": 0, "ret_sum": 0.0})
        agg["markets"] += 1
        agg["wins"] += 1 if v["outcome_won"] else 0
        agg["ret_sum"] += v["ret_if_taken"] or 0.0
    cf_unique_list = [
        {"reason": a["reason"], "markets": a["markets"], "win_rate": round(a["wins"] / a["markets"], 4), "ret_per_dollar": round(a["ret_sum"] / a["markets"], 4), "pnl_at_5": round(a["ret_sum"] * 5, 2)}
        for a in cf_unique_rows.values()
    ]
    cf_unique_list.sort(key=lambda r: -r["markets"])

    # cycles / cadence
    cycles = [e for e in events if e.get("event") == "cycle"]
    recent_cycles = [e for e in cycles if fnum(e.get("ts")) >= now - 1800]
    gaps = [fnum(b["ts"]) - fnum(a["ts"]) for a, b in zip(recent_cycles, recent_cycles[1:])]
    last_cycle_ts = fnum(cycles[-1]["ts"]) if cycles else 0.0
    posts_24h = sum(int(e.get("posts") or 0) for e in cycles if fnum(e.get("ts")) >= now - 86400)
    log_path = DATA / ("live.log" if is_live() else "night.log")
    log_age = now - log_path.stat().st_mtime if log_path.exists() else 1e9
    log_lines = tail_lines(log_path, 400)
    err_lines = tail_lines(DATA / ("live.err.log" if is_live() else "night.err.log"), 8)
    halted = bool(ledger.get("halted"))
    status = "halted" if halted else "running" if log_age < 90 else "down"
    reconnects = sum(1 for ln in log_lines if "reconnect" in ln)

    # gate
    days_active = max(1, len(daily_rows))
    trades_per_day = len(trades) / days_active
    gate = {
        "fills": {"value": len(trades), "target": GATE["fills"], "pass": len(trades) >= GATE["fills"]},
        "t_stat": {"value": round(tstat, 3) if tstat is not None else None, "target": GATE["t_stat"], "pass": bool(tstat is not None and tstat >= GATE["t_stat"])},
        "positive_days": {"value": streak, "target": GATE["positive_days"], "pass": streak >= GATE["positive_days"]},
        "trades_per_day": round(trades_per_day, 1),
        "days_to_fills": round((GATE["fills"] - len(trades)) / trades_per_day, 1) if trades_per_day > 0 and len(trades) < GATE["fills"] else 0,
    }
    gate["all_pass"] = all(gate[k]["pass"] for k in ("fills", "t_stat", "positive_days"))

    settings_env = {k: os.getenv(k) for k in ("CAMPAIGN_TARGET_USD", "MAX_SESSION_DRAWDOWN", "CRYPTO_MIN_ASK", "MAX_CRYPTO_STAKE", "CRYPTO_MIN_EDGE", "LIVE")}
    try:
        from scout.config import settings_from_env

        st = settings_from_env()
        settings_env.update({"CAMPAIGN_TARGET_USD": st.campaign_target_usd, "MAX_SESSION_DRAWDOWN": st.max_session_drawdown, "CRYPTO_MIN_EDGE": st.crypto_min_edge, "MAX_CRYPTO_STAKE": st.max_crypto_stake, "LIVE": st.live, "TAKER_FEE_RATE": st.taker_fee_rate, "MODEL_WEIGHT": st.crypto_model_weight})
    except Exception:
        pass
    target = fnum(settings_env.get("CAMPAIGN_TARGET_USD"), 500.0)
    cash_floor = fnum(ledger.get("cash_floor"), 0.0)

    hours = breakdown(trades, lambda t: datetime.fromtimestamp(t["entry_ts"], ET).hour if t["entry_ts"] else None, order=list(range(24)))
    weekdays = breakdown(trades, lambda t: datetime.fromtimestamp(t["entry_ts"], ET).weekday() if t["entry_ts"] else None, order=list(range(7)))
    left_buckets = breakdown(trades, lambda t: (int(t["left_s"]) // 30) * 30 if t.get("left_s") is not None else None, order=list(range(30, 271, 30)))
    z_buckets = breakdown(trades, lambda t: ("<-2" if t["z"] < -2 else "-2..-1" if t["z"] < -1 else "-1..0" if t["z"] < 0 else "0..1" if t["z"] < 1 else "1..2" if t["z"] < 2 else ">2") if t.get("z") is not None else None, order=["<-2", "-2..-1", "-1..0", "0..1", "1..2", ">2"])
    edge_buckets = breakdown(trades, lambda t: ("0.03-0.05" if t["edge"] < 0.05 else "0.05-0.08" if t["edge"] < 0.08 else "0.08-0.12" if t["edge"] < 0.12 else "0.12-0.20" if t["edge"] < 0.20 else "0.20+") if t.get("edge") is not None else None, order=["0.03-0.05", "0.05-0.08", "0.08-0.12", "0.12-0.20", "0.20+"])
    age_buckets = breakdown(trades, lambda t: ("<1s" if t["signal_age_ms"] < 1000 else "1-2s" if t["signal_age_ms"] < 2000 else "2-3s" if t["signal_age_ms"] < 3000 else "3s+") if t.get("signal_age_ms") is not None else None, order=["<1s", "1-2s", "2-3s", "3s+"])
    bands = breakdown(trades, lambda t: t["band"], order=[f"{lo:.2f}-{hi:.2f}" for lo, hi in BANDS])
    sides = breakdown(trades, lambda t: t["side"], order=["YES", "NO"])
    sources = breakdown(trades, lambda t: t.get("source") or "unknown", order=["chainlink", "chainlink_raw", "binance", "unknown"])
    disagreement = breakdown(
        trades,
        lambda t: (
            ("<0.05" if d < 0.05 else "0.05-0.10" if d < 0.10 else "0.10-0.15" if d < 0.15 else "0.15-0.20" if d < 0.20 else "0.20+")
            if (d := (float(t["fair"]) - float(t["market_p_side"])) if (t.get("fair") is not None and t.get("market_p_side") is not None) else None) is not None
            else None
        ),
        order=["<0.05", "0.05-0.10", "0.10-0.15", "0.15-0.20", "0.20+"],
    )
    strategies = breakdown(trades, lambda t: t["strategy"])
    lessons = read_json_stable(ROOT / "learning" / "lessons.json", None)

    mc = monte_carlo(pnls, cash, target, cash_floor if cash_floor > 0 else max(0.0, cash - 12.0))
    lab_paper = read_json_stable(DATA / "lab" / "leaderboard.json", None)
    lab_bt = read_json_stable(DATA / "lab" / "backtest_leaderboard.json", None)
    lab_ticks = read_json_stable(DATA / "lab" / "tick_leaderboard.json", None)
    ages = sorted(float(t["signal_age_ms"]) for t in trades if t.get("signal_age_ms") is not None)

    return clean(
        {
            "now": now,
            "status": status,
            "log_age_s": round(log_age, 1),
            "halt_reason": ledger.get("halt_reason") or "",
            "mode": ledger.get("mode") or "paper",
            "live_flag": is_live(), "ledger_file": ledger_file().name,
            "settings": settings_env,
            "bankroll": {
                "start": start_bank, "cash": cash, "equity": equity, "realized": round(realized, 2),
                "today": round(fnum(today_row.get("pnl")), 2), "today_n": today_row.get("n", 0),
                "session": round(sum(t["pnl"] for t in session_trades), 2), "session_n": len(session_trades),
                "fees": round(fees, 2), "fee_drag": round(fees / gross_win, 4) if gross_win else None,
                "peak_realized": round(peak, 2), "current_dd": current_dd, "max_dd": round(max_dd, 2),
            },
            "stats": {
                "n": len(trades), "wins": len(wins), "losses": len(losses),
                "win_rate": round(len(wins) / len(trades), 4) if trades else None,
                "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
                "avg_win": round(gross_win / len(wins), 2) if wins else None,
                "avg_loss": round(gross_loss / len(losses), 2) if losses else None,
                "expectancy": round(mean_pnl, 3), "sd": round(sd_pnl, 3),
                "roi_on_cost": round(realized / total_cost, 4) if total_cost else None,
                "avg_ret_per_dollar": round(statistics.fmean(rets), 4) if rets else None,
                "t_stat": round(tstat, 3) if tstat is not None else None,
                "t_stat_ret": round(tstat_ret, 3) if tstat_ret is not None else None,
                "ci90_mean_pnl": [round(ci[0], 3), round(ci[1], 3)] if ci else None,
                "sharpe_per_trade": round(mean_pnl / sd_pnl, 4) if sd_pnl else None,
                "avg_stake": round(statistics.fmean(t["stake"] for t in trades), 2) if trades else None,
                "avg_claimed_edge": round(statistics.fmean(float(t["edge"]) for t in trades if t.get("edge") is not None), 4) if any(t.get("edge") is not None for t in trades) else None,
                "brier_model": brier(cal_model), "brier_market": brier(cal_market), "brier_n": len(cal_model),
                "mean_fair": round(statistics.fmean(p for p, _ in cal_model), 4) if cal_model else None,
                "mean_market_p": round(statistics.fmean(p for p, _ in cal_market), 4) if cal_market else None,
                "realized_rate": round(statistics.fmean(o for _, o in cal_model), 4) if cal_model else None,
                "brier_model_all": brier(cand_pairs_model), "brier_market_all": brier(cand_pairs_market), "cand_n": len(cand_pairs_model),
                "signal_age_median_ms": ages[len(ages) // 2] if ages else None,
                "signal_age_p95_ms": ages[max(0, int(len(ages) * 0.95) - 1)] if ages else None,
                "trades_per_day": round(trades_per_day, 1), "days_active": days_active,
            },
            "gate": gate,
            "risk": {
                "baseline": ledger.get("risk_baseline_cash"), "peak": ledger.get("realized_peak_cash"),
                "locked_floor": ledger.get("locked_equity_floor"), "cash_floor": ledger.get("cash_floor"),
                "loss_streak": ledger.get("crypto_loss_streak"), "session_started_at": session_start,
                "halted_at": ledger.get("halted_at"), "max_session_drawdown": settings_env.get("MAX_SESSION_DRAWDOWN"),
            },
            "curve": curve, "drawdown": dd_series, "daily": daily_rows, "rolling": rolling,
            "trades": trades[::-1],
            "breakdowns": {
                "band": bands, "hour": hours, "weekday": weekdays, "seconds_left": left_buckets,
                "z": z_buckets, "edge": edge_buckets, "signal_age": age_buckets, "side": sides, "strategy": strategies,
                "source": sources, "disagreement": disagreement,
            },
            "calibration": {
                "model": reliability(cal_model), "market": reliability(cal_market),
                "model_all": reliability(cand_pairs_model), "market_all": reliability(cand_pairs_market),
            },
            "vetoes": {
                "counts": veto_counts.most_common(), "counts_24h": veto_counts_24h.most_common(),
                "counterfactual": cf_rows, "counterfactual_unique": cf_unique_list,
                "recent": veto_rows[-12:][::-1],
                "resolver": dict(RESOLVER.stats),
            },
            "lessons": lessons,
            "monte_carlo": mc,
            "lab": {"paper": lab_paper, "backtest": lab_bt, "ticks": lab_ticks},
            "health": {
                "last_cycle_age_s": round(now - last_cycle_ts, 1) if last_cycle_ts else None,
                "cycle_gap_median_s": round(statistics.median(gaps), 1) if gaps else None,
                "cycle_gap_p95_s": round(percentile(gaps, 0.95), 1) if gaps else None,
                "cycles_30m": len(recent_cycles), "posts_24h": posts_24h,
                "stream_reconnects_recent": reconnects, "err_tail": err_lines,
                "journal_events": len(events), "launchd": launchd_status(),
                "features_rows": INDICATORS.rows_written, "indicator_errors": dict(INDICATORS.errors),
                "audit": ENGINE.audit_summary(),
            },
            "funnel": parse_funnel(log_lines),
            "log_tail": [ln.rstrip() for ln in log_lines[-40:]],
            "feed": [e for e in events if e.get("event") in {"submit", "veto", "tape", "execution_reject"}][-40:][::-1],
        }
    )


def build_live() -> dict[str, Any]:
    now = time.time()
    with ENGINE.lock:
        snap = dict(ENGINE.snapshot)
        series = list(ENGINE.series)[-720:]
    with INDICATORS.lock:
        ind = dict(INDICATORS.latest)
    ledger = read_json_stable(ledger_file(), {})
    positions = []
    from scout import streams  # cheap: module already imported by the engine

    for p in ledger.get("positions") or []:
        m = re.search(r"updown-(\d+)m-(\d{10})", str(p.get("slug") or p.get("event_slug") or ""))
        end = int(m.group(2)) + int(m.group(1)) * 60 if m else None
        token = p.get("yes_token") if p.get("side") == "YES" else p.get("no_token")
        bk = streams.book(str(token or "")) if token else None
        mark = bk["bid"] if bk and bk.get("bid") else fnum(p.get("entry_price"))
        cost = fnum(p.get("cost_basis"), fnum(p.get("stake")))
        positions.append(
            {
                "question": p.get("question"), "side": p.get("side"), "entry_price": p.get("entry_price"),
                "stake": p.get("stake"), "shares": p.get("shares"), "cost": cost, "opened_at": p.get("opened_at"),
                "expires_in_s": max(0, int(end - now)) if end else None, "window_end": end,
                "mark": mark, "unrealized": round(fnum(p.get("shares")) * mark - cost, 2),
                "payout_if_win": round(fnum(p.get("shares")) - cost, 2),
                "reason": p.get("reason"),
            }
        )
    log_path = DATA / ("live.log" if is_live() else "night.log")
    log_age = now - log_path.stat().st_mtime if log_path.exists() else 1e9
    halted = bool(ledger.get("halted"))
    status = "halted" if halted else "running" if log_age < 90 else "down"
    # cooldown: last losing BTC close within 900s blocks new entries
    cooldown_until = None
    for f in reversed(ledger.get("fills") or []):
        if str(f.get("side") or "").startswith("CLOSE_"):
            if fnum(f.get("pnl")) < 0:
                cooldown_until = parse_ts(f.get("ts")) + 900
            break
    return clean(
        {
            "now": now, "status": status, "log_age_s": round(log_age, 1), "halt_reason": ledger.get("halt_reason") or "",
            "live_flag": is_live(), "ledger_file": ledger_file().name, "default_view": default_view(),
            "cash": fnum(ledger.get("cash")), "equity": fnum(ledger.get("last_equity"), fnum(ledger.get("cash"))),
            "positions": positions, "cooldown_until": cooldown_until if cooldown_until and cooldown_until > now else None,
            "loss_streak": ledger.get("crypto_loss_streak"),
            "live": snap, "series": series, "indicators": ind,
            "funnel": parse_funnel(tail_lines(log_path, 120)),
            "log_tail": [ln.rstrip() for ln in tail_lines(log_path, 12)],
        }
    )


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "scout-dashboard/2.0"

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        set_view(params.get("ledger"))
        try:
            if path == "/api/state":
                self._send(json.dumps(build_state()).encode(), "application/json")
            elif path == "/api/live":
                self._send(json.dumps(build_live()).encode(), "application/json")
            elif path == "/api/health":
                self._send(json.dumps(clean({"launchd": launchd_status(), "engine_ok": ENGINE.snapshot.get("ok"), "indicators": INDICATORS.errors})).encode(), "application/json")
            elif path in {"/", "/index.html"}:
                self._send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(b"not found", "text/plain", 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send(json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json", 500)

    def log_message(self, *args):  # quiet
        pass


def main() -> None:
    ENGINE.start()
    INDICATORS.start()
    RESOLVER.start()
    print(f"scout dashboard v2: http://localhost:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
