"""Shared loading, execution and statistics for the tennis_model study (standard library only).

Events: Kalshi tennis match events (two complementary markets, one per player) from data/kalshi_lab/{markets,candles}.
Split: events ordered by close time; DISCOVERY = first 70%, VALIDATION = last 30% (one cut for the whole family).
Execution: decide at t with candles <= t; fill as taker one minute later at the quote then (YES at yes_ask, NO at
1 - yes_bid), skip if that quote is older than 30 minutes; fee ceil-to-cent per order (default order 10 contracts)."""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

SERIES = ("KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGERMATCH")
MK = Path("data/kalshi_lab/markets"); CD = Path("data/kalshi_lab/candles")
OUT = Path("data/kalshi_lab/strategies/tennis_model")
MAX_AGE = 30 * 60
SPLIT = 0.7
ORDER_SIZE = 10


def fee_per_contract(p: float, n: int = ORDER_SIZE) -> float:
    """Kalshi taker fee 0.07*p*(1-p) per contract, rounded UP to the cent per order of n contracts."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def load(series=SERIES) -> list[dict]:
    """One dict per event: {e, series, close, exp, open, mk: {ticker: {name, won, c}}} for events with both markets."""
    ev = {}
    for s in series:
        meta = {}
        for line in (MK / f"{s}.jsonl").open():
            m = json.loads(line); meta[m["t"]] = m
        cand = {}
        for line in (CD / f"{s}.jsonl").open():
            x = json.loads(line)
            cand[x["t"]] = [r for r in x["c"] if r[1] is not None and r[2] is not None]
        for t, m in meta.items():
            e = ev.setdefault(m["e"], {"e": m["e"], "series": s, "close": m["close"], "exp": m["exp"], "open": m["open"], "mk": {}})
            e["close"] = max(e["close"], m["close"])
            e["mk"][t] = {"t": t, "name": m["sub"], "won": m["result"] == "yes", "c": cand.get(t, []), "vol": m["vol"]}
    out = [e for e in ev.values() if len(e["mk"]) == 2 and sum(x["won"] for x in e["mk"].values()) == 1]
    out.sort(key=lambda e: (e["close"], e["e"]))
    return out


def split(events: list[dict], frac: float = SPLIT) -> tuple[list[dict], list[dict], int]:
    cut = events[int(len(events) * frac)]["close"]
    return [e for e in events if e["close"] < cut], [e for e in events if e["close"] >= cut], cut


def quote(c: list[list], t: int):
    """(yes_ask, yes_bid, ts) of the last candle at or before t, if not older than MAX_AGE."""
    lo, hi = 0, len(c)
    while lo < hi:
        mid = (lo + hi) // 2
        if c[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    r = c[lo - 1]
    if t - r[0] > MAX_AGE:
        return None
    return r[1], r[2], r[0]


def fill(mk: dict, side: str, t_decide: int, close: int, delay: int = 60):
    """Taker fill one minute after the decision. Returns (price, won) or None (no fresh quote / market closed)."""
    tf = t_decide + delay
    if tf >= close:
        return None
    q = quote(mk["c"], tf)
    if not q:
        return None
    ask, bid, _ = q
    px = ask if side == "YES" else 1 - bid
    if not (0.01 <= px <= 0.99):
        return None
    won = mk["won"] if side == "YES" else not mk["won"]
    return px, won


def trade_row(e: dict, mk: dict, side: str, px: float, won: bool, t: int, **kw) -> dict:
    f = fee_per_contract(px)
    pnl = (1.0 if won else 0.0) - px - f
    return {"e": e["e"], "series": e["series"], "close": e["close"], "t": t, "ticker": mk["t"], "side": side, "px": px, "won": won,
            "pnl": pnl, "ret": pnl / px, **kw}


def stats(rows: list[dict]) -> dict:
    """Equal-$ return per trade; t clustered by event; mean w/o 3 best; halves by close time."""
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em)
    sd = st.pstdev(em) if n_e > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(n_e)) if n_e > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["close"]); h = len(srt) // 2
    h1 = [r["ret"] for r in srt[:h]]; h2 = [r["ret"] for r in srt[h:]]
    return {"n": len(rows), "events": n_e, "win": round(sum(r["won"] for r in rows) / len(rows), 4), "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "pnl_per_contract": round(st.mean(r["pnl"] for r in rows), 4)}


def fmt(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"{name:44s} n=0"
    return (f"{name:44s} n={s['n']:4d} ev={s['events']:4d} win={s['win']:.2f} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.3f} t={s['t'] if s['t'] is not None else float('nan'):+.2f} "
            f"wo3={s['ret_wo3'] if s['ret_wo3'] is not None else float('nan'):+.3f} h1={s['half1'] if s['half1'] is not None else float('nan'):+.3f} h2={s['half2'] if s['half2'] is not None else float('nan'):+.3f}")
