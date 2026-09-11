"""Do the logged indicators predict anything? Joins data/features.jsonl (15 s snapshots)
with BTC 5m window outcomes and with the bots' own trades.

  .venv/bin/python -m lab.features_study
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lab import data as labdata  # noqa: E402

FEATURES = ["funding_rate", "funding_premium", "perp_premium_bps", "coinbase_vs_oracle_bps", "oi_change_1h_pct", "long_short_ratio",
            "taker_buy_ratio_5m", "liq_long_5m", "liq_short_5m", "dvol", "realized_sigma_5m_pct_1h", "ret_15m_bps", "ret_60m_bps", "fear_greed"]


def load_features():
    rows = []
    for line in open(ROOT / "data" / "features.jsonl"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("ts"):
            rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows


def snapshot_before(rows, ts_list, t):
    i = bisect.bisect_right(ts_list, t) - 1
    if i < 0 or t - ts_list[i] > 120:
        return None
    return rows[i]


def outcomes():
    out = {}
    w = labdata.load_windows("btc", 5)
    for e, r in w.items():
        if "up_won" in r:
            out[e] = bool(r["up_won"])
    try:
        res = json.loads((ROOT / "data" / "resolutions.json").read_text())
    except Exception:
        res = {}
    try:
        oc = json.loads((ROOT / "lab" / "cache" / "outcomes.json").read_text())
    except Exception:
        oc = {}
    for slug, up in oc.items():
        if slug.startswith("btc-updown-5m-"):
            out.setdefault(int(slug.rsplit("-", 1)[1]), bool(up))
    return out


def auc(pairs):
    """pairs: (score, label). Rank-based AUC."""
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return None
    ranked = sorted(pairs, key=lambda p: p[0])
    # average ranks with ties
    ranks = {}
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and ranked[j + 1][0] == ranked[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    rank_sum_pos = sum(ranks[k] for k, (s, y) in enumerate(ranked) if y)
    return (rank_sum_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def quintiles(pairs, q=5):
    s = sorted(pairs, key=lambda p: p[0])
    n = len(s)
    out = []
    for k in range(q):
        chunk = s[k * n // q:(k + 1) * n // q]
        if chunk:
            out.append((round(statistics.fmean(p[0] for p in chunk), 4), round(statistics.fmean(1.0 if p[1] else 0.0 for p in chunk), 3), len(chunk)))
    return out


def main():
    rows = load_features()
    ts_list = [r["ts"] for r in rows]
    outs = outcomes()
    lo, hi = ts_list[0], ts_list[-1]
    windows = [e for e in outs if lo <= e + 60 <= hi]
    print(f"features {len(rows)} rows over {(hi - lo) / 3600:.1f} h · BTC 5m windows with outcome inside that span: {len(windows)}")
    up_rate = statistics.fmean(1.0 if outs[e] else 0.0 for e in windows)
    print(f"base rate P(up) = {up_rate:.3f}\n")
    print("== Does the indicator at window start + 60 s predict the window outcome (Up)?  AUC 0.5 = nothing")
    print(f"{'feature':26s}{'n':>6s}{'AUC':>7s}  quintile means -> P(up)")
    for f in FEATURES:
        pairs = []
        for e in windows:
            snap = snapshot_before(rows, ts_list, e + 60)
            if snap and snap.get(f) is not None:
                pairs.append((float(snap[f]), outs[e]))
        if len(pairs) < 100:
            continue
        a = auc(pairs)
        se = math.sqrt((a * (1 - a) + 0.25) / len(pairs)) if a else 0
        print(f"{f:26s}{len(pairs):6d}{a:7.3f}  {quintiles(pairs)}  {'*' if abs(a - 0.5) > 2 * se else ''}")

    # --- the bot's own trades vs the trend at entry
    print("\n== Scout paper trades: outcome by 60-minute drift alignment at entry")
    L = json.load(open(ROOT / "data" / "ledger.json"))
    entries, trades = {}, []
    for fl in L["fills"]:
        if fl["side"].startswith("CLOSE_"):
            e = entries.pop(fl["market_id"], None)
            if e:
                trades.append({"ts": e.get("epoch_ts") or datetime.fromisoformat(e["ts"]).timestamp(), "side": fl["side"][6:], "pnl": fl["pnl"], "cost": e.get("cost_basis", e["stake"]), "price": e["price"]})
        else:
            entries[fl["market_id"]] = fl
    joined = []
    for t in trades:
        snap = snapshot_before(rows, ts_list, t["ts"])
        if snap and snap.get("ret_60m_bps") is not None:
            joined.append((t, snap))
    print(f"trades with a feature snapshot: {len(joined)} of {len(trades)}")
    for key in ("ret_60m_bps", "ret_15m_bps"):
        for thr in (0, 10, 25):
            aligned, against, flat = [], [], []
            for t, snap in joined:
                d = float(snap[key])
                if abs(d) <= thr:
                    flat.append(t)
                elif (d > 0 and t["side"] == "YES") or (d < 0 and t["side"] == "NO"):
                    aligned.append(t)
                else:
                    against.append(t)
            def stat(xs):
                return f"n={len(xs):3d} win={sum(x['pnl'] > 0 for x in xs) / max(1, len(xs)):.2f} pnl={sum(x['pnl'] for x in xs):+7.1f} ret/$={statistics.fmean(x['pnl'] / x['cost'] for x in xs) if xs else 0:+.3f}"
            print(f"  {key} |thr|={thr:2d}:  with-trend {stat(aligned)} | against {stat(against)} | flat {stat(flat)}")

    # --- arena: same test on the unfiltered underdog fills (bigger n, same days)
    print("\n== Arena lag_raw_dog fills (btc 5m, ask 0.25-0.40): by 60-minute drift alignment")
    A = json.load(open(ROOT / "data" / "lab" / "ledgers.json"))
    fills = [f for f in A["lag_raw_dog"]["fills"] if f["asset"] == "btc" and 0.25 <= f["price"] < 0.40]
    for thr in (0, 10, 25):
        aligned, against, flat = [], [], []
        for f in fills:
            snap = snapshot_before(rows, ts_list, f["opened_ts"])
            if not snap or snap.get("ret_60m_bps") is None:
                continue
            d = float(snap["ret_60m_bps"])
            (flat if abs(d) <= thr else aligned if (d > 0) == (f["side"] == "YES") else against).append(f)
        def st2(xs):
            return f"n={len(xs):3d} win={sum(x['won'] for x in xs) / max(1, len(xs)):.2f} ret/$={statistics.fmean(x['ret'] for x in xs) if xs else 0:+.3f}"
        print(f"  |thr|={thr:2d}:  with-trend {st2(aligned)} | against {st2(against)} | flat {st2(flat)}")
    print("\n== Arena lag_raw_dog: by taker_buy_ratio_5m alignment (buy-pressure with our side)")
    for thr in (0.55, 0.60):
        aligned, against, neutral = [], [], []
        for f in fills:
            snap = snapshot_before(rows, ts_list, f["opened_ts"])
            if not snap or snap.get("taker_buy_ratio_5m") is None:
                continue
            r = float(snap["taker_buy_ratio_5m"])
            if 1 - thr < r < thr:
                neutral.append(f)
            elif (r >= thr) == (f["side"] == "YES"):
                aligned.append(f)
            else:
                against.append(f)
        print(f"  ratio thr {thr}: with-flow {st2(aligned)} | against {st2(against)} | neutral {st2(neutral)}")


if __name__ == "__main__":
    main()
