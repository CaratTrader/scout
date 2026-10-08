"""quake_entertain (b): Rotten Tomatoes (KXRT) late-window check.

Settlement: Tomatometer at 10:00 ET on the settlement Monday, the same instant the market closes, so there is no window
in which the settlement number is known and the market is still open. The only possible observation edge is that the
score shown on the RT page in the last hours almost always equals the settlement score; a market that is slow to price
that would show the favourite side underpriced late. No timestamped RT score history is free, so the tradable rule is
price-only (favourite band at a fixed time before close) and the look-ahead diagnostic (distance of the strike from the
FINAL score) is reported only as an upper bound, never as a strategy.
Usage: python lab/kalshi/strategies/quake_entertain_rt.py"""
from __future__ import annotations
import json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.quake_entertain import OUT, quote, fee_order, summarize, fmt

TAUS = (720, 360, 180, 60, 30, 10)


def load():
    ms = [json.loads(l) for l in (OUT / "rt_markets.jsonl").open()]
    cs = {}
    for l in (OUT / "rt_candles.jsonl").open():
        x = json.loads(l); cs[x["t"]] = sorted(x["c"])
    return ms, cs


def rows(ms, cs):
    out = []
    for m in ms:
        c = cs.get(m["t"], [])
        if m["result"] not in ("yes", "no") or not c:
            continue
        for tau in TAUS:
            t = m["close"] - tau * 60
            q = quote(c, t + 60)
            if not q:
                continue
            ask, bid, _ = q
            won_yes = m["result"] == "yes"
            for side, px, won in (("YES", ask, won_yes), ("NO", 1 - bid, not won_yes)):
                if not (0.01 <= px <= 0.99):
                    continue
                pnl = (1.0 if won else 0.0) - px - fee_order(px)
                dist = None
                try:
                    dist = abs(float(m["value"]) - float(m["floor"]))
                except Exception:
                    pass
                out.append({"e": m["e"], "t_close": m["close"], "tau": tau, "side": side, "px": px, "won": won, "ret": pnl / px, "dist": dist, "k": m["floor"]})
    return out


def main():
    ms, cs = load()
    R = rows(ms, cs)
    ev_close = sorted({m["close"] for m in ms})
    evs = sorted({(m["close"], m["e"]) for m in ms})
    cut = evs[int(len(evs) * 0.7)][0]
    disc = [r for r in R if r["t_close"] < cut]; val = [r for r in R if r["t_close"] >= cut]
    print(f"KXRT events {len(evs)} markets {len(ms)} with candles {sum(1 for m in ms if cs.get(m['t']))}; cut at event #{int(len(evs)*0.7)}")
    res = {"cells": {}, "diag": {}}
    print("\nDISCOVERY: favourite side (px in band) by minutes before close")
    for tau in TAUS:
        for lo, hi in ((0.50, 0.70), (0.70, 0.90), (0.90, 0.97)):
            x = [r for r in disc if r["tau"] == tau and lo <= r["px"] < hi]
            s = summarize(x); res["cells"][f"disc|{tau}|{lo}-{hi}"] = s
            print(fmt(f"tau={tau} fav px {lo}-{hi}", s))
    print("\nLOOK-AHEAD DIAGNOSTIC (all events; strike >= 3 points from the FINAL score; NOT tradable): correct-side price")
    for tau in TAUS:
        x = [r for r in R if r["tau"] == tau and r["dist"] is not None and r["dist"] >= 3 and r["won"]]
        if x:
            pxs = sorted(r["px"] for r in x)
            cheap = sum(1 for p in pxs if p <= 0.90)
            res["diag"][tau] = {"n": len(x), "median_px": st.median(pxs), "share_le_0.90": cheap / len(x)}
            print(f"  tau={tau:4d} n={len(x):4d} median correct-side px={st.median(pxs):.3f} share <=0.90: {cheap/len(x):.1%}")
    return res, disc, val


if __name__ == "__main__":
    main()
