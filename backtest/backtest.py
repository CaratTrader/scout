"""Backtest the crypto_lag strategy over historical BTC 5m up/down windows.

Replays the live bot's own scoring functions (p_up_from_move, realized_window_sigma,
taker_fee gates, Kelly sizing) against:
  - Binance 1m klines           -> signal inputs (open, spot, sigma)
  - CLOB prices-history         -> market price at decision time
  - Gamma resolved outcome      -> settlement (actual Chainlink result)

Known approximations vs live:
  - Market history is last-trade at ~1min fidelity; live bot uses a fresh order book
    with a 3s TTL. We model asks as last_trade +/- half_spread (sensitivity-tested)
    and require the last trade to be <= 120s old (else "stale book", skip).
  - Signal spot is Binance (the bot's own fallback path, oracle_noise=0.00015);
    live prefers the Chainlink stream, which is not available historically.
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout.config import settings_from_env  # noqa: E402
from scout.crypto_lag import (  # noqa: E402
    crypto_side_ok,
    in_entry_window,
    p_up_from_move,
    realized_window_sigma,
)
from scout.math_risk import stake_usd, taker_fee_per_share  # noqa: E402

BT = Path(__file__).resolve().parent
CACHE = BT / "cache"
CACHE.mkdir(exist_ok=True)
WINDOWS_CACHE = CACHE / "windows.jsonl"
KLINES_CACHE = CACHE / "klines.json"
RESULTS = BT / "results.json"

UA = {"User-Agent": "scout-backtest/0.1", "Accept": "application/json"}
DAYS = 30
DECISION_OFFSETS = (60, 120, 180, 240)  # minute ticks inside the entry window
MAX_QUOTE_AGE = 120.0  # last trade older than this = stale book, no entry
HALF_SPREADS = (0.005, 0.010, 0.015)  # ask model sensitivity


def get(url: str, timeout: int = 20, attempts: int = 4):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # includes IncompleteRead via Avast proxy
            last = exc
            time.sleep(0.6 * (i + 1))
    raise last


# ---------------------------------------------------------------- klines
def fetch_klines(start: int, end: int) -> dict[str, list]:
    if KLINES_CACHE.exists():
        data = json.loads(KLINES_CACHE.read_text())
        if data.get("start") <= start and data.get("end") >= end:
            return data
    opens: dict[str, float] = {}
    closes: dict[str, float] = {}
    cur = start
    calls = 0
    while cur < end:
        rows = get(
            "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
            f"&startTime={cur * 1000}&endTime={end * 1000}&limit=1000"
        )
        calls += 1
        if not rows:
            break
        for r in rows:
            minute = int(r[0] // 1000)
            opens[str(minute)] = float(r[1])
            closes[str(minute)] = float(r[4])
        cur = int(rows[-1][0] // 1000) + 60
        if calls % 10 == 0:
            print(f"klines: {calls} calls, up to {time.strftime('%m-%d %H:%M', time.gmtime(cur))}Z", flush=True)
    data = {"start": start, "end": end, "opens": opens, "closes": closes}
    KLINES_CACHE.write_text(json.dumps(data))
    print(f"klines cached: {len(opens)} minutes", flush=True)
    return data


# ---------------------------------------------------------------- windows
def fetch_window(epoch: int) -> dict | None:
    slug = f"btc-updown-5m-{epoch}"
    try:
        evs = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
    except Exception as exc:
        return {"epoch": epoch, "error": f"events:{type(exc).__name__}"}
    if not evs or not evs[0].get("markets"):
        return {"epoch": epoch, "error": "no_market"}
    m = evs[0]["markets"][0]
    if not m.get("closed"):
        return {"epoch": epoch, "error": "not_closed"}
    try:
        prices = json.loads(m.get("outcomePrices") or "[]")
        up_won = float(prices[0]) > 0.5
        token = json.loads(m["clobTokenIds"])[0]
    except Exception:
        return {"epoch": epoch, "error": "bad_meta"}
    try:
        hist = get(
            "https://clob.polymarket.com/prices-history?market=" + token
            + f"&startTs={epoch - 120}&endTs={epoch + 300}&fidelity=1"
        )
        points = [(int(p["t"]), float(p["p"])) for p in hist.get("history", [])]
    except Exception as exc:
        return {"epoch": epoch, "error": f"history:{type(exc).__name__}"}
    return {"epoch": epoch, "up_won": up_won, "points": points}


def fetch_all_windows(epochs: list[int]) -> dict[int, dict]:
    done: dict[int, dict] = {}
    if WINDOWS_CACHE.exists():
        for line in WINDOWS_CACHE.read_text().splitlines():
            try:
                row = json.loads(line)
                done[int(row["epoch"])] = row
            except Exception:
                continue
    todo = [e for e in epochs if e not in done]
    print(f"windows: {len(done)} cached, {len(todo)} to fetch", flush=True)
    if not todo:
        return done
    written = 0
    with WINDOWS_CACHE.open("a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(fetch_window, e): e for e in todo}
            for fut in as_completed(futures):
                row = fut.result()
                if row is None:
                    continue
                out.write(json.dumps(row) + "\n")
                done[int(row["epoch"])] = row
                written += 1
                if written % 250 == 0:
                    out.flush()
                    ok = sum(1 for r in done.values() if "up_won" in r)
                    print(f"windows: {written}/{len(todo)} fetched ({ok} usable)", flush=True)
    return done


# ---------------------------------------------------------------- replay
def last_trade_at(points: list, t: int) -> tuple[float, float] | None:
    """(price, age_seconds) of the last trade at or before t."""
    best = None
    for ts, p in points:
        if ts <= t:
            best = (p, float(t - ts))
        else:
            break
    return best


def replay(windows: dict[int, dict], kl: dict, half_spread: float, settings) -> dict:
    opens, closes = kl["opens"], kl["closes"]
    bankroll = 50.0
    peak = 50.0
    max_dd = 0.0
    trades = []
    skip = {"no_data": 0, "stale_book": 0, "no_edge": 0, "gate": 0, "no_klines": 0}
    for epoch in sorted(windows):
        w = windows[epoch]
        if "up_won" not in w:
            skip["no_data"] += 1
            continue
        S, E = epoch, epoch + 300
        open_px = opens.get(str(S))
        recent = [closes[str(S - 60 * i)] for i in range(31, 0, -1) if str(S - 60 * i) in closes]
        if not open_px or len(recent) < 6:
            skip["no_klines"] += 1
            continue
        sigma = realized_window_sigma(recent, 300)
        entered = False
        for off in DECISION_OFFSETS:
            t = S + off
            seconds_left = E - t
            if not in_entry_window(seconds_left, 300, now=t, start=S):
                continue
            spot = opens.get(str(t))
            if not spot:
                continue
            lt = last_trade_at(w["points"], t)
            if lt is None or lt[1] > MAX_QUOTE_AGE:
                continue
            yes_last = lt[0]
            yes_ask = min(0.99, yes_last + half_spread)
            yes_bid = max(0.01, yes_last - half_spread)
            no_ask = min(0.99, (1.0 - yes_last) + half_spread)
            chg = (spot - open_px) / open_px
            raw_p = p_up_from_move(chg, seconds_left, 300, sigma_full=sigma, oracle_noise=0.00015)
            market_p = min(0.99, max(0.01, (yes_ask + yes_bid) / 2.0))
            p_up = settings.crypto_model_weight * raw_p + (1 - settings.crypto_model_weight) * market_p
            fee_yes = taker_fee_per_share(yes_ask, settings.taker_fee_rate)
            fee_no = taker_fee_per_share(no_ask, settings.taker_fee_rate)
            up_edge = p_up - yes_ask - fee_yes
            down_edge = (1.0 - p_up) - no_ask - fee_no
            side = None
            if crypto_side_ok(yes_ask, p_up) and up_edge >= settings.crypto_min_edge:
                side, ask, fair, edge, fee = "YES", yes_ask, p_up, up_edge, fee_yes
            elif crypto_side_ok(no_ask, 1.0 - p_up) and down_edge >= settings.crypto_min_edge:
                side, ask, fair, edge, fee = "NO", no_ask, 1.0 - p_up, down_edge, fee_no
            if side is None:
                continue
            stake = stake_usd(
                fair, ask, bankroll,
                cap=settings.crypto_bankroll_frac,
                kelly_mult=settings.crypto_kelly_mult,
            )
            stake = max(settings.min_crypto_stake, min(settings.max_crypto_stake, stake))
            if stake > bankroll:
                break
            shares = stake / ask
            fee_paid = shares * fee
            won = w["up_won"] if side == "YES" else not w["up_won"]
            pnl = (shares - stake - fee_paid) if won else (-stake - fee_paid)
            bankroll += pnl
            peak = max(peak, bankroll)
            max_dd = max(max_dd, peak - bankroll)
            trades.append({
                "epoch": epoch, "t_off": off, "side": side, "ask": round(ask, 3),
                "fair": round(fair, 3), "edge": round(edge, 4), "stake": round(stake, 2),
                "won": won, "pnl": round(pnl, 4), "bankroll": round(bankroll, 2),
                "chg_bps": round(chg * 10000, 1), "quote_age": round(lt[1], 1),
            })
            entered = True
            break
        if not entered:
            skip["no_edge"] += 1
        if bankroll < 2.0:
            break
    wins = sum(1 for t in trades if t["won"])
    return {
        "half_spread": half_spread,
        "trades": len(trades),
        "wins": wins,
        "win_rate": round(wins / len(trades), 4) if trades else None,
        "net_pnl": round(bankroll - 50.0, 2),
        "final_bankroll": round(bankroll, 2),
        "max_drawdown": round(max_dd, 2),
        "avg_stake": round(sum(t["stake"] for t in trades) / len(trades), 2) if trades else None,
        "skips": skip,
        "trade_log": trades,
    }


def main() -> None:
    settings = settings_from_env()
    now = int(time.time())
    end = now - (now % 300) - 3600  # stop 1h back: fully resolved
    start = end - DAYS * 86400
    epochs = list(range(start, end, 300))
    print(f"backtest: {len(epochs)} five-minute windows over {DAYS} days", flush=True)

    kl = fetch_klines(start - 40 * 60, end + 600)
    windows = fetch_all_windows(epochs)
    usable = sum(1 for w in windows.values() if "up_won" in w)
    print(f"usable windows: {usable}/{len(epochs)}", flush=True)

    results = []
    for hs in HALF_SPREADS:
        res = replay(windows, kl, hs, settings)
        results.append(res)
        print(
            f"half_spread={hs:.3f}: trades={res['trades']} win_rate={res['win_rate']} "
            f"net_pnl=${res['net_pnl']} final=${res['final_bankroll']} maxDD=${res['max_drawdown']}",
            flush=True,
        )
    RESULTS.write_text(json.dumps({
        "generated_at": now, "days": DAYS, "windows_total": len(epochs),
        "windows_usable": usable, "runs": results,
    }, indent=1))
    print("results written:", RESULTS, flush=True)


if __name__ == "__main__":
    main()
