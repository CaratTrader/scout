"""r4_single_appearance_seeded_books: the frozen mention-maker rule C1 on single-appearance mention formats that
round 2 (21 series) and round 3 (178 earnings series, sports announcers, accumulators, ladders) never tested, with the
order cancelled BEFORE the scheduled appearance day.

Pre-registration (written before any candle/price was fetched; amendments 1-3 before any return was computed):
data/kalshi_lab/strategies/r4_single_appearance_seeded_books/preregistration_backtest.json.

Cell P (C1S): at the first top of the UTC hour H1 >= open + 1 h, quote = the last candle close at or before H1 (the
candle stream is gap-free: in 6,771 gaps the next candle opened exactly at the previous close, so the book did not move
in a gap; amendment 3), else the first 1-minute candle in (H1, H1 + 12 min]. Skip if any market of the event has
closed, if not 0 < yes_bid < yes_ask < 1, if the spread is < 2c, or if posting + 120 s is not before the cancel.
Rest SELL-YES (= buy NO at 1 - s) at s = yes_ask - 1c for N = floor($5 / (1 - s)) contracts. Cancel at
C = min(00:00 ET on D*, the first close of any market of the event, the market's own close), with D* = min(event-ticker
date, ET date of the earliest expected_expiration_time - 1 day). Hold to settlement; maker fee 0 ('quadratic').
Fills (gate amendment c, through-only): a print strictly above s in a candle wholly after post + 120 s (a candle that
straddles the post time counts only if its high is >= s + 2c) and not after the candle containing C (the hour or minute
containing a first-close cancel is INCLUDED: conservative). Size-weighted counterfactual: filled contracts =
min(N, volume of those candles) - exact /markets/trades prints instead where fetched (live tier). 'Any' = print >= s
or yes_bid high >= s.
Mirror: BUY-YES at b = yes_bid + 1c, same H1 / eligibility / cancel; through = a print strictly below b.
Contrast L (literal r2 C1): identical, cancel at the event's first close only (rests through the appearance start).

Split: sampled events ordered by their last close; discovery = first 70%, validation = last 30%.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_single_appearance_seeded_books orders|need_trades|fetch_trades N|discovery|validate|result
"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_single_appearance_seeded_books_data import (OUT, load_markets, events, GROUP_OF, ARCHIVE_CUTOFF,
                                                                            ts, trades as fetch_prints)

EPS = 1e-9
STAKE = 5.0
SPLIT = 0.7
POST_GAP = 120
ENTRY_LATE = 12 * 60


# ------------------------------------------------------------------------------------------------ loading
def load_candles() -> dict:
    C = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l)
        if x["c"] or x["t"] not in C:
            C[x["t"]] = {"period": x["period"], "c": sorted(x["c"], key=lambda r: r[0])}
    return C


def load_prints() -> dict:
    f = OUT / "trades.jsonl"
    T = {}
    if f.exists():
        for l in f.open():
            x = json.loads(l)
            T[x["t"]] = x
    return T


# ------------------------------------------------------------------------------------------------ order simulation
def quote_at(cs: list, period: int, H1: int, strict30: bool = False):
    """(tq, ask, bid, age_s) or None. Last candle with end <= H1; else first 1-minute candle in (H1, H1 + 12 min]."""
    last = None
    for r in cs:
        if r[0] <= H1:
            last = r
        else:
            break
    if last is not None and last[1] is not None and last[2] is not None:
        age = H1 - last[0]
        if not strict30 or age <= 1800:
            return H1, last[1], last[2], age
    if period == 1:
        for r in cs:
            if H1 < r[0] <= H1 + ENTRY_LATE and r[1] is not None and r[2] is not None:
                return r[0], r[1], r[2], 0
    return None


def scan(cs: list, period: int, lo: float, hi: float, s: float, b: float) -> dict:
    """Fill evidence for a sell-YES at s and a buy-YES at b live in (lo, hi). Candle r covers (r0 - p, r0]."""
    p = 60 * period
    out = {"thr": False, "any": False, "vthr": 0.0, "vany": 0.0, "t_thr": None,
           "m_thr": False, "m_any": False, "m_vthr": 0.0, "m_vany": 0.0, "max_hi": None, "min_lo": None}
    for r in cs:
        T = r[0]
        if T <= lo:
            continue
        if T - p >= hi:
            break                                     # candles wholly after the cancel
        straddle = T - p < lo
        bid_hi, px_lo, px_hi, ask_lo, vol = r[6], r[7], r[8], r[3], r[9] or 0.0
        if px_hi is not None:
            out["max_hi"] = px_hi if out["max_hi"] is None else max(out["max_hi"], px_hi)
        if px_lo is not None:
            out["min_lo"] = px_lo if out["min_lo"] is None else min(out["min_lo"], px_lo)
        # sell-YES at s
        if straddle:
            thr = px_hi is not None and px_hi >= s + 0.02 - EPS
            anyf = thr
        else:
            thr = px_hi is not None and px_hi > s + EPS
            anyf = (px_hi is not None and px_hi >= s - EPS) or (bid_hi is not None and bid_hi > 0 and bid_hi >= s - EPS)
        if thr:
            out["thr"] = True; out["vthr"] += vol; out["t_thr"] = out["t_thr"] or T
        if anyf:
            out["any"] = True; out["vany"] += vol
        # buy-YES at b (mirror)
        if straddle:
            mthr = px_lo is not None and px_lo <= b - 0.02 + EPS
            many = mthr
        else:
            mthr = px_lo is not None and px_lo < b - EPS
            many = (px_lo is not None and px_lo <= b + EPS) or (ask_lo is not None and ask_lo < 1 and ask_lo <= b + EPS)
        if mthr:
            out["m_thr"] = True; out["m_vthr"] += vol
        if many:
            out["m_any"] = True; out["m_vany"] += vol
    return out


def exact(prints: list, lo: float, hi: float, s: float, b: float) -> dict:
    """prints: [ts, yes_price, count, taker_side, is_block]."""
    w = [p for p in prints if lo < p[0] < hi and not p[4]]
    return {"thr": sum(p[2] for p in w if p[1] > s + EPS), "any": sum(p[2] for p in w if p[1] >= s - EPS),
            "m_thr": sum(p[2] for p in w if p[1] < b - EPS), "m_any": sum(p[2] for p in w if p[1] <= b + EPS), "n": len(w)}


def build_orders(strict30: bool = False) -> list[dict]:
    P = json.loads((OUT / "plan.json").read_text())
    el = {x["t"]: x for x in P["eligible"]}
    E = events(); C = load_candles(); TR = load_prints(); M = load_markets()
    out = []
    for e, tks in P["sample"].items():
        ev = E[e]
        for rank, t in enumerate(tks):
            x = el[t]; cd = C.get(t)
            base = {"t": t, "e": e, "series": x["series"], "group": x["group"], "tier": "live" if e in P["live_events"] else "arch",
                    "rank": rank, "H1": x["H1"], "C": x["C"], "C_lit": x["C_lit"], "T_day": x["T_day"], "D": x["D"],
                    "ev_last_close": ev["last_close"], "ev_first_close": ev["first_close"], "yes": M[t]["result"] == "yes"}
            if not cd or not cd["c"]:
                out.append({**base, "posted": False, "reason": "no_candles"}); continue
            q = quote_at(cd["c"], cd["period"], x["H1"], strict30)
            if q is None:
                out.append({**base, "posted": False, "reason": "no_quote"}); continue
            tq, ask, bid, age = q
            reason = None
            if ev["first_close"] <= tq:
                reason = "event_started"
            elif not (bid > 0 and ask < 1 and bid < ask):
                reason = "no_two_sided_quote"
            elif ask - bid < 0.02 - EPS:
                reason = "spread_lt_2c"
            elif tq + POST_GAP >= x["C"]:
                reason = "no_time_before_cancel"
            if reason:
                out.append({**base, "posted": False, "reason": reason, "ask": ask, "bid": bid}); continue
            s = round(ask - 0.01, 2); b = round(bid + 0.01, 2); px = round(1 - s, 2)
            N = max(1, math.floor(STAKE / px)); Nb = max(1, math.floor(STAKE / b))
            lo = tq + POST_GAP
            fP = scan(cd["c"], cd["period"], lo, x["C"], s, b)
            fL = scan(cd["c"], cd["period"], lo, x["C_lit"], s, b)
            o = {**base, "posted": True, "tq": tq, "age": age, "ask": ask, "bid": bid, "spread": round(ask - bid, 2), "s": s, "b": b,
                 "px": px, "N": N, "Nb": Nb, "period": cd["period"], "hours_P": round((x["C"] - tq) / 3600, 2),
                 "hours_L": round((x["C_lit"] - tq) / 3600, 2), "P": fP, "L": fL}
            pr = TR.get(t)
            if pr:
                o["xP"] = exact(pr["trades"], lo, x["C"], s, b)
                o["xL"] = exact(pr["trades"], lo, x["C_lit"], s, b)
                o["prints_capped"] = pr.get("cursor", False)
            out.append(o)
    return out


# ------------------------------------------------------------------------------------------------ scoring
def score(o: dict, arm: str = "P", side: str = "sell", fill: str = "thr") -> dict | None:
    """Per-order outcome for one arm. Returns None if the order did not fill (under 'fill')."""
    f = o[arm]
    x = o.get("x" + arm)
    if side == "sell":
        hit = f["thr"] if fill == "thr" else f["any"]
        if x is not None:                                      # exact prints override the candle evidence
            vol = x["thr"] if fill == "thr" else x["any"]
            hit = vol > 0
        else:
            vol = f["vthr"] if fill == "thr" else f["vany"]
        if not hit:
            return None
        px, N, win = o["px"], o["N"], not o["yes"]
    else:
        hit = f["m_thr"] if fill == "thr" else f["m_any"]
        if x is not None:
            vol = x["m_thr"] if fill == "thr" else x["m_any"]
            hit = vol > 0
        else:
            vol = f["m_vthr"] if fill == "thr" else f["m_vany"]
        if not hit:
            return None
        px, N, win = o["b"], o["Nb"], o["yes"]
    q = min(N, vol) if vol > 0 else 0.0
    return {"e": o["e"], "t": o["t"], "px": px, "won": win, "ret": ((1.0 if win else 0.0) - px) / px, "q": q,
            "pnl": q * ((1.0 if win else 0.0) - px), "cost": q * px, "ev_last_close": o["ev_last_close"], "group": o["group"]}


def binom_lo(k: int, n: int, a: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound on p: P(X >= k | n, p) = a."""
    if n == 0 or k == 0:
        return 0.0
    def tail(p):
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < a:
            lo = mid
        else:
            hi = mid
    return lo


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em)
    sd = st.pstdev(em) if n_e > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(n_e)) if n_e > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    cost = sum(r["cost"] for r in rows); pnl = sum(r["pnl"] for r in rows)
    first = {}
    for r in sorted(rows, key=lambda r: (r["ev_last_close"], r["t"])):
        first.setdefault(r["e"], r)
    fe = list(first.values()); k = sum(r["won"] for r in fe); pxe = st.mean(r["px"] for r in fe)
    wlo = binom_lo(k, len(fe))
    srt = sorted(rows, key=lambda r: r["ev_last_close"])
    mid = srt[len(srt) // 2]["ev_last_close"]
    h1 = [r["ret"] for r in srt if r["ev_last_close"] < mid]; h2 = [r["ret"] for r in srt if r["ev_last_close"] >= mid]
    qs = sorted(r["q"] for r in rows)
    return {"n": len(rows), "events": n_e, "win": round(st.mean(r["won"] for r in rows), 4), "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 3) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "size_weighted_ret": round(pnl / cost, 4) if cost > 0 else None, "sw_contracts": round(sum(r["q"] for r in rows), 1),
            "median_filled_contracts": qs[len(qs) // 2] if qs else None,
            "beta_lo_event_win": round(wlo, 4), "beta_lo_ret": round((wlo - pxe) / pxe, 4), "event_first_px": round(pxe, 4)}


def cell(orders: list[dict], arm: str = "P", side: str = "sell", fill: str = "thr", pred=None) -> dict:
    os_ = [o for o in orders if o["posted"] and (pred is None or pred(o))]
    rows = [r for r in (score(o, arm, side, fill) for o in os_) if r]
    s = stats(rows)
    filled = {r["t"] for r in rows}
    if side == "sell":
        fw = [not o["yes"] for o in os_ if o["t"] in filled]; uw = [not o["yes"] for o in os_ if o["t"] not in filled]
        s["filled_NO_win"] = round(st.mean(fw), 4) if fw else None; s["unfilled_NO_win"] = round(st.mean(uw), 4) if uw else None
    else:
        fw = [o["yes"] for o in os_ if o["t"] in filled]; uw = [o["yes"] for o in os_ if o["t"] not in filled]
        s["filled_YES_win"] = round(st.mean(fw), 4) if fw else None; s["unfilled_YES_win"] = round(st.mean(uw), 4) if uw else None
    s["posted"] = len(os_); s["fill_rate"] = round(len(rows) / len(os_), 4) if os_ else None
    return s


def split(orders: list[dict]) -> tuple[set, set, float]:
    evs = sorted({(o["ev_last_close"], o["e"]) for o in orders})
    cut = evs[int(len(evs) * SPLIT)][0]
    disc = {e for c, e in evs if c < cut}; val = {e for c, e in evs if c >= cut}
    return disc, val, cut


def save_orders(orders: list[dict], name: str = "orders.jsonl") -> None:
    (OUT / name).write_text("".join(json.dumps(o) + "\n" for o in orders))


def need_trades(orders: list[dict]) -> list[tuple[str, int, int]]:
    """Live-tier posted orders whose candles show a through fill of the primary sell order (P): one prints call each, over
    (post + 120 s, max(C, C_lit) + 1 h). Candles aggregate the prints exactly, so an order without a candle through-print
    has no through-print; the budget (200 calls in all) does not allow prints for the mirror or the contrast arm."""
    out = []
    for o in orders:
        if o["posted"] and o["tier"] == "live" and o["P"]["thr"]:
            out.append((o["t"], int(o["tq"] + POST_GAP), int(max(o["C"], o["C_lit"]) + 3600)))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "orders":
        O = build_orders(); save_orders(O)
        from collections import Counter
        print(len(O), Counter(o.get("reason") if not o["posted"] else "posted" for o in O))
    elif cmd == "need_trades":
        O = build_orders(); print(len(need_trades(O)), need_trades(O)[:5])
    elif cmd == "fetch_trades":
        O = build_orders(); fetch_prints(need_trades(O), int(sys.argv[2]))


# ------------------------------------------------------------------------------------------------ discovery / validation
def rescan_capped(orders: list[dict], hours: float) -> list[dict]:
    """Variant: cancel at min(C, post + hours). Re-scans the candles (exact prints too) into arm 'Q'."""
    C = load_candles(); TR = load_prints()
    out = []
    for o in orders:
        o = dict(o)
        if o["posted"]:
            hi = min(o["C"], o["tq"] + hours * 3600)
            cd = C[o["t"]]
            o["Q"] = scan(cd["c"], cd["period"], o["tq"] + POST_GAP, hi, o["s"], o["b"])
            if o["t"] in TR:
                o["xQ"] = exact(TR[o["t"]]["trades"], o["tq"] + POST_GAP, hi, o["s"], o["b"])
        out.append(o)
    return out


def variants(orders: list[dict]) -> dict:
    """Every cell examined on a set of orders (the same function runs on discovery; validation only for frozen cells)."""
    V = {}
    V["P_sell_thr"] = cell(orders, "P", "sell", "thr")
    V["P_sell_any"] = cell(orders, "P", "sell", "any")
    V["P_mirror_thr"] = cell(orders, "P", "buy", "thr")
    V["P_mirror_any"] = cell(orders, "P", "buy", "any")
    V["L_sell_thr"] = cell(orders, "L", "sell", "thr")
    V["L_sell_any"] = cell(orders, "L", "sell", "any")
    V["L_mirror_thr"] = cell(orders, "L", "buy", "thr")
    for g in ("CB", "ADDR", "DEB", "AWD", "KEY", "LEAGUE"):
        V[f"P_sell_thr|group={g}"] = cell(orders, "P", "sell", "thr", lambda o, g=g: o["group"] == g)
    for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)):
        V[f"P_sell_thr|NO_px[{lo},{hi})"] = cell(orders, "P", "sell", "thr", lambda o, lo=lo, hi=hi: lo <= o["px"] < hi)
    V["P_sell_thr|spread>=0.10"] = cell(orders, "P", "sell", "thr", lambda o: o["spread"] >= 0.10 - EPS)
    V["P_sell_thr|spread<0.10"] = cell(orders, "P", "sell", "thr", lambda o: o["spread"] < 0.10 - EPS)
    V["P_sell_thr|tier=live"] = cell(orders, "P", "sell", "thr", lambda o: o["tier"] == "live")
    V["P_sell_thr|tier=arch"] = cell(orders, "P", "sell", "thr", lambda o: o["tier"] == "arch")
    V["P_sell_thr|1min"] = cell(orders, "P", "sell", "thr", lambda o: o["period"] == 1)
    V["P_sell_thr|hourly"] = cell(orders, "P", "sell", "thr", lambda o: o["period"] == 60)
    for h in (24, 48):
        q = rescan_capped(orders, h)
        V[f"P{h}h_sell_thr (cancel min(C, post+{h}h))"] = cell(q, "Q", "sell", "thr")
    return V


def strict_orders() -> list[dict]:
    return build_orders(strict30=True)


def discovery() -> dict:
    O = build_orders(); save_orders(O)
    disc, val, cut = split(O)
    D = [o for o in O if o["e"] in disc]
    V = variants(D)
    S = [o for o in strict_orders() if o["e"] in disc]
    V["P_sell_thr|strict30_quote"] = cell(S, "P", "sell", "thr")
    res = {"cut_last_close": cut, "events_disc": len(disc), "events_val": len(val),
           "orders_disc_posted": sum(o["posted"] for o in D), "variants_examined_on_discovery": len(V), "cells": V}
    (OUT / "discovery.json").write_text(json.dumps(res, indent=1))
    return res


def show(V: dict) -> None:
    for k, v in V.items():
        if not v.get("n"):
            print(f"  {k:52s} n=0 posted={v.get('posted')}"); continue
        print(f"  {k:52s} n={v['n']:3d} ev={v['events']:3d} fill={v['fill_rate']} win={v['win']:.2f} px={v['avg_px']:.2f} "
              f"ret={v['ret_per_dollar']:+.3f} t={v['t']} wo3={v['ret_wo3']} sw={v['size_weighted_ret']} "
              f"fNO={v.get('filled_NO_win', v.get('filled_YES_win'))} uNO={v.get('unfilled_NO_win', v.get('unfilled_YES_win'))} blo={v['beta_lo_ret']}")


if __name__ == "__main__" and sys.argv[1] == "discovery":
    r = discovery()
    print({k: v for k, v in r.items() if k != "cells"})
    show(r["cells"])


def validate() -> dict:
    O = build_orders()
    disc, val, cut = split(O)
    out = {"cut_last_close": cut}
    for name, evs in (("validation", val), ("discovery", disc), ("pooled", disc | val)):
        X = [o for o in O if o["e"] in evs]
        Q = rescan_capped(X, 48)
        out[name] = {"V1_P_sell_thr": cell(X, "P", "sell", "thr"), "V1_P_mirror_thr": cell(X, "P", "buy", "thr"),
                     "V1_P_sell_any": cell(X, "P", "sell", "any"), "V1_P_mirror_any": cell(X, "P", "buy", "any"),
                     "V2_L_sell_thr": cell(X, "L", "sell", "thr"), "V2_L_mirror_thr": cell(X, "L", "buy", "thr"),
                     "V3_P48_sell_thr": cell(Q, "Q", "sell", "thr"), "V3_P48_mirror_thr": cell(Q, "Q", "buy", "thr"),
                     "V1_P_sell_thr|live_tier": cell(X, "P", "sell", "thr", lambda o: o["tier"] == "live"),
                     "V1_P_sell_thr|arch_tier": cell(X, "P", "sell", "thr", lambda o: o["tier"] == "arch"),
                     "V1_P_sell_thr|D*_ok (first close >= T_day; ex post, diagnostic only)": cell(X, "P", "sell", "thr", lambda o: o["ev_first_close"] >= o["T_day"])}
    (OUT / "validation.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__" and sys.argv[1] == "validate":
    r = validate()
    for k in ("validation", "discovery", "pooled"):
        print("==", k); show(r[k])


# ------------------------------------------------------------------------------------------------ result.json
NOTES = (
    "VERDICT dead by the pre-registered kill (test plan: dead if the pooled through-only size-weighted return < +5% OR the "
    "mirror is not negative): the pooled maker-YES mirror is +15.6%/$ equal-$ (n 26, t 0.46), so kill B fires. It fires on "
    "ONE fill (KXPOWELLMENTION-25OCT15 'PARD', YES bid at 0.06 paying 16.7x, in an event the ticker dated a day late, so the "
    "order rested into the speech); without it the mirror is -46%, and in validation it is -43.5% (n 16, t -1.37). Kill A "
    "does not fire (pooled P size-weighted +57%). The sell cell itself fails the gate by a wide margin: validation n 11 fills / "
    "9 events (bar 40), t 1.22 (bar 4.53), wo3 -22%, Beta lower bound -7.6%; pooled n 31 / 29 events, +50.5%/$, t 1.37, "
    "+0.2% without the 3 best, +25% without the single best (KXDEBATEMENTION-26OCT07 'Trump' NO at 0.11 = +809%). "
    "WHAT THE DATA SAY: (1) Kalshi seeds wide books at listing in these formats too (87% of 87 posted orders had a listing "
    "spread >= 10c, median 36c, median NO price 0.24; 21 were the bid <= 0.04 / ask >= 0.79 placeholder). (2) The payoff is a "
    "lottery: selling YES at 0.6-0.9 = buying NO at 0.1-0.4; win rate 55% at avg NO 0.41. (3) The one robust contrast is "
    "adverse selection: resting through the appearance start (literal r2 C1, cancel at first close) fills 70% of orders and "
    "filled NO wins 43% vs 92% for unfilled; cancelling at 00:00 ET of the appearance day (P) fills 36% and filled NO wins "
    "55% vs 59% unfilled. So the day-before cancel removes most of the pick-off, as the family hypothesis says, but what is "
    "left is not distinguishable from noise: discovery P +25.6% (t 0.73, wo3 -29%), the mirror was +110% on discovery. "
    "Groups (pooled P): central bankers 0/5 fills won (-100%), keynotes -9%, addresses +83%, debates +123%, fights +136% "
    "(4 fills); every group has <= 9 fills. V3 (cancel at min(C, post + 48 h), chosen on discovery) pooled n 21, +82%, t 1.71, "
    "Beta lower bound +4.6%, but its validation is n 5. "
    "SCHEDULE: archived occurrence_datetime is overwritten after settlement and rules text rarely carries a date, so the "
    "cancel uses D* = min(event-ticker date, expected_expiration date - 1 day) at 00:00 ET (amendment 1, before any price); "
    "it fails (first close before T_day) in 3.8% of events and those orders are among the worst fills. "
    "DATA LIMITS: 85 events / 100 sampled markets (budget 200 calls; 13 calls lost because /historical refuses 1-minute "
    "requests above ~5,000 candles); archive fills are sized from candle volume (counterfactual, optimistic for at-level "
    "fills); only the 11 live-tier P fills have exact prints. Live tier since 2026-08: only 15 P-eligible events in 70 days "
    "(debates, Waller, Carney, Netanyahu, Jensen, Apple, Warsh); 22 of the 30 series listed nothing since August. "
    "WHAT WOULD DECIDE IT: about 150+ forward fills over 80+ events with exact prints (the logger, at ~1 fill/day, needs "
    "~5 months); a mirror that stays negative; and a size-weighted mean that survives without its best 3 fills. Prior that "
    "it reaches t 4.5: well below 5%.")


def result() -> dict:
    V = json.loads((OUT / "validation.json").read_text()); D = json.loads((OUT / "discovery.json").read_text())
    def item(rule, c, extra=None):
        x = {"rule": rule, "n": c["n"], "events": c["events"], "win": c["win"], "avg_px": c["avg_px"], "ret_per_dollar": c["ret_per_dollar"],
             "t": c["t"], "ret_wo3": c["ret_wo3"], "half1": c["half1"], "half2": c["half2"],
             "median_capacity_contracts": c["median_filled_contracts"], "size_weighted_ret": c["size_weighted_ret"],
             "beta_lo_ret": c["beta_lo_ret"], "filled_NO_win": c.get("filled_NO_win"), "unfilled_NO_win": c.get("unfilled_NO_win"),
             "posted": c["posted"], "fill_rate": c["fill_rate"]}
        x.update(extra or {})
        return x
    v, p = V["validation"], V["pooled"]
    tpd = {"V1": 1.3, "V2": 2.6, "V3": 0.7}   # 0.21 P-eligible events/day (live tier) x ~19 markets x fill rate
    val = [
        item("V1_P (pre-registered C1S): sell YES at ask-1c at H1 = first top of hour >= open+1h, cancel at min(00:00 ET on D*, first close in event); through-only fills, no fee",
             v["V1_P_sell_thr"], {"trades_per_day": tpd["V1"], "mirror_buy_yes_ret": v["V1_P_mirror_thr"]["ret_per_dollar"], "mirror_t": v["V1_P_mirror_thr"]["t"],
                                  "mirror_n": v["V1_P_mirror_thr"]["n"], "pooled_ret": p["V1_P_sell_thr"]["ret_per_dollar"], "pooled_t": p["V1_P_sell_thr"]["t"],
                                  "pooled_n": p["V1_P_sell_thr"]["n"], "pooled_events": p["V1_P_sell_thr"]["events"], "pooled_wo3": p["V1_P_sell_thr"]["ret_wo3"],
                                  "pooled_size_weighted": p["V1_P_sell_thr"]["size_weighted_ret"], "pooled_mirror_ret": p["V1_P_mirror_thr"]["ret_per_dollar"],
                                  "pooled_mirror_t": p["V1_P_mirror_thr"]["t"], "discovery_ret": D["cells"]["P_sell_thr"]["ret_per_dollar"], "discovery_t": D["cells"]["P_sell_thr"]["t"]}),
        item("V2_L literal r2 C1 (contrast): same order, cancel at the event's first close only (rests into the appearance)", v["V2_L_sell_thr"],
             {"trades_per_day": tpd["V2"], "mirror_buy_yes_ret": v["V2_L_mirror_thr"]["ret_per_dollar"], "pooled_ret": p["V2_L_sell_thr"]["ret_per_dollar"],
              "pooled_t": p["V2_L_sell_thr"]["t"], "pooled_filled_NO_win": p["V2_L_sell_thr"]["filled_NO_win"], "pooled_unfilled_NO_win": p["V2_L_sell_thr"]["unfilled_NO_win"]}),
        item("V3_P48 (chosen on discovery): P with cancel at min(C, post + 48 h)", v["V3_P48_sell_thr"],
             {"trades_per_day": tpd["V3"], "mirror_buy_yes_ret": v["V3_P48_mirror_thr"]["ret_per_dollar"], "pooled_ret": p["V3_P48_sell_thr"]["ret_per_dollar"],
              "pooled_t": p["V3_P48_sell_thr"]["t"], "pooled_n": p["V3_P48_sell_thr"]["n"], "pooled_beta_lo_ret": p["V3_P48_sell_thr"]["beta_lo_ret"]}),
    ]
    db = D["cells"]["L_sell_any"]
    calls = (OUT / "calls.log").read_text().splitlines()
    res = {
        "name": "r4_single_appearance_seeded_books",
        "hypothesis": "Kalshi seeds wide books when it lists single-appearance mention markets (central-bank speeches, addresses, debates, award shows, keynotes, league pressers/fights). Narrative YES buyers lift those books before the appearance; a resting sell-YES one tick inside the listing ask (= buy NO), cancelled before the appearance day so it never rests through informed in-appearance flow, earns >= +10%/$ after (zero) maker fees, and the mirror buy-YES maker loses.",
        "why_edge_could_exist": "These series are fee_type quadratic (makers pay nothing), retail-heavy, listed hours to weeks before a scheduled appearance with seeded books (87% of evaluated listings had spreads >= 10c, median 36c). Early YES takers buy narratives; a home Mac posting once at the first hour after listing needs no speed, and a scheduled cancel (00:00 ET of the appearance day) keeps the order away from viewers trading on the live speech, the flow that picked off round 3's accumulator and sports orders.",
        "data_used": f"Kalshi public API, {len(calls)} calls (budget 200; calls.log): 31 /historical/markets lists + 22 live-tier lists for 30 pre-registered series (4,754 markets, 290 events, 284 complete); 100 seeded-sample markets of 85 events (70 archive events x 1 market, 15 live events x 2): 83 /historical per-market candle calls (1-minute if the window <= ~83 h, else hourly; 13 of them repeat calls after the 5,000-candle limit returned nothing) and 15 batched live /markets/candlesticks calls; 11 /markets/trades calls for exact prints of live-tier fills; 12 logger calls (2 live passes of 5 discovery calls each + a 2-call dry pass on the open debate series in a scratch directory). Read-only reuse of the cached /series?category=Mentions catalog from r3_earnings_call_mentions.",
        "code_path": "lab/kalshi/strategies/r4_single_appearance_seeded_books.py (orders, fills, discovery, validate, result); _data.py (lists, plan/sampling, candles, prints); _api.py (counted cached Kalshi GET + logger call log); _logger.py (forward paper logger); _summary.py (forward summary)",
        "variants_examined": 45,
        "discovery_best": {"rule": "L_sell_any: literal C1 (cancel at first close), any-print fills (discovery only; not a validation candidate because it rests through the appearance and is strongly adversely selected)",
                           "n": db["n"], "ret_per_dollar": db["ret_per_dollar"], "t": db["t"], "events": db["events"]},
        "validation": val,
        "verdict": "dead",
        "kalshi_calls_used": len(calls),
        "live_requirements": "None required (verdict dead). Optional, cheap monitor: run `.venv/bin/python -m lab.kalshi.strategies.r4_single_appearance_seeded_books_logger` every 10 min at minutes 3,13,23,33,43,53 (launchd StartCalendarInterval; not installed), <= 10 Kalshi calls per pass via the bot-idle fetch (typically 5 discovery calls; ~30/h); summary: `.venv/bin/python -m lab.kalshi.strategies.r4_single_appearance_seeded_books_summary`. Pre-registration: data/kalshi_lab/strategies/r4_single_appearance_seeded_books/preregistration.json (frozen before pass 1). Expect ~1 fill/day; ~5 months to reach a decisive sample. A live version would need resting orders with the owner's API key, and collateral caps a $50 book at ~10 concurrent $5 orders while the rule posts ~15-20 per event.",
        "notes": NOTES,
    }
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__" and sys.argv[1] == "result":
    print(json.dumps(result(), indent=1)[:3000])
