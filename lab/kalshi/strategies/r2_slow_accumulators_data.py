"""Data for r2_slow_accumulators: Kalshi monthly precipitation / snowfall markets (archive + last 68 days),
hourly candles (sampled), and free daily station history (ACIS threaded records = NWS CLI values).
Usage: python -m lab.kalshi.strategies.r2_slow_accumulators_data lists|recent|candles_recent|candles_arch|acis"""
from __future__ import annotations
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_slow_accumulators_api import kget, used, oget, OUT

RAIN = ["KXRAINNYCM", "KXRAINCHIM", "KXRAINSTPM", "KXRAINNAPAM", "KXRAINDENM", "KXRAINLEXM", "KXRAINAUSM", "KXRAINCMHM",
        "KXRAINLAXM", "KXRAINSFOM", "KXRAINPVDM", "KXRAINMKEM", "KXRAINDALM", "KXRAINHOUM", "KXRAINSEAM", "KXRAINCLLM", "KXRAINMIAM"]
SNOW = ["KXNYCSNOWM", "KXCHISNOWM", "KXBOSSNOWM", "KXDENSNOWM", "KXMSPSNOWM", "KXDETSNOWM", "KXPHILSNOWM", "KXDCSNOWM",
        "KXSEASNOWM", "KXSLCSNOWM", "KXPITSNOWM", "KXMKESNOWM"]
MKF = OUT / "markets.jsonl"
CDF = OUT / "candles.jsonl"


def ts(s):
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def norm(m: dict, series: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": series, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
            "exp": ts(m.get("expected_expiration_time")), "result": m.get("result"), "type": m.get("strike_type"),
            "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"),
            "vol": float(m.get("volume_fp") or m.get("volume") or 0), "xv": m.get("expiration_value"),
            "early": m.get("early_close_condition"), "rules": (m.get("rules_primary") or "")[:300]}


def load_markets() -> dict[str, dict]:
    return {json.loads(l)["t"]: json.loads(l) for l in MKF.open()} if MKF.exists() else {}


def save_markets(mk: dict[str, dict]) -> None:
    MKF.write_text("".join(json.dumps(m) + "\n" for m in sorted(mk.values(), key=lambda m: (m["close"] or 0, m["t"]))))


def lists(series: list[str]) -> None:
    mk = load_markets()
    for s in series:
        cursor = ""
        for _ in range(3):
            d = kget(f"/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets", []):
                if m.get("result") in ("yes", "no"):
                    mk[m["ticker"]] = norm(m, s)
            cursor = d.get("cursor") or ""
            if not cursor:
                break
        print(s, sum(1 for m in mk.values() if m["series"] == s), "calls", used(), flush=True)
    save_markets(mk)


def recent(series: list[str]) -> None:
    mk = load_markets()
    min_close = int(dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp())
    for s in series:
        d = kget(f"/markets?series_ticker={s}&status=settled&min_close_ts={min_close}&limit=1000")
        for m in d.get("markets", []):
            if m.get("result") in ("yes", "no"):
                mk[m["ticker"]] = norm(m, s)
        print(s, len(d.get("markets", [])), "calls", used(), flush=True)
    save_markets(mk)


def compact(cs: list[dict]) -> list[list]:
    def g(c, k, sub):   # live candles use *_dollars keys, archived (/historical) candles plain keys
        d = c.get(k) or {}
        v = d.get(sub) if d.get(sub) is not None else d.get(sub.replace("_dollars", ""))
        return float(v) if v is not None else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask", "close_dollars"), g(c, "yes_bid", "close_dollars"), g(c, "yes_ask", "low_dollars"),
             g(c, "yes_bid", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def load_candles() -> dict[str, list]:
    out = {}
    if CDF.exists():
        for l in CDF.open():
            x = json.loads(l); out[x["t"]] = x["c"]
    return out


def candles_recent(events: list[str]) -> None:
    """One event-candlesticks call per live (post-2026-08-08) event: hourly candles of all its markets."""
    mk = load_markets(); have = load_candles()
    with CDF.open("a") as fh:
        for e in events:
            ms = [m for m in mk.values() if m["e"] == e]
            if not ms or all(m["t"] in have for m in ms):
                continue
            lo = min(m["open"] for m in ms) - 3600; hi = max(m["close"] for m in ms) + 3600
            d = kget(f"/series/{ms[0]['series']}/events/{e}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
            tk = d.get("market_tickers") or []; cs = d.get("market_candlesticks") or []
            for t, c in zip(tk, cs):
                if t not in have:
                    fh.write(json.dumps({"t": t, "c": compact(c)}) + "\n"); have[t] = 1
            print("event", e, len(tk), "calls", used(), flush=True)


def candles_arch(tickers: list[str]) -> None:
    mk = load_markets(); have = load_candles()
    with CDF.open("a") as fh:
        for t in tickers:
            if t in have:
                continue
            m = mk[t]
            d = kget(f"/historical/markets/{t}/candlesticks?start_ts={m['open'] - 3600}&end_ts={m['close'] + 3600}&period_interval=60")
            cs = d.get("candlesticks")
            if cs is None:
                print("no candles", t, str(d)[:120]); continue
            fh.write(json.dumps({"t": t, "c": compact(cs)}) + "\n"); have[t] = 1
            print("arch", t, len(cs), "calls", used(), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "lists":
        lists(sys.argv[2].split(",") if len(sys.argv) > 2 else RAIN + SNOW)
    elif cmd == "recent":
        recent(sys.argv[2].split(",") if len(sys.argv) > 2 else RAIN)


STATIONS = {"NYC": "KNYC", "MDW": "KMDW", "ORD": "KORD", "AUS": "KAUS", "DFW": "KDFW", "DEN": "KDEN", "HOU": "KHOU",
            "LAX": "KLAX", "MIA": "KMIA", "SEA": "KSEA", "SFO": "KSFO", "SPG": "KSPG"}


def acis(sid: str, sdate: str = "1971-01-01", edate: str | None = None) -> dict[str, float | None]:
    """Daily precipitation (inches) from ACIS (NWS official daily values, same as the CLI). T -> 0.0, M -> None."""
    edate = edate or dt.date.today().isoformat()
    p = json.dumps({"sid": sid, "sdate": sdate, "edate": edate, "elems": "pcpn", "meta": "name,sids,valid_daterange"}).encode()
    txt = oget("https://data.rcc-acis.org/StnData", f"acis_{edate}", post=p)
    d = json.loads(txt)
    out = {}
    for day, v in d.get("data", []):
        v = v.strip()
        if v in ("M", "", "S"):
            out[day] = None
        elif v == "T":
            out[day] = 0.0
        else:
            try:
                out[day] = float(v.rstrip("ASa"))
            except ValueError:
                out[day] = None
    return out


def station_of(m: dict) -> str | None:
    r = m["rules"]
    for key, st in (("Central Park", "NYC"), ("CLIMDW", "MDW"), ("Midway", "MDW"), ("CLIORD", "ORD"), ("CLIAUS", "AUS"), ("CLIDFW", "DFW"),
                    ("CLIDEN", "DEN"), ("CLIHOU", "HOU"), ("CLILAX", "LAX"), ("CLIMIA", "MIA"), ("CLISEA", "SEA"), ("CLISFO", "SFO"), ("CLISPG", "SPG")):
        if key in r:
            return st
    return None


def nbe_month(st: str, y: int, m: int) -> list[dict]:
    """NBM extended text guidance (IEM MOS archive, model NBE) for runs in [day before month start, month end]:
    rows {runtime, ftime, q12 (hundredths of an inch, 12 h ending at ftime), p12}, all runs (00Z/12Z, some eras 01Z/13Z);
    the model uses the latest run at least 3 h before the decision time."""
    import calendar as _c
    a = dt.datetime(y, m, 1) - dt.timedelta(days=1); b = dt.datetime(y, m, _c.monthrange(y, m)[1], 9)
    url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/mos.py?station={STATIONS[st]}&model=NBE"
           f"&sts={a:%Y-%m-%dT06:00Z}&ets={b:%Y-%m-%dT%H:00Z}&format=csv")
    txt = oget(url, "nbe", pace=2.0)
    out = []
    lines = txt.strip().splitlines()
    if not lines or not lines[0].startswith("runtime"):
        return out
    hdr = lines[0].split(",")
    iq, ip, ir, iF = hdr.index("q12"), hdr.index("p12"), hdr.index("runtime"), hdr.index("ftime")
    for ln in lines[1:]:
        f = ln.split(",")
        if len(f) < len(hdr):
            continue
        q = f[iq]
        out.append({"runtime": f[ir], "ftime": f[iF], "q12": float(q) if q not in ("", "None") else None,
                    "p12": float(f[ip]) if f[ip] not in ("", "None") else None})
    return out


def candles_live(events: list[str]) -> None:
    """Hourly candles for live-window events (Aug 2026 on) via the batch endpoint, <= 9,500 market-hours per call.
    KXRAINNYCM-26AUG was fetched while probing the endpoints (event endpoint, adjusted end, then batch): merged here."""
    mk = load_markets(); have = load_candles()
    with CDF.open("a") as fh:
        for e in events:
            ms = sorted((m for m in mk.values() if m["e"] == e), key=lambda m: m["t"])
            if not ms or all(m["t"] in have for m in ms):
                continue
            lo = min(m["open"] for m in ms) - 3600; hi = max(m["close"] for m in ms) + 3600
            got = defaultdict(list)
            if e == "KXRAINNYCM-26AUG":
                d1 = kget(f"/series/KXRAINNYCM/events/{e}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60", cache_only=True)
                for t, c in zip(d1.get("market_tickers") or [], d1.get("market_candlesticks") or []):
                    got[t].extend(compact(c))
                lo2 = d1["adjusted_end_ts"] - 3600
                orig = [m for m in mk.values() if m["e"] == e]
                d2 = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in orig)}&start_ts={lo2}&end_ts={hi}&period_interval=60", cache_only=True)
                for x in d2.get("markets") or []:
                    got[x.get("market_ticker")].extend(compact(x.get("candlesticks") or []))
            else:
                span = (hi - lo) // 3600 + 1; per = max(1, 9500 // span)
                for i in range(0, len(ms), per):
                    b = ms[i:i + per]
                    d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in b)}&start_ts={lo}&end_ts={hi}&period_interval=60")
                    for x in d.get("markets") or []:
                        got[x.get("market_ticker")].extend(compact(x.get("candlesticks") or []))
            for t, c in got.items():
                dd = {}
                for r in c:
                    dd[r[0]] = r
                fh.write(json.dumps({"t": t, "c": [dd[k] for k in sorted(dd)]}) + "\n"); have[t] = 1
            print("live", e, len(got), "calls", used(), flush=True)
