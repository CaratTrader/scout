"""fx_rates15 data: Kalshi 15-minute FX / Treasury-yield up-down markets, their 1-minute candles, and Yahoo 1-minute
spot bars (no keys). Everything is cached under data/kalshi_lab/strategies/fx_rates15/.

  markets  -> markets_<SERIES>.jsonl  one settled market per line (ticker, event, open, close, floor_strike, expiration_value, result, volume)
  candles  -> candles_<SERIES>.jsonl  {"t": ticker, "c": [[end_ts, yes_ask_close, yes_bid_close, yes_ask_low, yes_bid_high, volume, ask_high, bid_low], ...]}
              via the batch /markets/candlesticks endpoint, grouping concurrent markets of all series so that
              markets x span-minutes stays under 10,000 per call (Kalshi budget for this study: 200 calls).
  yahoo    -> yahoo/<SYM>.json  {bar_start_ts: [open, high, low, close]}
Kalshi calls go through lab.us.data_refresh.fetch (shares the ~1 req/s limit with the paper bot); every call is logged
to kalshi_calls.json.
Usage: python -m lab.kalshi.strategies.fx_rates15_data markets|candles|yahoo [days]"""
from __future__ import annotations
import datetime as dt, json, sys, time, urllib.parse, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/fx_rates15")
SERIES = ("KXEURUSD15M", "KXUSDJPY15M", "KXGBPUSD15M", "KXAUDUSD15M", "KXUSDCAD15M")   # yield 15M series: no markets listed (checked 2026-10-08)
YIELD_SERIES = (
          "KX10YRRATE15M", "KX2YRRATE15M", "KX5YRRATE15M", "KX30YRRATE15M")
YSYMS = ("EURUSD=X", "JPY=X", "GBPUSD=X", "AUDUSD=X", "CAD=X", "^TNX", "^FVX", "^TYX", "2YY=F", "10Y=F", "30Y=F", "5YY=F",
         "ZN=F", "ZF=F", "ZT=F", "ZB=F", "6E=F", "6J=F", "6B=F", "6A=F", "6C=F")
CALLS = OUT / "kalshi_calls.json"
BUDGET = 195


def kget(url: str) -> dict:
    log = json.loads(CALLS.read_text()) if CALLS.exists() else []
    if len(log) >= BUDGET:
        raise SystemExit(f"Kalshi budget exhausted ({len(log)} calls)")
    txt = fetch(url, pace=1.15)
    log.append({"ts": int(time.time()), "url": url[:300], "ok": bool(txt)}); CALLS.write_text(json.dumps(log))
    return json.loads(txt) if txt else {}


def f2(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def markets(days: float = 10.0) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    min_close = int(time.time() - days * 86400)
    for s in SERIES:
        f = OUT / f"markets_{s}.jsonl"
        have = {json.loads(l)["t"]: json.loads(l) for l in f.open()} if f.exists() else {}
        cursor = ""; n = 0
        while True:
            d = kget(f"{K}/markets?series_ticker={s}&status=settled&min_close_ts={min_close}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets") or []:
                if m.get("result") not in ("yes", "no"):
                    continue
                have[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": s, "open": ts(m.get("open_time")),
                                     "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")),
                                     "result": m["result"], "type": m.get("strike_type"), "floor": f2(m.get("floor_strike")),
                                     "cap": f2(m.get("cap_strike")), "xv": f2(m.get("expiration_value")),
                                     "vol": f2(m.get("volume_fp")), "oi": f2(m.get("open_interest_fp")),
                                     "settle": ts(m.get("settlement_ts"))}
                n += 1
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
        with f.open("w") as fh:
            for m in sorted(have.values(), key=lambda m: m["close"] or 0):
                fh.write(json.dumps(m) + "\n")
        print(s, "fetched", n, "stored", len(have), flush=True)


def candles(days: float = 10.0, max_calls: int = 150) -> None:
    allm = []
    for s in SERIES:
        f = OUT / f"markets_{s}.jsonl"
        if f.exists():
            allm += [json.loads(l) for l in f.open()]
    have = set()
    for s in SERIES:
        f = OUT / f"candles_{s}.jsonl"
        if f.exists():
            have |= {json.loads(l)["t"] for l in f.open()}
    lo = time.time() - days * 86400
    todo = sorted((m for m in allm if m["t"] not in have and m["close"] and m["close"] >= lo), key=lambda m: m["close"])
    print("markets to fetch", len(todo), flush=True)
    calls = 0; i = 0
    while i < len(todo) and calls < max_calls:
        grp = [todo[i]]; j = i + 1
        while j < len(todo):
            g2 = grp + [todo[j]]
            start = min(m["open"] for m in g2); end = max(m["close"] for m in g2)
            span = (end - start) // 60 + 2
            if len(g2) * span > 9500 or len(g2) > 100:
                break
            grp = g2; j += 1
        start = min(m["open"] for m in grp) - 60; end = max(m["close"] for m in grp) + 60
        d = kget(f"{K}/markets/candlesticks?market_tickers={','.join(m['t'] for m in grp)}&start_ts={start}&end_ts={end}&period_interval=1")
        calls += 1
        got = {x.get("market_ticker"): x.get("candlesticks") or [] for x in d.get("markets") or []}
        if not d:
            print("empty response; stopping", flush=True); break
        by_s = {}
        for m in grp:
            rows = []
            for c in got.get(m["t"], []):
                a, b = c.get("yes_ask") or {}, c.get("yes_bid") or {}
                rows.append([c["end_period_ts"], f2(a.get("close_dollars")), f2(b.get("close_dollars")), f2(a.get("low_dollars")),
                             f2(b.get("high_dollars")), f2(c.get("volume_fp")) or 0.0, f2(a.get("high_dollars")), f2(b.get("low_dollars"))])
            by_s.setdefault(m["series"], []).append({"t": m["t"], "c": rows})
        for s, lst in by_s.items():
            with (OUT / f"candles_{s}.jsonl").open("a") as fh:
                for x in lst:
                    fh.write(json.dumps(x) + "\n")
        print(f"call {calls}: {len(grp)} markets, span {(end - start) // 60} min, with candles {sum(1 for m in grp if got.get(m['t']))}", flush=True)
        i = j


def yget(sym: str, p1: int, p2: int) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1m&period1={p1}&period2={p2}&includePrePost=true"
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            print("retry", sym, i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return {}


def yahoo(days: float = 14.0) -> None:
    d0 = OUT / "yahoo"; d0.mkdir(parents=True, exist_ok=True)
    end = int(time.time()) + 600; start = int(end - days * 86400)
    for sym in YSYMS:
        f = d0 / f"{sym.replace('^', '').replace('=', '_')}.json"
        bars = {int(k): v for k, v in json.loads(f.read_text()).items()} if f.exists() else {}
        p = start
        while p < end:
            p2 = min(p + 6 * 86400, end)
            res = (yget(sym, p, p2).get("chart") or {}).get("result") or []; n = 0
            if res:
                r = res[0]; q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
                for k, t in enumerate(r.get("timestamp") or []):
                    vals = [(q.get(x) or [None] * (k + 1))[k] for x in ("open", "high", "low", "close")]
                    if vals[3] is not None:
                        bars[int(t)] = vals; n += 1
            print(sym, dt.datetime.utcfromtimestamp(p).date(), "bars", n, flush=True)
            p = p2; time.sleep(1.0)
        f.write_text(json.dumps({str(k): bars[k] for k in sorted(bars)}))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "markets"
    days = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    {"markets": markets, "candles": candles, "yahoo": yahoo}[cmd](days)
