"""Replay strategies on the arena's 1-second tick recordings — real order books.

Fills are taken at the recorded best ask of that second when the displayed size covers
a $5 order (maker orders fill when a later ask trades down to the limit, fee 0).
Outcomes come from lab/cache windows (Gamma) or a direct Gamma lookup by slug.

  .venv/bin/python -m lab.replay_ticks                # all recorded windows
  .venv/bin/python -m lab.replay_ticks --only lock_twap,scout_clone
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab import data as labdata  # noqa: E402
from lab.replay import RESULTS_DIR, stake_for, summarize  # noqa: E402
from lab.strategies import FEE_RATE, Order, State, Strategy, catalog  # noqa: E402
from scout.math_risk import taker_fee_per_share  # noqa: E402

TICKS = ROOT / "data" / "lab" / "ticks"
OUTCOMES_PATH = ROOT / "lab" / "cache" / "outcomes.json"
LEADERBOARD_OUT = ROOT / "data" / "lab" / "tick_leaderboard.json"
FIXED = 5.0
# REPLAY_LATENCY=<seconds>: taker fills require the offer to still be there that long after the
# decision (0 = the old next-print assumption). Live median signal->order is 0.6 s; use 1-2.
LATENCY_S = float(__import__("os").getenv("REPLAY_LATENCY") or 0)


def load_rows() -> dict[tuple[str, int, int], list[list]]:
    groups: dict[tuple[str, int, int], list[list]] = defaultdict(list)
    for fn in sorted(glob.glob(str(TICKS / "*.jsonl*"))):
        op = gzip.open if fn.endswith(".gz") else open
        with op(fn, "rt") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if len(r) < 13:
                    continue
                groups[(r[1], int(r[2]), int(r[3]))].append(r)
    for rows in groups.values():
        rows.sort(key=lambda r: r[0])
    return groups


def outcomes_for(keys: list[tuple[str, int, int]]) -> dict[tuple[str, int, int], bool]:
    cache: dict[str, Any] = {}
    if OUTCOMES_PATH.exists():
        try:
            cache = json.loads(OUTCOMES_PATH.read_text())
        except Exception:
            cache = {}
    out: dict[tuple[str, int, int], bool] = {}
    windows_cache: dict[tuple[str, int], dict[int, dict]] = {}
    todo = []
    for asset, mins, epoch in keys:
        slug = f"{asset}-updown-{mins}m-{epoch}"
        if slug in cache:
            out[(asset, mins, epoch)] = bool(cache[slug])
            continue
        w = windows_cache.setdefault((asset, mins), labdata.load_windows(asset, mins))
        row = w.get(epoch)
        if row and "up_won" in row:
            out[(asset, mins, epoch)] = bool(row["up_won"])
            cache[slug] = bool(row["up_won"])
            continue
        todo.append((asset, mins, epoch, slug))
    fetched = 0
    for asset, mins, epoch, slug in todo:
        if epoch + mins * 60 > time.time() - 180:
            continue
        try:
            # Gamma indexes these windows by EVENT slug; /markets?slug= returns [] for them.
            payload = labdata.get(f"https://gamma-api.polymarket.com/events?slug={slug}")
        except Exception:
            continue
        event = payload[0] if isinstance(payload, list) and payload else None
        row = (event.get("markets") or [None])[0] if isinstance(event, dict) else None
        if not row or not row.get("closed"):
            continue
        try:
            prices = json.loads(row.get("outcomePrices") or "[]")
            up = float(prices[0]) > 0.5
        except Exception:
            continue
        out[(asset, mins, epoch)] = up
        cache[slug] = up
        fetched += 1
    if fetched:
        OUTCOMES_PATH.write_text(json.dumps(cache))
    print(f"outcomes: {len(out)} known ({fetched} fetched now), {len(keys) - len(out)} missing", flush=True)
    return out


def replay(groups, outcomes, strategies: list[Strategy]) -> dict[str, dict[str, Any]]:
    # BTC z per (mins, epoch, second) for cross-asset strategies
    btc_rows: dict[tuple[int, int], dict[int, list]] = {}
    for (asset, mins, epoch), rows in groups.items():
        if asset == "btc":
            btc_rows[(mins, epoch)] = {int(r[0]): r for r in rows}
    per: dict[str, dict[str, Any]] = {s.name: {"trades": [], "unfilled": 0, "skipped_size": 0, "bankroll": 50.0, "last_loss": {}} for s in strategies}
    order = sorted(groups, key=lambda k: (k[2], k[0], k[1]))
    for key in order:
        asset, mins, epoch = key
        up = outcomes.get(key)
        if up is None:
            continue
        rows = groups[key]
        end = epoch + mins * 60
        yes_hist: list[tuple[float, float]] = []
        oracle_hist: list[tuple[float, float]] = []
        states: list[State] = []
        for r in rows:
            t, _, _, _, raw, twap, open_ref, ya, yb, na, nb, ysz, sigma = r[:13]
            if not (0 < ya < 1 and 0 < na < 1) or open_ref <= 0:
                continue
            oracle_hist.append((float(t), float(raw)))
            btc_z = None
            if asset != "btc":
                b = btc_rows.get((mins, epoch), {}).get(int(t))
                if b and b[6] > 0 and b[12] > 0:
                    tt = max((end - t) / (mins * 60), 0.02)
                    btc_z = ((b[4] - b[6]) / b[6]) / (b[12] * math.sqrt(tt) + 1e-12)
            s = State(asset=asset, mins=mins, epoch=epoch, t=float(t), seconds_left=end - float(t), open_px=float(open_ref), spot_raw=float(raw), spot_twap=float(twap),
                      sigma=float(sigma), yes_ask=float(ya), yes_bid=float(yb), no_ask=float(na), no_bid=float(nb), yes_ask_size=float(ysz), yes_hist=yes_hist, oracle_hist=oracle_hist,
                      hour_utc=datetime.fromtimestamp(t, timezone.utc).hour, btc_z=btc_z)
            if len(r) >= 16 and r[13] is not None:
                s.cb_price, s.cb_bid_size, s.cb_ask_size, s.cb_ts = float(r[13]), float(r[14] or 0), float(r[15] or 0), float(t)
            yes_hist.append((float(t), s.market_p()))
            states.append(s)
        if not states:
            continue
        for strat in strategies:
            P = per[strat.name]
            if asset not in strat.assets or mins not in strat.windows:
                continue
            if strat.cooldown_s and epoch < P["last_loss"].get(asset, -1e9) + strat.cooldown_s:
                continue
            last_bucket = -1
            done = False
            taken_sides: set[str] = set()
            for i, s in enumerate(states):
                if done or (strat.multi_leg and len(taken_sides) == 2):
                    break
                if not strat.applies(s):
                    continue
                if strat.sample_every_s:
                    bucket = int(s.t - epoch) // int(strat.sample_every_s)
                    if bucket == last_bucket:
                        continue
                    last_bucket = bucket
                try:
                    orders = strat.quotes(s)
                except Exception:
                    orders = []
                for o in orders:
                  if o.side in taken_sides:
                    continue
                  if _fill_leg(strat, P, s, o, states, i, epoch, end, up, asset, mins):
                    taken_sides.add(o.side)
                    if not strat.multi_leg:
                        done = True
                        break
                  elif strat.multi_leg:
                    taken_sides.add(o.side)  # a quote that never filled: do not re-quote this side
                  else:
                    break
            continue
    return per


def _fill_leg(strat, P, s, o, states, i, epoch, end, up, asset, mins) -> bool:
    if True:
        if True:
            if True:
                if o.kind == "maker":
                    filled = None
                    for s2 in states[i + 1:]:
                        if s2.seconds_left < 20:
                            break
                        ask2 = s2.yes_ask if o.side == "YES" else s2.no_ask
                        if ask2 <= o.price + 1e-9:
                            filled = s2
                            break
                    if filled is None:
                        P["unfilled"] += 1
                        return False
                    price, fee, fill_t = o.price, 0.0, filled.t
                else:
                    if (s.yes_ask_size or 0) < FIXED / o.price:
                        P["skipped_size"] += 1
                        return False
                    if LATENCY_S > 0:
                        # Our order reaches the venue LATENCY_S after the decision (live median
                        # signal->order 0.6 s plus the 0.3 s watch tick). Fill only if the offer
                        # is still there then, at that later price if it is better.
                        later = next((s2 for s2 in states[i + 1:] if s2.t - s.t >= LATENCY_S), None)
                        if later is None:
                            P["unfilled"] += 1
                            return False
                        ask_later = later.yes_ask if o.side == "YES" else later.no_ask
                        if not (0 < ask_later <= o.price + 1e-9):
                            P["unfilled"] += 1
                            return False
                        o = Order(o.side, ask_later, o.fair, o.fair - ask_later - taker_fee_per_share(ask_later, FEE_RATE), kind=o.kind, note=o.note)
                        s = later
                    price, fee, fill_t = o.price, taker_fee_per_share(o.price, FEE_RATE), s.t
                won = up if o.side == "YES" else (not up)
                shares5 = FIXED / price
                cost5 = FIXED + shares5 * fee
                pnl5 = (shares5 - cost5) if won else -cost5
                stake = stake_for(strat.sizing, P["bankroll"], o.fair, price, fee) if P["bankroll"] >= 2 else 0.0
                shares = stake / price if stake else 0.0
                cost = stake + shares * fee
                pnl = ((shares - cost) if won else -cost) if stake else 0.0
                P["bankroll"] += pnl
                if not won:
                    P["last_loss"][asset] = end
                P["trades"].append({"epoch": epoch, "asset": asset, "mins": mins, "t_off": int(fill_t - epoch), "side": o.side, "kind": o.kind, "price": round(price, 3), "fair": round(o.fair, 4), "edge": round(o.edge, 4),
                                    "won": won, "ret": round(pnl5 / cost5, 4), "pnl5": round(pnl5, 4), "pnl": round(pnl, 4), "bankroll": round(P["bankroll"], 2), "z": round(s.z("raw"), 3), "hour": s.hour_utc, "sigma": round(s.sigma, 5), "seconds_left": round(s.seconds_left, 1), "market_p": round(s.market_p(), 3)})
                return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    groups = load_rows()
    print(f"tick windows: {len(groups)}", flush=True)
    outcomes = outcomes_for(list(groups))
    strategies = catalog()
    if args.only:
        keep = set(args.only.split(","))
        strategies = [s for s in strategies if s.name in keep]
    eps = [k[2] for k in groups if k in outcomes]
    days = (max(eps) - min(eps)) / 86400.0 if eps else 0.0
    per = replay(groups, outcomes, strategies)
    rows = []
    for strat in strategies:
        P = per[strat.name]
        res = {"trades": P["trades"], "unfilled": P["unfilled"], "final_bankroll": round(P["bankroll"], 2), "avg_slippage": None}
        r = summarize(strat.name, strat, res, days)
        r["skipped_size"] = P["skipped_size"]
        r["fills_below_085"] = sum(1 for t in P["trades"] if t["price"] < 0.85)
        rows.append(r)
        print(f"{strat.name:18s} n={r['n']:4d} win={(r['win_rate'] or 0):.3f} ret/$={(r['ret_mean'] or 0):+.4f} t={r['t']} pnl$5={r['pnl5']:+8.2f} fill={r['avg_price']} skip={P['skipped_size']} unf={P['unfilled']}", flush=True)
    rows.sort(key=lambda r: (r["t"] if r["t"] is not None else -99), reverse=True)
    out = {"generated_at": time.time(), "days": round(days, 2), "windows": len(groups), "windows_with_outcome": len(outcomes), "source": "1-second arena ticks, real books", "strategies": rows}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    (RESULTS_DIR / f"ticks_{stamp}.json").write_text(json.dumps(out, indent=1))
    if not args.only:
        LEADERBOARD_OUT.write_text(json.dumps({**{k: v for k, v in out.items() if k != "strategies"}, "strategies": [{k: v for k, v in r.items() if k != "daily"} for r in rows]}))
    print("written", RESULTS_DIR / f"ticks_{stamp}.json", flush=True)


if __name__ == "__main__":
    main()
