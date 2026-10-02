"""Does the NWS afternoon Daily Climate Report (CLIxxx, "VALID TODAY AS OF 0400 PM ... TODAY MAXIMUM x") let us act
earlier than the 00Z METAR group, on the Polymarket US ladders (sfo lax mdw nyc mia)?
For each city-day: parse the intraday issuance, compare its max with the final official high, check the METAR peak
state at the moment the report is usable, and simulate R1c = R1x triggered by the report when the peak has passed.

Timing (honest since 2026-10-01): a report is usable only REPORT_LAG_MIN minutes after its issuance, the IEM AFOS
entered time in the archive file name, not at its "as of" time. Reports are issued a median ~30 min after the as-of
(NYC/Miami/Chicago 4 PM reports at ~16:25-16:40 local); the earlier version acted at as-of + 2 min, i.e. on data it
could not have had. Usage: python -m lab.us.cli_backtest [delay_min] [report_lag_min]"""
from __future__ import annotations
import datetime as dt, json, math, re, statistics as st, sys, zoneinfo
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lab.us.temp_backtest as B

CLI_DIR = Path("data/lab/us/cli_intraday"); PIL = {"mia": "CLIMIA", "nyc": "CLINYC", "mdw": "CLIMDW", "sfo": "CLISFO", "lax": "CLILAX", "bos": "CLIBOS", "dca": "CLIDCA", "phl": "CLIPHL", "atl": "CLIATL",
       "den": "CLIDEN", "aus": "CLIAUS", "dfw": "CLIDFW", "msp": "CLIMSP", "phx": "CLIPHX", "sea": "CLISEA", "las": "CLILAS", "san": "CLISAN"}
MON = {m: i for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}
REPORT_LAG_MIN = 7      # report usable this long after issuance: api.weather.gov listing latency + the bot's 10-minute product cache
_HDR = re.compile(r"^[A-Z]{4}\d\d [A-Z]{4} (\d\d)(\d\d)(\d\d)\b", re.M)   # WMO header "CDUS42 KMFL 152028" = day 15, 20:28Z


def parse_cli(text: str) -> dict | None:
    """day (the climate day, from 'SUMMARY FOR'), asof_min, max, max_time; 'strict' = 'VALID TODAY AS OF' present (the
    bot's rule, scout/us_temp_paper.parse_cli); 'today' = strict, or the temperature block is headed TODAY (Minneapolis,
    Dallas and Austin label their 4 PM report 'VALID AS OF 0400 PM' without TODAY)."""
    m = re.search(r"SUMMARY FOR (\w+) (\d{1,2}) (\d{4})", text)
    v = re.search(r"VALID (TODAY|YESTERDAY)? ?AS OF (\d{3,4}) (AM|PM)", text)
    mx = re.search(r"\n\s*MAXIMUM\s+(-?\d+)R?\s+(\d{1,2}:\d{2} [AP]M)?", text)
    if not (m and mx):
        return None
    try:
        day = dt.date(int(m.group(3)), MON[m.group(1).upper()], int(m.group(2)))
    except (KeyError, ValueError):
        return None
    asof = None
    if v:
        hhmm = v.group(2).zfill(4); h = int(hhmm[:2]) % 12 + (12 if v.group(3) == "PM" else 0); asof = h * 60 + int(hhmm[2:])
    strict = bool(v and v.group(1) == "TODAY")
    return {"day": day.isoformat(), "asof_min": asof, "strict": strict,
            "today": strict or ("TODAY" in text.split("TEMPERATURE")[1][:80] if "TEMPERATURE" in text else False),
            "max": float(mx.group(1)), "max_time": mx.group(2)}


def issued_utc(file_day: str, hhmmz: str, text: str = "") -> dt.datetime:
    """Issuance time (UTC) of an archived product. The file name <PIL>_<YYYY-MM-DD>_<HHMM>Z carries the IEM AFOS entered
    time and its date is the UTC date, not the climate day (a 7 PM CDT Minneapolis report for Aug 10 is
    CLIMSP_2026-08-11_0055Z). The WMO header agrees on 4,383 of 4,389 files; when it is later than the entered time
    by up to 3 hours (two corrected / delayed afternoon reports) the later time is used."""
    d = dt.date.fromisoformat(file_day)
    t = dt.datetime(d.year, d.month, d.day, int(hhmmz[:2]), int(hhmmz[2:4]), tzinfo=dt.timezone.utc)
    h = _HDR.search(text[:200])
    if h:
        dd, hh, mi = (int(x) for x in h.groups())
        for c in (d, d - dt.timedelta(days=1), d + dt.timedelta(days=1)):
            if c.day == dd and hh < 24 and mi < 60:
                ht = dt.datetime(c.year, c.month, c.day, hh, mi, tzinfo=dt.timezone.utc)
                if dt.timedelta(0) < ht - t <= dt.timedelta(hours=3):
                    t = ht
                break
    return t


def local_minute(day: str, t_utc: dt.datetime, tz: zoneinfo.ZoneInfo) -> int:
    """Minute on the local clock of `day` at which t_utc falls (0 = local midnight; >= 1440 on the next local date)."""
    lt = t_utc.astimezone(tz)
    return (lt.date() - dt.date.fromisoformat(day)).days * 1440 + lt.hour * 60 + lt.minute


def all_intraday_reports(strict: bool = True, min_asof: int | None = 12 * 60, lag: int = REPORT_LAG_MIN) -> dict[tuple[str, str], list[dict]]:
    """(city, climate day) -> every intraday issuance carrying that day's max-so-far, sorted by issuance. Keys: day,
    asof_min, max, max_time, issued (HHMMZ of the entered time), issued_utc, iss_min (local minute of the climate
    day), usable_min (iss_min + lag), strict, file. Keyed by the report's own climate day, so a report issued after
    00Z (UTC date = next day) is kept. strict=True: only 'VALID TODAY AS OF' reports (what the bot accepts).
    min_asof: drop morning issuances (the bot ignores as-of < 12:00). A report whose issuance precedes its as-of or
    trails it by more than 6 h is dropped as mislabelled."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in sorted(CLI_DIR.glob("CLI*_*.txt")):
        pil, fday, iss = f.stem.split("_")
        city = next((c for c, p in PIL.items() if p == pil), None)
        if city is None:
            continue
        text = f.read_text(); rep = parse_cli(text)
        if not rep or rep["asof_min"] is None or not (rep["strict"] if strict else rep["today"]):
            continue
        if min_asof is not None and rep["asof_min"] < min_asof:
            continue
        tu = issued_utc(fday, iss, text); im = local_minute(rep["day"], tu, zoneinfo.ZoneInfo(B.TZ[city]))
        if not (0 <= im - rep["asof_min"] <= 360):
            continue
        out[(city, rep["day"])].append({**rep, "issued": iss, "issued_utc": tu.isoformat(), "iss_min": im, "usable_min": im + lag, "file": f.name})
    for k in out:
        out[k].sort(key=lambda r: r["iss_min"])
    return dict(out)


def intraday_reports(strict: bool = False, min_asof: int | None = None, lag: int = REPORT_LAG_MIN) -> dict[tuple[str, str], dict]:
    """(city, day) -> earliest intraday issuance that reports TODAY's maximum (lowest as-of, then earliest issued).
    Same keys as all_intraday_reports(); use r['usable_min'], never r['asof_min'], as the time it can be acted on."""
    out = {}
    for k, reps in all_intraday_reports(strict, min_asof, lag).items():
        out[k] = min(reps, key=lambda r: (r["asof_min"], r["iss_min"]))
    return out


def _stats(rows: list[dict]) -> str:
    p = [x["pnl"] for x in rows]; r = [x["pnl"] / x["px"] for x in rows]
    t_c = st.mean(p) / st.pstdev(p) * math.sqrt(len(p)) if len(p) > 2 and st.pstdev(p) else float("nan")
    t_d = st.mean(r) / st.pstdev(r) * math.sqrt(len(r)) if len(r) > 2 and st.pstdev(r) else float("nan")
    return (f"n={len(rows)} win={sum(x['won'] for x in rows)/len(rows):.0%} avg px={st.mean(x['px'] for x in rows):.2f} pnl/sh={st.mean(p)*100:+.1f}c "
            f"per$={sum(p)/sum(x['px'] for x in rows):+.2f} t={t_c:.1f} t($-equal)={t_d:.1f} $/trade@25={25*st.mean(r):+.2f}")


def main() -> None:
    delay = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    lag = int(sys.argv[2]) if len(sys.argv) > 2 else REPORT_LAG_MIN
    reps = intraday_reports(strict=True, min_asof=12 * 60, lag=lag); cli = json.load(open("data/lab/us/asos/cli_high.json"))
    MET = B.metar(); lad = json.load(open("data/lab/us/temp_ladders.json"))
    print(f"intraday afternoon reports parsed: {len(reps)} city-days; decision at issuance + {lag} min, fill at the first quote >= decision + {delay} min")
    eq = defaultdict(lambda: [0, 0]); eq_peak = defaultdict(lambda: [0, 0]); asof = defaultdict(list); iss = defaultdict(list); lagm = defaultdict(list); trades = []; skipped_empty = 0
    for (city, day), r in reps.items():
        H = cli.get("K" + city.upper(), {}).get(day)
        if H is None:
            continue
        asof[city].append(r["asof_min"]); iss[city].append(r["iss_min"]); lagm[city].append(r["iss_min"] - r["asof_min"])
        same = r["max"] == H
        eq[city][0] += 1; eq[city][1] += same
        t = r["usable_min"]
        obs = MET.get(B.STN[city], {}).get(day) or []
        past = [o for o in obs if o[0] <= t]
        if not past:
            continue
        M, tM = B.robust_max(past); T = min(o[1] for o in past if o[0] == past[-1][0])
        if r["max"] > M:
            M, tM = r["max"], r["asof_min"]   # the bot lifts its max to the report's and dates it at the as-of time
        peak = t >= 15 * 60 and (M - T) >= 2 and (t - tM) >= 60
        if peak:
            eq_peak[city][0] += 1; eq_peak[city][1] += same
        mk = lad.get(f"{city}:{day}")
        if not mk or not peak:
            continue
        tz = zoneinfo.ZoneInfo(B.TZ[city]); rM = math.floor(r["max"] + 0.5)
        for m in mk:
            try:
                pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except Exception:
                continue
            if len(pr) != 2 or pr[0] == pr[1]:
                continue
            won = pr[0] > pr[1]; lo, hi = B.bounds(m["slug"]); q = B.quote_at(B.series(m["slug"], tz), t + delay)
            if not q:
                continue
            ask, bid = q; no_ask = 1 - bid
            if abs(ask - 0.5) < 1e-9 and abs(bid - 0.5) < 1e-9:
                skipped_empty += 1; continue   # 0.50/0.50 is the empty-book placeholder (every bucket on 2026-04-23), not a fillable quote
            if lo <= rM <= hi and 0.02 <= ask <= 0.90:
                trades.append({"city": city, "day": day, "side": "YES", "px": ask, "pnl": (1 if won else 0) - ask - B.fee(ask), "won": won, "t": t})
            elif not (lo <= rM <= hi) and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                trades.append({"city": city, "day": day, "side": "NO", "px": no_ask, "pnl": (0 if won else 1) - no_ask - B.fee(no_ask), "won": not won, "t": t})
    hm = lambda v: f"{int(v)//60:02d}:{int(v)%60:02d}"
    print("\ncity  as-of   issued (median local, p10-p90)   lag min (med/min)  report max == final high | same, when METAR says the peak has passed at issuance+lag")
    for c in sorted(eq, key=lambda c: -eq[c][0]):
        n, k = eq[c]; n2, k2 = eq_peak[c]; a = sorted(asof[c]); i = sorted(iss[c]); g = sorted(lagm[c])
        print(f"{c:4s}  {hm(a[len(a)//2])}   {hm(i[len(i)//2])} ({hm(i[len(i)//10])}-{hm(i[len(i)*9//10])})              {g[len(g)//2]:3d}/{g[0]:3d}           {k}/{n} = {k/n:.0%}                   {k2}/{n2} = {(k2/n2 if n2 else 0):.0%}")
    print(f"\nquotes skipped as empty-book placeholders (0.50/0.50): {skipped_empty}")
    if trades:
        print(f"R1c (report usable + peak passed), Polymarket US: {_stats(trades)}")
        for c in ("mia", "nyc", "mdw", "sfo", "lax"):
            v = [x for x in trades if x["city"] == c]
            if v: print(f"   {c}: {_stats(v)}")
        e = [x for x in trades if x["city"] in ("mia", "nyc", "mdw")]
        if e: print(f"   mia+nyc+mdw: {_stats(e)}")
        json.dump(trades, open("data/lab/us/cli_trades.json", "w"))
    else:
        print("\nR1c: no trades")


if __name__ == "__main__":
    main()
