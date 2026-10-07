"""Kalshi daily-high temperature ladders: same observed-max rules as lab/us/temp_backtest.py, on Kalshi's 1-minute
candlesticks (yes bid/ask close per minute) and settled results. Strikes: 'less' cap X -> max <= X-1; 'between'
floor..cap inclusive; 'greater' floor X -> max >= X+1.

The NWS afternoon climate report is usable only at its issuance (IEM AFOS entered time, the archive file name) plus
KB_REPORT_LAG minutes (default 7), not at its as-of time: reports come out a median 33 min after the as-of. That
minute is added to the 15-minute decision grid. Before 2026-10-01 the report was used at as-of + 5 min (look-ahead).
When the report's max-so-far beats the METAR max it lifts the max and the max is dated at the report's as-of time,
as in the bot (scout/us_temp_paper.py); only 'VALID TODAY AS OF' reports with as-of >= 12:00 from the offices in
KB_REPORT_OFFICES are used (the bot's rules; KB_REPORT_PARSE=lenient also takes the 'VALID AS OF 0400 PM' Minneapolis
/ Dallas reports).
LIVE configuration: R0 + R2 (paper job since 2026-09-27); since 2026-10-06 R2 measures its margin from the
unfiltered max and the 5-minute max (variant R2m, USTEMP_R2_MAX=raw5m), 21 cities; one trade per market, equal $25 stakes. R1c (report trigger, off in the bot) is still computed for reference.
Usage: python -m lab.us.kalshi_backtest [delay_min] [yes_cap]"""
from __future__ import annotations
import datetime as dt, json, math, os, statistics as st, sys, zoneinfo
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lab.us.temp_backtest as B
from lab.us.cli_backtest import all_intraday_reports, REPORT_LAG_MIN
from scout import us_temp_paper as U
import csv, re

KD = Path("data/lab/us/kalshi")
SERIES = {"KXHIGHNY": ("NYC", "KNYC", "America/New_York"), "KXHIGHCHI": ("MDW", "KMDW", "America/Chicago"), "KXHIGHMIA": ("MIA", "KMIA", "America/New_York"),
          "KXHIGHLAX": ("LAX", "KLAX", "America/Los_Angeles"), "KXHIGHTSFO": ("SFO", "KSFO", "America/Los_Angeles"), "KXHIGHTBOS": ("BOS", "KBOS", "America/New_York"),
          "KXHIGHTDC": ("DCA", "KDCA", "America/New_York"), "KXHIGHPHIL": ("PHL", "KPHL", "America/New_York"), "KXHIGHTATL": ("ATL", "KATL", "America/New_York"),
          "KXHIGHDEN": ("DEN", "KDEN", "America/Denver"), "KXHIGHAUS": ("AUS", "KAUS", "America/Chicago"), "KXHIGHTDAL": ("DFW", "KDFW", "America/Chicago"),
          "KXHIGHTMIN": ("MSP", "KMSP", "America/Chicago"), "KXHIGHTPHX": ("PHX", "KPHX", "America/Phoenix"), "KXHIGHTSEA": ("SEA", "KSEA", "America/Los_Angeles"),
          "KXHIGHTLV": ("LAS", "KLAS", "America/Los_Angeles"), "KXHIGHTSAN": ("SAN", "KSAN", "America/Los_Angeles"),
          "KXHIGHTHOU": ("HOU", "KHOU", "America/Chicago"), "KXHIGHTOKC": ("OKC", "KOKC", "America/Chicago"),
          "KXHIGHTSATX": ("SAT", "KSAT", "America/Chicago"), "KXHIGHTNOLA": ("MSY", "KMSY", "America/Chicago"),
          "KXHIGHTEWR": ("EWR", "KEWR", "America/New_York"), "KXHIGHTTTN": ("TTN", "KTTN", "America/New_York"),
          "KXHIGHTSDF": ("SDF", "KSDF", "America/Kentucky/Louisville")}
CLI_OK = {"NYC", "MIA", "MDW", "DCA", "PHL", "BOS", "ATL", "DFW", "MSP"}   # the bot's report offices (USTEMP_CLI_CITIES)
Z00 = {"NYC": 20, "MIA": 20, "BOS": 20, "DCA": 20, "PHL": 20, "ATL": 20, "MDW": 19, "AUS": 19, "DFW": 19, "MSP": 19, "DEN": 18, "LAX": 17, "SFO": 17, "SEA": 17, "LAS": 17, "SAN": 17, "PHX": 17,
       "HOU": 19, "OKC": 19, "SAT": 19, "MSY": 19, "EWR": 20, "TTN": 20, "SDF": 20}
FEE = 0.07
STAKE = 25.0
REPORT_LAG = int(os.environ.get("KB_REPORT_LAG", REPORT_LAG_MIN))
REPORT_STRICT = os.environ.get("KB_REPORT_PARSE", "strict") != "lenient"
REPORT_OFFICES = {c.strip().upper() for c in (os.environ.get("KB_REPORT_OFFICES") or ",".join(sorted(CLI_OK))).split(",") if c.strip()}
HF_LAG = int(os.environ.get("KB_HF_LAG", 20))   # api.weather.gov serves the 5-minute observations ~18 min after the fact
NEW = {"HOU", "OKC", "SAT", "MSY", "EWR", "TTN", "SDF"}   # cities added 2026-10-06/07 (never used to design the rules)
DESIGN = {("LAX", "2026-10-02")}                 # the loss the 5-minute fix was designed around: never part of a holdout
NO_LIVE_HF = {"NYC", "SAT"}                      # api.weather.gov serves no 5-minute rows for KNYC / KSAT (IEM has SAT's)
def fee(p): return FEE * p * (1 - p)
def rnd(x: float) -> int: return math.floor(x + 0.5)   # round half up, like the climate report and the bot


def hf_and_groups():
    """(station -> local day -> [(minute, whole C)] from the IEM 5-minute HFMETAR rows ('MADISHF'),
        station -> local day -> [(minute, F)] of every 6-hour maximum group, unfiltered, for the raw max).
    5-minute rows carry whole degrees C: the true reading lies within C +- 0.5."""
    hf = defaultdict(lambda: defaultdict(list)); gr = defaultdict(lambda: defaultdict(list))
    for stn, _, tzn in SERIES.values():
        f = Path(f"data/lab/us/asos_raw/{stn}.csv")
        if not f.exists():
            continue
        start = B.day_start_minutes(stn, tzn)
        for r in csv.DictReader(open(f)):
            raw = r.get("metar") or ""; day, hm = r["valid"][:10], r["valid"][11:16]
            minute = int(hm[:2]) * 60 + int(hm[3:]); ds = start(day)
            if minute < ds:
                continue
            if "MADISHF" in raw and stn not in NO_LIVE_HF:
                m = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", raw)
                if m:
                    c = float(m.group(1).replace("M", "-"))
                else:
                    g = re.search(r"\bT([01])(\d{3})", raw)
                    if not g:
                        continue
                    c = round(int(g.group(2)) / 10.0 * (-1 if g.group(1) == "1" else 1))
                hf[stn][day].append((minute, c))
            else:
                t = U.metar_temps_f(raw)
                if len(t) > 1 and minute >= ds + 6 * 60:
                    gr[stn][day].append((minute, t[1]))
    for d in (hf, gr):
        for stn in d:
            for day in d[stn]:
                d[stn][day].sort()
    return hf, gr


def interval(m: dict) -> tuple[float, float]:
    t = m.get("strike_type")
    if t == "less":
        return -1e9, float(m["cap_strike"]) - 1
    if t == "greater":
        return float(m["floor_strike"]) + 1, 1e9
    return float(m["floor_strike"]), float(m["cap_strike"])


def candles(ticker: str, tz: zoneinfo.ZoneInfo) -> list[tuple[int, float, float]]:
    f = KD / f"{ticker}.json"
    if not f.exists():
        return []
    out = []
    for c in json.loads(f.read_text()):
        try:
            ask = float(c["yes_ask"]["close_dollars"]); bid = float(c["yes_bid"]["close_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        lt = dt.datetime.fromtimestamp(int(c["end_period_ts"]), tz)
        out.append((lt.hour * 60 + lt.minute, ask, bid))
    return out


def quote_at(ser, minute, max_age: int = 90):
    """Candles are sparse (only minutes with activity), so a quote persists until the next candle: use the last
    candle at or before `minute`, provided it is not older than max_age minutes."""
    best = None
    for m, a, b in ser:
        if m <= minute:
            best = (m, a, b)
        else:
            break
    if best and minute - best[0] <= max_age:
        return best[1], best[2]
    return None


def one_per_market(trades: list[dict], rules: tuple[str, ...]) -> list[dict]:
    """First trade in time on each market among `rules` (the bot holds one position per market)."""
    seen, out = set(), []
    for r in sorted((x for x in trades if x["rule"] in rules), key=lambda x: (x["day"], x["t"], rules.index(x["rule"]))):
        if r["ticker"] not in seen:
            seen.add(r["ticker"]); out.append(r)
    return out


def stats(rows: list[dict], days: int) -> dict:
    """Per contract (pnl) and per equal-$ stake (ret = pnl / px; a $25 trade makes 25 * ret)."""
    p = [r["pnl"] for r in rows]; ret = [r["pnl"] / r["px"] for r in rows]; n = len(rows)
    tt = lambda v: st.mean(v) / st.pstdev(v) * math.sqrt(len(v)) if len(v) > 2 and st.pstdev(v) > 0 else float("nan")
    return {"n": n, "win": sum(r["won"] for r in rows) / n, "px": st.mean(r["px"] for r in rows), "c": st.mean(p) * 100, "per$": sum(p) / sum(r["px"] for r in rows),
            "t": tt(p), "t$": tt(ret), "per_day": n / days, "usd": STAKE * sum(ret), "usd_day": STAKE * sum(ret) / days, "ret": st.mean(ret)}


def main():
    delay = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    CAP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.80
    M = json.load(open(KD / "markets.json")); MET = B.metar(); cli = json.load(open("data/lab/us/asos/cli_high.json")); HF, GR = hf_and_groups()
    REPS = all_intraday_reports(strict=REPORT_STRICT, min_asof=12 * 60, lag=REPORT_LAG)   # (city, day) -> [reports by issuance]
    CITY = {stn: stn.lower() for stn, _, _ in SERIES.values()}
    days = defaultdict(list)
    for t, m in M.items():
        if m.get("result") not in ("yes", "no") or m["series"] not in SERIES:
            continue
        stn, icao, tzn = SERIES[m["series"]]
        # market day = local date of close_time minus 1 (close is 05:00Z next day)
        close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")); day = (close.astimezone(zoneinfo.ZoneInfo(tzn)) - dt.timedelta(hours=6)).date().isoformat()
        if not (os.environ.get("KB_FROM", "0000") <= day <= os.environ.get("KB_TO", "9999")):
            continue   # KB_FROM / KB_TO: restrict to a date window (e.g. the out-of-sample archive 2026-01-01..04-22)
        days[(stn, day)].append(m)
    print(f"Kalshi ladders: {len(days)} station-days, {sum(len(v) for v in days.values())} markets")
    print(f"report: usable at issuance + {REPORT_LAG} min, parse {'strict (VALID TODAY)' if REPORT_STRICT else 'lenient'}, offices {','.join(sorted(REPORT_OFFICES))}")
    trades = []; used = []; skipped = defaultdict(int); truth = defaultdict(int); rep_days = []; lifted = 0
    for (stn, day), mk in days.items():
        icao = next(v[1] for v in SERIES.values() if v[0] == stn); tzn = next(v[2] for v in SERIES.values() if v[0] == stn); tz = zoneinfo.ZoneInfo(tzn)
        obs = MET.get(stn, {}).get(day)
        if not obs or len(obs) < 12:
            skipped["no_metar"] += 1; continue
        # resolution check: Kalshi expiration_value vs NWS CLI high
        ev = next((float(m["expiration_value"]) for m in mk if m.get("expiration_value")), None); H = cli.get(icao, {}).get(day)
        if ev is not None and H is not None:
            truth[int(round(ev - H))] += 1
        rows = []
        for m in mk:
            ser = candles(m["ticker"], tz)
            if ser:
                lo, hi = interval(m); rows.append({"m": m, "lo": lo, "hi": hi, "won": m["result"] == "yes", "ser": ser})
        if not rows:
            skipped["no_candles"] += 1; continue
        used.append((stn, day)); done = set()
        reps = REPS.get((CITY[stn], day), []) if stn in REPORT_OFFICES else []
        if reps:
            rep_days.append(reps[0]["usable_min"])
        # decision grid: every 15 min from 12:00, plus the minute each report becomes usable
        grid = sorted(set(range(12 * 60, 23 * 60 + 30, 15)) | {r["usable_min"] for r in reps if 12 * 60 <= r["usable_min"] < 23 * 60 + 30})
        day_lift = False
        for t in grid:
            past = [r for r in obs if r[0] <= t]
            if not past:
                continue
            M_obs, tM = B.robust_max(past); T = min(r[1] for r in past if r[0] == past[-1][0])
            cr = None
            for r in reps:                      # latest report already usable at t (the bot fetches the newest one)
                if r["usable_min"] <= t:
                    cr = r
            Mx = M_obs
            if cr and cr["max"] > M_obs:        # the official max-so-far beats hourly METAR: lift, dated at its as-of time
                Mx, tM = cr["max"], cr["asof_min"]; day_lift = True
            rM = rnd(Mx); fall = Mx - T; since = t - tM
            # variants (2026-10-06): unfiltered max; 5-minute observations usable HF_LAG min after the fact
            Mraw = max([r[1] for r in past] + [g for gm, g in GR.get(stn, {}).get(day, []) if gm <= t])
            h5 = [(m_, c) for m_, c in HF.get(stn, {}).get(day, []) if m_ + HF_LAG <= t]
            M5_mid = max((c * 1.8 + 32 for _, c in h5), default=-1e9); M5_hi = max(((c + 0.5) * 1.8 + 32 for _, c in h5), default=-1e9)
            # dead-bucket lower bound: two consecutive 5-minute readings (<= 10 min apart) at >= C, minus half a degree C
            M5_lo2 = max((((min(a[1], b_[1]) - 0.5) * 1.8 + 32) for a, b_ in zip(h5, h5[1:]) if b_[0] - a[0] <= 10), default=-1e9)
            rM_raw = rnd(max(Mx, Mraw)); rM_m = rnd(max(Mx, Mraw, M5_mid)); rM_u = rnd(max(Mx, Mraw, M5_hi)); M0h = max(M_obs, M5_lo2)
            peak = t >= 15 * 60 and fall >= 1 and since >= 45
            after00 = t >= Z00[stn] * 60 + 5 and any(len(r) > 2 and r[2] and r[0] >= Z00[stn] * 60 - 40 for r in past)
            cli_gate = cr is not None and t >= 15 * 60 and fall >= 2 and since >= 60
            for b in rows:
                q = quote_at(b["ser"], t + delay)
                if not q:
                    continue
                ask, bid = q; no_ask = 1 - bid
                key = b["m"]["ticker"]
                def rec(rule, side, px, won):
                    trades.append({"rule": rule, "stn": stn, "day": day, "side": side, "px": px, "pnl": (1 if won else 0) - px - fee(px), "won": won, "t": t, "ticker": key,
                                   "m_obs": M_obs, "rep_max": cr["max"] if cr else None, "rep_usable": cr["usable_min"] if cr else None})
                holds = b["lo"] <= rM <= b["hi"]
                if cli_gate and not after00 and ("R1c", key) not in done:
                    if holds and 0.02 <= ask <= CAP:
                        done.add(("R1c", key)); rec("R1c", "YES", ask, b["won"])
                    elif not holds and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                        done.add(("R1c", key)); rec("R1c", "NO", no_ask, not b["won"])
                for F, S in ((1, 45), (2, 60), (3, 60)):
                    tag = f"R1_f{F}"
                    if (tag, key) not in done and t >= 15 * 60 and fall >= F and since >= S and holds and (rM + 1 <= b["hi"] or b["hi"] >= 1e8) and 0.02 <= ask <= 0.90:
                        done.add((tag, key)); rec(tag, "YES", ask, b["won"])
                if after00 and ("R1x", key) not in done:
                    if holds and 0.02 <= ask <= CAP:
                        done.add(("R1x", key)); rec("R1x", "YES", ask, b["won"])
                    elif not holds and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                        done.add(("R1x", key)); rec("R1x", "NO", no_ask, not b["won"])
                if ("R0", key) not in done:     # certain rule: METAR-only max, a full degree above the bucket top
                    if b["hi"] + 1.0 <= M_obs and 0.02 <= no_ask <= 0.97:
                        done.add(("R0", key)); rec("R0", "NO", no_ask, not b["won"])
                    elif b["hi"] >= 1e8 and M_obs >= b["lo"] + 0.05 and 0.02 <= ask <= 0.97:
                        done.add(("R0", key)); rec("R0", "YES", ask, b["won"])
                if peak and ("R2", key) not in done and b["lo"] >= rM + 3 and bid >= 0.15 and no_ask >= 0.02:
                    done.add(("R2", key)); rec("R2", "NO", no_ask, not b["won"])
                for tag, rr, floor in (("R2raw", rM_raw, 0.02), ("R2m", rM_m, 0.02), ("R2u", rM_u, 0.02), ("R2rawc", rM_raw, 0.20)):
                    # R2rawc: unfiltered max, and no fade when the market prices the bucket above 80% (NO < 0.20)
                    if peak and (tag, key) not in done and b["lo"] >= rr + 3 and bid >= 0.15 and no_ask >= floor:
                        done.add((tag, key)); rec(tag, "NO", no_ask, not b["won"])
                if ("R0h", key) not in done:    # R0 on max(METAR max, 5-minute lower bound)
                    if b["hi"] + 1.0 <= M0h and 0.02 <= no_ask <= 0.97:
                        done.add(("R0h", key)); rec("R0h", "NO", no_ask, not b["won"])
                    elif b["hi"] >= 1e8 and M0h >= b["lo"] + 0.05 and 0.02 <= ask <= 0.97:
                        done.add(("R0h", key)); rec("R0h", "YES", ask, b["won"])
        lifted += day_lift
    cal = len({d for _, d in used})
    rep_days.sort()
    print(f"station-days used {len(used)} over {cal} calendar days ({min(d for _, d in used)} .. {max(d for _, d in used)}), skipped {dict(skipped)}; Kalshi settlement minus NWS CLI high: {dict(sorted(truth.items()))}; delay {delay} min, YES cap {CAP}")
    if rep_days:
        print(f"station-days with a usable report: {len(rep_days)} (first usable minute median {rep_days[len(rep_days)//2]//60:02d}:{rep_days[len(rep_days)//2]%60:02d}); report lifted the max on {lifted}")

    def rep(rows, label):
        if not rows: print(f"{label:30s} n=0"); return
        s = stats(rows, cal)
        print(f"{label:30s} n={s['n']:4d} win={s['win']:4.0%} avg px={s['px']:.3f} pnl/c={s['c']:+6.1f}c per$={s['per$']:+.3f} t={s['t']:5.1f} t($)={s['t$']:5.1f} "
              f"/day={s['per_day']:.2f} $/day@{STAKE:.0f}={s['usd_day']:+6.2f} total@{STAKE:.0f}=${s['usd']:+8.2f}")

    print(f"\nRULES (per contract after 7% taker fee; t($) and $ on equal ${STAKE:.0f} stakes; /day over {cal} calendar days, 17 cities)")
    for rule in ("R0", "R2", "R1c", "R1x", "R1_f1", "R1_f2", "R1_f3"):
        rep([r for r in trades if r["rule"] == rule], rule)
        if rule in ("R1x", "R1c"):
            rep([r for r in trades if r["rule"] == rule and r["side"] == "YES"], f"  {rule} YES"); rep([r for r in trades if r["rule"] == rule and r["side"] == "NO"], f"  {rule} NO")
    LIVE_R2 = {"metar": "R2", "raw": "R2raw", "raw5m": "R2m", "raw5u": "R2u"}[U.CFG["r2_max"]]
    live = one_per_market(trades, ("R0", LIVE_R2)); ref = one_per_market(trades, ("R0", "R1c", "R2"))
    print(f"\nLIVE CONFIGURATION: R0 + {LIVE_R2} (USTEMP_R2_MAX={U.CFG['r2_max']}; report lifts the max once usable), one trade per market, YES cap {CAP}, ${STAKE:.0f} per trade, {len(SERIES)} cities")
    rep(live, f"LIVE R0+{LIVE_R2}")
    if live:
        dollars = [STAKE * r["pnl"] / r["px"] for r in live]; i = max(range(len(live)), key=lambda k: dollars[k]); top = live[i]
        print(f"  largest winner: {top['ticker']} {top['rule']} {top['side']} @ {top['px']:.2f} -> ${dollars[i]:+.2f} = {dollars[i]/sum(dollars):.0%} of total ${sum(dollars):+.2f}")
        rep([r for k, r in enumerate(live) if k != i], "  without the largest winner")
        print("  by month:"); [rep([r for r in live if r["day"][:7] == mo], f"    {mo}") for mo in sorted({r["day"][:7] for r in live})]
        print("  by city:"); [rep([r for r in live if r["stn"] == s], f"    {s}") for s in sorted(Z00, key=lambda s: -sum(r["stn"] == s for r in live)) if any(r["stn"] == s for r in live)]
    print(f"\n5-MINUTE / RAW-MAX VARIANTS (5-minute data usable {HF_LAG} min after the fact; stations with 5-minute rows: {sorted(HF)})")
    print("  caveat: 5-minute rows come from the IEM HFMETAR archive as a stand-in for api.weather.gov; row coverage differs slightly between the two")
    for rule in ("R2raw", "R2m", "R2u", "R2rawc", "R0h"):
        rep([r for r in trades if r["rule"] == rule], rule)
    combos = {}
    for combo in (("R0", "R2"), ("R0", "R2raw"), ("R0", "R2m"), ("R0", "R2u"), ("R0", "R2rawc"), ("R0h", "R2m")):
        combos[combo] = one_per_market(trades, combo); rep(combos[combo], "+".join(combo))
        rep([r for r in combos[combo] if r["stn"] not in NEW and r["day"] < "2026-09-27"], "    old 17 cities to 09-26")
        rep([r for r in combos[combo] if r["stn"] not in NEW and r["day"] >= "2026-09-27" and (r["stn"], r["day"]) not in DESIGN], "    holdout: old cities 09-27+ ex LAX 10-02")
        rep([r for r in combos[combo] if r["stn"] in NEW], "    holdout: new cities")
    base = {r["ticker"]: r for r in trades if r["rule"] == "R2"}
    for tag in ("R2raw", "R2m", "R2u"):
        kept = {r["ticker"] for r in trades if r["rule"] == tag}
        gone = [r for k, r in base.items() if k not in kept]
        print(f"  R2 trades {tag} removes: {len(gone)}, of which losers {sum(not r['won'] for r in gone)}: " + ", ".join(f"{r['ticker']}@{r['px']:.2f}{'W' if r['won'] else 'L'}" for r in gone))
    r0 = {r["ticker"] for r in trades if r["rule"] == "R0"}
    extra = [r for r in trades if r["rule"] == "R0h" and r["ticker"] not in r0]
    print(f"  R0h trades not in R0: {len(extra)}, losers {sum(not r['won'] for r in extra)}"); rep(extra, "  R0h extra")
    earlier = [(r, next(x for x in trades if x["rule"] == "R0" and x["ticker"] == r["ticker"])) for r in trades if r["rule"] == "R0h" and r["ticker"] in r0]
    print(f"  R0h on R0 markets: {len(earlier)}, earlier on {sum(a['t'] < b_['t'] for a, b_ in earlier)}, avg px R0h {st.mean(a['px'] for a, _ in earlier) if earlier else 0:.3f} vs R0 {st.mean(b_['px'] for _, b_ in earlier) if earlier else 0:.3f}")
    print("  losing R0h trades:", ", ".join(f"{r['ticker']} {r['side']}@{r['px']:.2f} t={r['t']//60:02d}:{r['t']%60:02d}" for r in trades if r["rule"] == "R0h" and not r["won"]))
    print("\nREFERENCE: R0 + R1c + R2 (report trigger on), one trade per market")
    rep(ref, "R0+R1c+R2")
    print("\nR1c by station:"); [rep([r for r in trades if r["rule"] == "R1c" and r["stn"] == s], f"  {s}") for s in Z00 if any(r["stn"] == s and r["rule"] == "R1c" for r in trades)]
    print("R1c by entry price:"); [rep([r for r in trades if r["rule"] == "R1c" and lo <= r["px"] < lo + 0.2], f"  px {lo:.1f}-{lo+0.2:.1f}") for lo in (0.0, 0.2, 0.4, 0.6, 0.8)]
    print("R2 by station:"); [rep([r for r in trades if r["rule"] == "R2" and r["stn"] == s], f"  {s}") for s in Z00 if any(r["stn"] == s and r["rule"] == "R2" for r in trades)]
    print("R2 by entry price:"); [rep([r for r in trades if r["rule"] == "R2" and lo <= r["px"] < lo + 0.2], f"  px {lo:.1f}-{lo+0.2:.1f}") for lo in (0.0, 0.2, 0.4, 0.6, 0.8)]
    with open(KD / f"trades_d{delay}.jsonl", "w") as fh:
        for r in trades: fh.write(json.dumps(r) + "\n")

if __name__ == "__main__":
    main()
