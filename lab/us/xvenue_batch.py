"""Batch cross-venue study: for resolved Polymarket US moneylines, align 1-min US bid/ask with the global
venue's 1-min price for the same game, then simulate (a) taker convergence trades, (b) resting maker quotes at
global fair, (c) single-venue calibration. Conservative fills: a taker signal must persist into the next minute
and fills at that later ask; a resting quote fills only when the displayed US best price crosses through it.
Usage: python -m lab.us.xvenue_batch <us_markets.json> [max_games] [out.json]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics as st
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.us import xvenue as X  # noqa: E402

CACHE = Path("data/lab/us/series"); CACHE.mkdir(parents=True, exist_ok=True)
TAKER = 0.0695
REBATE = 0.0125


def fee(p: float) -> float:
    return TAKER * p * (1 - p)


def rebate(p: float) -> float:
    return REBATE * p * (1 - p)


def winner_idx(prices_json: str) -> int | None:
    try:
        pr = [float(x) for x in json.loads(prices_json)]
    except Exception:
        return None
    if len(pr) != 2 or pr[0] == pr[1]:
        return None
    return 0 if pr[0] > pr[1] else 1


def load_game(m: dict) -> dict | None:
    slug = m["slug"]; f = CACHE / f"{slug}.json"
    if f.exists():
        return json.loads(f.read_text())
    gs_iso = m.get("gameStartTime")
    if not gs_iso:
        return None
    gs = dt.datetime.fromisoformat(gs_iso.replace("Z", "+00:00")).timestamp()
    gm, idx, how = X.global_match2(m)
    if not gm:
        return {"slug": slug, "how": how}
    # identify the US YES side by the resolved winner on both venues (a fixed fact about the contracts)
    us_w = winner_idx(m.get("outcomePrices") or "[]"); g_w = winner_idx(gm.get("outcomePrices") or "[]")
    if us_w is None or g_w is None:
        return {"slug": slug, "how": "unresolved"}
    idx = g_w if us_w == 0 else 1 - g_w
    us = X.us_series(slug, gs); gl = X.global_series(gm, idx, gs - 6 * 3600, gs + 5 * 3600)
    time.sleep(0.15)
    g = {"slug": slug, "how": how, "league": slug.split("-")[1], "gs": gs, "us_yes_won": us_w == 0, "global_slug": gm["slug"], "global_vol": float(gm.get("volumeNum") or 0),
         "us": {str(k): v for k, v in us.items()}, "gl": {str(k): v for k, v in gl.items()}}
    f.write_text(json.dumps(g))
    return g


def simulate(g: dict, theta: float, delta: float = 0.01) -> dict:
    us = {int(k): v for k, v in g["us"].items()}; gl = {int(k): v for k, v in g["gl"].items()}
    gs = g["gs"]; won = 1.0 if g["us_yes_won"] else 0.0
    ts = sorted(set(us) & set(gl))
    out = {"n_min": len(ts), "taker": [], "maker": [], "snaps": {}}
    if len(ts) < 10:
        return out
    # ---- taker convergence, one open position per side per phase
    for phase, sel in (("pre", [t for t in ts if t < gs]), ("live", [t for t in ts if t >= gs])):
        for side in ("YES", "NO"):
            i = 0
            while i < len(sel) - 1:
                t, t1 = sel[i], sel[i + 1]
                if t1 - t > 120:
                    i += 1; continue
                mid, ask, bid = us[t]; mid1, ask1, bid1 = us[t1]; gp, gp1 = gl[t], gl[t1]
                if side == "YES":
                    sig = ask <= gp - theta and ask1 <= gp1 - theta and 0.02 < ask1 < 0.98
                    px, fair, pay = ask1, gp1, won
                else:
                    sig = bid >= gp + theta and bid1 >= gp1 + theta and 0.02 < 1 - bid1 < 0.98
                    px, fair, pay = 1 - bid1, 1 - gp1, 1 - won
                if not sig:
                    i += 1; continue
                # exit B: first later minute where the US side price is back at fair (sell at our side's bid)
                exit_px = None; exit_t = None
                for tau in sel[i + 2:]:
                    m2, a2, b2 = us[tau]; g2 = gl[tau]
                    side_bid = b2 if side == "YES" else 1 - a2
                    side_fair = g2 if side == "YES" else 1 - g2
                    if side_bid >= side_fair - 0.005:
                        exit_px, exit_t = side_bid, tau; break
                pnl_hold = pay - px - fee(px)
                pnl_exit = (exit_px - px - fee(px) - fee(exit_px)) if exit_px is not None else pnl_hold
                out["taker"].append({"phase": phase, "side": side, "px": px, "fair": fair, "edge": fair - px, "pnl_hold": pnl_hold, "pnl_exit": pnl_exit, "exited": exit_px is not None, "t": t1})
                # skip ahead past the exit (or 30 min) before allowing another entry on this side
                nxt = exit_t if exit_t else t1 + 1800
                while i < len(sel) and sel[i] <= nxt:
                    i += 1
    # ---- maker: rest YES bid at fair-delta and YES ask at fair+delta each minute; fill when the US display price crosses
    for phase, sel in (("pre", [t for t in ts if t < gs]), ("live", [t for t in ts if t >= gs])):
        filled = {"bid": False, "ask": False}
        for i in range(len(sel) - 1):
            t, t1 = sel[i], sel[i + 1]
            if t1 - t > 120:
                continue
            q_bid = round(gl[t] - delta, 3); q_ask = round(gl[t] + delta, 3)
            mid1, ask1, bid1 = us[t1]
            if not filled["bid"] and 0.02 < q_bid < 0.98 and ask1 <= q_bid:
                filled["bid"] = True
                out["maker"].append({"phase": phase, "side": "YES", "px": q_bid, "fair_at_fill": gl[t1], "pnl": won - q_bid + rebate(q_bid), "t": t1})
            if not filled["ask"] and 0.02 < q_ask < 0.98 and bid1 >= q_ask:
                filled["ask"] = True
                pxno = 1 - q_ask
                out["maker"].append({"phase": phase, "side": "NO", "px": pxno, "fair_at_fill": 1 - gl[t1], "pnl": (1 - won) - pxno + rebate(pxno), "t": t1})
    # ---- single-venue calibration snapshots
    snaps = {}
    for lab, t_target in (("pre60", gs - 3600), ("pre5", gs - 300), ("live0", gs + 60)):
        c = [t for t in us if abs(t - t_target) <= 90]
        if c:
            t = min(c, key=lambda x: abs(x - t_target)); snaps[lab] = {"ask": us[t][1], "bid": us[t][2], "won": won}
    out["snaps"] = snaps
    return out


def tstat(xs: list[float]) -> float:
    return (st.mean(xs) / st.pstdev(xs) * math.sqrt(len(xs))) if len(xs) > 2 and st.pstdev(xs) > 0 else float("nan")


def main() -> None:
    import random
    src = sys.argv[1]; per_league = int(sys.argv[2]) if len(sys.argv) > 2 else 10 ** 9; outp = sys.argv[3] if len(sys.argv) > 3 else "data/lab/us/xvenue_results.json"
    allm = [m for m in json.load(open(src)) if m.get("sportsMarketTypeV2") == "SPORTS_MARKET_TYPE_MONEYLINE" and m.get("status") == "MARKET_STATUS_RESOLVED" and m.get("gameStartTime")]
    random.seed(7); by = defaultdict(list)
    for m in allm:
        by[m["slug"].split("-")[1]].append(m)
    ms = []
    for lg, lst in by.items():
        random.shuffle(lst); ms += lst[:per_league]
    trades_path = Path(outp).with_suffix(".trades.jsonl"); tf = open(trades_path, "w")
    games, how = [], defaultdict(int)
    for k, m in enumerate(ms):
        g = load_game(m)
        if not g:
            how["skip"] += 1; continue
        how[g.get("how")] += 1
        if "us" in g and len(g["us"]) >= 30 and len(g["gl"]) >= 30:
            games.append(g)
        if k % 25 == 0:
            print(f"  ... {k}/{len(ms)} matched={len(games)}", flush=True)
    print(f"games with both series: {len(games)} of {len(ms)}  match methods: {dict(how)}")
    res = {"n_games": len(games), "leagues": {}}
    print("games per league:", dict(sorted(((lg, sum(1 for g in games if g["league"] == lg)) for lg in {g["league"] for g in games}), key=lambda kv: -kv[1])))
    for theta in (0.02, 0.03, 0.05):
        rows = defaultdict(lambda: defaultdict(list))
        for g in games:
            s = simulate(g, theta)
            liquid = g.get("global_vol", 0) >= 100_000
            for tr in s["taker"]:
                tf.write(json.dumps({"theta": theta, "slug": g["slug"], "league": g["league"], "gvol": g.get("global_vol", 0), **tr}) + "\n")
                for key in ((g["league"], tr["phase"]), ("ALL", tr["phase"]), ("ALL-liquid" if liquid else "ALL-thin", tr["phase"])):
                    rows[key]["hold"].append(tr["pnl_hold"]); rows[key]["exit"].append(tr["pnl_exit"]); rows[key]["px"].append(tr["px"])
        print(f"\nTAKER convergence, theta={theta:.2f} (buy on US when its price is >= theta below the global price for two consecutive minutes; P&L per share)")
        print(f"{'league':8s} {'phase':5s} {'n':>5s} {'avg px':>7s} {'hold/sh':>8s} {'t':>5s} {'exit/sh':>8s} {'t':>5s} {'win%':>5s}")
        for (lg, ph), r in sorted(rows.items(), key=lambda kv: (-len(kv[1]['hold']))):
            h, e = r["hold"], r["exit"]
            print(f"{lg:8s} {ph:5s} {len(h):5d} {st.mean(r['px']):7.3f} {st.mean(h)*100:+7.2f}c {tstat(h):5.1f} {st.mean(e)*100:+7.2f}c {tstat(e):5.1f} {sum(1 for x in h if x>0)/len(h):5.0%}")
        res[f"taker_{theta}"] = {f"{lg}/{ph}": {"n": len(r["hold"]), "hold": st.mean(r["hold"]), "exit": st.mean(r["exit"]), "t_hold": tstat(r["hold"]), "t_exit": tstat(r["exit"])} for (lg, ph), r in rows.items()}
    # maker
    rows = defaultdict(list); snaps = defaultdict(list)
    for g in games:
        s = simulate(g, 0.03)
        liquid = g.get("global_vol", 0) >= 100_000
        for f in s["maker"]:
            tf.write(json.dumps({"maker": True, "slug": g["slug"], "league": g["league"], "gvol": g.get("global_vol", 0), **f}) + "\n")
            rows[(g["league"], f["phase"])].append(f["pnl"]); rows[("ALL", f["phase"])].append(f["pnl"]); rows[("ALL-liquid" if liquid else "ALL-thin", f["phase"])].append(f["pnl"])
        for lab, sn in s["snaps"].items():
            snaps[lab].append(sn)
    print("\nMAKER at global fair +/- 1c (fills only when the US display price crosses our quote; hold to settlement; rebate included)")
    for (lg, ph), r in sorted(rows.items(), key=lambda kv: -len(kv[1])):
        print(f"{lg:8s} {ph:5s} fills={len(r):4d} pnl/sh={st.mean(r)*100:+6.2f}c t={tstat(r):5.1f} win={sum(1 for x in r if x>0)/len(r):4.0%}")
    res["maker"] = {f"{lg}/{ph}": {"n": len(r), "pnl": st.mean(r), "t": tstat(r)} for (lg, ph), r in rows.items()}
    print("\nSINGLE-VENUE calibration: buy YES at the US ask at snapshot time, taker fee, hold to settlement (by ask bucket)")
    for lab in ("pre60", "pre5", "live0"):
        b = defaultdict(list)
        for sn in snaps[lab]:
            a = sn["ask"]
            if 0.02 < a < 0.98:
                b[round(a * 10) / 10].append(sn["won"] - a - fee(a))
        print(f"  {lab}: " + "  ".join(f"{k:.1f}:{st.mean(v)*100:+.1f}c(n{len(v)})" for k, v in sorted(b.items())))
    tf.close()
    json.dump(res, open(outp, "w"), indent=1)
    print("saved", outp, "and", trades_path)


if __name__ == "__main__":
    main()
