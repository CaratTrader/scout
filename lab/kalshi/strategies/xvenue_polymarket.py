"""xvenue_polymarket: does Kalshi lag Polymarket? Taker strategy "buy the Kalshi side toward the Polymarket price".

Data: Kalshi 1-minute candles on disk (data/kalshi_lab/candles) for crypto 15-minute up/down (BTC, ETH, SOL, XRP) and
sports moneylines (MLB, NFL, NHL, NBA); Polymarket CLOB 1-minute price history for the same markets, cached by
xvenue_polymarket_fetch.py under data/kalshi_lab/strategies/xvenue_polymarket/.

Decision at minute t (a Kalshi candle end): Kalshi quote = last candle <= t; Polymarket price = last history point <= t
(<= 120 s old). Signal YES if pm - yes_ask > thr, NO if yes_bid - pm > thr. Fill as taker at the Kalshi quote at t+60
(candle <= 30 min old, market still open at t+60), fee 0.07 p (1-p) per contract rounded up to the cent on a $5 order,
hold to settlement. One trade per market (crypto) / per game (sports): the first signal.
Split: clusters ordered by close time, discovery = first 70%, validation = last 30%. t-stat clustered by
15-minute window (crypto: all coins of one window move together) or by game (sports).
Usage: python -m lab.kalshi.strategies.xvenue_polymarket"""
from __future__ import annotations
import bisect, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/xvenue_polymarket"
MK = ROOT / "data/kalshi_lab/markets"; CD = ROOT / "data/kalshi_lab/candles"
MAX_AGE = 30 * 60; PM_AGE = 120; STAKE = 5.0; SPLIT = 0.7
# PM_SHIFT: seconds subtracted from Polymarket prices-history stamps (0 = take stamps at face value). 60 is the
# alignment the live recording supports if the history stamps run one minute late (see live check in main()).
import os
PM_SHIFT = int(os.environ.get("PM_SHIFT", "0"))
FILL_DELAY = int(os.environ.get("FILL_DELAY", "60"))   # 60 = protocol; 0 = zero-latency upper bound (diagnostic only)


def fee_pc(p: float) -> float:
    n = max(1, math.floor(STAKE / p))
    return math.ceil(100 * 0.07 * n * p * (1 - p) - 1e-9) / 100 / n


class Series:
    """Step function over sorted (ts, value) points: value at t = last point <= t if not older than age."""
    def __init__(self, pts, age):
        self.ts = [p[0] for p in pts]; self.v = [p[1:] if len(p) > 2 else p[1] for p in pts]; self.age = age

    def at(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > self.age:
            return None
        return self.v[i]


def load_meta(series):
    return {m["t"]: m for s in series for m in map(json.loads, (MK / f"{s}.jsonl").open())}


def load_candles(series, want):
    out = {}
    for s in series:
        for l in (CD / f"{s}.jsonl").open():
            x = json.loads(l)
            if x["t"] in want and x["c"]:
                out[x["t"]] = Series([[r[0], r[1], r[2]] for r in x["c"] if r[1] is not None and r[2] is not None], MAX_AGE)
    return out


def last_ok(path: Path, key: str):
    d = {}
    if path.exists():
        for l in path.open():
            x = json.loads(l)
            if not x.get("miss"):
                d[x[key]] = x
    return d


# --------------------------------------------------------------------------- pairs: (kalshi market, pm series, meta)
def crypto_pairs():
    S = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M")
    pm = last_ok(OUT / "pm_crypto.jsonl", "t"); meta = load_meta(S); cs = load_candles(S, set(pm))
    pairs = []; agree = [0, 0]
    for t, x in pm.items():
        m = meta.get(t)
        if not m or t not in cs or len(x["h"]) < 5:
            continue
        if x.get("pm_up_won") is not None:
            agree[0] += (x["pm_up_won"] == (m["result"] == "yes")); agree[1] += 1
        pairs.append({"fam": "crypto15", "t": t, "cl": str(m["close"]), "close": m["close"], "start": m["open"], "won": m["result"] == "yes",
                      "k": cs[t], "p": Series(sorted([a - PM_SHIFT, b] for a, b in x["h"]), PM_AGE), "phase_t": m["open"]})
    return pairs, agree


def sports_pairs():
    from lab.kalshi.strategies.xvenue_polymarket_fetch import ALIAS
    S = ("KXMLBGAME", "KXNFLGAME", "KXNHLGAME", "KXNBAGAME")
    pm = last_ok(OUT / "pm_sports.jsonl", "e"); meta = load_meta(S)
    by_e = defaultdict(list)
    for m in meta.values():
        if m["e"] in pm:
            by_e[m["e"]].append(m)
    cs = load_candles(S, {m["t"] for v in by_e.values() for m in v})
    pairs = []; agree = [0, 0]; import datetime as dt
    for e, ms in by_e.items():
        x = pm[e]; lg = x["slug"].split("-")[0]; first = x["slug"].split("-")[1]
        code = {ALIAS.get((lg, c), c).lower(): c for c in (x["away"], x["home"])}.get(first)
        if not code or len(x["h0"]) < 10 or len(ms) != 2:
            continue
        res = json.loads(x["pm_res"] or "[]")
        gs = int(dt.datetime.fromisoformat(x["gameStart"].replace(" ", "T").replace("+00", "+00:00")).timestamp()) if x.get("gameStart") else None
        if gs is None:
            continue
        h0 = sorted([a - PM_SHIFT, b] for a, b in x["h0"])
        for m in ms:
            mine = m["t"].rsplit("-", 1)[1] == code
            if res and len(res) == 2 and float(res[0]) in (0.0, 1.0):
                agree[0] += ((float(res[0]) == 1.0) == mine) == (m["result"] == "yes"); agree[1] += 1
            if m["t"] not in cs:
                continue
            h = h0 if mine else [[a, 1 - b] for a, b in h0]
            pairs.append({"fam": "sports_" + lg, "t": m["t"], "cl": e, "close": m["close"], "start": gs, "won": m["result"] == "yes",
                          "k": cs[m["t"]], "p": Series(h, PM_AGE), "phase_t": gs})
    return pairs, agree


# --------------------------------------------------------------------------- minute grid
def grid(pr):
    """Yield (t, ask, bid, pm, fill_ask, fill_bid, pm_prev2) for each decision minute with fresh data."""
    k = pr["k"]
    for t in k.ts:
        f = t + FILL_DELAY
        if t + 60 >= pr["close"]:
            break
        q = k.at(t); fq = k.at(f); p = pr["p"].at(t)
        if q is None or fq is None or p is None:
            continue
        yield t, q[0], q[1], p, fq[0], fq[1], pr["p"].at(t - 120)


def leadlag(pairs):
    """Who closes the gap? Regress next-minute changes of the Kalshi mid and of the PM price on gap = pm - kalshi_mid."""
    out = {}
    for fam in sorted({p["fam"] for p in pairs}):
        xs = []; yk = []; yp = []; spreads = []; gaps = []
        for pr in (p for p in pairs if p["fam"] == fam):
            k = pr["k"]
            for t, a, b, p, fa, fb, _ in grid(pr):
                p1 = pr["p"].at(t + 60)
                if p1 is None or not (0.03 < p < 0.97):
                    continue
                g = p - (a + b) / 2
                xs.append(g); yk.append((fa + fb) / 2 - (a + b) / 2); yp.append(p1 - p); spreads.append(a - b); gaps.append(abs(g))
        if len(xs) < 50:
            continue
        n = len(xs); mx = sum(xs) / n; mk = sum(yk) / n; mp = sum(yp) / n; vx = sum((x - mx) ** 2 for x in xs)
        bk = sum((x - mx) * (y - mk) for x, y in zip(xs, yk)) / vx
        bp = sum((x - mx) * (y - mp) for x, y in zip(xs, yp)) / vx
        out[fam] = {"minutes": len(xs), "kalshi_closes_frac": bk, "pm_closes_frac": -bp, "median_spread": st.median(spreads),
                    "abs_gap_median": st.median(gaps), "abs_gap_gt5c": sum(g > 0.05 for g in gaps) / len(gaps)}
    return out


# --------------------------------------------------------------------------- strategy
def signals(pairs, thr, phase, min_move=0.0):
    """First signal per market (crypto) or per game (sports). phase: crypto 'early' (>=8 min to close) / 'late' (2-7 min);
    sports 'pre' (before scheduled start) / 'in' (after). min_move: PM moved >= this toward the signal in the last 2 min."""
    best = {}
    for pr in pairs:
        for t, a, b, p, fa, fb, p2 in grid(pr):
            if pr["fam"] == "crypto15":
                mtc = (pr["close"] - t) / 60
                if (phase == "early" and not mtc >= 8) or (phase == "late" and not (2 <= mtc < 8)):
                    continue
            else:
                if (phase == "pre" and t >= pr["phase_t"]) or (phase == "in" and t < pr["phase_t"]):
                    continue
            for side, gap, px, won, mv in (("YES", p - a, fa, pr["won"], (p - p2) if p2 is not None else None),
                                           ("NO", b - p, 1 - fb, not pr["won"], (p2 - p) if p2 is not None else None)):
                if gap <= thr or not (0.02 <= px <= 0.98):
                    continue
                if min_move and (mv is None or mv < min_move):
                    continue
                key = pr["t"] if pr["fam"] == "crypto15" else pr["cl"]
                if key in best and best[key]["ts"] <= t:
                    continue
                pnl = (1.0 if won else 0.0) - px - fee_pc(px)
                best[key] = {"e": pr["cl"], "fam": pr["fam"], "t": pr["t"], "ts": t, "close": pr["close"], "side": side, "gap": gap, "pm": p,
                             "px": px, "won": won, "ret": pnl / px, "pm_ev": ((p if side == "YES" else 1 - p) - px - fee_pc(px)) / px}
                break
    return list(best.values())


def stats(rows):
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; n_e = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    rows = sorted(rows, key=lambda r: r["close"]); mid = len(rows) // 2
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "avg_px": st.mean(r["px"] for r in rows),
            "ret_per_dollar": st.mean(r["ret"] for r in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(r["ret"] for r in rows[:mid]) if mid else float("nan"), "half2": st.mean(r["ret"] for r in rows[mid:]),
            "pm_implied_ret": st.mean(r["pm_ev"] for r in rows), "avg_gap": st.mean(r["gap"] for r in rows)}


def split(pairs):
    cl = sorted({(p["close"], p["cl"]) for p in pairs}); keys = [c for _, c in cl]
    seen = []; [seen.append(k) for k in keys if k not in seen]
    cut = set(seen[: int(len(seen) * SPLIT)])
    return [p for p in pairs if p["cl"] in cut], [p for p in pairs if p["cl"] not in cut]


def fmt(name, s):
    if not s.get("n"):
        return f"  {name:42s} n=0"
    return (f"  {name:42s} n={s['n']:4d} ev={s['events']:4d} win={s['win']:.0%} px={s['avg_px']:.2f} gap={s['avg_gap']:.3f} "
            f"ret={s['ret_per_dollar']:+.1%} t={s['t']:5.2f} wo3={s['ret_wo3']:+.1%} h={s['half1']:+.1%}/{s['half2']:+.1%} pmEV={s['pm_implied_ret']:+.1%}")


THR = (0.03, 0.05, 0.08, 0.12)
# Frozen validation candidates (family, thr, phase, min_move), chosen on discovery only; passed as JSON in env FROZEN.
FROZEN = [tuple(x) for x in json.loads(os.environ.get("FROZEN", "[]"))]


def main(stage="discovery"):
    cp, ca = crypto_pairs(); sp, sa = sports_pairs()
    print(f"crypto pairs {len(cp)} (PM/Kalshi settle agree {ca[0]}/{ca[1]}); sports market pairs {len(sp)} games {len({p['cl'] for p in sp})} (agree {sa[0]}/{sa[1]})")
    fams = {"crypto15": cp, "sports": sp}
    res = {"leadlag": leadlag(cp + [dict(p, fam="sports") for p in sp]), "discovery": {}, "validation": {}}
    print("\nLEAD-LAG (all minutes, both periods; fraction of the PM-Kalshi gap closed in the next minute by each venue)")
    for f, v in res["leadlag"].items():
        print(f"  {f:10s} " + " ".join(f"{k}={v[k]:.3f}" if isinstance(v[k], float) else f"{k}={v[k]}" for k in v))
    variants = 0
    for fam, pairs in fams.items():
        if not pairs:
            continue
        d, v = split(pairs)
        phases = ("early", "late") if fam == "crypto15" else ("pre", "in")
        print(f"\nDISCOVERY {fam}: {len({p['cl'] for p in d})} clusters (validation {len({p['cl'] for p in v})})")
        for ph in phases:
            for thr in THR:
                for mm in (0.0, 0.03):
                    s = stats(signals(d, thr, ph, mm)); variants += 1
                    res["discovery"][f"{fam}|{ph}|thr{thr}|move{mm}"] = s
                    print(fmt(f"{fam} {ph} thr={thr} move>={mm}", s))
        res[f"split_{fam}"] = {"disc_clusters": len({p['cl'] for p in d}), "val_clusters": len({p['cl'] for p in v})}
        if stage == "validate":
            for (f2, thr, ph, mm) in FROZEN:
                if f2 != fam:
                    continue
                rows = signals(v, thr, ph, mm); s = stats(rows)
                res["validation"][f"{fam}|{ph}|thr{thr}|move{mm}"] = s
                print("  VALIDATION " + fmt(f"{fam} {ph} thr={thr} move>={mm}", s))
                (OUT / f"val_trades_{fam}_{ph}_{thr}_{mm}.json").write_text(json.dumps(rows, default=str))
    res["variants_examined"] = variants
    (OUT / f"analysis_{stage}_shift{PM_SHIFT}_delay{FILL_DELAY}.json").write_text(json.dumps(res, indent=1, default=str))
    print("variants", variants)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "discovery")
