"""Check the historical series against the live recording (xvenue_polymarket_live.py output):
1. Polymarket prices-history stamp offset: for each history point (t, p), which live book mid (at t - d) does p match?
2. Kalshi candle stamp offset: for each 1-minute candle (end_period_ts T, ask, bid), which live quote (at T - d) matches?
3. Real-clock lead-lag: when the venues disagree, which one moves to close the gap over the next 20-60 s?
Kalshi calls: one batch candlestick call for the recorded markets.
Usage: python -m lab.kalshi.strategies.xvenue_polymarket_livecheck"""
from __future__ import annotations
import bisect, json, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K
from lab.kalshi.strategies.xvenue_polymarket_fetch import get, C, OUT


def step(pts, dflt=60):
    ts = [p[0] for p in pts]; vs = [p[1] for p in pts]
    def at(t, age=dflt):
        i = bisect.bisect_right(ts, t) - 1
        return vs[i] if i >= 0 and t - ts[i] <= age else None
    return at


def main():
    rows = [json.loads(l) for l in (OUT / "live_btc.jsonl").open()]
    toks = {r["open"]: r["tok"] for r in rows if r["v"] == "Ptok"}
    P = sorted((r["t1"], (r["bid"][0] + r["ask"][0]) / 2, r["open"]) for r in rows if r["v"] == "P" and r["bid"] and r["ask"] and r["ask"][0] - r["bid"][0] <= 0.03)
    Kq = sorted((r["t1"], (float(r["ask"]) + float(r["bid"])) / 2, r["tk"], float(r["ask"]), float(r["bid"]), float(r["ask_sz"] or 0)) for r in rows
                if r["v"] == "K" and r["ask"] and r["bid"] and float(r["ask"]) - float(r["bid"]) <= 0.03)
    res = {"pm_samples": len(P), "k_samples": len(Kq)}
    # 1. Polymarket history offset
    err = {d: [] for d in (-30, 0, 15, 30, 45, 60, 75, 90, 120)}
    for o, tok in toks.items():
        live = step([(t, m) for t, m, oo in P if oo == o])
        h = (get(f"{C}/prices-history?market={tok}&startTs={o - 60}&endTs={o + 960}&fidelity=1") or {}).get("history", [])
        for p in h:
            for d in err:
                v = live(p["t"] - d, 10)
                if v is not None and 0.05 < v < 0.95:
                    err[d].append(abs(v - p["p"]))
    res["pm_history_offset_mae"] = {d: (round(st.mean(e), 4), len(e)) for d, e in err.items() if e}
    # 2. Kalshi candle offset (one batch call)
    tks = sorted({k[2] for k in Kq}); t0 = int(min(k[0] for k in Kq)) - 120; t1 = int(max(k[0] for k in Kq)) + 120
    cf = OUT / "live_kalshi_candles.json"
    if not cf.exists():
        cf.write_text(fetch(f"{K}/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={t0}&end_ts={t1}&period_interval=1", pace=1.15) or "{}")
    d = json.loads(cf.read_text())
    kerr = {x: [] for x in (-30, 0, 15, 30, 45, 60, 90)}
    for mk in d.get("markets", []):
        tk = mk.get("market_ticker") or mk.get("ticker")
        live = step([(k[0], k[1]) for k in Kq if k[2] == tk])
        for c in mk.get("candlesticks", []):
            a = (c.get("yes_ask") or {}).get("close_dollars"); b = (c.get("yes_bid") or {}).get("close_dollars")
            if a is None or b is None:
                continue
            mid = (float(a) + float(b)) / 2
            for x in kerr:
                v = live(c["end_period_ts"] - x, 25)
                if v is not None and 0.05 < v < 0.95:
                    kerr[x].append(abs(v - mid))
    res["kalshi_candle_offset_mae"] = {x: (round(st.mean(e), 4), len(e)) for x, e in kerr.items() if e}
    # 3. real-clock lead-lag at the Kalshi sample times
    pm = step([(t, m) for t, m, _ in P], 10)
    km = {tk: step([(k[0], k[1]) for k in Kq if k[2] == tk], 30) for tk in tks}
    ll = {}
    for h in (20, 40, 60):
        g = []; dk = []; dp = []
        for t, m, tk, a, b, sz in Kq:
            p0 = pm(t); p1 = pm(t + h); k1 = km[tk](t + h)
            if None in (p0, p1, k1) or not (0.05 < m < 0.95):
                continue
            g.append(p0 - m); dk.append(k1 - m); dp.append(p1 - p0)
        if len(g) > 10:
            mg = st.mean(g); vg = sum((x - mg) ** 2 for x in g)
            ll[h] = {"n": len(g), "kalshi_closes": sum((x - mg) * (y - st.mean(dk)) for x, y in zip(g, dk)) / vg,
                     "pm_closes": -sum((x - mg) * (y - st.mean(dp)) for x, y in zip(g, dp)) / vg, "abs_gap_med": st.median(abs(x) for x in g),
                     "abs_gap_gt3c": sum(abs(x) > 0.03 for x in g) / len(g)}
    res["realclock_leadlag"] = ll
    res["kalshi_top_ask_size_median"] = st.median(k[5] for k in Kq)
    print(json.dumps(res, indent=1))
    (OUT / "livecheck.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
