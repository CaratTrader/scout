"""Data for the rain_history researcher (all cached under data/kalshi_lab/strategies/rain_history/):
  metar/<STN>_<from>_<to>.csv   IEM ASOS routine+special reports with tmpf, dwpf, alti, sky, wxcodes, metar (UTC)
  cli/<ICAO>_<year>.json        IEM CLI daily climate reports (truth check only; never used for decisions)
  markets.jsonl                 KXRAIN 2026-07-15.. (archive + live) and KXRAINNYC 2025.. settled markets
  candles.jsonl                 {"t": ticker, "c": [[ts, yes_ask, yes_bid, ask_low, bid_high, vol], ...]} 1-minute candles
Kalshi calls go through rain_history_api.kget (counted, cached, budget 200).
Usage: python -m lab.kalshi.strategies.rain_history_data metar|lists|live|arch <args>"""
from __future__ import annotations
import datetime as dt, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.rain_history_api import kget, used

OUT = Path("data/kalshi_lab/strategies/rain_history")
MET = OUT / "metar"; CLI = OUT / "cli"
MKF = OUT / "markets.jsonl"; CDF = OUT / "candles.jsonl"
STATIONS = json.loads(Path("data/kalshi_lab/rain_stations.json").read_text())
CODE2STN = {c: s[3:] for c, s in STATIONS.items()}
CODE2STN.update({"PROV": "PVD", "MIL": "MKE", "COL": "CMH"})   # one-off older city codes seen in the KXRAIN list


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def http(url: str, tries: int = 4) -> str:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=300) as r:
                return r.read().decode()
        except Exception as e:
            print("retry", str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return ""


def iem_metar(stn: str, start: dt.date, end: dt.date) -> Path:
    MET.mkdir(parents=True, exist_ok=True)
    f = MET / f"{stn}_{start}_{end}.csv"
    if f.exists() and f.stat().st_size > 1000:
        return f
    cols = "".join(f"&data={c}" for c in ("tmpf", "dwpf", "alti", "skyc1", "skyc2", "skyc3", "wxcodes", "metar"))
    url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={stn}{cols}&year1={start.year}&month1={start.month}&day1={start.day}"
           f"&year2={end.year}&month2={end.month}&day2={end.day}&tz=Etc/UTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no"
           f"&report_type=3&report_type=4")
    txt = http(url)
    if txt:
        f.write_text(txt)
    time.sleep(2)
    return f


def iem_cli(icao: str, year: int) -> Path:
    CLI.mkdir(parents=True, exist_ok=True)
    f = CLI / f"{icao}_{year}.json"
    if not f.exists():
        txt = http(f"https://mesonet.agron.iastate.edu/json/cli.py?station={icao}&year={year}")
        if txt:
            f.write_text(txt)
        time.sleep(2)
    return f


def compact(cs: list[dict]) -> list[list]:
    def g(c, k, sub):   # live candles use *_dollars keys, archived (/historical) candles plain keys
        d = c.get(k) or {}
        v = d.get(sub) if d.get(sub) is not None else d.get(sub.replace("_dollars", ""))
        return float(v) if v is not None else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask", "close_dollars"), g(c, "yes_bid", "close_dollars"), g(c, "yes_ask", "low_dollars"),
             g(c, "yes_bid", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def norm(m: dict, series: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": series, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
            "exp": ts(m.get("expected_expiration_time")), "result": m.get("result"), "floor": m.get("floor_strike"), "sub": m.get("yes_sub_title"),
            "vol": float(m.get("volume_fp") or 0), "rules": (m.get("rules_primary") or "")[:300]}


def load_markets() -> dict[str, dict]:
    return {json.loads(l)["t"]: json.loads(l) for l in MKF.open()} if MKF.exists() else {}


def save_markets(mk: dict[str, dict]) -> None:
    MKF.write_text("".join(json.dumps(m) + "\n" for m in sorted(mk.values(), key=lambda m: (m["close"] or 0, m["t"]))))


def load_candles() -> dict[str, list]:
    out = {}
    if CDF.exists():
        for l in CDF.open():
            x = json.loads(l); out[x["t"]] = x["c"]
    return out


def lists() -> None:
    mk = load_markets()
    for s in ("KXRAIN", "KXRAINNYC"):
        for m in kget(f"/historical/markets?series_ticker={s}&limit=1000").get("markets", []):
            if m["ticker"].startswith(s + "-") and m.get("result") in ("yes", "no"):
                mk[m["ticker"]] = norm(m, s)
    cursor = ""
    min_close = int(dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc).timestamp())
    for _ in range(4):
        d = kget(f"/markets?series_ticker=KXRAIN&status=settled&min_close_ts={min_close}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        for m in d.get("markets", []):
            if m.get("result") in ("yes", "no"):
                mk[m["ticker"]] = norm(m, "KXRAIN")
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    save_markets(mk)
    print("markets", len(mk), "calls used", used())


def window(m: dict, hours: int = 24) -> tuple[int, int]:
    return m["close"] - hours * 3600, m["close"]


def live(tickers: list[str], hours: int = 18) -> None:
    """1-minute candles for live-window markets via the batch endpoint (<= 9,500 candles per call)."""
    mk = load_markets(); have = load_candles()
    todo = sorted((mk[t] for t in tickers if t not in have), key=lambda m: m["close"])
    with CDF.open("a") as fh:
        i = 0
        while i < len(todo):
            lo, hi = window(todo[i], hours); batch = [todo[i]]
            while i + len(batch) < len(todo):
                w2 = window(todo[i + len(batch)], hours)
                if (len(batch) + 1) * ((max(hi, w2[1]) - min(lo, w2[0])) // 60 + 1) > 9500:
                    break
                batch.append(todo[i + len(batch)]); lo, hi = min(lo, w2[0]), max(hi, w2[1])
            i += len(batch)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=1")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            for m in batch:
                if m["t"] in got:
                    fh.write(json.dumps({"t": m["t"], "c": compact(got[m["t"]])}) + "\n")
            print("live batch", len(batch), "got", len(got), "calls", used(), flush=True)


def arch(tickers: list[str], hours: int = 24) -> None:
    """1-minute candles for archived markets: one call per market."""
    mk = load_markets(); have = load_candles()
    with CDF.open("a") as fh:
        for t in tickers:
            if t in have:
                continue
            lo, hi = window(mk[t], hours)
            d = kget(f"/historical/markets/{t}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=1")
            cs = d.get("candlesticks")
            if cs is None:
                print("no candles", t, str(d)[:120]); continue
            fh.write(json.dumps({"t": t, "c": compact(cs)}) + "\n")
            print("arch", t, len(cs), "calls", used(), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "lists":
        lists()
    elif cmd == "metar":
        for stn in sorted(set(CODE2STN.values())):
            iem_metar(stn, dt.date(2026, 7, 13), dt.date(2026, 10, 9))
        iem_metar("NYC", dt.date(2024, 12, 31), dt.date(2026, 7, 18))
        print("metar done")
    elif cmd == "cli":
        for stn in sorted(set(CODE2STN.values())):
            iem_cli("K" + stn, 2026)
        iem_cli("KNYC", 2025)
        print("cli done")
