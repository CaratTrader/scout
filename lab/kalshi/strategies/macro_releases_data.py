"""macro_releases data layer: Kalshi event lists, reference values known at decision time, strike selection and
hourly candle fetch (one call per archived market, one call per live event) within the 200-call budget.

Information used for SELECTING which strikes to download is only what was known before the release:
  claims: previous week's first print (the previous Kalshi event's expiration value / settled bracket),
  CPI:    Cleveland Fed nowcast dated before the release day.
Usage: python -m lab.kalshi.strategies.macro_releases_data plan|fetch"""
from __future__ import annotations
import datetime as dt, json, re, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.macro_releases_fetch import list_markets, kget, OUT, CALLS

HIST_CUT = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc)
CANDLES = OUT / "candles"; CANDLES.mkdir(parents=True, exist_ok=True)
NOWCAST = OUT / "ext" / "nowcast_month.json"


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def num(s) -> float | None:
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace("%", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def events(series: str) -> list[dict]:
    """Settled events with strike ladder ('greater'/'greater_or_equal' only), sorted by close."""
    ev = defaultdict(list)
    for m in list_markets(series):
        if m.get("strike_type") in ("greater", "greater_or_equal") and m.get("floor_strike") is not None and m.get("result") in ("yes", "no"):
            ev[m["event_ticker"]].append(m)
    out = []
    for e, ms in ev.items():
        ms.sort(key=lambda m: m["floor_strike"])
        close = ts(ms[0]["close_time"])
        val = None
        for m in ms:
            v = num(m.get("expiration_value"))
            if v is not None and "least" not in str(m.get("expiration_value")):
                val = v; break
        # bracket from results: YES for all strikes <= value (greater_or_equal) / < value (greater)
        yes = [m["floor_strike"] for m in ms if m["result"] == "yes"]; no = [m["floor_strike"] for m in ms if m["result"] == "no"]
        lo = max(yes) if yes else None; hi = min(no) if no else None
        out.append({"e": e, "series": series, "close": close, "type": ms[0]["strike_type"], "value": val, "lo": lo, "hi": hi,
                    "markets": [{"t": m["ticker"], "k": m["floor_strike"], "result": m["result"], "vol": num(m.get("volume_fp") or m.get("volume")) or 0} for m in ms],
                    "hist": close < HIST_CUT.timestamp()})
    return sorted(out, key=lambda x: x["close"])


def claims_value(ev: dict) -> float | None:
    if ev["value"] is not None:
        return ev["value"]
    if ev["lo"] is not None and ev["hi"] is not None:
        return (ev["lo"] + ev["hi"]) / 2
    return None


def claims_events() -> list[dict]:
    evs = events("KXJOBLESSCLAIMS")
    for i, e in enumerate(evs):
        e["actual"] = claims_value(e)
        prev = [p for p in evs[:i] if 5 * 86400 <= e["close"] - p["close"] <= 9 * 86400]
        e["p1"] = claims_value(prev[-1]) if prev else None          # previous week's first print, known since last release
        prev2 = [p for p in evs[:i] if 12 * 86400 <= e["close"] - p["close"] <= 16 * 86400]
        e["p2"] = claims_value(prev2[-1]) if prev2 else None
    return evs


def nowcasts() -> dict:
    """target 'YYYY-M' -> {'rows': [(date, cpi, core)], 'act_cpi', 'act_core'} (unrounded MoM %)."""
    out = {}
    for x in json.loads(NOWCAST.read_text()):
        tgt = x["chart"]["subcaption"]; ty, tm = map(int, tgt.split("-"))
        cats = [c["label"] for c in x["categories"][0]["category"] if "vline" not in c]
        ds = {s["seriesname"]: [v.get("value") for v in s["data"]] for s in x["dataset"]}
        rows = []; first_m = None
        for i, lab in enumerate(cats):
            mm, dd = map(int, lab.split("/"))
            y = ty + (1 if mm < tm else 0)
            f = lambda k: float(ds[k][i]) if ds.get(k) and i < len(ds[k]) and ds[k][i] not in (None, "") else None
            rows.append((dt.date(y, mm, dd), f("CPI Inflation"), f("Core CPI Inflation")))
        act = lambda k: next((float(v) for v in ds.get(k, []) if v not in (None, "")), None)
        out[tgt] = {"rows": rows, "act_cpi": act("Actual CPI Inflation"), "act_core": act("Actual Core CPI Inflation")}
    return out


MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def cpi_target(event: str) -> str | None:
    m = re.search(r"-(\d\d)([A-Z]{3})T?$", event)
    return f"20{m.group(1)}-{MON[m.group(2)]}" if m else None


def nowcast_before(nc: dict, tgt: str, day: dt.date, which: int) -> float | None:
    """Latest nowcast dated strictly before `day` (which: 1 headline, 2 core)."""
    v = None
    for r in nc.get(tgt, {}).get("rows", []):
        if r[0] < day and r[which] is not None:
            v = r[which]
    return v


def cpi_events(series: str) -> list[dict]:
    nc = nowcasts(); which = 1 if series == "KXCPI" else 2
    evs = [e for e in events(series) if e["e"].split("-")[0] in ("CPI", "KXCPI", "CPICORE", "KXCPICORE")]
    for e in evs:
        tgt = cpi_target(e["e"]); e["target"] = tgt
        rel = dt.datetime.fromtimestamp(e["close"], dt.timezone.utc).date()
        e["nowcast"] = nowcast_before(nc, tgt, rel, which) if tgt else None
        e["actual"] = e["value"]
    return [e for e in evs if e["nowcast"] is not None]


def plan() -> list[tuple[str, str, dict]]:
    """[(kind, key, info)] calls to make: ('hist', ticker) or ('event', event_ticker)."""
    todo = []
    for e in claims_events():
        if e["p1"] is None:
            continue
        if not e["hist"]:
            todo.append(("event", e["e"], e)); continue
        ks = sorted(e["markets"], key=lambda m: m["k"])
        below = [m for m in ks if m["k"] <= e["p1"]]; above = [m for m in ks if m["k"] > e["p1"]]
        pick = ([below[-1]] if below else []) + ([above[0]] if above else [])
        for m in pick:
            todo.append(("hist", m["t"], e))
    for s, start in (("KXCPI", "2022-12-01"), ("KXCPICORE", "2024-01-01")):
        for e in cpi_events(s):
            if e["close"] < ts(start + "T00:00:00Z"):
                continue
            if not e["hist"]:
                todo.append(("event", e["e"], e)); continue
            m = min(e["markets"], key=lambda m: abs(m["k"] + 0.05 - e["nowcast"]))
            todo.append(("hist", m["t"], e))
    return todo


def fetch_all() -> None:
    for kind, key, e in plan():
        f = CANDLES / f"{key}.json"
        if f.exists():
            continue
        end = e["close"]; start = end - 7 * 86400
        if kind == "hist":
            d = kget(f"/historical/markets/{key}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60")["d"]
            if "candlesticks" in d:
                f.write_text(json.dumps({key: d["candlesticks"]}))
        else:
            ser = key.split("-")[0]
            d = kget(f"/series/{ser}/events/{key}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60")["d"]
            if d.get("market_tickers"):
                f.write_text(json.dumps(dict(zip(d["market_tickers"], d["market_candlesticks"]))))
        print(kind, key, "ok" if f.exists() else "FAIL", json.loads(CALLS.read_text())["n"], flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "plan":
        p = plan(); print(len(p), "calls;", sum(1 for x in p if x[0] == "hist"), "hist,", sum(1 for x in p if x[0] == "event"), "event")
        for k, key, e in p[:4] + p[-4:]:
            print(k, key, e.get("p1") or e.get("nowcast"))
    else:
        fetch_all()
