"""Candle timestamp audit: which seconds does a Kalshi 1-minute candle stamped end_period_ts = T really cover?

Ground truth = public trade prints (/markets/trades, microsecond created_time). For every candle that is fully covered
by a print window, and for every shift s (seconds), the prints are bucketed into (T-60+s, T+s]:
  volume test  - candle volume vs bucket volume (exact match rate, MAE);
  price test   - candle price.close / price.open vs the last / first print in the bucket (full candles only);
  quote test   - candle yes_ask.close (yes_bid.close) vs the price of YES-buy (NO-buy) prints within 2 s of T+s.
The shift that maximises agreement is the true alignment; s = 0 means the candle covers exactly (T-60, T].
Inputs: any list of (ticker, prints, print_window, candles) - see load_* helpers."""
from __future__ import annotations
import bisect, datetime as dt, json, statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SHIFTS = list(range(-40, 41, 2)) + [-1, 1, 3, -3]


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def norm_prints(raw: list[dict]) -> list[tuple]:
    """-> sorted [(t, yes_price, taker_side 'yes'/'no', count)]"""
    out = []
    for x in raw:
        c = x.get("count_fp", x.get("count"))
        p = x.get("yes_price_dollars")
        if p is None and x.get("yes_price") is not None:
            p = x["yes_price"] / 100
        out.append((ts(x["created_time"]), float(p), x.get("taker_side"), float(c or 0)))
    return sorted(out)


def norm_candles(cs: list) -> list[dict]:
    """Accept full API candlesticks or the lab's compact rows [ts, ask, bid, ask_lo, bid_hi, vol]."""
    out = []
    for c in cs:
        if isinstance(c, dict):
            f = lambda k, s: (float(c[k][s]) if c.get(k) and c[k].get(s) is not None else None)
            out.append({"T": c["end_period_ts"], "vol": float(c.get("volume_fp") or c.get("volume") or 0), "ask": f("yes_ask", "close_dollars"),
                        "bid": f("yes_bid", "close_dollars"), "pc": f("price", "close_dollars"), "po": f("price", "open_dollars"),
                        "ph": f("price", "high_dollars"), "pl": f("price", "low_dollars"), "full": True})
        else:
            out.append({"T": c[0], "vol": float(c[5] or 0), "ask": c[1], "bid": c[2], "pc": None, "po": None, "ph": None, "pl": None, "full": False})
    return sorted(out, key=lambda r: r["T"])


def align(items: list[dict], shifts=SHIFTS) -> dict:
    """items: {tk, prints (normalised), lo, hi (seconds fully covered by the prints), candles (normalised), dense (bool:
    minutes without a candle had zero volume)}"""
    vol = {s: [0, 0, 0.0] for s in shifts}          # exact matches, n, abs err sum
    vol_active = {s: [0, 0] for s in shifts}        # same, restricted to minutes where candle or bucket has volume
    pc = {s: [0, 0] for s in shifts}; po = {s: [0, 0] for s in shifts}; hl = {s: [0, 0] for s in shifts}
    qa = {s: [0, 0, 0.0] for s in shifts}; qb = {s: [0, 0, 0.0] for s in shifts}
    ra = {s: [0, 0] for s in shifts}; rb = {s: [0, 0] for s in shifts}   # first revealing print after T+s
    for it in items:
        P = it["prints"]; pt = [p[0] for p in P]; lo, hi = it["lo"], it["hi"]
        cmap = {c["T"]: c for c in it["candles"]}
        Ts = sorted(cmap) if not it.get("dense") else list(range(int(lo // 60 * 60 + 120), int(hi // 60 * 60), 60))
        for T in Ts:
            if T - 60 + min(shifts) < lo or T + max(shifts) > hi:
                continue
            c = cmap.get(T)
            cv = c["vol"] if c else 0.0
            for s in shifts:
                i = bisect.bisect_right(pt, T - 60 + s); j = bisect.bisect_right(pt, T + s)
                b = P[i:j]; bv = sum(p[3] for p in b)
                ok = abs(bv - cv) < 0.011 + 1e-7 * cv
                vol[s][0] += ok; vol[s][1] += 1; vol[s][2] += abs(bv - cv)
                if bv > 0 or cv > 0:
                    vol_active[s][0] += ok; vol_active[s][1] += 1
                if c and c["full"] and b and c["pc"] is not None and cv > 0:
                    pc[s][0] += abs(b[-1][1] - c["pc"]) < 1e-6; pc[s][1] += 1
                    po[s][0] += abs(b[0][1] - c["po"]) < 1e-6; po[s][1] += 1
                    hl[s][0] += abs(max(p[1] for p in b) - c["ph"]) < 1e-6 and abs(min(p[1] for p in b) - c["pl"]) < 1e-6; hl[s][1] += 1
                if c and c["ask"] is not None and 0.02 < (c["ask"] or 0) < 0.98:
                    k1 = bisect.bisect_left(pt, T + s - 2); k2 = bisect.bisect_right(pt, T + s + 2)
                    ya = [p[1] for p in P[k1:k2] if p[2] == "yes"]; nb = [p[1] for p in P[k1:k2] if p[2] == "no"]
                    if ya:
                        e = abs(st.median(ya) - c["ask"]); qa[s][0] += e < 1e-6; qa[s][1] += 1; qa[s][2] += e
                    if nb and c["bid"] is not None:
                        e = abs(st.median(nb) - c["bid"]); qb[s][0] += e < 1e-6; qb[s][1] += 1; qb[s][2] += e
                    k = bisect.bisect_right(pt, T + s); fa = fb = None
                    while k < len(P) and P[k][0] <= T + s + 5 and (fa is None or fb is None):
                        if P[k][2] == "yes" and fa is None: fa = P[k][1]
                        if P[k][2] == "no" and fb is None: fb = P[k][1]
                        k += 1
                    if fa is not None:
                        ra[s][0] += abs(fa - c["ask"]) < 1e-6; ra[s][1] += 1
                    if fb is not None and c["bid"] is not None:
                        rb[s][0] += abs(fb - c["bid"]) < 1e-6; rb[s][1] += 1
    r = lambda d: {s: (round(v[0] / v[1], 3), v[1]) for s, v in d.items() if v[1]}
    out = {"volume_exact_rate": r(vol), "volume_exact_rate_active_minutes": r(vol_active),
           "volume_mae": {s: round(v[2] / v[1], 3) for s, v in vol.items() if v[1]},
           "price_close_match": r(pc), "price_open_match": r(po), "price_high_low_match": r(hl),
           "ask_close_vs_yesbuy_prints_match": r(qa), "ask_close_mae": {s: round(v[2] / v[1], 4) for s, v in qa.items() if v[1]},
           "bid_close_vs_nobuy_prints_match": r(qb), "ask_close_vs_first_yesbuy_after": r(ra), "bid_close_vs_first_nobuy_after": r(rb), "bid_close_mae": {s: round(v[2] / v[1], 4) for s, v in qb.items() if v[1]}}
    for k in ("volume_exact_rate_active_minutes", "price_close_match", "ask_close_vs_yesbuy_prints_match", "bid_close_vs_nobuy_prints_match",
              "ask_close_vs_first_yesbuy_after", "bid_close_vs_first_nobuy_after"):
        if out[k]:
            out["best_shift_" + k] = max(out[k], key=lambda s: (out[k][s][0], -abs(s)))
    return out


def window_of(raw: list[dict], min_ts: float, max_ts: float, limit: int = 1000) -> tuple[float, float]:
    """Seconds fully covered by a /markets/trades response for [min_ts, max_ts] (newest first; truncated at limit)."""
    if len(raw) >= limit:
        return min(ts(x["created_time"]) for x in raw), max_ts
    return min_ts, max_ts
