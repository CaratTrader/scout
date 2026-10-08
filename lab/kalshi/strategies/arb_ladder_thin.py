"""arb_ladder pre-registered out-of-sample probe (data/kalshi_lab/strategies/arb_ladder/preregistration_thin_oos.json):
survivor-filtered full-set buys on US daily-LOW temperature ladders (never used for any choice).
  fetch : 1-minute candles over the 13 h before close, batched by close time (<= 10,000 candles per call)
  test  : the frozen rule (ALLYES, thr 0.01, age 60 s, t+60 taker, N=5) + a descriptive grid"""
from __future__ import annotations
import collections, datetime as dt, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.arb_ladder import Mk, scan_event, stats, THRS, AGES

D = Path("data/kalshi_lab/strategies/arb_ladder/thin_oos")
W = 13 * 3600


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fetch_all():
    from lab.kalshi.strategies.arb_ladder_live import call, K
    allm = json.loads((D / "settled_markets.json").read_text())
    have = {}
    cf = D / "candles.json"
    if cf.exists():
        have = json.loads(cf.read_text())
    by_close = collections.defaultdict(list)
    for s, ms in allm.items():
        for m in ms:
            if m["ticker"] not in have:
                by_close[m["close_time"]].append(m["ticker"])
    for c, tick in sorted(by_close.items()):
        hi = ts(c); lo = hi - W
        per = 10000 // (W // 60 + 1)
        for i in range(0, len(tick), per):
            ch = tick[i:i + per]
            d = call(f"{K}/markets/candlesticks?market_tickers={','.join(ch)}&start_ts={lo}&end_ts={hi}&period_interval=1")
            for x in d.get("markets") or []:
                t = x.get("market_ticker") or x.get("ticker")
                g = lambda cc, k: float(cc[k]["close_dollars"]) if (cc.get(k) or {}).get("close_dollars") is not None else None
                have[t] = [[int(cc["end_period_ts"]), g(cc, "yes_ask"), g(cc, "yes_bid")] for cc in x.get("candlesticks") or []]
            cf.write_text(json.dumps(have))
            print(c, len(ch), "tickers ->", sum(1 for t in ch if t in have), flush=True)


def test():
    allm = json.loads((D / "settled_markets.json").read_text()); cand = json.loads((D / "candles.json").read_text())
    ev = collections.defaultdict(list)
    for s, ms in allm.items():
        for m in ms:
            if cand.get(m["ticker"]):
                ev[m["event_ticker"]].append(Mk(m["ticker"], m["event_ticker"], m["ticker"], m["result"] == "yes", cand[m["ticker"]], ts(m["close_time"])))
    rows = []; n_ev = 0
    for e, mks in ev.items():
        if len(mks) != 6:
            continue
        n_ev += 1
        r, _, _ = scan_event("thin_low", "excl", e, mks, thrs=THRS, ages=AGES)
        rows += r
    (D / "episodes.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    ok = [r for r in rows if r.get("fill") == "ok"]
    frozen = [r for r in ok if r["dir"] == "ALLYES" and r["thr"] == 0.01 and r["age"] == 60]
    res = {"events": n_ev, "frozen_rule": stats(frozen), "grid": {}}
    print(f"events with full 6-bucket candles: {n_ev}")
    print("PRE-REGISTERED RULE (ALLYES thr .01 age 60, t+60 taker, N=5):", res["frozen_rule"])
    for k in sorted({(r["dir"], r["thr"], r["age"]) for r in ok}):
        g = [r for r in ok if (r["dir"], r["thr"], r["age"]) == k]
        a = stats(g); res["grid"]["|".join(map(str, k))] = a
        for r in g:
            r["ret0"] = r["pnl0"] / r["cost0"] if r.get("cost0") else None
        print("  descriptive", k, f"n={a['n']} ev={a['events']} ret={a['ret']:+.2%} t={a['t']:.1f} | t0 {stats(g, 'ret0')['ret']:+.2%}")
    (D / "result.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    {"fetch": fetch_all, "test": test}[sys.argv[1]]()
