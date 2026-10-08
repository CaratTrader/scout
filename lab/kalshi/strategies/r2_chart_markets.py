"""r2_chart_markets: is there a taker edge in Kalshi's weekly entertainment-chart markets?

Products (all 'quadratic' fee, weekly):
  Netflix Top 10 #1 - KXNETFLIXRANKSHOW / KXNETFLIXRANKMOVIE (US), KXNETFLIXRANKSHOWGLOBAL / KXNETFLIXRANKMOVIEGLOBAL.
     Week Mon-Sun, chart published Tuesday, market closes Monday 23:59 ET.
  Billboard Hot 100 #1 - KXTOPSONG. Tracking week Fri-Thu, top 10 revealed Monday, market closes Sunday 23:59 ET.
  Billboard 200 #1 - KXTOPALBUM. Same tracking week; Billboard reveals the album top 10 on Sunday ~15:00 ET,
     i.e. BEFORE the Sunday 23:59 ET close (the 1-minute data shows the reprice within 2-3 minutes).
  (KXSPOTIFYD / 2D / GLOBALD / ARTISTD / ALBUMD daily Spotify series and KXNETFLIXRANKING / *RANKD daily Netflix series
  had no settled market in the last 70 days, so no daily chart product is live.)

Leading free data: kworb.net Spotify US weekly chart (Fri-Thu, the Hot 100 tracking week) from Wayback snapshots
(r2_chart_markets_kworb.py). FlixPatrol (the only free daily Netflix rank source) sits behind a Cloudflare challenge,
so it can be used neither for the backtest nor live; no Netflix model is possible within the rules.

Tests (time split over the family's events ordered by close: discovery = first 70%, validation = last 30%):
  A. Favourite rules: at T-h (h = 72/48/24 h before close; all before the B200 / Netflix information releases),
     buy YES on the market favourite (highest mid on the last hourly candle at or before T-h) if the fill ask
     (the hourly candle ending one minute after T-h, quote age <= 30 min) lies in a band. Taker fee 0.07 p (1-p) per
     contract, rounded up to the cent on a 10-contract order.
  B. Hot 100 model: P(#1) from the Spotify US weekly leader's margin and incumbency, vs the market mid (Brier and log
     loss on the candidate markets, discovery only). Kill if it does not beat the mid by >= 0.02 Brier.
  C. Information-release diagnostics (B200 Sunday reveal, Netflix Monday early-afternoon reprice): 1-minute paths.
Usage: python -m lab.kalshi.strategies.r2_chart_markets"""
from __future__ import annotations
import collections, datetime as dt, json, math, re, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_chart_markets_api import OUT, used

ET = dt.timezone(dt.timedelta(hours=-4))
SPLIT = 0.7
MAX_AGE = 30 * 60
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
NETFLIX = ("KXNETFLIXRANKSHOW", "KXNETFLIXRANKMOVIE", "KXNETFLIXRANKSHOWGLOBAL", "KXNETFLIXRANKMOVIEGLOBAL")
BANDS = ((0.5, 0.9), (0.7, 0.9), (0.8, 0.9), (0.9, 0.97), (0.97, 0.995), (0.5, 0.97))
SCOPES = {"all": None, "netflix_us": ("KXNETFLIXRANKSHOW", "KXNETFLIXRANKMOVIE"), "hot100": ("KXTOPSONG",)}
TAUS = (72, 48, 24)


def fee10(p: float) -> float:
    """Per-contract taker fee on a 10-contract order, Kalshi rounding the order fee up to the cent."""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def load():
    ms = {}
    for f in ("markets.jsonl", "markets_hist.jsonl"):
        for l in (OUT / f).open():
            m = json.loads(l)
            ms.setdefault(m["t"], m)
    cs = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l); cs[x["t"]] = x["c"]
    return ms, cs


def asof(c: list, ts: int, age: int = MAX_AGE):
    b = None
    for r in c:
        if r[0] <= ts:
            b = r
        else:
            break
    if not b or ts - b[0] > age or b[1] is None or b[2] is None:
        return None
    return b


def chart_date(e: str):
    x = re.search(r"-(\d\d)([A-Z]{3})(\d\d)$", e)
    return dt.date(2000 + int(x.group(1)), MON[x.group(2)], int(x.group(3))) if x else None


def cluster(m: dict) -> str:
    dom = "netflix" if m["series"] in NETFLIX else "billboard"
    return f"{dom}:{dt.datetime.fromtimestamp(m['close'] - 30 * 3600, dt.timezone.utc).date()}"


def events(ms, cs):
    ev = collections.defaultdict(list)
    for m in ms.values():
        if m["series"] in NETFLIX + ("KXTOPSONG", "KXTOPALBUM") and m["result"] in ("yes", "no"):
            ev[m["e"]].append(m)
    out = {}
    for e, L in ev.items():
        fetched = [m for m in L if m["t"] in cs]
        if fetched:
            out[e] = {"e": e, "series": L[0]["series"], "close": L[0]["close"], "markets": L, "fetched": fetched,
                      "complete": len(fetched) == len(L), "cl": cluster(L[0])}
    return out


def fav_trade(E: dict, cs: dict, h: int, lo: float, hi: float):
    """Signal from candles at or before T-h; fill one minute later at the ask (limit hi)."""
    t = E["close"] - h * 3600
    best = None
    for m in E["fetched"]:
        r = asof(cs[m["t"]], t, 3600)          # signal: the latest hourly candle at or before t (<= 60 min old)
        if r and 0 < r[1] <= 1 and r[2] >= 0:
            mid = (r[1] + r[2]) / 2
            if best is None or mid > best[0]:
                best = (mid, m, r)
    if not best:
        return None
    mid, m, r = best
    if not E["complete"] and mid < 0.5:
        return None                            # archive event with an unfetched market that may be the favourite
    if not (lo <= r[1] < hi):
        return None
    f = asof(cs[m["t"]], t + 60)               # fill: quote at t + 1 min, at most 30 min old
    if not f or not (0 < f[1] < hi + 1e-9) or f[1] >= 1:
        return None
    px = f[1]; won = m["result"] == "yes"
    return {"e": E["e"], "cl": E["cl"], "series": E["series"], "close": E["close"], "t": m["t"], "px": px, "won": won,
            "ret": ((1.0 if won else 0.0) - px - fee10(px)) / px}


def stats(rows: list[dict], key: str = "cl") -> dict:
    if not rows:
        return {"n": 0}
    g = collections.defaultdict(list)
    for r in rows:
        g[r[key]].append(r["ret"])
    em = [st.mean(v) for v in g.values()]
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))) if len(em) > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    rows = sorted(rows, key=lambda r: r["close"]); h = len(rows) // 2
    return {"n": len(rows), "events": len(em), "win": round(st.mean(r["won"] for r in rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in rows[:h]), 4) if h else None, "half2": round(st.mean(r["ret"] for r in rows[h:]), 4)}


def hot100_model(ms, cs, disc_cut: int) -> dict:
    """Spotify-leader / incumbent model vs the market mid on the candidate markets (Hot 100, discovery events only)."""
    kw = json.loads((OUT / "kworb_us_weekly.json").read_text())
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower().split("(")[0].split(" feat")[0].split(" w/")[0])
    ev = collections.defaultdict(list)
    for m in ms.values():
        if m["series"] == "KXTOPSONG" and chart_date(m["e"]):
            ev[m["e"]].append(m)
    prev = None; recs = []
    for e in sorted(ev, key=chart_date):
        L = ev[e]; d = chart_date(e); k = kw.get((d - dt.timedelta(days=9)).isoformat())
        tk = {norm(m["sub"]): m for m in L}
        win = [m for m in L if m["result"] == "yes"]
        sp = tk.get(norm(k["rows"][0][2])) if k else None
        mg = k["rows"][0][3] / k["rows"][1][3] - 1 if k else None
        inc = tk.get(prev) if prev else None
        recs.append({"e": e, "close": L[0]["close"], "sp": sp, "mg": mg, "inc": inc,
                     "sp_won": bool(sp and sp["result"] == "yes"), "inc_won": bool(inc and inc["result"] == "yes")})
        prev = norm(win[0]["sub"]) if win else None
    # model probabilities fitted on discovery-period base rates (all KXTOPSONG weeks with kworb data before the cut)
    base = [r for r in recs if r["close"] < disc_cut and r["mg"] is not None]
    def p_sp(r, pool):
        same = r["sp"] is not None and r["inc"] is not None and r["sp"]["t"] == r["inc"]["t"]
        sub = [x for x in pool if (x["sp"] is not None and x["inc"] is not None and x["sp"]["t"] == x["inc"]["t"]) == same and (x["mg"] >= 0.15) == (r["mg"] >= 0.15)]
        return (sum(x["sp_won"] for x in sub) + 1) / (len(sub) + 2)
    def p_inc(r, pool):
        sub = [x for x in pool if x["inc"] is not None and (x["sp"] is None or x["sp"]["t"] != x["inc"]["t"])]
        return (sum(x["inc_won"] for x in sub) + 1) / (len(sub) + 2)
    out = {}
    for h in (48, 24):
        bm = []; bk = []; lm = []; lk = []
        for r in recs:
            if r["close"] >= disc_cut or r["mg"] is None:
                continue
            for m, p in ((r["sp"], p_sp(r, base)), (r["inc"], p_inc(r, base))):
                if m is None or m["t"] not in cs or (r["sp"] and r["inc"] and m is r["inc"] and r["sp"]["t"] == r["inc"]["t"]):
                    continue
                q = asof(cs[m["t"]], r["close"] - h * 3600, 6 * 3600)
                if not q or q[1] >= 1 and q[2] <= 0:
                    continue
                mid = min(max((q[1] + q[2]) / 2, 0.005), 0.995); y = 1.0 if m["result"] == "yes" else 0.0
                bm.append((p - y) ** 2); bk.append((mid - y) ** 2)
                lm.append(-math.log(p if y else 1 - p)); lk.append(-math.log(mid if y else 1 - mid))
        out[f"T-{h}h"] = {"n_market_weeks": len(bm), "brier_model": round(st.mean(bm), 4) if bm else None, "brier_mid": round(st.mean(bk), 4) if bk else None,
                          "logloss_model": round(st.mean(lm), 4) if lm else None, "logloss_mid": round(st.mean(lk), 4) if lk else None}
    agree = [r for r in recs if r["mg"] is not None]
    out["spotify1_is_hot100_1"] = f"{sum(r['sp_won'] for r in agree)}/{len(agree)} weeks with kworb data (2024-12..2026-10)"
    out["incumbent_holds"] = f"{sum(r['inc_won'] for r in recs if r['inc'])}/{sum(1 for r in recs if r['inc'])} weeks"
    return out


def snipe_diag() -> dict:
    out = {}
    for name, f, tk in (("B200 26AUG08 Sunday reveal", "min_b200_26AUG08.json", "KXTOPALBUM-26AUG08-IMT"),
                        ("Netflix US movie 26SEP21 Monday reprice", "min_netflix_movie_26SEP21.json", "KXNETFLIXRANKMOVIE-26SEP21-WHY")):
        p = OUT / f
        if not p.exists():
            continue
        cs = json.loads(p.read_text()).get(tk) or []
        path = [(dt.datetime.fromtimestamp(c["end_period_ts"], ET).strftime("%a %H:%M ET"), float((c.get("yes_bid") or {}).get("close_dollars") or 0),
                 float((c.get("yes_ask") or {}).get("close_dollars") or 0)) for c in cs if float(c.get("volume_fp") or 0) > 0]
        jump = next((i for i in range(1, len(path)) if path[i][2] - path[i - 1][2] >= 0.15), None)
        out[name] = path[max(0, (jump or 0) - 2):(jump or 0) + 5] if jump else path[:5]
    return out


def main() -> dict:
    ms, cs = load()
    E = events(ms, cs)
    closes = sorted({x["close"] for x in E.values()})
    cut = closes[int(len(closes) * SPLIT)]
    disc = {e: x for e, x in E.items() if x["close"] < cut}; val = {e: x for e, x in E.items() if x["close"] >= cut}
    variants = []
    for h in TAUS:
        for lo, hi in BANDS:
            for sc, ser in SCOPES.items():
                name = f"FAV T-{h}h ask[{lo},{hi}) {sc}"
                rows = [r for x in disc.values() if (ser is None or x["series"] in ser) for r in [fav_trade(x, cs, h, lo, hi)] if r]
                variants.append((name, (h, lo, hi, sc), stats(rows)))
    ranked = sorted([v for v in variants if v[2]["n"] >= 8], key=lambda v: -(v[2]["t"] or -9))
    frozen = ranked[:3]
    validation = []
    for name, (h, lo, hi, sc), d in frozen:
        ser = SCOPES[sc]
        rows = [r for x in val.values() if (ser is None or x["series"] in ser) for r in [fav_trade(x, cs, h, lo, hi)] if r]
        s = stats(rows)
        days = max(1, (max(x["close"] for x in val.values()) - min(x["close"] for x in val.values())) / 86400)
        s.update({"rule": name, "trades_per_day": round(s["n"] / days, 3), "discovery": d})
        validation.append(s)
    model = hot100_model(ms, cs, cut)
    res = {"n_events": len(E), "n_disc": len(disc), "n_val": len(val), "cut_utc": dt.datetime.fromtimestamp(cut, dt.timezone.utc).isoformat(),
           "events_by_series": dict(collections.Counter(x["series"] for x in E.values())),
           "discovery_top": [{"rule": v[0], **v[2]} for v in ranked[:8]], "validation": validation, "hot100_model_discovery": model,
           "snipe_paths": snipe_diag(), "variants_fav": len(variants), "kalshi_calls_used": used()}
    (OUT / "analysis.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    print(json.dumps(main(), indent=1, default=str))
