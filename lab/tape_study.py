"""Trade-tape study for the resting-bid version of the lock.

For recent closed 5m windows, pull the public trade prints (data-api) and measure, inside the
last 55 s, how many dollars traded on the WINNING side at prices <= 0.99 (a resting bid at
0.99 on the favourite would have been filled by those sellers, fee 0) and how the prints split
by price. This is the number the order-book snapshots cannot give: fills at the bid.

  .venv/bin/python -m lab.tape_study --assets btc,eth,sol --days 2 --max-windows 300
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab import data as labdata  # noqa: E402

OUT = ROOT / "lab" / "results"


def gamma_market(market_id: str) -> dict:
    rows = labdata.get(f"https://gamma-api.polymarket.com/markets/{market_id}")
    return rows if isinstance(rows, dict) else {}


def trades_for(condition_id: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while offset < 3000:
        rows = labdata.get(f"https://data-api.polymarket.com/trades?market={condition_id}&limit=1000&offset={offset}")
        if not isinstance(rows, list) or not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="btc,eth,sol")
    ap.add_argument("--mins", type=int, default=5)
    ap.add_argument("--days", type=float, default=2.0)
    ap.add_argument("--max-windows", type=int, default=300)
    ap.add_argument("--last-seconds", type=float, default=55.0)
    args = ap.parse_args()
    since = time.time() - args.days * 86400
    per_window: list[dict] = []
    price_hist: Counter = Counter()
    for asset in args.assets.split(","):
        windows = labdata.load_windows(asset, args.mins)
        rows = [w for e, w in sorted(windows.items()) if e >= since and "up_won" in w and w.get("market_id")]
        rows = rows[-args.max_windows :]
        print(f"{asset}: {len(rows)} windows", flush=True)
        for w in rows:
            epoch = int(w["epoch"]); end = epoch + args.mins * 60
            try:
                m = gamma_market(str(w["market_id"]))
                cid = m.get("conditionId")
                if not cid:
                    continue
                trades = trades_for(cid)
            except Exception as exc:
                print("  fetch failed", asset, epoch, type(exc).__name__, flush=True)
                continue
            winner = "Up" if w["up_won"] else "Down"
            late = [t for t in trades if end - args.last_seconds <= float(t.get("timestamp", 0)) < end]
            # each print: side (BUY/SELL from the taker's view), outcome (Up/Down), price, size (shares)
            win_buys = [t for t in late if t.get("outcome") == winner and t.get("side") == "BUY"]
            win_sells = [t for t in late if t.get("outcome") == winner and t.get("side") == "SELL"]
            # a resting 0.99 bid on the winner is filled by taker SELLs of the winner at <= 0.99
            fillable = sum(float(t["size"]) * float(t["price"]) for t in win_sells if float(t["price"]) <= 0.99)
            fillable98 = sum(float(t["size"]) * float(t["price"]) for t in win_sells if float(t["price"]) <= 0.98)
            for t in late:
                if t.get("outcome") == winner:
                    price_hist[(t.get("side"), round(float(t["price"]), 2))] += float(t["size"])
            per_window.append({
                "asset": asset, "epoch": epoch, "winner": winner, "late_trades": len(late),
                "winner_taker_buy_usd": round(sum(float(t["size"]) * float(t["price"]) for t in win_buys), 2),
                "winner_taker_sell_usd": round(sum(float(t["size"]) * float(t["price"]) for t in win_sells), 2),
                "sell_at_or_below_099_usd": round(fillable, 2), "sell_at_or_below_098_usd": round(fillable98, 2),
            })
    n = len(per_window)
    if not n:
        print("no windows"); return
    def share(key, thresh):
        return sum(1 for r in per_window if r[key] >= thresh) / n
    print(f"\nwindows analysed: {n}")
    print(f"windows with ANY winner-side taker sells at <=0.99 in the last {args.last_seconds:.0f}s: {share('sell_at_or_below_099_usd', 0.01):.0%}")
    print(f"  >= $5 of them: {share('sell_at_or_below_099_usd', 5):.0%}   >= $20: {share('sell_at_or_below_099_usd', 20):.0%}   >= $100: {share('sell_at_or_below_099_usd', 100):.0%}")
    print(f"windows with >= $5 sold at <=0.98: {share('sell_at_or_below_098_usd', 5):.0%}")
    med = sorted(r["sell_at_or_below_099_usd"] for r in per_window)
    print(f"median $ sold at <=0.99 per window: {med[n // 2]:.0f}   p75: {med[int(n * .75)]:.0f}   p90: {med[int(n * .9)]:.0f}")
    print("winner-side prints in the last 55 s by (taker side, price), shares:")
    for (side, px), sz in sorted(price_hist.items(), key=lambda kv: -kv[1])[:14]:
        print(f"  {side:4} @ {px:.2f}: {sz:,.0f}")
    OUT.mkdir(exist_ok=True)
    path = OUT / f"tape_{time.strftime('%Y%m%d_%H%M')}.json"
    path.write_text(json.dumps({"args": vars(args), "windows": per_window}, indent=1))
    print("written", path)


if __name__ == "__main__":
    main()
