"""Adversarial reproduction of the 'microstructure' family's rain claims, written from the claim text only.

Claim under test (frozen candidate E|rain|S0.06|H30|NO): when the KXRAIN spread is >= 6c, rest a NO bid at
1 - (yes_ask - 0.01) (= a YES offer one cent inside the ask) for 30 minutes; it fills only when the YES bid trades
through the offer (yes_bid_high >= offer + 1c) in a later 1-minute candle; hold to settlement; no maker fee (KXRAIN is
fee_type 'quadratic'). Secondary: A|rain|k5|X0.20|REV (mid moved >= 20c in 5 min -> buy the side that fell, taker).

Timing (no look-ahead): a candle [ts, ask, bid, ask_lo, bid_hi, vol] covers (ts-60, ts]. At decision minute t the
quote is the last candle with ts <= t (<= 30 min old). The order is placed at t+60 at a price set from the t quote.
If the t+60 quote already crosses it (yes_bid >= offer) it fills as a taker at 1 - yes_bid with the taker fee
("taker fallback", optional). Otherwise only candles with ts >= t+120 (wholly after placement) can fill it, until
t+60+H*60 or the close. After a fill or expiry the next decision is made at that minute.

Split: same as the claim, KXRAIN markets with close >= 1790236800 (2026-09-24 08:00 UTC) are validation; also an
event-level 70/30 split (by event close) for comparison.
Usage: .venv/bin/python -m lab.kalshi.strategies.microstructure_repro"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MK = ROOT / "data/kalshi_lab/markets/KXRAIN.jsonl"
CD = ROOT / "data/kalshi_lab/candles/KXRAIN.jsonl"
OUT = ROOT / "data/kalshi_lab/strategies/microstructure/repro"
CUT = 1790236800          # claimed rain cutoff (cutoffs.json)
WIN = 18 * 3600           # decision window before close
MAX_AGE = 30 * 60


def taker_fee(p: float) -> float:
    return 0.07 * p * (1 - p)


def load():
    meta = {}
    for line in MK.open():
        m = json.loads(line); meta[m["t"]] = m
    cand = {}
    for line in CD.open():
        x = json.loads(line)
        if x["t"] in meta and x["c"]:
            cand[x["t"]] = sorted(x["c"], key=lambda r: r[0])
    return meta, cand


class Book:
    """Carry-forward quote lookup over one market's candles."""
    def __init__(self, c):
        self.c = c; self.ts = [r[0] for r in c]

    def idx(self, t):     # index of last candle with ts <= t
        lo, hi = 0, len(self.ts)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.ts[mid] <= t: lo = mid + 1
            else: hi = mid
        return lo - 1

    def quote(self, t):
        i = self.idx(t)
        if i < 0: return None
        r = self.c[i]
        if t - r[0] > MAX_AGE or r[1] is None or r[2] is None: return None
        return r[1], r[2]


def maker_no(m, bk, S=0.06, H=30, through=0.01, fallback=True, price_ref="t", lo=0.05, hi=0.95, lookahead=False, vol_floor=0.0):
    """Simulate the resting-NO rule on one market; returns list of trades."""
    out = []; close = m["close"]; t = close - WIN; t -= t % 60
    won_no = m["result"] == "no"
    while t <= close - 120:
        q = bk.quote(t)
        if not q or q[0] - q[1] < S - 1e-9:
            t += 60; continue
        tp = t + 60
        if price_ref == "t+1":
            q1 = bk.quote(tp)
            if not q1 or q1[0] - q1[1] < S - 1e-9:
                t += 60; continue
            offer = round(q1[0] - 0.01, 2)
        else:
            offer = round(q[0] - 0.01, 2)
        no_px = round(1 - offer, 2)
        if not (lo <= no_px <= hi):
            t += 60; continue
        q1 = bk.quote(tp)
        if q1 and q1[1] >= offer - 1e-9:          # order would cross on arrival -> taker at the NO ask
            if fallback:
                px = round(1 - q1[1], 2)
                if lo <= px <= hi:
                    pnl = (1.0 if won_no else 0.0) - px - taker_fee(px)
                    out.append({"t": m["t"], "e": m["e"], "close": close, "ts": tp, "px": px, "won": won_no, "ret": pnl / px, "kind": "taker", "vol": None})
            t = tp; continue
        end = min(tp + H * 60, close)
        first = tp if lookahead else tp + 60
        i = bk.idx(first - 1) + 1; filled = None
        while i < len(bk.c) and bk.c[i][0] <= end:
            r = bk.c[i]
            if r[4] is not None and r[4] >= offer + through - 1e-9 and (r[5] or 0) >= vol_floor:
                filled = r; break
            i += 1
        if filled:
            pnl = (1.0 if won_no else 0.0) - no_px     # quadratic series: no maker fee
            out.append({"t": m["t"], "e": m["e"], "close": close, "ts": filled[0], "px": no_px, "won": won_no, "ret": pnl / no_px, "kind": "maker",
                        "vol": filled[5], "bid_hi": filled[4], "offer": offer})
            t = filled[0]
        else:
            t = end if end > tp else t + 60
    return out


def taker_rev(m, bk, k=5, X=0.20, cooldown=5, lo=0.01, hi=0.99):
    """A|rain|k5|X0.20|REV: mid moved >= X over k minutes -> buy the side that fell one minute later (taker)."""
    out = []; close = m["close"]; t0 = close - WIN; won_yes = m["result"] == "yes"; last = -10 ** 12
    for r in bk.c:
        t = r[0]
        if t < t0 + k * 60 or t > close - 60 or t - last < cooldown * 60: continue
        q, qk = bk.quote(t), bk.quote(t - k * 60)
        if not q or not qk: continue
        d = (q[0] + q[1]) / 2 - (qk[0] + qk[1]) / 2
        if abs(d) < X - 1e-9: continue
        q1 = bk.quote(t + 60)
        if not q1: continue
        if d < 0: side, px, w = "YES", q1[0], won_yes
        else: side, px, w = "NO", round(1 - q1[1], 2), not won_yes
        if not (lo <= px <= hi): continue
        pnl = (1.0 if w else 0.0) - px - taker_fee(px)
        out.append({"t": m["t"], "e": m["e"], "close": close, "ts": t + 60, "px": px, "won": w, "ret": pnl / px, "side": side, "vol": r[5]})
        last = t
    return out


def stats(rows):
    if not rows: return {"n": 0}
    n = len(rows); mean = st.mean(r["ret"] for r in rows)
    ev = defaultdict(list)
    for r in rows: ev[r["e"]].append(r["ret"])
    G = len(ev)
    # trade-weighted mean, cluster-robust SE (CR0 with small-sample factor G/(G-1))
    s2 = sum((sum(v) - len(v) * mean) ** 2 for v in ev.values())
    se = math.sqrt(s2 * G / max(G - 1, 1)) / n if s2 > 0 else float("nan")
    em = [st.mean(v) for v in ev.values()]
    t_ev = st.mean(em) / (st.pstdev(em) / math.sqrt(G)) if G > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["ts"]); mid = srt[n // 2]["close"]
    h1 = [r["ret"] for r in rows if r["close"] < mid]; h2 = [r["ret"] for r in rows if r["close"] >= mid]
    vols = sorted(r["vol"] for r in rows if r.get("vol") is not None)
    return {"n": n, "events": G, "markets": len({r["t"] for r in rows}), "win": round(sum(r["won"] for r in rows) / n, 4),
            "avg_px": round(st.mean(r["px"] for r in rows), 4), "ret_per_dollar": round(mean, 4),
            "t_clustered": round(mean / se, 2) if se == se and se > 0 else None, "t_event_means": round(t_ev, 2) if t_ev == t_ev else None,
            "mean_event_means": round(st.mean(em), 4), "ret_wo3": round(st.mean(rs[3:]), 4) if n > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "median_fill_minute_volume": vols[len(vols) // 2] if vols else None}


HOLD = {   # extra holdouts, data cached by other researchers (read-only), simulated with this file's code
    "oos1_AUG08-22": (ROOT / "data/kalshi_lab/strategies/rain_history/markets.jsonl", ROOT / "data/kalshi_lab/strategies/rain_history/candles.jsonl",
                      lambda e: "KXRAIN-26AUG08" <= e <= "KXRAIN-26AUG22"),
    "oos2_JUL31-AUG07": (ROOT / "data/kalshi_lab/strategies/microstructure/arch_markets.jsonl", ROOT / "data/kalshi_lab/strategies/microstructure/arch_candles.jsonl",
                         lambda e: True),
    "fresh_OCT07": (ROOT / "data/kalshi_lab/strategies/rain_history/markets.jsonl", ROOT / "data/kalshi_lab/strategies/rain_history/candles.jsonl",
                    lambda e: e == "KXRAIN-26OCT07"),
}


def holdouts(kw_list):
    out = {}; pooled = defaultdict(list)
    for name, (mf, cf, sel) in HOLD.items():
        if not (mf.exists() and cf.exists()):
            continue
        meta = {}
        for line in mf.open():
            m = json.loads(line)
            if m.get("series") == "KXRAIN" and m["t"].startswith("KXRAIN-") and sel(m["e"]) and m.get("result") in ("yes", "no"):
                meta[m["t"]] = m
        out[name] = {"markets": len(meta)}
        seen = set()
        for line in cf.open():
            x = json.loads(line)
            if x["t"] not in meta or not x["c"] or x["t"] in seen:
                continue
            seen.add(x["t"]); bk = Book(sorted(x["c"], key=lambda r: r[0]))
            for vn, kw in kw_list.items():
                out[name].setdefault(vn, []).extend(maker_no(meta[x["t"]], bk, **kw))
        out[name]["markets_with_candles"] = len(seen)
        for vn in kw_list:
            rows = out[name].get(vn, [])
            if name != "fresh_OCT07": pooled[vn] += rows
            out[name][vn] = stats(rows)
    return out, pooled


def main():
    meta, cand = load()
    books = {t: Book(c) for t, c in cand.items()}
    events = sorted({m["e"] for m in meta.values()}, key=lambda e: max(x["close"] for x in meta.values() if x["e"] == e))
    ev_cut = set(events[int(len(events) * 0.7):])
    split = {"claim": lambda m: m["close"] >= CUT, "event70": lambda m: m["e"] in ev_cut}
    variants = {
        "E frozen (through 1c, taker fallback, price from t quote)": {},
        "E no taker fallback": {"fallback": False},
        "E touch fill": {"through": 0.0},
        "E through 2c": {"through": 0.02},
        "E price from t+1 quote": {"price_ref": "t+1"},
        "E look-ahead fill minute (WRONG, check only)": {"lookahead": True},
        "E fill-minute vol>=10": {"vol_floor": 10.0},
        "E NO px 0.01-0.99": {"lo": 0.01, "hi": 0.99},
        "E S0.03": {"S": 0.03},
        "E H5": {"H": 5},
    }
    res = {"variants": {}, "notes": []}
    allrows = {}
    for name, kw in variants.items():
        rows = []
        for tk, m in meta.items():
            if tk in books: rows += maker_no(m, books[tk], **kw)
        allrows[name] = rows
        res["variants"][name] = {sp: {"disc": stats([r for r in rows if not f(meta[r["t"]])]), "val": stats([r for r in rows if f(meta[r["t"]])])}
                                 for sp, f in split.items()}
        res["variants"][name]["all"] = stats(rows)
    rowsA = []
    for tk, m in meta.items():
        if tk in books: rowsA += taker_rev(m, books[tk])
    res["A_rev"] = {sp: {"disc": stats([r for r in rowsA if not f(meta[r["t"]])]), "val": stats([r for r in rowsA if f(meta[r["t"]])])} for sp, f in split.items()}
    # breakdowns of the frozen rule on the claimed validation split
    E = [r for r in allrows["E frozen (through 1c, taker fallback, price from t quote)"] if meta[r["t"]]["close"] >= CUT]
    by = lambda key: {k: stats(v) for k, v in sorted(_group(E, key).items())}
    res["E_val_by_day"] = {k: (v["n"], v["ret_per_dollar"]) for k, v in by(lambda r: r["e"]).items()}
    res["E_val_by_city"] = {k: (v["n"], v["ret_per_dollar"]) for k, v in by(lambda r: r["t"].split("-")[-1]).items()}
    bands = [(0.05, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 0.96)]
    res["E_val_by_band"] = {f"{a}-{b}": stats([r for r in E if a <= r["px"] < b]) for a, b in bands}
    res["E_val_kind"] = {k: stats([r for r in E if r["kind"] == k]) for k in ("maker", "taker")}
    first = {}
    for r in sorted(E, key=lambda r: r["ts"]): first.setdefault(r["t"], r)
    res["E_val_first_fill_per_market"] = stats(list(first.values()))
    mk = defaultdict(list)
    for r in E: mk[r["t"]].append(r)
    # leave-one-market-out: the largest single-market contribution
    tot = sum(r["ret"] for r in E)
    contrib = sorted(((sum(r["ret"] for r in v), k, len(v)) for k, v in mk.items()), reverse=True)
    res["E_val_top_market_contributions"] = [(k, n, round(s, 2)) for s, k, n in contrib[:5]]
    res["E_val_without_top_market"] = stats([r for r in E if r["t"] != contrib[0][1]])
    res["E_val_without_top2_markets"] = stats([r for r in E if r["t"] not in (contrib[0][1], contrib[1][1])])
    # per-market equal weight (one unit of mean return per market)
    res["E_val_market_mean_of_means"] = round(st.mean(st.mean(r["ret"] for r in v) for v in mk.values()), 4)
    # unconditional taker NO benchmark: every market, NO at 1 - yes_bid at 12:00 and 18:00 local-ish (close-12h, close-6h)
    bench = []
    for tk, m in meta.items():
        if tk not in books: continue
        for h in (12, 6):
            q = books[tk].quote(m["close"] - h * 3600 + 60)
            if not q: continue
            px = round(1 - q[1], 2)
            if 0.05 <= px <= 0.95:
                bench.append({"t": tk, "e": m["e"], "close": m["close"], "ts": m["close"] - h * 3600, "px": px, "won": m["result"] == "no",
                              "ret": ((1.0 if m["result"] == "no" else 0.0) - px - taker_fee(px)) / px})
    res["benchmark_taker_NO_close-12h/-6h"] = {"disc": stats([r for r in bench if meta[r["t"]]["close"] < CUT]), "val": stats([r for r in bench if meta[r["t"]]["close"] >= CUT])}
    # base rates
    for nm, f in (("disc", lambda m: m["close"] < CUT), ("val", lambda m: m["close"] >= CUT)):
        ms = [m for m in meta.values() if f(m)]
        res[f"base_rate_no_{nm}"] = round(sum(m["result"] == "no" for m in ms) / len(ms), 4)
    # regime check: since ~2026-09-07 spreads >= 6c are rare (<1% of window minutes); stats of the frozen rule there
    Eall = allrows["E frozen (through 1c, taker fallback, price from t quote)"]
    tight = [r for r in Eall if r["close"] >= 1789358400]          # events KXRAIN-26SEP07 and later (close >= 2026-09-08 04:00 UTC)
    res["regime_tight_spreads_SEP07-OCT06"] = stats(tight)
    mk3 = defaultdict(float)
    for r in tight: mk3[r["t"]] += r["ret"]
    top3 = [k for k, _ in sorted(mk3.items(), key=lambda kv: -kv[1])[:2]]
    res["regime_tight_spreads_SEP07-OCT06_without_top2_markets"] = dict(stats([r for r in tight if r["t"] not in top3]), dropped=top3)
    res["fills_per_day_OCT01-OCT06"] = {e: sum(1 for r in Eall if r["e"] == e) for e in sorted({m["e"] for m in meta.values()}) if e.startswith("KXRAIN-26OCT")}
    hk = {"E frozen": {}, "E no taker fallback": {"fallback": False}}
    ho, pooled = holdouts(hk)
    res["holdouts"] = ho
    for vn in hk:
        vrows = allrows["E frozen (through 1c, taker fallback, price from t quote)" if vn == "E frozen" else "E no taker fallback"]
        vrows = [r for r in vrows if meta[r["t"]]["close"] >= CUT]
        res.setdefault("pooled_unselected", {})[vn] = stats(vrows + pooled[vn])
        # leave-one-market-out on the pooled sample: drop the single largest-contribution market
        mk2 = defaultdict(float)
        for r in vrows + pooled[vn]: mk2[r["t"]] += r["ret"]
        top = sorted(mk2.items(), key=lambda kv: -kv[1])[:3]
        res["pooled_unselected"][vn + " top3 markets"] = top
        res["pooled_unselected"][vn + " without top3 markets"] = stats([r for r in vrows + pooled[vn] if r["t"] not in {k for k, _ in top}])
        firsts = {}
        for r in sorted(vrows + pooled[vn], key=lambda r: r["ts"]): firsts.setdefault(r["t"], r)
        res["pooled_unselected"][vn + " first fill per market"] = stats(list(firsts.values()))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "repro.json").write_text(json.dumps(res, indent=1, default=str))
    with (OUT / "E_val_trades.jsonl").open("w") as fh:
        for r in sorted(E, key=lambda r: r["ts"]): fh.write(json.dumps(r) + "\n")
    for name, v in res["variants"].items():
        for sp in ("claim", "event70"):
            for part in ("disc", "val"):
                x = v[sp][part]
                if x.get("n"):
                    print(f"{name[:52]:52s} {sp:7s} {part:4s} n={x['n']:5d} ev={x['events']:3d} mk={x['markets']:4d} win={x['win']:.2f} px={x['avg_px']:.3f} "
                          f"ret={x['ret_per_dollar']:+.1%} tcl={x['t_clustered']} tev={x['t_event_means']} wo3={x['ret_wo3']:+.1%} h={x['half1']:+.1%}/{x['half2']:+.1%}")
    for sp in ("claim", "event70"):
        for part in ("disc", "val"):
            x = res["A_rev"][sp][part]
            if x.get("n"):
                print(f"A rev k5 X.20 {sp} {part} n={x['n']} ev={x['events']} win={x['win']} px={x['avg_px']} ret={x['ret_per_dollar']:+.1%} tcl={x['t_clustered']} tev={x['t_event_means']} wo3={x['ret_wo3']}")
    print(json.dumps({k: res[k] for k in ("E_val_by_day", "E_val_by_city", "E_val_top_market_contributions", "E_val_market_mean_of_means",
                                          "base_rate_no_disc", "base_rate_no_val")}, default=str))
    for k in ("E_val_kind", "E_val_by_band"):
        for kk, x in res[k].items():
            if x.get("n"): print(k, kk, x["n"], x["ret_per_dollar"], x["t_clustered"], x["ret_wo3"])
    for k in ("E_val_first_fill_per_market", "E_val_without_top_market", "E_val_without_top2_markets"):
        print(k, res[k])
    print("bench", res["benchmark_taker_NO_close-12h/-6h"])
    for name, v in res["holdouts"].items():
        print("HOLDOUT", name, {k: v[k] for k in ("markets", "markets_with_candles")})
        for vn in hk:
            print("   ", vn, v[vn])
    for k, v in res["pooled_unselected"].items(): print("POOLED", k, v)
    for k in ("regime_tight_spreads_SEP07-OCT06", "regime_tight_spreads_SEP07-OCT06_without_top2_markets", "fills_per_day_OCT01-OCT06"): print(k, res[k])


def _group(rows, key):
    g = defaultdict(list)
    for r in rows: g[key(r)].append(r)
    return g


if __name__ == "__main__":
    main()
