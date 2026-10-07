"""Where are Kalshi prices wrong? Taker returns by series family, side, price band and decision time, with a strict
time split: cells are *chosen* on the first 70% of each family's events (discovery) and *judged* on the last 30%
(validation), with the significance bar raised for the number of cells examined (docs/KALSHI_LAB.md).

Decision times: minutes before a fixed close ("close" series) or minutes relative to the scheduled end
(expected_expiration_time, sports; the actual close is when a winner is declared and would leak the result).
Fill: the side's taker price one minute after the decision (yes_ask, or 1 - yes_bid for NO), carried forward from the
last 1-minute candle if not older than MAX_AGE; fee 0.07 * p * (1 - p) per contract.
Usage: python -m lab.kalshi.calib [--split 0.7] [--delay 1]"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.kalshi.fetch import PLAN, MK, CD

ROOT = Path("data/kalshi_lab")
FAMILY = {**{s: "crypto15" for s in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")},
          **{s: "commod15" for s in ("KXGOLD15M", "KXWTI15M", "KXSILVER15M")},
          **{s: "cryptoH" for s in ("KXBTCD", "KXETHD")}, **{s: "indexH" for s in ("KXINXU", "KXNASDAQ100U")},
          **{s: "tempH" for s in ("KXTEMPMIAH", "KXTEMPNYCHS", "KXTEMPCHIHS", "KXTEMPLAXHS")}, "KXRAIN": "rain",
          **{s: "daily_commod" for s in ("KXAAAGASD", "KXWTI", "KXGOLDD", "KXNATGASD")},
          **{s: "sports" for s in ("KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME", "KXUEFANLGAME")},
          **{s: "tennis" for s in ("KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGERMATCH")},
          "KXCS2GAME": "esports"}
TAU_CLOSE = (1, 2, 5, 10, 15, 30, 60, 120, 240, 480, 720, 1080)          # minutes before close
TAU_EXP = (-360, -240, -180, -120, -60, -30, 0, 30, 60, 90, 120)          # minutes relative to the scheduled end
BANDS = (0.01, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.98, 0.995)
MAX_AGE = 30   # minutes a quote may be carried forward
FEE = 0.07
MIN_N = 30


def fee(p: float) -> float:
    return FEE * p * (1 - p)


def band(p: float) -> str | None:
    for lo, hi in zip(BANDS, BANDS[1:]):
        if lo <= p < hi:
            return f"{lo:.3f}-{hi:.3f}".replace("0.", ".")
    return None


def quote(c: list[list], t: int) -> tuple[float, float] | None:
    """(yes_ask, yes_bid) from the last candle at or before t, if fresh enough. Candles: [ts, ask, bid, ask_lo, bid_hi, vol]."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if not best or t - best[0] > MAX_AGE * 60 or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


def trades(delay: int = 1) -> list[dict]:
    out = []
    for s, fam in FAMILY.items():
        mf, cf = MK / f"{s}.jsonl", CD / f"{s}.jsonl"
        if not (mf.exists() and cf.exists()):
            continue
        meta = {m["t"]: m for m in map(json.loads, mf.open())}
        anchor = PLAN[s][2]
        for line in cf.open():
            x = json.loads(line); m = meta.get(x["t"]); c = x["c"]
            if not m or not c:
                continue
            won = m["result"] == "yes"
            points = [(f"-{tau}m", m["close"] - tau * 60) for tau in TAU_CLOSE] if anchor == "close" else \
                     [(f"exp{off:+d}m", m["exp"] + off * 60) for off in TAU_EXP]
            for lab, t in points:
                if anchor == "expected" and t + delay * 60 >= m["close"]:
                    continue   # the market had already closed (result known): no trade possible at this time
                if anchor == "close" and t < m["close"] - PLAN[s][1] * 60:
                    continue
                q = quote(c, t + delay * 60)
                if not q:
                    continue
                ask, bid = q
                for side, px, w in (("YES", ask, won), ("NO", 1 - bid, not won)):
                    if not (0.01 <= px <= 0.995):
                        continue
                    b = band(px)
                    if b is None:
                        continue
                    pnl = (1.0 if w else 0.0) - px - fee(px)
                    out.append({"fam": fam, "s": s, "e": m["e"], "t_close": m["close"], "tau": lab, "side": side, "band": b, "px": px, "won": w, "ret": pnl / px})
    return out


def cell_stats(rows: list[dict]) -> dict:
    """Equal-$ return per trade; t-stat clustered by event (markets of one event move together)."""
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em); mean = st.mean(r["ret"] for r in rows)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "px": st.mean(r["px"] for r in rows), "ret": mean, "t": t,
            "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan")}


def main(split: float = 0.7, delay: int = 1) -> None:
    T = trades(delay)
    by_fam = defaultdict(list)
    for r in T:
        by_fam[r["fam"]].append(r)
    cells_disc = {}; cutoffs = {}
    for fam, rows in by_fam.items():
        closes = sorted({r["t_close"] for r in rows}); cut = closes[int(len(closes) * split)] if closes else 0; cutoffs[fam] = cut
        g = defaultdict(list)
        for r in rows:
            if r["t_close"] < cut:
                g[(fam, r["tau"], r["side"], r["band"])].append(r)
        for k, v in g.items():
            if len(v) >= MIN_N:
                cells_disc[k] = cell_stats(v)
    K = len(cells_disc); z = NormalDist().inv_cdf(1 - 0.05 / max(K, 1))
    cand = {k: v for k, v in cells_disc.items() if v["ret"] >= 0.10 and v["t"] >= 2.0}
    print(f"trades {len(T)}; families {sorted(by_fam)}; discovery cells (n>={MIN_N}) K={K}; bar t >= {z:.2f}; discovery candidates (ret>=10%, t>=2): {len(cand)}")
    print("\nBEST DISCOVERY CELLS (return per $ after fees)")
    for k, v in sorted(cells_disc.items(), key=lambda kv: -kv[1]["ret"])[:25]:
        print(f"  {k[0]:12s} {k[1]:9s} {k[2]:3s} px {k[3]:11s} n={v['n']:5d} ev={v['events']:4d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.1f} wo3={v['ret_wo3']:+.1%}")
    print("\nVALIDATION of the discovery candidates (the only cells judged)")
    val = {}
    for k in cand:
        fam = k[0]; rows = [r for r in by_fam[fam] if r["t_close"] >= cutoffs[fam] and (r["fam"], r["tau"], r["side"], r["band"]) == k]
        if not rows:
            print(f"  {k}: no validation trades"); continue
        v = cell_stats(rows); mid = sorted(r["t_close"] for r in rows)[len(rows) // 2]
        h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
        v["halves"] = (st.mean(h1) if h1 else float("nan"), st.mean(h2) if h2 else float("nan"))
        v["gate"] = v["n"] >= 40 and v["ret"] >= 0.10 and v["t"] >= z and v["ret_wo3"] > 0 and min(v["halves"]) > 0
        val[k] = v
        print(f"  {k[0]:12s} {k[1]:9s} {k[2]:3s} px {k[3]:11s} n={v['n']:5d} ev={v['events']:4d} win={v['win']:.0%} ret={v['ret']:+.1%} t={v['t']:5.1f} "
              f"wo3={v['ret_wo3']:+.1%} halves={v['halves'][0]:+.1%}/{v['halves'][1]:+.1%} GATE={'PASS' if v['gate'] else 'fail'}")
    print("\nFAMILY OVERVIEW (all decision times and bands, discovery period): taker return per $ by side")
    for fam, rows in sorted(by_fam.items()):
        d = [r for r in rows if r["t_close"] < cutoffs[fam]]
        for side in ("YES", "NO"):
            x = [r for r in d if r["side"] == side]
            if x:
                v = cell_stats(x); print(f"  {fam:12s} {side:3s} n={v['n']:6d} ev={v['events']:5d} ret={v['ret']:+.1%} win={v['win']:.0%} px={v['px']:.3f}")
    (ROOT / "calib.json").write_text(json.dumps({"K": K, "z": z, "split": split, "delay": delay,
                                                  "discovery": {"|".join(k): v for k, v in cells_disc.items()},
                                                  "validation": {"|".join(k): v for k, v in val.items()}}, default=str))


if __name__ == "__main__":
    a = sys.argv
    main(float(a[a.index("--split") + 1]) if "--split" in a else 0.7, int(a[a.index("--delay") + 1]) if "--delay" in a else 1)
