"""Plug-in interface of the Kalshi strategy lab paper trader (scout/kalshi_lab_paper.py, docs/KALSHI_LAB.md).

A plug-in is a module with one Strategy subclass (or `STRATEGY = <class>`), named by its registry entry:
  "my_rule": {"module": "my_rule", "status": "paper", "frozen": "...", "params": {"q_hat": 0.8, ...}, "poll_s": 60}
A bare name is looked up in scout.kalshi_lab_strategies, then lab.kalshi.strategies; a dotted name is imported as is;
"module:Class" picks one class of a module that defines several.

Every poll_s seconds (registry "poll_s" > class poll_s > KLAB_POLL_S) the harness:
  1. asks series_now(now) which Kalshi series it needs and fetches each one's open markets ONCE per poll, shared by all
     strategies due in that poll (GET /markets?series_ticker=<series>&status=open&limit=200; an entry containing "="
     is used as the query itself, e.g. "event_ticker=KXBTCD-26OCT0817");
  2. asks external_now(now) for outside data and gets it through the cached fetchers below (one call per fetcher and
     arguments per TTL, shared by all strategies);
  3. calls decide(now, market_quotes, external_data) -> signals;
  4. per signal: fetches the order book (only now), logs the signal at a unit stake (signals.jsonl) and, unless the
     bankroll is halted, opens a paper position at max_price, sized by quarter-Kelly and the book depth;
  5. settles open positions on the Kalshi result after close (or through result() when settle_on_kalshi is False).
Plug-ins never call Kalshi themselves: every Kalshi request goes through the harness, which keeps the temperature bot's
idle-window pacing and the per-minute call cap (Kalshi allows ~1 request/s per IP, shared with com.tradeinc.kalshitemp).

Signals are dicts:
  ticker, side ("YES" / "NO"), max_price (price paid per contract; the book counts depth up to max_price + 0.005), why
  optional: label (name in the log line, default ticker), close (ISO close time, default the quote's close_time),
            q_hat (win-rate estimate for the stake, default params["q_hat"]), size (contracts available: replaces the
            order-book call, e.g. when mirroring another bot's fill), opened (ISO open time, default now),
            log (fields written to signals.jsonl after ticker/side/px, default {"close", "why"})
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

UA = "scout-kalshi-lab"


class Strategy:
    """Base class of a lab strategy. Subclasses set `series` / `poll_s` and implement decide()."""
    series: tuple[str, ...] = ()     # Kalshi series it may need (also the call plan: one open-markets call per poll each)
    poll_s: float | None = None      # seconds between decisions; None = the harness default (KLAB_POLL_S)
    min_stake: float = 0.5           # smaller quarter-Kelly stakes are not filled
    settle_on_kalshi: bool = True    # False: the harness settles open positions through result() instead
    one_signal_per_market: bool = True   # the harness acts on (and logs) one signal per ticker and side, remembered in
    #                                      ledger["signalled"]; strategies that keep their own bookkeeping may turn it off

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        self.params: dict[str, Any] = spec.get("params") or {}
        self.ledger: dict[str, Any] = {}   # this strategy's paper ledger, bound by the harness before every call. Read it
        #                                    freely; write only the bookkeeping keys "decided" / "mirrored" / "state".
        self.notes: list[str] = []         # this poll's notes for the log line (cleared by the harness)

    @property
    def state(self) -> dict[str, Any]:
        """Free-form JSON state persisted in the ledger (saved with it after every poll)."""
        return self.ledger.setdefault("state", {})

    def series_now(self, now: dt.datetime) -> list[str]:
        """Series whose open markets this poll needs (default: all of `series`; return [] to make no Kalshi call)."""
        return list(self.series)

    def external_now(self, now: dt.datetime) -> dict[str, dict[str, Any]]:
        """Outside data this poll needs: {fetcher name: keyword arguments}. "name:label" keys ask one fetcher twice."""
        return {}

    def decide(self, now: dt.datetime, market_quotes: dict[str, dict[str, dict] | None], external_data: dict[str, Any]) -> list[dict[str, Any]]:
        """market_quotes: {series: {ticker: Kalshi market dict} or None when the fetch failed};
        external_data: {key: fetcher result or None when it failed}. Returns signals (see the module docstring)."""
        raise NotImplementedError

    def result(self, pos: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
        """Only when settle_on_kalshi is False: {"won": bool, "settled": ISO time} once the position is decided, else None."""
        return None

    def summary(self) -> str:
        led = self.ledger
        return f"{self.name}: cash ${led['cash']:.2f} open {len(led['positions'])} " + "; ".join(self.notes)


# ------------------------------------------------------------------ external data
FETCHERS: dict[str, tuple[Callable[..., Any], float]] = {}


def fetcher(name: str, ttl: float) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register an external-data fetcher; results are cached for `ttl` seconds per argument set."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        FETCHERS[name] = (fn, ttl)
        return fn
    return deco


def http_json(url: str, timeout: float = 30) -> Any:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read())


class Feeds:
    """Cached external data shared by every strategy in the process. Failures return None and are not cached."""

    def __init__(self, on_error: Callable[[str, Exception], None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.on_error = on_error
        self.clock = clock
        self.calls = 0
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}

    def get(self, key: str, kwargs: dict[str, Any] | None = None) -> Any:
        name = key.split(":", 1)[0]
        kwargs = kwargs or {}
        if name not in FETCHERS:
            if self.on_error:
                self.on_error(name, KeyError(f"unknown fetcher {name!r}"))
            return None
        fn, ttl = FETCHERS[name]
        ck = (name, json.dumps(kwargs, sort_keys=True, default=str))
        hit = self._cache.get(ck); t = self.clock()
        if hit and t - hit[0] < ttl:
            return hit[1]
        try:
            val = fn(**kwargs)
        except Exception as exc:
            if self.on_error:
                self.on_error(name, exc)
            return None
        finally:
            self.calls += 1
        if ttl > 0:
            if len(self._cache) > 256:
                self._cache = {k: v for k, v in self._cache.items() if t - v[0] < 3600}
            self._cache[ck] = (t, val)
        return val


@fetcher("metar", ttl=60)
def metar(stations: list[str], hours: int = 30) -> dict[str, list[dict]]:
    """aviationweather.gov METARs of US stations (3-letter codes, "K" added), {station: [rows]} over the last `hours`."""
    rows = http_json(f"https://aviationweather.gov/api/data/metar?ids={','.join('K' + s for s in stations)}&format=json&hours={hours}", timeout=40)
    out: dict[str, list[dict]] = {}
    for r in rows or []:
        out.setdefault(str(r.get("icaoId", ""))[1:], []).append(r)
    return out


@fetcher("coinbase", ttl=30)
def coinbase_candles(product: str, granularity: int = 60, minutes: int = 300) -> list[list[float]]:
    """Coinbase Exchange public candles of `product` (e.g. "BTC-USD"), [[time, low, high, open, close, volume], ...]
    oldest first, covering the last `minutes` (at most 300 candles per call)."""
    end = int(time.time()) // granularity * granularity
    start = end - min(300, max(1, minutes * 60 // granularity)) * granularity
    iso = lambda s: dt.datetime.fromtimestamp(s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = http_json(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity={granularity}&start={iso(start)}&end={iso(end)}")
    return sorted(rows or [])


@fetcher("espn", ttl=60)
def espn_scoreboard(sport: str, league: str, dates: str | None = None) -> dict[str, Any]:
    """ESPN public scoreboard, e.g. ("baseball", "mlb", "20261008"); default today's."""
    q = f"?dates={dates}&limit=300" if dates else "?limit=300"
    return http_json(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard{q}")


@fetcher("polymarket", ttl=30)
def polymarket(path: str, **query: Any) -> Any:
    """Polymarket Gamma API GET (public), e.g. polymarket("events", slug="btc-updown-15m-1791486000")."""
    return http_json(f"https://gamma-api.polymarket.com/{path.lstrip('/')}" + (f"?{urllib.parse.urlencode(query)}" if query else ""))
