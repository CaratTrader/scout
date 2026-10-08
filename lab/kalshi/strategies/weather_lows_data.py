"""weather_lows data: settled KXLOWT* ladders (last ~70 days via /markets) and hourly-close candles via the batch
/markets/candlesticks endpoint (period_interval=60), cached and counted by weather_lows_fetch.kget.
Output: data/kalshi_lab/strategies/weather_lows/markets.jsonl, candles_h.jsonl
Usage: python -m lab.kalshi.strategies.weather_lows_data [markets|candles|all]"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.weather_lows_fetch import kget, calls_used, OUT

# Kalshi low series -> (IEM station id in data/lab/us/asos_raw, tz) ; station confirmed from rules_primary (CLIxxx)
SERIES = {
    "KXLOWTNYC": ("NYC", "America/New_York"), "KXLOWTCHI": ("MDW", "America/Chicago"), "KXLOWTMIA": ("MIA", "America/New_York"),
    "KXLOWTLAX": ("LAX", "America/Los_Angeles"), "KXLOWTAUS": ("AUS", "America/Chicago"), "KXLOWTDEN": ("DEN", "America/Denver"),
    "KXLOWTPHIL": ("PHL", "America/New_York"), "KXLOWTSFO": ("SFO", "America/Los_Angeles"), "KXLOWTBOS": ("BOS", "America/New_York"),
    "KXLOWTDC": ("DCA", "America/New_York"), "KXLOWTATL": ("ATL", "America/New_York"), "KXLOWTDAL": ("DFW", "America/Chicago"),
    "KXLOWTMIN": ("MSP", "America/Chicago"), "KXLOWTPHX": ("PHX", "America/Phoenix"), "KXLOWTSEA": ("SEA", "America/Los_Angeles"),
    "KXLOWTLV": ("LAS", "America/Los_Angeles"), "KXLOWTSAN": ("SAN", "America/Los_Angeles"), "KXLOWTHOU": ("HOU", "America/Chicago"),
    "KXLOWTOKC": ("OKC", "America/Chicago"), "KXLOWTSATX": ("SAT", "America/Chicago"), "KXLOWTNOLA": ("MSY", "America/Chicago"),
    "KXLOWTEWR": ("EWR", "America/New_York"), "KXLOWTTTN": ("TTN", "America/New_York"), "KXLOWTSDF": ("SDF", "America/Kentucky/Louisville"),
}
MF = OUT / "markets.jsonl"; CF = OUT / "candles_h.jsonl"


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def markets(days: int = 70) -> None:
    out = {}
    if MF.exists():
        out = {json.loads(l)["t"]: json.loads(l) for l in MF.open()}
    min_close = int(time.time() - days * 86400) // 3600 * 3600   # hour-aligned so the cached URL is reused within the hour
    for s in SERIES:
        cursor = ""
        while True:
            d = kget(f"/markets?series_ticker={s}&status=settled&limit=1000&min_close_ts={min_close}" + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets") or []:
                if m.get("result") not in ("yes", "no"):
                    continue
                out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": s, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
                                    "exp": ts(m.get("expected_expiration_time")), "result": m["result"], "type": m.get("strike_type"),
                                    "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"),
                                    "vol": float(m.get("volume_fp") or m.get("volume") or 0), "xv": m.get("expiration_value"),
                                    "rules": (m.get("rules_primary") or "")[:160]}
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
        print(s, sum(1 for m in out.values() if m["series"] == s), "markets | calls", calls_used(), flush=True)
    MF.write_text("".join(json.dumps(m) + "\n" for m in sorted(out.values(), key=lambda m: (m["close"], m["t"]))))


def candles(span_h: int = 25, max_c: int = 9500, max_tk: int = 100) -> None:
    """Hourly candles over [close - span_h h, close] for every market; batches sized so markets x span-hours <= max_c."""
    ms = [json.loads(l) for l in MF.open()]
    have = {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()
    todo = sorted([m for m in ms if m["t"] not in have], key=lambda m: m["close"])
    i = 0
    with CF.open("a") as fh:
        while i < len(todo):
            batch = [todo[i]]; lo, hi = todo[i]["close"] - span_h * 3600, todo[i]["close"]
            while i + len(batch) < len(todo) and len(batch) < max_tk:
                m2 = todo[i + len(batch)]; lo2, hi2 = min(lo, m2["close"] - span_h * 3600), max(hi, m2["close"])
                if (len(batch) + 1) * ((hi2 - lo2) // 3600 + 1) > max_c:
                    break
                batch.append(m2); lo, hi = lo2, hi2
            i += len(batch)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {}
            for x in d.get("markets") or []:
                got[x.get("market_ticker") or x.get("ticker")] = x.get("candlesticks") or []
            if not got:
                print("batch failed", batch[0]["t"], str(d)[:200], flush=True); continue
            f = lambda c, k, sub: (float(c[k][sub]) if (c.get(k) or {}).get(sub) is not None else None)
            for m in batch:
                cs = got.get(m["t"], [])
                c = [[int(x["end_period_ts"]), f(x, "yes_ask", "close_dollars"), f(x, "yes_bid", "close_dollars"), f(x, "yes_ask", "low_dollars"),
                      f(x, "yes_bid", "high_dollars"), float(x.get("volume_fp") or x.get("volume") or 0)] for x in cs]
                fh.write(json.dumps({"t": m["t"], "c": c}) + "\n")
            print("batch", len(batch), "markets, span h", (hi - lo) // 3600, "| calls", calls_used(), flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("markets", "all"):
        markets()
    if what in ("candles", "all"):
        candles()
