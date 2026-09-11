"""Dry run of the live LOCK profile against the real market — prints what the live
loop would score and size, never places an order and never touches a ledger.

    .venv/bin/python -m lab.lock_dryrun --minutes 30

Sets the lock profile's environment (same values as docs/com.tradeinc.scout.live.lock.plist)
unless the variables are already set, then runs the same collect → refresh books → score →
build_candidates path as scout.agent.cycle every ~2 s and logs lock candidates with the
slug so outcomes can be checked afterwards.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROFILE = {
    "CRYPTO_ONLY": "1",
    "CRYPTO_ASSETS": "btc,eth,sol,xrp,doge,bnb,hype",
    "CRYPTO_WINDOWS_MIN": "5,15,240",
    "CRYPTO_EVENT_CACHE_SECONDS": "20",
    "CRYPTO_LAG_MODEL": "off",
    "LESSONS": "0",
    "TWAP_LOCK_MIN_SECONDS": "8",
    "TWAP_LOCK_MAX_SECONDS": "55",
    "TWAP_LOCK_ASK_FLOOR": "0.955",
    "TWAP_LOCK_ASK_CAP": "0.99",
    "TWAP_LOCK_MIN_Z": "3.5",
    "CRYPTO_MIN_EDGE": "0.004",
    "CRYPTO_MIN_SECONDS_TO_EXPIRY": "5",
    "CRYPTO_SIGNAL_TTL_SECONDS": "6",
    "MIN_CRYPTO_STAKE": "2",
    "MAX_CRYPTO_STAKE": "20",
    "MAX_CRYPTO_POSITIONS": "3",
    "MAX_POSITIONS": "3",
    "CAMPAIGN_TARGET_USD": "0",
}
for _k, _v in PROFILE.items():
    os.environ.setdefault(_k, _v)

from scout import streams, twap_lock  # noqa: E402  (after the profile env is set)
from scout.agent import _refresh_crypto_books, collect_universe  # noqa: E402
from scout.config import settings_from_env  # noqa: E402
from scout.crypto_lag import is_crypto_updown, parse_window, score_crypto_windows, start_chainlink_stream  # noqa: E402
from scout.signals import attach_sizing, build_candidates  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _settle(pending: list[dict], settings, *, force: bool = False) -> None:
    """Print WIN/LOSS for WOULD BUY windows once Gamma has closed them."""
    from scout.crypto_lag import _get_json

    for row in list(pending):
        if not force and time.time() < row["end"] + 90:
            continue
        try:
            # Gamma indexes these windows by EVENT slug; /markets?slug= returns nothing for them.
            rows = _get_json(f"https://gamma-api.polymarket.com/events?slug={row['slug']}", settings.user_agent, timeout=10)
        except Exception as exc:
            print(f"{_now()} settle lookup failed {row['slug']}: {type(exc).__name__}", flush=True)
            continue
        ev = rows[0] if isinstance(rows, list) and rows else None
        m = (ev.get("markets") or [None])[0] if isinstance(ev, dict) else None
        if not m or not m.get("closed"):
            if force:
                print(f"{_now()} UNSETTLED {row['slug']} {row['side']} @ {row['ask']:.3f}", flush=True)
                pending.remove(row)
            continue
        try:
            import json as _json

            up = float(_json.loads(m.get("outcomePrices") or "[]")[0]) > 0.5
        except Exception:
            continue
        won = up if row["side"] == "YES" else not up
        fee = 0.07 * row["ask"] * (1 - row["ask"])
        pnl = (1.0 - row["ask"] - fee) if won else -(row["ask"] + fee)
        print(f"{_now()} {'WIN ' if won else 'LOSS'} {row['slug']} {row['side']} @ {row['ask']:.3f} → {pnl / row['ask']:+.3f} per $ staked", flush=True)
        pending.remove(row)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--cash", type=float, default=38.0, help="bankroll used for sizing only")
    args = ap.parse_args()
    settings = settings_from_env()
    print(
        f"{_now()} lock dry run {args.minutes:.0f} min · window {twap_lock.MIN_SECONDS:.0f}-{twap_lock.MAX_SECONDS:.0f} s · "
        f"ask {twap_lock.ASK_FLOOR}-{twap_lock.ASK_CAP} · |z|>={twap_lock.MIN_Z} · min edge {settings.crypto_min_edge} · "
        f"lag model {'off' if os.getenv('CRYPTO_LAG_MODEL') == 'off' else 'ON'}",
        flush=True,
    )
    start_chainlink_stream()
    streams.start_streams()
    t_end = time.time() + args.minutes * 60
    cycles = 0
    durations: list[float] = []
    logged: set[tuple[str, str]] = set()
    pending: list[dict] = []
    while time.time() < t_end:
        t0 = time.time()
        try:
            universe = collect_universe(settings)
            crypto = [m for m in universe if is_crypto_updown(m)]
            fresh = _refresh_crypto_books(crypto)
            scores = score_crypto_windows(crypto, settings)
            cands = attach_sizing(build_candidates(universe, settings, scores), args.cash, settings)
        except Exception as exc:  # keep the dry run alive; the live loop has its own retry
            print(f"{_now()} cycle error {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            time.sleep(2)
            continue
        dt = time.time() - t0
        durations.append(dt)
        cycles += 1
        by_id = {m["id"]: m for m in crypto}
        for mid, sc in scores.items():
            if sc.get("edge_type") != "twap_lock":
                continue
            m = by_id.get(mid) or {}
            win = parse_window(m) or {}
            key = (mid, "score")
            if key not in logged:
                logged.add(key)
                print(
                    f"{_now()} LOCK   {m.get('slug', mid)} p_up={sc['p_yes']:.3f} left={sc.get('seconds_left_at_signal', 0):.0f}s "
                    f"yes_ask={m.get('yes_ask')} no_ask={m.get('no_ask')} · {sc.get('thesis', '')}",
                    flush=True,
                )
        for c in cands:
            if c.get("edge_type") != "twap_lock":
                continue
            key = (c["id"], c["side"])
            if key in logged:
                continue
            logged.add(key)
            win = parse_window(c) or {}
            left = float(win.get("end", 0)) - time.time() if win else 0.0
            print(
                f"{_now()} WOULD BUY {c.get('slug', c['id'])} {c['side']} ask={c['price']:.3f} fair={c['fair']:.3f} "
                f"edge={c['edge']:+.4f} stake=${c.get('stake', 0):.2f} left={left:.0f}s",
                flush=True,
            )
            pending.append({"slug": c.get("slug", c["id"]), "side": c["side"], "ask": float(c["price"]), "end": float(win.get("end", time.time()))})
        if cycles % 10 == 0:
            _settle(pending, settings)
        if cycles % 30 == 0:
            durations.sort()
            print(
                f"{_now()} heartbeat cycles={cycles} markets={len(crypto)} fresh={fresh} "
                f"cycle_s median={durations[len(durations) // 2]:.2f} p90={durations[int(len(durations) * 0.9)]:.2f}",
                flush=True,
            )
        time.sleep(max(0.0, 2.0 - dt))
    if pending:
        wait = max(0.0, max(r["end"] for r in pending) + 120 - time.time())
        if wait:
            print(f"{_now()} waiting {wait:.0f}s for the last windows to settle", flush=True)
            time.sleep(min(wait, 600))
        _settle(pending, settings, force=True)
    durations.sort()
    print(f"{_now()} done cycles={cycles} median cycle {durations[len(durations) // 2]:.2f}s" if durations else "no cycles", flush=True)


if __name__ == "__main__":
    main()
