"""index_model: 1-minute index / futures / ETF bars from the Yahoo chart API (no key), cached under
data/kalshi_lab/strategies/index_model/yahoo/<SYM>.json as {ts(bar start, unix s): [open, high, low, close]}.
Yahoo keeps 1-minute history for ~30 days and serves at most ~7 days per request.
Usage: python -m lab.kalshi.strategies.index_model_data [YYYY-MM-DD start] [YYYY-MM-DD end]"""
from __future__ import annotations
import datetime as dt, json, sys, time, urllib.parse, urllib.request
from pathlib import Path

OUT = Path("data/kalshi_lab/strategies/index_model/yahoo")
SYMS = ("^GSPC", "^NDX", "ES=F", "NQ=F", "SPY", "QQQ", "^VIX")


def get(sym: str, p1: int, p2: int) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1m&period1={p1}&period2={p2}&includePrePost=true"
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:
            print("retry", sym, i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return {}


def run(start: dt.date, end: dt.date) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for sym in SYMS:
        f = OUT / f"{sym.replace('^', '').replace('=', '_')}.json"
        bars = {int(k): v for k, v in json.loads(f.read_text()).items()} if f.exists() else {}
        d = start
        while d < end:
            d2 = min(d + dt.timedelta(days=7), end)
            p1 = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp())
            p2 = int(dt.datetime(d2.year, d2.month, d2.day, tzinfo=dt.timezone.utc).timestamp())
            res = (get(sym, p1, p2).get("chart") or {}).get("result") or []
            n = 0
            if res:
                r = res[0]; q = (r.get("indicators") or {}).get("quote") or [{}]
                for i, t in enumerate(r.get("timestamp") or []):
                    o, h, l, c = (q[0].get(k, [None] * (i + 1))[i] for k in ("open", "high", "low", "close"))
                    if c is not None:
                        bars[int(t)] = [o, h, l, c]; n += 1
            print(sym, d, d2, "bars", n, flush=True)
            d = d2; time.sleep(1.5)
        f.write_text(json.dumps({str(k): bars[k] for k in sorted(bars)}))


if __name__ == "__main__":
    a = sys.argv[1:]
    s = dt.date.fromisoformat(a[0]) if a else dt.date.today() - dt.timedelta(days=29)
    e = dt.date.fromisoformat(a[1]) if len(a) > 1 else dt.date.today() + dt.timedelta(days=1)
    run(s, e)
