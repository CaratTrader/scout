"""Bring the Kalshi backtest data up to date, and add new cities, without re-downloading what is already there.

  IEM ASOS raw (routine + special + 5-minute HFMETAR rows, local time): appends days after the last row of
  data/lab/us/asos_raw/<STN>.csv (or fetches from 2026-07-01 for a new station); NWS CLI highs -> asos/cli_high.json.
  Kalshi: settled markets of the last 75 days -> kalshi/markets.json, and 1-minute candles for the last 14 h before
  close via the event candlesticks endpoint (one call per event instead of one per market). Paced at 2 s per call so
  the paper bot, which shares the ~1 req/s public limit, keeps working.
Usage: python -m lab.us.data_refresh [iem|kalshi|all] [SERIES,...]"""
from __future__ import annotations
import csv, datetime as dt, io, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scout.kalshi_temp_paper import SERIES as BOT_SERIES

K = "https://api.elections.kalshi.com/trade-api/v2"
RAW = Path("data/lab/us/asos_raw"); KD = Path("data/lab/us/kalshi"); CLI = Path("data/lab/us/asos/cli_high.json")


BOT_LOG = Path("data/kalshi_temp.log")   # the paper bot writes one line at the end of each poll


def bot_idle(window: float = 33.0) -> None:
    """Kalshi calls only in the ~35 s after a bot poll ends (polls start every 60 s and take ~20 s): sharing the
    ~1 req/s limit with the bot's burst made every other call a 429. No-op when the bot is not running."""
    if not BOT_LOG.exists() or time.time() - BOT_LOG.stat().st_mtime > 180:
        return
    while time.time() - BOT_LOG.stat().st_mtime > window:
        time.sleep(0.5)


def fetch(url: str, pace: float = 0.0, tries: int = 6) -> str:
    for i in range(tries):
        if "kalshi.com" in url:
            bot_idle()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research", "Accept": "application/json"}), timeout=300) as r:
                txt = r.read().decode()
            time.sleep(pace); return txt
        except urllib.error.HTTPError as e:
            time.sleep(6 * (i + 1) if e.code == 429 else 3)
        except Exception as e:
            print("retry", i, str(e)[:60], flush=True); time.sleep(8 * (i + 1))
    return ""


def iem(series: list[str]) -> None:
    cli = json.loads(CLI.read_text()) if CLI.exists() else {}
    today = dt.date.today()
    for s in series:
        _, icao, tz = BOT_SERIES[s]; st = icao[1:]; f = RAW / f"{st}.csv"
        have = f.read_text().splitlines() if f.exists() else []
        last = have[-1].split(",")[1] if len(have) > 1 else ""
        start = dt.date.fromisoformat(last[:10]) if last else dt.date(2026, 7, 1)
        end = today + dt.timedelta(days=1)
        url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={st}&data=tmpf,metar&year1={start.year}&month1={start.month}&day1={start.day}"
               f"&year2={end.year}&month2={end.month}&day2={end.day}&tz={tz}&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no"
               f"&report_type=1&report_type=3&report_type=4")
        lines = fetch(url, pace=3).strip().splitlines()
        if not lines:
            print(st, "IEM fetch failed", flush=True); continue
        hdr, body = lines[0], lines[1:]
        if have:
            seen = set(have[1:]); new = [l for l in body if l.split(",")[1] >= last and l not in seen]
            f.write_text("\n".join(have + new) + "\n")
        else:
            new = body; f.write_text("\n".join([hdr] + body) + "\n")
        txt = fetch(f"https://mesonet.agron.iastate.edu/json/cli.py?station={icao}&year=2026", pace=2)
        try:
            d = json.loads(txt); rows = d.get("results") or d.get("data") or []
            cli[icao] = {r["valid"]: float(r["high"]) for r in rows if r.get("high") not in (None, "M", "")}
        except Exception as e:
            print(st, "CLI fail", str(e)[:60], flush=True)
        CLI.write_text(json.dumps(cli))
        print(st, "rows +", len(new), "| 5-min rows", sum("MADISHF" in l for l in new), "| CLI days", len(cli.get(icao, {})), flush=True)


def kalshi(series: list[str]) -> None:
    KD.mkdir(parents=True, exist_ok=True); mfile = KD / "markets.json"
    markets = json.loads(mfile.read_text()) if mfile.exists() else {}
    min_close = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=75)).timestamp())
    for s in series:
        cursor = ""; n = 0
        while True:
            d = json.loads(fetch(f"{K}/markets?series_ticker={s}&status=settled&limit=200&min_close_ts={min_close}" + (f"&cursor={cursor}" if cursor else ""), pace=1.15) or "{}")
            for m in d.get("markets") or []:
                markets[m["ticker"]] = {k: m.get(k) for k in ("ticker", "event_ticker", "title", "floor_strike", "cap_strike", "strike_type", "result", "expiration_value", "open_time", "close_time", "volume", "volume_fp")}
                markets[m["ticker"]]["series"] = s; n += 1
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
        print(s, "settled markets", n, flush=True); mfile.write_text(json.dumps(markets))
    events: dict[str, list[dict]] = {}
    for t, m in markets.items():
        if m["series"] in series and m.get("close_time") and not (KD / f"{t}.json").exists():
            events.setdefault(m["event_ticker"], []).append(m)
    print("events to fetch", len(events), flush=True)
    for i, (ev, mk) in enumerate(sorted(events.items())):
        ct = int(dt.datetime.fromisoformat(mk[0]["close_time"].replace("Z", "+00:00")).timestamp())
        got: dict[str, list] = {}; start = ct - 14 * 3600
        for _ in range(4):   # the endpoint may cut the range short (adjusted_end_ts): continue from there
            d = json.loads(fetch(f"{K}/series/{mk[0]['series']}/events/{ev}/candlesticks?start_ts={start}&end_ts={ct}&period_interval=1", pace=1.15) or "{}")
            for tk, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                got.setdefault(tk, []).extend(cs)
            adj = int(d.get("adjusted_end_ts") or ct)
            if not d or adj >= ct - 3600:   # the endpoint trims the last few minutes before close: only continue on a real cut
                break
            start = adj
        if not got:
            continue  # leave the files missing so a rerun retries this event
        for m in mk:
            (KD / f"{m['ticker']}.json").write_text(json.dumps(got.get(m["ticker"], [])))
        if (i + 1) % 50 == 0:
            print("events", i + 1, flush=True)
    print("DONE kalshi", flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    ser = sys.argv[2].split(",") if len(sys.argv) > 2 else list(BOT_SERIES)
    if what in ("iem", "all"):
        iem(ser)
    if what in ("kalshi", "all"):
        kalshi(ser)
