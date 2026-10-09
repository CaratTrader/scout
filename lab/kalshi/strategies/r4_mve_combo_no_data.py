"""Data for r4_mve_combo_no: random-time samples of exchange-wide trade prints, keeping the multivariate-combo (KXMVE*)
fills, joined to each combo market's metadata and result.

Why trade prints: the feasibility probe (calls 1-7, see calls.log) found that combos have NO secondary book. Of 1,037
open combos (KXMVECROSSCATEGORY + KXMVESPORTSMULTIGAMEEXTENDED) none showed a resting bid or ask, and every fill is an
RFQ fill (a requester accepts a market maker's two-sided quote). The only NO price a home bot can get is therefore an
RFQ quote, and the only public record of such quotes is the prints where the taker took the NO side
(taker_side == "no": new NO positions and YES holders cashing out, both pay 1 - yes_price).

Sampling: N_WIN window starts drawn uniformly (seed 4) over [2026-09-08, 2026-10-07) UTC; per window one
/markets/trades?min_ts=T&max_ts=T+600&limit=1000 call (it returns the first ~5-60 s of the window), then one
/markets?tickers=... call per <=180 combo tickers. Output: data/kalshi_lab/strategies/r4_mve_combo_no/fills.jsonl
(one combo fill per line, with its market) and markets.json.
Usage: python lab/kalshi/strategies/r4_mve_combo_no_data.py [N_WIN]"""
from __future__ import annotations
import datetime as dt, json, random, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from r4_mve_combo_no_api import kget, used, OUT

START = int(dt.datetime(2026, 9, 8, tzinfo=dt.timezone.utc).timestamp())
END = int(dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc).timestamp())


def compact(m: dict) -> dict:
    return {"t": m["ticker"], "coll": m.get("mve_collection_ticker"), "created": m["created_time"], "close": m["close_time"],
            "exp": m.get("expected_expiration_time"), "latest_exp": m.get("latest_expiration_time"), "status": m["status"],
            "result": m.get("result", ""), "vol": float(m.get("volume_fp") or 0), "oi": float(m.get("open_interest_fp") or 0),
            "legs": [[l["market_ticker"], l["side"], l.get("event_ticker")] for l in m.get("mve_selected_legs") or []]}


def main(n_win: int) -> None:
    rnd = random.Random(4)
    starts = sorted(rnd.randrange(START, END) for _ in range(n_win))
    mfile = OUT / "markets.json"
    markets = json.loads(mfile.read_text()) if mfile.exists() else {}
    fills = []
    for i, T in enumerate(starts):
        try:
            d = kget(f"/markets/trades?limit=1000&min_ts={T}&max_ts={T + 600}")
        except RuntimeError as e:
            print("stop:", e); break
        tr = d.get("trades") or []
        combo = [t for t in tr if t["ticker"].startswith("KXMVE")]
        need = sorted({t["ticker"] for t in combo} - set(markets))
        for j in range(0, len(need), 180):
            try:
                r = kget("/markets?limit=1000&tickers=" + ",".join(need[j:j + 180]))
            except RuntimeError as e:
                print("stop:", e); break
            for m in r.get("markets") or []:
                markets[m["ticker"]] = compact(m)
        for t in combo:
            fills.append({"win": T, "n_trades_win": len(tr), "ticker": t["ticker"], "ts": t["created_time"], "side": t["taker_side"],
                          "yes": float(t["yes_price_dollars"]), "count": float(t["count_fp"]), "block": t.get("is_block_trade", False)})
        span = (tr[0]["created_time"], tr[-1]["created_time"]) if tr else ("", "")
        print(i, dt.datetime.utcfromtimestamp(T).isoformat(), "trades", len(tr), "combo", len(combo), "span", span[1][11:19], span[0][11:19], "calls", used(), flush=True)
        mfile.write_text(json.dumps(markets))
    (OUT / "fills.jsonl").write_text("".join(json.dumps(f) + "\n" for f in fills))
    print("fills", len(fills), "markets", len(markets), "calls", used())


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 75)
