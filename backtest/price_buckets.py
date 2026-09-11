"""Win rate by entry price bucket: does the cheap-longshot pattern lose?"""
import json
from pathlib import Path

BT = Path(__file__).resolve().parent
runs = json.loads((BT / "results.json").read_text())["runs"]

live_fills = json.loads((BT.parent / "data" / "ledger.json").read_text())["fills"]
live = []
entry_by_mkt = {}
for f in live_fills:
    side = str(f.get("side", ""))
    if side.startswith("CLOSE_"):
        e = entry_by_mkt.get(str(f.get("market_id")))
        if e:
            live.append({"ask": float(e["price"]), "won": float(f.get("pnl") or 0) > 0,
                         "pnl_per_$": float(f.get("pnl") or 0) / float(e["stake"])})
    else:
        entry_by_mkt[str(f.get("market_id"))] = f

BUCKETS = [(0.05, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.92)]

def bucketize(trades):
    out = []
    for lo, hi in BUCKETS:
        rows = [t for t in trades if lo <= t["ask"] < hi]
        if not rows:
            out.append((lo, hi, 0, None, None, None))
            continue
        wins = sum(1 for t in rows if t["won"])
        # per-$1-stake return incl 7% fee curve approx already in pnl for backtest rows
        ret = sum(t.get("pnl_per_$", (1 / t["ask"] - 1) if t["won"] else -1) * 1 for t in rows) / len(rows)
        breakeven = sum(t["ask"] for t in rows) / len(rows)  # avg ask ~ needed win prob (pre-fee)
        out.append((lo, hi, len(rows), wins / len(rows), ret, breakeven))
    return out

for run in runs:
    tl = run["trade_log"]
    trades = [{"ask": t["ask"], "won": t["won"], "pnl_per_$": t["pnl"] / t["stake"]} for t in tl]
    print(f"\n== backtest half_spread={run['half_spread']} n={len(trades)} ==")
    print(f"{'ask range':<12}{'n':>6}{'win%':>8}{'ret/$1':>9}{'avg ask':>9}")
    for lo, hi, n, wr, ret, be in bucketize(trades):
        if n:
            print(f"{lo:.2f}-{hi:.2f}  {n:>6}{wr*100:>7.1f}%{ret:>+9.3f}{be:>9.2f}")

print(f"\n== live paper fills n={len(live)} ==")
for lo, hi, n, wr, ret, be in bucketize(live):
    if n:
        print(f"{lo:.2f}-{hi:.2f}  {n:>6}{wr*100:>7.1f}%{ret:>+9.3f}{be:>9.2f}")
