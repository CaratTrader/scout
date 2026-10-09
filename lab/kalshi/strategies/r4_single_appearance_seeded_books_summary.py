"""Summary of the r4_single_appearance_seeded_books forward paper log (no Kalshi calls).

Reads data/kalshi_lab/strategies/r4_single_appearance_seeded_books/forward.jsonl and scores every posted order that has a
fill row and a settled result, exactly as frozen in preregistration.json:
  sell arm (the rule): through-only size-weighted return per $ (gate row 2 for makers), equal-$ return per filled order,
              t clustered by event, mean without the 3 best, halves, exact-binomial lower bound (amendment d),
              filled vs unfilled NO win rate, any-print and queue-aware variants;
  mirror arm (mechanism check): buy-YES at bid + 1c, through-only equal-$ and size-weighted;
  coverage: markets listed / entered / posted / missed, listing-spread distribution (kill: placeholder books gone).
Writes summary.json next to the log and prints it.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_single_appearance_seeded_books_summary"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import Counter, defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_single_appearance_seeded_books import binom_lo

OUT = Path("data/kalshi_lab/strategies/r4_single_appearance_seeded_books")


LAB_K_FLOOR = 17217     # lab K after round 3 (~17,180, docs/KALSHI_LAB.md) + the 37 cells of this family; K.json holds only calib cells


def k_bar() -> tuple[int, float]:
    from statistics import NormalDist
    K = LAB_K_FLOOR
    try:
        d = json.loads(Path("data/kalshi_lab/K.json").read_text())
        K = max(K, int(d.get("K") or len(d.get("cells") or [])))
    except Exception:
        pass
    return K, NormalDist().inv_cdf(1 - 0.05 / K)


def load() -> tuple[list[dict], dict, dict, dict]:
    f = OUT / "forward.jsonl"
    rows = [json.loads(l) for l in f.open()] if f.exists() else []
    entries = [r for r in rows if r["type"] == "entry"]
    fills = {r["ticker"]: r for r in rows if r["type"] == "fill"}
    res = {}
    for r in rows:
        if r["type"] == "settle":
            for t, v in r["markets"].items():
                res[t] = v[0]
    return rows, {r["ticker"]: r for r in entries}, fills, res


def stats(xs: list[dict]) -> dict:
    if not xs:
        return {"n": 0}
    ev = defaultdict(list)
    for x in xs:
        ev[x["e"]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(len(em))) if len(em) > 2 and sd > 0 else None
    rs = sorted((x["ret"] for x in xs), reverse=True)
    srt = sorted(xs, key=lambda x: x["post"]); mid = srt[len(srt) // 2]["post"]
    h1 = [x["ret"] for x in srt if x["post"] < mid]; h2 = [x["ret"] for x in srt if x["post"] >= mid]
    cost = sum(x["cost"] for x in xs); pnl = sum(x["pnl"] for x in xs)
    first = {}
    for x in srt:
        first.setdefault(x["e"], x)
    fe = list(first.values()); k = sum(x["won"] for x in fe); pxe = st.mean(x["px"] for x in fe)
    wlo = binom_lo(k, len(fe)); qs = sorted(x["q"] for x in xs)
    return {"n": len(xs), "events": len(em), "win": round(st.mean(x["won"] for x in xs), 4), "avg_px": round(st.mean(x["px"] for x in xs), 4),
            "ret_equal_dollar": round(st.mean(rs), 4), "t": round(t, 3) if t is not None else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None, "half1": round(st.mean(h1), 4) if h1 else None,
            "half2": round(st.mean(h2), 4) if h2 else None, "size_weighted_ret": round(pnl / cost, 4) if cost > 0 else None,
            "pnl_dollars": round(pnl, 2), "cost_dollars": round(cost, 2), "median_filled_contracts": qs[len(qs) // 2],
            "beta_lo_ret_per_dollar": round((wlo - pxe) / pxe, 4)}


def arm(entries: dict, fills: dict, res: dict, side: str, key: str) -> tuple[dict, dict]:
    xs, fw, uw = [], [], []
    for t, e in entries.items():
        if not e.get("posted") or t not in fills or res.get(t) not in ("yes", "no"):
            continue
        f = fills[t]; yes = res[t] == "yes"
        if side == "sell":
            px = 1 - e["s_c"] / 100; N = e["N"]; cnt = f.get(key); win = not yes
        else:
            px = e["b_c"] / 100; N = e["Nb"]; cnt = f.get(key); win = yes
        if cnt is None:
            continue
        q = min(N, cnt)
        (fw if q > 0 else uw).append(win)
        if q > 0:
            xs.append({"e": e["e"], "t": t, "post": e["ts"], "px": px, "won": win, "ret": ((1.0 if win else 0.0) - px) / px, "q": q,
                       "pnl": q * ((1.0 if win else 0.0) - px), "cost": q * px})
    s = stats(xs)
    s["filled_win"] = round(st.mean(fw), 4) if fw else None; s["unfilled_win"] = round(st.mean(uw), 4) if uw else None
    s["scored_orders"] = len(fw) + len(uw)
    return s, {x["t"]: x for x in xs}


def main() -> dict:
    rows, entries, fills, res = load()
    lst = [r for r in rows if r["type"] == "listing"]
    ev_entries = [e for e in entries.values()]
    spreads = [e["ask_c"] - e["bid_c"] for e in ev_entries if e.get("ask_c") is not None and e.get("bid_c") is not None]
    K, bar = k_bar()
    out = {"rows": len(rows), "passes": sum(1 for r in rows if r["type"] == "pass"),
           "calls": sum(r.get("calls", 0) for r in rows if r["type"] == "pass"),
           "listings": len(lst), "listing_status": dict(Counter(r["status"] for r in lst)),
           "entries": len(ev_entries), "entry_reasons": dict(Counter(e.get("reason") or "posted" for e in ev_entries)),
           "missed": sum(1 for r in rows if r["type"] == "missed"),
           "events_with_orders": len({e["e"] for e in ev_entries if e.get("posted")}),
           "share_listing_spread_ge_10c": round(sum(s >= 10 for s in spreads) / len(spreads), 4) if spreads else None,
           "fills_rows": len(fills), "settled_markets": len(res), "K": K, "t_bar": round(bar, 3) if bar else None}
    out["sell_through"], sel = arm(entries, fills, res, "sell", "thr_cnt")
    out["sell_any"], _ = arm(entries, fills, res, "sell", "any_cnt")
    out["sell_queue_probed_only"], _ = arm({t: e for t, e in entries.items() if "queue_cnt" in fills.get(t, {})}, fills, res, "sell", "queue_cnt")
    out["mirror_through"], _ = arm(entries, fills, res, "buy", "m_thr_cnt")
    s = out["sell_through"]; m = out["mirror_through"]
    out["gate"] = {
        "1_fills>=40_events>=30": bool(s.get("n", 0) >= 40 and s.get("events", 0) >= 30),
        "2_size_weighted>=+10%": bool((s.get("size_weighted_ret") or -9) >= 0.10),
        "3_t>=bar_and_beta_lo>0": bool(bar and (s.get("t") or 0) >= bar and (s.get("beta_lo_ret_per_dollar") or -9) > 0),
        "4_wo3>0": bool((s.get("ret_wo3") or -9) > 0),
        "5_halves>0": bool((s.get("half1") or -9) > 0 and (s.get("half2") or -9) > 0),
        "6_median_fill>=5": bool((s.get("median_filled_contracts") or 0) >= 5),
        "mechanism_mirror<0": bool(m.get("n") and (m.get("ret_equal_dollar") or 0) < 0)}
    out["kill_check"] = {"after_30_events": s.get("events", 0) >= 30,
                         "size_weighted<0": bool(s.get("n") and (s.get("size_weighted_ret") or 0) < 0),
                         "mirror>0": bool(m.get("n") and (m.get("ret_equal_dollar") or 0) > 0),
                         "placeholder_books_gone(<20% spreads>=10c)": bool(out["share_listing_spread_ge_10c"] is not None and len(spreads) >= 50
                                                                            and out["share_listing_spread_ge_10c"] < 0.20)}
    (OUT / "summary.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=1))
