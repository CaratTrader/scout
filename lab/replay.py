"""Replay every lab strategy over cached windows with one shared fill model.

  .venv/bin/python -m lab.replay                 # all assets/windows in lab/cache + legacy BTC 5m
  .venv/bin/python -m lab.replay --half-spread 0.015 --since 2026-08-07

Decision grid: one State per minute inside the window (the CLOB history is 1-minute
fidelity), one trade per window per strategy (first trigger). Taker fills at the ask
plus fee; maker orders fill if a later print trades through the limit (fee 0).
Outcomes are real Gamma resolutions. Metrics are reported on flat $5 stakes so
strategies compare on signal quality, plus a compounding run with each strategy's
own sizing.
"""
from __future__ import annotations

import argparse
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
from lab.strategies import FEE_RATE, Order, State, Strategy, catalog  # noqa: E402
from scout.crypto_lag import realized_window_sigma  # noqa: E402
from scout.math_risk import kelly_buy, taker_fee_per_share  # noqa: E402

RESULTS_DIR = ROOT / "lab" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LEADERBOARD_OUT = ROOT / "data" / "lab" / "backtest_leaderboard.json"
TWAP_SWITCH = int(datetime(2026, 8, 7, tzinfo=timezone.utc).timestamp())
MULTIPLE_TEST_T = 3.0  # ~ max of 30 null t-stats; the bar a winner must clear


def stake_for(rule: str, bankroll: float, fair: float, price: float, fee: float) -> float:
    if rule == "fixed5":
        return 5.0 if bankroll >= 5 else max(0.0, bankroll)
    mult = 0.5 if rule == "kelly" else 0.25
    eff = min(0.999999, price + fee)
    frac = min(0.40, kelly_buy(fair, eff) * mult)
    return round(min(5.0, max(2.0, bankroll * frac), bankroll), 2)


def last_print(points: list, t: float) -> tuple[float, float] | None:
    best = None
    for ts, p in points:
        if ts <= t:
            best = (p, t - ts)
        else:
            break
    return best


def next_print(points: list, t: float, horizon: float = 90.0) -> tuple[float, float] | None:
    """(price, delay) of the first print strictly after t — the price a taker could
    actually get once the book has repriced (last prints are up to a minute stale)."""
    for ts, p in points:
        if ts > t:
            return (p, ts - t) if ts - t <= horizon else None
    return None


def build_states(asset: str, mins: int, windows: dict[int, dict], kl: dict, btc_kl: dict | None, half_spread: float):
    """Yield (window, [State per decision minute]) for every usable window."""
    opens, closes = kl["opens"], kl["closes"]
    step = mins * 60
    for epoch in sorted(windows):
        w = windows[epoch]
        if "up_won" not in w:
            continue
        open_px = opens.get(str(epoch))
        recent = [closes[str(epoch - 60 * i)] for i in range(31, 0, -1) if str(epoch - 60 * i) in closes]
        if not open_px or len(recent) < 6:
            continue
        sigma = realized_window_sigma(recent, step)
        btc_sigma = None
        btc_open = None
        if btc_kl is not None and asset != "btc":
            btc_open = btc_kl["opens"].get(str(epoch))
            rec_b = [btc_kl["closes"][str(epoch - 60 * i)] for i in range(31, 0, -1) if str(epoch - 60 * i) in btc_kl["closes"]]
            if btc_open and len(rec_b) >= 6:
                btc_sigma = realized_window_sigma(rec_b, step)
        states = []
        for off in range(60, step, 60):
            t = epoch + off
            spot = opens.get(str(t))
            prev = opens.get(str(t - 60))
            if not spot or not prev:
                continue
            lp = last_print(w["points"], t)
            if lp is None or lp[1] > 120:
                continue
            yes_last = lp[0]
            yes_ask = min(0.99, yes_last + half_spread)
            yes_bid = max(0.01, yes_last - half_spread)
            no_ask = min(0.99, (1.0 - yes_last) + half_spread)
            no_bid = max(0.01, (1.0 - yes_last) - half_spread)
            btc_z = None
            if btc_open and btc_sigma:
                b_spot = btc_kl["opens"].get(str(t))
                if b_spot:
                    tt = max((epoch + step - t) / step, 0.02)
                    btc_z = ((b_spot - btc_open) / btc_open) / (btc_sigma * math.sqrt(tt) + 1e-12)
            states.append(
                State(
                    asset=asset, mins=mins, epoch=epoch, t=float(t), seconds_left=float(epoch + step - t),
                    open_px=float(open_px), spot_raw=float(spot), spot_twap=(float(prev) + float(spot)) / 2.0,
                    sigma=sigma, yes_ask=yes_ask, yes_bid=yes_bid, no_ask=no_ask, no_bid=no_bid,
                    quote_age=lp[1], yes_hist=[(float(ts), float(p)) for ts, p in w["points"] if ts <= t],
                    hour_utc=datetime.fromtimestamp(t, timezone.utc).hour, btc_z=btc_z,
                )
            )
        if states:
            yield w, states


def maker_filled(order: Order, w: dict, t: float, end: float) -> bool:
    for ts, p in w["points"]:
        if ts <= t or ts > end - 5:
            continue
        if order.side == "YES" and p <= order.price + 1e-9:
            return True
        if order.side == "NO" and (1.0 - p) <= order.price + 1e-9:
            return True
    return False


EXEC_MODE = "next"  # next: fill at the first print after the decision (conservative) | last: stale last print (optimistic)
HALF_SPREAD = 0.01
MAX_SLIP = 1.0  # default: accept whatever the next print says (no calm-window selection); pass --max-slip 0.05 for the capped variant


def run_strategy(strat: Strategy, datasets: list[tuple[str, int, list]], *, fixed_stake: float = 5.0) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    bankroll = 50.0
    unfilled = 0
    slippage: list[float] = []
    for asset, mins, rows in datasets:
        if asset not in strat.assets or mins not in strat.windows:
            continue
        for w, states in rows:
            for s in states:
                if not strat.applies(s):
                    continue
                order = strat.decide(s)
                if order is None:
                    continue
                end = s.epoch + s.window_s
                if order.kind == "maker":
                    if not maker_filled(order, w, s.t, end):
                        unfilled += 1
                        break
                    fee = 0.0
                else:
                    if EXEC_MODE == "next":
                        nxt = next_print(w["points"], s.t)
                        if nxt is None:
                            unfilled += 1
                            break
                        yes_px = nxt[0]
                        exec_px = min(0.99, yes_px + HALF_SPREAD) if order.side == "YES" else min(0.99, (1.0 - yes_px) + HALF_SPREAD)
                        slippage.append(exec_px - order.price)
                        if exec_px > order.price + MAX_SLIP:
                            unfilled += 1  # the book moved away by more than the slip cap: no fill
                            break
                        order = Order(order.side, exec_px, order.fair, order.fair - exec_px - taker_fee_per_share(exec_px, FEE_RATE), note=order.note)
                    fee = taker_fee_per_share(order.price, FEE_RATE)
                won = w["up_won"] if order.side == "YES" else (not w["up_won"])
                shares5 = fixed_stake / order.price
                cost5 = fixed_stake + shares5 * fee
                pnl5 = (shares5 - cost5) if won else -cost5
                stake = stake_for(strat.sizing, bankroll, order.fair, order.price, fee) if bankroll >= 2.0 else 0.0
                shares = stake / order.price if stake > 0 else 0.0
                cost = stake + shares * fee
                pnl = ((shares - cost) if won else -cost) if stake > 0 else 0.0
                bankroll += pnl
                trades.append(
                    {
                        "epoch": s.epoch, "asset": asset, "mins": mins, "t_off": int(s.t - s.epoch), "side": order.side,
                        "kind": order.kind, "price": round(order.price, 3), "fair": round(order.fair, 4), "edge": round(order.edge, 4),
                        "won": won, "ret": round(pnl5 / cost5, 4), "pnl5": round(pnl5, 4), "pnl": round(pnl, 4), "bankroll": round(bankroll, 2),
                        "z": round(s.z("raw"), 3), "hour": s.hour_utc, "sigma": round(s.sigma, 5),
                    }
                )
                break  # one trade per window
    return {"trades": trades, "unfilled": unfilled, "final_bankroll": round(bankroll, 2),
            "avg_slippage": round(statistics.fmean(slippage), 4) if slippage else None}


def summarize(name: str, strat: Strategy, res: dict[str, Any], days: float) -> dict[str, Any]:
    tr = res["trades"]
    n = len(tr)
    rets = [t["ret"] for t in tr]
    pnl5 = [t["pnl5"] for t in tr]

    def tstat(xs):
        if len(xs) < 3:
            return None
        m, sd = statistics.fmean(xs), statistics.stdev(xs)
        return round(m / (sd / math.sqrt(len(xs))), 2) if sd > 0 else None

    def stats(rows):
        if not rows:
            return {"n": 0}
        r = [x["ret"] for x in rows]
        return {"n": len(rows), "win": round(sum(x["won"] for x in rows) / len(rows), 3), "ret": round(statistics.fmean(r), 4), "t": tstat(r), "pnl5": round(sum(x["pnl5"] for x in rows), 2)}

    cum = peak = dd = 0.0
    for p in pnl5:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    gw = sum(p for p in pnl5 if p > 0)
    gl = -sum(p for p in pnl5 if p < 0)
    brier = statistics.fmean((t["fair"] - (1.0 if t["won"] else 0.0)) ** 2 for t in tr) if tr else None
    mid = tr[len(tr) // 2]["epoch"] if tr else 0
    first, second = [t for t in tr if t["epoch"] < mid], [t for t in tr if t["epoch"] >= mid]
    pre, post = [t for t in tr if t["epoch"] < TWAP_SWITCH], [t for t in tr if t["epoch"] >= TWAP_SWITCH]
    t_all = tstat(rets)
    h1, h2 = stats(first), stats(second)
    passes = bool(t_all is not None and t_all >= MULTIPLE_TEST_T and (h1.get("ret") or 0) > 0 and (h2.get("ret") or 0) > 0 and n >= 100)
    return {
        "name": name, "family": strat.family, "desc": strat.desc, "sizing": strat.sizing,
        "assets": list(strat.assets), "windows": list(strat.windows),
        "n": n, "unfilled": res["unfilled"], "avg_slippage": res.get("avg_slippage"), "trades_per_day": round(n / days, 1) if days else None,
        "win_rate": round(sum(t["won"] for t in tr) / n, 4) if n else None,
        "ret_mean": round(statistics.fmean(rets), 4) if rets else None,
        "ret_sd": round(statistics.stdev(rets), 4) if len(rets) > 1 else None,
        "t": t_all, "pnl5": round(sum(pnl5), 2), "max_dd5": round(dd, 2),
        "profit_factor": round(gw / gl, 3) if gl > 0 else None,
        "brier": round(brier, 4) if brier is not None else None,
        "avg_price": round(statistics.fmean(t["price"] for t in tr), 3) if tr else None,
        "avg_edge": round(statistics.fmean(t["edge"] for t in tr), 4) if tr else None,
        "final_bankroll": res["final_bankroll"],
        "half1": h1, "half2": h2, "pre_twap": stats(pre), "post_twap": stats(post),
        "passes_bar": passes,
        "daily": _daily(tr),
    }


def _daily(tr: list[dict[str, Any]]) -> list[dict[str, Any]]:
    d: dict[str, float] = defaultdict(float)
    for t in tr:
        d[datetime.fromtimestamp(t["epoch"], timezone.utc).date().isoformat()] += t["pnl5"]
    return [{"date": k, "pnl5": round(v, 2)} for k, v in sorted(d.items())]


def load_datasets(assets: list[str], wins: list[int], half_spread: float, since: int | None) -> tuple[list, float]:
    datasets = []
    btc_kl = labdata.load_klines("btc")
    lo = hi = None
    for asset in assets:
        kl = labdata.load_klines(asset)
        for mins in wins:
            windows = labdata.load_windows(asset, mins)
            if since:
                windows = {e: w for e, w in windows.items() if e >= since}
            rows = list(build_states(asset, mins, windows, kl, btc_kl if asset != "btc" else None, half_spread))
            if not rows:
                continue
            eps = [w["epoch"] for w, _ in rows]
            lo = min(lo, min(eps)) if lo else min(eps)
            hi = max(hi, max(eps)) if hi else max(eps)
            datasets.append((asset, mins, rows))
            print(f"dataset {asset} {mins}m: {len(rows)} windows", flush=True)
    days = ((hi - lo) / 86400.0) if lo and hi else 0.0
    return datasets, days


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol")
    ap.add_argument("--windows", default="5,15")
    ap.add_argument("--half-spread", type=float, default=0.01)
    ap.add_argument("--since", default="", help="ISO date; e.g. 2026-08-07 for the TWAP regime only")
    ap.add_argument("--only", default="", help="comma list of strategy names")
    ap.add_argument("--exec", default="next", choices=["next", "last"], help="taker fill price: next print (honest) or last print (stale, optimistic)")
    ap.add_argument("--max-slip", type=float, default=1.0)
    ap.add_argument("--tag", default="", help="label for the output file")
    args = ap.parse_args()
    global EXEC_MODE, HALF_SPREAD, MAX_SLIP
    EXEC_MODE, HALF_SPREAD, MAX_SLIP = args.exec, args.half_spread, args.max_slip
    since = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()) if args.since else None
    datasets, days = load_datasets([a for a in args.assets.split(",") if a], [int(w) for w in args.windows.split(",") if w], args.half_spread, since)
    strategies = catalog()
    if args.only:
        keep = set(args.only.split(","))
        strategies = [s for s in strategies if s.name in keep]
    rows = []
    t0 = time.time()
    for strat in strategies:
        res = run_strategy(strat, datasets)
        rows.append(summarize(strat.name, strat, res, days))
        r = rows[-1]
        print(f"{strat.name:18s} n={r['n']:5d} win={r['win_rate'] or 0:.3f} ret/$={r['ret_mean'] if r['ret_mean'] is not None else 0:+.4f} t={r['t']} pnl$5={r['pnl5']:+8.2f} dd={r['max_dd5']:.1f} brier={r['brier']} slip={r['avg_slippage']} h1={r['half1'].get('ret')} h2={r['half2'].get('ret')} {'PASS' if r['passes_bar'] else ''}", flush=True)
    rows.sort(key=lambda r: (r["t"] if r["t"] is not None else -99), reverse=True)
    out = {
        "generated_at": time.time(), "half_spread": args.half_spread, "exec": args.exec, "max_slip": args.max_slip, "tag": args.tag, "since": args.since or None, "days": round(days, 1),
        "datasets": [{"asset": a, "mins": m, "windows": len(rows_)} for a, m, rows_ in datasets],
        "multiple_test_t": MULTIPLE_TEST_T, "strategies": rows, "elapsed_s": round(time.time() - t0, 1),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    path = RESULTS_DIR / f"replay_{stamp}{('_' + args.tag) if args.tag else ''}.json"
    path.write_text(json.dumps(out, indent=1))
    if not args.tag or args.tag == "main":
        LEADERBOARD_OUT.parent.mkdir(parents=True, exist_ok=True)
        LEADERBOARD_OUT.write_text(json.dumps({k: v for k, v in out.items() if k != "strategies"} | {"strategies": [{k: v for k, v in r.items() if k != "daily"} for r in rows]}))
    print("written", path, "and", LEADERBOARD_OUT, flush=True)


if __name__ == "__main__":
    main()
