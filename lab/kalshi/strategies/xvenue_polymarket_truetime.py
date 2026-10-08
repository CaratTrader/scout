"""True-clock check for xvenue_polymarket on the live-recorded window: Kalshi trade prints (exact created_time) vs the
Kalshi REST /markets snapshot (staleness) and vs the Polymarket book mid polled every 3 s (who leads, by how much).
Input: live_btc.jsonl, live_kalshi_trades.json. Usage: python -m lab.kalshi.strategies.xvenue_polymarket_truetime"""
from __future__ import annotations
import bisect, datetime as dt, json, statistics as st
from pathlib import Path
OUT = Path(__file__).resolve().parents[3] / "data/kalshi_lab/strategies/xvenue_polymarket"


def main():
    rows = [json.loads(l) for l in (OUT / "live_btc.jsonl").open()]
    trades = json.load((OUT / "live_kalshi_trades.json").open())
    res = {}
    for tk, tr in trades.items():
        pts = sorted((dt.datetime.fromisoformat(x["created_time"].replace("Z", "+00:00")).timestamp(), float(x["yes_price_dollars"])) for x in tr)
        if not pts:
            continue
        ts = [p[0] for p in pts]; lo, hi = ts[0], ts[-1]

        def ktrue(t, w=3.0):
            i = bisect.bisect_left(ts, t - w); j = bisect.bisect_right(ts, t)
            return st.median(p[1] for p in pts[i:j]) if j > i else None
        rest = [(r["t1"], (float(r["ask"]) + float(r["bid"])) / 2, r["t0"]) for r in rows if r["v"] == "K" and r.get("tk") == tk and lo + 60 <= r["t1"] <= hi]
        stal = {}
        for L in (0, 5, 10, 20, 30, 45, 60, 90):
            e = [abs(m - ktrue(t - L)) for t, m, _ in rest if ktrue(t - L) is not None]
            if e:
                stal[L] = (round(st.mean(e), 4), len(e))
        o = int(lo) // 900 * 900
        P = sorted((r["t1"], (r["bid"][0] + r["ask"][0]) / 2) for r in rows if r["v"] == "P" and r["open"] == o and r["bid"] and r["ask"] and lo + 60 <= r["t1"] <= hi)
        # cross-correlation of 6-s changes: corr(dPM(t), dK(t+lag)); positive lag peak = PM leads
        grid = [P[0][0] + 6 * i for i in range(int((P[-1][0] - P[0][0]) / 6))] if P else []
        pts_p = [p[0] for p in P]
        pm = lambda t: P[bisect.bisect_right(pts_p, t) - 1][1] if bisect.bisect_right(pts_p, t) > 0 else None
        xc = {}
        for lag in (-30, -18, -12, -6, 0, 6, 12, 18, 30):
            a = []; b = []
            for t in grid:
                p0, p1, k0, k1 = pm(t), pm(t + 6), ktrue(t + lag), ktrue(t + lag + 6)
                if None not in (p0, p1, k0, k1):
                    a.append(p1 - p0); b.append(k1 - k0)
            if len(a) > 10:
                ma, mb = st.mean(a), st.mean(b); sa = sum((x - ma) ** 2 for x in a) ** .5; sb = sum((x - mb) ** 2 for x in b) ** .5
                xc[lag] = round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb), 3) if sa and sb else None
        res[tk] = {"trades": len(pts), "span_s": round(hi - lo), "trades_per_s": round(len(pts) / (hi - lo), 1),
                   "rest_staleness_mae_by_lag_s": stal, "xcorr_dPM_vs_dK_at_lag_s(+ = Kalshi later)": xc, "pm_samples": len(P)}
    print(json.dumps(res, indent=1))
    (OUT / "truetime.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
