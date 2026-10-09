"""r4_nested_deadline_term_structure: in nested 'by date' ladders (one event, rungs 'X happens before D_1 < D_2 < ...'),
is the near rung's YES priced above the hazard implied by the far rungs, so that NO on the near rung earns more than
the plain long-dated NO band (band B of the round-4 census)?

Data: r4_nested_deadline_term_structure_data (rung listings; hourly candles [ts, ask, bid, vol, hi, lo]).
Clock (leak-free): daily snapshot t at 14:00 UTC. Visible rungs: open <= t < min(close, D). The rung deadline D is parsed
from rules text fixed at listing (never the close, which moves when the event happens). Quotes: last hourly candle
<= t, carried forward <= CF hours. Ladders are thinned to deadlines >= 7 d apart (listing fields only). Near rung: open
rung with the smallest D and tau = D - t >= 1 day. Far rungs: the NFAR = 3 nearest open rungs with D_near < D <= t + 365 d
and a two-sided quote with spread <= 0.15 (mid used).
Hazard: y_j = -ln(1 - mid_j) = lambda * tau_j. 'ls' = least squares through 0 over those far rungs, 'next' = the nearest
far rung only. q = 1 - exp(-lambda * tau_near).
Signal S(kappa, hz): near YES bid >= kappa * q, tau_near in [1, 60] d, decision NO price 1 - bid in [0.50, 0.97).
Fill: taker NO at 1 - yes_bid of the last candle <= t + 1 h (<= CF old), limit = decision NO price + 5c (a worse quote is
not filled); if the market closed within that hour (the event happened) the fill is the decision quote, so losing
trades are never dropped. Band membership always uses the decision quote, never the fill quote.
Fee 0.07 p (1 - p) per contract, rounded up to the cent on a 6-contract order. First entry per market; hold to settlement.
Band B (r4 census, pre-registered): NO 0.70-0.97 at D in {60,45,30,21,14} days before the rung deadline, first entry
per market, evaluated on exactly the markets that can be near rungs (the same markets as S).
Encompassing test: incremental = mean ret S - mean ret B on the same market set; plus, within B entries, B&S vs B&notS
(the signal state at B's own entry snapshot). Kill if the discovery increment is < +5%.
Split: events ordered by their latest rung close; discovery = first 70%, validation = last 30%; t clustered by event.
Result (2026-10-08, 33 ladders, 183 Kalshi calls): DEAD. The discovery increment over band B comes entirely from SpaceX
Starship ladders (political/policy ladders: the signal loses); on validation two of three frozen candidates lose to B
(-9%, -7%) and the pre-specified primary (k 1.5, ls, NO 0.50-0.97) beats it by +6.9% on 9 trades with no losses (noise).
Band B itself is the r3 long-dated 'nothing happens' NO factor (+17.5% in a validation regime where every NO won).
Runner: .venv/bin/python -m lab.kalshi.strategies.r4_nested_deadline_term_structure_run [disc|val|robust]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_nested_deadline_term_structure_api import OUT
from lab.kalshi.strategies.r4_nested_deadline_term_structure_data import MF, EF, NFAR, load_candles, cached_market_candles, merge
from lab.kalshi.strategies.r4_nested_deadline_term_structure_parse import thin

DAY, H = 86400, 3600
CF = 72                  # hours a quote may be carried forward
SPLIT = 0.7
DGRID_B = (60, 45, 30, 21, 14)
SNAP_H = 14              # 14:00 UTC
KAPPAS = (1.25, 1.5, 2.0)
HAZARDS = ("ls", "next")
NO_LO, NO_HI = 0.50, 0.97
MAX_SPREAD_FAR = 0.15
LIMIT_SLIP = 0.05        # limit = decision NO price + 5c; a worse t + 1 h quote is not filled
FAR_MAX_D = 365          # far rungs more than a year out are not used for the hazard


def fee(p: float, n: int = 6) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def quote(c: list, t: int, cf: int = CF):
    lo, hi = 0, len(c)
    while lo < hi:                      # last row with ts <= t
        m = (lo + hi) // 2
        if c[m][0] <= t:
            lo = m + 1
        else:
            hi = m
    if lo == 0:
        return None
    r = c[lo - 1]
    if t - r[0] > cf * H or r[1] is None or r[2] is None:
        return None
    return r[1], r[2]


def load():
    M = {json.loads(l)["t"]: json.loads(l) for l in MF.open()}
    E = json.loads(EF.read_text())
    C = cached_market_candles(M)
    for t, rows in load_candles().items():
        C[t] = merge(C.get(t, []), rows)
    return M, E, C


def snapshots(E: dict, M: dict, C: dict) -> list[dict]:
    """One row per (event, daily snapshot) with the near rung, its quotes, the far rungs and the implied q."""
    out = []
    for e, v in E.items():
        rs_all = thin([M[t] for t in v["rungs"]])
        rs = [r for r in rs_all if r["result"] in ("yes", "no")]
        if len(rs_all) < 2 or not rs:
            continue
        t0 = min(r["open"] for r in rs_all); t1 = max(min(r["close"], r["D"]) for r in rs_all)
        day0 = (t0 // DAY + 1) * DAY + SNAP_H * H
        for t in range(day0, t1, DAY):
            vis = sorted([r for r in rs_all if r["open"] <= t < min(r["close"], r["D"]) and r["D"] - t >= DAY], key=lambda r: r["D"])
            if len(vis) < 2:
                continue
            near = vis[0]
            if near["result"] not in ("yes", "no"):
                continue
            qn = quote(C.get(near["t"], []), t)
            if not qn:
                continue
            far = []
            for r in [r for r in vis[1:] if r["D"] - t <= FAR_MAX_D * DAY][:NFAR]:
                q = quote(C.get(r["t"], []), t)
                if q and q[0] - q[1] <= MAX_SPREAD_FAR and 0 < q[0] <= 1 and q[1] >= 0:
                    mid = min(max((q[0] + q[1]) / 2, 0.005), 0.995)
                    far.append(((r["D"] - t) / DAY, mid, r["t"]))
            tau = (near["D"] - t) / DAY
            row = {"e": e, "series": v["series"], "t": near["t"], "ts": t, "tau": tau, "ask": qn[0], "bid": qn[1],
                   "far": far, "result": near["result"]}
            if far:
                ys = [(-math.log(1 - m), tj) for tj, m, _ in far]
                lam_ls = sum(y * tj for y, tj in ys) / sum(tj * tj for _, tj in ys)
                lam_nx = ys[0][0] / ys[0][1]
                row["q_ls"] = 1 - math.exp(-lam_ls * tau); row["q_next"] = 1 - math.exp(-lam_nx * tau)
                row["far_mid_next"] = far[0][1]
            fq = quote(C.get(near["t"], []), t + H)
            if near["close"] <= t + H:
                # closed within the hour after the decision (the event happened): an order sent at t would have been
                # filled near the decision quote, so fill there instead of dropping the trade (no survivorship).
                row["fill_ok"] = True; row["px"] = 1 - qn[1]
            else:
                row["fill_ok"] = bool(fq)
                row["px"] = 1 - fq[1] if fq else None
            out.append(row)
    return out


def trade(row: dict, rule: str) -> dict:
    p = row["px"]; won = row["result"] == "no"
    pnl = (1.0 if won else 0.0) - p - fee(p)
    return {"rule": rule, "e": row["e"], "series": row["series"], "t": row["t"], "ts": row["ts"], "tau": row["tau"],
            "bid": row["bid"], "q": row.get("q_ls"), "q_next": row.get("q_next"), "px": p, "won": won, "pnl": pnl, "ret": pnl / p}


def signal(row: dict, kappa: float, hz: str) -> bool:
    q = row.get(f"q_{hz}")
    return q is not None and 1 <= row["tau"] <= 60 and row["bid"] >= kappa * q and row["bid"] > 0


def fillable(r: dict) -> bool:
    """Taker NO with a limit at the decision NO price + LIMIT_SLIP: filled at the t + 1 h quote if not worse than that."""
    return r["fill_ok"] and r["px"] is not None and r["px"] <= (1 - r["bid"]) + LIMIT_SLIP and r["px"] < 0.995


def rule_S(snaps: list[dict], kappa: float, hz: str, lo: float = None, hi: float = None) -> list[dict]:
    lo = NO_LO if lo is None else lo; hi = NO_HI if hi is None else hi
    first = {}
    for r in sorted(snaps, key=lambda r: r["ts"]):
        if r["t"] in first or not (lo <= 1 - r["bid"] < hi) or not fillable(r):
            continue
        if signal(r, kappa, hz):
            first[r["t"]] = trade(r, f"S k{kappa} {hz}")
    return list(first.values())


def market_snaps(E: dict, M: dict, C: dict, near: list[dict]) -> list[dict]:
    """Daily 14:00 UTC rows for every settled rung of every thinned ladder, near or not (band B is a per-market rule).
    A row that is also the near-rung snapshot carries its q values (so the signal state is known at B's entry)."""
    nr = {(r["t"], r["ts"]): r for r in near}
    out = []
    for e, v in E.items():
        rs_all = thin([M[t] for t in v["rungs"]])
        if len(rs_all) < 2:
            continue
        for m in rs_all:
            if m["result"] not in ("yes", "no"):
                continue
            c = C.get(m["t"], [])
            day0 = (m["open"] // DAY + 1) * DAY + SNAP_H * H
            for t in range(day0, min(m["close"], m["D"]), DAY):
                if m["D"] - t < DAY:
                    continue
                if (m["t"], t) in nr:
                    out.append(nr[(m["t"], t)]); continue
                qn = quote(c, t)
                if not qn:
                    continue
                row = {"e": e, "series": v["series"], "t": m["t"], "ts": t, "tau": (m["D"] - t) / DAY, "ask": qn[0], "bid": qn[1],
                       "far": [], "result": m["result"], "not_near": True}
                fq = quote(c, t + H)
                if m["close"] <= t + H:
                    row["fill_ok"] = True; row["px"] = 1 - qn[1]
                else:
                    row["fill_ok"] = bool(fq); row["px"] = 1 - fq[1] if fq else None
                out.append(row)
    return out


def rule_B(msnaps: list[dict], markets: set[str]) -> list[dict]:
    """Band B on the given markets (any rung position): the daily snapshot with tau in (D - 1, D] for D in the grid
    (largest D first); entry if the decision NO price 1 - bid is in [0.70, 0.97) and the order fills; first entry per
    market."""
    by = defaultdict(list)
    for r in msnaps:
        if r["t"] in markets:
            by[r["t"]].append(r)
    out = []
    for t, rows in by.items():
        rows.sort(key=lambda r: r["ts"])
        for D in DGRID_B:
            hit = next((r for r in rows if D - 1 < r["tau"] <= D), None)
            if hit and 0.70 <= 1 - hit["bid"] < 0.97 and fillable(hit):
                x = trade(hit, "B"); x["D"] = D; x["row"] = hit; out.append(x); break
    return out


def stats(rows: list[dict], key: str = "e") -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for x in rows:
        ev[x[key]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(len(em))) if len(em) > 2 and sd > 0 else float("nan")
    rs = sorted((x["ret"] for x in rows), reverse=True)
    return {"n": len(rows), "events": len(em), "win": sum(x["won"] for x in rows) / len(rows), "avg_px": st.mean(x["px"] for x in rows),
            "ret_per_dollar": st.mean(x["ret"] for x in rows), "ev_mean": st.mean(em), "t": t,
            "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan")}


def fmt(k: str, v: dict) -> str:
    if not v.get("n"):
        return f"  {k:40s} n=0"
    return (f"  {k:40s} n={v['n']:3d} ev={v['events']:3d} win={v['win']:.0%} px={v['avg_px']:.3f} ret={v['ret_per_dollar']:+.1%} "
            f"evmean={v['ev_mean']:+.1%} t={v['t']:5.2f} wo3={v['ret_wo3']:+.1%}")


def split(E: dict, M: dict):
    ec = {}
    for e, v in E.items():
        cl = [min(M[t]["close"], M[t]["D"]) for t in v["rungs"] if M[t]["result"] in ("yes", "no")]
        if cl:
            ec[e] = max(cl)
    order = sorted(ec, key=lambda e: ec[e])
    return ec, order


if __name__ == "__main__":
    print("see r4_nested_deadline_term_structure_run.py")
