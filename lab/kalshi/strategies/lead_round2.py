"""lead_round2: lead adjudication of the round-2 hunt (no Kalshi calls; reads other researchers' caches read-only).

Check 1 (r2_mentions_baserate, the only family with a positive validation): the two verifiers disagree.
  Verifier B says the candle fetch window [max(first open, last close - 96 h) - 1 h, ...] made the backtest enter
  ~9% of markets at last_close - 97 h (a time fixed by the event's FUTURE last close) instead of the frozen
  "first top-of-hour >= open + 1 h". Re-run the frozen C1/C2/C3 with the researcher's own code, split into
  rule-consistent entries (fetch window covered open + 1 h) and truncated ones, and by fill class
  (full = >= 2c through with >= 20 lots in the candle; marginal = only 1c-through or thin).
Outputs: data/kalshi_lab/strategies/lead_round2/checks.json
Usage: .venv/bin/python -m lab.kalshi.strategies.lead_round2
"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r2_mentions_baserate as MB

OUT = Path("data/kalshi_lab/strategies/lead_round2")


def day_t(rows):
    """t of day means (cluster by UTC calendar day of the event's last close)."""
    d = defaultdict(list)
    for r in rows:
        d[r["tclose"] // 86400].append(r["ret"])
    m = [st.mean(v) for v in d.values()]
    if len(m) < 3 or st.pstdev(m) == 0:
        return None, len(m)
    return round(st.mean(m) / (st.pstdev(m) / math.sqrt(len(m) - 1)), 2), len(m)


def mentions_check():
    mk, events, C, pool = MB.load()
    # reconstruct each event's fetch-window start exactly as r2_mentions_baserate_data.batch_candles did
    ev_m = defaultdict(list)
    for m in mk:
        ev_m[m["event_ticker"]].append(m)
    lo = {}
    for e, x in ev_m.items():
        cmax = max(m["close_ts"] for m in x); omin = min(m["open_ts"] for m in x)
        lo[e] = (max(omin, cmax - 96 * 3600) // 3600) * 3600 - 3600
    BR = MB.BaseRates(pool); F = MB.load_full()
    out = {}
    for part in ("disc", "val"):
        cands = MB.candidates(mk, events, C, BR, part, "first")
        info = {seq[0]["m"]["ticker"]: seq[0] for seq in cands if seq}
        for name, cfg in MB.FROZEN.items():
            rows, posted = MB.run_maker(cands, F, events, cfg["side"], cfg["thr"], MB.M1_FROZEN, "B")
            rowsA, _ = MB.run_maker(cands, F, events, cfg["side"], cfg["thr"], MB.M1_FROZEN, "A")
            okA = {r["t"] for r in rowsA}
            for r in rows:
                m = info[r["t"]]["m"]
                r["trunc"] = m["open_ts"] < lo[m["event_ticker"]]          # window began after the market's open
                r["lag_h"] = (r["H"] - m["open_ts"]) / 3600
                r["cls"] = MB.fill_class(info[r["t"]], m, F, events)
                r["winA"] = r["t"] in okA
            res = {"posted": posted, "all": MB.cstats(rows)}
            sub = {
                "rule_consistent": [r for r in rows if not r["trunc"]],
                "truncated_entry": [r for r in rows if r["trunc"]],
                "rule_consistent_windowA": [r for r in rows if not r["trunc"] and r["winA"]],
                "rule_consistent_full_fills": [r for r in rows if not r["trunc"] and r["cls"] == "full"],
                "rule_consistent_marginal_fills": [r for r in rows if not r["trunc"] and r["cls"] == "marginal"],
            }
            for k, v in sub.items():
                s = MB.cstats(v)
                s["t_day"], s["days"] = day_t(v) if v else (None, 0)
                res[k] = s
            rc = sub["rule_consistent"]
            res["rule_consistent_median_entry_lag_h"] = round(st.median(r["lag_h"] for r in rc), 2) if rc else None
            res["truncated_share"] = round(len(sub["truncated_entry"]) / len(rows), 3) if rows else None
            out[f"{part}|{name}"] = res
            a, b, c = res["all"], res["rule_consistent"], res["truncated_entry"]
            print(f"[{part}] {name}: all n={a.get('n')} ret={a.get('ret_per_dollar')} t={a.get('t')} | rule-consistent n={b.get('n')} "
                  f"ret={b.get('ret_per_dollar')} t={b.get('t')} t_day={b.get('t_day')} halves={b.get('half1')}/{b.get('half2')} wo3={b.get('ret_wo3')} | "
                  f"truncated n={c.get('n')} ret={c.get('ret_per_dollar')} | full-only ret={res['rule_consistent_full_fills'].get('ret_per_dollar')} "
                  f"(n={res['rule_consistent_full_fills'].get('n')}, t={res['rule_consistent_full_fills'].get('t')}) "
                  f"marginal ret={res['rule_consistent_marginal_fills'].get('ret_per_dollar')} (n={res['rule_consistent_marginal_fills'].get('n')})")
    return out


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    r = {"mentions_window_check": mentions_check()}
    (OUT / "checks.json").write_text(json.dumps(r, indent=1))
    print("wrote", OUT / "checks.json")
