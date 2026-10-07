"""Out-of-sample data for the weather rules: Kalshi's archive (markets settled before the 2026-08-08 cutoff, served by
/historical/...) for the seven long-running daily-high series, over a window no rule was designed on, plus the IEM
ASOS rows (hourly, specials and 5-minute) for those days.

Design data so far: Polymarket 2026-04-23..09-18 (NYC, LAX, MDW, MIA, SFO) and Kalshi 2026-07-19..10-05. The default
window 2026-01-01..04-22 precedes both, so R0 / R2 / R2m have never seen it (winter and spring weather included).
Candles: one request per market (the archive has no batch endpoint), [close - 14 h, close], 1-minute, written in the
format of lab/us/kalshi_fetch.py so lab/us/kalshi_backtest.py reads them unchanged (markets flagged "hist").
Usage: python -m lab.us.hist_fetch [iem|kalshi|all] [YYYY-MM-DD start] [YYYY-MM-DD end]"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.us.data_refresh import fetch, K

RAW = Path("data/lab/us/asos_raw"); KD = Path("data/lab/us/kalshi")
WX = {"KXHIGHNY": ("NYC", "America/New_York"), "KXHIGHCHI": ("MDW", "America/Chicago"), "KXHIGHMIA": ("MIA", "America/New_York"),
      "KXHIGHLAX": ("LAX", "America/Los_Angeles"), "KXHIGHAUS": ("AUS", "America/Chicago"), "KXHIGHDEN": ("DEN", "America/Denver"),
      "KXHIGHPHIL": ("PHL", "America/New_York")}


def iem_backfill(start: dt.date) -> None:
    for s, (st, tz) in WX.items():
        f = RAW / f"{st}.csv"
        have = f.read_text().splitlines() if f.exists() else []
        first = dt.date.fromisoformat(have[1].split(",")[1][:10]) if len(have) > 1 else dt.date.today()
        if first <= start:
            print(st, "already from", first, flush=True); continue
        parts = []; d0 = start
        while d0 < first:
            d1 = min(first, (d0.replace(day=1) + dt.timedelta(days=32)).replace(day=1))
            url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={st}&data=tmpf,metar&year1={d0.year}&month1={d0.month}&day1={d0.day}"
                   f"&year2={d1.year}&month2={d1.month}&day2={d1.day}&tz={tz}&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no"
                   f"&report_type=1&report_type=3&report_type=4")
            lines = fetch(url, pace=3).strip().splitlines()
            parts += [l for l in lines[1:] if l.split(",")[1][:10] < first.isoformat()]
            d0 = d1
        hdr = have[0] if have else "station,valid,tmpf,metar"
        f.write_text("\n".join([hdr] + parts + have[1:]) + "\n")
        print(st, "backfilled", len(parts), "rows from", start, "to", first, flush=True)


def conv(c: dict) -> dict:
    g = lambda k: {"close_dollars": (c.get(k) or {}).get("close")}
    return {"end_period_ts": c["end_period_ts"], "yes_ask": g("yes_ask"), "yes_bid": g("yes_bid"), "volume_fp": c.get("volume")}


def kalshi(start: dt.date, end: dt.date) -> None:
    KD.mkdir(parents=True, exist_ok=True); mfile = KD / "markets.json"
    markets = json.loads(mfile.read_text()) if mfile.exists() else {}
    lo = dt.datetime.combine(start, dt.time(), dt.timezone.utc).isoformat(); hi = dt.datetime.combine(end + dt.timedelta(days=2), dt.time(), dt.timezone.utc).isoformat()
    for s in WX:
        cursor = ""; n = 0
        while True:
            d = json.loads(fetch(f"{K}/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else ""), pace=1.15) or "{}")
            ms = d.get("markets") or []
            for m in ms:
                ct = (m.get("close_time") or "").replace("Z", "+00:00")
                if lo <= ct < hi and m.get("result") in ("yes", "no"):
                    markets[m["ticker"]] = {k: m.get(k) for k in ("ticker", "event_ticker", "title", "floor_strike", "cap_strike", "strike_type", "result",
                                                                   "expiration_value", "open_time", "close_time", "volume", "volume_fp")}
                    markets[m["ticker"]].update(series=s, hist=True); n += 1
            cursor = d.get("cursor") or ""
            if not cursor or not ms or min((m.get("close_time") or "9") for m in ms).replace("Z", "+00:00") < lo:
                break
        print(s, "archived markets in window", n, flush=True)
    mfile.write_text(json.dumps(markets))
    todo = sorted((m for m in markets.values() if m.get("hist") and not (KD / f"{m['ticker']}.json").exists()), key=lambda m: m["close_time"])
    print("candles to fetch", len(todo), flush=True)
    for i, m in enumerate(todo):
        ct = int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        d = json.loads(fetch(f"{K}/historical/markets/{m['ticker']}/candlesticks?start_ts={ct - 14 * 3600}&end_ts={ct}&period_interval=1", pace=1.15) or "{}")
        if "candlesticks" not in d:
            continue   # retried on the next run
        (KD / f"{m['ticker']}.json").write_text(json.dumps([conv(c) for c in d["candlesticks"]]))
        if (i + 1) % 200 == 0:
            print("candles", i + 1, flush=True)
    print("DONE hist", flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    start = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else dt.date(2026, 1, 1)
    end = dt.date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else dt.date(2026, 4, 22)
    if what in ("iem", "all"):
        iem_backfill(start)
    if what in ("kalshi", "all"):
        kalshi(start, end)
