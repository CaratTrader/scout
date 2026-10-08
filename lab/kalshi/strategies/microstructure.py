"""microstructure: do short-horizon Kalshi price dynamics (big 1-10 minute moves, volume spikes, stale quotes next to moving
related markets, complementary-market lag, last-minute drift) predict the settlement beyond the price a slow taker pays?

Pre-specified rule families (written before any outcome was looked at; parameters are the full grid, chosen on discovery):
  A  MOVE(k, X, dir)   mid moved >= X over the last k minutes (k 1/2/5/10, X .05/.10/.20); dir MOM buys the side that
                       rose, REV the side that fell. Candle at t required (something happened), spread at t <= 0.10.
  B  VOL(V, dir)       minute volume >= V x max(1, median of the previous 15 minutes) (V 5/20) with |1-min mid move| >= .02.
  C1 XLAG(k, Y)        15-min crypto/commodity windows: the median k-minute move of the OTHER coins of the same window
                       (gold<->silver for commodities) >= Y while this market moved < Y/4 the same way: buy the lagging side.
  C2 STALE(k, Y)       ladder events (cryptoH, indexH, tempH, daily_commod): both neighbouring strikes moved >= Y the same
                       way over k minutes while this strike's quote did not change at all: buy it in the neighbours' direction.
  C3 COMP(k, Y)        2-outcome structured events (sports, tennis, esports): market A moved >= Y over k minutes, its
                       complement B moved < Y/4 the mirror way: buy B's lagging side. Plus the riskless check
                       ask_A + ask_B + fees < 1 or (1-bid_A) + (1-bid_B) + fees < 1 at the fill minute.
  D  DRIFT(tau, X, dir) fixed-close families: at tau = 2/5 minutes before close, the 10-minute mid drift >= X (.05/.15).
  C4 XSERIES(k, Y)     (added with E) cross-series lag: the live 15-minute BTC/ETH market (leader) moved >= Y over k
                       minutes while a near-money strike (mid .15-.85) of the hourly KXBTCD/KXETHD ladder moved < Y/4 the
                       same way: buy the strike in the leader's direction. Also S&P (KXINXU) <-> Nasdaq-100 (KXNASDAQ100U)
                       hourly ladders, leader move = median move of its near-money strikes. Plus a 3-way (UEFA) riskless check.
  E  MAKER(S, H, side) (added after A-D were seen to lose as takers, before any maker result): when the spread >= S
                       (.03/.06), rest a bid one cent inside it (YES at bid+.01, or NO at 1-(ask-.01)), live from T+60 s
                       for H minutes (5/30); filled only if the opposite quote traded THROUGH our price (ask_low <= P-.01
                       for a YES bid, bid_high >= P'+.01 for a YES offer) - conservative, and every fill is adversely
                       selected by construction; if the book already crossed our price at T+60 it is a taker fill with fee.
                       Held to settlement. Maker fee 0 on 'quadratic' series, 0.25 x taker x multiplier otherwise.
Execution: decision at the minute T whose candle ended <= T; fill at the quote of the minute ending T + 60 s (YES at the
ask, NO at 1 - bid, carried forward <= 30 min), only before close, side price in [0.03, 0.97]; held to settlement;
fee 0.07 p (1-p) per contract rounded up to the cent per order of floor($5 / p) contracts. One trade per market per rule
per 30 minutes; for 2-market structured events only the alphabetically first market is used for A/B/D (the other is
its mirror). Split per data family: events by close time, discovery = first 70%, validation = last 30%.

Result (2026-10-08): every taker rule (A-D, C1-C4) loses after fees in every family; stale strikes and riskless
two/three-leg sums essentially never survive one minute. The one survivor is a MAKER rule on KXRAIN: E|rain|S0.06|H30|NO
(rest a NO bid 1c above the best NO bid when the spread is >= 6c, trade-through fills, hold): discovery +12.8%/$ (t 1.6),
validation +46.6% (n 159, 13 days, t 2.57 < K bar 3.67), pre-registered extra holdouts on earlier dates +36.3%
(08-08..08-22, t 2.69) and +23.8% (07-31..08-07, t 2.36); pooled unselected 36 days +33.6%, t 3.80. See result.json.

Usage:
  python -m lab.kalshi.strategies.microstructure build      # all rule trades -> trades.jsonl (split-labelled)
  python -m lab.kalshi.strategies.microstructure discover   # discovery-only cell table, K, candidates
  python -m lab.kalshi.strategies.microstructure validate   # frozen candidates (frozen.json), run once on validation
  python -m lab.kalshi.strategies.microstructure oos_rain   # extra holdout 1 (frozen.json addendum_1), KXRAIN 08-08..08-22
  python -m lab.kalshi.strategies.microstructure oos_rain2  # extra holdout 2 (addendum_2), archived KXRAIN 07-31..08-07
  (archive candles: python -m lab.kalshi.strategies.microstructure_fetch 160)
"""
from __future__ import annotations
import json, math, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import FAMILY
from lab.kalshi.strategies.microstructure_data import load_series, event_close, SPLIT

OUT = Path("data/kalshi_lab/strategies/microstructure")
TRADES = OUT / "trades.jsonl"
PX_LO, PX_HI = 0.03, 0.97
COOLDOWN = 30 * 60
MAX_SPREAD = 0.10
STAKE = 5.0

A_K = (1, 2, 5, 10); A_X = (0.05, 0.10, 0.20)
B_V = (5, 20)
C1_K = (1, 2); C1_Y = (0.10, 0.20)
C2_K = (5, 10); C2_Y = (0.05, 0.10)
C3_K = (1, 2); C3_Y = (0.05, 0.10)
D_TAU = (2, 5); D_X = (0.05, 0.15)
E_S = (0.03, 0.06); E_H = (5, 30)
C4_K = (1, 2); C4_Y = (0.10, 0.20)
LADDER_FAMS = ("cryptoH", "indexH", "tempH", "daily_commod")
STRUCT_FAMS = ("sports", "tennis", "esports")
CLOSE_FAMS = ("crypto15", "commod15", "cryptoH", "indexH", "tempH", "rain", "daily_commod")


def fee_order(p: float, n: int) -> float:
    """Taker fee per contract for an n-contract order, rounded up to the cent per order."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def fill(g, i: int, side: str, rule: str, extra: dict | None = None) -> dict | None:
    """Taker fill one minute after the decision index i (quote of grid index i+1)."""
    j = i + 1
    if j >= g.n:
        return None
    a, b = g.ask[j], g.bid[j]
    if a is None or b is None:
        return None
    px = a if side == "YES" else 1 - b
    px = round(px, 4)
    if not (PX_LO <= px <= PX_HI):
        return None
    won = g.won if side == "YES" else not g.won
    n = max(1, int(STAKE / px))
    pnl = (1.0 if won else 0.0) - px - fee_order(px, n)
    vols = [g.vol[x] for x in range(max(0, j - 2), min(g.n, j + 3))]
    r = {"rule": rule, "fam": g.fam, "s": g.series, "m": g.t, "e": g.e, "t": g.T(i), "side": side, "px": px, "won": won,
         "ret": pnl / px, "vol": st.median(vols) if vols else 0.0}
    if extra:
        r.update(extra)
    return r


def _fee_info() -> dict:
    sa = json.loads(Path("data/kalshi_lab/series_all.json").read_text())
    sa = sa.get("series", sa) if isinstance(sa, dict) else sa
    return {x.get("ticker"): (x.get("fee_type") or "quadratic", float(x.get("fee_multiplier") or 1)) for x in sa}


FEES = None


def maker_fee(series: str, p: float, n: int) -> float:
    global FEES
    if FEES is None:
        FEES = _fee_info()
    ft, mult = FEES.get(series, ("quadratic", 1.0))
    if ft != "quadratic_with_maker_fees":
        return 0.0
    return math.ceil(round(0.25 * 0.07 * mult * p * (1 - p) * n * 100, 6)) / 100 / n


def rules_maker(g) -> list[dict]:
    """E: rest a one-cent-improving bid when the spread is wide; trade-through fills only; hold to settlement."""
    out = []
    last = defaultdict(lambda: -10 ** 12)
    for i in range(0, g.n - 2):
        if not g.fresh[i] or g.mid[i] is None:
            continue
        a, b = g.ask[i], g.bid[i]
        s = a - b; T = g.T(i)
        for S in E_S:
            if s < S - 1e-9:
                continue
            for H in E_H:
                for side in ("YES", "NO"):
                    rule = f"E|{g.fam}|S{S:.2f}|H{H}|{side}"
                    if T - last[rule] < COOLDOWN:
                        continue
                    # side price we pay: YES bid at b+.01; NO bid at 1-(a-.01)
                    P = round(b + 0.01, 4) if side == "YES" else round(1 - (a - 0.01), 4)
                    if not (0.05 <= P <= 0.95):
                        continue
                    last[rule] = T
                    j = i + 1
                    if g.ask[j] is None or g.bid[j] is None:
                        continue
                    cross = g.ask[j] <= P if side == "YES" else (1 - g.bid[j]) <= P
                    filled_px = None; taker = False
                    if cross:
                        filled_px = g.ask[j] if side == "YES" else 1 - g.bid[j]; taker = True
                    else:
                        for x in range(j + 1, min(g.n, j + 1 + H)):
                            if not g.fresh[x]:
                                continue
                            if side == "YES" and g.alow[x] is not None and g.alow[x] <= P - 0.01 + 1e-9:
                                filled_px = P; break
                            if side == "NO" and g.bhigh[x] is not None and (1 - g.bhigh[x]) <= P - 0.01 + 1e-9:
                                filled_px = P; break
                    if filled_px is None or not (0.01 <= filled_px <= 0.99):
                        continue
                    won = g.won if side == "YES" else not g.won
                    n = max(1, int(STAKE / filled_px))
                    f = fee_order(filled_px, n) if taker else maker_fee(g.series, filled_px, n)
                    pnl = (1.0 if won else 0.0) - filled_px - f
                    out.append({"rule": rule, "fam": g.fam, "s": g.series, "m": g.t, "e": g.e, "t": T, "side": side, "px": round(filled_px, 4),
                                "won": won, "ret": pnl / filled_px, "vol": g.vol[j], "taker": taker, "spread": round(s, 3), "mid": round(g.mid[i], 3)})
    return out


def canonical(grids):
    """For 2-market structured events keep only the alphabetically first market (the other is its mirror)."""
    by = defaultdict(list)
    for g in grids:
        by[g.e].append(g)
    keep = []
    for e, gs in by.items():
        if gs[0].fam in STRUCT_FAMS and len(gs) == 2:
            keep.append(min(gs, key=lambda g: g.t))
        else:
            keep.extend(gs)
    return keep


def rules_single(g) -> list[dict]:
    """Families A, B, D on one market."""
    out = []
    last = defaultdict(lambda: -10 ** 12)
    mid, ask, bid, fresh, vol = g.mid, g.ask, g.bid, g.fresh, g.vol
    for i in range(1, g.n - 1):
        if not fresh[i] or mid[i] is None or ask[i] - bid[i] > MAX_SPREAD:
            continue
        T = g.T(i)
        # A: big k-minute moves
        for k in A_K:
            if i - k < 0 or mid[i - k] is None:
                continue
            dm = mid[i] - mid[i - k]
            for X in A_X:
                if abs(dm) < X:
                    continue
                for d in ("MOM", "REV"):
                    rule = f"A|{g.fam}|k{k}|X{X:.2f}|{d}"
                    if T - last[rule] < COOLDOWN:
                        continue
                    side = "YES" if (dm > 0) == (d == "MOM") else "NO"
                    r = fill(g, i, side, rule, {"dm": round(dm, 4)})
                    if r:
                        out.append(r); last[rule] = T
        # B: volume spikes with a price move
        if i >= 15 and mid[i - 1] is not None:
            dm1 = mid[i] - mid[i - 1]
            if abs(dm1) >= 0.02:
                base = max(1.0, st.median(vol[i - 15:i]))
                for V in B_V:
                    if vol[i] < V * base:
                        continue
                    for d in ("MOM", "REV"):
                        rule = f"B|{g.fam}|V{V}|{d}"
                        if T - last[rule] < COOLDOWN:
                            continue
                        side = "YES" if (dm1 > 0) == (d == "MOM") else "NO"
                        r = fill(g, i, side, rule, {"dm": round(dm1, 4), "vratio": round(vol[i] / base, 1)})
                        if r:
                            out.append(r); last[rule] = T
    # D: last-minute drift (fixed-close families only); decision exactly tau minutes before close
    if g.fam in CLOSE_FAMS:
        for tau in D_TAU:
            T = g.close - tau * 60
            i = (T - g.g0) // 60
            if i < 10 or i + 1 >= g.n or (T - g.g0) % 60:
                continue
            if mid[i] is None or mid[i - 10] is None or ask[i] - bid[i] > MAX_SPREAD:
                continue
            dm = mid[i] - mid[i - 10]
            for X in D_X:
                if abs(dm) < X:
                    continue
                for d in ("MOM", "REV"):
                    side = "YES" if (dm > 0) == (d == "MOM") else "NO"
                    r = fill(g, i, side, f"D|{g.fam}|tau{tau}|X{X:.2f}|{d}", {"dm": round(dm, 4)})
                    if r:
                        out.append(r)
    return out


def at(g, T: int) -> int | None:
    i = (T - g.g0) // 60
    return i if 0 <= i < g.n and (T - g.g0) % 60 == 0 else None


def mid_at(g, T: int):
    i = at(g, T)
    return g.mid[i] if i is not None else None


def rules_xlag(grids) -> list[dict]:
    """C1: 15-min crypto/commodity windows, lagging market vs the median move of its peers."""
    out = []
    groups = defaultdict(list)
    for g in grids:
        if g.fam in ("crypto15", "commod15"):
            groups[(g.fam, g.close)].append(g)
    for (fam, close), gs in groups.items():
        if fam == "commod15":
            gs = [g for g in gs if g.series in ("KXGOLD15M", "KXSILVER15M")]
        if len(gs) < 2:
            continue
        last = defaultdict(lambda: -10 ** 12)
        t0 = min(g.g0 for g in gs); t1 = max(g.T(g.n - 1) for g in gs)
        for T in range(t0, t1 + 1, 60):
            for k in C1_K:
                dms = {}
                for g in gs:
                    a, b = mid_at(g, T), mid_at(g, T - 60 * k)
                    if a is not None and b is not None:
                        dms[g.t] = a - b
                for g in gs:
                    if g.t not in dms:
                        continue
                    i = at(g, T)
                    if g.ask[i] - g.bid[i] > MAX_SPREAD:
                        continue
                    peers = [v for t, v in dms.items() if t != g.t]
                    if not peers:
                        continue
                    pm = st.median(peers); own = dms[g.t]
                    sgn = 1 if pm > 0 else -1
                    for Y in C1_Y:
                        if abs(pm) < Y or own * sgn >= Y / 4:
                            continue
                        rule = f"C1|{fam}|k{k}|Y{Y:.2f}"
                        key = (rule, g.t)
                        if T - last[key] < COOLDOWN:
                            continue
                        r = fill(g, i, "YES" if pm > 0 else "NO", rule, {"dm": round(own, 4), "peer": round(pm, 4)})
                        if r:
                            out.append(r); last[key] = T
    return out


def rules_stale(grids) -> list[dict]:
    """C2: ladder strike whose quote did not change while both neighbouring strikes moved the same way."""
    out = []
    by = defaultdict(list)
    for g in grids:
        if g.fam in LADDER_FAMS and g.type in ("greater", "greater_or_equal") and g.floor is not None:
            by[g.e].append(g)
    for e, gs in by.items():
        gs.sort(key=lambda g: g.floor)
        if len(gs) < 3:
            continue
        last = defaultdict(lambda: -10 ** 12)
        t0 = min(g.g0 for g in gs); t1 = max(g.T(g.n - 1) for g in gs)
        for T in range(t0, t1 + 1, 60):
            for k in C2_K:
                Tk = T - 60 * k
                mv = []
                for g in gs:
                    a, b = mid_at(g, T), mid_at(g, Tk)
                    mv.append(a - b if a is not None and b is not None else None)
                for j in range(1, len(gs) - 1):
                    d1, d2 = mv[j - 1], mv[j + 1]
                    if d1 is None or d2 is None or d1 * d2 <= 0:
                        continue
                    g = gs[j]
                    i, i0 = at(g, T), at(g, Tk)
                    if i is None or i0 is None or g.mid[i] is None or not (0.05 < g.mid[i] < 0.95):
                        continue
                    if g.ask[i] != g.ask[i0] or g.bid[i] != g.bid[i0] or any(g.fresh[x] for x in range(i0 + 1, i + 1)):
                        continue
                    if g.ask[i] - g.bid[i] > MAX_SPREAD:
                        continue
                    mn = min(abs(d1), abs(d2))
                    for Y in C2_Y:
                        if mn < Y:
                            continue
                        rule = f"C2|{g.fam}|k{k}|Y{Y:.2f}"
                        key = (rule, g.t)
                        if T - last[key] < COOLDOWN:
                            continue
                        r = fill(g, i, "YES" if d1 > 0 else "NO", rule, {"dm": 0.0, "peer": round((d1 + d2) / 2, 4)})
                        if r:
                            out.append(r); last[key] = T
    return out


def rules_comp(grids) -> list[dict]:
    """C3: 2-outcome structured events; complement lag and riskless two-leg check."""
    out = []
    by = defaultdict(list)
    for g in grids:
        if g.fam in STRUCT_FAMS:
            by[g.e].append(g)
    for e, gs in by.items():
        if len(gs) != 2:
            continue
        A, B = sorted(gs, key=lambda g: g.t)
        last = defaultdict(lambda: -10 ** 12)
        t0 = max(A.g0, B.g0); t1 = min(A.T(A.n - 1), B.T(B.n - 1))
        for T in range(t0, t1 + 1, 60):
            ia, ib = at(A, T), at(B, T)
            if ia is None or ib is None:
                continue
            # riskless two-leg check at the decision minute, executed at the next minute's quotes
            if ia + 1 < A.n and ib + 1 < B.n:
                qa, qb = (A.ask[ia], A.bid[ia]), (B.ask[ib], B.bid[ib])
                fa, fb = (A.ask[ia + 1], A.bid[ia + 1]), (B.ask[ib + 1], B.bid[ib + 1])
                if None not in qa + qb + fa + fb:
                    for leg, pa, pb, pa1, pb1 in (("YY", qa[0], qb[0], fa[0], fb[0]), ("NN", 1 - qa[1], 1 - qb[1], 1 - fa[1], 1 - fb[1])):
                        if 0 < pa < 1 and 0 < pb < 1 and pa + pb + 0.07 * (pa * (1 - pa) + pb * (1 - pb)) < 0.995:
                            key = ("ARB" + leg, e)
                            if T - last[key] >= COOLDOWN and 0 < pa1 < 1 and 0 < pb1 < 1:
                                n = max(1, int(STAKE / max(pa1, pb1)))
                                cost = pa1 + pb1 + fee_order(pa1, n) + fee_order(pb1, n)
                                out.append({"rule": f"C3|{A.fam}|ARB{leg}", "fam": A.fam, "s": A.series, "m": A.t, "e": e, "t": T, "side": leg,
                                            "px": round(pa1 + pb1, 4), "won": True, "ret": (1 - cost) / (pa1 + pb1), "vol": min(A.vol[ia + 1], B.vol[ib + 1]),
                                            "edge_at_t": round(1 - pa - pb, 4)})
                                last[key] = T
            for k in C3_K:
                ma, ma0, mb, mb0 = mid_at(A, T), mid_at(A, T - 60 * k), mid_at(B, T), mid_at(B, T - 60 * k)
                if None in (ma, ma0, mb, mb0):
                    continue
                for lead, lag, dl, dg, il in ((A, B, ma - ma0, mb - mb0, ib), (B, A, mb - mb0, ma - ma0, ia)):
                    if not lead.fresh[at(lead, T)]:
                        continue
                    if lag.ask[il] - lag.bid[il] > MAX_SPREAD:
                        continue
                    sgn = 1 if dl > 0 else -1
                    for Y in C3_Y:
                        # the lagging complement should have moved by -dl; it moved less than Y/4 that way
                        if abs(dl) < Y or (-dg) * sgn >= Y / 4:
                            continue
                        rule = f"C3|{lead.fam}|k{k}|Y{Y:.2f}"
                        key = (rule, e)
                        if T - last[key] < COOLDOWN:
                            continue
                        r = fill(lag, il, "NO" if dl > 0 else "YES", rule, {"dm": round(dg, 4), "peer": round(dl, 4)})
                        if r:
                            out.append(r); last[key] = T
    return out


def _ladder_move(gs, T: int, k: int):
    v = []
    for g in gs:
        a, b = mid_at(g, T), mid_at(g, T - 60 * k)
        if a is not None and b is not None and 0.2 <= a <= 0.8:
            v.append(a - b)
    return st.median(v) if v else None


def rules_xseries(lead_grids, follow_grids, fam: str, mode: str) -> list[dict]:
    """C4: leader (15-min market, or the other index ladder) moved; near-money follower strikes lagged."""
    out = []
    fev = defaultdict(list)
    for g in follow_grids:
        fev[g.e].append(g)
    if mode == "15m":
        lead_by_close = {g.close: g for g in lead_grids}
    else:
        lev = defaultdict(list)
        for g in lead_grids:
            lev[g.close].append(g)
    for e, gs in fev.items():
        last = defaultdict(lambda: -10 ** 12)
        t0 = min(g.g0 for g in gs); t1 = max(g.T(g.n - 1) for g in gs)
        for T in range(t0, t1 + 1, 60):
            for k in C4_K:
                if mode == "15m":
                    c = ((T // 900) + 1) * 900          # the 15-minute market live at T closes at the next quarter hour
                    L = lead_by_close.get(c if c > T else c + 900)
                    if L is None or T - 60 * k < L.close - 900:
                        continue
                    i = at(L, T)
                    if i is None or not L.fresh[i]:
                        continue
                    a, b = mid_at(L, T), mid_at(L, T - 60 * k)
                    if a is None or b is None:
                        continue
                    lm = a - b
                else:
                    lgs = lev.get(gs[0].close)
                    if not lgs:
                        continue
                    lm = _ladder_move(lgs, T, k)
                    if lm is None:
                        continue
                sgn = 1 if lm > 0 else -1
                for g in gs:
                    i = at(g, T)
                    if i is None or g.mid[i] is None or not (0.15 <= g.mid[i] <= 0.85) or g.ask[i] - g.bid[i] > MAX_SPREAD:
                        continue
                    b0 = mid_at(g, T - 60 * k)
                    if b0 is None:
                        continue
                    own = g.mid[i] - b0
                    for Y in C4_Y:
                        if abs(lm) < Y or own * sgn >= Y / 4:
                            continue
                        rule = f"C4|{fam}|{mode}|k{k}|Y{Y:.2f}"
                        key = (rule, g.t)
                        if T - last[key] < COOLDOWN:
                            continue
                        r = fill(g, i, "YES" if lm > 0 else "NO", rule, {"dm": round(own, 4), "peer": round(lm, 4)})
                        if r:
                            out.append(r); last[key] = T
    return out


def rules_arb3(grids) -> list[dict]:
    """3-outcome events (UEFA home/tie/away): sum of YES asks + fees < 1, or sum of NO costs + fees < 2."""
    out = []
    by = defaultdict(list)
    for g in grids:
        by[g.e].append(g)
    for e, gs in by.items():
        if len(gs) != 3:
            continue
        last = -10 ** 12
        t0 = max(g.g0 for g in gs); t1 = min(g.T(g.n - 1) for g in gs)
        for T in range(t0, t1 + 1, 60):
            idx = [at(g, T) for g in gs]
            if None in idx or any(i + 1 >= g.n for i, g in zip(idx, gs)):
                continue
            q = [(g.ask[i], g.bid[i]) for i, g in zip(idx, gs)]
            f = [(g.ask[i + 1], g.bid[i + 1]) for i, g in zip(idx, gs)]
            if any(None in x for x in q + f):
                continue
            for leg, cost_t, cost_f, need in (("YYY", [x[0] for x in q], [x[0] for x in f], 1.0), ("NNN", [1 - x[1] for x in q], [1 - x[1] for x in f], 2.0)):
                if not all(0 < c < 1 for c in cost_t + cost_f):
                    continue
                if sum(cost_t) + sum(0.07 * c * (1 - c) for c in cost_t) < need - 0.005 and T - last >= COOLDOWN:
                    n = max(1, int(STAKE / max(cost_f)))
                    tot = sum(cost_f) + sum(fee_order(c, n) for c in cost_f)
                    out.append({"rule": f"C3|sports|ARB{leg}", "fam": "sports", "s": gs[0].series, "m": gs[0].t, "e": e, "t": T, "side": leg,
                                "px": round(sum(cost_f), 4), "won": True, "ret": (need - tot) / sum(cost_f), "vol": 0.0, "edge_at_t": round(need - sum(cost_t), 4)})
                    last = T
    return out


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fams = defaultdict(list)
    for s, f in FAMILY.items():
        fams[f].append(s)
    ev_close_all = {}; cut = {}
    fh = TRADES.open("w"); total = 0
    for fam, series in fams.items():
        t0 = time.time()
        grids = []
        for s in series:
            grids.extend(load_series(s))
        ec = event_close(grids)
        closes = sorted(ec.values())
        cut[fam] = closes[int(len(closes) * SPLIT)] if closes else 0
        rows = []
        for g in canonical(grids):
            rows.extend(rules_single(g))
            rows.extend(rules_maker(g))
        rows.extend(rules_xlag(grids))
        rows.extend(rules_stale(grids))
        rows.extend(rules_comp(grids))
        if fam == "sports":
            rows.extend(rules_arb3([g for g in grids if g.series == "KXUEFANLGAME"]))
        if fam == "cryptoH":
            for lead_s, fol_s in (("KXBTC15M", "KXBTCD"), ("KXETH15M", "KXETHD")):
                rows.extend(rules_xseries(load_series(lead_s), [g for g in grids if g.series == fol_s], fam, "15m"))
        if fam == "indexH":
            a = [g for g in grids if g.series == "KXINXU"]; b = [g for g in grids if g.series == "KXNASDAQ100U"]
            rows.extend(rules_xseries(a, b, fam, "spx>ndx"))
            rows.extend(rules_xseries(b, a, fam, "ndx>spx"))
        for r in rows:
            r["ec"] = ec[r["e"]]; r["split"] = "disc" if ec[r["e"]] < cut[fam] else "val"
            fh.write(json.dumps(r) + "\n")
        total += len(rows)
        print(f"{fam:12s} markets {len(grids):6d} events {len(ec):5d} cutoff {time.strftime('%Y-%m-%d %H:%M', time.gmtime(cut[fam]))} rows {len(rows):7d} ({time.time() - t0:.0f}s)", flush=True)
        del grids
    fh.close()
    (OUT / "cutoffs.json").write_text(json.dumps(cut, indent=1))
    print("total rows", total)


def stats(rows: list[dict]) -> dict:
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em); mean = st.mean(r["ret"] for r in rows)
    sd = st.pstdev(em) if n_e > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(n_e)) if n_e > 2 and sd > 0 else float("nan")
    # clustered t of the trade-weighted mean (event sums of deviations), the statistic matching `mean`
    dev = [sum(x - mean for x in v) for v in ev.values()]
    se = math.sqrt(sum(d * d for d in dev)) / len(rows) if rows else float("nan")
    tc = mean / se if se and se > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "px": st.mean(r["px"] for r in rows),
            "ret": mean, "t": tc, "t_evmean": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan")}


def load_rows(split: str) -> list[dict]:
    import gzip
    fh = TRADES.open() if TRADES.exists() else gzip.open(str(TRADES) + ".gz", "rt")   # build output is gzipped after the run
    return [r for r in map(json.loads, fh) if r["split"] == split]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()


def discover(min_n: int = 30) -> None:
    rows = load_rows("disc")
    cells = defaultdict(list)
    for r in rows:
        cells[r["rule"]].append(r)
    allrules = sorted(cells)
    res = {k: stats(v) for k, v in cells.items() if len(v) >= min_n and len({r["e"] for r in v}) >= 10}
    K = len(res); z = NormalDist().inv_cdf(1 - 0.05 / max(K, 1))
    print(f"discovery rows {len(rows)}; rules with any trade {len(allrules)}; cells n>={min_n} & >=10 events: K={K}; Bonferroni bar t>={z:.2f}")
    fmt = lambda k, v: (f"  {k:38s} n={v['n']:6d} ev={v['events']:5d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} "
                        f"t={v['t']:5.2f} tE={v['t_evmean']:5.2f} wo3={v['ret_wo3']:+.1%}")
    print("\nTOP 30 BY RETURN")
    for k, v in sorted(res.items(), key=lambda kv: -kv[1]["ret"])[:30]:
        print(fmt(k, v))
    print("\nTOP 30 BY t")
    for k, v in sorted(res.items(), key=lambda kv: -(kv[1]["t"] if kv[1]["t"] == kv[1]["t"] else -99))[:30]:
        print(fmt(k, v))
    print("\nBOTTOM 15 BY t")
    for k, v in sorted(res.items(), key=lambda kv: (kv[1]["t"] if kv[1]["t"] == kv[1]["t"] else 99))[:15]:
        print(fmt(k, v))
    cand = {k: v for k, v in res.items() if v["ret"] >= 0.10 and v["t"] >= 2.0 and v["ret_wo3"] > 0}
    print(f"\nCANDIDATES (ret>=10%, t>=2, wo3>0): {len(cand)}")
    for k, v in sorted(cand.items(), key=lambda kv: -kv[1]["t"]):
        print(fmt(k, v))
    (OUT / "discovery.json").write_text(json.dumps({"K": K, "z": z, "rules_with_trades": len(allrules), "cells": res}, indent=0, default=str))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "discover":
    discover()


def validate() -> None:
    fz = json.loads((OUT / "frozen.json").read_text())
    disc = json.loads((OUT / "discovery.json").read_text())
    K = disc["K"]; z = NormalDist().inv_cdf(1 - 0.05 / K)
    rows = load_rows("val")
    out = []
    for rule in fz["candidates"]:
        R = sorted((r for r in rows if r["rule"] == rule), key=lambda r: r["t"])
        if not R:
            print(rule, "no validation trades"); out.append({"rule": rule, "n": 0}); continue
        s = stats(R)
        ecs = sorted({r["ec"] for r in R}); mid = ecs[len(ecs) // 2]
        h1 = [r["ret"] for r in R if r["ec"] < mid]; h2 = [r["ret"] for r in R if r["ec"] >= mid]
        days = max(1.0, (R[-1]["t"] - R[0]["t"]) / 86400)
        cap = st.median(r["vol"] for r in R)
        gate = s["n"] >= 40 and s["ret"] >= 0.10 and s["t"] >= z and s["ret_wo3"] > 0 and h1 and h2 and min(st.mean(h1), st.mean(h2)) > 0
        v = {"rule": rule, "n": s["n"], "events": s["events"], "win": round(s["win"], 4), "avg_px": round(s["px"], 4), "ret_per_dollar": round(s["ret"], 4),
             "t": round(s["t"], 2), "t_event_means": round(s["t_evmean"], 2), "ret_wo3": round(s["ret_wo3"], 4),
             "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
             "median_capacity_contracts": round(cap, 1), "trades_per_day": round(len(R) / days, 2), "gate_pass": bool(gate)}
        out.append(v)
        print(json.dumps(v))
    (OUT / "validation.json").write_text(json.dumps({"K": K, "z": z, "results": out}, indent=1))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "validate":
    validate()


def oos_rain(candle_file: str = "data/kalshi_lab/strategies/rain_history/candles.jsonl",
             market_file: str = "data/kalshi_lab/strategies/rain_history/markets.jsonl", out_name: str = "oos_rain.json",
             addendum: str = "addendum_1", whole_dates: bool = False) -> list[dict]:
    """Pre-registered extra out-of-sample test (frozen.json addendum_1): the frozen rain rules on KXRAIN events that closed
    before my discovery data starts. Candles are read-only from another researcher's cache (same compact format)."""
    from lab.kalshi.strategies.microstructure_data import Grid
    from lab.kalshi.fetch import PLAN, window
    fz = json.loads((OUT / "frozen.json").read_text())
    rules = fz["addendum_1"]["rules"]   # addendum_2 judges only the first (E|rain|S0.06|H30|NO); the other is shown for reference
    mine = [json.loads(l) for l in open("data/kalshi_lab/markets/KXRAIN.jsonl")]
    have_c = {json.loads(l)["t"] for l in open("data/kalshi_lab/candles/KXRAIN.jsonl")}
    first = min(m["close"] for m in mine if m["t"] in have_c)
    meta = {}
    for l in open(market_file):
        m = json.loads(l)
        if m.get("series") == "KXRAIN" and m.get("result") in ("yes", "no") and m["close"] < first:
            m.setdefault("type", "greater"); meta[m["t"]] = m
    rows = []; days = set(); seen = set()
    if whole_dates:   # only dates whose every market has candles (fetch stopped on whole dates)
        lines = [json.loads(l) for l in open(candle_file)]
        got = {x["t"] for x in lines}
        per = defaultdict(set)
        for t, m in meta.items():
            per[m["e"]].add(t)
        meta = {t: m for t, m in meta.items() if per[m["e"]] <= got}
    for l in open(candle_file):
        x = json.loads(l); m = meta.get(x["t"])
        if not m or not x["c"] or x["t"] in seen:
            continue
        seen.add(x["t"])
        w = window(m, PLAN["KXRAIN"])
        c = [r for r in x["c"] if w[0] <= r[0] <= w[1] + 60]
        if not c:
            continue
        g = Grid(m, "rain", c, w[1])
        if g.n < 2:
            continue
        days.add(g.e)
        rows.extend(r for r in rules_single(g) + rules_maker(g) if r["rule"] in rules)
    print(f"OOS KXRAIN markets {len(seen)} event-days {len(days)} (closing before {time.strftime('%Y-%m-%d', time.gmtime(first))})")
    res = {}
    for rule in rules:
        R = [r for r in rows if r["rule"] == rule]
        if len(R) < 4:
            print(rule, "n", len(R)); res[rule] = {"n": len(R)}; continue
        s = stats(R)
        es = sorted({r["e"] for r in R}); mid = es[len(es) // 2]
        h1 = [r["ret"] for r in R if r["e"] < mid]; h2 = [r["ret"] for r in R if r["e"] >= mid]
        s["half1"] = st.mean(h1) if h1 else None; s["half2"] = st.mean(h2) if h2 else None
        s["days"] = sorted(es)
        res[rule] = s
        print(rule, json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items() if k != "days"}))
    (OUT / out_name).write_text(json.dumps(res, indent=1, default=str))
    return rows


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "oos_rain":
    # whole dates only: rain_history later added archive markets selected by its METAR trigger (dry afternoons), which
    # would bias this test toward NO; at the time of the addendum_1 run its cache held only whole dates 08-08..08-22
    oos_rain(whole_dates=True)
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "oos_rain2":
    oos_rain("data/kalshi_lab/strategies/microstructure/arch_candles.jsonl", "data/kalshi_lab/strategies/rain_history/markets.jsonl",
             "oos_rain2.json", "addendum_2", whole_dates=True)
