"""quake_entertain: observation-latency and base-rate edges on KXBIGGESTQUAKE (daily max USGS magnitude worldwide),
plus a Rotten Tomatoes (KXRT) late-calibration check (see quake_entertain_rt.py).

Data (built by quake_entertain_getq.py / quake_entertain_usgs.py, cached under data/kalshi_lab/strategies/quake_entertain/):
  quake_markets.jsonl / quake_candles.jsonl  Kalshi markets + 1-minute candles over each event's whole life
  usgs_versions.json  every origin version (updateTime, source, status, magnitude) of each M>=4.7 quake in the period
  usgs_hist.csv       M>=5.0 catalog 2010-01-01..2026-08-31 (before the first Kalshi event) for base rates

Rules (taker, decision at t with data <= t, fill at the quote at t+60 s, 0.07 p(1-p) fee rounded up per order):
  SNIPE  buy YES on strike k as soon as the USGS-displayed day max first reaches k (+margin), after a poll lag.
  MODEL  conditional base-rate model P(day max >= k | posted max so far < k, hour) from 2010-2026 history; buy the side
         whose model edge (prob - price - fee) >= threshold at fixed decision times.
Split: events ordered by close; discovery = first 70%, validation = last 30%.
Usage: python lab/kalshi/strategies/quake_entertain.py"""
from __future__ import annotations
import bisect, csv, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import cell_stats

OUT = Path("data/kalshi_lab/strategies/quake_entertain")
STRIKES = (5.2, 5.4, 5.6, 5.8, 6.0, 6.2, 6.4, 6.6, 6.8, 7.0)
MAX_AGE = 30 * 60
VARIANTS = []   # every rule variant examined (name, split) -> counted


def fee_order(p: float, n: int = 10) -> float:
    """Per-contract fee with Kalshi's round-up to the cent per order (10-contract order)."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def quote(c: list[list], t: int):
    i = bisect.bisect_right(c, [t, 9e9]) - 1
    if i < 0:
        return None
    r = c[i]
    if t - r[0] > MAX_AGE or r[1] is None or r[2] is None:
        return None
    return r[1], r[2], r


def load():
    ms = [json.loads(l) for l in (OUT / "quake_markets.jsonl").open()]
    cs = {}
    for l in (OUT / "quake_candles.jsonl").open():
        x = json.loads(l); cs[x["t"]] = sorted(x["c"])
    q = json.loads((OUT / "usgs_versions.json").read_text())
    return ms, cs, q


def day_of(e: str) -> str:
    import datetime as dt
    return dt.datetime.strptime(e.split("-")[1], "%d%b%y").strftime("%Y-%m-%d")


def quakes_by_day(q: dict, tsunami: bool = False) -> dict[str, list]:
    """tsunami=False: drop tsunami-centre products (at, pt), which are preliminary and carry unreliable updateTimes
    (used for SNIPE triggers). tsunami=True keeps them, with an updateTime before the origin moved to origin + 10 min
    (used to decide that a strike may already have been reached, so MODEL does not trade it)."""
    import datetime as dt
    out = defaultdict(list)
    for eid, x in q.items():
        d = dt.datetime.utcfromtimestamp(x["time"] / 1000).strftime("%Y-%m-%d")
        o = x["time"] / 1000
        vs = [(max(v[0] / 1000, o + 600) if v[1] in ("at", "pt") else v[0] / 1000, float(v[3]), v[1], v[2]) for v in x["versions"]
              if v[3] not in (None, "") and (tsunami or v[1] not in ("at", "pt"))]
        if vs:
            out[d].append({"id": eid, "origin": x["time"] / 1000, "final": x["mag"], "vs": sorted(vs), "place": x["place"]})
    return out


def disp(qk: dict, t: float):
    """USGS-displayed magnitude of one quake at time t: latest origin version posted at or before t."""
    m = None
    for ut, mag, src, status in qk["vs"]:
        if ut <= t:
            m = mag
        else:
            break
    return m


def posted_max(qs: list, t: float) -> float:
    m = 0.0
    for qk in qs:
        x = disp(qk, t)
        if x is not None and x > m:
            m = x
    return m


def crossing_times(qs: list) -> dict[float, tuple]:
    """For each strike: first time the displayed day max reaches it, and the quake that did it."""
    pts = sorted({v[0] for qk in qs for v in qk["vs"]})
    out = {}
    for t in pts:
        m = posted_max(qs, t)
        for k in STRIKES:
            if k not in out and m >= k - 1e-9:
                who = max(qs, key=lambda qk: disp(qk, t) or 0)
                out[k] = (t, m, who)
    return out


# ---------------------------------------------------------------- base-rate model from history (pre-period only)
def hist_table():
    """P(max over [h, 24h) >= k | max over [0, h) < k) for h = 0..23 (origin-time hours), empirical 2010..2026-08."""
    import datetime as dt
    rows = list(csv.DictReader((OUT / "usgs_hist.csv").open()))
    by_day = defaultdict(list)
    for r in rows:
        t = dt.datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        by_day[t.strftime("%Y-%m-%d")].append((t.hour * 3600 + t.minute * 60 + t.second, float(r["mag"])))
    d0, d1 = dt.date(2010, 1, 1), dt.date(2026, 8, 31)
    days = [(d0 + dt.timedelta(i)).isoformat() for i in range((d1 - d0).days)]
    tab = {}
    for k in STRIKES:
        first = []
        for d in days:
            ts = [t for t, m in by_day.get(d, []) if m >= k - 1e-9]
            first.append(min(ts) if ts else None)
        for h4 in range(0, 96):      # 15-minute grid
            s = h4 * 900
            alive = [f for f in first if f is None or f >= s]
            y = sum(1 for f in alive if f is not None)
            tab[(k, h4)] = (y + 0.5) / (len(alive) + 1)
    return tab


def model_p(tab, k: float, sec_of_day: float, lag: float = 1200) -> float:
    """P(YES) for a strike not yet reached at decision time: quakes with origin >= t - lag (unposted) still count."""
    s = max(0, sec_of_day - lag)
    return tab[(k, min(95, int(s // 900)))]


# ---------------------------------------------------------------- trades
def snipe_trades(ms, cs, qd, lag=60, margin=0.0, max_px=0.99):
    out = []; diag = []
    ev = defaultdict(list)
    for m in ms:
        ev[m["e"]].append(m)
    for e, xs in ev.items():
        d = day_of(e); qs = qd.get(d, [])
        cr = crossing_times(qs)
        for m in xs:
            k = m["floor"]
            if k is None or m["result"] not in ("yes", "no"):
                continue
            hit = None
            for kk, (t, mag, who) in sorted(cr.items(), key=lambda kv: kv[1][0]):
                if mag >= k + margin - 1e-9:
                    hit = (t, mag, who); break
            if not hit:
                continue
            t, mag, who = hit
            tdec = t + lag; tfill = tdec + 60
            c = cs.get(m["t"], [])
            qn = quote(c, tfill)
            row = {"e": e, "t": m["t"], "k": k, "t_post": t, "mag0": mag, "final": who["final"], "close": m["close"],
                   "close_minus_post": (m["close"] - t) / 60, "origin_to_post": (t - who["origin"]) / 60, "result": m["result"]}
            if m["close"] <= tfill or not qn:
                row["traded"] = False; diag.append(row); continue
            ask, bid, r = qn
            row.update({"traded": True, "ask": ask, "bid": bid, "ask_age": tfill - r[0]})
            diag.append(row)
            if not (0.01 <= ask <= max_px):
                continue
            won = m["result"] == "yes"
            pnl = (1.0 if won else 0.0) - ask - fee_order(ask)
            out.append({"e": e, "t_close": m["close"], "px": ask, "won": won, "ret": pnl / ask, "k": k, "side": "YES"})
    return out, diag


def jumped(c: list[list], t: int, look: int = 6 * 3600, jump: float = 0.25) -> bool:
    """True if the YES mid rose by >= jump within 5 minutes during [t - look, t]: the market reacted to a USGS posting
    (possibly one later deleted from ComCat and so invisible in this backtest). A live bot would have seen that posting."""
    pts = [(r[0], (r[1] + r[2]) / 2) for r in c if t - look <= r[0] <= t and r[1] is not None and r[2] is not None]
    for i, (ti, mi) in enumerate(pts):
        for tj, mj in pts[i + 1:]:
            if tj - ti > 300:
                break
            if mj - mi >= jump:
                return True
    return False


def model_trades(ms, cs, qd, tab, hours=(12, 16, 18, 20, 21, 22, 23), thr=0.10, sides=("YES", "NO"), lag=1200, kmin=0.0,
                 guard=True, min_px=0.0, once=False):
    """once=True: at most one entry per market (the first decision time whose edge clears thr)."""
    import datetime as dt
    out = []
    for m in ms:
        k = m["floor"]
        if k is None or m["result"] not in ("yes", "no") or k < kmin:
            continue
        d = day_of(m["e"]); qs = qd.get(d, [])
        base = int(dt.datetime.fromisoformat(d + "T00:00:00+00:00").timestamp())
        c = cs.get(m["t"], [])
        for h in hours:
            t = base + h * 3600
            if t + 60 >= m["close"]:
                continue
            if posted_max(qs, t) >= k - 1e-9:
                continue                      # already reached: covered by SNIPE
            if guard and jumped(c, t):
                continue
            p = model_p(tab, k, h * 3600, lag)
            qn = quote(c, t + 60)
            if not qn:
                continue
            ask, bid, _ = qn
            won_yes = m["result"] == "yes"
            for side, px, pw, won in (("YES", ask, p, won_yes), ("NO", 1 - bid, 1 - p, not won_yes)):
                if side not in sides or not (max(0.01, min_px) <= px <= 0.99):
                    continue
                if pw - px - fee_order(px) < thr:
                    continue
                pnl = (1.0 if won else 0.0) - px - fee_order(px)
                out.append({"e": m["e"], "t": m["t"], "t_close": base + 86400, "px": px, "won": won, "ret": pnl / px, "k": k, "side": side, "h": h, "p": pw})
    if once:
        seen = set(); keep = []
        for r in sorted(out, key=lambda r: (r["t"], r["h"])):
            if (r["t"], r["side"]) not in seen:
                seen.add((r["t"], r["side"])); keep.append(r)
        out = keep
    return out


def split(rows, events_sorted, frac=0.7):
    cut = events_sorted[int(len(events_sorted) * frac)]
    return [r for r in rows if r["t_close"] < cut], [r for r in rows if r["t_close"] >= cut]


def summarize(rows):
    if not rows:
        return {"n": 0}
    s = cell_stats(rows)
    rs = sorted(rows, key=lambda r: r["t_close"]); mid = rs[len(rs) // 2]["t_close"]
    h1 = [r["ret"] for r in rs if r["t_close"] < mid]; h2 = [r["ret"] for r in rs if r["t_close"] >= mid]
    s["half1"] = st.mean(h1) if h1 else float("nan"); s["half2"] = st.mean(h2) if h2 else float("nan")
    return s


def fmt(name, s):
    if not s.get("n"):
        return f"  {name:40s} n=0"
    return (f"  {name:40s} n={s['n']:4d} ev={s['events']:3d} win={s['win']:.0%} px={s['px']:.3f} ret={s['ret']:+.1%} t={s['t']:5.2f} "
            f"wo3={s['ret_wo3']:+.1%} h1={s.get('half1', float('nan')):+.1%} h2={s.get('half2', float('nan')):+.1%}")


def snipe_window_trades(ms, cs, qd, lag=120, margin=0.0, max_px=0.90):
    """Live-bot version of SNIPE: from posting+lag until the market closes, buy YES at the first minute whose ask
    <= max_px (decision on the candle at t, fill at the quote at t+60 s, which must also be <= max_px)."""
    out = []
    ev = defaultdict(list)
    for m in ms:
        ev[m["e"]].append(m)
    for e, xs in ev.items():
        qs = qd.get(day_of(e), []); cr = crossing_times(qs)
        for m in xs:
            k = m["floor"]
            if k is None or m["result"] not in ("yes", "no"):
                continue
            hit = next(((t, mag) for kk, (t, mag, who) in sorted(cr.items(), key=lambda kv: kv[1][0]) if mag >= k + margin - 1e-9), None)
            if not hit:
                continue
            t0 = hit[0] + lag; c = cs.get(m["t"], [])
            for r in c:
                if r[0] < t0 or r[0] + 60 >= m["close"]:
                    continue
                if r[1] is None or r[1] > max_px:
                    continue
                qn = quote(c, r[0] + 60)
                if not qn or not (0.01 <= qn[0] <= max_px):
                    continue
                ask = qn[0]; won = m["result"] == "yes"
                pnl = (1.0 if won else 0.0) - ask - fee_order(ask)
                out.append({"e": e, "t_close": m["close"], "px": ask, "won": won, "ret": pnl / ask, "k": k, "side": "YES",
                            "wait_min": (r[0] - hit[0]) / 60, "mag0": hit[1]})
                break
    return out


H7 = (12, 16, 18, 20, 21, 22, 23)
H30 = tuple(x / 2 for x in range(24, 48))
FROZEN = {
    "C1 SNIPE window lag120 px<=0.90 (YES)": ("snipe", dict(lag=120, margin=0.0, max_px=0.90)),
    "C2 MODEL NO once H30 thr0.15 px>=0.50": ("model", dict(hours=H30, thr=0.15, sides=("NO",), guard=True, min_px=0.5, once=True)),
    "C3 MODEL NO multi H7 thr0.10 px>=0.50": ("model", dict(hours=H7, thr=0.10, sides=("NO",), guard=True, min_px=0.5, once=False)),
}


def run_rule(kind, kw, ms, cs, qd, qd_all, tab):
    return snipe_window_trades(ms, cs, qd, **kw) if kind == "snipe" else model_trades(ms, cs, qd_all, tab, **kw)


def discovery_grid(ms, cs, qd, qd_all, tab, cut, tc):
    """Every variant examined on discovery (counted in variants_examined)."""
    res = {}
    for lag in (60, 120):
        for margin in (0.0, 0.1):
            for mp in (0.90, 0.95, 0.99):
                res[f"snipe_first lag{lag} m{margin} px<={mp}"] = tc(snipe_trades(ms, cs, qd, lag=lag, margin=margin, max_px=mp)[0])
            for mp in (0.85, 0.90, 0.95):
                res[f"snipe_window lag{lag} m{margin} px<={mp}"] = tc(snipe_window_trades(ms, cs, qd, lag=lag, margin=margin, max_px=mp))
    for thr in (0.03, 0.05, 0.10, 0.15):
        for side in ("YES", "NO"):
            res[f"model {side} thr{thr} noguard (non-tsunami catalog)"] = tc(model_trades(ms, cs, qd, tab, thr=thr, sides=(side,), guard=False))
    for guard in (False, True):
        for min_px in (0.0, 0.5):
            for thr in (0.05, 0.10, 0.15):
                res[f"model NO thr{thr} guard{guard} minpx{min_px}"] = tc(model_trades(ms, cs, qd_all, tab, thr=thr, sides=("NO",), guard=guard, min_px=min_px))
    for hours, hn in ((H7, "H7"), (H30, "H30")):
        for min_px in (0.0, 0.5):
            for thr in (0.10, 0.15):
                res[f"model NO once {hn} thr{thr} minpx{min_px}"] = tc(model_trades(ms, cs, qd_all, tab, hours=hours, thr=thr, sides=("NO",), guard=True, min_px=min_px, once=True))
    return {k: summarize([r for r in v if r["t_close"] < cut]) for k, v in res.items()}


def main():
    ms, cs, q = load()
    qd = quakes_by_day(q); qd_all = quakes_by_day(q, tsunami=True); tab = hist_table()
    ev_end = {}
    for m in ms:
        if m["result"] in ("yes", "no"):
            ev_end[m["e"]] = max(ev_end.get(m["e"], 0), m["exp"])   # scheduled end (23:59:59 UTC), never the early close
    evs = sorted(ev_end.values()); cut = evs[int(len(evs) * 0.7)]

    def tc(rows):
        for r in rows:
            r["t_close"] = ev_end[r["e"]]
        return rows
    print(f"KXBIGGESTQUAKE: events with results {len(evs)}; discovery {sum(1 for e in evs if e < cut)}, validation {sum(1 for e in evs if e >= cut)}")
    disc = discovery_grid(ms, cs, qd, qd_all, tab, cut, tc)
    print("\nDISCOVERY GRID")
    for k, v in disc.items():
        print(fmt(k, v))
    print("\nVALIDATION (frozen candidates, run once)")
    val = {}
    for name, (kind, kw) in FROZEN.items():
        rows = tc(run_rule(kind, kw, ms, cs, qd, qd_all, tab))
        d = [r for r in rows if r["t_close"] < cut]; v = [r for r in rows if r["t_close"] >= cut]
        val[name] = {"disc": summarize(d), "val": summarize(v), "val_trades": v}
        print(fmt("disc " + name, val[name]["disc"])); print(fmt("VAL  " + name, val[name]["val"]))
        for r in v:
            print(f"      {r['e'][-7:]} k={r['k']} side={r['side']} px={r['px']:.2f} won={r['won']} ret={r['ret']:+.1%}" + (f" h={r['h']}" if 'h' in r else ""))
    # all-period (descriptive only)
    allp = {name: summarize(tc(run_rule(kind, kw, ms, cs, qd, qd_all, tab))) for name, (kind, kw) in FROZEN.items()}
    return {"cut": cut, "n_events": len(evs), "discovery": disc, "validation": val, "all_period": allp}


if __name__ == "__main__":
    import datetime as dt
    R = main()
    (OUT / "quake_results.json").write_text(json.dumps(R, default=str, indent=1))
