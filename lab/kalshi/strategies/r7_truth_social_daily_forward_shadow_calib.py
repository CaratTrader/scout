"""Descriptive calibration of the FROZEN daily application of the r6 weekly model on past daily windows, from Roll Call
posts only (r6 posts.json, fetched 2026-10-08; no Kalshi prices, nothing chosen from it; run after the freeze).

For every ET day 2026-02-08 .. 2026-10-07 and decision times 10:00, 14:00, 18:00, 22:00, 23:30 ET: the D1 model's
distribution of the final daily count (as the logger computes it, with the deletion flags Roll Call had set by then),
versus the final kept count. Reports per decision time: mean log score of the frozen phi = 1 and, for reference only,
other phi values (never used by the rule); coverage of the model's central 80% interval; P(final < 5) and P(final >= 40)
predicted vs realised; split into the r6 discovery span (to 07-18) and later days.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_truth_social_daily_forward_shadow_calib"""
from __future__ import annotations
import bisect, datetime as dt, json, statistics as st, sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies import r7_truth_social_daily_forward_shadow_logger as L
from lab.kalshi.strategies.r6_truth_social_count_endgame import nb_cdf_table, nb_logpmf

ET = ZoneInfo("America/New_York")
POSTS = ROOT / "data/kalshi_lab/strategies/r6_truth_social_count_endgame/posts.json"
DISC_END = dt.date(2026, 7, 19)


def main() -> dict:
    M = L.load_model()
    raw = json.loads(POSTS.read_text())
    allp = [{"ts": p["ts"], "deleted": p["deleted"],
             "flag_ts": int(dt.datetime.fromisoformat(p["deleted_date"]).timestamp()) if p.get("deleted_date") else None} for p in raw.values()]
    allp.sort(key=lambda r: r["ts"])
    kept_final = sorted(r["ts"] for r in allp if not r["deleted"])
    hours = ((10, 0), (14, 0), (18, 0), (22, 0), (23, 30))
    res = {}
    for span in ("disc", "later"):
        res[span] = {f"{h:02d}:{m:02d}": {"n": 0, "ls": {phi: [] for phi in (1.0, 2.0, 4.0, 8.0)}, "cov80": 0, "p_lt5": [], "y_lt5": 0,
                                          "p_ge40": [], "y_ge40": 0, "pit": []} for h, m in hours}
    day = dt.date(2026, 2, 15)
    finals = []
    while day <= dt.date(2026, 10, 7):
        A = int(dt.datetime(day.year, day.month, day.day, tzinfo=ET).timestamp())
        B = int((dt.datetime(day.year, day.month, day.day, tzinfo=ET) + dt.timedelta(days=1)).timestamp())
        fin = bisect.bisect_left(kept_final, B) - bisect.bisect_left(kept_final, A)
        finals.append(fin)
        span = "disc" if day < DISC_END else "later"
        for h, m in hours:
            t = int(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ET).timestamp())
            lo_i = bisect.bisect_left(TS, t - L.LOOKBACK_S - 3600); hi_i = bisect.bisect_right(TS, t)
            rows = [{"ts": r["ts"], "deleted": r["deleted"] and r["flag_ts"] is not None and r["flag_ts"] <= t,
                     "flag_ts": r["flag_ts"] if (r["deleted"] and r["flag_ts"] is not None and r["flag_ts"] <= t) else None}
                    for r in allp[lo_i:hi_i]]
            P = L.Posts(rows, t)
            c, seen, recent = L.kept_est(M, P, A, t)
            mu = L.level(M, P, t, t - L.LOOKBACK_S) * L.profile_mass(M, t, B)
            R = fin - seen   # realised remaining net of deletions (deletions folded in; small)
            cell = res[span][f"{h:02d}:{m:02d}"]
            cell["n"] += 1
            for phi in cell["ls"]:
                cell["ls"][phi].append(nb_logpmf(max(R, 0), mu, phi))
            cdf = nb_cdf_table(mu, M.phi, 400)
            q10 = next(k for k, v in enumerate(cdf) if v >= 0.10); q90 = next(k for k, v in enumerate(cdf) if v >= 0.90)
            cell["cov80"] += q10 <= R <= q90
            cell["pit"].append(cdf[R - 1] if R > 0 else 0.0)
            brs = [{"t": "lt5", "lo": 0, "hi": 4}, {"t": "ge40", "lo": 40, "hi": L.BIG}]
            pr = L.ladder_probs(seen, recent, M.delta, mu, M.phi, brs)
            cell["p_lt5"].append(pr["lt5"]); cell["y_lt5"] += fin < 5
            cell["p_ge40"].append(pr["ge40"]); cell["y_ge40"] += fin >= 40
        day += dt.timedelta(days=1)
    out = {"days": len(finals), "daily_final_mean": round(st.mean(finals), 2), "daily_final_sd": round(st.stdev(finals), 2),
           "daily_final_share_lt5": round(sum(f < 5 for f in finals) / len(finals), 3),
           "daily_final_share_ge40": round(sum(f >= 40 for f in finals) / len(finals), 3), "by_span": {}}
    for span, cells in res.items():
        out["by_span"][span] = {}
        for k, c in cells.items():
            if not c["n"]:
                continue
            out["by_span"][span][k] = {"n": c["n"], "mean_log_score": {str(phi): round(st.mean(v), 4) for phi, v in c["ls"].items()},
                                       "coverage_central80_phi1": round(c["cov80"] / c["n"], 3),
                                       "pred_P_lt5": round(st.mean(c["p_lt5"]), 3), "real_lt5": round(c["y_lt5"] / c["n"], 3),
                                       "pred_P_ge40": round(st.mean(c["p_ge40"]), 3), "real_ge40": round(c["y_ge40"] / c["n"], 3),
                                       "pit_share_below_0.1": round(sum(p < 0.1 for p in c["pit"]) / c["n"], 3),
                                       "pit_share_above_0.9": round(sum(p > 0.9 for p in c["pit"]) / c["n"], 3)}
    (L.OUT / "calibration_descriptive.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return out


_raw = json.loads(POSTS.read_text())
TS = sorted(p["ts"] for p in _raw.values())

if __name__ == "__main__":
    main()
