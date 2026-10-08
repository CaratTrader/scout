"""arb_ladder: structural arbitrage / consistency violations inside one Kalshi event.

Two kinds of riskless trades exist when quotes are inconsistent:
  (a) exhaustive, mutually exclusive outcomes (weather daily-high buckets: 6 per event; sports/tennis/esports 2-way;
      soccer 3-way): buy YES on every outcome when sum(ask + fee) < 1 (payoff exactly 1), or buy NO on a subset S
      when sum_S(bid - fee(1 - bid)) > 1 (payoff >= |S| - 1).
  (b) monotone "above K" ladders (KXBTCD, KXETHD, KXINXU, KXNASDAQ100U, KXAAAGASD, KXGOLDD, KXWTI, KXNATGASD,
      KXTEMP*H): for K1 < K2, YES(above K1) + NO(above K2) pays >= 1 (2 if K1 < X <= K2), so
      ask(K1) + fee + (1 - bid(K2)) + fee < 1 is riskless.

Data: data/kalshi_lab/{markets,candles}/<SERIES>.jsonl (1-minute yes_ask/yes_bid closes, carried forward <= 30 min)
and the weather daily-high archive data/lab/us/kalshi (every minute). Ask 1.00 = no seller, bid 0.00 = no buyer.

Execution model (protocol): the signal is computed from quotes at t (a candle's end), the trade is filled as taker at
the quotes of t + delay minutes for the SAME legs; skipped if a leg has no quote / no liquidity then or the market
has closed. A second, more realistic model ("limit"): each leg is an IOC limit at the price seen at t; a leg fills
only if its quote at t + delay is at least as good, so the arb can be left with a naked leg (leg risk).
Fees: Kalshi taker fee ceil(0.07 * N * p * (1 - p)) cents per order, N contracts per leg.

Usage: .venv/bin/python -m lab.kalshi.strategies.arb_ladder scan      # all families -> episodes.jsonl (no network)
       .venv/bin/python -m lab.kalshi.strategies.arb_ladder report    # 70/30 split, discovery grid, 3 frozen -> validation
       .venv/bin/python -m lab.kalshi.strategies.arb_ladder_live snap|cross2 ...   # live checks (Kalshi calls, counted)
       .venv/bin/python -m lab.kalshi.strategies.arb_ladder_thin fetch|test        # pre-registered thin-ladder probe
"""
from __future__ import annotations
import bisect, datetime as dt, json, math, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path("data/kalshi_lab"); MK = ROOT / "markets"; CD = ROOT / "candles"
OUT = ROOT / "strategies" / "arb_ladder"
WX = Path("data/lab/us/kalshi")
MAX_AGE = 30 * 60
FEE = 0.07

MONO = {"cryptoH": ("KXBTCD", "KXETHD"), "indexH": ("KXINXU", "KXNASDAQ100U"),
        "daily_commod": ("KXAAAGASD", "KXGOLDD", "KXWTI", "KXNATGASD"),
        "tempH": ("KXTEMPMIAH", "KXTEMPNYCHS", "KXTEMPCHIHS", "KXTEMPLAXHS")}
EXCL = {"sports": ("KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME"), "soccer3": ("KXUEFANLGAME",),
        "tennis": ("KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGERMATCH"),
        "esports": ("KXCS2GAME",), "wx_daily": ("WX",)}


def fee(p: float) -> float:
    return FEE * p * (1 - p)


def order_fee(n: int, p: float) -> float:
    """Kalshi rounds the taker fee of an order up to the cent."""
    return math.ceil(round(FEE * n * p * (1 - p) * 100, 6)) / 100


class Mk:
    __slots__ = ("t", "e", "key", "won", "ts", "ask", "bid", "close")

    def __init__(self, t, e, key, won, rows, close):
        self.t, self.e, self.key, self.won, self.close = t, e, key, won, close
        rows = [r for r in rows if r[1] is not None and r[2] is not None]
        self.ts = [r[0] for r in rows]; self.ask = [r[1] for r in rows]; self.bid = [r[2] for r in rows]

    def q(self, t: int):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > MAX_AGE:
            return None
        return self.ask[i], self.bid[i]


def load_series(s: str) -> dict[str, list[Mk]]:
    meta = {m["t"]: m for m in map(json.loads, (MK / f"{s}.jsonl").open())}
    ev = defaultdict(list)
    for line in (CD / f"{s}.jsonl").open():
        x = json.loads(line); m = meta.get(x["t"])
        if not m or not x["c"]:
            continue
        key = m["floor"] if m["type"] in ("greater", "greater_or_equal") else m["t"]
        ev[m["e"]].append(Mk(m["t"], m["e"], key, m["result"] == "yes", x["c"], m["close"]))
    return ev


def load_wx() -> dict[str, list[Mk]]:
    M = json.loads((WX / "markets.json").read_text())
    ev = defaultdict(list)
    for t, m in M.items():
        f = WX / f"{t}.json"
        if not f.exists():
            continue
        cs = json.loads(f.read_text())
        g = lambda c, k: float(c[k]["close_dollars"]) if (c.get(k) or {}).get("close_dollars") is not None else None
        rows = [[int(c["end_period_ts"]), g(c, "yes_ask"), g(c, "yes_bid")] for c in cs]
        close = int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        ev[m["event_ticker"]].append(Mk(t, m["event_ticker"], t, m["result"] == "yes", rows, close))
    return {e: v for e, v in ev.items() if len(v) == 6}


# ---------------------------------------------------------------- arb detection at one instant
def ok_ask(a):
    return a is not None and 0.01 <= a <= 0.99


def ok_bid(b):
    return b is not None and 0.01 <= b <= 0.99


def best_mono(mks: list[Mk], t: int):
    """Best riskless pair at t (per-contract, unrounded fees). Returns (edge, i_low, i_high) or None. mks sorted by strike."""
    best = None; mincost = 9.0; imin = -1; prev_key = None; pend = []
    for j, m in enumerate(mks):
        q = m.q(t)
        if q is None:
            continue
        a, b = q
        if m.key != prev_key:                     # strictly lower strikes only
            for c, i in pend:
                if c < mincost:
                    mincost, imin = c, i
            pend = []; prev_key = m.key
        if ok_bid(b) and imin >= 0:
            edge = 1 - mincost - ((1 - b) + fee(1 - b))
            if best is None or edge > best[0]:
                best = (edge, imin, j)
        if ok_ask(a):
            pend.append((a + fee(a), j))
    return best


def best_excl(mks: list[Mk], t: int):
    """(edge_yes, edge_no, S_no) at t; edge None when not computable."""
    qs = [m.q(t) for m in mks]
    if any(q is None for q in qs):
        return None
    ey = 1 - sum(a + fee(a) for a, _ in qs) if all(ok_ask(a) for a, _ in qs) else None
    S = [i for i, (a, b) in enumerate(qs) if ok_bid(b) and b - fee(1 - b) > 0]
    en = sum(qs[i][1] - fee(1 - qs[i][1]) for i in S) - 1 if len(S) >= 2 else None
    return ey, en, S


# ---------------------------------------------------------------- trade simulation
def legs_cost(legs, t, mks, n):
    """legs: list of (index, side). Returns (cost_rounded_N, cost_per_contract_unrounded, prices) at t, or None."""
    cost = 0.0; cpc = 0.0; px = []
    for i, side in legs:
        q = mks[i].q(t)
        if q is None:
            return None
        a, b = q
        if side == "YES":
            if not ok_ask(a):
                return None
            p = a
        else:
            if not ok_bid(b):
                return None
            p = 1 - b
        cost += n * p + order_fee(n, p); cpc += p + fee(p); px.append(p)
    return cost, cpc, px


def payoff(legs, mks):
    return sum((1 if mks[i].won else 0) if side == "YES" else (0 if mks[i].won else 1) for i, side in legs)


def limit_fill(legs, t, delay, mks, n):
    """IOC limit at the t price per leg: fills at the t+delay price when that is no worse, else that leg is missed.
    Returns (pnl, cost, n_filled_legs)."""
    pnl = 0.0; cost = 0.0; nf = 0
    for i, side in legs:
        q0, q1 = mks[i].q(t), mks[i].q(t + delay)
        if q0 is None or q1 is None:
            continue
        p0 = q0[0] if side == "YES" else 1 - q0[1]
        p1 = q1[0] if side == "YES" else 1 - q1[1]
        valid1 = ok_ask(q1[0]) if side == "YES" else ok_bid(q1[1])
        if not valid1 or p1 > p0 + 1e-9:
            continue
        c = n * p1 + order_fee(n, p1)
        win = mks[i].won if side == "YES" else not mks[i].won
        pnl += n * (1 if win else 0) - c; cost += c; nf += 1
    return pnl, cost, nf


THRS = (0.0, 0.01, 0.02, 0.03, 0.05)
AGES = (0, 60, 120, 300)     # seconds the violation must already have lasted at decision time (survivor filter)


def edges_at(kind: str, mks: list[Mk], t: int) -> dict:
    out = {}
    if kind == "mono":
        b = best_mono(mks, t)
        if b:
            out["MONO"] = (b[0], ((b[1], "YES"), (b[2], "NO")))
    else:
        b = best_excl(mks, t)
        if b:
            ey, en, S = b
            if ey is not None:
                out["ALLYES"] = (ey, tuple((i, "YES") for i in range(len(mks))))
            if en is not None:
                out["ALLNO"] = (en, tuple((i, "NO") for i in S))
    return out


def trade(fam, e, d, edge, legs, t, close, mks, delay, n):
    """Decision at t on legs chosen from quotes at t; taker fill of the same legs at t + delay (protocol), plus the
    IOC-limit (leg-risk) model and the zero-latency reference."""
    tr = {"fam": fam, "e": e, "dir": d, "t": t, "close": close, "edge": edge, "nlegs": len(legs),
          "legs": [(mks[i].t, sd) for i, sd in legs]}
    po = payoff(legs, mks); tr["payoff"] = po
    guar = 1 if d != "ALLNO" else len(legs) - 1
    l0 = legs_cost(legs, t, mks, n)
    if l0:
        tr.update(cost0=l0[0], pnl0=n * po - l0[0], px0=l0[2], guar_pnl0=n * guar - l0[0])
    tf = t + delay
    if tf >= close:
        tr["fill"] = "closed"; return tr
    lc = legs_cost(legs, tf, mks, n)
    if lc:
        cost, cpc, px = lc
        tr.update(fill="ok", cost=cost, pnl=n * po - cost, ret=(n * po - cost) / cost, edge_fill=guar - cpc, px=px, guar_pnl=n * guar - cost)
    else:
        tr["fill"] = "noquote"
    lp, lcst, nf = limit_fill(legs, t, delay, mks, n)
    tr.update(lim_pnl=lp, lim_cost=lcst, lim_legs=nf)
    return tr


def scan_event(fam: str, kind: str, e: str, mks: list[Mk], thrs=THRS, ages=AGES, delay: int = 60, n: int = 5):
    """Walk every quote-change instant of an event. An episode (per direction and threshold) starts when the best edge
    first exceeds thr and ends at the first instant it is <= thr. For each survivor age A, one trade per episode at
    t_start + A if the violation is still there then (checked with quotes at that time only)."""
    if kind == "mono":
        mks = [m for m in mks if m.key is not None]
        mks.sort(key=lambda m: m.key)
    close = max(m.close for m in mks)
    times = [t for t in sorted({ts for m in mks for ts in m.ts}) if t < close]
    series = [(t, edges_at(kind, mks, t)) for t in times]
    arb_seconds = 0
    for (t, E), (t2, _) in zip(series, series[1:]):
        if any(v[0] > 0 for v in E.values()):
            arb_seconds += t2 - t
    rows = []
    for thr in thrs:
        cur = {}; done = []
        for t, E in series:
            for d, (edge, legs) in E.items():
                if edge > thr:
                    if d in cur:
                        cur[d]["max_edge"] = max(cur[d]["max_edge"], edge)
                    else:
                        cur[d] = {"d": d, "t0": t, "max_edge": edge, "t_end": None}
            for d in list(cur):
                if d not in E or E[d][0] <= thr:
                    cur[d]["t_end"] = t; done.append(cur.pop(d))
        for d, ep in cur.items():
            ep["t_end"] = close; done.append(ep)
        for ep in done:
            for A in ages:
                tA = ep["t0"] + A
                if tA >= close:
                    continue
                E = edges_at(kind, mks, tA) if A else dict(series[[x[0] for x in series].index(ep["t0"])][1])
                if ep["d"] not in E or E[ep["d"]][0] <= thr:
                    continue                 # the violation did not survive to age A (known at tA)
                edge, legs = E[ep["d"]]
                tr = trade(fam, e, ep["d"], edge, legs, tA, close, mks, delay, n)
                tr.update(thr=thr, age=A, t_start=ep["t0"], dur=ep["t_end"] - ep["t0"], max_edge=ep["max_edge"])
                rows.append(tr)
    return rows, len(series), arb_seconds


def scan_all() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    allep = []; summary = {}; closes = defaultdict(list)
    fams = [(f, "mono", ss) for f, ss in MONO.items()] + [(f, "excl", ss) for f, ss in EXCL.items()]
    for fam, kind, ss in fams:
        for s in ss:
            t0 = time.time()
            ev = load_wx() if s == "WX" else load_series(s)
            n_ev = 0; inst = 0; arbsec = 0; neps = 0; nyes = defaultdict(int); days = set()
            for e, mks in ev.items():
                if len(mks) < 2:
                    continue
                if kind == "excl":
                    nyes[sum(m.won for m in mks)] += 1
                eps, ni, asec = scan_event(fam, kind, e, mks)
                for x in eps:
                    x["s"] = s
                allep += eps; n_ev += 1; inst += ni; arbsec += asec; neps += len(eps)
                days.add(dt.datetime.utcfromtimestamp(max(m.close for m in mks)).date()); closes[fam].append(max(m.close for m in mks))
            summary[s] = {"fam": fam, "kind": kind, "events": n_ev, "days": len(days), "quote_instants": inst,
                          "arb_minutes_edge_gt_0": round(arbsec / 60, 1), "episodes_all_thr": neps, "yes_count_per_event": dict(nyes)}
            print(f"{fam:12s} {s:22s} events {n_ev:5d} days {len(days):4d} instants {inst:7d} arb-min {arbsec/60:8.1f} episodes {neps:5d} yes/event {dict(nyes)} ({time.time()-t0:.0f}s)", flush=True)
    with (OUT / "episodes.jsonl").open("w") as fh:
        for x in allep:
            fh.write(json.dumps(x) + "\n")
    (OUT / "scan_summary.json").write_text(json.dumps(summary, indent=1))
    (OUT / "event_closes.json").write_text(json.dumps(closes))
    return summary


def stats(rows: list[dict], key: str = "ret") -> dict:
    """Equal-$ return per trade, t clustered by event, mean without the 3 best, win = guaranteed-or-better payoff."""
    rows = [r for r in rows if r.get(key) is not None]
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r[key])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(len(em))) if len(em) > 2 and sd > 0 else float("nan")
    rs = sorted((r[key] for r in rows), reverse=True)
    return {"n": len(rows), "events": len(em), "win": sum(r[key] > 0 for r in rows) / len(rows),
            "avg_cost_per_set": st.mean(r["cost"] / 5 for r in rows if "cost" in r) if any("cost" in r for r in rows) else None,
            "ret": st.mean(r[key] for r in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan")}


def report(split: float = 0.7) -> dict:
    R = [json.loads(l) for l in (OUT / "episodes.jsonl").open()]
    for r in R:
        if r.get("fill") == "ok":
            r["ret0"] = r["pnl0"] / r["cost0"] if r.get("cost0") else None
            r["lim_ret"] = r["lim_pnl"] / r["lim_cost"] if r.get("lim_cost") else 0.0
    by_fam = defaultdict(list)
    for r in R:
        by_fam[r["fam"]].append(r)
    cut = {}; out = {"cells": {}, "cutoffs": {}}
    allcl = json.loads((OUT / "event_closes.json").read_text())
    for fam, rows in by_fam.items():
        closes = sorted(allcl[fam]); cut[fam] = closes[int(len(closes) * split)]   # 70% of ALL events of the family
        out["cutoffs"][fam] = dt.datetime.utcfromtimestamp(cut[fam]).isoformat()
    cells = defaultdict(list)
    for r in R:
        if r.get("fill") != "ok":
            continue
        part = "disc" if r["close"] < cut[r["fam"]] else "val"
        cells[(r["fam"], r["dir"], r["thr"], r["age"], part)].append(r)
    print(f"{'family':12s} {'dir':6s} {'thr':>5s} {'age':>4s} | DISCOVERY taker t+1: n  ev  win   ret     t    wo3 | t0-ref  limit | dur>=2m")
    keys = sorted({k[:4] for k in cells})
    for k in keys:
        d = cells.get(k + ("disc",), [])
        if not d:
            continue
        a = stats(d); a0 = stats(d, "ret0"); al = stats(d, "lim_ret")
        dur2 = sum(r["dur"] >= 120 for r in d) / len(d)
        out["cells"]["|".join(map(str, k))] = {"disc": a, "disc_t0": a0, "disc_limit": al}
        print(f"{k[0]:12s} {k[1]:6s} {k[2]:5.2f} {k[3]:4d} | {a['n']:5d} {a['events']:4d} {a['win']:4.0%} {a['ret']:+6.1%} {a['t']:5.1f} {a['ret_wo3']:+6.1%} | {a0['ret']:+6.1%} {al['ret']:+6.1%} | {dur2:.0%}")
    # sanity: guaranteed payoff must hold on every filled trade
    bad = [r for r in R if r.get("fill") == "ok" and r["payoff"] < (1 if r["dir"] != "ALLNO" else r["nlegs"] - 1)]
    print(f"\nsanity: trades whose settled payoff broke the guaranteed minimum: {len(bad)}")
    # frozen candidates: top 3 discovery cells (n >= 10) by mean taker-t+1 return
    disc = [(k, v["disc"]) for k, v in out["cells"].items() if v["disc"]["n"] >= 10]
    disc.sort(key=lambda kv: -kv[1]["ret"])
    frozen = [k for k, _ in disc[:3]]
    print("\nVALIDATION of the 3 frozen candidates (top discovery cells with n >= 10, taker at t+1 as the protocol requires)")
    out["frozen"] = frozen; out["validation"] = {}
    for k in frozen:
        fam, d, thr, age = k.split("|")
        rows = cells.get((fam, d, float(thr), int(age), "val"), [])
        v = stats(rows); v0 = stats(rows, "ret0"); vl = stats(rows, "lim_ret")
        if rows:
            mid = sorted(r["close"] for r in rows)[len(rows) // 2]
            h1 = [r["ret"] for r in rows if r["close"] < mid]; h2 = [r["ret"] for r in rows if r["close"] >= mid]
            v["halves"] = (st.mean(h1) if h1 else float("nan"), st.mean(h2) if h2 else float("nan"))
            span_days = (max(r["t"] for r in rows) - min(r["t"] for r in rows)) / 86400 or 1
            v["trades_per_day"] = len(rows) / span_days
            v["avg_px"] = st.mean(st.mean(r["px"]) for r in rows)
            v["hold_hours"] = st.mean((r["close"] - r["t"]) / 3600 for r in rows)
        out["validation"][k] = {"taker_t+1": v, "zero_latency_ref": v0, "ioc_limit_leg_risk": vl}
        print(f"  {k:28s} n={v.get('n',0)} ev={v.get('events',0)} ret={v.get('ret',float('nan')):+.2%} t={v.get('t',float('nan')):.1f} wo3={v.get('ret_wo3',float('nan')):+.2%} "
              f"halves={v.get('halves')} | zero-latency ref {v0.get('ret',float('nan')):+.2%} | IOC-limit {vl.get('ret',float('nan')):+.2%}")
    (OUT / "report_cells.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "scan":
        scan_all()
    elif a[0] == "report":
        report()
