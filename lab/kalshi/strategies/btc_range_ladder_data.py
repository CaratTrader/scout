"""Data for the btc_range_ladder study: hourly crypto RANGE ladders (KXBTC $100 buckets, KXETH $5 buckets) and the
15-minute coin race (KXCRYPTOLEAD15M: which of BTC/ETH/SOL/XRP/HYPE has the highest 15-minute return).

Outputs (all under data/kalshi_lab/strategies/btc_range_ladder/):
  cb_<asset>.json            Coinbase 1-minute candles {"<minute start ts>": [low, high, open, close, volume]} (seeded from the
                             cryptoH_model / crypto15_spot caches, extended with public Coinbase calls)
  candles_KXBTC.jsonl,
  candles_KXETH.jsonl        lab candle format {"t", "e", "c": [[end_ts, yes_ask, yes_bid, ask_lo, bid_hi, vol], ...]}
  lead_markets.jsonl         settled coin-race markets {"t","e","open","close","coin","result","vol"}
  candles_LEAD.jsonl         coin-race candles

Range events: the bucket tickers are built from the KXBTCD/KXETHD ladder on disk (same CF Benchmarks 60-second BRTI average,
same close; strikes align: above/below strike X.99 <-> bucket [X+0.01, next strike]); verified on KXBTC-26OCT0800 and
KXETH-26OCT0800. Which buckets to fetch is decided from the Coinbase spot at the event OPEN (decision-time information).
Two consecutive hourly events share one batched candlestick call (82 markets x 121 minutes < 10,000).
Usage: python -m lab.kalshi.strategies.btc_range_ladder_data coinbase | range BTC|ETH max_calls days | lead_markets days | lead_candles max_calls days"""
from __future__ import annotations
import datetime as dt, json, shutil, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/btc_range_ladder")
PROD = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD", "hype": "HYPE-USD"}
SEEDS = {"btc": "data/kalshi_lab/strategies/cryptoH_model/cb_btc.json", "eth": "data/kalshi_lab/strategies/cryptoH_model/cb_eth.json",
         "sol": "data/kalshi_lab/strategies/crypto15_spot/cb_sol.json", "xrp": "data/kalshi_lab/strategies/crypto15_spot/cb_xrp.json"}
RANGE = {"BTC": ("KXBTC", "KXBTCD", "btc", 100.0, 50), "ETH": ("KXETH", "KXETHD", "eth", 5.0, 2.5)}
PAIR_HOURS = (0, 6, 12, 18)     # UTC hours with full above/below ladders on disk (lab); hour+1 is covered by cryptoH_model
N_BUCKETS = 41
CALLS = OUT / "kalshi_calls.log"


def iso(t: int) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cb(asset: str) -> dict[int, list]:
    p = OUT / f"cb_{asset}.json"
    return {int(k): v for k, v in json.loads(p.read_text()).items()} if p.exists() else {}


def spot_at(cb: dict[int, list], t: int) -> float | None:
    """Last Coinbase close known at time t (the candle starting at m closes at m + 60)."""
    m = t - t % 60 - 60
    for k in range(m, m - 30 * 60, -60):
        if k in cb:
            return cb[k][3]
    return None


def coinbase(asset: str, start: int, end: int) -> None:
    p = OUT / f"cb_{asset}.json"
    if not p.exists() and asset in SEEDS and Path(SEEDS[asset]).exists():
        shutil.copy(SEEDS[asset], p)
    have = load_cb(asset); cur = start - start % 60; calls = 0
    while cur < end:
        stop = min(cur + 300 * 60, end)
        if sum(m in have for m in range(cur, stop, 60)) >= 0.97 * (stop - cur) / 60:
            cur = stop; continue
        url = f"https://api.exchange.coinbase.com/products/{PROD[asset]}/candles?granularity=60&start={iso(cur)}&end={iso(stop - 60)}"
        rows = []
        for i in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}), timeout=30) as r:
                    rows = json.loads(r.read().decode())
                break
            except Exception as e:
                print("retry", i, str(e)[:80], flush=True); time.sleep(3 * (i + 1))
        for t, lo, hi, op, cl, v in rows:
            have[int(t)] = [lo, hi, op, cl, v]
        calls += 1; cur = stop; time.sleep(0.35)
    p.write_text(json.dumps({str(k): v for k, v in sorted(have.items())}))
    print(f"coinbase {asset}: {len(have)} minutes ({calls} calls)", flush=True)


def kcall(url: str) -> dict:
    from lab.us.data_refresh import fetch
    txt = fetch(url, pace=1.15)
    with CALLS.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "url": url[:160], "ok": bool(txt)}) + "\n")
    return json.loads(txt or "{}")


def range_candles(asset: str, max_calls: int, days: int) -> None:
    from lab.us.data_refresh import K
    from lab.kalshi.fetch import compact
    ser, above, cba, width, mid = RANGE[asset]
    ev = {}
    for l in open(f"data/kalshi_lab/markets/{above}.jsonl"):
        m = json.loads(l)
        if m["close"] - m["open"] == 3600:
            ev.setdefault(m["e"], []).append(m)
    out = OUT / f"candles_{ser}.jsonl"
    have = {json.loads(l)["e"] for l in out.open()} if out.exists() else set()
    cb = load_cb(cba)
    last = max(v[0]["close"] for v in ev.values()); first = last - days * 86400
    by_close = {v[0]["close"]: (e, v) for e, v in ev.items() if v[0]["close"] >= first}
    slots = []
    for T, (e, v) in sorted(by_close.items(), reverse=True):     # newest first
        h = dt.datetime.fromtimestamp(T, dt.timezone.utc).hour
        if h in PAIR_HOURS and T + 3600 in by_close:
            slots.append([by_close[T], by_close[T + 3600]])
    calls = 0
    for pair in slots:
        if calls >= max_calls:
            break
        if all(e.replace(above, ser) in have for e, _ in pair):
            continue
        pick = []; lo = min(v[0]["open"] for _, v in pair); hi = max(v[0]["close"] for _, v in pair)
        for e, v in pair:
            S = spot_at(cb, v[0]["open"])
            if S is None:
                continue
            floors = sorted(m["floor"] + 0.01 for m in v)
            near = sorted(floors[1:-1], key=lambda f: abs(f + width / 2 - S))[:N_BUCKETS]
            eb = e.replace(above, ser)
            pick += [(eb, f"{eb}-B{int(f + mid)}", f) for f in near]
        if not pick:
            continue
        d = kcall(f"{K}/markets/candlesticks?market_tickers={','.join(p[1] for p in pick)}&start_ts={lo}&end_ts={hi}&period_interval=1")
        calls += 1
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
        if not got:
            print("empty response", iso(hi), flush=True); continue
        with out.open("a") as fh:
            for eb, t, f in pick:
                op = [v for e, v in pair if e.replace(above, ser) == eb][0][0]
                c = [r for r in compact(got.get(t, [])) if op["open"] <= r[0] <= op["close"] + 60]
                fh.write(json.dumps({"t": t, "e": eb, "floor": round(f, 2), "cap": round(f + width - 0.01, 2), "c": c}) + "\n")
        if calls % 10 == 0:
            print(f"{ser}: {calls} calls, last {iso(hi)}, got {len(got)}/{len(pick)}", flush=True)
    print(f"{ser}: done {calls} calls, {len(slots)} slot pairs available", flush=True)


def lead_markets(days: int) -> None:
    now = int(time.time()); cur = ""; rows = {}
    out = OUT / "lead_markets.jsonl"
    if out.exists():
        rows = {json.loads(l)["t"]: json.loads(l) for l in out.open()}
    for _ in range(12):
        from lab.us.data_refresh import K
        d = kcall(f"{K}/markets?series_ticker=KXCRYPTOLEAD15M&status=settled&min_close_ts={now - days * 86400}&limit=1000" + (f"&cursor={cur}" if cur else ""))
        for m in d.get("markets") or []:
            ts = lambda s: int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
            rows[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "open": ts(m["open_time"]), "close": ts(m["close_time"]),
                                 "coin": (m.get("custom_strike") or {}).get("Cryptocurrency") or m.get("yes_sub_title"), "result": m.get("result"),
                                 "vol": m.get("volume_fp") or m.get("volume")}
        cur = d.get("cursor") or ""
        if not cur or not d.get("markets"):
            break
    out.write_text("".join(json.dumps(r) + "\n" for r in sorted(rows.values(), key=lambda r: (r["close"], r["t"]))))
    print("lead markets", len(rows), flush=True)


def lead_candles(max_calls: int, days: int) -> None:
    from lab.us.data_refresh import K
    from lab.kalshi.fetch import compact
    ms = [json.loads(l) for l in (OUT / "lead_markets.jsonl").open()]
    ev = {}
    for m in ms:
        ev.setdefault(m["e"], []).append(m)
    out = OUT / "candles_LEAD.jsonl"
    have = {json.loads(l)["e"] for l in out.open()} if out.exists() else set()
    last = max(m["close"] for m in ms)
    todo = sorted((e for e, v in ev.items() if e not in have and v[0]["close"] >= last - days * 86400), key=lambda e: -ev[e][0]["close"])
    calls = 0; i = 0
    while i < len(todo) and calls < max_calls:
        batch = [todo[i]]
        while i + len(batch) < len(todo):
            nxt = todo[i + len(batch)]; es = batch + [nxt]
            lo = min(ev[e][0]["open"] for e in es); hi = max(ev[e][0]["close"] for e in es)
            if sum(len(ev[e]) for e in es) * ((hi - lo) // 60 + 1) > 9500 or sum(len(ev[e]) for e in es) > 100:
                break
            batch.append(nxt)
        i += len(batch)
        lo = min(ev[e][0]["open"] for e in batch); hi = max(ev[e][0]["close"] for e in batch)
        tick = [m["t"] for e in batch for m in ev[e]]
        d = kcall(f"{K}/markets/candlesticks?market_tickers={','.join(tick)}&start_ts={lo}&end_ts={hi}&period_interval=1")
        calls += 1
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
        if not got:
            print("empty", iso(hi), flush=True); continue
        with out.open("a") as fh:
            for e in batch:
                for m in ev[e]:
                    c = [r for r in compact(got.get(m["t"], [])) if m["open"] <= r[0] <= m["close"] + 60]
                    fh.write(json.dumps({"t": m["t"], "e": e, "c": c}) + "\n")
        if calls % 10 == 0:
            print(f"lead: {calls} calls, {len(batch)} events/call, last {iso(hi)}", flush=True)
    print(f"lead: done {calls} calls", flush=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    a = sys.argv
    if a[1] == "coinbase":
        start = int(dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc).timestamp()); end = int(time.time()) // 60 * 60
        for asset in (a[2].split(",") if len(a) > 2 else PROD):
            coinbase(asset, start, end)
    elif a[1] == "range":
        range_candles(a[2], int(a[3]), int(a[4]))
    elif a[1] == "lead_markets":
        lead_markets(int(a[2]))
    elif a[1] == "lead_candles":
        lead_candles(int(a[2]), int(a[3]))
