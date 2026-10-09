"""Simulation core for r4_long_dated_maker_no: virtual resting NO bids (sell YES at yes_ask - 1c) on long-dated
markets, judged on hourly candles (trade-price high) and, for an audited subset, on trade prints.

Timeline of one signal (market m, D days before the scheduled deadline S):
  t      = S - D days (00:00 UTC). Signal quote: last hourly candle ending <= t, age <= MAXAGE_H. Band on the NO taker
           price 1 - yes_bid (same selection as the round-3 taker rule).
  t_post = t + 1 h. Order quote: last candle ending <= t_post. Maker price s = yes_ask - 0.01 (NO bid at 1 - s),
           only if yes_ask - yes_bid >= 2c (else the order would cross: no maker order, signal counted as 'no_spread').
  Fill   : 'through' = some candle ending in (t_post, min(t_post + W, close, S)] has trade-price high > s (strictly through
           our price: the whole order was consumed first by price-time priority); 'any' = high >= s.
           Daily-refresh variant: each day k < W the order is re-priced at that day's quote (ask - 1c, spread >= 2c, NO
           taker price still in band, else it rests nowhere that day).
  Taker comparator on the same signal: NO at 1 - yes_bid of the t_post quote, taker fee.
Fees (current series schedule, per order of N = floor($5 / price) contracts, rounded up to the cent):
  taker 0.07 * mult * p (1 - p); maker 0 for 'quadratic', 0.25 x the taker formula for 'quadratic_with_maker_fees'."""
from __future__ import annotations
import math, statistics as st
from collections import defaultdict

DAY = 86400
MAXAGE_H = 72.0
STAKE = 5.0


def quote(c: list, t: int, maxage_h: float = MAXAGE_H):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > maxage_h * 3600:
        return None
    return best


def n_contracts(p: float) -> int:
    return max(1, int(STAKE // p)) if p > 0 else 1


def fee_pc(p: float, kind: str, ftype: str, mult: float, n: int) -> float:
    if kind == "maker":
        if ftype != "quadratic_with_maker_fees":
            return 0.0
        rate = 0.25 * 0.07 * mult
    else:
        rate = 0.07 * mult
    if rate <= 0:
        return 0.0
    return math.ceil(round(rate * n * p * (1 - p) * 100, 9)) / 100 / n


def ret_of(won: bool, p: float, fee: float) -> float:
    return ((1.0 if won else 0.0) - p - fee) / p


def window_fill(c: list, lo: int, hi: int, s: float, mode: str):
    """First candle ending in (lo, hi] whose trade-price high is through (> s) or at-or-through (>= s) the order."""
    for r in c:
        if r[0] <= lo:
            continue
        if r[0] > hi:
            break
        ph = r[7]
        if ph is None:
            continue
        if (mode == "through" and ph > s + 1e-9) or (mode == "any" and ph >= s - 1e-9):
            return r
    return None


def vol_through(c: list, lo: int, hi: int, s: float) -> tuple[float, float]:
    """Candle-volume bounds on contracts traded through s in (lo, hi]: (hours whose low > s, hours whose high > s)."""
    a = b = 0.0
    for r in c:
        if r[0] <= lo:
            continue
        if r[0] > hi:
            break
        if r[7] is not None and r[7] > s + 1e-9:
            b += r[10]
            if r[8] is not None and r[8] > s + 1e-9:
                a += r[10]
    return a, b


def simulate(M: dict, C: dict, fees: dict, Ds=(60, 45, 30), Ws=(3, 7, 14), maxage_h=MAXAGE_H, first_hour=True) -> list[dict]:
    """One row per (market, D) with a valid signal quote: taker outcome plus maker outcomes for every W and fill mode."""
    rows = []
    for k, m in M.items():
        c = C.get(k)
        if not c:
            continue
        ftype, mult = fees.get(m["series"], ("quadratic", 1.0))
        won = m["result"] == "no"
        end_all = min(m["close"], m["S"])
        for D in Ds:
            t = m["S"] - D * DAY
            tp = t + 3600
            if t < m["open"] or tp >= m["close"]:
                continue
            qs, qp = quote(c, t, maxage_h), quote(c, tp, maxage_h)
            if not qs or not qp:
                continue
            ps = 1 - qs[2]
            px_t = 1 - qp[2]
            if not (0.01 <= px_t <= 0.99):
                continue
            nt = n_contracts(px_t)
            r = {"t": k, "e": m["e"], "series": m["series"], "D": D, "t_sig": t, "t_post": tp, "close": m["close"], "S": m["S"], "won": won,
                 "ps": round(ps, 4), "ask": qp[1], "bid": qp[2], "spread": round(qp[1] - qp[2], 4), "px_t": round(px_t, 4),
                 "ret_t": ret_of(won, px_t, fee_pc(px_t, "taker", ftype, mult, nt)), "ftype": ftype, "hist": m.get("hist", True), "src": m.get("src")}
            s = round(qp[1] - 0.01, 4) if qp[1] - qp[2] >= 0.02 - 1e-9 else None
            r["s"] = s
            if s is not None:
                pm = round(1 - s, 4); nm = n_contracts(pm)
                r["px_m"] = pm; r["N"] = nm
                r["ret_m"] = ret_of(won, pm, fee_pc(pm, "maker", ftype, mult, nm))
                lo = tp if first_hour else tp + 3600
                for W in Ws:
                    hi = min(tp + W * DAY, end_all)
                    for mode in ("through", "any"):
                        f = window_fill(c, lo, hi, s, mode)
                        r[f"f_{mode}_{W}"] = f[0] if f else None
                    a, b = vol_through(c, lo, hi, s)
                    r[f"vt_lo_{W}"] = a; r[f"vt_hi_{W}"] = b
            # daily refresh (re-priced each day at that day's quote while the NO taker price is in a band set later)
            r["days"] = []
            for d in range(max(Ws)):
                td = tp + d * DAY
                if td >= end_all:
                    break
                q = quote(c, td, maxage_h)
                if not q:
                    r["days"].append(None); continue
                sd = round(q[1] - 0.01, 4) if q[1] - q[2] >= 0.02 - 1e-9 else None
                fd = window_fill(c, td if first_hour else td + 3600, min(td + DAY, end_all), sd, "through") if sd is not None else None
                r["days"].append((round(1 - q[2], 4), sd, fd[0] if fd else None))
            rows.append(r)
    return rows


def refresh_fill(r: dict, lo: float, hi: float, W: int, ftype: str, mult: float):
    """Daily-refresh order: first day k < W on which the day's order (in band, spread >= 2c) traded through."""
    for d, x in enumerate(r["days"][:W]):
        if x is None:
            continue
        ps_d, sd, fd = x
        if sd is None or not (lo <= ps_d < hi + 0.03):
            continue
        if fd is not None:
            pm = round(1 - sd, 4); nm = n_contracts(pm)
            return {"px_m": pm, "ret_m": ret_of(r["won"], pm, fee_pc(pm, "maker", ftype, mult, nm)), "fill_ts": fd, "day": d}
    return None


# ------------------------------------------------------------------------------------------------ statistics
def clopper_lo(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson / Beta) lower bound on a binomial rate."""
    if n == 0 or k == 0:
        return 0.0
    def tail(p):   # P(X >= k | p)
        return 1 - sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k))
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def stats(rows: list[dict], key="ret", px="px", w=None) -> dict:
    """Equal-$ return per trade, t clustered by event (calib.cell_stats), halves by fill time, wo3, size-weighted mean
    (weights w), and amendment (d): exact-binomial lower bound on the per-$ return over unique events (first trade)."""
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r[key])
    em = [st.mean(v) for v in ev.values()]
    ne = len(em); sd = st.pstdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne)) if ne > 2 and sd > 0 else None
    rs = sorted((r[key] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r.get("ts", r["t_post"]))
    h = len(srt) // 2
    first = {}
    for r in srt:
        first.setdefault(r["e"], r)
    fe = list(first.values()); kk = sum(r["won"] for r in fe); pa = st.mean(r[px] for r in fe)
    lo = clopper_lo(kk, len(fe))
    out = {"n": len(rows), "events": ne, "markets": len({r["t"] for r in rows}), "series": len({r["series"] for r in rows}),
           "win": round(st.mean(r["won"] for r in rows), 4), "avg_px": round(st.mean(r[px] for r in rows), 4),
           "ret_per_dollar": round(st.mean(rs), 4), "t": round(t, 2) if t is not None else None,
           "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
           "half1": round(st.mean(r[key] for r in srt[:h]), 4) if h else None, "half2": round(st.mean(r[key] for r in srt[h:]), 4),
           "uniq_events": len(fe), "uniq_losses": len(fe) - kk, "win_lo95_events": round(lo, 4), "ret_at_win_lo95": round((lo - pa) / pa, 4)}
    if w:
        ws = [r[w] for r in rows]
        if sum(ws) > 0:
            out["ret_size_weighted"] = round(sum(r[key] * r[w] for r in rows) / sum(ws), 4)
    return out


def split_cut(rows: list[dict], M: dict, frac: float = 0.7) -> int:
    evc = defaultdict(int)
    for m in M.values():
        evc[m["e"]] = max(evc[m["e"]], m["close"])
    closes = sorted(evc.values())
    return closes[int(len(closes) * frac)] if closes else 0, evc
