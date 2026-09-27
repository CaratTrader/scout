"""Kalshi daily-high temperature markets: settled markets (last ~75 days) for six US series + 1-minute candlesticks
(bid/ask/price OHLC) for the last 14 h before close. Public API, paced ~1 req/s (429 otherwise). Output:
data/lab/us/kalshi/markets.json and kalshi/<ticker>.json"""
import datetime as dt, json, time, urllib.request
from pathlib import Path
K = "https://api.elections.kalshi.com/trade-api/v2"; OUT = Path("data/lab/us/kalshi"); OUT.mkdir(parents=True, exist_ok=True)
import sys
SERIES = sys.argv[1].split(",") if len(sys.argv) > 1 else ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHTSFO", "KXHIGHTBOS"]
def get(u):
    for i in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "scout-research", "Accept": "application/json"}), timeout=60) as r:
                time.sleep(1.1); return json.loads(r.read())
        except urllib.error.HTTPError as e:
            time.sleep(5 * (i + 1) if e.code == 429 else 2)
        except Exception:
            time.sleep(3)
    return None
mfile = OUT / "markets.json"; markets = json.loads(mfile.read_text()) if mfile.exists() else {}
min_close = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=75)).timestamp())
for s in SERIES:
    cursor = ""; n = 0
    while True:
        d = get(f"{K}/markets?series_ticker={s}&status=settled&limit=200&min_close_ts={min_close}" + (f"&cursor={cursor}" if cursor else "")) or {}
        for m in d.get("markets") or []:
            markets[m["ticker"]] = {k: m.get(k) for k in ("ticker", "event_ticker", "title", "floor_strike", "cap_strike", "strike_type", "result", "expiration_value", "open_time", "close_time", "volume", "volume_fp")}
            markets[m["ticker"]]["series"] = s; n += 1
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets"):
            break
    print(s, "settled markets", n, flush=True); mfile.write_text(json.dumps(markets))
done = 0
for t, m in markets.items():
    f = OUT / f"{t}.json"
    if f.exists() or not m.get("close_time"):
        continue
    ct = int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
    cs = get(f"{K}/series/{m['series']}/markets/{t}/candlesticks?start_ts={ct - 14 * 3600}&end_ts={ct}&period_interval=1") or {}
    f.write_text(json.dumps(cs.get("candlesticks") or [])); done += 1
    if done % 100 == 0:
        print("candles", done, flush=True)
print("DONE markets", len(markets), "candles fetched", done)
