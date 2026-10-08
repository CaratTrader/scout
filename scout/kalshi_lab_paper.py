"""Kalshi strategy lab: paper trading on live data with a simulated $50 bankroll per strategy (docs/KALSHI_LAB.md).

Strategies are plug-ins (interface: scout/kalshi_lab_strategies/base.py) named by their registry entry in
data/kalshi_lab/registry.json (field "module"; entries without one fall back to their family: rain -> rain_n,
weather -> weather_mirror). Parameters are frozen there; only the nightly loop may change a strategy's status.
Every signal is also logged at a unit stake (signals.jsonl), so statistics do not depend on bankroll limits.
Sizing: stake = min(quarter-Kelly with the win rate shrunk halfway to the price, max_stake, cash), contracts capped by
the order-book depth at the price. Halts (bankroll only): 3 losses in a row, $10 daily loss, $15 cumulative loss.
Kalshi calls: one open-markets call per series per poll (shared by all strategies due in it), an order book only when
a signal fires, made only in the temperature bot's idle window (shared ~1 request/s limit), >= 1.2 s apart and at most
KLAB_MAX_CALLS_PER_MIN (25) in any minute. Each strategy polls at its own interval (registry "poll_s", else the
plug-in's, else KLAB_POLL_S); intervals are stretched when the planned open-market calls would use more than
KLAB_PLAN_SHARE (60%) of the cap, leaving the rest for order books and settlement."""
from __future__ import annotations

import collections
import datetime as dt
import importlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from scout.kalshi_lab_strategies.base import Feeds, Strategy
from scout.kalshi_lab_strategies.rain_n import RAIN_STATIONS, TZ, precip_in, rain_state  # noqa: F401  (re-exported)

K = "https://api.elections.kalshi.com/trade-api/v2"
ROOT = Path(os.getenv("KLAB_ROOT") or "data/kalshi_lab")
PAPER = ROOT / "paper"; REG = ROOT / "registry.json"; SIGNALS = ROOT / "signals.jsonl"; JOURNAL = ROOT / "lab_journal.jsonl"
BOT_LOG = Path("data/kalshi_temp.log")
POLL_S = float(os.getenv("KLAB_POLL_S") or 300)
MAX_CALLS_PER_MIN = int(os.getenv("KLAB_MAX_CALLS_PER_MIN") or 25)
PLAN_SHARE = float(os.getenv("KLAB_PLAN_SHARE") or 0.6)
FEE = 0.07
BUILTIN = {"rain": "rain_n", "weather": "weather_mirror"}   # family -> plug-in for registry entries without "module"
DEFAULT_REGISTRY = {
    "rain_n15": {"family": "rain", "module": "rain_n", "status": "paper", "frozen": "2026-10-07", "params": {"hour": 15, "cap": 0.97, "q_hat": 0.81},
                 "source": "lab/kalshi/rain.py discovery 2026-10-07 (partial data): n=205, win 81%, +14%/$, t 1.8"},
    "weather": {"family": "weather", "module": "weather_mirror", "status": "paper", "frozen": "2026-10-06", "params": {"q_hat": 0.89},
                "source": "lab/us/kalshi_backtest.py R0+R2m: 47 trades, 89% win, +19%/$, t($) 2.6"},
}
LIMITS = {"bankroll": 50.0, "max_stake": 5.0, "kelly": 0.25, "halt_streak": 3, "halt_day": 10.0, "halt_total": 15.0}
ACTIVE = ("paper", "gate-pass", "monitor")


def journal(ev: dict[str, Any]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), **ev}) + "\n")


def fee(px: float, n: float) -> float:
    return round(FEE * px * (1 - px) * n, 4)


# ------------------------------------------------------------------ Kalshi (paced, budgeted)
class CallBudget:
    """At most `per_min` requests in any 60-second window (sliding)."""

    def __init__(self, per_min: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_min = per_min
        self.clock = clock
        self.stamps: collections.deque[float] = collections.deque()

    def _trim(self, t: float) -> None:
        while self.stamps and t - self.stamps[0] >= 60:
            self.stamps.popleft()

    def wait_s(self) -> float:
        """Seconds until a request may be made (0 = now)."""
        t = self.clock(); self._trim(t)
        return 0.0 if len(self.stamps) < self.per_min else 60 - (t - self.stamps[0]) + 0.01

    def take(self) -> None:
        self.stamps.append(self.clock())

    def used(self) -> int:
        self._trim(self.clock())
        return len(self.stamps)


_LAST = [0.0]
BUDGET = CallBudget(MAX_CALLS_PER_MIN)
CALLS = {"total": 0}


def _pace() -> None:
    """Wait for the temperature bot's idle window (its polls start every 60 s and take ~20 s), >= 1.2 s since the last
    call, and a free slot in the per-minute budget; a budget wait re-checks the idle window."""
    while True:
        if BOT_LOG.exists() and time.time() - BOT_LOG.stat().st_mtime < 180:
            while time.time() - BOT_LOG.stat().st_mtime > 33:
                time.sleep(0.5)
        wait = 1.2 - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        hold = BUDGET.wait_s()
        if hold <= 0:
            break
        time.sleep(hold)
    BUDGET.take(); _LAST[0] = time.time(); CALLS["total"] += 1


def kget(url: str) -> Any:
    """Kalshi GET, paced (_pace) and retried on 429."""
    for attempt in range(4):
        _pace()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-kalshi-lab", "Accept": "application/json"}), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (attempt + 1)); continue
            journal({"event": "error", "url": url[-80:], "err": f"HTTP {e.code}"}); return None
        except Exception as exc:
            journal({"event": "error", "url": url[-80:], "err": str(exc)[:100]}); return None
    return None


def depth(ticker: str, side: str, n: int = 5) -> list[list[float]] | None:
    d = kget(f"{K}/markets/{ticker}/orderbook")
    if d is None:
        return None
    ob = d.get("orderbook_fp") or {}
    lv = ob.get("no_dollars" if side == "YES" else "yes_dollars") or []
    return [[round(1 - float(p), 4), float(s)] for p, s in sorted(((float(p), float(s)) for p, s in lv), key=lambda x: -x[0])[:n]]


def open_markets(key: str) -> dict[str, dict] | None:
    """Open markets of a series ({ticker: market}), or of a raw query when `key` contains "="; None when the call failed."""
    d = kget(f"{K}/markets?{key if '=' in key else 'series_ticker=' + key}&status=open&limit=200")
    if d is None:
        return None
    return {m["ticker"]: m for m in d.get("markets") or []}


# ------------------------------------------------------------------ ledgers
def load(name: str) -> dict[str, Any]:
    f = PAPER / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"name": name, "cash": LIMITS["bankroll"], "start": LIMITS["bankroll"], "positions": [], "fills": [], "streak": 0, "halted": None, "decided": [], "mirrored": []}


def save(led: dict[str, Any]) -> None:
    PAPER.mkdir(parents=True, exist_ok=True); f = PAPER / f"{led['name']}.json"
    tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(led, indent=1)); tmp.replace(f)


def stake_for(led: dict[str, Any], px: float, q_hat: float) -> float:
    q = px + 0.5 * (q_hat - px)                      # shrink the measured win rate halfway to the price
    f = max(0.0, (q - px) / (1 - px)) * LIMITS["kelly"]
    return round(min(LIMITS["max_stake"], f * (led["cash"] + sum(p["stake"] for p in led["positions"])), led["cash"]), 2)


def halted(led: dict[str, Any]) -> str | None:
    today = dt.date.today().isoformat()
    day_pnl = sum(f["pnl"] for f in led["fills"] if f.get("settled", "")[:10] == today)
    total = sum(f["pnl"] for f in led["fills"])
    if led["streak"] >= LIMITS["halt_streak"]:
        return f"{led['streak']} losses in a row"
    if day_pnl <= -LIMITS["halt_day"]:
        return f"daily loss ${-day_pnl:.2f}"
    if total <= -LIMITS["halt_total"]:
        return f"cumulative loss ${-total:.2f}"
    return None


def signal(name: str, rec: dict[str, Any]) -> None:
    with SIGNALS.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "strategy": name, **rec}) + "\n")


def open_position(led: dict[str, Any], ticker: str, side: str, px: float, q_hat: float, close: str | None, why: str, book: list | None,
                  size: float | None = None, opened: str | None = None, min_stake: float = 0.5) -> dict | None:
    """Paper fill at px: quarter-Kelly stake, contracts capped by `size` or else the book depth up to px + 0.005."""
    stake = stake_for(led, px, q_hat)
    if stake < min_stake:
        return None
    if size is None:
        size = sum(s for p, s in (book or []) if p <= px + 0.005) if book else 0.0
    n = round(min(stake / px, size), 2)
    if n < 1 or led["cash"] < n * px + fee(px, n):
        return None
    pos = {"ticker": ticker, "side": side, "px": px, "shares": n, "stake": round(n * px, 4), "fee": fee(px, n), "opened": opened or dt.datetime.now(dt.timezone.utc).isoformat(),
           "close": close, "why": why}
    led["cash"] = round(led["cash"] - pos["stake"] - pos["fee"], 4); led["positions"].append(pos)
    journal({"event": "fill", "strategy": led["name"], **pos})
    return pos


def settle(led: dict[str, Any], strat: Strategy | None = None, now: dt.datetime | None = None, markets: dict[str, dict] | None = None) -> None:
    """Settle open positions on the Kalshi result after their close time (`markets` caches results within a poll), or
    through strat.result() for strategies that settle themselves."""
    now = now or dt.datetime.now(dt.timezone.utc)
    markets = {} if markets is None else markets
    for pos in list(led["positions"]):
        if strat is not None and not strat.settle_on_kalshi:
            r = strat.result(pos, now)
            if not r:
                continue
            win, when = bool(r["won"]), r["settled"]
        else:
            if pos.get("close") and dt.datetime.fromisoformat(pos["close"].replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc):
                continue
            if pos["ticker"] not in markets:
                markets[pos["ticker"]] = (kget(f"{K}/markets/{pos['ticker']}") or {}).get("market") or {}
            m = markets[pos["ticker"]]
            if m.get("result") not in ("yes", "no"):
                continue
            yes = m["result"] == "yes"; win = yes if pos["side"] == "YES" else not yes; when = dt.datetime.now(dt.timezone.utc).isoformat()
        pnl = round((pos["shares"] if win else 0.0) - pos["stake"] - pos["fee"], 4)
        led["cash"] = round(led["cash"] + (pos["shares"] if win else 0.0), 4); led["positions"].remove(pos)
        led["streak"] = 0 if win else led["streak"] + 1
        led["fills"].append({**pos, "settled": when, "won": win, "pnl": pnl})
        journal({"event": "settle", "strategy": led["name"], "ticker": pos["ticker"], "won": win, "pnl": pnl})


# ------------------------------------------------------------------ plug-ins
def registry() -> dict[str, Any]:
    if not REG.exists():
        ROOT.mkdir(parents=True, exist_ok=True); REG.write_text(json.dumps(DEFAULT_REGISTRY, indent=1))
    return json.loads(REG.read_text())


def load_plugin(name: str, spec: dict[str, Any], fresh: bool = False) -> Strategy | None:
    """Instantiate the plug-in a registry entry names; None when it names none (e.g. nightly-loop "calib" cells).
    fresh: re-import the module (its file may have changed since it was first imported)."""
    mod_name = spec.get("module") or BUILTIN.get(spec.get("family", ""))
    if not mod_name:
        return None
    mod_name, _, cls_name = mod_name.partition(":")   # "module:Class" picks one of several classes
    paths = [mod_name] if "." in mod_name else [f"scout.kalshi_lab_strategies.{mod_name}", f"lab.kalshi.strategies.{mod_name}"]
    if fresh:
        importlib.invalidate_caches()
    for path in paths:
        try:
            mod = importlib.reload(sys.modules[path]) if fresh and path in sys.modules else importlib.import_module(path)
        except ModuleNotFoundError as e:
            if e.name and (path == e.name or path.startswith(e.name + ".")):
                continue   # this candidate does not exist; a missing dependency inside it is a real error
            raise
        cls = getattr(mod, cls_name) if cls_name else getattr(mod, "STRATEGY", None)
        if cls is None:
            found = [c for c in vars(mod).values() if isinstance(c, type) and issubclass(c, Strategy) and c is not Strategy and c.__module__ == mod.__name__]
            if len(found) != 1:
                raise ImportError(f"{path}: expected one Strategy subclass or STRATEGY, found {len(found)}")
            cls = found[0]
        return cls(name, spec)
    raise ImportError(f"no plug-in module {mod_name!r} (tried {', '.join(paths)})")


class Harness:
    def __init__(self) -> None:
        self.strats: dict[str, Strategy] = {}
        self.specs: dict[str, str] = {}
        self.failed: dict[str, tuple[str, float]] = {}   # name -> (spec, time) of a failed load: reported once, retried every 10 min
        self.next_due: dict[str, float] = {}
        self.stretch = 1.0
        self.feeds = Feeds(on_error=lambda src, exc: journal({"event": "error", "url": src, "err": str(exc)[:100]}))

    def refresh(self) -> None:
        """(Re)load plug-ins for the active registry entries; a changed entry is re-imported and re-instantiated."""
        try:
            reg = registry()
        except (OSError, ValueError) as exc:   # e.g. caught mid-write by the nightly loop: keep the current set
            journal({"event": "error", "url": "registry", "err": str(exc)[:100]}); return
        keep = {}
        for name, spec in reg.items():
            if spec.get("status") not in ACTIVE:
                continue
            key = json.dumps(spec, sort_keys=True)
            if self.specs.get(name) == key and name in self.strats:
                keep[name] = self.strats[name]; continue
            fail = self.failed.get(name)
            if fail and fail[0] == key and time.time() - fail[1] < 600:
                continue
            try:
                s = load_plugin(name, spec, fresh=name in self.specs or fail is not None)
            except Exception as exc:
                if not (fail and fail[0] == key):
                    print(f"{name} plug-in error: {type(exc).__name__}: {exc}", flush=True); journal({"event": "plugin_error", "strategy": name, "err": str(exc)[:200]})
                self.failed[name] = (key, time.time())
                continue
            if s is not None:
                keep[name] = s; self.specs[name] = key; self.failed.pop(name, None)
        self.strats = keep
        self.stretch = max(1.0, self.planned_per_min() / (PLAN_SHARE * MAX_CALLS_PER_MIN))

    def base_interval(self, s: Strategy) -> float:
        return float(s.spec.get("poll_s") or s.poll_s or POLL_S)

    def interval(self, s: Strategy) -> float:
        return self.base_interval(s) * self.stretch

    def planned_per_min(self) -> float:
        """Open-market calls per minute at the base intervals (each series once per poll of its fastest strategy)."""
        fastest: dict[str, float] = {}
        for s in self.strats.values():
            for k in s.series:
                fastest[k] = min(fastest.get(k, math.inf), self.base_interval(s))
        return sum(60.0 / v for v in fastest.values())

    def due(self, t: float) -> list[Strategy]:
        return [s for name, s in self.strats.items() if self.next_due.get(name, 0.0) <= t]

    def run(self, strats: list[Strategy], now: dt.datetime) -> None:
        """One poll of the given strategies: shared open-market calls, decisions, fills, settlement, ledgers."""
        wants: dict[str, tuple[list[str], dict[str, dict]]] = {}
        for s in strats:
            s.ledger = load(s.name); s.notes = []
            try:
                wants[s.name] = (list(s.series_now(now)), dict(s.external_now(now)))
            except Exception as exc:
                self._error(s, now, exc)
        quotes: dict[str, dict[str, dict] | None] = {}
        for series, _ in wants.values():
            for key in series:
                if key not in quotes:
                    quotes[key] = open_markets(key)
        markets: dict[str, dict] = {}
        for s in strats:
            if s.name not in wants:
                continue
            series, ext = wants[s.name]
            try:
                mq = {k: quotes[k] for k in series}
                for sig in s.decide(now, mq, {k: self.feeds.get(k, kw) for k, kw in ext.items()}) or []:
                    self.execute(s, sig, mq)
                settle(s.ledger, s, now, markets)
                save(s.ledger)
                print(now.strftime("%m-%d %H:%M"), s.summary(), flush=True)
            except Exception as exc:
                self._error(s, now, exc)

    def _error(self, s: Strategy, now: dt.datetime, exc: Exception) -> None:
        print(f"{s.name} error: {type(exc).__name__}: {exc}", flush=True); journal({"event": "cycle_error", "strategy": s.name, "err": str(exc)[:200]})

    def execute(self, s: Strategy, sig: dict[str, Any], quotes: dict[str, dict[str, dict] | None]) -> None:
        """A signal: order book (unless the signal carries a size), unit-stake log, then a bankroll fill unless halted.
        Repeats of a ticker and side are dropped when the strategy has one_signal_per_market."""
        led = s.ledger; ticker = sig["ticker"]; side = sig["side"]; px = float(sig["max_price"]); label = sig.get("label") or ticker
        if side not in ("YES", "NO") or not 0 < px < 1:
            s.notes.append(f"{label}: bad signal {side} @ {px}"); return
        if s.one_signal_per_market:
            seen = led.setdefault("signalled", []); key = f"{ticker}|{side}"
            if key in seen:
                return
            seen.append(key); del seen[:-1000]
        close = sig["close"] if "close" in sig else next((q[ticker].get("close_time") for q in quotes.values() if q and ticker in q), None)
        book = None if "size" in sig else depth(ticker, side)
        rec = {"ticker": ticker, "side": side, "px": px, **(sig["log"] if "log" in sig else {"close": close, "why": sig.get("why", "")})}
        if "size" not in sig:
            rec["book"] = book
        signal(s.name, rec)
        if led["halted"] or halted(led):
            led["halted"] = led["halted"] or halted(led); s.notes.append(f"{label}: signal (bankroll halted: {led['halted']})"); return
        q_hat = sig.get("q_hat", s.params.get("q_hat"))
        if q_hat is None:
            s.notes.append(f"{label}: signal (no q_hat to size it)"); return
        pos = open_position(led, ticker, side, px, q_hat, close, sig.get("why", ""), book, size=sig.get("size"), opened=sig.get("opened"), min_stake=s.min_stake)
        s.notes.append(f"{label}: {side} @ {px:.2f}" + (f" x{pos['shares']:.0f}" if pos else " (no size)"))

    def tick(self) -> None:
        """Run every strategy that is due; each next poll is aligned to a multiple of its interval (so strategies with
        the same interval share their open-market calls)."""
        self.refresh(); t = time.time(); due = self.due(t)
        if not due:
            return
        before = CALLS["total"]
        self.run(due, dt.datetime.now(dt.timezone.utc))
        for s in due:
            iv = self.interval(s); self.next_due[s.name] = (math.floor(t / iv) + 1) * iv
        if CALLS["total"] > before:
            print(dt.datetime.now(dt.timezone.utc).strftime("%m-%d %H:%M"), f"kalshi calls: {CALLS['total'] - before} this poll, {BUDGET.used()}/{MAX_CALLS_PER_MIN} in the last minute", flush=True)

    def sleep_s(self) -> float:
        nxt = min((self.next_due.get(n, 0.0) for n in self.strats), default=time.time() + 30)
        return min(30.0, max(1.0, nxt - time.time()))


def main() -> None:
    once = "--once" in sys.argv
    h = Harness(); h.refresh()
    plan = ", ".join(f"{n} ({type(s).__name__}, {h.interval(s):.0f}s)" for n, s in h.strats.items())
    print(f"Kalshi lab paper: strategies [{plan}], bankroll ${LIMITS['bankroll']:.0f} each; planned {h.planned_per_min():.1f} open-market calls/min, "
          f"cap {MAX_CALLS_PER_MIN}/min" + (f", intervals stretched x{h.stretch:.2f}" if h.stretch > 1 else ""), flush=True)
    while True:
        h.tick()
        if once:
            break
        time.sleep(h.sleep_s())


if __name__ == "__main__":
    main()
