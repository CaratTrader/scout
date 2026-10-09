"""Adversarial reproduction of r3_seasonal_regime_monitors (written from the claim's description only; none of its code is used).

Claim 1, rule E|rain|S0.06|H30|NO on daily KXRAIN, frozen quote fill:
  at a decision minute T (quote = last 1-minute candle ending <= T, at most 30 min old) with yes_ask - yes_bid >= 0.06,
  rest a NO bid one cent inside the spread, i.e. offer YES at P = ask_T - 0.01 (NO price 1 - P), live from T+60 s for
  30 minutes. If the quote at T+60 already crossed it (yes_bid >= P) it is a taker NO fill at 1 - yes_bid(T+60) with the
  taker fee; otherwise it fills (maker, fee 0 on 'quadratic' KXRAIN) only if some later candle inside the live window
  shows yes_bid_high >= P + 0.01 (trade-through). NO price in [0.03, 0.97]; one order per market per 30 minutes; held
  to settlement. Fee 0.07 p (1-p) per contract, rounded up to the cent per order of floor($5/p) contracts.
  Split: KXRAIN event dates 2026-07-31..10-07 ordered by close, discovery = first 70% of dates, validation = last 30%.
Claim 2, C2 monthly rain (r2_slow_accumulators frozen C2): daily at 10:00 local, NO if 1 - yes_bid in [0.35, 0.65) on a
  strike not yet exceeded by the month-to-date (through yesterday), one entry per market, fill at the 11:00 local
  hourly quote (NO at 1 - yes_bid), validation universe = Jun-Sep 2026 events with candles.

Usage: .venv/bin/python -m lab.kalshi.strategies.r3_seasonal_regime_monitors_repro [E|C2|all]
"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r3_seasonal_regime_monitors/repro"
D = ROOT / "data/kalshi_lab"
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
EPS = 1e-9
STAKE = 5.0


def order_fee(p: float, n: int) -> float:
    return math.ceil(0.07 * p * (1 - p) * n * 100 - EPS) / 100 if n > 0 else 0.0


def ev_date(e: str) -> dt.date:
    s = e.split("-")[1]                     # e.g. 26SEP17
    return dt.date(2000 + int(s[:2]), MON[s[2:5]], int(s[5:7]))


# ------------------------------------------------------------------ data
def load_rain() -> dict:
    """{ticker: (meta, candles)} for whole event dates 07-31..10-07, each date from exactly one source."""
    srcs = [  # (markets, candles, date predicate)
        (D / "strategies/microstructure/arch_markets.jsonl", D / "strategies/microstructure/arch_candles.jsonl",
         lambda d: dt.date(2026, 7, 31) <= d <= dt.date(2026, 8, 7)),
        (D / "strategies/rain_history/markets.jsonl", D / "strategies/rain_history/candles.jsonl",
         lambda d: dt.date(2026, 8, 8) <= d <= dt.date(2026, 8, 22) or d == dt.date(2026, 10, 7)),
        (D / "markets/KXRAIN.jsonl", D / "candles/KXRAIN.jsonl",
         lambda d: dt.date(2026, 8, 23) <= d <= dt.date(2026, 10, 6)),
    ]
    out = {}
    for mp, cp, ok in srcs:
        meta = {}
        for line in mp.open():
            m = json.loads(line)
            if m["t"].startswith("KXRAIN-"):
                meta[m["t"]] = m
        for line in cp.open():
            x = json.loads(line)
            m = meta.get(x["t"])
            if not m or not x["c"] or not ok(ev_date(m["e"])):
                continue
            assert x["t"] not in out
            out[x["t"]] = (m, sorted(x["c"], key=lambda r: r[0]))
    return out


def q_at(c: list, idx_ts: list, t: int, max_age: int = 1800):
    """Last candle with ts <= t, if not older than max_age seconds."""
    import bisect
    i = bisect.bisect_right(idx_ts, t) - 1
    if i < 0 or t - c[i][0] > max_age or c[i][1] is None or c[i][2] is None:
        return None
    return c[i]


# ------------------------------------------------------------------ rule E
def sim_E(m: dict, c: list, S=0.06, H=30, scan="candle", cooldown="order", clip_h=18, lo=0.03, hi=0.97, delay=60,
          margin=0.01, maker_only=False) -> list[dict]:
    """All orders of rule E on one market; each order dict has filled flag, fill price, kind (maker/taker)."""
    import bisect
    ts = [r[0] for r in c]
    close = m["close"]
    start = close - clip_h * 3600 if clip_h else c[0][0]
    if scan == "candle":
        times = [r[0] for r in c]
    else:  # every minute from the first candle on, carrying the quote forward
        times = list(range(c[0][0], close, 60))
    orders, next_ok = [], -1
    for T in times:
        if T < start or T < next_ok or T + delay >= close:
            continue
        q = q_at(c, ts, T)
        if not q:
            continue
        ask, bid = q[1], q[2]
        if ask is None or bid is None or ask - bid < S - EPS or ask > 1.0 or bid < 0:
            continue
        P = round(ask - 0.01, 2)               # our YES offer = NO bid at 1 - P
        nb = round(1 - P, 2)
        if not (lo - EPS <= nb <= hi + EPS):
            continue
        o = {"t": m["t"], "e": m["e"], "date": str(ev_date(m["e"])), "T": T, "ask": ask, "bid": bid, "P": P,
             "won": m["result"] == "no", "filled": False}
        q1 = q_at(c, ts, T + delay)
        if q1 and q1[2] is not None and q1[2] >= P - EPS:
            if maker_only:                     # the book crossed our price before we could rest: no order
                continue
            px = round(1 - q1[2], 4)           # book crossed at T+60: taker NO at 1 - yes_bid
            if lo - EPS <= px <= hi + EPS:
                n = int(STAKE // px)
                o.update(filled=True, kind="taker", px=px, fee=order_fee(px, n) / n if n else 0.0, tf=T + delay)
        else:
            end = min(T + delay + H * 60, close)
            j = bisect.bisect_right(ts, T + delay)
            while j < len(c) and c[j][0] <= end:
                bh = c[j][4]
                if bh is not None and bh >= P + margin - EPS:
                    o.update(filled=True, kind="maker", px=nb, fee=0.0, tf=c[j][0])
                    break
                j += 1
        if o["filled"]:
            o["ret"] = ((1.0 if o["won"] else 0.0) - o["px"] - o["fee"]) / o["px"]
        orders.append(o)
        if cooldown == "order":
            next_ok = T + 30 * 60
        elif cooldown == "fill":                 # a new order only once the previous one filled or expired
            next_ok = (o["tf"] + 60) if o["filled"] else T + delay + H * 60
    return orders


def stats(rows: list[dict], key="e") -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r[key]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    ne = len(em)
    sd = st.pstdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne)) if ne > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    dates = sorted({r["date"] for r in rows})
    mid_date = dates[len(dates) // 2]
    h1 = [r["ret"] for r in rows if r["date"] < mid_date]
    h2 = [r["ret"] for r in rows if r["date"] >= mid_date]
    # calib-style halves: median close of the rows
    mk = defaultdict(float)
    for r in rows:
        mk[r["t"]] += r["ret"]
    top = [k for k, _ in sorted(mk.items(), key=lambda kv: -kv[1])]
    wo = lambda k: st.mean([r["ret"] for r in rows if r["t"] not in set(top[:k])]) if len(top) > k else float("nan")
    return {"n": len(rows), "events": ne, "win": sum(r["won"] for r in rows) / len(rows),
            "avg_px": st.mean(r["px"] for r in rows), "ret": st.mean(r["ret"] for r in rows), "t": t,
            "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
            "wo_top1_mkt": wo(1), "wo_top3_mkt": wo(3), "top_mkts": top[:3],
            "taker_share": sum(r.get("kind") == "taker" for r in rows) / len(rows)}


def wide_share(data: dict, S=0.06) -> dict:
    """Per event date: share of (open market, top-of-hour) quotes with spread >= S (an hourly W proxy)."""
    import bisect
    num, den = defaultdict(int), defaultdict(int)
    for tk, (m, c) in data.items():
        ts = [r[0] for r in c]
        d = str(ev_date(m["e"]))
        h0 = (c[0][0] // 3600 + 1) * 3600
        for T in range(h0, m["close"], 3600):
            if T < m["close"] - 18 * 3600:
                continue
            q = q_at(c, ts, T)
            if not q:
                continue
            den[d] += 1
            num[d] += (q[1] - q[2]) >= S - EPS
    return {d: num[d] / den[d] for d in sorted(den)}


def run_E() -> dict:
    data = load_rain()
    dates = sorted({str(ev_date(m["e"])) for m, _ in data.values()}, key=lambda d: d)
    # order dates by (max) close time of their markets
    dclose = defaultdict(int)
    for m, _ in data.values():
        dclose[str(ev_date(m["e"]))] = max(dclose[str(ev_date(m["e"]))], m["close"])
    dates = sorted(dclose, key=lambda d: dclose[d])
    cut = dates[int(len(dates) * 0.7)]
    disc_dates, val_dates = set(dates[:dates.index(cut)]), set(dates[dates.index(cut):])
    res = {"n_markets": len(data), "n_dates": len(dates), "first": dates[0], "last": dates[-1], "val_first": cut,
           "val_markets": sum(1 for m, _ in data.values() if str(ev_date(m["e"])) in val_dates), "variants": {}}
    variants = {
        "primary(candle-scan,cooldown-order,clip18)": dict(scan="candle", cooldown="order", clip_h=18),
        "minute-scan": dict(scan="minute", cooldown="order", clip_h=18),
        "cooldown-fill": dict(scan="candle", cooldown="fill", clip_h=18),
        "no-clip": dict(scan="candle", cooldown="order", clip_h=0),
        "minute-scan,no-clip": dict(scan="minute", cooldown="order", clip_h=0),
        "STRESS delay120s": dict(scan="candle", cooldown="order", clip_h=18, delay=120),
        "STRESS delay180s": dict(scan="candle", cooldown="order", clip_h=18, delay=180),
        "STRESS tradethrough+2c": dict(scan="candle", cooldown="order", clip_h=18, margin=0.02),
        "STRESS maker-only(no taker branch)": dict(scan="candle", cooldown="order", clip_h=18, maker_only=True),
    }
    keep = {}
    for name, kw in variants.items():
        orders = []
        for m, c in data.values():
            orders += sim_E(m, c, **kw)
        fills = [o for o in orders if o["filled"]]
        dv = [o for o in fills if o["date"] in disc_dates]
        vv = [o for o in fills if o["date"] in val_dates]
        vo = [o for o in orders if o["date"] in val_dates]
        unf = [o for o in vo if not o["filled"]]
        r = {"discovery": stats(dv), "validation": stats(vv),
             "val_orders": len(vo), "val_fill_rate": len(vv) / len(vo) if vo else float("nan"),
             "val_unfilled_no_winrate": sum(o["won"] for o in unf) / len(unf) if unf else float("nan"),
             "val_trades_per_day": len(vv) / len(val_dates)}
        # period breakdown on all dates
        per = {"Aug": lambda d: d < "2026-09-01", "Sep1-16": lambda d: "2026-09-01" <= d <= "2026-09-16",
               "Sep17-Oct7": lambda d: d >= "2026-09-17"}
        r["periods"] = {k: {kk: vv_ for kk, vv_ in stats([o for o in fills if f(o["date"])]).items()
                            if kk in ("n", "events", "ret", "t", "wo_top3_mkt")} for k, f in per.items()}
        # validation without KXRAIN-26SEP30-DTW
        r["val_wo_DTW"] = st.mean([o["ret"] for o in vv if o["t"] != "KXRAIN-26SEP30-DTW"]) if vv else None
        r["val_DTW_fills"] = sum(o["t"] == "KXRAIN-26SEP30-DTW" for o in vv)
        res["variants"][name] = r
        keep[name] = vv
    W = wide_share(data)
    res["W_by_date"] = W
    for k, f in {"Aug": lambda d: d < "2026-09-01", "Sep1-15": lambda d: "2026-09-01" <= d <= "2026-09-15",
                 "Sep16-30": lambda d: "2026-09-16" <= d <= "2026-09-30", "Oct1-7": lambda d: d >= "2026-10-01"}.items():
        xs = [v for d, v in W.items() if f(d)]
        res.setdefault("W_period_mean", {})[k] = st.mean(xs) if xs else None
    res["W_last_date_above_10pct"] = max([d for d, v in W.items() if v > 0.10], default=None)
    # T1 gate on validation: W > 10% on each of the 5 prior event dates
    gated = []
    for o in keep["primary(candle-scan,cooldown-order,clip18)"]:
        i = dates.index(o["date"])
        if i >= 5 and all(W.get(d, 0) > 0.10 for d in dates[i - 5:i]):
            gated.append(o)
    res["T1_gated_validation"] = stats(gated)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "E_validation_fills.jsonl").open("w") as f:
        for o in keep["primary(candle-scan,cooldown-order,clip18)"]:
            f.write(json.dumps(o) + "\n")
    return res


# ------------------------------------------------------------------ C2 monthly rain
SA = D / "strategies/r2_slow_accumulators"
STN = {"KXRAINNYCM": ("NY CITY CENTRAL PARK", "America/New_York"), "RAINNYCM": ("NY CITY CENTRAL PARK", "America/New_York"),
       "KXRAINCHIM": ("CHICAGO MIDWAY AP", "America/Chicago"), "KXRAINAUSM": ("AUSTIN BERGSTROM INTL AP", "America/Chicago"),
       "KXRAINDALM": ("DAL-FTW WSCMO AP", "America/Chicago"), "KXRAINDENM": ("DENVER INTL AP", "America/Denver"),
       "KXRAINHOUM": ("HOUSTON WILLIAM P HOBBY AP", "America/Chicago"), "KXRAINLAXM": ("LOS ANGELES INTL AP", "America/Los_Angeles"),
       "KXRAINMIAM": ("MIAMI INTERNATIONAL AP", "America/New_York"), "KXRAINSEAM": ("SEATTLE TACOMA AIRPORT", "America/Los_Angeles"),
       "KXRAINSFOM": ("SAN FRANCISCO INTERNATIONAL AP", "America/Los_Angeles"),
       "KXRAINSTPM": ("ST PETERSBURG ALBERT WHITTED AP", "America/New_York")}


def load_acis() -> dict:
    out = {}
    for f in (SA / "acis_2026-10-07").glob("*.txt"):
        j = json.loads(f.read_text())
        daily = {}
        for d_, v in j["data"]:
            daily[d_] = 0.0 if v in ("T", "0.00") else (float(v) if v not in ("M", "S", "") and not v.endswith("A") else (float(v[:-1]) if v.endswith("A") else None))
        out[j["meta"]["name"]] = daily
    return out


def ev_month(e: str) -> tuple[int, int]:
    s = e.split("-")[1]                     # e.g. 26AUG
    return 2000 + int(s[:2]), MON[s[2:5]]


def run_C2(max_age_h: float = 24, lo=0.35, hi=0.65, dec_h=10, fill_h=11, months=((2026, 6), (2026, 7), (2026, 8), (2026, 9))) -> dict:
    import bisect
    from zoneinfo import ZoneInfo
    acis = load_acis()
    meta = {}
    for line in (SA / "markets.jsonl").open():
        m = json.loads(line)
        meta[m["t"]] = m
    rows, skipped = [], defaultdict(int)
    for line in (SA / "candles.jsonl").open():
        x = json.loads(line)
        m = meta.get(x["t"])
        if not m or not x["c"]:
            continue
        y, mo = ev_month(m["e"])
        if (y, mo) not in months:
            continue
        stn, tzn = STN[m["series"]]
        tz = ZoneInfo(tzn)
        daily = acis[stn]
        c = sorted(x["c"], key=lambda r: r[0]); ts = [r[0] for r in c]
        floor = float(m["floor"])
        ndays = (dt.date(y + (mo == 12), mo % 12 + 1, 1) - dt.date(y, mo, 1)).days
        mtd = 0.0
        for day in range(1, ndays + 1):
            if day > 1:
                v = daily.get(str(dt.date(y, mo, day - 1)))
                mtd += v or 0.0
            if mtd > floor + EPS:
                skipped["locked"] += 1
                break                                   # strike exceeded: locked, no more entries
            T = int(dt.datetime(y, mo, day, dec_h, tzinfo=tz).timestamp())
            TF = int(dt.datetime(y, mo, day, fill_h, tzinfo=tz).timestamp())
            if T < m["open"] or TF >= m["close"]:
                continue
            i = bisect.bisect_right(ts, T) - 1
            if i < 0 or T - c[i][0] > max_age_h * 3600 or c[i][2] is None:
                continue
            nop = 1 - c[i][2]
            if not (lo - EPS <= nop < hi - EPS):
                continue
            j = bisect.bisect_right(ts, TF) - 1
            if j < 0 or TF - c[j][0] > max_age_h * 3600 or c[j][2] is None:
                skipped["no_fill_quote"] += 1
                break
            px = round(1 - c[j][2], 4)
            if not (0.01 <= px <= 0.99):
                break
            n = int(STAKE // px)
            fee_c = order_fee(px, n) / n
            won = m["result"] == "no"
            rows.append({"t": m["t"], "e": m["e"], "date": str(dt.date(y, mo, day)), "entry": TF, "close": m["close"],
                         "px": px, "won": won, "fee": fee_c, "ret": ((1.0 if won else 0.0) - px - fee_c) / px,
                         "dec_no": nop, "series": m["series"]})
            break                                       # one entry per market
    r = stats(rows)
    # alternative halves: chronological by entry time and by event close (calib style)
    srt = sorted(rows, key=lambda q: q["entry"])
    h = len(srt) // 2
    r["half_entry"] = (st.mean(q["ret"] for q in srt[:h]), st.mean(q["ret"] for q in srt[h:])) if rows else None
    mid = sorted(q["close"] for q in rows)[len(rows) // 2] if rows else 0
    a = [q["ret"] for q in rows if q["close"] < mid]; b = [q["ret"] for q in rows if q["close"] >= mid]
    r["half_close"] = (st.mean(a) if a else None, st.mean(b) if b else None)
    g = defaultdict(list)
    for q in rows:
        g[q["e"]].append(q["ret"])
    if rows:
        mu = r["ret"]; G = len(g)
        se = math.sqrt(sum(sum(v - mu for v in vs) ** 2 for vs in g.values())) / len(rows) * math.sqrt(G / (G - 1))
        r["t_cr1"] = mu / se if se else float("nan")
    r["skipped"] = dict(skipped)
    r["by_month"] = {k: {kk: vv for kk, vv in stats([q for q in rows if q["e"].endswith(k)]).items() if kk in ("n", "ret")}
                     for k in ("26JUN", "26JUL", "26AUG", "26SEP")}
    return r, rows


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    OUT.mkdir(parents=True, exist_ok=True)
    if what in ("E", "all"):
        r = run_E()
        (OUT / "E_repro.json").write_text(json.dumps(r, indent=1, default=str))
        for k, v in r["variants"].items():
            dv, vv = v["discovery"], v["validation"]
            print(f"{k:45s} DISC n={dv['n']} ret={dv['ret']:+.3f} t={dv['t']:.2f} | VAL n={vv['n']} ev={vv['events']} win={vv['win']:.3f} "
                  f"px={vv['avg_px']:.3f} ret={vv['ret']:+.4f} t={vv['t']:.2f} wo3={vv['ret_wo3']:+.3f} h={vv['half1']:+.3f}/{vv['half2']:+.3f} "
                  f"woTop1={vv['wo_top1_mkt']:+.3f} woTop3={vv['wo_top3_mkt']:+.3f} taker={vv['taker_share']:.2f} woDTW={v['val_wo_DTW']:+.3f} DTW={v['val_DTW_fills']}")
            print("   periods", json.dumps(v["periods"]))
        print("W", json.dumps(r["W_period_mean"]), "last>10%", r["W_last_date_above_10pct"], "T1 gated", r["T1_gated_validation"].get("n"))
        print({k: r[k] for k in ("n_markets", "n_dates", "first", "last", "val_first", "val_markets")})
    if what in ("C2", "all"):
        allr = {}
        for age in (2, 6, 24, 1e9):
            r, rows = run_C2(max_age_h=age)
            allr[f"max_age_{age:g}h"] = r
            print(f"C2 max_age {age:g}h: n={r['n']} ev={r.get('events')} win={r.get('win', 0):.3f} px={r.get('avg_px', 0):.3f} ret={r.get('ret', 0):+.4f} "
                  f"t_evmean={r.get('t', 0):.2f} t_cr1={r.get('t_cr1', 0):.2f} wo3={r.get('ret_wo3', 0):+.3f} halves_date={r.get('half1', 0):+.3f}/{r.get('half2', 0):+.3f} "
                  f"half_entry={r['half_entry']} half_close={r['half_close']} skipped={r['skipped']} months={r['by_month']}")
            if age == 24:
                with (OUT / "C2_validation_entries.jsonl").open("w") as f:
                    for q in rows:
                        f.write(json.dumps(q) + "\n")
        (OUT / "C2_repro.json").write_text(json.dumps(allr, indent=1, default=str))
