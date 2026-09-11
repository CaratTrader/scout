"""Historical data for the lab: Polymarket up/down windows (outcome + CLOB YES price
history at 1-minute fidelity) and 1-minute klines per asset.

Files (lab/cache/):
  windows_{asset}_{mins}m.jsonl   one row per window: epoch, up_won, points[(ts, yes_price)]
  klines_{asset}.json             {"start","end","opens":{minute:px},"closes":{minute:px}}

The BTC 5m set reuses backtest/cache (30 days) and is extended forward to now.
Run:  .venv/bin/python -m lab.data --assets btc,eth,sol --windows 5,15 --days 10
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "lab" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
LEGACY_WINDOWS = ROOT / "backtest" / "cache" / "windows.jsonl"
LEGACY_KLINES = ROOT / "backtest" / "cache" / "klines.json"
UA = {"User-Agent": "scout-lab/0.1", "Accept": "application/json"}
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}
KLINE_HOSTS = ("https://api.binance.us", "https://api.binance.com")
_RATE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
MAX_RPS = 10.0


def _throttle() -> None:
    with _RATE_LOCK:
        now = time.time()
        wait = _LAST_CALL[0] + 1.0 / MAX_RPS - now
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()


def get(url: str, timeout: int = 20, attempts: int = 4):
    last = None
    for i in range(attempts):
        try:
            _throttle()
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (i + 1))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------- klines
def klines_path(asset: str) -> Path:
    return CACHE / f"klines_{asset}.json"


def load_klines(asset: str) -> dict:
    p = klines_path(asset)
    if p.exists():
        return json.loads(p.read_text())
    if asset == "btc" and LEGACY_KLINES.exists():
        return json.loads(LEGACY_KLINES.read_text())
    return {"start": 0, "end": 0, "opens": {}, "closes": {}}


def fetch_klines(asset: str, start: int, end: int) -> dict:
    """Fill [start, end) into the asset's kline cache (minute grid) from Coinbase
    (liquid, a Chainlink constituent; binance.us prints flat zero-volume minutes)."""
    from datetime import datetime, timezone

    data = load_klines(asset)
    opens, closes = data["opens"], data["closes"]
    product = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD"}[asset]
    cur = start - (start % 60)
    calls = 0
    while cur < end:
        if str(cur) in opens and str(cur + 60) in opens:
            cur += 60
            continue
        chunk_end = min(end, cur + 300 * 60)
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            rows = get(f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=60&start={iso(cur)}&end={iso(chunk_end)}")
        except Exception as exc:
            print(f"klines {asset}: coinbase error {type(exc).__name__} at {iso(cur)}", flush=True)
            rows = []
        calls += 1
        for r in rows or []:
            minute = int(r[0])
            opens[str(minute)] = float(r[3])
            closes[str(minute)] = float(r[4])
        cur = chunk_end
    data["start"] = min(data.get("start") or start, start)
    data["end"] = max(data.get("end") or end, end)
    klines_path(asset).write_text(json.dumps(data))
    print(f"klines {asset}: {len(opens)} minutes cached ({calls} calls)", flush=True)
    return data


# ---------------------------------------------------------------- windows
def windows_path(asset: str, mins: int) -> Path:
    return CACHE / f"windows_{asset}_{mins}m.jsonl"


def fetch_window(asset: str, mins: int, epoch: int) -> dict:
    slug = f"{asset}-updown-{mins}m-{epoch}"
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
            + f"&startTs={epoch - 120}&endTs={epoch + mins * 60}&fidelity=1"
        )
        points = [(int(p["t"]), float(p["p"])) for p in hist.get("history", [])]
    except Exception as exc:
        return {"epoch": epoch, "error": f"history:{type(exc).__name__}"}
    return {"epoch": epoch, "up_won": up_won, "points": points, "market_id": str(m.get("id") or ""), "volume": m.get("volumeNum")}


def load_windows(asset: str, mins: int) -> dict[int, dict]:
    done: dict[int, dict] = {}
    paths = [windows_path(asset, mins)]
    if asset == "btc" and mins == 5:
        paths.insert(0, LEGACY_WINDOWS)
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            e = int(row.get("epoch") or 0)
            if e and ("up_won" in row or e not in done):
                done[e] = row
    return done


def fetch_windows(asset: str, mins: int, epochs: list[int], workers: int = 6) -> dict[int, dict]:
    done = load_windows(asset, mins)
    todo = [e for e in epochs if e not in done or "up_won" not in done[e] and done[e].get("error") in {"not_closed", None}]
    todo = [e for e in todo if not (e in done and "up_won" in done[e])]
    print(f"windows {asset} {mins}m: {sum(1 for r in done.values() if 'up_won' in r)} usable cached, {len(todo)} to fetch", flush=True)
    if not todo:
        return done
    written = 0
    out_path = windows_path(asset, mins)
    with out_path.open("a", encoding="utf-8") as out:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_window, asset, mins, e): e for e in todo}
            for fut in as_completed(futures):
                row = fut.result()
                out.write(json.dumps(row) + "\n")
                done[int(row["epoch"])] = row
                written += 1
                if written % 200 == 0:
                    out.flush()
                    ok = sum(1 for r in done.values() if "up_won" in r)
                    print(f"windows {asset} {mins}m: {written}/{len(todo)} fetched ({ok} usable)", flush=True)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol")
    ap.add_argument("--windows", default="5,15")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--btc5-extend", action="store_true", help="extend the legacy 30d BTC 5m cache to now")
    args = ap.parse_args()
    now = int(time.time())
    end = now - (now % 300) - 3600
    assets = [a for a in args.assets.split(",") if a]
    wins = [int(w) for w in args.windows.split(",") if w]
    for asset in assets:
        start = end - args.days * 86400
        fetch_klines(asset, start - 40 * 60, end + 900)
        for mins in wins:
            step = mins * 60
            first = start - (start % step)
            epochs = list(range(first, end - (end % step), step))
            if asset == "btc" and mins == 5 and args.btc5_extend:
                legacy = load_windows("btc", 5)
                if legacy:
                    epochs = [e for e in epochs if e not in legacy] + [e for e in range(max(legacy) + 300, end - (end % 300), 300)]
                    epochs = sorted(set(epochs))
                    fetch_klines("btc", min(epochs) - 40 * 60, end + 900)
            fetch_windows(asset, mins, epochs)
    print("done", flush=True)


if __name__ == "__main__":
    main()
