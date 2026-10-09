"""r6_weather_d1_mos_brackets common: guidance (IEM NBS / GFS MAV / NAM MET), CLI truth, Kalshi ladders with hourly
quotes at the decision times, and the walk-forward calibrated bracket model.

Decision times (local clock, the city's tz):
  A = D-1 15:59, fill at the 16:00 quote (close of the hourly candle ending 16:00, or the last earlier candle);
  B = D 06:59, fill at the 07:00 quote.
Guidance is usable only from runtime + LAG (NBS 2 h, GFS MAV 4.5 h, NAM MET 3.5 h; conservative issuance delays).
Max forecast for climate day D: NBS 'txn' / MOS 'n_x' at ftime (D+1) 00Z (the daytime max column).
Bias and spread are walk-forward: only CLI days whose report was out before the decision (A: <= D-2, B: <= D-1)."""
from __future__ import annotations
import bisect, csv, datetime as dt, json, math, statistics as st, sys, zoneinfo
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_weather_d1_mos_brackets_data import OUT, IEM, CF, markets, load_candles
from lab.us.kalshi_backtest import SERIES

LAG_H = {"NBS": 2.0, "GFS": 4.5, "NAM": 3.5}
MAIN7 = {"KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHPHIL"}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
DEC = {"A": (-1, 16), "B": (0, 7)}      # (day offset from D, local fill hour); decision is 1 minute before the fill
FEE = 0.07


def ev_date(e: str) -> dt.date:
    s = e.split("-")[1]
    return dt.date(2000 + int(s[:2]), MON[s[2:5]], int(s[5:7]))


def fee_order(p: float, c: int) -> float:
    """Kalshi taker fee for one order of c contracts at price p, rounded up to the cent (dollars)."""
    return math.ceil(round(FEE * c * p * (1 - p) * 100, 6)) / 100


def ret_5usd(p: float, won: bool, stake: float = 5.0) -> float:
    c = max(1, int(stake // p))
    return (c * (1.0 if won else 0.0) - c * p - fee_order(p, c)) / (c * p)


# ---------------------------------------------------------------- guidance
def load_guidance() -> dict:
    """(model, icao) -> {D: sorted [(avail_ts, runtime_ts, value, sd)]}."""
    out = {}
    for f in sorted(IEM.glob("*_K*.txt")):
        model, icao = f.name.split("_")[:2]
        if model not in LAG_H:
            continue
        by = defaultdict(list)
        col = "txn" if model == "NBS" else "n_x"
        for r in csv.DictReader(f.open()):
            ft = r.get("ftime") or ""
            if ft[11:13] != "00" or not r.get(col):
                continue
            try:
                v = float(r[col])
            except ValueError:
                continue
            rt = dt.datetime.fromisoformat(r["runtime"]).replace(tzinfo=dt.timezone.utc)
            ftd = dt.datetime.fromisoformat(ft).replace(tzinfo=dt.timezone.utc)
            if (ftd - rt).total_seconds() < 6 * 3600:      # the daytime max period has already started
                continue
            D = (ftd - dt.timedelta(days=1)).date()
            sd = None
            if model == "NBS" and r.get("xnd"):
                try:
                    sd = float(r["xnd"])
                except ValueError:
                    sd = None
            avail = rt.timestamp() + LAG_H[model] * 3600
            by[D].append((avail, rt.timestamp(), v, sd))
        for D in by:
            by[D].sort()
        out[(model, icao)] = dict(by)
    return out


def latest(g: dict, D: dt.date, t: float):
    xs = g.get(D) or []
    best = None
    for x in xs:
        if x[0] <= t:
            best = x
        else:
            break
    return best


def load_cli() -> dict:
    """icao -> {date: high} from IEM CLI json (2024-2026) plus the lab's cli_high.json."""
    out = defaultdict(dict)
    for f in IEM.glob("cli_K*.txt"):
        icao = f.name.split("_")[1]
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        for r in d.get("results") or []:
            h = r.get("high")
            if h in (None, "M", ""):
                continue
            try:
                out[icao][dt.date.fromisoformat(r["valid"])] = float(h)
            except (ValueError, TypeError):
                pass
    try:
        for icao, v in json.loads(Path("data/lab/us/asos/cli_high.json").read_text()).items():
            for k, h in v.items():
                out[icao].setdefault(dt.date.fromisoformat(k), float(h))
    except FileNotFoundError:
        pass
    return out


def dec_ts(D: dt.date, tz: str, which: str) -> float:
    off, hr = DEC[which]
    d = D + dt.timedelta(days=off)
    return dt.datetime(d.year, d.month, d.day, hr, 0, tzinfo=zoneinfo.ZoneInfo(tz)).timestamp()


# ---------------------------------------------------------------- the model
def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bracket_p(lo: float, hi: float, mu: float, sd: float) -> float:
    a = 0.0 if lo < -1e8 else ncdf((lo - 0.5 - mu) / sd)
    b = 1.0 if hi > 1e8 else ncdf((hi + 0.5 - mu) / sd)
    return max(0.0, b - a)


def interval(m: dict) -> tuple[float, float]:
    if m["type"] == "less":
        return -1e9, float(m["cap"]) - 1
    if m["type"] == "greater":
        return float(m["floor"]) + 1, 1e9
    return float(m["floor"]), float(m["cap"])


class Calib:
    """Walk-forward residual store: residual r = CLI - forecast for (station, decision, model-combo), by date."""

    def __init__(self):
        self.r = defaultdict(list)    # key -> sorted [(date, resid)]

    def add(self, key, D, r):
        self.r[key].append((D, r))

    def finish(self):
        for k in self.r:
            self.r[k].sort()

    def before(self, key, D_last: dt.date) -> list[tuple[dt.date, float]]:
        xs = self.r.get(key) or []
        i = bisect.bisect_right(xs, (D_last, float("inf")))
        return xs[:i]

    def bias_sd(self, key, D: dt.date, which: str, mode: str, k_shrink: float = 10.0, win: int = 30):
        """(bias, sd, n) from residual days known at the decision: A -> <= D-2, B -> <= D-1."""
        last = D - dt.timedelta(days=2 if which == "A" else 1)
        xs = self.before(key, last)
        if len(xs) < 30:
            return None
        allr = [r for _, r in xs]
        m_all = st.mean(allr)
        if mode == "none":
            b = 0.0
        else:
            same = [r for d, r in xs if d.month == D.month]
            b_month = (sum(same) + k_shrink * m_all) / (len(same) + k_shrink)
            recent = [r for d, r in xs if (last - d).days < win]
            b_roll = st.mean(recent) if len(recent) >= 10 else b_month
            b = {"month": b_month, "roll": b_roll, "mix": 0.5 * (b_month + b_roll), "station": m_all}[mode]
        # spread: residual sd about the bias, from the last 365 days (same seasonal mix as the bias)
        rec = [(d, r) for d, r in xs if (last - d).days < 365]
        if mode == "none":
            sd = math.sqrt(st.mean([r * r for _, r in rec]))
        else:
            same = [r for d, r in rec if d.month == D.month]
            mm = (sum(same) + k_shrink * m_all) / (len(same) + k_shrink)
            sd = math.sqrt(st.mean([(r - (mm if d.month == D.month else m_all)) ** 2 for d, r in rec]))
        return b, max(sd, 0.8), len(xs)


COMBOS = {"NBS": ("NBS",), "GFS": ("GFS",), "NBS+GFS": ("NBS", "GFS"), "ALL3": ("NBS", "GFS", "NAM")}


def combo_forecast(G: dict, icao: str, D: dt.date, t: float, combo: str):
    vals = []; sd = None; ages = []
    for mdl in COMBOS[combo]:
        x = latest(G.get((mdl, icao), {}), D, t)
        if x is None:
            return None
        vals.append(x[2]); ages.append((t - x[1]) / 3600)
        if mdl == "NBS":
            sd = x[3]
    return st.mean(vals), sd, max(ages)


def build_calib(G: dict, CLI: dict, start: dt.date, end: dt.date) -> Calib:
    """Residuals for every station-day in [start, end] with a CLI high and guidance at the decision time."""
    C = Calib()
    for s, (stn, icao, tz) in SERIES.items():
        D = start
        while D <= end:
            h = CLI.get(icao, {}).get(D)
            if h is not None:
                for which in DEC:
                    t = dec_ts(D, tz, which) - 60
                    for combo in COMBOS:
                        f = combo_forecast(G, icao, D, t, combo)
                        if f is not None:
                            C.add((icao, which, combo), D, h - f[0])
            D += dt.timedelta(days=1)
    C.finish()
    return C


def quote_at(c: list[list], t: float, max_age_h: float = 6.0):
    """(yes_ask, yes_bid, age_h, vol_last_3h) from the last hourly candle ending at or before t."""
    best = None; vol = 0.0
    for r in c:
        if r[0] <= t:
            best = r
            if t - r[0] < 3 * 3600 + 1:
                vol += r[5] or 0
        else:
            break
    if not best:
        return None
    age = (t - best[0]) / 3600
    if age > max_age_h:
        return None
    ask = best[1] if best[1] is not None else 1.0
    bid = best[2] if best[2] is not None else 0.0
    return ask, bid, age, vol


def ladders() -> dict:
    """event -> {series, D, icao, tz, markets:[...]} for the KXHIGH* ladders."""
    out = {}
    for m in markets():
        stn, icao, tz = SERIES[m["series"]]
        e = out.setdefault(m["e"], {"e": m["e"], "series": m["series"], "D": ev_date(m["e"]), "icao": icao, "tz": tz, "close": m["close"], "markets": []})
        e["markets"].append(m); e["close"] = max(e["close"], m["close"])
    return out
