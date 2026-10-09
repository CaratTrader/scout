"""r6_weather_d1_mos_brackets data: hourly Kalshi candles for the daily-high ladders before the climate day's morning,
plus free IEM guidance (NBM text NBS, GFS MOS MAV, NAM MOS MET) and NWS CLI highs.

Kalshi: the lab's 1-minute candles (data/lab/us/kalshi/<ticker>.json) only cover the last 14 h before close (from about
10:00-11:00 local on the climate day). D-1 16:00 local and D 07:00 local quotes come from the batch endpoint
/markets/candlesticks (period_interval=60, <= 100 tickers and <= 9,500 market-hours per call) over [open - 1 h,
close - 13 h], for every market settled inside Kalshi's live tier. Every GET is cached by URL and counted
(budget 200 for the task). IEM responses are cached on disk under data/kalshi_lab/strategies/r6_weather_d1_mos_brackets/iem/.
Usage: python -m lab.kalshi.strategies.r6_weather_d1_mos_brackets_data [probe|candles|iem|cli|all]"""
from __future__ import annotations
import datetime as dt, hashlib, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K
from lab.us.kalshi_backtest import SERIES   # series -> (asos_raw station, ICAO, tz)

OUT = Path("data/kalshi_lab/strategies/r6_weather_d1_mos_brackets"); RAW = OUT / "raw"; IEM = OUT / "iem"; CNT = OUT / "kalshi_calls.txt"
KD = Path("data/lab/us/kalshi"); CF = OUT / "candles_h.jsonl"
BUDGET = 200


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def calls_used() -> int:
    return int(CNT.read_text()) if CNT.exists() else 0


def kget(path: str) -> dict:
    RAW.mkdir(parents=True, exist_ok=True)
    url = K + path; f = RAW / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if f.exists():
        return json.loads(f.read_text())
    n = calls_used()
    if n >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    CNT.write_text(str(n + 1))
    txt = fetch(url, pace=1.15)
    d = json.loads(txt) if txt else {}
    if d:
        f.write_text(json.dumps({"_url": url, **d}))
    return d


def iget(url: str, tag: str, pace: float = 1.5) -> str:
    """Cached polite GET for IEM (no key)."""
    IEM.mkdir(parents=True, exist_ok=True)
    f = IEM / f"{tag}_{hashlib.sha1(url.encode()).hexdigest()[:16]}.txt"
    if f.exists():
        return f.read_text()
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=180) as r:
                txt = r.read().decode()
            if txt.startswith("runtime") or txt.startswith("{") or txt.startswith("["):
                f.write_text(txt)
            time.sleep(pace); return txt
        except Exception as e:
            print("iem retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return ""


def markets() -> list[dict]:
    d = json.loads((KD / "markets.json").read_text())
    out = []
    for m in d.values():
        if m["series"] not in SERIES or m.get("result") not in ("yes", "no"):
            continue
        out.append({"t": m["ticker"], "e": m["event_ticker"], "series": m["series"], "open": ts(m["open_time"]), "close": ts(m["close_time"]),
                    "type": m["strike_type"], "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "result": m["result"],
                    "xv": m.get("expiration_value"), "vol": float(m.get("volume_fp") or m.get("volume") or 0), "hist": bool(m.get("hist"))})
    return sorted(out, key=lambda m: (m["close"], m["t"]))


def compact(cs: list[dict]) -> list[list]:
    def f(x, k, s):
        try:
            v = (x.get(k) or {}).get(s)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    out = []
    for x in cs:
        try:
            vol = float(x.get("volume_fp") or x.get("volume") or 0)
        except (TypeError, ValueError):
            vol = 0.0
        out.append([int(x["end_period_ts"]), f(x, "yes_ask", "close_dollars"), f(x, "yes_bid", "close_dollars"), f(x, "yes_ask", "low_dollars"),
                    f(x, "yes_bid", "high_dollars"), vol, f(x, "price", "close_dollars")])
    return out


def load_candles() -> dict:
    out = {}
    if CF.exists():
        for l in CF.open():
            x = json.loads(l); out[x["t"]] = x["c"]
    return out


def candles(min_close: int, max_close: int, max_tk: int = 100, max_c: int = 9500, dry: bool = False) -> None:
    """Hourly candles over [open - 1 h, close - 13 h] for markets with min_close <= close < max_close."""
    have = load_candles()
    todo = [m for m in markets() if min_close <= m["close"] < max_close and m["t"] not in have]
    print("markets to fetch", len(todo), "approx calls", -(-len(todo) // max_tk), "| used", calls_used(), flush=True)
    if dry:
        return
    i = 0
    with CF.open("a") as fh:
        while i < len(todo):
            b = [todo[i]]; lo, hi = todo[i]["open"] - 3600, todo[i]["close"] - 13 * 3600
            while i + len(b) < len(todo) and len(b) < max_tk:
                m2 = todo[i + len(b)]; lo2, hi2 = min(lo, m2["open"] - 3600), max(hi, m2["close"] - 13 * 3600)
                if (len(b) + 1) * ((hi2 - lo2) // 3600 + 1) > max_c:
                    break
                b.append(m2); lo, hi = lo2, hi2
            i += len(b)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in b)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {}
            for x in d.get("markets") or []:
                got[x.get("market_ticker") or x.get("ticker")] = compact(x.get("candlesticks") or [])
            if not got:
                print("batch failed", b[0]["t"], str(d)[:300], flush=True); continue
            for m in b:
                fh.write(json.dumps({"t": m["t"], "c": got.get(m["t"], [])}) + "\n")
            print("batch", len(b), "markets", b[0]["t"], "..", b[-1]["t"], "span h", (hi - lo) // 3600, "nonempty", sum(1 for m in b if got.get(m["t"])),
                  "| calls", calls_used(), flush=True)


ICAO = {s: v[1] for s, v in SERIES.items()}


def iem_mos(model: str, start: dt.date, end: dt.date) -> None:
    """IEM MOS archive CSV, one request per station over [start, end] (model NBS / GFS / NAM)."""
    for s, (stn, icao, tz) in SERIES.items():
        url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py?station={icao}&model={model}"
               f"&sts={start:%Y-%m-%d}T00:00Z&ets={end:%Y-%m-%d}T00:00Z&format=csv")
        txt = iget(url, f"{model}_{icao}")
        print("MOS", model, icao, "rows", txt.count("\n") if txt.startswith("runtime") else "FAIL " + txt[:100], flush=True)


def mos_file(model: str, icao: str) -> Path | None:
    fs = sorted(IEM.glob(f"{model}_{icao}_*.txt"))
    return fs[0] if fs else None


def iem_cli(years=(2024, 2025, 2026)) -> None:
    for s, (stn, icao, tz) in SERIES.items():
        for y in years:
            txt = iget(f"https://mesonet.agron.iastate.edu/json/cli.py?station={icao}&year={y}", f"cli_{icao}_{y}", pace=1.0)
            if not txt.startswith("{"):
                print("CLI fail", icao, y, txt[:100], flush=True)
        print("CLI", icao, "done", flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what == "probe":   # one small batch at the earliest T-city dates: is the live tier serving them?
        ms = [m for m in markets() if m["series"] == "KXHIGHTATL"][:6] + [m for m in markets() if m["series"] == "KXHIGHNY" and m["close"] >= ts("2026-07-21T00:00:00Z")][:6]
        lo = min(m["open"] for m in ms) - 3600; hi = max(m["close"] for m in ms) - 13 * 3600
        d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in ms)}&start_ts={lo}&end_ts={hi}&period_interval=60")
        for x in d.get("markets") or []:
            c = x.get("candlesticks") or []
            print(x.get("market_ticker"), len(c), c[:1])
        print(str(d)[:500]); print("calls", calls_used())
    if what in ("candles",):
        a = ts(sys.argv[2]); b = ts(sys.argv[3]); candles(a, b, dry=len(sys.argv) > 4)
    if what in ("iem", "all"):
        for mdl in ("NBS", "GFS", "NAM"):
            iem_mos(mdl, dt.date(2025, 5, 25), dt.date(2026, 10, 9))
    if what in ("cli", "all"):
        iem_cli()
