"""r7_public_tally_count_ladder_census - forward-only summary and gate (no Kalshi calls). Reads settled.jsonl written by
r7_public_tally_count_ladder_census_logger settle; forced/test passes never reach it.

Forward-only gate (preregistration.json; docs/KALSHI_LAB.md rows 1-7 with amendments (d), (k), (l)), PRIMARY rule C1:
  independent events      n_events >= 40 (one trade per event: the first C1 fill of each event; series pooled)
  mean per $              >= +10% after the taker fee (equal-$ stakes)
  significance            t (sample sd, clustered by event) >= z(0.05 / K), K from data/kalshi_lab/K.json on review day
  Beta bound              one-sided 95% Clopper-Pearson lower bound on the event win rate, converted to a per-$ return
                          at the average price and fee, > 0
  not one lucky trade     mean > 0 without the 3 best events; both halves (by settlement order) > 0
  capacity                median top-of-book size at the signal price >= 5 contracts
Kill rule (checked every summary): after >= 20 settled events, mean per $ < 0 -> KILL; any wrong-side fill from a
data/code error -> KILL; after >= 40 events, t < 1.0 -> KILL. Diagnostics C2/C3/P1 are reported, never gated.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_public_tally_count_ladder_census_summary [--K 20175]"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_data import OUT, ROOT  # noqa: E402

MIN_EVENTS, MIN_RET, KILL_AFTER, KILL_T_AFTER = 40, 0.10, 20, 40


def _binom_sf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided (1 - alpha) Clopper-Pearson lower bound = Beta(alpha; k, n - k + 1) quantile."""
    if k == 0 or n == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(80):
        m = (lo + hi) / 2
        if _binom_sf(k, n, m) < alpha:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def first_per_event(rows: list[dict]) -> list[dict]:
    first = {}
    for r in sorted(rows, key=lambda r: (r["ts"], r["t"])):
        first.setdefault(r["event"], r)
    return sorted(first.values(), key=lambda r: r["ts"])


def stats(rows: list[dict], z: float) -> dict:
    ev = first_per_event(rows)
    if not ev:
        return {"n_events": 0}
    R = [r["ret_per_dollar"] for r in ev]; n = len(R); mean = st.mean(R)
    sd = st.stdev(R) if n > 1 else float("nan")
    t = mean / (sd / math.sqrt(n)) if n > 2 and sd > 0 else float("nan")
    k = sum(r["won"] for r in ev); q = st.mean(r["px"] for r in ev); fee = st.mean(r["fee"] for r in ev)
    lo = cp_lower(k, n); ret_lo = (lo - q - fee) / q
    srt = sorted(R, reverse=True); h = n // 2
    caps = [r.get("size_at_px") for r in ev if r.get("size_at_px") is not None]
    out = {"n_events": n, "n_trades_all": len(rows), "win": k / n, "avg_px": q, "mean_ret_per_dollar": mean, "t": t,
           "ret_wo3": st.mean(srt[3:]) if n > 3 else float("nan"), "half1": st.mean(R[:h]) if h else float("nan"),
           "half2": st.mean(R[h:]) if n - h else float("nan"), "win_lo95": lo, "ret_at_win_lo95": ret_lo,
           "median_capacity_contracts": st.median(caps) if caps else None,
           "by_series": {s: {"n": len(x), "mean": st.mean(r["ret_per_dollar"] for r in x)}
                         for s in sorted({r["series"] for r in ev}) for x in [[r for r in ev if r["series"] == s]]}}
    out["gate"] = {"n": n >= MIN_EVENTS, "mean": mean >= MIN_RET, "t": (t >= z) if t == t else False, "beta": ret_lo > 0,
                   "wo3": out["ret_wo3"] > 0 if n > 3 else False,
                   "halves": (out["half1"] > 0 and out["half2"] > 0) if n > 1 else False,
                   "capacity": (out["median_capacity_contracts"] or 0) >= 5}
    out["gate_pass"] = all(out["gate"].values())
    return out


def kill(c1: dict, wrong_side: int) -> str | None:
    n = c1.get("n_events", 0)
    if wrong_side:
        return f"{wrong_side} wrong-side fill(s) from a data/code error"
    if n >= KILL_AFTER and c1["mean_ret_per_dollar"] < 0:
        return f"mean per $ {c1['mean_ret_per_dollar']:+.3f} < 0 after {n} events"
    if n >= KILL_T_AFTER and not (c1["t"] >= 1.0):
        return f"t {c1['t']:.2f} < 1 after {n} events"
    return None


def main(K: int | None = None, rows: list[dict] | None = None, write: bool = True) -> dict:
    if K is None:
        try:
            K = int(json.loads((ROOT / "data/kalshi_lab/K.json").read_text()).get("K"))
        except (OSError, ValueError, TypeError):
            K = 20175
    z = NormalDist().inv_cdf(1 - 0.05 / K)
    if rows is None:
        f = OUT / "settled.jsonl"
        rows = [json.loads(x) for x in f.open()] if f.exists() else []
    wrong = sum(1 for r in rows if r.get("wrong_side_error"))
    out = {"K": K, "bar_t": z, "rules": {}}
    for name in ("C1", "C2", "C3", "P1"):
        out["rules"][name] = stats([r for r in rows if r["rule"] == name], z)
    out["primary"] = "C1"
    out["kill"] = kill(out["rules"]["C1"], wrong)
    out["verdict"] = ("KILLED: " + out["kill"]) if out["kill"] else ("GATE PASS (forward)" if out["rules"]["C1"].get("gate_pass") else "running")
    if write:
        (OUT / "summary.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    a = sys.argv
    r = main(int(a[a.index("--K") + 1]) if "--K" in a else None)
    print(json.dumps({"verdict": r["verdict"], "bar_t": round(r["bar_t"], 2), "C1": r["rules"]["C1"]}, indent=1, default=str))
