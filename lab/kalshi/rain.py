"""KXRAIN ("will it rain today in <city>", YES if the day's precipitation at the CLI station is > 0): once a METAR from
the settlement station reports measurable precipitation (hourly P-group or 6-hour 6RRRR group >= 0.01 in) inside the
climate day, YES is all but certain. RAIN-R0 buys YES at the ask if the market still trades it at <= cap.

Climate day = midnight to midnight local standard time (01:00-01:00 local under daylight saving). A P-group covers the
time since the previous routine report, so groups are used only from reports >= 70 min after the day start (their
interval lies inside the day). Reports are usable LAG minutes after their time (AWC publishes within minutes).
Exploratory NO rule (RAIN-N, counted in K): at a fixed local hour with no measurable rain so far and no precipitation
mentioned in the last three reports, buy NO if its price is <= cap.
Usage: python -m lab.kalshi.rain [lag_min] [delay_min]"""
from __future__ import annotations
import csv, datetime as dt, io, json, re, statistics as st, sys, time, urllib.request, zoneinfo
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.kalshi.calib import quote, fee, cell_stats

ROOT = Path("data/kalshi_lab"); RM = ROOT / "rain_metar"
STATIONS = json.loads((ROOT / "rain_stations.json").read_text()) if (ROOT / "rain_stations.json").exists() else {}
TZ = {"ABQ": "America/Denver", "ATL": "America/New_York", "AUS": "America/Chicago", "BOS": "America/New_York", "ORD": "America/Chicago",
      "CLL": "America/Chicago", "CMH": "America/New_York", "DFW": "America/Chicago", "DCA": "America/New_York", "DEN": "America/Denver",
      "EWR": "America/New_York", "HOU": "America/Chicago", "LAX": "America/Los_Angeles", "LEX": "America/New_York", "LAS": "America/Los_Angeles",
      "MIA": "America/New_York", "MSP": "America/Chicago", "MKE": "America/Chicago", "MSY": "America/Chicago", "NYC": "America/New_York",
      "OKC": "America/Chicago", "PHL": "America/New_York", "PHX": "America/Phoenix", "PIT": "America/New_York", "PVD": "America/New_York",
      "SAT": "America/Chicago", "SEA": "America/Los_Angeles", "SFO": "America/Los_Angeles", "SGF": "America/Chicago", "TTN": "America/New_York"}
CAP = 0.97


def fetch_iem(stn: str, start: dt.date, end: dt.date) -> None:
    RM.mkdir(parents=True, exist_ok=True)
    url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={stn}&data=metar&year1={start.year}&month1={start.month}&day1={start.day}"
           f"&year2={end.year}&month2={end.month}&day2={end.day}&tz=Etc/UTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=3&report_type=4")
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=300) as r:
                (RM / f"{stn}.csv").write_text(r.read().decode()); return
        except Exception as e:
            print("retry", stn, str(e)[:60]); time.sleep(5 * (i + 1))


def precip_in(raw: str) -> float | None:
    """Measurable precipitation in inches from the remark groups (None = no group; 0.0 = trace or none)."""
    if " RMK " not in f" {raw} ":
        return None
    toks = raw.split(" RMK ", 1)[1].split() if " RMK " in raw else []
    best = None; skip = 0
    for i, tok in enumerate(toks):
        if skip:
            skip -= 1; continue
        if tok == "PK" and i + 1 < len(toks) and toks[i + 1] == "WND":
            skip = 2; continue
        m = re.fullmatch(r"P(\d{4})", tok) or re.fullmatch(r"6(\d{4})", tok)
        if m:
            v = int(m.group(1)) / 100.0; best = max(best or 0.0, v)
    return best


WX = re.compile(r"\s(?:\+|-|VC)?(?:TS|SH)?(?:RA|DZ|SN|PL|GR|GS|UP)\w*")


def first_rain(stn: str) -> dict[str, dict]:
    """local climate day -> {"first": minute of first measurable report (usable time not added), "wet_recent": [...]}"""
    tz = zoneinfo.ZoneInfo(TZ[stn]); out: dict[str, dict] = {}
    f = RM / f"{stn}.csv"
    if not f.exists():
        return out
    rows = []
    for r in csv.DictReader(io.StringIO(f.read_text())):
        if not r.get("metar") or r["metar"] == "M":
            continue
        t = dt.datetime.fromisoformat(r["valid"]).replace(tzinfo=dt.timezone.utc); rows.append((t, r["metar"]))
    rows.sort()
    for t, raw in rows:
        loc = t.astimezone(tz); std = loc.utcoffset() - (loc.dst() or dt.timedelta(0))
        d = (t + std).date(); day_start = dt.datetime.combine(d, dt.time(0), tzinfo=dt.timezone(std))
        key = d.isoformat(); o = out.setdefault(key, {"first": None, "reports": []})
        p = precip_in(raw)
        o["reports"].append((int(t.timestamp()), bool(WX.search(" " + raw.split(" RMK")[0] + " ")) or (p is not None and p >= 0.0)))
        if o["first"] is None and p is not None and p >= 0.01 and t - day_start >= dt.timedelta(minutes=70):
            o["first"] = int(t.timestamp())
    return out


def main(lag: int = 5, delay: int = 1) -> None:
    mk = {json.loads(l)["t"]: json.loads(l) for l in (ROOT / "markets" / "KXRAIN.jsonl").open()}
    cd = {json.loads(l)["t"]: json.loads(l)["c"] for l in (ROOT / "candles" / "KXRAIN.jsonl").open()}
    code2stn = {code: cli[3:] for code, cli in STATIONS.items() if cli}
    need = sorted({code2stn[t.split("-")[-1]] for t in mk if t.split("-")[-1] in code2stn})
    days = sorted({dt.datetime.strptime(t.split("-")[1], "%y%b%d").date() for t in mk})
    for stn in need:
        if not (RM / f"{stn}.csv").exists():
            fetch_iem(stn, days[0] - dt.timedelta(days=1), days[-1] + dt.timedelta(days=2)); time.sleep(2)
    obs = {stn: first_rain(stn) for stn in need}
    trades = []; truth = defaultdict(int); n_days = 0
    for t, m in mk.items():
        code = t.split("-")[-1]; stn = code2stn.get(code)
        if not stn or t not in cd:
            continue
        day = dt.datetime.strptime(t.split("-")[1], "%y%b%d").date().isoformat(); o = obs[stn].get(day)
        if not o:
            continue
        n_days += 1; won = m["result"] == "yes"
        truth[(o["first"] is not None, won)] += 1
        if o["first"] is not None:
            tq = o["first"] + (lag + delay) * 60
            if tq < m["close"]:
                q = quote(cd[t], tq)
                if q and 0.02 <= q[0] <= CAP:
                    px = q[0]; trades.append({"rule": "RAIN-R0", "e": m["e"], "t": t, "t_close": m["close"], "px": px, "won": won, "ret": ((1.0 if won else 0.0) - px - fee(px)) / px})
        tz = zoneinfo.ZoneInfo(TZ[stn])
        for hour in (12, 15, 18, 20, 22):   # exploratory NO rule: only what was known at the decision time
            d0 = dt.datetime.fromisoformat(day).replace(tzinfo=tz)
            tdec = int(d0.replace(hour=hour).timestamp())
            if o["first"] is not None and o["first"] + lag * 60 <= tdec:
                continue   # measurable rain already reported: not a NO candidate
            if True:
                recent = [w for ts_, w in o["reports"] if ts_ <= tdec - lag * 60][-3:]
                if len(recent) < 3 or any(recent):
                    continue
                q = quote(cd[t], tdec + delay * 60)
                if q and 0.02 <= 1 - q[1] <= CAP:
                    px = 1 - q[1]; trades.append({"rule": f"RAIN-N{hour}", "e": m["e"], "t": t, "t_close": m["close"], "px": px, "won": not won, "ret": ((0.0 if won else 1.0) - px - fee(px)) / px})
    print(f"KXRAIN station-days with METAR {n_days}; (METAR measurable rain seen, market YES): {dict(truth)}  lag {lag} min, delay {delay} min")
    closes = sorted({r["t_close"] for r in trades}); cut = closes[int(len(closes) * 0.7)] if closes else 0
    print(f"discovery = markets closing before {dt.datetime.fromtimestamp(cut, dt.timezone.utc).date()} (70% of days); validation after")
    for part, sel in (("DISCOVERY", lambda r: r["t_close"] < cut), ("VALIDATION", lambda r: r["t_close"] >= cut)):
        print(part)
        for rule in sorted({r["rule"] for r in trades}):
            rows = [r for r in trades if r["rule"] == rule and sel(r)]
            if len(rows) > 2:
                v = cell_stats(rows); h = sorted(r["t_close"] for r in rows)[len(rows) // 2]
                a = [r["ret"] for r in rows if r["t_close"] < h]; b_ = [r["ret"] for r in rows if r["t_close"] >= h]
                print(f"  {rule:9s} n={v['n']:4d} ev={v['events']:3d} win={v['win']:.0%} px={v['px']:.3f} ret/$={v['ret']:+.1%} t={v['t']:.1f} wo3={v['ret_wo3']:+.1%} halves={st.mean(a):+.1%}/{st.mean(b_):+.1%}")
    print("ALL DATA")
    for rule in sorted({r["rule"] for r in trades}):
        rows = [r for r in trades if r["rule"] == rule]
        v = cell_stats(rows)
        print(f"{rule:9s} n={v['n']:4d} ev={v['events']:3d} win={v['win']:.0%} avg px={v['px']:.3f} ret/$={v['ret']:+.1%} t={v['t']:.1f} wo3={v['ret_wo3']:+.1%}")
        if rule == "RAIN-R0":
            for lo, hi in ((0.02, 0.5), (0.5, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.971)):
                x = [r for r in rows if lo <= r["px"] < hi]
                if x:
                    s = cell_stats(x); print(f"   px {lo:.2f}-{hi:.2f}: n={s['n']} win={s['win']:.0%} ret/$={s['ret']:+.1%}")
            print("   losers:", [(r["t"], r["px"]) for r in rows if not r["won"]][:12])
    (ROOT / "rain_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in trades))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5, int(sys.argv[2]) if len(sys.argv) > 2 else 1)
