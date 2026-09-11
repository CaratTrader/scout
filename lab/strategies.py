"""Strategy catalog for the lab.

One interface for history and live: a Strategy sees a State (what is knowable at
decision time) and returns an Order or None. Nothing here touches ledgers or the
production bot. Families:

  lag_*      the incumbent probability model (vol-scaled move since open, calibrated
             table, blended with the book) with different state variables
  mom_*      continuation: buy the favourite once the move is large
  fade_*     reversal: buy the underdog when the book moved but price did not
  follow/fade_jump   react to the book's own jumps (informed flow vs overreaction)
  lock_*     endgame: the settlement average can no longer flip
  table_pure calibration table alone, no Gaussian, no blend
  fav_lock   near-certain favourites late in the window
  x_*        cross-asset: BTC's move as a prior for ETH/SOL windows
  *_maker    same signal, rest at the bid (makers pay no fee)
  filters    time-of-day / volatility-regime gates on lag_raw
  ensemble   trade only when two independent families agree
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scout.crypto_lag import p_up_from_move  # noqa: E402
from scout.math_risk import taker_fee_per_share  # noqa: E402

FEE_RATE = 0.07
ASK_FLOOR = 0.25
ASK_CAP = 0.92
TRUST_CAP = 0.20


@dataclass
class State:
    asset: str
    mins: int
    epoch: int
    t: float
    seconds_left: float
    open_px: float
    spot_raw: float
    spot_twap: float
    sigma: float
    yes_ask: float
    yes_bid: float
    no_ask: float
    no_bid: float
    quote_age: float = 0.0
    yes_ask_size: float | None = None
    yes_hist: list[tuple[float, float]] = field(default_factory=list)  # (ts, yes price)
    hour_utc: int = 0
    btc_z: float | None = None
    tick: float = 0.01
    oracle_hist: list[tuple[float, float]] = field(default_factory=list)  # (ts, raw oracle) inside the window
    cb_price: float | None = None  # Coinbase last trade (leads the oracle by seconds)
    cb_bid: float | None = None
    cb_ask: float | None = None
    cb_bid_size: float | None = None
    cb_ask_size: float | None = None
    cb_ts: float | None = None

    @property
    def window_s(self) -> int:
        return self.mins * 60

    def cb_fresh(self, max_age: float = 3.0) -> bool:
        return self.cb_price is not None and self.cb_ts is not None and 0 <= self.t - self.cb_ts <= max_age

    def basis_bps(self) -> float | None:
        """Coinbase minus oracle, in basis points; positive = Coinbase leads up."""
        if not self.cb_fresh() or self.spot_raw <= 0:
            return None
        return (self.cb_price - self.spot_raw) / self.spot_raw * 1e4

    def cb_imbalance(self) -> float | None:
        """Level-1 order-book imbalance on Coinbase: (bid size - ask size) / total."""
        if not self.cb_fresh() or self.cb_bid_size is None or self.cb_ask_size is None:
            return None
        tot = self.cb_bid_size + self.cb_ask_size
        return (self.cb_bid_size - self.cb_ask_size) / tot if tot > 0 else None

    def chg(self, kind: str = "raw") -> float:
        if kind == "coinbase" and self.cb_fresh():
            spot = self.cb_price
        else:
            spot = self.spot_raw if kind in ("raw", "coinbase") else self.spot_twap
        return (spot - self.open_px) / self.open_px if self.open_px > 0 else 0.0

    def sigma_t(self) -> float:
        t = max(self.seconds_left / self.window_s, 0.02)
        return math.sqrt((self.sigma * math.sqrt(t)) ** 2 + 0.00008**2)

    def z(self, kind: str = "raw") -> float:
        return self.chg(kind) / (self.sigma_t() + 1e-12)

    def market_p(self) -> float:
        bid = self.yes_bid if self.yes_bid > 0 else max(0.0, 1.0 - self.no_ask)
        return min(0.99, max(0.01, (self.yes_ask + bid) / 2.0))

    def yes_price_ago(self, seconds: float) -> float | None:
        cutoff = self.t - seconds
        best = None
        for ts, px in self.yes_hist:
            if ts <= cutoff:
                best = px
            else:
                break
        return best

    def yes_now(self) -> float | None:
        """Latest known book probability at or before this state's time (never the future)."""
        for ts, px in reversed(self.yes_hist):
            if ts <= self.t:
                return px
        return None


@dataclass
class Order:
    side: str
    price: float
    fair: float
    edge: float
    kind: str = "taker"  # taker | maker
    note: str = ""


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def model_p_up(s: State, spot_kind: str = "raw", *, model_weight: float = 0.75, calibrated: bool = True) -> float:
    raw = p_up_from_move(s.chg(spot_kind), s.seconds_left, s.window_s, sigma_full=s.sigma, oracle_noise=0.00008, calibrated=calibrated)
    return model_weight * raw + (1.0 - model_weight) * s.market_p()


def side_edge(s: State, side: str, p_yes: float, *, fee_rate: float = FEE_RATE) -> tuple[float, float, float]:
    """(ask, fair, edge) for buying `side` as a taker."""
    ask = s.yes_ask if side == "YES" else s.no_ask
    fair = p_yes if side == "YES" else 1.0 - p_yes
    return ask, fair, fair - ask - taker_fee_per_share(ask, fee_rate)


def pick_side(
    s: State,
    p_yes: float,
    *,
    min_edge: float = 0.03,
    ask_floor: float = ASK_FLOOR,
    ask_cap: float = ASK_CAP,
    trust_cap: float = TRUST_CAP,
    favourite_only: bool = False,
    underdog_only: bool = False,
    note: str = "",
) -> Order | None:
    best: Order | None = None
    for side in ("YES", "NO"):
        ask, fair, edge = side_edge(s, side, p_yes)
        if not (ask_floor <= ask <= ask_cap):
            continue
        if fair < ask - 0.02 or fair - ask > trust_cap:
            continue
        if favourite_only and ask < 0.50:
            continue
        if underdog_only and ask >= 0.50:
            continue
        if edge < min_edge:
            continue
        if best is None or edge > best.edge:
            best = Order(side, ask, fair, edge, note=note)
    return best


def to_maker(order: Order | None, s: State) -> Order | None:
    """Rest one tick under the ask (at the bid when it is there). Fee 0."""
    if order is None:
        return None
    if order.side == "YES":
        px = max(s.yes_bid, s.yes_ask - s.tick) if s.yes_bid > 0 else s.yes_ask - s.tick
    else:
        px = max(s.no_bid, s.no_ask - s.tick) if s.no_bid > 0 else s.no_ask - s.tick
    px = round(min(max(px, 0.01), 0.99), 3)
    return Order(order.side, px, order.fair, order.fair - px, kind="maker", note=order.note)


# ---------------------------------------------------------------- base
class Strategy:
    name = "base"
    family = "base"
    desc = ""
    assets: tuple[str, ...] = ("btc",)
    windows: tuple[int, ...] = (5,)
    entry: tuple[float, float] = (50.0, 250.0)  # seconds-left band (5m); scaled for 15m
    sizing = "kelly"  # kelly | fixed5 | qkelly
    cooldown_s: float = 0.0  # engine skips new entries this long after a losing close
    sample_every_s: float = 0.0  # engine evaluates only every N seconds of window time (0 = every tick)
    multi_leg: bool = False  # True: one position per side per window (market-making); False: one per window

    def quotes(self, s: State) -> list[Order]:
        o = self.decide(s)
        return [o] if o else []

    def entry_band(self, mins: int) -> tuple[float, float]:
        lo, hi = self.entry
        if mins == 15:
            return (max(lo, 90.0), 720.0)
        return (lo, hi)

    def applies(self, s: State) -> bool:
        lo, hi = self.entry_band(s.mins)
        return s.asset in self.assets and s.mins in self.windows and lo <= s.seconds_left <= hi

    def decide(self, s: State) -> Order | None:  # pragma: no cover - abstract
        raise NotImplementedError


class _Persist:
    """Require a condition to hold on the same side for N consecutive evaluations of a
    window. Per-second threshold rules otherwise fire on the first noisy tick that
    crosses the line (first-passage bias), which is what sank the naive lock/momentum
    variants on real books."""

    def __init__(self, n: int):
        self.n = n
        self._streak: dict[tuple[str, int], tuple[str, int]] = {}

    def ok(self, s: State, side: str | None) -> bool:
        key = (s.asset, s.epoch)
        if side is None:
            self._streak.pop(key, None)
            return False
        prev = self._streak.get(key)
        count = prev[1] + 1 if prev and prev[0] == side else 1
        self._streak[key] = (side, count)
        if len(self._streak) > 500:
            for k in list(self._streak)[:-200]:
                self._streak.pop(k, None)
        return count >= self.n


class LagModel(Strategy):
    family = "lag"

    def __init__(self, name: str, spot_kind: str = "raw", *, model_weight: float = 0.75, min_edge: float = 0.03, entry=(50.0, 250.0), calibrated: bool = True, favourite_only=False, underdog_only=False, sizing="kelly", assets=("btc",), windows=(5,), desc="", cooldown_s: float = 0.0, sample_every_s: float = 0.0, ask_cap: float = ASK_CAP):
        self.name, self.spot_kind, self.model_weight, self.min_edge = name, spot_kind, model_weight, min_edge
        self.entry, self.calibrated, self.favourite_only, self.underdog_only = entry, calibrated, favourite_only, underdog_only
        self.sizing, self.assets, self.windows, self.desc = sizing, assets, windows, desc
        self.cooldown_s, self.sample_every_s, self.ask_cap = cooldown_s, sample_every_s, ask_cap

    def decide(self, s: State) -> Order | None:
        p = model_p_up(s, self.spot_kind, model_weight=self.model_weight, calibrated=self.calibrated)
        return pick_side(s, p, min_edge=self.min_edge, ask_cap=self.ask_cap, favourite_only=self.favourite_only, underdog_only=self.underdog_only, note=f"z={s.z(self.spot_kind):+.2f}")


class Momentum(Strategy):
    family = "momentum"

    def __init__(self, name: str, z_min: float, ask_cap: float = 0.85, entry=(50.0, 250.0), desc="", sizing="kelly", persist: int = 1):
        self.name, self.z_min, self.ask_cap, self.entry, self.desc, self.sizing = name, z_min, ask_cap, entry, desc, sizing
        self.persist = _Persist(persist)

    def decide(self, s: State) -> Order | None:
        z = s.z("raw")
        side = ("YES" if z > 0 else "NO") if abs(z) >= self.z_min else None
        if not self.persist.ok(s, side):
            return None
        p = model_p_up(s, "raw", model_weight=1.0)
        ask, fair, edge = side_edge(s, side, p)
        if not (0.50 <= ask <= self.ask_cap):
            return None
        return Order(side, ask, fair, edge, note=f"z={z:+.2f}")


class Fade(Strategy):
    family = "fade"

    def __init__(self, name: str, z_max: float, ask_hi: float = 0.42, ask_lo: float = 0.25, entry=(50.0, 250.0), desc="", sizing="kelly"):
        self.name, self.z_max, self.ask_hi, self.ask_lo, self.entry, self.desc, self.sizing = name, z_max, ask_hi, ask_lo, entry, desc, sizing

    def decide(self, s: State) -> Order | None:
        if abs(s.z("raw")) > self.z_max:
            return None
        side = "YES" if s.yes_ask < s.no_ask else "NO"
        p = model_p_up(s, "raw", model_weight=1.0)
        ask, fair, edge = side_edge(s, side, p)
        if not (self.ask_lo <= ask <= self.ask_hi):
            return None
        return Order(side, ask, fair, edge, note=f"z={s.z('raw'):+.2f} book overreacts")


class Jump(Strategy):
    family = "jump"

    def __init__(self, name: str, jump: float, follow: bool, lookback: float = 60.0, ask_cap: float = 0.85, entry=(50.0, 250.0), desc="", sizing="kelly"):
        self.name, self.jump, self.follow, self.lookback, self.ask_cap, self.entry, self.desc, self.sizing = name, jump, follow, lookback, ask_cap, entry, desc, sizing

    def decide(self, s: State) -> Order | None:
        now, ago = s.yes_now(), s.yes_price_ago(self.lookback)
        if now is None or ago is None:
            return None
        d = now - ago
        if abs(d) < self.jump:
            return None
        up_side = d > 0
        side = ("YES" if up_side else "NO") if self.follow else ("NO" if up_side else "YES")
        p = model_p_up(s, "raw", model_weight=0.5)
        ask, fair, edge = side_edge(s, side, p)
        if not (0.15 <= ask <= self.ask_cap):
            return None
        return Order(side, ask, fair, edge, note=f"jump={d:+.2f}/{self.lookback:.0f}s")


class Lock(Strategy):
    family = "lock"
    entry = (30.0, 90.0)

    def __init__(self, name: str, k: float, ask_cap: float = 0.92, desc="", sizing="kelly", assets=("btc",), windows=(5,), persist: int = 1):
        self.name, self.k, self.ask_cap, self.desc, self.sizing = name, k, ask_cap, desc, sizing
        self.assets, self.windows = assets, windows
        self.persist = _Persist(persist)

    def entry_band(self, mins: int) -> tuple[float, float]:
        return self.entry  # the last 90 s, whatever the window length

    def decide(self, s: State) -> Order | None:
        r = s.seconds_left
        sigma_avg = s.sigma * math.sqrt(r / s.window_s) / math.sqrt(3.0) + 2e-5
        zl = s.chg("raw") / sigma_avg
        side = ("YES" if zl > 0 else "NO") if abs(zl) >= self.k else None
        if not self.persist.ok(s, side):
            return None
        p_side = min(0.995, phi(abs(zl)))
        ask = s.yes_ask if side == "YES" else s.no_ask
        if not (0.30 <= ask <= self.ask_cap):
            return None
        return Order(side, ask, p_side, p_side - ask - taker_fee_per_share(ask, FEE_RATE), note=f"z_lock={zl:+.1f}")


class LockTwap(Strategy):
    """Endgame lock with the settlement arithmetic done properly: integrate the oracle
    over the part of the 60 s settlement average already observed, ask what the rest
    must average to flip the outcome, and convert that distance into a probability.
    Persistence and a book-agreement guard keep it off noisy single ticks."""

    family = "lock"
    entry = (25.0, 90.0)

    def __init__(self, name: str, p_min: float = 0.95, persist: int = 5, ask_floor: float = 0.55, ask_cap: float = 0.93, min_edge: float = 0.02, desc="", sizing="kelly", assets=("btc",), windows=(5,), min_z: float = 0.0, entry: tuple[float, float] | None = None):
        self.name, self.p_min, self.ask_floor, self.ask_cap, self.min_edge, self.desc, self.sizing = name, p_min, ask_floor, ask_cap, min_edge, desc, sizing
        self.assets, self.windows = assets, windows
        self.persist = _Persist(persist)
        self.min_z = min_z
        if entry is not None:
            self.entry = entry

    def entry_band(self, mins: int) -> tuple[float, float]:
        return self.entry

    def p_up(self, s: State) -> float | None:
        end = s.epoch + s.window_s
        r = s.seconds_left
        if r <= 0 or s.open_px <= 0:
            return None
        twap_start = end - 60.0
        rows = [(ts, px) for ts, px in s.oracle_hist if twap_start - 6 <= ts <= s.t]
        observed = 0.0
        if s.t > twap_start:
            pts = [(min(max(ts, twap_start), s.t), px) for ts, px in rows]
            if len(pts) < 2 or pts[0][0] > twap_start + 6:
                return None
            for (a, pa), (b, pb) in zip(pts, pts[1:]):
                dt = b - a
                if dt > 6:
                    return None
                observed += (pa + pb) / 2.0 * dt
            observed += pts[-1][1] * max(0.0, s.t - pts[-1][0])
            remaining = end - s.t
        else:
            remaining = 60.0
        # settlement = (observed + future_avg * remaining) / 60 >= open  <=> future_avg >= p_req
        p_req = (s.open_px * 60.0 - observed) / remaining
        dist = (p_req - s.spot_raw) / s.spot_raw
        sigma_avg = s.sigma * math.sqrt(remaining / s.window_s) / math.sqrt(3.0) + 2e-5
        z = dist / sigma_avg
        if self.min_z > 0 and abs(z) < self.min_z:
            return None
        return min(0.995, max(0.005, 1.0 - phi(z)))

    def decide(self, s: State) -> Order | None:
        p = self.p_up(s)
        side = None
        if p is not None:
            side = "YES" if p >= self.p_min else "NO" if p <= 1 - self.p_min else None
        if not self.persist.ok(s, side):
            return None
        mp = s.market_p() if side == "YES" else 1 - s.market_p()
        if mp < 0.55:
            return None  # the book disagrees hard: assume it knows something the oracle tick does not
        ask, fair, edge = side_edge(s, side, p)
        if not (self.ask_floor <= ask <= self.ask_cap) or edge < self.min_edge:
            return None
        return Order(side, ask, fair, edge, note=f"p_lock={p:.3f} mkt={mp:.2f}")


class LockMaker(LockTwap):
    """The lock as a MAKER: when the settlement arithmetic is decisive and the favourite is
    asked inside the band, rest a bid `ticks` below the ask instead of lifting it. Fee 0;
    fills only if a later ask trades down to the bid (replay rule), so this measures how much
    of the lock edge is available without paying the spread."""

    family = "lock"

    def __init__(self, name: str, ticks: int = 1, **kw):
        super().__init__(name, **kw)
        self.ticks = ticks

    def decide(self, s: State) -> Order | None:
        o = super().decide(s)
        if o is None:
            return None
        px = round(o.price - self.ticks * s.tick, 3)
        if px <= 0:
            return None
        return Order(o.side, px, o.fair, o.fair - px, kind="maker", note=o.note + f" maker@{px:.3f}")


class MakerBoth(Strategy):
    """Two-sided liquidity: rest a bid on YES and a bid on NO, each `offset` under the
    model's fair value, inside a time band; legs that fill are held to settlement. If
    both fill you own a complete set (pays $1 for less than $1). Makers pay no fee.
    This is the test of whether earning the spread beats adverse selection."""

    family = "mm"
    multi_leg = True

    def __init__(self, name: str, offset: float = 0.03, entry=(40.0, 220.0), anchor: str = "fair", desc="", sizing="fixed5", assets=("btc",), windows=(5,)):
        self.name, self.offset, self.entry, self.anchor, self.desc, self.sizing = name, offset, entry, anchor, desc, sizing
        self.assets, self.windows = assets, windows

    def entry_band(self, mins: int) -> tuple[float, float]:
        return self.entry if mins == 5 else (self.entry[0], self.entry[1] * 3)

    def decide(self, s: State) -> Order | None:  # pragma: no cover - quotes() is the interface
        q = self.quotes(s)
        return q[0] if q else None

    def quotes(self, s: State) -> list[Order]:
        p = model_p_up(s, "raw", model_weight=0.5)
        out = []
        for side in ("YES", "NO"):
            fair = p if side == "YES" else 1.0 - p
            ask = s.yes_ask if side == "YES" else s.no_ask
            bid = s.yes_bid if side == "YES" else s.no_bid
            if self.anchor == "fair":
                px = fair - self.offset
            else:  # join the bid
                px = bid if bid > 0 else ask - s.tick
            px = round(min(px, ask - s.tick), 2)
            if not (0.05 <= px <= 0.95):
                continue
            out.append(Order(side, px, fair, fair - px, kind="maker", note=f"quote {side} {px:.2f} fair {fair:.2f}"))
        return out


class FavLock(Strategy):
    family = "lock"
    entry = (50.0, 120.0)

    def __init__(self, name: str, p_min: float = 0.90, ask_cap: float = 0.95, min_edge: float = 0.005, desc="", sizing="kelly"):
        self.name, self.p_min, self.ask_cap, self.min_edge, self.desc, self.sizing = name, p_min, ask_cap, min_edge, desc, sizing

    def decide(self, s: State) -> Order | None:
        mp = s.market_p()
        if not (mp >= self.p_min or mp <= 1 - self.p_min):
            return None
        side = "YES" if mp >= 0.5 else "NO"
        z = s.z("raw")
        if (side == "YES" and z < 0) or (side == "NO" and z > 0):
            return None
        p = model_p_up(s, "raw", model_weight=0.5)
        ask, fair, edge = side_edge(s, side, p)
        if ask > self.ask_cap or edge < self.min_edge:
            return None
        return Order(side, ask, fair, edge, note=f"mkt={mp:.2f} z={z:+.2f}")


class CrossAsset(Strategy):
    family = "cross"

    def __init__(self, name: str, beta: float = 0.6, min_edge: float = 0.03, assets=("eth", "sol"), desc="", sizing="kelly"):
        self.name, self.beta, self.min_edge, self.assets, self.desc, self.sizing = name, beta, min_edge, assets, desc, sizing

    def decide(self, s: State) -> Order | None:
        if s.btc_z is None:
            return None
        z = s.z("raw") + self.beta * s.btc_z
        raw = min(0.985, max(0.015, phi(z)))
        p = 0.75 * raw + 0.25 * s.market_p()
        return pick_side(s, p, min_edge=self.min_edge, note=f"z={s.z('raw'):+.2f} btc_z={s.btc_z:+.2f}")


class Maker(Strategy):
    family = "maker"

    def __init__(self, name: str, base: Strategy, desc=""):
        self.name, self.base, self.desc = name, base, desc
        self.assets, self.windows, self.entry, self.sizing = base.assets, base.windows, base.entry, base.sizing

    def decide(self, s: State) -> Order | None:
        return to_maker(self.base.decide(s), s)


class Filtered(Strategy):
    family = "filter"

    def __init__(self, name: str, base: Strategy, pred: Callable[[State], bool], desc=""):
        self.name, self.base, self.pred, self.desc = name, base, pred, desc
        self.assets, self.windows, self.entry, self.sizing = base.assets, base.windows, base.entry, base.sizing

    def decide(self, s: State) -> Order | None:
        return self.base.decide(s) if self.pred(s) else None


class BasisFilter(Strategy):
    """Only take a side when Coinbase, which leads the oracle by seconds, is already on
    that side of the oracle by at least `min_bps`. Real-book evidence 2026-09-08: every
    taker strategy did ~0.1/$ better with the basis than against it."""

    family = "basis"

    def __init__(self, name: str, base: Strategy, min_bps: float = 0.15, desc=""):
        self.name, self.base, self.min_bps, self.desc = name, base, min_bps, desc
        self.assets, self.windows, self.entry, self.sizing = base.assets, base.windows, base.entry, base.sizing
        self.cooldown_s, self.sample_every_s, self.multi_leg = base.cooldown_s, base.sample_every_s, base.multi_leg

    def entry_band(self, mins: int) -> tuple[float, float]:
        return self.base.entry_band(mins)

    def decide(self, s: State) -> Order | None:
        o = self.base.decide(s)
        if o is None:
            return None
        b = s.basis_bps()
        if b is None:
            return None
        if (o.side == "YES" and b >= self.min_bps) or (o.side == "NO" and b <= -self.min_bps):
            return Order(o.side, o.price, o.fair, o.edge, kind=o.kind, note=f"{o.note} basis={b:+.2f}bp")
        return None


class ObiFilter(Strategy):
    """Only take a side when Coinbase's level-1 book leans that way (bid size vs ask size)."""

    family = "basis"

    def __init__(self, name: str, base: Strategy, min_imb: float = 0.2, desc=""):
        self.name, self.base, self.min_imb, self.desc = name, base, min_imb, desc
        self.assets, self.windows, self.entry, self.sizing = base.assets, base.windows, base.entry, base.sizing
        self.cooldown_s, self.sample_every_s, self.multi_leg = base.cooldown_s, base.sample_every_s, base.multi_leg

    def entry_band(self, mins: int) -> tuple[float, float]:
        return self.base.entry_band(mins)

    def decide(self, s: State) -> Order | None:
        o = self.base.decide(s)
        if o is None:
            return None
        imb = s.cb_imbalance()
        if imb is None:
            return None
        if (o.side == "YES" and imb >= self.min_imb) or (o.side == "NO" and imb <= -self.min_imb):
            return Order(o.side, o.price, o.fair, o.edge, kind=o.kind, note=f"{o.note} obi={imb:+.2f}")
        return None


class Ensemble(Strategy):
    family = "ensemble"

    def __init__(self, name: str, a: Strategy, b: Strategy, desc=""):
        self.name, self.a, self.b, self.desc = name, a, b, desc
        self.assets, self.windows, self.entry, self.sizing = a.assets, a.windows, a.entry, a.sizing

    def decide(self, s: State) -> Order | None:
        oa, ob = self.a.decide(s), self.b.decide(s)
        if oa and ob and oa.side == ob.side:
            return Order(oa.side, oa.price, oa.fair, oa.edge, note=f"agree {oa.note} | {ob.note}")
        return None


# ---------------------------------------------------------------- catalog
def catalog() -> list[Strategy]:
    lag_twap = LagModel("lag_twap", "twap", desc="Scout as deployed: 60s-TWAP spot, 75/25 blend, edge>=0.03, ask 0.25-0.92")
    lag_raw = LagModel("lag_raw", "raw", desc="Scout with the fresh raw price as spot (CRYPTO_SPOT_SOURCE=raw)")
    lag_raw_pure = LagModel("lag_raw_pure", "raw", model_weight=1.0, desc="raw spot, model only (no book blend)")
    lag_raw_edge6 = LagModel("lag_raw_edge6", "raw", min_edge=0.06, desc="raw spot, demand 6% edge after fees")
    lag_raw_late = LagModel("lag_raw_late", "raw", entry=(50.0, 130.0), desc="raw spot, only the last 50-130s")
    lag_raw_early = LagModel("lag_raw_early", "raw", entry=(170.0, 250.0), desc="raw spot, only 170-250s left")
    lag_raw_fav = LagModel("lag_raw_fav", "raw", favourite_only=True, desc="raw spot, favourites only (ask>=0.50)")
    lag_raw_dog = LagModel("lag_raw_dog", "raw", underdog_only=True, desc="raw spot, underdogs only (ask<0.50) - Scout's current de-facto book")
    lag_gauss = LagModel("lag_gauss", "raw", calibrated=False, desc="raw spot, plain Gaussian (no calibration table)")
    lag_raw_fixed = LagModel("lag_raw_fixed", "raw", sizing="fixed5", desc="raw spot, flat $5 stakes")
    lag_raw_15m = LagModel("lag_raw_15m", "raw", windows=(15,), entry=(90.0, 720.0), desc="raw spot on BTC 15-minute windows")
    lag_raw_eth = LagModel("lag_raw_eth", "raw", assets=("eth",), desc="raw spot on ETH 5m")
    lag_raw_sol = LagModel("lag_raw_sol", "raw", assets=("sol",), desc="raw spot on SOL 5m")
    mom1 = Momentum("mom_z1", 1.0, desc="buy the favourite when |z|>=1 and ask<=0.85")
    mom15_late = Momentum("mom_z15_late", 1.5, ask_cap=0.90, entry=(50.0, 130.0), desc="|z|>=1.5 in the last 130s, ask<=0.90")
    fade = Fade("fade_flat", 0.35, desc="buy the underdog (0.25-0.42) when price barely moved (|z|<=0.35)")
    follow = Jump("follow_jump", 0.15, True, desc="book jumped >=0.15 in 60s: follow it (informed flow)")
    fadej = Jump("fade_jump", 0.15, False, desc="book jumped >=0.15 in 60s: fade it (overreaction)")
    lock3 = Lock("lock_k3", 3.0, desc="<=90s left, lead is >=3 sigma of the remaining average")
    lock2 = Lock("lock_k2", 2.0, desc="<=90s left, lead >=2 sigma")
    favlock = FavLock("fav_lock_90", desc="book >=0.90 with the move on its side, <=120s, tiny edge")
    table_pure = LagModel("table_pure", "raw", model_weight=1.0, min_edge=0.05, desc="calibration table only, edge>=0.05")
    xeth = CrossAsset("x_btc_eth", assets=("eth",), desc="ETH 5m with BTC's z as a prior")
    xsol = CrossAsset("x_btc_sol", assets=("sol",), desc="SOL 5m with BTC's z as a prior")
    maker_lag = Maker("lag_raw_maker", lag_raw, desc="lag_raw signal, rest at the bid, zero fee")
    maker_mom = Maker("mom_z1_maker", mom1, desc="momentum signal, rest at the bid")
    us_hours = Filtered("lag_raw_us", lag_raw, lambda s: 13 <= s.hour_utc < 21, desc="lag_raw during US hours (13-21 UTC)")
    off_hours = Filtered("lag_raw_offhours", lag_raw, lambda s: not (13 <= s.hour_utc < 21), desc="lag_raw outside US hours")
    lowvol = Filtered("lag_raw_lowvol", lag_raw, lambda s: s.sigma <= 0.0012, desc="lag_raw when sigma<=0.12%/window")
    highvol = Filtered("lag_raw_highvol", lag_raw, lambda s: s.sigma > 0.0012, desc="lag_raw when sigma>0.12%/window")
    ens = Ensemble("ens_lag_mom", lag_raw, mom1, desc="trade only when lag_raw and momentum agree on the side")
    # pre-registered refinements from the 2026-09-05 replay: the edge sat in fills <0.85 and outside US hours
    lock2_cheap = Lock("lock_k2_cheap", 2.0, ask_cap=0.85, desc="lock_k2 but only when the book is still <=0.85 (the mispriced subset)")
    lock2_off = Filtered("lock_k2_offhours", lock2, lambda s: not (11 <= s.hour_utc < 17), desc="lock_k2 outside 11-17 UTC (thinner books)")
    mom15_cheap = Momentum("mom_z15_cheap", 1.5, ask_cap=0.85, entry=(50.0, 130.0), desc="|z|>=1.5 in the last 130s, only while ask<=0.85")
    lock2_alt = Lock("lock_k2_alt", 2.0, assets=("eth", "sol"), desc="lock_k2 on ETH and SOL 5m")
    lock2_15m = Lock("lock_k2_15m", 2.0, windows=(15,), desc="lock_k2 on BTC 15m (last 90s)")
    # 2026-09-06: real-book results showed first-passage bias in per-second threshold rules
    lock2_p5 = Lock("lock_k2_p5", 2.0, persist=5, desc="lock_k2, condition must hold 5 consecutive seconds")
    lock3_p5 = Lock("lock_k3_p5", 3.0, persist=5, desc="lock_k3, 5 s persistence")
    mom15_p5 = Momentum("mom_z15_p5", 1.5, ask_cap=0.90, entry=(50.0, 130.0), persist=5, desc="late momentum, 5 s persistence")
    locktwap = LockTwap("lock_twap", desc="settlement-integral lock: p>=0.95 for 5 s, book agrees (>=0.55), ask 0.55-0.93")
    locktwap97 = LockTwap("lock_twap_97", p_min=0.97, persist=8, desc="settlement-integral lock, p>=0.97 for 8 s")
    locktwap_all = LockTwap("lock_twap_all", assets=("btc", "eth", "sol"), windows=(5, 15), desc="settlement-integral lock on every asset and window")
    # 2026-09-10: live mirror of the late-window favourite pocket (tick study, section 12):
    # |z| >= 3.5 inside the last 45 s, favourite asked 0.955-0.98, no persistence (the live loop samples every ~2 s)
    lock_live = LockTwap("lock_live_mirror", persist=1, ask_floor=0.955, ask_cap=0.99, min_edge=0.004, min_z=3.5, entry=(8.0, 55.0), assets=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype"), windows=(5, 15, 240),
                         desc="LIVE MIRROR: settlement lock |z|>=3.5, favourite at 0.955-0.99, 8-55 s left, seven assets, 5m/15m/4h")
    lock_tight = LockTwap("lock_tight", persist=1, ask_floor=0.955, ask_cap=0.98, min_edge=0.012, min_z=3.5, entry=(8.0, 45.0), assets=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype"), windows=(5, 15),
                          desc="the original 0.98 / 45 s cut kept as the conservative control")
    lock_wide = LockTwap("lock_wide", persist=1, ask_floor=0.955, ask_cap=0.99, min_edge=0.004, min_z=3.5, entry=(8.0, 55.0), assets=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype"), windows=(5, 15),
                         desc="lock mirror widened: ask up to 0.99, 8-55 s left, all listed assets")
    lock_maker1 = LockMaker("lock_maker1", ticks=1, persist=1, ask_floor=0.955, ask_cap=0.99, min_edge=0.004, min_z=3.5, entry=(20.0, 55.0), assets=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype"), windows=(5, 15),
                            desc="lock as maker: bid one tick under the favourite's ask, fee 0, fills only if the ask comes down")
    lock_maker2 = LockMaker("lock_maker2", ticks=2, persist=1, ask_floor=0.955, ask_cap=0.99, min_edge=0.004, min_z=3.5, entry=(20.0, 55.0), assets=("btc", "eth", "sol", "xrp", "doge", "bnb", "hype"), windows=(5, 15),
                            desc="lock as maker, two ticks under the ask")
    scout_clone = LagModel("scout_clone", "raw", underdog_only=True, ask_cap=0.40, cooldown_s=900.0, sample_every_s=35.0, desc="Scout as deployed: raw spot, 0.25-0.40 asks (lessons veto the rest), 900 s loss cooldown, one look per 35 s")
    scout_nocool = LagModel("scout_nocool", "raw", underdog_only=True, ask_cap=0.40, sample_every_s=35.0, desc="Scout clone without the loss cooldown")
    scout_fav = LagModel("scout_fav_clone", "raw", favourite_only=True, cooldown_s=900.0, sample_every_s=35.0, desc="Scout's loop but favourites only")
    # market making: the only participant class the fee schedule pays
    mm3 = MakerBoth("mm_fair3", offset=0.03, desc="rest YES and NO bids 3c under model fair, 220-40 s left, hold fills")
    mm5 = MakerBoth("mm_fair5", offset=0.05, desc="rest both sides 5c under fair")
    mm_join = MakerBoth("mm_join_bid", anchor="bid", desc="join the best bid on both sides")
    mm_all = MakerBoth("mm_fair3_all", offset=0.03, assets=("btc", "eth", "sol"), windows=(5, 15), desc="mm_fair3 on every asset and window")
    # 2026-09-08: Coinbase leads the oracle; the basis sign separated winners from losers in every taker family
    lag_cb = LagModel("lag_cb", "coinbase", desc="lag model with Coinbase (the leading venue) as spot")
    lag_cb_fav = LagModel("lag_cb_fav", "coinbase", favourite_only=True, desc="Coinbase spot, favourites only")
    lag_raw_basis = BasisFilter("lag_raw_basis", lag_raw, desc="lag_raw only when Coinbase already leads in that direction (>=0.15 bp)")
    lag_fav_basis = BasisFilter("lag_fav_basis", lag_raw_fav, desc="favourites only, Coinbase agrees")
    lag_fav_basis3 = BasisFilter("lag_fav_basis3", lag_raw_fav, min_bps=0.3, desc="favourites only, Coinbase leads by >=0.3 bp")
    mom1_basis = BasisFilter("mom_z1_basis", mom1, desc="momentum, Coinbase agrees")
    scout_basis = BasisFilter("scout_clone_basis", scout_clone, desc="Scout's rules plus the Coinbase agreement filter")
    lag_fav_obi = ObiFilter("lag_fav_obi", lag_raw_fav, desc="favourites only, Coinbase level-1 book leans our way")
    lag_fav_basis_all = BasisFilter("lag_fav_basis_all", LagModel("lag_raw_fav_all", "raw", favourite_only=True, assets=("btc", "eth", "sol"), windows=(5, 15)), desc="favourites + Coinbase agreement, every asset and window")
    return [
        lag_twap, lag_raw, lag_raw_pure, lag_raw_edge6, lag_raw_late, lag_raw_early, lag_raw_fav, lag_raw_dog, lag_gauss,
        lag_raw_fixed, lag_raw_15m, lag_raw_eth, lag_raw_sol, mom1, mom15_late, fade, follow, fadej, lock3, lock2, favlock,
        table_pure, xeth, xsol, maker_lag, maker_mom, us_hours, off_hours, lowvol, highvol, ens,
        lock2_cheap, lock2_off, mom15_cheap, lock2_alt, lock2_15m,
        lock2_p5, lock3_p5, mom15_p5, locktwap, locktwap97, locktwap_all, lock_live, lock_tight, lock_wide, lock_maker1, lock_maker2, scout_clone, scout_nocool, scout_fav,
        mm3, mm5, mm_join, mm_all,
        lag_cb, lag_cb_fav, lag_raw_basis, lag_fav_basis, lag_fav_basis3, mom1_basis, scout_basis, lag_fav_obi, lag_fav_basis_all,
    ]


def by_name() -> dict[str, Strategy]:
    return {s.name: s for s in catalog()}
