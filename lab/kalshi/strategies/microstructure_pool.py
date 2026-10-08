"""microstructure: POST-HOC robustness diagnostics of the frozen rain NO maker rule (not used for choosing anything):
fill-model variants (touch / through 1c / through 2c / no taker fallback / fill-minute volume), spread and lifetime
variants, price bands, per day and city, first fill per market, the owner live profile (3 open positions), and the pooled
unselected sample (validation + holdout 1 + holdout 2). Run: .venv/bin/python lab/kalshi/strategies/microstructure_pool.py"""
import json, sys, statistics as st
sys.path.insert(0, "/Users/roomyhome/Tradeinc")

from collections import defaultdict
import lab.kalshi.strategies.microstructure as M
from lab.kalshi.strategies.microstructure_data import Grid
from lab.kalshi.fetch import PLAN, window
from lab.kalshi.strategies.microstructure_robust import rain_grids, maker, show

G = rain_grids()
meta = {}
for l in open("/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/rain_history/markets.jsonl"):
    m = json.loads(l)
    if m.get("series") == "KXRAIN" and m.get("result") in ("yes", "no"):
        m.setdefault("type", "greater"); meta[m["t"]] = m
arch = []
for l in open("/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/microstructure/arch_candles.jsonl"):
    x = json.loads(l); m = meta[x["t"]]; w = window(m, PLAN["KXRAIN"])
    c = [r for r in x["c"] if w[0] <= r[0] <= w[1] + 60]
    if c:
        g = Grid(m, "rain", c, w[1])
        if g.n >= 2: arch.append(g)
G["oos2"] = arch
res = {}
for per in ("oos2", "oos", "disc", "val"):
    R = [r for g in G[per] for r in maker(g, 0.06, 30, "NO")]
    res[per] = R
    print("==", per)
    show("E rain NO S.06 H30 (frozen)", R)
    first = {}
    for r in sorted(R, key=lambda r: r["t"]):
        first.setdefault(r["m"], r)
    show("  first fill per market only", list(first.values()))
    for lo, hi in ((0.05, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 0.96)):
        show(f"  NO px {lo}-{hi}", [r for r in R if lo <= r["px"] < hi])
    show("  excluding NO px < 0.30", [r for r in R if r["px"] >= 0.30])
pool = res["val"] + res["oos"] + res["oos2"]
print("== POOLED UNSELECTED (validation + holdout1 + holdout2)")
show("E rain NO S.06 H30", pool)
first = {}
for r in sorted(pool, key=lambda r: r["t"]): first.setdefault(r["m"], r)
show("  first fill per market", list(first.values()))
show("  excluding NO px < 0.30", [r for r in pool if r["px"] >= 0.30])
s = M.stats(pool)
by = defaultdict(list)
for r in pool: by[r["e"]].append(r["ret"])
dm = sorted(st.mean(v) for v in by.values())
print("  days", len(dm), "positive days", sum(1 for x in dm if x > 0), "median day mean", round(st.median(dm), 3))
vol = []
json.dump({"pooled_unselected": M.stats(pool), "first_fill_pooled": M.stats(list(first.values())),
           "periods": {k: M.stats(v) for k, v in res.items()}}, open("/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/microstructure/rain_maker_robustness.json", "w"), indent=1)

print("== LIVE PROFILE: max 3 open positions -> first 3 fills per event-day (chronological), $5 each")
allp = {}
for per in ("oos2", "oos", "disc", "val"):
    R = sorted(res[per], key=lambda r: r["t"])
    by = defaultdict(list)
    for r in R:
        if len(by[r["e"]]) < 3 and all(x["m"] != r["m"] for x in by[r["e"]]):   # one position per market
            by[r["e"]].append(r)
    L = [r for v in by.values() for r in v]
    allp[per] = L
    show(f"{per} first-3 distinct markets/day", L)
    pnl = sum(5.0 * r["ret"] for r in L)
    print(f"     $ pnl on $5 stakes: {pnl:+.1f} over {len(by)} days")
show("UNSELECTED pooled first-3/day", allp["val"] + allp["oos"] + allp["oos2"])
# time to fill and fill-minute volume
