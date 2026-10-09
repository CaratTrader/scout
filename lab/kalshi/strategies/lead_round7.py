"""Round-7 lead check (descriptive only, cached data, zero network calls).

Re-scores the r6 Truth Social weekly C1 rule (the rule the live com.tradeinc.klab.truthsocial logger runs) after:
  A. as coded in r6 (reproduction)
  B. mass-conserving bracket probabilities: pending deletions as Binomial(recent, delta) instead of the fractional
     shift c = seen - delta*recent, which drops one remaining-count value at every bracket edge (ladder sums < 1)
  C. as coded + gate amendment (h): signal and band tested on the decision quote at t, fill at the quote at t + 60 s
  D. B + (h)
Nothing here is a new strategy or a choice; it measures how much of the r6 C1 reference survives the bug fix and (h).
Run: .venv/bin/python -m lab.kalshi.strategies.lead_round7
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r6_truth_social_count_endgame as r6  # noqa: E402
from lab.kalshi.calib import quote  # noqa: E402

OUT = Path(__file__).resolve().parents[3] / "data" / "kalshi_lab" / "strategies" / "lead_round7"


def binom_pmf(n: int, p: float) -> list[float]:
    return [math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(n + 1)]


def recent_count(M: r6.Model, x: dict, P, t: int) -> tuple[int, int]:
    seen = P.seen(x["A"], t); tv = t - getattr(P, "lag", 0)
    lt = dt.datetime.fromtimestamp(t, r6.ET); last = lt.replace(hour=3, minute=0, second=0, microsecond=0)
    if last > lt:
        last -= dt.timedelta(days=1)
    recent = max(0, bisect.bisect_right(P.all, tv) - bisect.bisect_left(P.all, max(int(last.timestamp()), x["A"])))
    return seen, recent


def probs_fixed(M: r6.Model, x: dict, P, t: int) -> dict:
    """Same mu as r6 (dist()), but final = seen - Del + R with Del ~ Bin(recent, delta): integer support, sums to 1."""
    if t >= x["B"]:
        return M.bracket_probs(x, P, t)
    _, _, mu = M.dist(x, P, t)
    seen, recent = recent_count(M, x, P, t)
    kmax = 400; cdf = r6.nb_cdf_table(mu, M.phi, kmax)
    dels = binom_pmf(recent, M.delta)

    def p_range(lo_r: int, hi_r: int) -> float:
        if hi_r < 0:
            return 0.0
        lo_r = max(lo_r, 0); hi_r = min(hi_r, kmax)
        return cdf[hi_r] - (cdf[lo_r - 1] if lo_r > 0 else 0.0)

    out = {}
    for b in x["brackets"]:
        p = 0.0
        for d, w in enumerate(dels):
            if w < 1e-12:
                continue
            base = seen - d
            hi = b["hi"] if b["hi"] < 10 ** 6 else base + kmax
            p += w * p_range(b["lo"] - base, hi - base)
        out[b["t"]] = max(0.0, min(1.0, p))
    return out


def signals(evs, P, M, C, split, probs, amend_h: bool, theta=0.10, lag=120, start_h=24, phase="pre", side_filter="NO",
            step=300, px_lo=0.03, px_hi=0.97, delay=60):
    LP = r6.LaggedPosts(P, lag); out = []; sums = []
    for x in evs:
        if x["split"] != split:
            continue
        have = [b for b in x["brackets"] if b["t"] in C]; done = set()
        for t in r6.grid(x, step=step, start_h=start_h):
            if phase == "pre" and t >= x["B"]:
                break
            pr = None
            for b in have:
                if b["t"] in done:
                    continue
                qd = quote(C[b["t"]], t if amend_h else t + delay)
                qf = quote(C[b["t"]], t + delay)
                if not qd or not qf:
                    continue
                if pr is None:
                    pr = probs(M, x, LP, t); sums.append(sum(pr.values()))
                p = pr[b["t"]]
                for side, pxd, pxf, pw, won in (("YES", qd[0], qf[0], p, b["won"]), ("NO", 1 - qd[1], 1 - qf[1], 1 - p, not b["won"])):
                    if side_filter and side != side_filter:
                        continue
                    if not (px_lo <= pxd <= px_hi) or pw - pxd < theta:
                        continue
                    px = pxf
                    if not (0.0 < px < 1.0):
                        done.add(b["t"]); break
                    f = r6.fee10(px); pnl = (1.0 if won else 0.0) - px - f
                    out.append({"e": x["e"], "t": b["t"], "side": side, "px": px, "px_decision": pxd, "model": round(pw, 4),
                                "won": won, "ret": pnl / px, "t_dec": t, "t_close": x["close"]})
                    done.add(b["t"]); break
    return out, sums


def sample_t(rows):
    """amendment (k): event-clustered t with the sample standard deviation"""
    from collections import defaultdict
    import statistics as st
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    if len(em) < 3 or st.stdev(em) == 0:
        return float("nan")
    return st.mean(em) / (st.stdev(em) / math.sqrt(len(em)))


def main() -> dict:
    evs = r6.events(); P = r6.Posts(); C = r6.load_candles(); M = r6.Model(evs, P, **r6.FROZEN)
    variants = {"A_as_coded": (lambda M_, x, P_, t: M_.bracket_probs(x, P_, t), False),
                "B_mass_conserving": (probs_fixed, False),
                "C_as_coded_plus_h": (lambda M_, x, P_, t: M_.bracket_probs(x, P_, t), True),
                "D_mass_conserving_plus_h": (probs_fixed, True)}
    res = {"note": "descriptive re-score of the r6 weekly C1 rule; post hoc; changes no verdict; zero network calls",
           "rule": r6.CANDIDATES["C1_model_NO_theta10_sat"], "variants": {}}
    keysets = {}
    for name, (fn, h) in variants.items():
        res["variants"][name] = {}
        for split in ("disc", "val"):
            rows, sums = signals(evs, P, M, C, split, fn, h)
            s = r6.stats(rows); s["t_sample_sd"] = sample_t(rows)
            s["ladder_sum_min"] = round(min(sums), 3) if sums else None
            s["ladder_sum_median"] = round(sorted(sums)[len(sums) // 2], 3) if sums else None
            res["variants"][name][split] = {"stats": s, "trades": rows}
            keysets[(name, split)] = {(r["e"], r["t"]) for r in rows}
    for split in ("disc", "val"):
        a = keysets[("A_as_coded", split)]; b = keysets[("B_mass_conserving", split)]
        res[f"overlap_{split}"] = {"as_coded": len(a), "fixed": len(b), "both": len(a & b), "only_as_coded": len(a - b)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "r6_c1_rescore.json").write_text(json.dumps(res, indent=1, default=str))
    for name in variants:
        for split in ("disc", "val"):
            s = res["variants"][name][split]["stats"]
            print(f"{name:26s} {split:4s} n={s.get('n')} ev={s.get('events')} win={s.get('win', 0):.2f} px={s.get('avg_px', 0):.3f} "
                  f"ret={s.get('ret_per_dollar', 0):+.3f} t_pop={s.get('t', float('nan')):.2f} t_sd={s['t_sample_sd']:.2f} "
                  f"wo3={s.get('ret_wo3', float('nan')):+.3f} sum_min={s['ladder_sum_min']} sum_med={s['ladder_sum_median']}")
    print(res["overlap_disc"], res["overlap_val"])
    return res


if __name__ == "__main__":
    main()
