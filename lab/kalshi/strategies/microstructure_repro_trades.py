"""Fill-model check for the rain maker rule with real Kalshi trade prints.

For the FIRST resting order of the frozen E|rain|S0.06|H30|NO rule in each market of a period, fetch the public trade
prints over the order's life [tp, tp+30 min] (one /markets/trades call per market, cached) and compare:
  model fill  = candle trade-through (yes_bid_high >= offer + 1c in a candle wholly after placement)
  print fill  = a taker bought YES at yes_price >= offer after placement (with our offer resting 1c inside the ask we
                would have been first in line for that buyer); 'print10' = such buys add up to >= 10 contracts.
Realistic fills are the union (model or print). Hold to settlement, no maker fee; equal-$ return per order.
Usage: .venv/bin/python -m lab.kalshi.strategies.microstructure_repro_trades [val|oos1] [max_calls]"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
from lab.kalshi.strategies import microstructure_repro as R
from lab.us.data_refresh import fetch, K

API = R.OUT / "api"


def first_orders(meta, cand, S=0.06, H=30, lo=0.05, hi=0.95):
    out = []
    for tk, m in meta.items():
        if tk not in cand: continue
        bk = R.Book(cand[tk]); close = m["close"]; t = close - R.WIN; t -= t % 60
        while t <= close - 120:
            q = bk.quote(t)
            if q and q[0] - q[1] >= S - 1e-9:
                offer = round(q[0] - 0.01, 2); px = round(1 - offer, 2)
                if lo <= px <= hi:
                    tp = t + 60; q1 = bk.quote(tp)
                    crossed = bool(q1 and q1[1] >= offer - 1e-9)
                    end = min(tp + H * 60, close)
                    mf = None
                    if not crossed:
                        for r in bk.c:
                            if tp + 60 <= r[0] <= end and r[4] is not None and r[4] >= offer + 0.01 - 1e-9:
                                mf = r[0]; break
                    out.append({"t": tk, "e": m["e"], "close": close, "tp": tp, "end": end, "offer": offer, "px": px, "won": m["result"] == "no",
                                "crossed": crossed, "model_fill_ts": mf})
                    break
            t += 60
    return out


def trades_for(o, budget):
    f = API / f"trades_{o['t']}_{o['tp']}.json"
    if f.exists():
        return json.loads(f.read_text()), budget
    if budget <= 0:
        return None, budget
    u = f"{K}/markets/trades?ticker={o['t']}&min_ts={o['tp']}&max_ts={o['end']}&limit=1000"
    txt = fetch(u, pace=1.15); budget -= 1
    with (API / "calls.log").open("a") as fh: fh.write(f"{int(time.time())} {u}\n")
    if not txt:
        return None, budget
    d = json.loads(txt); f.write_text(json.dumps(d))
    return d, budget


def main(period="val", max_calls=90):
    API.mkdir(parents=True, exist_ok=True)
    if period == "val":
        meta, cand = R.load(); meta = {k: v for k, v in meta.items() if v["close"] >= R.CUT}
    else:
        mf, cf, sel = R.HOLD["oos1_AUG08-22"]
        meta = {}
        for line in mf.open():
            m = json.loads(line)
            if m.get("series") == "KXRAIN" and m["t"].startswith("KXRAIN-") and sel(m["e"]) and m.get("result") in ("yes", "no"): meta[m["t"]] = m
        cand = {}
        for line in cf.open():
            x = json.loads(line)
            if x["t"] in meta and x["c"]: cand[x["t"]] = sorted(x["c"], key=lambda r: r[0])
    orders = [o for o in first_orders(meta, cand) if not o["crossed"]]   # resting orders only (taker fallback is not a fill-model question)
    orders.sort(key=lambda o: -o["close"])                                 # most recent first, outcome-blind
    budget = max_calls; rows = []
    for o in orders:
        d, budget = trades_for(o, budget)
        if d is None: continue
        tr = d.get("trades") or []
        if len(tr) >= 1000: o["truncated"] = True
        buys = sorted((int(_ts(x["created_time"])), float(x["yes_price_dollars"]), float(x.get("count_fp") or x.get("count") or 0))
                      for x in tr if x.get("taker_side") == "yes")
        hits = [b for b in buys if b[0] > o["tp"] and b[1] >= o["offer"] - 1e-9]
        o["print_fill_ts"] = hits[0][0] if hits else None
        cum = 0; o["print10_fill_ts"] = None
        for b in hits:
            cum += b[2]
            if cum >= 10: o["print10_fill_ts"] = b[0]; break
        o["n_trades"] = len(tr); rows.append(o)
    def st(sel):
        x = [{"e": o["e"], "t": o["t"], "close": o["close"], "ts": o["tp"], "px": o["px"], "won": o["won"], "ret": ((1.0 if o["won"] else 0.0) - o["px"]) / o["px"], "vol": None}
             for o in rows if sel(o)]
        return R.stats(x)
    res = {"period": period, "orders_checked": len(rows), "orders_total": len(orders), "calls_left": budget,
           "model_fill": st(lambda o: o["model_fill_ts"]),
           "print_fill": st(lambda o: o["print_fill_ts"]),
           "print10_fill": st(lambda o: o["print10_fill_ts"]),
           "union_model_or_print": st(lambda o: o["model_fill_ts"] or o["print_fill_ts"]),
           "union_model_or_print10": st(lambda o: o["model_fill_ts"] or o["print10_fill_ts"]),
           "print_only_(model_missed)": st(lambda o: o["print_fill_ts"] and not o["model_fill_ts"]),
           "model_only_(no_print)": st(lambda o: o["model_fill_ts"] and not o["print_fill_ts"]),
           "unfilled_any": st(lambda o: not (o["model_fill_ts"] or o["print_fill_ts"])),
           "all_orders_if_always_filled": st(lambda o: True),
           "truncated_pages": sum(1 for o in rows if o.get("truncated"))}
    (R.OUT / f"trades_check_{period}.json").write_text(json.dumps({"summary": res, "orders": rows}, indent=1))
    for k, v in res.items():
        if isinstance(v, dict) and v.get("n"):
            print(f"{k:30s} n={v['n']:3d} ev={v['events']:3d} win={v['win']:.2f} px={v['avg_px']:.3f} ret={v['ret_per_dollar']:+.1%} tcl={v['t_clustered']} tev={v['t_event_means']} wo3={v['ret_wo3']}")
        else:
            print(k, v)


def _ts(s):
    import datetime as dt
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0] if a else "val", int(a[1]) if len(a) > 1 else 90)
