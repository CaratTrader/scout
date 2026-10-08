"""microstructure: POST-HOC robustness diagnostics of the frozen rain NO maker rule (not used for choosing anything):
fill-model variants (touch / through 1c / through 2c / no taker fallback / fill-minute volume), spread and lifetime
variants, price bands, per day and city, first fill per market, the owner live profile (3 open positions), and the pooled
unselected sample (validation + holdout 1 + holdout 2). Run: .venv/bin/python lab/kalshi/strategies/microstructure_pool.py"""
import json, sys, statistics as st, time
sys.path.insert(0, "/Users/roomyhome/Tradeinc")
from collections import defaultdict
import lab.kalshi.strategies.microstructure as M
from lab.kalshi.strategies.microstructure_data import load_series, event_close, Grid
from lab.kalshi.fetch import PLAN, window

def rain_grids():
    """period -> grids: disc / val (my data) and oos (Aug 8-22 from rain_history cache)"""
    gs = load_series("KXRAIN"); ec = event_close(gs); closes = sorted(ec.values()); cut = closes[int(len(closes) * 0.7)]
    out = {"disc": [g for g in gs if ec[g.e] < cut], "val": [g for g in gs if ec[g.e] >= cut]}
    first = min(g.close for g in gs)
    meta = {}
    for l in open("/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/rain_history/markets.jsonl"):
        m = json.loads(l)
        if m.get("series") == "KXRAIN" and m.get("result") in ("yes", "no") and m["close"] < first:
            m.setdefault("type", "greater"); meta[m["t"]] = m
    oos = []; seen = set()
    lines = [json.loads(l) for l in open("/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/rain_history/candles.jsonl")]
    got = {x["t"] for x in lines}
    per = defaultdict(set)
    for t, m in meta.items(): per[m["e"]].add(t)
    meta = {t: m for t, m in meta.items() if per[m["e"]] <= got}
    for x in lines:
        m = meta.get(x["t"])
        if not m or not x["c"] or x["t"] in seen: continue
        seen.add(x["t"]); w = window(m, PLAN["KXRAIN"])
        c = [r for r in x["c"] if w[0] <= r[0] <= w[1] + 60]
        if c:
            g = Grid(m, "rain", c, w[1])
            if g.n >= 2: oos.append(g)
    out["oos"] = oos
    return out

def maker(g, S, H, side, fillmode="through", min_vol=0.0, allow_taker=True):
    out = []; last = -10**12
    for i in range(0, g.n - 2):
        if not g.fresh[i] or g.mid[i] is None: continue
        a, b = g.ask[i], g.bid[i]; T = g.T(i)
        if a - b < S - 1e-9 or T - last < M.COOLDOWN: continue
        P = round(b + 0.01, 4) if side == "YES" else round(1 - (a - 0.01), 4)
        if not (0.05 <= P <= 0.95): continue
        last = T; j = i + 1
        if g.ask[j] is None or g.bid[j] is None: continue
        cross = g.ask[j] <= P if side == "YES" else (1 - g.bid[j]) <= P
        px = None; taker = False
        if cross:
            if not allow_taker: continue
            px = g.ask[j] if side == "YES" else 1 - g.bid[j]; taker = True
        else:
            thr = {"through": 0.01, "touch": 0.0, "through2": 0.02}[fillmode]
            for x in range(j + 1, min(g.n, j + 1 + H)):
                if not g.fresh[x] or g.vol[x] < min_vol: continue
                if side == "YES" and g.alow[x] is not None and g.alow[x] <= P - thr + 1e-9: px = P; break
                if side == "NO" and g.bhigh[x] is not None and (1 - g.bhigh[x]) <= P - thr + 1e-9: px = P; break
        if px is None or not (0.01 <= px <= 0.99): continue
        won = g.won if side == "YES" else not g.won
        n = max(1, int(5 / px)); f = M.fee_order(px, n) if taker else 0.0
        out.append({"e": g.e, "m": g.t, "t": T, "px": px, "won": won, "ret": ((1.0 if won else 0.0) - px - f) / px, "taker": taker, "city": g.t.rsplit("-", 1)[1]})
    return out

def taker_at_wide(g, S, side):
    out = []; last = -10**12
    for i in range(0, g.n - 2):
        if not g.fresh[i] or g.mid[i] is None: continue
        a, b = g.ask[i], g.bid[i]; T = g.T(i)
        if a - b < S - 1e-9 or T - last < M.COOLDOWN: continue
        last = T
        r = M.fill(g, i, side, "x")
        if r: out.append(r)
    return out

def show(lab, R):
    if len(R) < 5: print(f"  {lab:44s} n={len(R)}"); return
    s = M.stats(R)
    print(f"  {lab:44s} n={s['n']:5d} days={s['events']:3d} win={s['win']:.2f} px={s['px']:.3f} ret={s['ret']:+.1%} t={s['t']:.2f} wo3={s['ret_wo3']:+.1%}")

if __name__ == "__main__":
    G = rain_grids()
    for per in ("disc", "val", "oos"):
        print("==", per, "markets", len(G[per]))
        for S in (0.03, 0.06):
            for H in (5, 30):
                show(f"maker NO S{S} H{H}", [r for g in G[per] for r in maker(g, S, H, "NO")])
        show("maker NO S.06 H30 touch-fill", [r for g in G[per] for r in maker(g, 0.06, 30, "NO", "touch")])
        show("maker NO S.06 H30 through-2c fill", [r for g in G[per] for r in maker(g, 0.06, 30, "NO", "through2")])
        show("maker NO S.06 H30 no taker fallback", [r for g in G[per] for r in maker(g, 0.06, 30, "NO", allow_taker=False)])
        show("maker NO S.06 H30 fill-minute vol>=10", [r for g in G[per] for r in maker(g, 0.06, 30, "NO", min_vol=10)])
        show("maker NO S.09 H30", [r for g in G[per] for r in maker(g, 0.09, 30, "NO")])
        show("maker YES S.06 H30", [r for g in G[per] for r in maker(g, 0.06, 30, "YES")])
        show("taker NO at wide-spread moments S.06", [r for g in G[per] for r in taker_at_wide(g, 0.06, "NO")])
        show("taker YES at wide-spread moments S.06", [r for g in G[per] for r in taker_at_wide(g, 0.06, "YES")])
        R = [r for g in G[per] for r in maker(g, 0.06, 30, "NO")]
        for lo, hi in ((0.05, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 0.96)):
            show(f"   NO px {lo}-{hi}", [r for r in R if lo <= r["px"] < hi])
        by = defaultdict(list)
        for r in R: by[r["e"]].append(r["ret"])
        print("   per day:", " ".join(f"{e[-5:]}:{len(v)}/{st.mean(v):+.2f}" for e, v in sorted(by.items())))
        byc = defaultdict(list)
        for r in R: byc[r["city"]].append(r["ret"])
        print("   per city:", " ".join(f"{c}:{len(v)}/{st.mean(v):+.2f}" for c, v in sorted(byc.items())))
