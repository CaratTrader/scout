"""r2_launch_window data (all cached under data/kalshi_lab/strategies/r2_launch_window/):
  markets.jsonl   settled markets of the launch products (+ KXRAINNYC 2025 days from rain_history's archive list)
  candles_h.jsonl hourly candles {"t", "c": [[end_ts, yes_ask, yes_bid, ask_low, bid_high, vol], ...]} over
                  [scheduled close - 15 h, scheduled close - 5 h] (batch /markets/candlesticks, <= 100 tickers per call)
  nyc_candles.jsonl 1-minute candles of KXRAINNYC over [close - 17 h, close - 3 h] (/historical, one call per market)
Kalshi calls go through r2_launch_window_api.kget (counted, cached, budget 195).
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_launch_window_data lists|candles|nyc"""
from __future__ import annotations
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.r2_launch_window_api import kget, used, OUT

MF = OUT / "markets.jsonl"; CF = OUT / "candles_h.jsonl"; NF = OUT / "nyc_candles.jsonl"
LAUNCH = ["KXSOFRD", "KXAAAGASDFL", "KXAAAGASDNC", "KXAAAGASDSC", "KXAAAGASDOH", "KXAAAGASDCA", "KXUSDCADAD",
          "KXTXERCOTPEAKD", "KXTRUMPAPPROVE", "KXVHGCB", "KXTRUTHSOCIALD"]
CANDLE_SERIES = [s for s in LAUNCH if s != "KXUSDCADAD"]   # FX daily opens 7.5 h before close (no tau-10h quote) and trades ~0


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def norm(m: dict, s: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": s, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
            "exp": ts(m.get("expected_expiration_time")), "result": m.get("result"), "type": m.get("strike_type"),
            "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"),
            "vol": float(m.get("volume_fp") or m.get("volume") or 0), "xv": m.get("expiration_value"),
            "early": bool(m.get("can_close_early")), "settle_ts": ts(m.get("settlement_ts")),
            "rules": (m.get("rules_primary") or "")[:300]}


def lists() -> None:
    out = {json.loads(l)["t"]: json.loads(l) for l in MF.open()} if MF.exists() else {}
    for s in LAUNCH:
        cursor = ""
        for _ in range(3):
            d = kget(f"/markets?series_ticker={s}&status=settled&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets") or []:
                if m.get("result") in ("yes", "no"):
                    out[m["ticker"]] = norm(m, s)
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
        n = [m for m in out.values() if m["series"] == s]
        print(s, len(n), "markets", len({m["e"] for m in n}), "events, first close",
              dt.datetime.utcfromtimestamp(min(m["close"] for m in n)).isoformat() if n else None, "| calls", used(), flush=True)
    MF.write_text("".join(json.dumps(m) + "\n" for m in sorted(out.values(), key=lambda m: (m["close"], m["t"]))))


EARLY_ACTUAL = {"KXBIGGESTQUAKE"}   # series whose stamped close is the actual (early, outcome-revealing) close


def sched_close(m: dict) -> int:
    """Scheduled close. Every fetched launch series closes all markets of an event at one fixed time (checked: no
    within-event close dispersion), so close_time is the schedule. KXBIGGESTQUAKE stamps the actual early close when a
    threshold is hit, so its schedule is the expected expiration (23:59:59 UTC of the measured day)."""
    return m["exp"] if m["series"] in EARLY_ACTUAL else m["close"]


def candles(lo_h: int = 15, hi_h: int = 5, max_tk: int = 100, max_c: int = 9500, only=None) -> None:
    ms = [json.loads(l) for l in MF.open()]
    ms = [m for m in ms if m["series"] in (only or CANDLE_SERIES) and m["result"] in ("yes", "no")]
    have = {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()
    todo = sorted([m for m in ms if m["t"] not in have], key=lambda m: sched_close(m))
    i = 0
    with CF.open("a") as fh:
        while i < len(todo):
            sc = sched_close(todo[i]); lo, hi = sc - lo_h * 3600, sc - hi_h * 3600 + 3600; batch = [todo[i]]
            while i + len(batch) < len(todo) and len(batch) < max_tk:
                m2 = todo[i + len(batch)]; s2 = sched_close(m2)
                lo2, hi2 = min(lo, s2 - lo_h * 3600), max(hi, s2 - hi_h * 3600 + 3600)
                if (len(batch) + 1) * ((hi2 - lo2) // 3600 + 1) > max_c:
                    break
                batch.append(m2); lo, hi = lo2, hi2
            i += len(batch)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            if not got:
                print("batch failed", batch[0]["t"], str(d)[:200], flush=True); continue
            f = lambda c, k, sub: (float(c[k][sub]) if (c.get(k) or {}).get(sub) is not None else None)
            for m in batch:
                cs = got.get(m["t"], [])
                c = [[int(x["end_period_ts"]), f(x, "yes_ask", "close_dollars"), f(x, "yes_bid", "close_dollars"), f(x, "yes_ask", "low_dollars"),
                      f(x, "yes_bid", "high_dollars"), float(x.get("volume_fp") or x.get("volume") or 0)] for x in cs]
                fh.write(json.dumps({"t": m["t"], "c": c}) + "\n")
            print("batch", len(batch), "span h", (hi - lo) // 3600, "| calls", used(), flush=True)


def nyc(start: str = "2025-07-31", end: str = "2025-10-07") -> None:
    """KXRAINNYC 1-minute candles, one /historical call per market, for every climate day in [start, end]."""
    src = ROOT / "data/kalshi_lab/strategies/rain_history/markets.jsonl"
    mk = {json.loads(l)["t"]: json.loads(l) for l in src.open()}
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    days = []
    for t, m in mk.items():
        if m["series"] != "KXRAINNYC":
            continue
        d = dt.datetime.strptime(t.split("-")[1], "%y%b%d").date()
        if a <= d <= b:
            days.append(m)
    have = {json.loads(l)["t"] for l in NF.open()} if NF.exists() else set()
    with NF.open("a") as fh:
        for m in sorted(days, key=lambda m: m["close"]):
            if m["t"] in have:
                continue
            lo, hi = m["close"] - 17 * 3600, m["close"] - 3 * 3600
            d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=1")
            cs = d.get("candlesticks")
            if cs is None:
                print("no candles", m["t"], str(d)[:160], flush=True); continue
            def g(c, k, sub):
                x = c.get(k) or {}
                v = x.get(sub) if x.get(sub) is not None else x.get(sub.replace("_dollars", ""))
                return float(v) if v is not None else None
            c = [[int(x["end_period_ts"]), g(x, "yes_ask", "close_dollars"), g(x, "yes_bid", "close_dollars"), g(x, "yes_ask", "low_dollars"),
                  g(x, "yes_bid", "high_dollars"), float(x.get("volume_fp") or x.get("volume") or 0)] for x in cs]
            fh.write(json.dumps({"t": m["t"], "c": c}) + "\n")
            print("nyc", m["t"], len(c), "| calls", used(), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "lists":
        lists()
    elif cmd == "candles":
        candles(only=sys.argv[2].split(",") if len(sys.argv) > 2 else None)
    elif cmd == "nyc":
        nyc(*sys.argv[2:4]) if len(sys.argv) > 2 else nyc()
