"""commod_model data: free underlying prices for the Kalshi commodity markets, cached under
data/kalshi_lab/strategies/commod_model/.

Yahoo chart API (no key): 1-minute bars for the last ~30 days (one call per 7-day chunk) and 5-minute bars for 60 days
for GC=F (COMEX gold), SI=F (COMEX silver), CL=F (NYMEX WTI), NG=F (NYMEX natural gas).
Bars are stored as [start_ts, open, high, low, close, volume]; a bar's close is known at start_ts + interval.
Usage: python -m lab.kalshi.strategies.commod_model_data"""
from __future__ import annotations
import json, sys, time, urllib.request
from pathlib import Path

OUT = Path("data/kalshi_lab/strategies/commod_model/yahoo")
SYMS = ("GC=F", "SI=F", "CL=F", "NG=F")


def get(url: str) -> dict:
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return {}


def bars(d: dict) -> list[list]:
    try:
        r = d["chart"]["result"][0]; q = r["indicators"]["quote"][0]
    except Exception:
        return []
    out = []
    for i, t in enumerate(r.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if c is None:
            continue
        out.append([int(t), o, h, l, c, v or 0])
    return out


def load(sym: str, interval: str = "1m") -> list[list]:
    f = OUT / f"{sym.replace('=', '_')}_{interval}.json"
    return json.loads(f.read_text()) if f.exists() else []


def run() -> None:
    OUT.mkdir(parents=True, exist_ok=True); now = int(time.time())
    for sym in SYMS:
        have = {b[0]: b for b in load(sym, "1m")}
        start = now - 29 * 86400 - 3600
        while start < now:
            end = min(start + 7 * 86400, now)
            d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&period1={start}&period2={end}")
            b = bars(d)
            for x in b:
                have[x[0]] = x
            print(sym, "1m", time.strftime('%m-%d', time.gmtime(start)), len(b), flush=True)
            start = end; time.sleep(2)
        (OUT / f"{sym.replace('=', '_')}_1m.json").write_text(json.dumps(sorted(have.values())))
        have5 = {b[0]: b for b in load(sym, "5m")}
        for x in bars(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=5m&range=60d")):
            have5[x[0]] = x
        (OUT / f"{sym.replace('=', '_')}_5m.json").write_text(json.dumps(sorted(have5.values())))
        print(sym, "1m bars", len(have), "5m bars", len(have5), flush=True); time.sleep(2)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    run()
