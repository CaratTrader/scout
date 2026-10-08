"""r2_thin_city_anchor: price the thin new Kalshi daily-high cities (KXHIGHTEWR / TTN / SAN) from their liquid
neighbour's ladder (KXHIGHNY / KXHIGHPHIL / KXHIGHLAX) shifted by the historical CLI daily-high difference, and quote
inside the thin city's wide spread as a maker (KXHIGH* fee_type 'quadratic': makers pay no fee).

Data (disk only; Kalshi trade prints are checked separately by `trades`):
  data/lab/us/kalshi/markets.json + <ticker>.json 1-minute candles (14 h before close; sparse: only minutes with activity)
  data/kalshi_lab/strategies/r2_thin_city_anchor/cli_hist.json  NWS CLI highs 2015-2025 (IEM), for the difference D
  data/lab/us/asos_raw/<STN>.csv  routine/special METAR (T-group tenths) for the thin city's observed max so far
Settlement check (step 0): Kalshi expiration_value == NWS CLI high on 49/49 SAN, 42/42 EWR, 42/42 TTN, 40/40 SDF dates
(series_all.json lists 'The Weather Company', but the value settled is the CLI).

Fair value at time t (data stamped <= t only):
  neighbour ladder mids (quotes <= 30 min old, all 6 buckets) -> integer pmf of the neighbour high
  ('normal': discretised normal fitted to the 6 bucket mids; 'np': 2F buckets split evenly, tails geometric r=0.6)
  convolved with the month's empirical D = thin CLI high - neighbour CLI high (2015-2025)
  truncated below the thin station's observed max so far (METAR T-group, available 5 min after valid time)
Maker simulation: from local hour H the quote is recomputed every minute for W=60 minutes from data <= tau; the order
is live during the candle minute ending tau+120 s (2 min information-to-fill lag). YES bid b = fair - m (floored to the
cent), YES ask a = fair + m (ceiled). Fill only on a strict trade-through in that minute (trade print < b fills the bid,
> a fills the ask) at the order's own price, no fee (maker, quadratic). One unit per market (first fill only).
Split: city-dates ordered by close time; discovery = first 70%, validation = last 30%; t clustered by date.
Usage: .venv/bin/python lab/kalshi/strategies/r2_thin_city_anchor.py [brier|sim|validate|trades|book|all]"""
from __future__ import annotations
import csv, datetime as dt, json, math, re, statistics as st, sys, zoneinfo
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data/kalshi_lab/strategies/r2_thin_city_anchor"
OUT.mkdir(parents=True, exist_ok=True)
KD = ROOT / "data/lab/us/kalshi"
PAIRS = {"KXHIGHTEWR": ("KXHIGHNY", "KEWR", "KNYC", "EWR", "America/New_York"),
         "KXHIGHTTTN": ("KXHIGHPHIL", "KTTN", "KPHL", "TTN", "America/New_York"),
         "KXHIGHTSAN": ("KXHIGHLAX", "KSAN", "KLAX", "SAN", "America/Los_Angeles")}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
HOURS = {"11:30": (11, 30), "13:00": (13, 0), "15:00": (15, 0)}
MARGINS = (0.02, 0.03, 0.05)
W_MIN = 60
MAX_AGE = 30 * 60
OBS_LAG = 5 * 60
SPLIT = 0.7
VARIANTS: list[str] = []   # every cell examined (counted in variants_examined)


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def ev_date(ev: str) -> dt.date:
    s = ev.split("-")[1]
    return dt.date(2000 + int(s[:2]), MON[s[2:5]], int(s[5:]))


def fee_pc(p: float, n: int = 10) -> float:
    return math.ceil(round(100 * 0.07 * p * (1 - p) * n, 6)) / 100 / n


def bucket(m: dict) -> tuple[int, int]:
    t = m["strike_type"]
    if t == "less":
        return -999, int(m["cap_strike"]) - 1
    if t == "greater":
        return int(m["floor_strike"]) + 1, 999
    return int(m["floor_strike"]), int(m["cap_strike"])


def load_candles(tk: str) -> list[tuple]:
    """[(end_ts, ask, bid, trade_lo, trade_hi, vol)] sorted; ask None -> 1.0 (no offers), bid None -> 0.0."""
    f = KD / f"{tk}.json"
    if not f.exists():
        return []
    try:
        raw = json.loads(f.read_text())
    except Exception:
        return []
    out = []
    for c in raw:
        g = lambda k, f2: (c.get(k) or {}).get(f2)
        a, b = g("yes_ask", "close_dollars"), g("yes_bid", "close_dollars")
        a = float(a) if a is not None else 1.0
        b = float(b) if b is not None else 0.0
        lo, hi = g("price", "low_dollars"), g("price", "high_dollars")
        out.append((int(c["end_period_ts"]), a, b, float(lo) if lo is not None else None, float(hi) if hi is not None else None,
                    float(c.get("volume_fp") or 0)))
    out.sort()
    return out


def quote(c: list, t: int, max_age: int = MAX_AGE):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > max_age:
        return None
    return best[1], best[2], t - best[0]


# ------------------------------------------------------------------ difference model (no Kalshi data)
def d_pmfs() -> dict:
    h = json.loads((OUT / "cli_hist.json").read_text())
    out = {}
    for thin, (nb, st_t, st_n, _, _) in PAIRS.items():
        for mo in range(1, 13):
            D = Counter(int(round(h[st_t][d] - h[st_n][d])) for d in h[st_t] if d in h[st_n] and int(d[5:7]) == mo)
            if not D:
                continue
            lo, hi = min(D) - 2, max(D) + 2
            tot = sum(D.values()) + 0.5 * (hi - lo + 1)
            out[(thin, mo)] = {k: (D.get(k, 0) + 0.5) / tot for k in range(lo, hi + 1)}
    return out


# ------------------------------------------------------------------ neighbour distribution
def Phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bucket_probs_normal(bks, mu, s):
    return [Phi((hi + 0.5 - mu) / s) - Phi((lo - 0.5 - mu) / s) for lo, hi in bks]


def fit_normal(bks, mids):
    core = [(lo, hi) for lo, hi in bks if lo > -999 and hi < 999]
    lo0 = min(lo for lo, _ in core); hi0 = max(hi for _, hi in core)
    cen = []
    for (lo, hi), p in zip(bks, mids):
        cen.append((lo0 - 1.5 if lo == -999 else hi0 + 1.5 if hi == 999 else (lo + hi) / 2, p))
    mu = sum(c * p for c, p in cen)
    s = max(1.0, math.sqrt(max(0.25, sum(p * (c - mu) ** 2 for c, p in cen))))
    obj = lambda mu_, s_: sum((q - p) ** 2 for q, p in zip(bucket_probs_normal(bks, mu_, s_), mids))
    best = obj(mu, s); step_m, step_s = 1.0, 0.5
    for _ in range(60):
        moved = False
        for dm, ds in ((step_m, 0), (-step_m, 0), (0, step_s), (0, -step_s)):
            m2, s2 = mu + dm, max(0.4, s + ds)
            v = obj(m2, s2)
            if v < best - 1e-12:
                best, mu, s, moved = v, m2, s2, True
        if not moved:
            step_m /= 2; step_s /= 2
            if step_m < 0.02:
                break
    return mu, s


def nb_pmf(bks, mids, model: str) -> dict[int, float]:
    if model == "normal":
        mu, s = fit_normal(bks, mids)
        lo, hi = int(math.floor(mu - 6 * s)), int(math.ceil(mu + 6 * s))
        pm = {k: Phi((k + 0.5 - mu) / s) - Phi((k - 0.5 - mu) / s) for k in range(lo, hi + 1)}
    else:
        pm = defaultdict(float)
        for (lo, hi), p in zip(bks, mids):
            if lo == -999:
                w = [0.4 * 0.6 ** i for i in range(12)]; z = sum(w)
                for i, x in enumerate(w):
                    pm[hi - i] += p * x / z
            elif hi == 999:
                w = [0.4 * 0.6 ** i for i in range(12)]; z = sum(w)
                for i, x in enumerate(w):
                    pm[lo + i] += p * x / z
            else:
                for k in range(lo, hi + 1):
                    pm[k] += p / (hi - lo + 1)
    z = sum(pm.values())
    return {k: v / z for k, v in pm.items() if v > 0}


def convolve(a: dict, b: dict) -> dict:
    out = defaultdict(float)
    for k, p in a.items():
        for d, q in b.items():
            out[k + d] += p * q
    return out


# ------------------------------------------------------------------ observations
def obs_series(stn: str, tzn: str) -> dict[dt.date, list[tuple[int, float]]]:
    """climate day -> [(available_ts, F)] from routine/special METAR T-group (tenths C)."""
    tz = zoneinfo.ZoneInfo(tzn); out = defaultdict(list)
    for r in csv.DictReader(open(ROOT / f"data/lab/us/asos_raw/{stn}.csv")):
        raw = r.get("metar") or ""
        if "MADISHF" in raw:
            continue
        m = re.search(r"\bT([01])(\d{3})[01]\d{3}\b", raw)
        if not m:
            continue
        f = (int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1)) * 9 / 5 + 32
        lt = dt.datetime.strptime(r["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        day_start_shift = 1 if lt.dst() else 0          # climate day = local standard time midnight-midnight
        cday = (lt - dt.timedelta(hours=day_start_shift)).date()
        out[cday].append((int(lt.timestamp()) + OBS_LAG, f))
    for v in out.values():
        v.sort()
    return out


def obs_floor(obs: list, t: int):
    mx = None
    for a, f in obs:
        if a <= t:
            mx = f if mx is None else max(mx, f)
        else:
            break
    return None if mx is None else math.floor(mx + 0.5)


# ------------------------------------------------------------------ dataset
def load():
    mk = json.loads((KD / "markets.json").read_text())
    by_ev = defaultdict(list)
    for tk, m in mk.items():
        if m.get("result") in ("yes", "no"):
            by_ev[m["event_ticker"]].append(m)
    days = []
    for thin, (nb, st_t, st_n, stn, tzn) in PAIRS.items():
        obs = obs_series(stn, tzn)
        for ev, ms in by_ev.items():
            if not ev.startswith(thin + "-"):
                continue
            d = ev_date(ev); nev = nb + "-" + ev.split("-")[1]
            nms = by_ev.get(nev)
            if not nms or len(ms) != 6 or len(nms) != 6:
                continue
            thin_mk = []
            for m in ms:
                c = load_candles(m["ticker"])
                thin_mk.append({"t": m["ticker"], "bk": bucket(m), "y": 1 if m["result"] == "yes" else 0, "c": c})
            nb_mk = [{"t": m["ticker"], "bk": bucket(m), "c": load_candles(m["ticker"])} for m in nms]
            days.append({"thin": thin, "ev": ev, "date": d, "close": ts(ms[0]["close_time"]), "tz": tzn,
                         "mk": thin_mk, "nb": nb_mk, "obs": obs.get(d, []), "cli": float(ms[0]["expiration_value"])})
    days.sort(key=lambda x: x["close"])
    dates = sorted({x["date"] for x in days})
    cut = dates[int(len(dates) * SPLIT)]
    for x in days:
        x["seg"] = "disc" if x["date"] < cut else "val"
    return days, cut


DPMF = None


def fair_at(day: dict, t: int, model: str, use_floor: bool = True):
    """{ticker: fair prob} for the thin ladder from neighbour quotes stamped <= t, or None."""
    mids, bks = [], []
    for m in day["nb"]:
        q = quote(m["c"], t)
        if not q:
            return None
        a, b, _ = q
        mids.append((a + b) / 2); bks.append(m["bk"])
    z = sum(mids)
    if z <= 0:
        return None
    mids = [x / z for x in mids]
    pm = convolve(nb_pmf(bks, mids, model), DPMF[(day["thin"], day["date"].month)])
    if use_floor:
        fl = obs_floor(day["obs"], t)
        if fl is not None:
            tot = sum(p for k, p in pm.items() if k >= fl)
            pm = {k: p / tot for k, p in pm.items() if k >= fl} if tot > 1e-9 else {fl: 1.0}
    out = {}
    for m in day["mk"]:
        lo, hi = m["bk"]
        out[m["t"]] = min(0.999, max(0.001, sum(p for k, p in pm.items() if lo <= k <= hi)))
    return out


def local_ts(day: dict, hh: int, mm: int) -> int:
    tz = zoneinfo.ZoneInfo(day["tz"]); d = day["date"]
    return int(dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).timestamp())


# ------------------------------------------------------------------ statistics
def cstats(rows: list[dict], key="ret") -> dict:
    if not rows:
        return {"n": 0}
    n = len(rows); mean = sum(r[key] for r in rows) / n
    by = defaultdict(float)
    for r in rows:
        by[r["cl"]] += r[key] - mean
    g = len(by)
    v = sum(s * s for s in by.values()) / n ** 2 * g / (g - 1) if g > 1 else 0
    t = mean / math.sqrt(v) if v > 0 else float("nan")
    rs = sorted((r[key] for r in rows), reverse=True)
    out = {"n": n, "events": len({r["ev"] for r in rows}), "dates": g, "win": round(sum(r["won"] for r in rows) / n, 3),
           "avg_px": round(sum(r["px"] for r in rows) / n, 3), "ret": round(mean, 4), "t": round(t, 2),
           "ret_wo3": round(sum(rs[3:]) / (n - 3), 4) if n > 3 else None}
    ds = sorted({r["cl"] for r in rows})
    if len(ds) >= 2:
        mid = ds[len(ds) // 2]
        h1 = [r[key] for r in rows if r["cl"] < mid]; h2 = [r[key] for r in rows if r["cl"] >= mid]
        out["half1"] = round(sum(h1) / len(h1), 4) if h1 else None
        out["half2"] = round(sum(h2) / len(h2), 4) if h2 else None
    return out


# ------------------------------------------------------------------ step 2a: Brier (kill test 1)
def brier(days):
    res = {}
    for hk, (hh, mm) in HOURS.items():
        for model in ("normal", "np"):
            for seg in ("disc", "val"):
                for floor in (True, False):
                    if seg == "val":
                        continue   # Brier is a discovery-only kill test; validation is read once at the end
                    tag = f"{hk}|{model}|floor={floor}|{seg}"
                    VARIANTS.append("brier|" + tag)
                    rows = []
                    for day in days:
                        if day["seg"] != seg:
                            continue
                        t = local_ts(day, hh, mm)
                        fv = fair_at(day, t, model, floor)
                        if not fv:
                            continue
                        for m in day["mk"]:
                            q = quote(m["c"], t, 6 * 3600)
                            if not q:
                                continue
                            a, b, age = q
                            mid = (a + b) / 2; f = fv[m["t"]]
                            rows.append({"cl": str(day["date"]), "city": day["thin"], "y": m["y"], "mid": mid, "f": f, "spr": a - b,
                                         "fresh": age <= MAX_AGE})
                    if not rows:
                        continue
                    def bs(rr, k):
                        return round(sum((r[k] - r["y"]) ** 2 for r in rr) / len(rr), 4) if rr else None
                    # date-clustered SE of the Brier difference (fair - mid)
                    byd = defaultdict(list)
                    for r in rows:
                        byd[r["cl"]].append((r["f"] - r["y"]) ** 2 - (r["mid"] - r["y"]) ** 2)
                    dm = [sum(v) for v in byd.values()]; n = len(rows); g = len(dm)
                    md = sum(sum(v) for v in byd.values()) / n
                    var = sum((sum(v) - md * len(v)) ** 2 for v in byd.values()) / n ** 2 * g / (g - 1)
                    blend = [dict(r, b5=0.5 * r["f"] + 0.5 * r["mid"]) for r in rows]
                    per_city = {c: {"n": len(rr), "brier_mid": bs(rr, "mid"), "brier_fair": bs(rr, "f")}
                                for c in PAIRS for rr in [[r for r in rows if r["city"] == c]] if rr}
                    fr = [r for r in rows if r["fresh"]]
                    res[tag] = {"n": n, "dates": g, "brier_mid": bs(rows, "mid"), "brier_fair": bs(rows, "f"),
                                "brier_blend50": bs(blend, "b5"), "diff_fair_minus_mid": round(md, 4),
                                "diff_t": round(md / math.sqrt(var), 2) if var > 0 else None,
                                "fresh30_n": len(fr), "fresh30_brier_mid": bs(fr, "mid"), "fresh30_brier_fair": bs(fr, "f"),
                                "median_spread": round(st.median(r["spr"] for r in rows), 3), "per_city": per_city}
                    print(tag, res[tag], flush=True)
    (OUT / "brier.json").write_text(json.dumps(res, indent=1))
    return res


# ------------------------------------------------------------------ step 2b: maker simulation
def simulate(days, hk: str, margin: float, model: str, segs=("disc",), take: bool = False, window: int = W_MIN,
             fairmode: str = "anchor", cities=tuple(PAIRS), inside: bool = False):
    """fairmode 'anchor' = neighbour-anchored fair; 'blend' = 0.5 anchor + 0.5 thin mid (thin quote <= 6 h old).
    Maker orders are post-only: a bid is posted only below the thin ask and an offer only above the thin bid seen at tau
    (thin quotes carried forward up to 6 h: candles exist only for minutes with activity). take=True instead lifts a
    crossing quote as a taker at the quote one minute later (fee rounded up per 10-lot)."""
    hh, mm = HOURS[hk]
    fills = []
    for day in days:
        if day["seg"] not in segs or day["thin"] not in cities:
            continue
        t0 = local_ts(day, hh, mm)
        done = set()
        for k in range(window):
            tau = t0 + 60 * k
            if tau + 120 > day["close"]:
                break
            fv = fair_at(day, tau, model, True)
            if not fv:
                continue
            for m in day["mk"]:
                if m["t"] in done:
                    continue
                f = fv[m["t"]]
                qt = quote(m["c"], tau, 6 * 3600)
                if fairmode == "blend" and qt:
                    f = 0.5 * f + 0.5 * (qt[0] + qt[1]) / 2
                b = math.floor(round((f - margin) * 100, 6)) / 100
                a = math.ceil(round((f + margin) * 100, 6)) / 100
                qb = b if 0.02 <= b <= 0.97 else None
                qa = a if 0.03 <= a <= 0.98 else None
                if qb is None and qa is None:
                    continue
                if take:
                    q1 = quote(m["c"], tau + 60)
                    if q1:
                        ask1, bid1, _ = q1
                        if qb is not None and ask1 <= qb and quote(m["c"], tau):
                            px = ask1; pnl = m["y"] - px - fee_pc(px)
                            fills.append(dict(cl=str(day["date"]), ev=day["ev"], city=day["thin"], t=m["t"], side="YES", px=px, won=m["y"] == 1,
                                              ret=pnl / px, kind="take", vol=None, fair=f, tfill=tau + 60, seg=day["seg"]))
                            done.add(m["t"]); continue
                        if qa is not None and bid1 >= qa and quote(m["c"], tau):
                            px = 1 - bid1; pnl = (1 - m["y"]) - px - fee_pc(px)
                            fills.append(dict(cl=str(day["date"]), ev=day["ev"], city=day["thin"], t=m["t"], side="NO", px=px, won=m["y"] == 0,
                                              ret=pnl / px, kind="take", vol=None, fair=f, tfill=tau + 60, seg=day["seg"]))
                            done.add(m["t"]); continue
                # post-only: never post a bid at/above the thin ask or an offer at/below the thin bid
                if qt:
                    if qb is not None and qb >= qt[0] - 1e-9:
                        qb = None
                    if qa is not None and qa <= qt[1] + 1e-9:
                        qa = None
                    if inside:   # only improve the thin quote (never rest behind it)
                        if qb is not None and qb <= qt[1] + 1e-9:
                            qb = None
                        if qa is not None and qa >= qt[0] - 1e-9:
                            qa = None
                elif inside:
                    continue
                if qb is None and qa is None:
                    continue
                # the order is live during the candle minute ending tau + 120
                cnd = next((r for r in m["c"] if r[0] == tau + 120), None)
                if not cnd or cnd[3] is None:
                    continue
                lo, hi, vol = cnd[3], cnd[4], cnd[5]
                if qb is not None and lo < qb - 1e-9:
                    px = qb; pnl = m["y"] - px
                    fills.append(dict(cl=str(day["date"]), ev=day["ev"], city=day["thin"], t=m["t"], side="YES", px=px, won=m["y"] == 1,
                                      ret=pnl / px, kind="make", vol=vol, fair=f, tfill=tau + 120, trade_lo=lo, trade_hi=hi, seg=day["seg"]))
                    done.add(m["t"])
                elif qa is not None and hi > qa + 1e-9:
                    px = 1 - qa; pnl = (1 - m["y"]) - px
                    fills.append(dict(cl=str(day["date"]), ev=day["ev"], city=day["thin"], t=m["t"], side="NO", px=round(px, 2), won=m["y"] == 0,
                                      ret=pnl / px, kind="make", vol=vol, fair=f, tfill=tau + 120, trade_lo=lo, trade_hi=hi, seg=day["seg"]))
                    done.add(m["t"])
    return fills


def summarize(fills):
    s = cstats(fills)
    if fills:
        vols = [r["vol"] for r in fills if r.get("vol") is not None]
        s["median_minute_volume_at_fill"] = st.median(vols) if vols else None
        s["by_side"] = {sd: cstats([r for r in fills if r["side"] == sd]) for sd in ("YES", "NO")}
        s["by_city"] = {c: cstats([r for r in fills if r["city"] == c]) for c in PAIRS}
        s["by_kind"] = {k: cstats([r for r in fills if r["kind"] == k]) for k in ("make", "take")}
    return s


CITYSETS = {"all3": tuple(PAIRS), "EWR+TTN": ("KXHIGHTEWR", "KXHIGHTTTN")}


def sim(days):
    res = {}
    model = "normal"
    for take in (False, True):
        for fm in ("anchor", "blend"):
            for cs, cities in CITYSETS.items():
                for hk in HOURS:
                    for mg in MARGINS:
                        if take and fm == "blend":
                            continue
                        tag = f"{hk}|m={mg}|{fm}|{cs}|take={take}"
                        VARIANTS.append("sim|" + tag)
                        f = simulate(days, hk, mg, model, ("disc",), take, W_MIN, fm, cities)
                        res[tag] = summarize(f)
                        s = res[tag]
                        print(f"{tag:44s} n={s.get('n')} dates={s.get('dates')} win={s.get('win')} px={s.get('avg_px')} ret={s.get('ret')} t={s.get('t')} "
                              f"wo3={s.get('ret_wo3')} vol={s.get('median_minute_volume_at_fill')} | YES {s.get('by_side', {}).get('YES', {}).get('ret')} "
                              f"(n={s.get('by_side', {}).get('YES', {}).get('n')}) NO {s.get('by_side', {}).get('NO', {}).get('ret')} "
                              f"(n={s.get('by_side', {}).get('NO', {}).get('n')})", flush=True)
    (OUT / "sim_discovery.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def ext(days):
    """Discovery-only extensions after the first grid lost everywhere: wider margins, and inside-the-spread-only quotes."""
    res = {}
    for inside in (False, True):
        for hk in HOURS:
            for mg in ((0.08, 0.10, 0.15) if not inside else MARGINS + (0.08, 0.10, 0.15)):
                tag = f"{hk}|m={mg}|anchor|all3|take=False|inside={inside}"
                VARIANTS.append("ext|" + tag)
                s = summarize(simulate(days, hk, mg, "normal", ("disc",), False, W_MIN, "anchor", tuple(PAIRS), inside))
                res[tag] = s
                print(f"{tag:52s} n={s.get('n')} dates={s.get('dates')} win={s.get('win')} px={s.get('avg_px')} ret={s.get('ret')} t={s.get('t')} "
                      f"wo3={s.get('ret_wo3')} | YES {s.get('by_side', {}).get('YES', {}).get('ret')} (n={s.get('by_side', {}).get('YES', {}).get('n')}) "
                      f"NO {s.get('by_side', {}).get('NO', {}).get('ret')} (n={s.get('by_side', {}).get('NO', {}).get('n')})", flush=True)
    (OUT / "sim_discovery_ext.json").write_text(json.dumps(res, indent=1, default=str))


# The three frozen candidates (chosen on discovery only, before any validation number was computed): the maker cell
# with the best discovery mean (11:30, margin 15c: +118%/$ but -75% without its 3 best trades, i.e. a few cheap YES
# bids that won), the least-bad taker cell and the least-bad blend cell of the discovery grids.
CANDIDATES = [
    {"rule": "maker 11:30 local, W=60 min requote, margin 15c, anchor(normal)+floor, EWR/TTN/SAN", "hk": "11:30", "m": 0.15, "fm": "anchor", "cities": tuple(PAIRS), "take": False, "inside": False},
    {"rule": "taker on cross 11:30 local, margin 5c, anchor(normal)+floor, EWR/TTN", "hk": "11:30", "m": 0.05, "fm": "anchor", "cities": ("KXHIGHTEWR", "KXHIGHTTTN"), "take": True, "inside": False},
    {"rule": "maker 11:30 local, margin 5c, blend 50/50 anchor+thin mid, EWR/TTN/SAN", "hk": "11:30", "m": 0.05, "fm": "blend", "cities": tuple(PAIRS), "take": False, "inside": False},
]


def validate(days):
    out = []
    for c in CANDIDATES:
        f = simulate(days, c["hk"], c["m"], "normal", ("val",), c["take"], W_MIN, c["fm"], c["cities"], c["inside"])
        s = summarize(f); s["rule"] = c["rule"]
        nd = len({d["date"] for d in days if d["seg"] == "val"})
        s["trades_per_day"] = round(s.get("n", 0) / nd, 2) if nd else None
        out.append(s)
        print("VALIDATION", c["rule"], {k: v for k, v in s.items() if k not in ("by_side", "by_city", "by_kind")}, flush=True)
        print("   by side", {k: (v.get("n"), v.get("ret")) for k, v in s.get("by_side", {}).items()},
              "by city", {k: (v.get("n"), v.get("ret")) for k, v in s.get("by_city", {}).items()}, flush=True)
        (OUT / f"fills_val_{CANDIDATES.index(c)}.json").write_text(json.dumps(f, default=str))
        fd = simulate(days, c["hk"], c["m"], "normal", ("disc",), c["take"], W_MIN, c["fm"], c["cities"], c["inside"])
        (OUT / f"fills_disc_{CANDIDATES.index(c)}.json").write_text(json.dumps(fd, default=str))
    (OUT / "validation.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def trades_check(n_max: int = 40):
    """Step 3: verify candle-based maker fills against Kalshi trade prints (/markets/trades), and measure how many
    contracts traded strictly through the order price in the fill minute (capacity). Cached under OUT/trades/."""
    from lab.us.data_refresh import fetch, K
    td = OUT / "trades"; td.mkdir(exist_ok=True)
    fills = []
    for i in range(len(CANDIDATES)):
        for seg in ("disc", "val"):
            f = OUT / f"fills_{seg}_{i}.json"
            if f.exists():
                fills += [dict(r, cand=i) for r in json.loads(f.read_text()) if r["kind"] == "make"]
    seen = set(); pick = []
    for r in sorted(fills, key=lambda r: (r["cand"], r["t"])):
        if r["t"] in seen:
            continue
        seen.add(r["t"]); pick.append(r)
    pick = pick[:n_max]
    calls = 0; rows = []
    for r in pick:
        cf = td / f"{r['t']}_{r['tfill']}.json"
        if cf.exists():
            tr = json.loads(cf.read_text())
        else:
            url = f"{K}/markets/trades?ticker={r['t']}&min_ts={r['tfill'] - 180}&max_ts={r['tfill'] + 60}&limit=1000"
            txt = fetch(url, pace=1.15); calls += 1
            try:
                tr = json.loads(txt).get("trades") or []
            except Exception:
                tr = None
            cf.write_text(json.dumps(tr))
        if tr is None:
            continue
        lo_m, hi_m = r["tfill"] - 60, r["tfill"]
        def yp(x):
            v = x.get("yes_price_dollars") or x.get("yes_price")
            v = float(v)
            return v / 100 if v > 1 else v
        def cnt(x):
            return float(x.get("count_fp") or x.get("count") or 0)
        def tsx(x):
            return ts(x["created_time"])
        inmin = [x for x in tr if lo_m < tsx(x) <= hi_m]
        if r["side"] == "YES":
            thr = [x for x in inmin if yp(x) < r["px"] - 1e-9]
        else:
            thr = [x for x in inmin if yp(x) > (1 - r["px"]) + 1e-9]
        rows.append({"t": r["t"], "side": r["side"], "px": r["px"], "tfill": r["tfill"], "won": r["won"], "trades_in_minute": len(inmin),
                     "trades_through": len(thr), "contracts_through": sum(cnt(x) for x in thr),
                     "taker_sides_through": dict(Counter(x.get("taker_side") for x in thr)), "candle_vol": r.get("vol")})
    conf = [x for x in rows if x["trades_through"] > 0]
    ct = sorted(x["contracts_through"] for x in conf)
    res = {"kalshi_calls": calls, "checked": len(rows), "confirmed_trade_through": len(conf),
           "median_contracts_through": st.median(ct) if ct else None,
           "share_ge5_contracts": round(sum(c >= 5 for c in ct) / len(ct), 3) if ct else None,
           "taker_side_through": dict(sum((Counter(x["taker_sides_through"]) for x in conf), Counter())), "rows": rows}
    (OUT / "trades_check.json").write_text(json.dumps(res, indent=1))
    print({k: v for k, v in res.items() if k != "rows"})
    return res


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    DPMF = d_pmfs()
    days, cut = load()
    print(f"city-dates {len(days)} (disc {sum(d['seg'] == 'disc' for d in days)}, val {sum(d['seg'] == 'val' for d in days)}), cut {cut}", flush=True)
    if what in ("brier", "all"):
        brier(days)
    if what in ("sim", "all"):
        sim(days)
    if what in ("ext", "all"):
        ext(days)
    if what == "validate":
        validate(days)
    if what == "trades":
        trades_check()
    if VARIANTS:
        (OUT / f"variants_{what}.json").write_text(json.dumps(VARIANTS))
