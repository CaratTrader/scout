"""Independent reproduction of the rain_history claim (KXRAIN late-day dry -> buy NO), written from the claim text only.

Rule family N (as described): at local clock hour h of the climate day, if the settlement (CLI) station's METARs show
  - no measurable precipitation so far in the climate day (hourly P-group >= 0.01 in; 3-/6-hour 6RRRR group >= 0.01 in
    from a report at least 3 h / 6 h after the day start),
  - no precipitation / thunder weather code (incl. VC.., TS, SH) and no CB/TCU in the reports of the last 3 h,
  - [C3 only] dew-point depression >= 10F at the last report,
then buy NO as taker at 1 - yes_bid one minute after the decision, if the NO price is inside the candidate band.
  C1 = h16, NO in [0.30, 0.90];  C2 = h16, NO in [0.80, 0.95];  C3 = h14, dd >= 10F, NO in [0.30, 0.90].
Reports usable 5 min after their time. Climate day = local standard time midnight..midnight. Fee: 0.07 p (1-p) per
contract, rounded up to the cent on a 10-contract order. Quotes from 1-minute candles (yes_ask/yes_bid close), carried
forward at most 30 min. Split (same as the claim): KXRAIN event-dates ordered by close, validation = 2026-09-14..10-07.

Data: data/kalshi_lab/markets|candles/KXRAIN.jsonl (lab, through 10-06), data/kalshi_lab/rain_metar/<STN>.csv (lab IEM);
the 10-07 event and the new DTW/IND stations are fetched here (python -m lab.kalshi.strategies.rain_history_repro fetch).
Outputs: data/kalshi_lab/strategies/rain_history/repro/.
Usage: python -m lab.kalshi.strategies.rain_history_repro [fetch|run]"""
from __future__ import annotations
import csv, datetime as dt, hashlib, io, json, math, re, statistics as st, sys, time, urllib.request, zoneinfo
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

LAB = Path("data/kalshi_lab")
OUT = LAB / "strategies" / "rain_history" / "repro"
UTC = dt.timezone.utc
VAL_FIRST, VAL_LAST = dt.date(2026, 9, 14), dt.date(2026, 10, 7)
DISC_FIRST = dt.date(2026, 8, 8)
TZ = {"ATL": "America/New_York", "AUS": "America/Chicago", "BOS": "America/New_York", "ORD": "America/Chicago", "DFW": "America/Chicago",
      "DCA": "America/New_York", "DEN": "America/Denver", "HOU": "America/Chicago", "LAX": "America/Los_Angeles", "LAS": "America/Los_Angeles",
      "MIA": "America/New_York", "MSP": "America/Chicago", "MSY": "America/Chicago", "NYC": "America/New_York", "OKC": "America/Chicago",
      "PHL": "America/New_York", "PHX": "America/Phoenix", "SAT": "America/Chicago", "SEA": "America/Los_Angeles", "SFO": "America/Los_Angeles",
      "EWR": "America/New_York", "TTN": "America/New_York", "LEX": "America/New_York", "CMH": "America/New_York", "PVD": "America/New_York",
      "CLL": "America/Chicago", "MKE": "America/Chicago", "PIT": "America/New_York", "SGF": "America/Chicago", "ABQ": "America/Denver",
      "DTW": "America/Detroit", "IND": "America/Indiana/Indianapolis"}
STD_OFF = {"America/New_York": -5, "America/Detroit": -5, "America/Indiana/Indianapolis": -5, "America/Chicago": -6,
           "America/Denver": -7, "America/Phoenix": -7, "America/Los_Angeles": -8}
CITY = {"ATL": "ATL", "AUS": "AUS", "BOS": "BOS", "CHI": "ORD", "DAL": "DFW", "DC": "DCA", "DEN": "DEN", "HOU": "HOU", "LAX": "LAX",
        "LV": "LAS", "MIA": "MIA", "MIN": "MSP", "NOLA": "MSY", "NYC": "NYC", "OKC": "OKC", "PHIL": "PHL", "PHX": "PHX", "SATX": "SAT",
        "SEA": "SEA", "SFO": "SFO", "EWR": "EWR", "TTN": "TTN", "LEX": "LEX", "CMH": "CMH", "PVD": "PVD", "PROV": "PVD", "CLL": "CLL",
        "COL": "CLL", "MKE": "MKE", "MIL": "MKE", "PIT": "PIT", "SGF": "SGF", "ABQ": "ABQ", "DTW": "DTW", "IND": "IND"}
CANDS = {"C1": dict(h=16, dd=0, lo=0.30, hi=0.90), "C2": dict(h=16, dd=0, lo=0.80, hi=0.95), "C3": dict(h=14, dd=10, lo=0.30, hi=0.90)}
LAG = 5 * 60
MAX_AGE = 30 * 60
CALLS = []


# ----------------------------------------------------------------------------------------------------------- fetch
def kget(url: str) -> dict:
    from lab.us.data_refresh import fetch, K
    txt = fetch(K + url, pace=1.15); CALLS.append((int(time.time()), len(txt), url))
    return json.loads(txt or "{}")


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def do_fetch() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 1) settled KXRAIN markets closing on/after 2026-09-14 (independent copy of metadata + rules -> station)
    mins = int(dt.datetime(2026, 9, 14, tzinfo=UTC).timestamp()); cur = ""; rows = []
    while True:
        d = kget(f"/markets?series_ticker=KXRAIN&status=settled&min_close_ts={mins}&limit=1000" + (f"&cursor={cur}" if cur else ""))
        for m in d.get("markets") or []:
            rows.append({"t": m["ticker"], "e": m["event_ticker"], "close": ts(m.get("close_time")), "result": m.get("result"),
                         "rules": m.get("rules_primary", ""), "sub": m.get("yes_sub_title"), "vol": m.get("volume_fp") or m.get("volume")})
        cur = d.get("cursor") or ""
        if not cur or not d.get("markets"):
            break
    (OUT / "markets_api.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print("markets", len(rows))
    # 2) candles for validation markets that the lab file lacks (the 10-07 event): only the decision windows are needed
    have = {json.loads(l)["t"] for l in (LAB / "candles" / "KXRAIN.jsonl").open()}
    todo = [r for r in rows if r["t"] not in have and r["e"].split("-")[1] and edate(r["e"]) <= VAL_LAST]
    out = []
    for i in range(0, len(todo), 25):
        b = todo[i:i + 25]
        lo = min(int(dec_time(edate(r["e"]), station(r), 14).timestamp()) for r in b) - 45 * 60
        hi = max(int(dec_time(edate(r["e"]), station(r), 16).timestamp()) for r in b) + 5 * 60
        assert len(b) * ((hi - lo) // 60 + 1) <= 10000
        d = kget(f"/markets/candlesticks?market_tickers={','.join(r['t'] for r in b)}&start_ts={lo}&end_ts={hi}&period_interval=1")
        got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
        for r in b:
            c = []
            for k in got.get(r["t"], []):
                a = (k.get("yes_ask") or {}).get("close_dollars"); bd = (k.get("yes_bid") or {}).get("close_dollars")
                c.append([int(k["end_period_ts"]), float(a) if a is not None else None, float(bd) if bd is not None else None,
                          float(k.get("volume_fp") or k.get("volume") or 0)])
            out.append({"t": r["t"], "c": c})
    (OUT / "candles_extra.jsonl").write_text("".join(json.dumps(x) + "\n" for x in out))
    print("extra candles", len(out), "markets")
    # 3) IEM METAR for settlement stations missing from the lab cache
    for stn in sorted({station(r) for r in rows} - {p.stem for p in (LAB / "rain_metar").glob("*.csv")}):
        url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={stn}&data=metar&year1=2026&month1=9&day1=12"
               f"&year2=2026&month2=10&day2=9&tz=Etc/UTC&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=3&report_type=4")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=120) as r:
            (OUT / f"metar_{stn}.csv").write_text(r.read().decode())
        print("metar", stn); time.sleep(2)
    with (OUT / "calls.log").open("a") as fh:
        for c in CALLS:
            fh.write("\t".join(map(str, c)) + "\n")
    print("kalshi calls", len(CALLS))


# ----------------------------------------------------------------------------------------------------------- helpers
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def edate(e: str) -> dt.date:
    s = e.split("-")[1]
    return dt.date(2000 + int(s[:2]), MON[s[2:5]], int(s[5:7]))


def station(m: dict) -> str:
    mm = re.search(r"\bCLI([A-Z]{3})\b", m.get("rules") or "")
    return mm.group(1) if mm else CITY[m["t"].split("-")[-1]]


def dec_time(d: dt.date, stn: str, h: int) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, h, 0, tzinfo=zoneinfo.ZoneInfo(TZ[stn])).astimezone(UTC)


def day_start(d: dt.date, stn: str) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone(dt.timedelta(hours=STD_OFF[TZ[stn]]))).astimezone(UTC)


PRECIP = ("DZ", "RA", "SN", "SG", "IC", "PL", "GR", "GS", "UP")
WXTOK = re.compile(r"^(\+|-)?(VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?((DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)*)$")
CLOUD_CB = re.compile(r"^(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(CB|TCU)$")


def parse(valid: str, raw: str) -> dict:
    t = dt.datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    raw = raw.replace("$", " ").strip()
    body, _, rmk = raw.partition(" RMK ")
    btoks = body.split()[2:]   # drop station and ddhhmmZ
    wx = False; cb = False; temp = dew = None
    for tok in btoks:
        if tok in ("COR", "AUTO") or tok.endswith("KT") or tok.endswith("SM"):
            continue
        if CLOUD_CB.match(tok):
            cb = True
        m = WXTOK.match(tok)
        if m and tok not in ("", "-", "+") and (m.group(2) or m.group(3) or m.group(4)):
            if m.group(3) in ("TS", "SH") or any(p in (m.group(4) or "") for p in PRECIP):
                wx = True
        mt = re.fullmatch(r"(M?\d{2})/(M?\d{2})?", tok)
        if mt:
            temp = int(mt.group(1).replace("M", "-")); dew = int(mt.group(2).replace("M", "-")) if mt.group(2) else None
    rtoks = rmk.split()
    cb_rmk = any(x in ("CB", "TCU", "CBMAM") or x.startswith("CB") and x[2:] in ("", "MAM") for x in rtoks)
    # remark begin/end of precip or thunder (e.g. RAB15E30, TSE20) = precip during the last hour
    wx_rmk = any(re.fullmatch(r"(\+|-)?(SH|TS|FZ)?(DZ|RA|SN|PL|GR|GS|UP|TS)+(B\d{2,4}|E\d{2,4})+\S*", x) for x in rtoks)
    # broad remark reading: vicinity showers/thunder, lightning, virga, any TS.. token (incl. sensor TSNO), CB/TCU
    rmk_broad = wx_rmk or cb_rmk or any(re.match(r"^(VCSH|VCTS|LTG|VIRGA|TS|CB|TCU)", x) for x in rtoks)
    hp = None; six = None
    for x in rtoks:
        m = re.fullmatch(r"P(\d{4})", x)
        if m:
            hp = int(m.group(1)) / 100
        m = re.fullmatch(r"6(\d{4})", x)
        if m:
            six = int(m.group(1)) / 100
        m = re.fullmatch(r"T([01])(\d{3})([01])(\d{3})", x)
        if m:
            temp = (-1 if m.group(1) == "1" else 1) * int(m.group(2)) / 10
            dew = (-1 if m.group(3) == "1" else 1) * int(m.group(4)) / 10
    nominal_hour = (t + dt.timedelta(minutes=10)).hour   # :51-:59 reports belong to the next hour
    ddr = (round(temp * 1.8 + 32) - round(dew * 1.8 + 32)) if temp is not None and dew is not None else None
    return {"t": t, "wx": wx, "cb": cb, "cb_rmk": cb_rmk, "wx_rmk": wx_rmk, "rmk_broad": rmk_broad, "ddr": ddr, "hp": hp, "six": six,
            "six_h": 6 if nominal_hour % 6 == 0 else (3 if nominal_hour % 3 == 0 else None),
            "routine_like": t.minute >= 45 or t.minute <= 5, "dd": (temp - dew) * 1.8 if temp is not None and dew is not None else None}


def load_metar(stn: str) -> list[dict]:
    f = LAB / "rain_metar" / f"{stn}.csv"
    if not f.exists():
        f = OUT / f"metar_{stn}.csv"
    out = []
    for r in csv.DictReader(io.StringIO(f.read_text())):
        if r.get("metar") and r.get("valid"):
            out.append(parse(r["valid"], r["metar"]))
    out.sort(key=lambda x: x["t"])
    return out


def quote(c: list, t: int):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > MAX_AGE or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


def fee10(p: float) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def signal(obs: list[dict], d: dt.date, stn: str, h: int, dd_min: float, opt: dict) -> tuple[bool, dict]:
    t = dec_time(d, stn, h); ds = day_start(d, stn)
    usable = [o for o in obs if o["t"] + dt.timedelta(seconds=LAG) <= t and o["t"] > ds - dt.timedelta(hours=3, minutes=10)]
    today = [o for o in usable if o["t"] > ds + dt.timedelta(minutes=opt.get("p_after", 0))]
    if not [o for o in usable if o["t"] >= t - dt.timedelta(hours=1, minutes=30)]:
        return False, {"why": "no recent METAR"}
    meas = False
    for o in today:
        if o["hp"] is not None and o["hp"] >= 0.01:
            meas = True
        if o["six"] is not None and o["six"] >= 0.01 and o["six_h"] and o["t"] >= ds + dt.timedelta(hours=o["six_h"], minutes=-15):
            meas = True
    last3 = [o for o in usable if o["t"] >= t - dt.timedelta(hours=3)]
    conv = any(o["wx"] or o["cb"] or (opt.get("cb_rmk", True) and o["cb_rmk"]) or (opt.get("wx_rmk", False) and o["wx_rmk"])
               or (opt.get("broad", False) and o["rmk_broad"]) for o in last3)
    dd = usable[-1]["ddr" if opt.get("dd_round") else "dd"]
    ok = (not meas) and (not conv) and (dd_min <= 0 or (dd is not None and dd >= dd_min))
    return ok, {"meas": meas, "conv": conv, "dd": dd, "t_dec": int(t.timestamp())}


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(len(em))) if len(em) > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    dates = sorted({r["date"] for r in rows})
    allv = sorted({r["date"] for r in rows} | set())
    return {"n": len(rows), "events": len(em), "win": sum(r["won"] for r in rows) / len(rows), "avg_px": st.mean(r["px"] for r in rows),
            "ret_per_dollar": st.mean(r["ret"] for r in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "pnl_per_contract_sum": sum(r["pnl"] for r in rows)}


def halves(rows: list[dict], first: dt.date, last: dt.date) -> tuple:
    days = [(first + dt.timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    mid = days[len(days) // 2]
    a = [r["ret"] for r in rows if r["date"] < mid]; b = [r["ret"] for r in rows if r["date"] >= mid]
    return (st.mean(a) if a else float("nan"), len(a)), (st.mean(b) if b else float("nan"), len(b))


def build(first: dt.date, last: dt.date, opt: dict) -> dict[str, list[dict]]:
    mk = {json.loads(l)["t"]: json.loads(l) for l in (LAB / "markets" / "KXRAIN.jsonl").open()}
    api = {json.loads(l)["t"]: json.loads(l) for l in (OUT / "markets_api.jsonl").open()} if (OUT / "markets_api.jsonl").exists() else {}
    for k, v in api.items():
        mk.setdefault(k, v)
        mk[k]["rules"] = v.get("rules", "")
    cd = {json.loads(l)["t"]: json.loads(l)["c"] for l in (LAB / "candles" / "KXRAIN.jsonl").open()}
    if (OUT / "candles_extra.jsonl").exists():
        for l in (OUT / "candles_extra.jsonl").open():
            x = json.loads(l); cd.setdefault(x["t"], x["c"])
    metar = {}
    out = {k: [] for k in CANDS}; skipped = defaultdict(int)
    for m in sorted(mk.values(), key=lambda m: (m["close"], m["t"])):
        d = edate(m["e"])
        if not (first <= d <= last):
            continue
        if m.get("result") not in ("yes", "no"):
            skipped["no result"] += 1; continue
        stn = station(m)
        if stn not in metar:
            metar[stn] = load_metar(stn)
        c = cd.get(m["t"])
        if c is None:
            skipped["no candles"] += 1; continue
        for name, p in CANDS.items():
            ok, info = signal(metar[stn], d, stn, p["h"], p["dd"], opt)
            if not ok:
                continue
            q = quote(c, info["t_dec"] + 60)
            if not q:
                skipped[f"{name} stale quote"] += 1; continue
            px = round(1 - q[1], 4)
            if not (0.02 <= px <= 0.99) or not (p["lo"] - 1e-9 <= px <= p["hi"] + 1e-9):
                continue
            won = m["result"] == "no"; f = fee10(px); pnl = (1.0 if won else 0.0) - px - f
            out[name].append({"t": m["t"], "e": m["e"], "date": d.isoformat(), "stn": stn, "px": px, "fee": f, "won": won, "pnl": pnl,
                              "ret": pnl / px, "dd": info["dd"], "t_dec": info["t_dec"]})
    out["_skipped"] = dict(skipped)
    return out


def report(first: dt.date, last: dt.date, opt: dict, tag: str, write: bool = False) -> dict:
    res = build(first, last, opt); summ = {}
    for name in CANDS:
        rows = res[name]; s = stats(rows); h1, h2 = halves(rows, first, last)
        s.update({"half1": h1[0], "half1_n": h1[1], "half2": h2[0], "half2_n": h2[1], "trades_per_day": len(rows) / ((last - first).days + 1)})
        summ[name] = s
        print(f"{tag} {name}: n={s['n']} ev={s.get('events')} win={s.get('win', 0):.3f} px={s.get('avg_px', 0):.3f} "
              f"ret={s.get('ret_per_dollar', 0):+.4f} t={s.get('t', float('nan')):.3f} wo3={s.get('ret_wo3', float('nan')):+.4f} "
              f"h1={h1[0]:+.3f}({h1[1]}) h2={h2[0]:+.3f}({h2[1]})")
        if write:
            (OUT / f"trades_{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(tag, "skipped", res["_skipped"])
    return summ


# ----------------------------------------------------------------------------------------------------------- robustness
def generic(first: dt.date, last: dt.date, h: int, lo: float, hi: float, opt: dict, filt: str = "full", dd_min: float = 0,
            delay: int = 60) -> list[dict]:
    """filt: 'none' (no METAR condition), 'dry' (no measurable so far only), 'full' (the N trigger incl. 3-h convection)."""
    mk = {json.loads(l)["t"]: json.loads(l) for l in (LAB / "markets" / "KXRAIN.jsonl").open()}
    api = {json.loads(l)["t"]: json.loads(l) for l in (OUT / "markets_api.jsonl").open()}
    for k, v in api.items():
        mk.setdefault(k, v); mk[k]["rules"] = v.get("rules", "")
    cd = {json.loads(l)["t"]: json.loads(l)["c"] for l in (LAB / "candles" / "KXRAIN.jsonl").open()}
    for l in (OUT / "candles_extra.jsonl").open():
        x = json.loads(l); cd.setdefault(x["t"], x["c"])
    metar = {}; out = []
    for m in mk.values():
        d = edate(m["e"])
        if not (first <= d <= last) or m.get("result") not in ("yes", "no") or m["t"] not in cd:
            continue
        stn = station(m)
        if stn not in metar:
            metar[stn] = load_metar(stn)
        ok, info = signal(metar[stn], d, stn, h, dd_min, opt)
        if filt == "dry":
            ok = info.get("meas") is False and (dd_min <= 0 or (info.get("dd") or -99) >= dd_min)
        elif filt == "none":
            ok = True; info = {"t_dec": int(dec_time(d, stn, h).timestamp()), "dd": None}
        if not ok:
            continue
        q = quote(cd[m["t"]], info["t_dec"] + delay)
        if not q:
            continue
        px = round(1 - q[1], 4)
        if not (0.02 <= px <= 0.99) or not (lo - 1e-9 <= px <= hi + 1e-9):
            continue
        won = m["result"] == "no"; f = fee10(px); pnl = (1.0 if won else 0.0) - px - f
        out.append({"t": m["t"], "e": m["e"], "date": d.isoformat(), "px": px, "won": won, "pnl": pnl, "ret": pnl / px})
    return out


def boot(rows: list[dict], n: int = 4000, seed: int = 7) -> tuple:
    import random
    rnd = random.Random(seed); ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    keys = list(ev); res = []
    for _ in range(n):
        s = [x for k in (rnd.choice(keys) for _ in keys) for x in ev[k]]
        res.append(st.mean(s))
    res.sort()
    return res[int(0.025 * n)], res[int(0.975 * n)], sum(x <= 0 for x in res) / n


def robust() -> dict:
    base = {"cb_rmk": True, "wx_rmk": False, "p_after": 0}
    claim = {"cb_rmk": True, "wx_rmk": True, "broad": True, "dd_round": True, "p_after": 0}
    R = {}
    def show(tag, rows):
        s = stats(rows)
        if rows:
            lo, hi, p0 = boot(rows); s.update({"boot95": [lo, hi], "boot_p_le0": p0})
        R[tag] = s
        print(f"{tag:58s} n={s['n']:4d} ev={s.get('events', 0):3d} px={s.get('avg_px', 0):.3f} ret={s.get('ret_per_dollar', 0):+.4f} "
              f"t={s.get('t', float('nan')):+.2f} wo3={s.get('ret_wo3', float('nan')):+.3f} boot95={[round(x, 3) for x in s.get('boot95', [])]}")
        return rows
    V0, V1 = VAL_FIRST, VAL_LAST
    print("--- validation 09-14..10-07: what does the METAR filter add? (14:00, NO in [0.30,0.90])")
    show("no filter (all markets)", generic(V0, V1, 14, 0.30, 0.90, base, "none"))
    show("dry so far only", generic(V0, V1, 14, 0.30, 0.90, base, "dry"))
    show("dry so far + dd>=10", generic(V0, V1, 14, 0.30, 0.90, base, "dry", 10))
    show("full N trigger, no dd", generic(V0, V1, 14, 0.30, 0.90, base, "full", 0))
    c3 = show("C3 strict reading", generic(V0, V1, 14, 0.30, 0.90, base, "full", 10))
    show("C3 claimant reading", generic(V0, V1, 14, 0.30, 0.90, claim, "full", 10))
    print("--- C3 strict by NO price band")
    for lo, hi in ((0.30, 0.50), (0.50, 0.70), (0.70, 0.80), (0.80, 0.90)):
        show(f"  band [{lo:.2f},{hi:.2f})", [r for r in c3 if lo <= r["px"] < hi or (hi == 0.90 and r["px"] == 0.90)])
    print("--- C3 strict: per-event contribution (sum of ret)")
    ev = defaultdict(list)
    for r in c3:
        ev[r["e"]].append(r["ret"])
    contrib = sorted(((round(sum(v), 3), len(v), k) for k, v in ev.items()), reverse=True)
    print("  ", contrib)
    tot = sum(r["ret"] for r in c3)
    print(f"   total sum ret {tot:.2f}; without best event {tot - contrib[0][0]:.2f} -> mean {(tot - contrib[0][0]) / (len(c3) - contrib[0][1]):+.4f}")
    R["c3_event_contrib"] = contrib
    print("--- C3 strict: fill delay")
    for dl in (120, 300):
        show(f"  delay {dl}s", generic(V0, V1, 14, 0.30, 0.90, base, "full", 10, dl))
    print("--- neighbours on validation (robustness only, not selection)")
    for h in (13, 15, 16):
        show(f"  h{h} dd>=10 [0.30,0.90]", generic(V0, V1, h, 0.30, 0.90, base, "full", 10))
    for ddm in (5, 8, 12, 15):
        show(f"  h14 dd>={ddm} [0.30,0.90]", generic(V0, V1, 14, 0.30, 0.90, base, "full", ddm))
    show("  h14 dd>=10 [0.30,0.95]", generic(V0, V1, 14, 0.30, 0.95, base, "full", 10))
    show("  h14 dd>=10 [0.30,0.85]", generic(V0, V1, 14, 0.30, 0.85, base, "full", 10))
    print("--- C3 strict on all lab-candle dates 08-23..10-07 (overlaps validation)")
    show("  C3 08-23..09-13 (part of claimant discovery)", generic(dt.date(2026, 8, 23), dt.date(2026, 9, 13), 14, 0.30, 0.90, base, "full", 10))
    show("  C3 08-23..10-07 pooled", generic(dt.date(2026, 8, 23), V1, 14, 0.30, 0.90, base, "full", 10))
    show("  no-filter 14:00 [0.30,0.90] 08-23..10-07", generic(dt.date(2026, 8, 23), V1, 14, 0.30, 0.90, base, "none"))
    return R


def main() -> None:
    mode = (sys.argv[1:] or ["run"])[0]
    if mode == "fetch":
        do_fetch(); return
    OUT.mkdir(parents=True, exist_ok=True)
    if mode == "robust":
        json.dump(robust(), (OUT / "robustness.json").open("w"), indent=1, default=str); return
    base = {"cb_rmk": True, "wx_rmk": False, "p_after": 0}
    claim = {"cb_rmk": True, "wx_rmk": True, "broad": True, "dd_round": True, "p_after": 0}
    main_ = report(VAL_FIRST, VAL_LAST, base, "VAL strict", write=True)
    sens = {}
    for tag, opt in {"claimant_reading(broad remarks, whole-F dd)": claim, "cb_body_only": {**base, "cb_rmk": False},
                     "with_rmk_begin_end": {**base, "wx_rmk": True}, "p_after_70min": {**base, "p_after": 70}}.items():
        sens[tag] = report(VAL_FIRST, VAL_LAST, opt, "VAL/" + tag)
    disc = report(dt.date(2026, 8, 23), dt.date(2026, 9, 13), base, "DISC(lab candles 08-23..09-13)")
    json.dump({"validation_strict": main_, "sensitivity": sens, "discovery_partial_08_23_09_13": disc}, (OUT / "repro_summary.json").open("w"), indent=1, default=str)


if __name__ == "__main__":
    main()
