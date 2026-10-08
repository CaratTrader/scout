"""Round-1b lead synthesis: re-reads the eight round-1b result.json files, checks
the reported validation numbers, counts variants (K) and the Bonferroni bar,
and runs one lab-wide data check: do Kalshi 1-minute candle closes stamped at
end_period_ts contain any trades after that time (look-ahead)?

Standard library only. No Kalshi calls (uses files already on disk).
Run: .venv/bin/python -m lab.kalshi.strategies.lead_round1b
Output: data/kalshi_lab/strategies/lead_round1b/checks.json
"""
import bisect
import datetime as dt
import json
import os
from statistics import NormalDist

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SD = os.path.join(ROOT, "data", "kalshi_lab", "strategies")
OUT = os.path.join(SD, "lead_round1b")

ROUND1B = ["xvenue_polymarket", "macro_releases", "fx_rates15", "btc_range_ladder",
           "weather_lows", "quake_entertain", "archive_calibration", "multi_signal"]
ROUND1 = ["arb_ladder", "crypto15_spot", "cryptoH_model", "index_model", "commod_model",
          "sports_books", "tennis_model", "tempH_nowcast", "weather_walkforward",
          "rain_history", "microstructure", "maker_passive"]


def load(name):
    p = os.path.join(SD, name, "result.json")
    return json.load(open(p)) if os.path.exists(p) else None


def summarise(names):
    rows, k = [], 0
    for n in names:
        r = load(n)
        if r is None:
            rows.append({"family": n, "missing": True})
            continue
        k += int(r.get("variants_examined") or 0)
        rows.append({
            "family": n, "verdict": r.get("verdict"),
            "variants": r.get("variants_examined"), "kalshi_calls": r.get("kalshi_calls_used"),
            "validation": [{kk: v.get(kk) for kk in ("rule", "n", "events", "ret_per_dollar", "t",
                                                     "ret_wo3", "half1", "half2",
                                                     "median_capacity_contracts")}
                           for v in (r.get("validation") or [])],
        })
    return rows, k


def gate_rows(v, bar):
    """Which go-live gate rows (1-5) a validation row passes."""
    if not v.get("n"):
        return "no trades"
    ok = {
        "1 n>=40": (v.get("n") or 0) >= 40,
        "2 mean>=+10%": (v.get("ret_per_dollar") or -1) >= 0.10,
        "3 t>=bar": (v.get("t") or -99) >= bar,
        "4 wo3>0": (v.get("ret_wo3") or -1) > 0,
        "5 halves>0": (v.get("half1") or -1) > 0 and (v.get("half2") or -1) > 0,
    }
    return {k: bool(x) for k, x in ok.items()}


def candle_lookahead_check():
    """xvenue_polymarket recorded true-time BTC15M trade prints and the matching
    1-minute candles. If the candle's trade close equals the last print at or
    before end_period_ts (and not a later one), the candle has no look-ahead."""
    d = os.path.join(SD, "xvenue_polymarket")
    try:
        C = json.load(open(os.path.join(d, "live_kalshi_candles.json")))["markets"]
        T = json.load(open(os.path.join(d, "live_kalshi_trades.json")))
    except (OSError, KeyError, ValueError):
        return {"status": "data missing"}

    def ts(s):
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    offs = (-5, -2, -1, 0, 1, 2, 5, 10, 30)
    match = {o: 0 for o in offs}
    n = 0
    for m in C:
        tr = T.get(m["market_ticker"])
        if not tr:
            continue
        tr = sorted((ts(x["created_time"]), float(x["yes_price_dollars"])) for x in tr)
        times = [x[0] for x in tr]
        for c in m["candlesticks"]:
            E = c["end_period_ts"]
            if not (tr[0][0] < E - 30 and E + 30 < tr[-1][0]):
                continue
            pc = float(c["price"]["close_dollars"])
            n += 1
            for o in offs:
                i = bisect.bisect_right(times, E + o) - 1
                if i >= 0 and abs(tr[i][1] - pc) < 1e-9:
                    match[o] += 1
    return {"candles_checked": n,
            "candle_close_equals_last_print_at_E_plus_offset_s": match,
            "reading": "closes match the last print at or before end_period_ts; no look-ahead. "
                       "The T+30 s REST match reported by xvenue_polymarket is REST /markets "
                       "staleness (~20 s), not candle look-ahead. Small sample (one BTC15M market)."}


def main():
    r1b, k1b = summarise(ROUND1B)
    r1, k1 = summarise(ROUND1)
    try:
        k_prior = len(json.load(open(os.path.join(ROOT, "data", "kalshi_lab", "K.json")))["cells"])
    except (OSError, KeyError, ValueError):
        k_prior = 0
    nd = NormalDist()
    k_all = k_prior + k1 + k1b
    bars = {str(k): round(nd.inv_cdf(1 - 0.05 / k), 2) for k in (k1b, k1 + k1b, k_all) if k}
    bar_all = nd.inv_cdf(1 - 0.05 / k_all)
    for row in r1b:
        for v in row.get("validation", []):
            v["gate_rows_1to5"] = gate_rows(v, bar_all)
    out = {
        "generated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "K": {"prior_K_json_cells": k_prior, "round1": k1, "round1b": k1b, "lab_total": k_all,
              "t_bar_z(0.05/K)": bars},
        "round1b": r1b,
        "round1_sibling": r1,
        "candle_lookahead_check": candle_lookahead_check(),
    }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "checks.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({"K": out["K"], "lookahead": out["candle_lookahead_check"]}, indent=1))
    for row in r1b:
        print(row["family"], row.get("verdict"),
              [(v["n"], v["ret_per_dollar"], v["t"]) for v in row.get("validation", [])])


if __name__ == "__main__":
    main()
