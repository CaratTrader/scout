"""r3_seasonal_regime_monitors: are the two regime-dependent dead edges coming back this winter?

  (1) KXRAIN maker NO inside wide spreads (microstructure rule E|rain|S0.06|H30|NO, frozen 2026-10-08): it earned
      +24..+47%/$ on Jul 31 - Sep 2026 days when market makers were absent and spreads >= 6c were common; spreads have
      since tightened to 1-2c and the rule no longer fires.
  (2) Monthly-rain YES overpricing (r2_slow_accumulators C2: taker NO when 1 - yes_bid in [0.35, 0.65) at 10:00 local,
      fill at the 11:00 quote, once per market): +19% on the dry Nov 2025 - May 2026 discovery stretch, +10% (t 0.7,
      +0.3% without the best 3) on Jun-Sep 2026.

No search and no K: the rules and the triggers were written in the round-2 lead (lead_round2/result.json, R3-8) before
this code ran. This module only (a) backcasts the regime indicators on the history on disk, to check that the
trigger separates the regimes and that the regime, not the per-market spread, carries the edge, and (b) summarises the
forward log written by r3_seasonal_regime_monitors_logger.py.

Indicator W (frozen here, used identically by the logger):
  snapshot at a top-of-hour H; universe = KXRAIN markets with 60 s < close - H <= 18 h (rule E's decision window);
  eligible = two-sided quote (0 < yes_bid < yes_ask < 1) with 0.06 <= yes_ask <= 0.96 (rule E's posting range: its NO
  price 1 - (ask - 0.01) must lie in [0.05, 0.95]); wide = eligible and yes_ask - yes_bid >= 0.06.
  W(day) = sum of wide / sum of eligible over all snapshots of the KXRAIN event date (pooled ratio).
  Also logged: W_all = share of ANY quoted market (no price filter) with spread >= 6c (the lead_round1 definition).
Trigger 1 (frozen): W > 0.10 on 5 consecutive event dates, each with >= 12 snapshots that had >= 1 eligible market
  (a date with fewer snapshots breaks the streak). When it fires, rule E runs forward (logger 'armed' mode).
Trigger 2 (frozen): the first 30 settled forward C2 entries average >= +10% per $ after fees -> pre-register C2 for a
  second, independent forward sample (no trading on the first sample).

Backcast data: KXRAIN 1-minute candles over the last 18 h before close, whole event dates only:
  2026-07-31..08-07 microstructure/arch_candles.jsonl, 08-08..08-22 rain_history/candles.jsonl (+ 10-07),
  08-23..10-06 data/kalshi_lab/candles/KXRAIN.jsonl. Candles are emitted only when the book or volume changes (97.6% of
  > 30 min gaps resume at the identical quote), so a snapshot carries the last quote forward without an age limit.
Monthly: r2_slow_accumulators markets/candles (hourly; sampled strikes Feb 2024 - Jul 2026, full ladders Aug/Sep 2026).

Result (2026-10-08, backcast_rain.json, fillcheck.json, backcast_monthly.json):
  * W fell from 27% (Aug) to 4% (Sep 1-15), 2.6% (Sep 16-30) and 0.5% (Oct 1-7); last date above 10%: 2026-08-31.
    T1 is far from firing. Gated by T1 ex ante, rule E trades on 17 discovery dates (+22.6%, t 1.64) and 0 validation dates.
  * Rule E did NOT stop when W collapsed: it fires on minute-level transient spreads (56 -> 29 -> 17 fills/day) and its
    mean stayed at +24-27%/$ in every period. But the breadth faded: without the 3 most profitable markets it makes
    +15.4% (t 2.3) in Aug, +14.9% (t 2.0) Sep 1-16, and +4.3% (t 0.3) Sep 17 - Oct 7, where one new-city launch day
    (KXRAIN-26SEP30-DTW) moves the mean from +13.6% to +26.5%. maker_fill() with print-bearing candles on the same
    dates: +30.8% (n 427, t 1.97), +8.8% without the top 3 markets. Daily W does not rank daily E returns (Spearman 0.04).
  * C2 is not a clean seasonal effect: +27% Nov 2025-May 2026 (n 46), -36% in the thin 2024-25 winter (n 5), -35% Jun-Jul
    2026 (n 14), +26% Aug-Sep 2026 full ladders (n 39); all 111 entries +15.0% (t 1.55).
  * Forward: the logger measures frozen rule E nightly on every new KXRAIN date regardless of W (gate in
    preregistration.json), and C2 on October 2026 (31 backfilled entries already) -> T2 screen ~2026-11-02.

Usage: python -m lab.kalshi.strategies.r3_seasonal_regime_monitors [backcast|monthly|fillcheck|forward|all]"""
from __future__ import annotations
import bisect, calendar, datetime as dt, json, math, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/r3_seasonal_regime_monitors")
RULE_E = "E|rain|S0.06|H30|NO"
WIN_H = 18                      # rule E decision window (hours before close)
W_SPREAD = 0.06
ASK_LO, ASK_HI = 0.06, 0.96     # rule E posting range on the YES ask
TRIG1_SHARE, TRIG1_DAYS, MIN_SNAPS = 0.10, 5, 12
TRIG2_N, TRIG2_RET = 30, 0.10
C2_LO, C2_HI = 0.35, 0.65
SOURCES = [   # (name, candles, markets) - later sources win on duplicate events
    ("arch", "data/kalshi_lab/strategies/microstructure/arch_candles.jsonl", "data/kalshi_lab/strategies/rain_history/markets.jsonl"),
    ("rain_history", "data/kalshi_lab/strategies/rain_history/candles.jsonl", "data/kalshi_lab/strategies/rain_history/markets.jsonl"),
    ("lab", "data/kalshi_lab/candles/KXRAIN.jsonl", "data/kalshi_lab/markets/KXRAIN.jsonl"),
]


# ------------------------------------------------------------------ shared indicator (also used by the logger)
def classify(ask: float | None, bid: float | None) -> tuple[bool, bool, bool, bool]:
    """-> (quoted, wide_any, eligible, wide_eligible) for one market quote (YES ask / bid in dollars)."""
    if ask is None or bid is None:
        return False, False, False, False
    quoted = True
    wide_any = ask - bid >= W_SPREAD - 1e-9
    eligible = 0 < bid < ask < 1 and ASK_LO - 1e-9 <= ask <= ASK_HI + 1e-9
    return quoted, wide_any, eligible, eligible and wide_any


def event_date(e: str) -> str:
    """KXRAIN-26AUG23 -> 2026-08-23."""
    code = e.split("-")[1]
    return dt.datetime.strptime(code, "%y%b%d").date().isoformat()


def stats(rows: list[dict], key: str = "e", order: str | None = None) -> dict:
    """Equal-$ return per trade, clustered t (event sums of deviations, the statistic matching the trade mean).
    Halves split at the median of the `order` field's distinct values (default: the cluster key, which must then sort
    chronologically, e.g. an ISO date)."""
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r[key]].append(r["ret"])
    mean = st.mean(r["ret"] for r in rows)
    dev = [sum(x - mean for x in v) for v in ev.values()]
    se = math.sqrt(sum(d * d for d in dev)) / len(rows)
    rs = sorted((r["ret"] for r in rows), reverse=True)
    out = {"n": len(rows), "events": len(ev), "win": round(sum(r["won"] for r in rows) / len(rows), 4),
           "avg_px": round(st.mean(r["px"] for r in rows), 4), "ret_per_dollar": round(mean, 4),
           "t": round(mean / se, 2) if se > 0 and len(ev) > 2 else None,
           "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None}
    ok = order or key
    ks = sorted({r[ok] for r in rows})
    if len(ks) >= 2:
        mid = ks[len(ks) // 2]
        h1 = [r["ret"] for r in rows if r[ok] < mid]; h2 = [r["ret"] for r in rows if r[ok] >= mid]
        out["half1"] = round(st.mean(h1), 4) if h1 else None; out["half2"] = round(st.mean(h2), 4) if h2 else None
    return out


# ------------------------------------------------------------------ backcast (1): KXRAIN spread regime and rule E
def load_rain() -> dict[str, tuple[dict, list]]:
    """ticker -> (market meta, candles clipped to [close - 18 h, close + 60]), whole event dates only."""
    from lab.kalshi.fetch import PLAN, window
    best: dict[str, tuple[str, dict, dict]] = {}       # event -> (source, {ticker: meta}, {ticker: candles})
    for name, cf, mf in SOURCES:
        meta = {}
        for l in open(mf):
            m = json.loads(l)
            if (m.get("series") or "KXRAIN") == "KXRAIN" and m["t"].startswith("KXRAIN-") and m.get("result") in ("yes", "no") and m.get("close"):
                m.setdefault("type", "greater"); meta[m["t"]] = m
        cand = {}
        for l in open(cf):
            x = json.loads(l)
            if x["t"] in meta and x["c"] and x["t"] not in cand:
                cand[x["t"]] = x["c"]
        per = defaultdict(set)
        for t, m in meta.items():
            per[m["e"]].add(t)
        for e, ts in per.items():
            if ts <= set(cand):
                best[e] = (name, {t: meta[t] for t in ts}, {t: cand[t] for t in ts})
    out = {}
    for e, (name, ms, cs) in best.items():
        for t, m in ms.items():
            w = window(m, PLAN["KXRAIN"])
            c = [r for r in cs[t] if w[0] <= r[0] <= w[1] + 60]
            if c:
                m = dict(m); m["src"] = name
                out[t] = (m, c)
    return out


def snapshots(m: dict, c: list) -> list[tuple[int, tuple]]:
    """Top-of-hour snapshots H in (close - 18 h, close - 60]: quote = last candle with T <= H (no age limit)."""
    T = [r[0] for r in c]; out = []
    h0 = ((m["close"] - WIN_H * 3600) // 3600 + 1) * 3600
    for H in range(h0, m["close"] - 60 + 1, 3600):
        i = bisect.bisect_right(T, H) - 1
        if i < 0:
            continue
        out.append((H, classify(c[i][1], c[i][2])))
    return out


def backcast() -> dict:
    from lab.kalshi.strategies.microstructure_data import Grid
    from lab.kalshi.strategies.microstructure import rules_maker
    from lab.kalshi.fetch import PLAN, window
    data = load_rain()
    day = defaultdict(lambda: {"snaps": defaultdict(lambda: [0, 0, 0, 0]), "markets": 0, "src": set()})
    E = []
    for t, (m, c) in data.items():
        d = event_date(m["e"]); D = day[d]; D["markets"] += 1; D["src"].add(m["src"])
        for H, (q, wa, el, we) in snapshots(m, c):
            s = D["snaps"][H]; s[0] += q; s[1] += wa; s[2] += el; s[3] += we
        g = Grid(m, "rain", c, window(m, PLAN["KXRAIN"])[1])
        if g.n >= 2:
            for r in rules_maker(g):
                if r["rule"] == RULE_E:
                    r["d"] = d; E.append(r)
    dates = sorted(day)
    table = []
    for d in dates:
        S = day[d]["snaps"].values()
        q = sum(s[0] for s in S); wa = sum(s[1] for s in S); el = sum(s[2] for s in S); we = sum(s[3] for s in S)
        good = sum(1 for s in S if s[2] > 0)
        er = [r for r in E if r["d"] == d]
        table.append({"date": d, "src": "+".join(sorted(day[d]["src"])), "markets": day[d]["markets"], "snapshots": len(S),
                      "snapshots_with_eligible": good, "W": round(we / el, 4) if el else None, "W_all": round(wa / q, 4) if q else None,
                      "eligible_per_snapshot": round(el / len(S), 2) if S else 0, "wide_per_snapshot": round(we / len(S), 2) if S else 0,
                      "E_fills": len(er), "E_ret": round(st.mean(r["ret"] for r in er), 4) if er else None})
    # trigger 1, ex ante: on for date D iff the 5 previous event dates (consecutive calendar dates) all had W > 10%
    # with >= 12 snapshots. Event D-1 closes by ~08:00 UTC on D, before D's 18 h window opens, so this is known in time.
    by = {r["date"]: r for r in table}
    for r in table:
        d0 = dt.date.fromisoformat(r["date"]); prev = [(d0 - dt.timedelta(days=k)).isoformat() for k in range(1, TRIG1_DAYS + 1)]
        r["trigger1_on"] = all(p in by and by[p]["W"] is not None and by[p]["W"] > TRIG1_SHARE and by[p]["snapshots_with_eligible"] >= MIN_SNAPS for p in prev)
        r["W_gt_10"] = r["W"] is not None and r["W"] > TRIG1_SHARE and r["snapshots_with_eligible"] >= MIN_SNAPS
    on = {r["date"] for r in table if r["trigger1_on"]}
    cut = dates[int(len(dates) * 0.7)]
    split = lambda R, part: [r for r in R if (r["d"] < cut) == (part == "disc")]
    for r in E:
        r["e"] = r["d"]
    res = {"dates": [dates[0], dates[-1]], "n_dates": len(dates), "split_cut": cut, "per_day": table}
    res["rule_E"] = {part: {"ungated": stats(split(E, part)), "gated_trigger1": stats([r for r in split(E, part) if r["d"] in on])}
                     for part in ("disc", "val")}
    hi = {r["date"] for r in table if r["W_gt_10"]}
    res["rule_E_same_day_regime (descriptive, not ex ante)"] = {
        "W>10%": stats([r for r in E if r["d"] in hi]), "W<=10%": stats([r for r in E if r["d"] not in hi])}
    # concentration by regime period: a skewed payoff can rest on a handful of markets
    def conc(R):
        bym = defaultdict(float)
        for r in R:
            bym[r["m"]] += r["ret"]
        top = [k for k, _ in sorted(bym.items(), key=lambda kv: -kv[1])]
        return {"all": stats(R), "without_top1_market": stats([r for r in R if r["m"] not in top[:1]]),
                "without_top3_markets": stats([r for r in R if r["m"] not in top[:3]]), "top3_markets": top[:3],
                "fills_per_day": round(len(R) / max(1, len({r["d"] for r in R})), 1),
                "median_fill_minute_volume": st.median(r["vol"] for r in R) if R else None}
    res["rule_E_by_regime_period"] = {lab: conc([r for r in E if lo <= r["d"] <= hi]) for lab, lo, hi in (
        ("wide 2026-07-31..08-31 (W 27%)", "2026-07-31", "2026-08-31"), ("transition 2026-09-01..09-16 (W 4%)", "2026-09-01", "2026-09-16"),
        ("narrow 2026-09-17..10-07 (W 0-3%)", "2026-09-17", "2026-10-07"))}
    # capacity: median contracts traded in the fill minute
    res["E_fill_minute_volume_median"] = {part: (st.median(r["vol"] for r in split(E, part)) if split(E, part) else None) for part in ("disc", "val")}
    # does the daily share predict the day's E return? (Spearman over days with fills)
    pts = [(r["W"], r["E_ret"]) for r in table if r["E_ret"] is not None and r["W"] is not None]
    res["spearman_W_vs_dayEret"] = round(spearman(pts), 3) if len(pts) > 5 else None
    res["days_with_E_fills"] = len(pts)
    # regime timeline: last date with W > 10%, streak lengths
    res["last_date_W_gt_10"] = max(hi) if hi else None
    res["trigger1_on_dates"] = sorted(on)
    W = [r["W"] for r in table if r["W"] is not None]
    res["W_by_period"] = {lab: (round(st.mean(x), 4) if x else None) for lab, x in (
        ("2026-07-31..08-31", [r["W"] for r in table if r["date"] <= "2026-08-31" and r["W"] is not None]),
        ("2026-09-01..09-15", [r["W"] for r in table if "2026-09-01" <= r["date"] <= "2026-09-15" and r["W"] is not None]),
        ("2026-09-16..09-30", [r["W"] for r in table if "2026-09-16" <= r["date"] <= "2026-09-30" and r["W"] is not None]),
        ("2026-10-01..10-07", [r["W"] for r in table if r["date"] >= "2026-10-01" and r["W"] is not None]))}
    (OUT / "backcast_rain.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def spearman(pts: list[tuple[float, float]]) -> float:
    def rank(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(o):
            j = i
            while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
                j += 1
            for k in range(i, j + 1):
                r[o[k]] = (i + j) / 2
            i = j + 1
        return r
    a = rank([p[0] for p in pts]); b = rank([p[1] for p in pts])
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b)); den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else float("nan")


# ------------------------------------------------------------------ backcast (2): monthly rain C2 by season
def fee_per(p: float, n: int) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def monthly() -> dict:
    """Frozen C2 (r2_slow_accumulators frozen.json) on every monthly-rain market with hourly candles, by event month.
    Read-only reuse of r2_slow_accumulators' markets/candles and its ACIS cache (month-to-date, for the 'unlocked' test)."""
    from lab.kalshi.strategies.r2_slow_accumulators_data import load_markets, load_candles, station_of
    from lab.kalshi.strategies.r2_slow_accumulators_model import mtd, ym, TZ
    from lab.kalshi.strategies.r2_slow_accumulators import at
    from lab.kalshi.strategies.r2_slow_accumulators_select import selected
    mk = load_markets(); cd = load_candles(); sel = set(selected())
    T = []; band_days = defaultdict(int)
    for t, c in cd.items():
        m = mk.get(t)
        if not m or m["type"] != "greater" or m["floor"] is None or not c or m.get("result") not in ("yes", "no"):
            continue
        s_ = station_of(m)
        if not s_:
            continue
        y, mo = ym(m["e"]); tz = ZoneInfo(TZ[s_]); K = float(m["floor"]); won_yes = m["result"] == "yes"
        for day in range(1, calendar.monthrange(y, mo)[1] + 1):
            tdec = int(dt.datetime(y, mo, day, 10, tzinfo=tz).timestamp())
            if not (m["open"] <= tdec < m["close"]):
                continue
            have = mtd(s_, y, mo, day)
            if have is None or have > K + 1e-9:
                continue
            q0 = at(c, tdec)
            if not q0 or not (C2_LO <= 1 - q0[1] < C2_HI):
                continue
            band_days[(y, mo)] += 1
            q1 = at(c, tdec + 3600)
            if not q1 or tdec + 3600 >= m["close"]:
                continue
            px = round(1 - q1[1], 4)
            if not (0.01 <= px <= 0.99):
                continue
            n = max(1, int(5 / px))
            pnl = (0.0 if won_yes else 1.0) - px - fee_per(px, n)
            T.append({"t": t, "e": m["e"], "ym": f"{y}-{mo:02d}", "mon": mo, "px": px, "won": not won_yes, "ret": pnl / px,
                      "sampled": t in sel or (y, mo) < (2026, 8), "full_ladder": (y, mo) >= (2026, 8), "vol": q1[2]})
            break                                   # once per market
    cool = lambda r: r["mon"] in (11, 12, 1, 2, 3, 4, 5)
    S = [r for r in T if r["sampled"]]
    res = {"rule": "C2: taker NO if 1-yes_bid in [0.35,0.65) at 10:00 local on an unlocked strike, fill at the 11:00 quote, once per market, $5 order fee rounding",
           "all_markets": stats(T, order="ym"), "sampled_strikes": stats(S, order="ym"),
           "sampled_cool_season_Nov_May": stats([r for r in S if cool(r)], order="ym"),
           "sampled_warm_season_Jun_Oct": stats([r for r in S if not cool(r)], order="ym"),
           "by_winter": {lab: stats([r for r in S if lo <= r["ym"] <= hi], order="ym") for lab, lo, hi in (
               ("2024-02..2024-05", "2024-02", "2024-05"), ("2024-11..2025-05", "2024-11", "2025-05"), ("2025-11..2026-05", "2025-11", "2026-05"))},
           "by_summer": {lab: stats([r for r in S if lo <= r["ym"] <= hi], order="ym") for lab, lo, hi in (
               ("2024-06..2024-10", "2024-06", "2024-10"), ("2025-06..2025-10", "2025-06", "2025-10"), ("2026-06..2026-09", "2026-06", "2026-09"))},
           "full_ladders_2026_08_09": stats([r for r in T if r["full_ladder"]], order="ym"),
           "r2_validation_universe_Jun_Sep_2026": stats([r for r in T if r["ym"] >= "2026-06" and (r["sampled"] or r["full_ladder"])], order="ym"),
           "r2_discovery_universe_to_May_2026": stats([r for r in T if r["ym"] <= "2026-05"], order="ym"),
           "by_month": {k: stats([r for r in T if r["ym"] == k]) for k in sorted({r["ym"] for r in T})},
           "band_market_days_per_month": {f"{y}-{m:02d}": v for (y, m), v in sorted(band_days.items())},
           "fill_hour_volume_median": st.median(r["vol"] for r in T) if T else None}
    (OUT / "backcast_monthly.json").write_text(json.dumps(res, indent=1, default=str))
    return res


# ------------------------------------------------------------------ forward summary (reads the logger's output)
def forward() -> dict:
    from lab.kalshi.strategies.r3_seasonal_regime_monitors_logger import summarize
    s = summarize()
    print(json.dumps(s, indent=1, default=str))
    return s


# ------------------------------------------------------------------ fill-model check on the backcast's validation dates
def fillcheck(d_from: str = "2026-09-17", d_to: str = "2026-10-07") -> dict:
    """Frozen rule E on the validation event dates with 1-minute candles that carry the trade 'price' block (fetched with
    the logger's own pipeline, tagged 'research'): the frozen quote-through fill vs the shared maker_fill() (prints at or
    through our created level, crossed -> taker + fee). Output: fillcheck/e_orders.jsonl, fillcheck.json."""
    import lab.kalshi.strategies.r3_seasonal_regime_monitors_logger as L
    from lab.kalshi.strategies import r3_seasonal_regime_monitors_api as A
    fc = OUT / "fillcheck"; fc.mkdir(parents=True, exist_ok=True)
    L.FWD = fc / "days.jsonl"; L.EORD = fc / "e_orders.jsonl"; L.OUT = fc; L.E_FIRST_DATE = d_from
    L.kget = lambda path, tag="research", cache=False: A.kget(path, tag="research")
    A.set_cap(10 ** 6)
    state = {"e_done": sorted({json.loads(l)["date"] for l in open(L.FWD)}) if L.FWD.exists() else []}
    end = dt.date.fromisoformat(d_to) + dt.timedelta(days=2)
    now = int(dt.datetime(end.year, end.month, end.day, 23, tzinfo=dt.timezone.utc).timestamp())
    L.e_forward(state, min(now, int(time.time())), 400)
    O = [json.loads(l) for l in open(L.EORD)] if L.EORD.exists() else []
    O = [o for o in O if d_from <= o["d"] <= d_to]
    q = [dict(o["q"], e=o["d"], won=o["won"]) for o in O if "q" in o]
    mf = [dict(o["mf"], e=o["d"], won=o["won"]) for o in O if "mf" in o]
    mf_maker = [x for x in mf if not x["taker"]]; mf_thr = [dict(o["mf"], e=o["d"], won=o["won"]) for o in O if "mf" in o and o["mf"].get("kind") in ("through", "quote", "crossed")]
    res = {"dates": [d_from, d_to], "event_dates": len({o["d"] for o in O}), "orders": len(O), "fill_rate_quote": round(len(q) / len(O), 3) if O else None,
           "fill_rate_maker_fill": round(len(mf) / len(O), 3) if O else None,
           "quote_fill": stats(q), "maker_fill": stats(mf), "maker_fill_resting_only": stats(mf_maker),
           "maker_fill_without_touch_fills": stats(mf_thr),
           "maker_fill_kinds": {k: sum(1 for o in O if o.get("mf", {}).get("kind") == k) for k in ("crossed", "through", "quote", "touch", "queue")},
           "unfilled_orders_win_rate_for_NO": round(st.mean(o["won"] for o in O if "mf" not in o), 3) if any("mf" not in o for o in O) else None,
           "filled_orders_win_rate_for_NO": round(st.mean(o["won"] for o in O if "mf" in o), 3) if mf else None}
    (OUT / "fillcheck.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    OUT.mkdir(parents=True, exist_ok=True)
    if cmd in ("backcast", "all"):
        r = backcast()
        print("KXRAIN dates", r["dates"], r["n_dates"], "cut", r["split_cut"])
        for x in r["per_day"]:
            print(f"  {x['date']} {x['src']:12s} mk={x['markets']:3d} snaps={x['snapshots']:3d}/{x['snapshots_with_eligible']:3d} "
                  f"W={x['W'] if x['W'] is not None else float('nan'):.3f} Wall={x['W_all'] if x['W_all'] is not None else float('nan'):.3f} "
                  f"el/snap={x['eligible_per_snapshot']:5.2f} wide/snap={x['wide_per_snapshot']:5.2f} trig={int(x['trigger1_on'])} "
                  f"E n={x['E_fills']:3d} ret={x['E_ret'] if x['E_ret'] is not None else float('nan'):+.3f}")
        print(json.dumps({k: v for k, v in r.items() if k != "per_day"}, indent=1, default=str))
    if cmd in ("monthly", "all"):
        print(json.dumps(monthly(), indent=1, default=str))
    if cmd == "forward":
        forward()
    if cmd == "fillcheck":
        print(json.dumps(fillcheck(), indent=1))
