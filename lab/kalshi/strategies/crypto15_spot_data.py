"""Coinbase 1-minute candles for the crypto15_spot study (public endpoint, no key).
Cache: data/kalshi_lab/strategies/crypto15_spot/cb_<asset>.json  {minute_start_ts: [open, high, low, close, volume]}
Usage: python -m lab.kalshi.strategies.crypto15_spot_data [assets] (defaults: btc eth sol xrp doge, range = Kalshi data range)"""
from __future__ import annotations
import datetime as dt, json, sys, time, urllib.request, urllib.error
from pathlib import Path

OUT = Path("data/kalshi_lab/strategies/crypto15_spot")
PRODUCT = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD", "doge": "DOGE-USD"}
SERIES = {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M", "xrp": "KXXRP15M", "doge": "KXDOGE15M"}


def get(url: str, tries: int = 6):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            time.sleep(3 * (i + 1) if e.code == 429 else 2)
        except Exception:
            time.sleep(4 * (i + 1))
    return None


def load(asset: str) -> dict[int, list[float]]:
    p = OUT / f"cb_{asset}.json"
    return {int(k): v for k, v in json.loads(p.read_text()).items()} if p.exists() else {}


def fill(asset: str, start: int, end: int) -> dict[int, list[float]]:
    data = load(asset); iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = start - start % 60; calls = 0
    while cur < end:
        ce = min(end, cur + 300 * 60)
        if all(m in data for m in range(cur, ce, 60 * 30)) and (ce - 60) in data:
            cur = ce; continue
        rows = get(f"https://api.exchange.coinbase.com/products/{PRODUCT[asset]}/candles?granularity=60&start={iso(cur)}&end={iso(ce - 60)}")
        calls += 1
        for r in rows or []:
            data[int(r[0])] = [float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5])]
        cur = ce; time.sleep(0.35)
    (OUT / f"cb_{asset}.json").write_text(json.dumps({str(k): v for k, v in sorted(data.items())}))
    print(f"{asset}: {len(data)} minutes cached ({calls} calls)", flush=True)
    return data


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    assets = sys.argv[1:] or list(PRODUCT)
    for a in assets:
        ms = [json.loads(l) for l in open(f"data/kalshi_lab/markets/{SERIES[a]}.jsonl")]
        lo = min(m["open"] for m in ms) - 3 * 3600; hi = max(m["close"] for m in ms) + 120
        fill(a, lo, hi)
