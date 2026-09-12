"""TWAP lock-in: the endgame edge.

These markets settle on a 60s Chainlink TWAP at window end vs window start.
Inside the final 60 seconds, part of that average is already determined by
observed oracle prices. Integrate the observed part exactly, then ask: what
constant price would the remaining seconds need to average to flip the outcome?
When that requirement is many sigmas away and the book still prices doubt,
buy the near-certain side.

Example that motivated this (real print): YES traded 0.165 with 46s left in a
window that settled 1.00.
"""
from __future__ import annotations

import math
import os
import time
from typing import Any

from . import streams



def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Entry rules for lock candidates (tighter and later than the lag model's window).
# Every knob has a TWAP_LOCK_* environment override so one job can run the
# late-window favourite profile (docs/STRATEGY_RESEARCH.md section 12) while the
# paper job keeps these defaults.
MIN_SECONDS = _env_float("TWAP_LOCK_MIN_SECONDS", 30.0)   # default matches execute's crypto_min_seconds_to_expiry
MAX_SECONDS = _env_float("TWAP_LOCK_MAX_SECONDS", 90.0)
MIN_P_LOCK = _env_float("TWAP_LOCK_MIN_P", 0.95)
ASK_FLOOR = _env_float("TWAP_LOCK_ASK_FLOOR", 0.30)
ASK_CAP = _env_float("TWAP_LOCK_ASK_CAP", 0.92)
MIN_Z = _env_float("TWAP_LOCK_MIN_Z", 0.0)  # |z| of the remaining-average requirement; 0 = gate on p only
MAX_ORACLE_AGE = _env_float("TWAP_LOCK_MAX_ORACLE_AGE", 3.0)  # newest oracle print must be this fresh
MIN_COVERAGE = 0.85  # observed tick coverage of the elapsed TWAP interval
MAX_TICK_GAP = 6.0   # seconds without an oracle tick = integral untrustworthy
AVG_VOL_SHRINK = 1.0 / math.sqrt(3.0)  # std of a Brownian time-average vs endpoint


def observed_integral(ticks: list[tuple[float, float]], t0: float, t1: float) -> tuple[float, float] | None:
    """Trapezoidal integral of oracle price over [t0, t1] plus coverage checks.

    Returns (integral, covered_seconds) or None when the tick record is too
    sparse to trust."""
    if t1 <= t0:
        return 0.0, 0.0
    rows = [t for t in ticks if t0 - MAX_TICK_GAP <= t[0] <= t1 + 1.0]
    if len(rows) < 2:
        return None
    # clamp the first sample to t0 (carry the earlier price forward)
    pts: list[tuple[float, float]] = []
    for ts, px in rows:
        pts.append((min(max(ts, t0), t1), px))
    if pts[0][0] > t0 + MAX_TICK_GAP:
        return None
    integral = 0.0
    covered = 0.0
    for (a_ts, a_px), (b_ts, b_px) in zip(pts, pts[1:]):
        dt = b_ts - a_ts
        if dt <= 0:
            continue
        if dt > MAX_TICK_GAP:
            return None
        integral += (a_px + b_px) / 2.0 * dt
        covered += dt
    # extend the last tick to t1 (price carried forward)
    tail = t1 - pts[-1][0]
    if tail > MAX_TICK_GAP:
        return None
    if tail > 0:
        integral += pts[-1][1] * tail
        covered += tail
    if covered / (t1 - t0) < MIN_COVERAGE:
        return None
    return integral, covered


def lock_signal(
    asset: str,
    win: dict[str, Any],
    open_twap: float,
    sigma_full: float,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """P(window resolves Up) from locked TWAP arithmetic, or None if not in the
    endgame / data insufficient. sigma_full = full-window relative vol."""
    now = time.time() if now is None else now
    end = float(win["end"])
    window_s = float(win["window_s"])
    r = end - now
    if not (MIN_SECONDS <= r <= MAX_SECONDS) or open_twap <= 0:
        return None
    spot_row = streams.oracle_spot(asset, max_age=MAX_ORACLE_AGE)
    if spot_row is None:
        return None
    spot, _ = spot_row
    twap_start = end - 60.0
    obs = observed_integral(streams.oracle_ticks(asset, twap_start - MAX_TICK_GAP), twap_start, now)
    if obs is None:
        return None
    integral, _ = obs
    # final = (integral + future_avg * r) / 60 ; Up iff final > open_twap
    p_req = (open_twap * 60.0 - integral) / r
    dist = (p_req - spot) / spot
    # vol of the *average* price over the remaining r seconds
    sigma_avg = sigma_full * math.sqrt(r / window_s) * AVG_VOL_SHRINK + 2e-5
    z = dist / sigma_avg
    if MIN_Z > 0 and abs(z) < MIN_Z:
        return None
    # future_avg > p_req flips to Up; below keeps Down (and vice versa)
    p_up = 1.0 - _phi(z)
    return {
        "p_up": min(0.995, max(0.005, p_up)),
        "seconds_left": r,
        "required_move_bps": round(dist * 10_000, 2),
        "z": round(z, 2),
        "spot": spot,
        "open_twap": open_twap,
    }


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def candidate_gate(side_ask: float, p_side: float, fee: float, min_edge: float) -> bool:
    """The lock candidate's own EV gate (bypasses crypto_side_ok's trust cap —
    fair here is arithmetic on the settlement oracle, not a model opinion)."""
    if not (ASK_FLOOR <= side_ask <= ASK_CAP):
        return False
    if p_side < MIN_P_LOCK:
        return False
    return p_side - side_ask - fee >= min_edge
