"""Data for the cryptoH_model study (hourly KXBTCD / KXETHD above/below ladders vs a spot-vol model).

1. Coinbase 1-minute candles (public, no key) cached to data/kalshi_lab/strategies/cryptoH_model/cb_<asset>.json as
   {"<minute start ts>": [low, high, open, close, volume]}.
2. Extra Kalshi hour-slots: the lab's fetch.py samples 4 hourly events a day (00/06/12/18 UTC). This adds the odd
   UTC hours (sampled by clock hour, never by outcome) for both series in ONE batched candlestick call per hour-slot:
   the 50 strikes of each series nearest the Coinbase spot at the window start (decision-time information only).
   Market metadata/results come from data/kalshi_lab/markets/<series>.jsonl (already on disk).
   Output: data/kalshi_lab/strategies/cryptoH_model/candles_<series>.jsonl (same format as the lab's candle files).

Usage: python -m lab.kalshi.strategies.cryptoH_model_data coinbase
       python -m lab.kalshi.strategies.cryptoH_model_data kalshi [max_calls]"""
from __future__ import annotations
import datetime as dt, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/cryptoH_model")
PROD = {"btc": "BTC-USD", "eth": "ETH-USD"}
SER = {"KXBTCD": "btc", "KXETHD": "eth"}
N_STRIKES = 50
WIN = 70 * 60


def iso(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cb(asset: str) -> dict[int, list]:
    p = OUT / f"cb_{asset}.json"
    return {int(k): v for k, v in json.loads(p.read_text()).items()} if p.exists() else {}


def coinbase(asset: str, start: int, end: int) -> None:
    have = load_cb(asset); cur = start - start % 60; calls = 0
    while cur < end:
        stop = min(cur + 300 * 60, end)
        if all(m in have for m in range(cur, stop, 60)):
            cur = stop; continue
        url = f"https://api.exchange.coinbase.com/products/{PROD[asset]}/candles?granularity=60&start={iso(cur)}&end={iso(stop - 60)}"
        for i in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=30) as r:
                    rows = json.loads(r.read().decode())
                break
            except Exception as e:
                print("retry", i, str(e)[:80], flush=True); time.sleep(3 * (i + 1)); rows = []
        for t, lo, hi, op, cl, v in rows:
            have[int(t)] = [lo, hi, op, cl, v]
        calls += 1; cur = stop; time.sleep(0.35)
    (OUT / f"cb_{asset}.json").write_text(json.dumps({str(k): v for k, v in sorted(have.items())}))
    print(f"coinbase {asset}: {len(have)} minutes ({calls} calls)", flush=True)


def spot_at(cb: dict[int, list], t: int) -> float | None:
    """Last Coinbase close known at time t (candle starting at m closes at m + 60)."""
    m = t - t % 60 - 60
    for k in range(m, m - 30 * 60, -60):
        if k in cb:
            return cb[k][3]
    return None


def kalshi(max_calls: int = 170) -> None:
    from lab.us.data_refresh import fetch, K
    from lab.kalshi.fetch import compact
    meta = {s: {} for s in SER}; have = {s: set() for s in SER}
    for s in SER:
        for l in open(f"data/kalshi_lab/markets/{s}.jsonl"):
            m = json.loads(l)
            if m["close"] - m["open"] == 3600:      # the hourly events only
                meta[s].setdefault(m["e"], []).append(m)
        for f in (Path(f"data/kalshi_lab/candles/{s}.jsonl"), OUT / f"candles_{s}.jsonl"):
            if f.exists():
                have[s] |= {json.loads(l)["t"] for l in f.open()}
    cbs = {a: load_cb(a) for a in PROD}
    slots = {}
    for s in SER:
        for e, ms in meta[s].items():
            T = ms[0]["close"]
            if dt.datetime.fromtimestamp(T, dt.timezone.utc).hour % 2 == 1 and not any(m["t"] in have[s] for m in ms):
                slots.setdefault(T, {})[s] = ms
    calls = 0; log = (OUT / "fetch_log.jsonl").open("a")
    for T in sorted(slots):
        if calls >= max_calls:
            break
        pick = []
        for s, ms in slots[T].items():
            S = spot_at(cbs[SER[s]], T - WIN)
            if S is None:
                continue
            pick += sorted(ms, key=lambda m: abs(m["floor"] - S))[:N_STRIKES]
        if not pick:
            continue
        lo, hi = T - WIN, T
        d = json.loads(fetch(f"{K}/markets/candlesticks?market_tickers={','.join(m['t'] for m in pick)}&start_ts={lo}&end_ts={hi}&period_interval=1", pace=1.15) or "{}")
        calls += 1
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
        log.write(json.dumps({"T": T, "asked": len(pick), "got": len(got), "ts": int(time.time())}) + "\n"); log.flush()
        if not got:
            print("empty response for", T, flush=True); continue
        for s in SER:
            with (OUT / f"candles_{s}.jsonl").open("a") as fh:
                for m in pick:
                    if m["series"] != s:
                        continue
                    c = [r for r in compact(got.get(m["t"], [])) if lo <= r[0] <= hi + 60]
                    fh.write(json.dumps({"t": m["t"], "c": c}) + "\n")
        if calls % 10 == 0:
            print(f"kalshi: {calls} calls, last slot {iso(T)}", flush=True)
    print(f"kalshi: done, {calls} calls, {len(slots)} odd-hour slots available", flush=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == "coinbase":
        a, b = int(dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc).timestamp()), int(dt.datetime(2026, 10, 8, 6, tzinfo=dt.timezone.utc).timestamp())
        for asset in PROD:
            coinbase(asset, a, b)
    elif sys.argv[1] == "kalshi":
        kalshi(int(sys.argv[2]) if len(sys.argv) > 2 else 170)
