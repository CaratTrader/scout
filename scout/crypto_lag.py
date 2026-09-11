from __future__ import annotations

import json
import math
import os
import re
import asyncio
import threading
import time
import urllib.request
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any, Iterator

from .config import Settings
from .math_risk import taker_fee_per_share

# Every asset Polymarket lists as a TWAP-settled 5m/15m Up-or-Down market and both
# Chainlink RTDS feeds carry (verified 2026-09-10: btc, eth, sol, xrp, doge, bnb, hype).
SUPPORTED_ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
# Window lengths Polymarket lists for these assets: 5m, 15m and 4h (slug tags "5m", "15m", "4h").
SUPPORTED_WINDOWS_S = (300, 900, 14400)
_WINDOW_TAG = {300: "5m", 900: "15m", 14400: "4h"}
_SLUG = re.compile(
    r"(?P<asset>" + "|".join(SUPPORTED_ASSETS) + r")-updown-(?:(?P<mins>5|15)m|(?P<hours>4)h)-(?P<start>\d{10})",
    re.I,
)


def stream_assets() -> list[str]:
    """Assets the oracle streams subscribe to (CRYPTO_STREAM_ASSETS, default: all supported)."""
    raw = (os.getenv("CRYPTO_STREAM_ASSETS") or ",".join(SUPPORTED_ASSETS)).lower().replace(" ", "")
    out = [a for a in raw.split(",") if a in SUPPORTED_ASSETS]
    return out or list(SUPPORTED_ASSETS)
_QUESTION = re.compile(
    r"(?P<name>bitcoin|ethereum|solana|btc|eth|sol).{0,48}?"
    r"(?P<mon>january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+(?P<day>\d{1,2}),\s*"
    r"(?P<t1>\d{1,2}:\d{2}\s*[ap]m)\s*-\s*(?P<t2>\d{1,2}:\d{2}\s*[ap]m)\s*et",
    re.I,
)
_SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT", "doge": "DOGEUSDT", "bnb": "BNBUSDT", "hype": "HYPEUSDT"}
_NAME = {
    "bitcoin": "btc", "ethereum": "eth", "solana": "sol", "btc": "btc", "eth": "eth", "sol": "sol",
    "xrp": "xrp", "ripple": "xrp", "dogecoin": "doge", "doge": "doge", "bnb": "bnb", "hyperliquid": "hype", "hype": "hype",
}
_UA = "scout-agent/0.2"
_BINANCE_HOSTS = ("https://api.binance.com", "https://api.binance.us")
_BINANCE_HOST: str | None = None
_CHAINLINK_LOCK = threading.Lock()
VERBOSE = True  # agent.cycle lowers this on quiet cycles when QUIET_CYCLES is set
_CHAINLINK_VALUES: dict[str, deque[tuple[int, Decimal]]] = defaultdict(
    lambda: deque(maxlen=20000)  # ~5.5 h at one value per second: covers a 4h window's open
)
_CHAINLINK_THREAD: threading.Thread | None = None


def start_chainlink_stream() -> None:
    """Start the credential-free Polymarket RTDS Chainlink 60s TWAP stream once."""
    global _CHAINLINK_THREAD
    if _CHAINLINK_THREAD and _CHAINLINK_THREAD.is_alive():
        return
    _CHAINLINK_THREAD = threading.Thread(
        target=lambda: asyncio.run(_chainlink_stream_forever()),
        name="chainlink-twap",
        daemon=True,
    )
    _CHAINLINK_THREAD.start()


async def _chainlink_stream_forever() -> None:
    from polymarket import AsyncPublicClient
    from polymarket.streams import CryptoPricesChainlinkTwapSpec

    while True:
        try:
            async with AsyncPublicClient() as client:
                spec = CryptoPricesChainlinkTwapSpec(
                    window_seconds=60,
                    symbols=[f"{a}/usd" for a in stream_assets()],
                )
                async with await client.subscribe(spec) as stream:
                    async for event in stream:
                        payload = event.payload
                        asset = str(payload.symbol).split("/", 1)[0].lower()
                        stamp = int(payload.timestamp) // 1000
                        with _CHAINLINK_LOCK:
                            _CHAINLINK_VALUES[asset].append((stamp, Decimal(payload.value)))
        except Exception as exc:
            print("Chainlink RTDS reconnect:", type(exc).__name__, str(exc)[:120])
            await asyncio.sleep(1)


def chainlink_window_context(
    asset: str,
    start: int,
    now: int,
    *,
    max_age: int = 8,
    start_tolerance: int = 8,
) -> tuple[float, float] | None:
    """Exact 60s TWAP at the market boundary and latest fresh observation."""
    with _CHAINLINK_LOCK:
        rows = list(_CHAINLINK_VALUES.get(asset) or ())
    if not rows:
        return None
    latest_stamp, latest = rows[-1]
    if now - latest_stamp > max_age:
        return None
    boundary = min(rows, key=lambda row: abs(row[0] - start))
    if abs(boundary[0] - start) > start_tolerance:
        return None
    return float(boundary[1]), float(latest)


def is_crypto_updown(market: dict[str, Any]) -> bool:
    blob = f"{market.get('slug') or ''} {market.get('question') or ''}".lower()
    return "updown" in blob.replace("-", "") or "up or down" in blob


def parse_window(market: dict[str, Any]) -> dict[str, Any] | None:
    blob = f"{market.get('slug') or ''} {market.get('event_slug') or ''}"
    match = _SLUG.search(blob)
    if match:
        start = int(match.group("start"))
        mins = int(match.group("mins")) if match.group("mins") else int(match.group("hours")) * 60
        return {
            "asset": match.group("asset").lower(),
            "window_s": mins * 60,
            "start": start,
            "end": start + mins * 60,
        }
    return parse_window_from_question(str(market.get("question") or ""), str(market.get("opened_at") or ""))


def parse_window_from_question(question: str, opened_at: str = "") -> dict[str, Any] | None:
    match = _QUESTION.search(question)
    if not match:
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo

    year = 2026
    if opened_at:
        try:
            year = int(opened_at[:4])
        except ValueError:
            pass
    t1 = match.group("t1").upper().replace(" ", "")
    t2 = match.group("t2").upper().replace(" ", "")
    mon = match.group("mon")[:3].title()
    day = int(match.group("day"))
    et = ZoneInfo("America/New_York")
    try:
        start_dt = datetime.strptime(f"{mon} {day} {year} {t1}", "%b %d %Y %I:%M%p").replace(tzinfo=et)
        end_dt = datetime.strptime(f"{mon} {day} {year} {t2}", "%b %d %Y %I:%M%p").replace(tzinfo=et)
    except ValueError:
        return None
    if end_dt <= start_dt:
        end_dt = end_dt.replace(day=end_dt.day)  # same day; if wraps midnight, add a day
        from datetime import timedelta

        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)
    asset = _NAME[match.group("name").lower()]
    return {
        "asset": asset,
        "window_s": int((end_dt - start_dt).total_seconds()),
        "start": int(start_dt.timestamp()),
        "end": int(end_dt.timestamp()),
    }


def crypto_settle_price(
    pos: dict[str, Any],
    *,
    now: int | None = None,
    user_agent: str = _UA,
    prefer_gamma: bool = True,
    allow_proxy: bool = False,
) -> float | None:
    """Payout per share. Live uses Polymarket close prices (Chainlink), not Binance."""
    win = parse_window(pos)
    now = now if now is not None else int(time.time())
    if win and now < win["end"] + 5:
        return None
    if prefer_gamma:
        from .gamma import resolved_outcome_prices

        slug = str(pos.get("event_slug") or pos.get("slug") or "")
        resolved = resolved_outcome_prices(slug, user_agent)
        if resolved is not None:
            yes_px, no_px = resolved
            if pos.get("side") == "YES":
                return yes_px
            if pos.get("side") == "NO":
                return no_px
            return None
        if win and now < win["end"] + 90:
            return None
        if not allow_proxy:
            return None
    if not win:
        return None
    symbol = _SYMBOL[win["asset"]]
    open_px = _binance_open(symbol, win["start"])
    close_px = _binance_open(symbol, max(win["start"], win["end"] - 60))
    if open_px <= 0 or close_px <= 0:
        return None
    up = close_px >= open_px
    if pos.get("side") == "YES":
        return 1.0 if up else 0.0
    if pos.get("side") == "NO":
        return 0.0 if up else 1.0
    return None


def crypto_side_ok(ask: float, fair: float) -> bool:
    """EV gate, not a price band. The edge requirement (crypto_min_edge) plus
    fractional-Kelly sizing already control risk; a hard [0.50, 0.90] band with a
    fair>=0.70 clause was rejecting genuinely +EV entries (e.g. ask=0.49 fair=0.60
    edge=0.088 died one cent under the floor, ask=0.40 fair=0.52 edge=0.101 died on
    the fair clause). Only refuse degenerate books: cheap longshots and near-locks
    where a single tick against us is -100%.

    Floor raised 0.05 -> CRYPTO_MIN_ASK (default 0.25) on 2026-08-28 backtest
    evidence: 30d replay, ask 0.05-0.15 won 7.5% (-35%/$ staked, n=326) and
    0.15-0.25 flipped negative at realistic spreads; live paper went 0-for-3
    under 0.15. The tail model overestimates reversals the book already priced."""
    floor = float(os.getenv("CRYPTO_MIN_ASK") or 0.25)
    if not (floor <= ask <= 0.92):
        return False
    if fair < ask - 0.02:
        return False
    # Trust cap: if the book prices this side >20c below our model, the market
    # is telling us something our oracle snapshot cannot see (Chainlink lag,
    # venue basis, resolution nuance). Never believe an underdog edge that big.
    return fair - ask <= 0.20

def _entry_filters() -> tuple[frozenset[str], frozenset[int]]:
    """Optional .env narrowing of NEW entries; exits never pass through here."""
    assets = frozenset(
        a for a in (os.getenv("CRYPTO_ASSETS") or "btc,eth,sol").lower().replace(" ", "").split(",") if a
    )
    windows = frozenset(
        int(m) * 60
        for m in (os.getenv("CRYPTO_WINDOWS_MIN") or "5,15").replace(" ", "").split(",")
        if m
    )
    return assets, windows


def is_short_updown_window(win: dict[str, Any] | None) -> bool:
    if not win or int(win.get("window_s") or 0) not in SUPPORTED_WINDOWS_S:
        return False
    assets, windows = _entry_filters()
    return str(win.get("asset") or "").lower() in assets and int(win["window_s"]) in windows


def _spot_source() -> str:
    """CRYPTO_SPOT_SOURCE=twap (default, unchanged behaviour): score the move with the
    latest 60s TWAP value as spot. =raw: use the latest raw Chainlink tick when it is
    fresh (<=3s). The 60s TWAP trails the raw price by ~30s of drift, so a fresh move
    is invisible to the TWAP while the book already prices it — which is exactly when
    the model "sees" a cheap reversal. The open reference stays the TWAP at window
    start (the resolution rule). Live record 2026-08-28..09-04: TWAP-sourced entries
    33% win / -0.10 per $, fresh-spot (Binance fallback) entries 66% / +0.55 per $."""
    return (os.getenv("CRYPTO_SPOT_SOURCE") or "twap").strip().lower()


def in_entry_window(seconds_left: float, window_s: int, *, now: int, start: int) -> bool:
    """Enter after the open coin-flip, before the book is a 95¢ lock."""
    if now < start - 5:
        return False
    if window_s <= 300:
        return 50 <= seconds_left <= 250
    return 90 <= seconds_left <= 720


_CALIBRATION: dict[str, Any] | None = None
_CALIBRATION_LOADED = False


def _calibration_table() -> dict[str, Any] | None:
    """Lazy-load scout/calibration.json (fit by backtest/calibrate.py)."""
    global _CALIBRATION, _CALIBRATION_LOADED
    if not _CALIBRATION_LOADED:
        _CALIBRATION_LOADED = True
        try:
            path = __import__("pathlib").Path(__file__).resolve().parent / "calibration.json"
            _CALIBRATION = json.loads(path.read_text())
        except Exception:
            _CALIBRATION = None
    return _CALIBRATION


def p_up_from_move(
    chg: float,
    seconds_left: float,
    window_s: int,
    *,
    sigma_full: float | None = None,
    oracle_noise: float = 0.00015,
    calibrated: bool = False,
) -> float:
    """P(finish up), retaining uncertainty for Binance-vs-Chainlink basis and TWAP lag.

    calibrated=True shrinks the Gaussian toward the measured win frequency for
    this (z, seconds_left) bucket over 30 days of windows — the Gaussian tails
    provably give longshots about twice their true odds (e.g. z -2..-1.4 @60s:
    modeled 4.5%, measured 2.1%, n=566)."""
    sigma_full = sigma_full or (0.0024 if window_s <= 300 else 0.0040)
    t = max(seconds_left / window_s, 0.02)
    sigma = math.sqrt((sigma_full * math.sqrt(t)) ** 2 + oracle_noise**2)
    z = chg / (sigma + 1e-12)
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    p = min(0.985, max(0.015, p))
    if not calibrated or window_s > 300:
        return p
    doc = _calibration_table()
    if not doc:
        return p
    time_key = str(min(doc["time_keys"], key=lambda k: abs(k - seconds_left)))
    edges = doc["z_edges"]
    b = next((i for i, edge in enumerate(edges) if z < edge), len(edges))
    emp = doc["table"].get(time_key, [None])[b] if b < len(doc["table"].get(time_key, [])) else None
    if emp is None:
        return p
    n = doc["counts"][time_key][b]
    m = float(doc.get("pseudo_count") or 60)
    blended = (n * emp + m * p) / (n + m)
    return min(0.985, max(0.015, blended))


def realized_window_sigma(closes: list[float], window_s: int) -> float:
    """Estimate full-window volatility from recent one-minute log returns."""
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(returns) < 5:
        return 0.0024 if window_s <= 300 else 0.0040
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    estimate = math.sqrt(variance) * math.sqrt(window_s / 60.0)
    floor = 0.0008 if window_s <= 300 else 0.0014
    ceiling = 0.008 if window_s <= 300 else 0.014
    return min(ceiling, max(floor, estimate))


def crypto_event_slugs(now: int) -> list[str]:
    """Event slugs for the previous, current and next window of every enabled asset
    (CRYPTO_ASSETS) and window length (CRYPTO_WINDOWS_MIN)."""
    assets, windows = _entry_filters()
    prefixes = tuple(
        f"{asset}-updown-{_WINDOW_TAG[window_s]}-"
        for asset in SUPPORTED_ASSETS
        if asset in assets
        for window_s in SUPPORTED_WINDOWS_S
        if window_s in windows
    )
    out: list[str] = []
    seen: set[str] = set()
    for window in SUPPORTED_WINDOWS_S:
        tag = f"-{_WINDOW_TAG[window]}-"
        start = now - (now % window)
        for prefix in prefixes:
            if tag not in prefix:
                continue
            for ts in (start - window, start, start + window):
                slug = f"{prefix}{ts}"
                if slug in seen:
                    continue
                seen.add(slug)
                out.append(slug)
    return out

_EVENT_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _event_cache_ttl() -> float:
    try:
        return float(os.getenv("CRYPTO_EVENT_CACHE_SECONDS") or 0)
    except ValueError:
        return 0.0


def iter_crypto_events(settings: Settings, now: int | None = None) -> Iterator[dict[str, Any]]:
    now = now or int(time.time())
    seen: set[str] = set()
    ttl = _event_cache_ttl()
    for slug in crypto_event_slugs(now):
        if slug in seen:
            continue
        seen.add(slug)
        cached = _EVENT_CACHE.get(slug) if ttl > 0 else None
        if cached and time.time() - cached[0] <= ttl:
            rows: Any = cached[1]
        else:
            try:
                rows = _get_json(
                    f"https://gamma-api.polymarket.com/events?slug={slug}",
                    settings.user_agent,
                )
            except Exception:
                continue
            if ttl > 0 and isinstance(rows, list):
                _EVENT_CACHE[slug] = (time.time(), rows)
        if isinstance(rows, list):
            for event in rows:
                yield event


def _try_twap_lock(
    market: dict[str, Any],
    win: dict[str, Any],
    settings: Settings,
    now: int,
    sigmas: dict[tuple[str, int], float],
    lock_signal: Any,
    min_p_lock: float,
) -> dict[str, Any] | None:
    """Score an endgame TWAP lock candidate, or None."""
    rules = f"{market.get('description') or ''} {market.get('resolution_source') or ''}".lower()
    if "twap" not in rules and "60s-stream" not in rules:
        return None
    asset = win["asset"]
    exact = chainlink_window_context(asset, win["start"], now)
    if exact is None:
        return None
    open_twap = exact[0]
    sigma_key = (asset, win["window_s"])
    if sigma_key not in sigmas:
        oracle_sigma = _oracle_minute_sigma(asset, win["window_s"], float(now))
        if oracle_sigma is not None:
            sigmas[sigma_key] = oracle_sigma
        else:
            try:
                sigmas[sigma_key] = realized_window_sigma(
                    _binance_recent_closes(_SYMBOL[asset]), win["window_s"]
                )
            except Exception:
                sigmas[sigma_key] = 0.0024 if win["window_s"] <= 300 else 0.0040
    sig = lock_signal(asset, win, open_twap, sigmas[sigma_key], now=float(now))
    if sig is None:
        return None
    p_up = float(sig["p_up"])
    if not (p_up >= min_p_lock or p_up <= 1.0 - min_p_lock):
        return None
    return {
        "p_yes": p_up,
        "raw_model_p_yes": p_up,
        "market_p_yes": None,
        "model_weight": 1.0,
        "confidence": 0.95,
        "already_priced": False,
        "edge_type": "twap_lock",
        "asset": asset,
        "signal_ts": float(now),
        "window_start": int(win["start"]),
        "window_end": int(win["end"]),
        "seconds_left_at_signal": float(sig["seconds_left"]),
        "oracle_open": open_twap,
        "oracle_spot": sig["spot"],
        "thesis": (
            f"{asset.upper()} TWAP lock: {sig['seconds_left']:.0f}s left, flip needs "
            f"{sig['required_move_bps']:+.1f}bps sustained (z={sig['z']:+.1f}) "
            f"p_up={p_up:.3f}"
        ),
        "sources": ["chainlink"],
    }


def _lag_model_enabled() -> bool:
    """CRYPTO_LAG_MODEL=off leaves only the TWAP lock (and the tape) trading."""
    return (os.getenv("CRYPTO_LAG_MODEL") or "on").strip().lower() not in {"0", "off", "false", "no"}


def _oracle_minute_sigma(asset: str, window_s: int, now: float) -> float | None:
    """Full-window sigma from the oracle's own one-minute closes. This is the
    arena's definition, so live lock z-scores match the tick-replay study."""
    from . import streams

    closes: dict[int, float] = {}
    for ts, px in streams.oracle_ticks(asset, now - 32 * 60):
        closes[int(ts) // 60 * 60] = float(px)
    rows = [px for _, px in sorted(closes.items())[-31:-1]]
    if len(rows) < 6:
        return None
    return realized_window_sigma(rows, window_s)


def _basis_min_bps() -> float:
    try:
        return float(os.getenv("CRYPTO_BASIS_MIN_BPS") or 0)
    except ValueError:
        return 0.0


def score_crypto_windows(markets: list[dict[str, Any]], settings: Settings) -> dict[str, dict[str, Any]]:
    from . import streams
    from .twap_lock import MIN_P_LOCK, lock_signal

    start_chainlink_stream()
    streams.start_streams()
    if _basis_min_bps() > 0:
        from . import coinbase

        coinbase.start()
    out: dict[str, dict[str, Any]] = {}
    now = int(time.time())
    spots: dict[str, float] = {}
    sigmas: dict[tuple[str, int], float] = {}
    opens: dict[tuple[str, int], float] = {}
    skip5 = {"stale": 0, "time": 0, "oracle": 0, "cheap": 0, "thin": 0, "priced": 0}
    skip15 = {"stale": 0, "time": 0, "oracle": 0, "cheap": 0, "thin": 0, "priced": 0}
    near: tuple[str, float, float, float] | None = None
    saw5 = False
    saw15 = False

    def bucket_for(window_s: int) -> dict[str, int]:
        return skip5 if window_s <= 300 else skip15

    for market in markets:
        win = parse_window(market)
        if not is_short_updown_window(win) or win is None:
            continue
        if win["window_s"] <= 300:
            saw5 = True
        else:
            saw15 = True
        bucket = bucket_for(win["window_s"])
        if market.get("clob_fresh") is False:
            bucket["stale"] += 1
            continue
        seconds_left = win["end"] - now
        if not in_entry_window(seconds_left, win["window_s"], now=now, start=win["start"]):
            # Endgame: past the lag window, but inside the settlement TWAP a
            # partly-locked average can make the outcome near-certain while the
            # book still prices doubt.
            lock = _try_twap_lock(market, win, settings, now, sigmas, lock_signal, MIN_P_LOCK)
            if lock:
                out[market["id"]] = lock
            else:
                bucket["time"] += 1
            continue
        if not _lag_model_enabled():
            bucket["time"] += 1
            continue
        asset = win["asset"]
        sigma_key = (asset, win["window_s"])
        key = (asset, win["start"])
        rules = f"{market.get('description') or ''} {market.get('resolution_source') or ''}".lower()
        uses_twap = "twap" in rules or "60s-stream" in rules
        if asset not in spots:
            ws = streams.exchange_spot(asset)
            if ws is not None:
                spots[asset] = ws[0]
            else:
                try:
                    spots[asset] = _binance_spot(_SYMBOL[asset])
                except Exception:
                    spots[asset] = 0.0
        open_px = 0.0
        spot = 0.0
        source = "binance"
        exchange_spot = spots.get(asset, 0.0)
        if uses_twap:
            exact = chainlink_window_context(asset, win["start"], now)
            if exact is not None:
                open_px, cl_spot = exact
                # The contract resolves from Chainlink. Cross-venue basis can be
                # predictive, but treating Binance spot as the current Chainlink
                # value fabricated confidence and was not calibrated.
                spot, source = cl_spot, "chainlink"
                if _spot_source() == "raw":
                    raw = streams.oracle_spot(asset, max_age=3.0)
                    if raw is not None and float(raw[0]) > 0:
                        spot, source = float(raw[0]), "chainlink_raw"
        if spot <= 0 or open_px <= 0:
            try:
                if key not in opens:
                    opens[key] = _binance_open(_SYMBOL[asset], win["start"])
                if open_px <= 0:
                    open_px = opens[key]
                if spot <= 0:
                    spot = spots[asset]
                source = "binance"
            except Exception:
                bucket["oracle"] += 1
                continue
        if spot <= 0 or open_px <= 0:
            bucket["oracle"] += 1
            continue
        if sigma_key not in sigmas:
            try:
                sigmas[sigma_key] = realized_window_sigma(
                    _binance_recent_closes(_SYMBOL[asset]), win["window_s"]
                )
            except Exception:
                sigmas[sigma_key] = 0.0024 if win["window_s"] <= 300 else 0.0040
        chg = (spot - open_px) / open_px
        sigma = sigmas[sigma_key]
        noise = 0.00008 if source in {"chainlink", "chainlink_raw", "binance_vs_cl"} else 0.00015
        use_cal = (os.getenv("CALIBRATION") or "1").strip().lower() not in {"0", "false", "off"}
        raw_model_p = p_up_from_move(
            chg, seconds_left, win["window_s"], sigma_full=sigma, oracle_noise=noise,
            calibrated=use_cal,
        )
        yes_ask = float(market["yes_ask"])
        no_ask = float(market["no_ask"])
        yes_bid = float(market.get("yes_bid") or max(0.0, 1.0 - no_ask))
        market_p = min(0.99, max(0.01, (yes_ask + yes_bid) / 2.0))
        model_weight = min(1.0, max(0.0, settings.crypto_model_weight))
        p_up = model_weight * raw_model_p + (1.0 - model_weight) * market_p
        fee_yes = taker_fee_per_share(yes_ask, settings.taker_fee_rate)
        fee_no = taker_fee_per_share(no_ask, settings.taker_fee_rate)
        up_edge = p_up - yes_ask - fee_yes
        down_edge = (1.0 - p_up) - no_ask - fee_no
        yes_fav = crypto_side_ok(yes_ask, p_up)
        no_fav = crypto_side_ok(no_ask, 1.0 - p_up)
        if not yes_fav and not no_fav:
            fav_yes = p_up >= 0.80
            fav_no = (1.0 - p_up) >= 0.80
            if (fav_yes and yes_ask > 0.90) or (fav_no and no_ask > 0.90):
                bucket["priced"] += 1
            else:
                bucket["cheap"] += 1
            best_edge = up_edge if p_up >= 0.5 else down_edge
            best_ask = yes_ask if p_up >= 0.5 else no_ask
            best_fair = p_up if p_up >= 0.5 else 1.0 - p_up
            label = f"{asset.upper()} {'YES' if p_up >= 0.5 else 'NO'}"
            if near is None or best_edge > near[3]:
                near = (label, best_ask, best_fair, best_edge)
            continue
        yes_ok = yes_fav and up_edge >= settings.crypto_min_edge
        no_ok = no_fav and down_edge >= settings.crypto_min_edge
        best_edge = up_edge if yes_fav else down_edge
        best_ask = yes_ask if yes_fav else no_ask
        best_fair = p_up if yes_fav else 1.0 - p_up
        if near is None or best_edge > near[3]:
            near = (
                f"{asset.upper()} {'YES' if yes_fav else 'NO'}",
                best_ask,
                best_fair,
                best_edge,
            )
        if not yes_ok and not no_ok:
            bucket["thin"] += 1
            continue
        z = chg / (sigma + 1e-12)
        conf = 0.70
        if abs(z) >= 2.0 or seconds_left <= 90:
            conf = 0.90
        elif abs(z) >= 1.4:
            conf = 0.82
        coinbase_basis = None
        if _basis_min_bps() > 0:
            from . import coinbase

            raw_row = streams.oracle_spot(asset, max_age=3.0)
            coinbase_basis = coinbase.basis_bps(asset, raw_row[0] if raw_row else spot)
        out[market["id"]] = {
            "coinbase_basis_bps": coinbase_basis,
            "p_yes": p_up,
            "raw_model_p_yes": raw_model_p,
            "market_p_yes": market_p,
            "model_weight": model_weight,
            "confidence": conf,
            "already_priced": False,
            "edge_type": "crypto_lag",
            "asset": asset,
            "signal_ts": float(now),
            "window_start": int(win["start"]),
            "window_end": int(win["end"]),
            "seconds_left_at_signal": float(seconds_left),
            "oracle_open": open_px,
            "oracle_spot": spot,
            "exchange_spot": exchange_spot,
            "exchange_basis_bps": (
                round((exchange_spot - spot) / spot * 10_000, 3)
                if exchange_spot > 0 and spot > 0
                else None
            ),
            "thesis": (
                f"{asset.upper()} {win['window_s'] // 60}m {source} {spot:.2f} vs open {open_px:.2f} "
                f"chg={chg * 100:.3f}% left={seconds_left}s sigma={sigma * 100:.3f}% "
                f"p_up={p_up:.3f} raw={raw_model_p:.3f} mkt={market_p:.3f}"
            ),
            "sources": [source],
        }
    if saw5 and VERBOSE:
        print(
            f"  5m skip stale={skip5['stale']} wait={skip5['time']} oracle={skip5['oracle']} "
            f"underdog={skip5['cheap']} priced={skip5['priced']} thin_edge={skip5['thin']}"
        )
    if saw15 and VERBOSE:
        print(
            f"  15m skip stale={skip15['stale']} wait={skip15['time']} oracle={skip15['oracle']} "
            f"underdog={skip15['cheap']} priced={skip15['priced']} thin_edge={skip15['thin']}"
        )
    if near and not out and VERBOSE:
        label, ask, fair, edge = near
        print(f"  closest {label} ask={ask:.2f} fair={fair:.2f} edge={edge:.3f}")
    return out


def _binance_spot(symbol: str) -> float:
    data = _binance_json(f"/api/v3/ticker/price?symbol={symbol}", timeout=8)
    return float(data.get("price") or 0)


def _binance_open(symbol: str, start: int) -> float:
    rows = _binance_json(
        f"/api/v3/klines?symbol={symbol}&interval=1m&startTime={start * 1000}&limit=1",
        timeout=8,
    )
    if not rows:
        return 0.0
    return float(rows[0][1])


def _binance_recent_closes(symbol: str, limit: int = 30) -> list[float]:
    rows = _binance_json(
        f"/api/v3/klines?symbol={symbol}&interval=1m&limit={limit}",
        timeout=8,
    )
    return [float(row[4]) for row in rows if len(row) > 4]


def _binance_json(path: str, timeout: int = 8) -> Any:
    global _BINANCE_HOST
    last: Exception | None = None
    hosts = []
    if _BINANCE_HOST:
        hosts.append(_BINANCE_HOST)
    hosts.extend(host for host in _BINANCE_HOSTS if host != _BINANCE_HOST)
    for host in hosts:
        try:
            data = _get_json(f"{host}{path}", _UA, timeout=timeout)
            _BINANCE_HOST = host
            return data
        except Exception as exc:
            last = exc
            continue
    raise last or RuntimeError(f"binance failed {path}")

def _get_json(url: str, user_agent: str, timeout: int = 12) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
