"""Exact trade-print check of the placeholder (P) cells of r5_seeded_book_placeholder_split (POST HOC, descriptive).

The archive split (r5_seeded_book_placeholder_split.run) sized r2 / r3 fills with the candle-volume proxy, an upper
bound. The P mirror (buy YES at bid + 1c) came out at +574%/$ equal-$; this check fetches the exact prints of the fill
window (post + 120 s, window end] for (1) every P order with a mirror fill and (2) every r3 validation P order with a
sell fill, and recomputes both sides exactly: sell through = prints strictly above s, mirror through = prints strictly
below b, block trades excluded, f = min(N, count). Seen after the split result, so it can only describe, not confirm.
Calls: lab.kalshi.strategies.r5_seeded_book_placeholder_split_api.research_get (cached, counted, capped).
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_seeded_book_placeholder_split_prints [fetch N] [report]"""
from __future__ import annotations
import datetime as dt, json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_seeded_book_placeholder_split_api import research_get, used, BudgetExhausted, OUT
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_stats import size_weighted, equal_dollar

EPS = 1e-9
ARCHIVE_TIER_BEFORE = int(dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc).timestamp())


def ts_of(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def targets() -> list[dict]:
    R = [json.loads(l) for l in (OUT / "archive_rows.jsonl").open()]
    a = [r for r in R if r["P"] and r["fam"] in ("r2", "r3") and r.get("mf")]
    b = [r for r in R if r["P"] and r["fam"] == "r3" and r["part"] == "val" and r["f"]]
    seen, out = set(), []
    for r in a + b:
        if r["t"] not in seen:
            seen.add(r["t"]); out.append(r)
    return out


def prints_of(r: dict) -> tuple[list, bool]:
    lo, hi = int(r["post"]) + 120, int(r["win_end"])
    allp, cursor, pages, hist = [], "", 0, False
    while True:
        base = "/historical/trades" if hist else "/markets/trades"
        d = research_get(f"{base}?ticker={r['t']}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        tr = d.get("trades") or []
        if not tr and pages == 0 and not hist and r["tclose"] < ARCHIVE_TIER_BEFORE + 5 * 86400:
            hist = True; continue                      # archive tier: retry on the historical endpoint
        for x in tr:
            allp.append([ts_of(x["created_time"]), float(x.get("yes_price_dollars") or 0), float(x.get("count_fp") or x.get("count") or 0),
                         x.get("taker_side"), bool(x.get("is_block_trade"))])
        pages += 1; cursor = d.get("cursor") or ""
        if not cursor or not tr or pages >= 3:
            return allp, bool(cursor)


def fetch(n: int) -> None:
    T = targets(); done = 0
    print("targets", len(T), "research calls used so far", used("research"))
    for r in T[:n]:
        try:
            p, trunc = prints_of(r)
        except BudgetExhausted:
            print("research cap reached"); break
        done += 1
    print("fetched", done, "research calls used", used("research"))


def report() -> dict:
    T = targets(); rows = []
    for r in T:
        try:
            p, trunc = prints_of(r)                    # cache hits only after fetch()
        except BudgetExhausted:
            continue
        lo, hi = r["post"] + 120, r["win_end"]
        w = [x for x in p if lo < x[0] <= hi and not x[4]]
        thr = sum(x[2] for x in w if x[1] > r["s"] + EPS)
        mthr = sum(x[2] for x in w if r["mb"] is not None and x[1] < r["mb"] - EPS)
        msides = defaultdict(float)
        for x in w:
            if r["mb"] is not None and x[1] < r["mb"] - EPS:
                msides[x[3]] += x[2]
        Nb = int(5.0 // r["mb"]) if r["mb"] else 0
        rows.append({**r, "x_f": float(min(r["N"], thr)), "x_mf": float(min(Nb, mthr)) if r["mb"] else None, "x_n_prints": len(w),
                     "x_trunc": trunc, "x_mirror_taker_sides": dict(msides),
                     "x_first_mirror_print_h_after_post": round((min(x[0] for x in w if x[1] < r["mb"] - EPS) - r["post"]) / 3600, 1)
                     if mthr > 0 else None})
    res = {"orders_checked": len(rows), "research_calls": used("research")}

    def cell(sel, side):
        if side == "sell":
            proxy = [dict(e=r["e"], f=r["f"], q=r["q"], won=r["won"], tclose=r["tclose"]) for r in sel if r["f"]]
            exact = [dict(e=r["e"], f=r["x_f"], q=r["q"], won=r["won"], tclose=r["tclose"]) for r in sel if r["x_f"]]
        else:
            proxy = [dict(e=r["e"], f=r["mf"], q=r["mb"], won=not r["won"], tclose=r["tclose"]) for r in sel if r.get("mf")]
            exact = [dict(e=r["e"], f=r["x_mf"], q=r["mb"], won=not r["won"], tclose=r["tclose"]) for r in sel if r.get("x_mf")]
        return {"proxy_sw": size_weighted([dict(x) for x in proxy]), "exact_sw": size_weighted([dict(x) for x in exact]),
                "exact_eq": equal_dollar([dict(x) for x in exact]),
                "proxy_fills": len(proxy), "exact_fills": len(exact),
                "proxy_contracts": round(sum(x["f"] for x in proxy), 1), "exact_contracts": round(sum(x["f"] for x in exact), 1)}
    mir = [r for r in rows if r.get("mf")]
    res["P_mirror_r2_r3"] = cell(mir, "mirror")
    res["P_mirror_r3"] = cell([r for r in mir if r["fam"] == "r3"], "mirror")
    res["P_mirror_r2"] = cell([r for r in mir if r["fam"] == "r2"], "mirror")
    sides = defaultdict(float)
    for r in mir:
        for k, v in r["x_mirror_taker_sides"].items():
            sides[k] += v
    res["P_mirror_taker_sides"] = {k: round(v, 1) for k, v in sides.items()}
    hs = [r["x_first_mirror_print_h_after_post"] for r in mir if r["x_first_mirror_print_h_after_post"] is not None]
    res["P_mirror_first_print_hours_after_post_median"] = st.median(hs) if hs else None
    res["P_mirror_first_print_within_48h_share"] = round(st.mean(h <= 48 for h in hs), 3) if hs else None
    sv = [r for r in rows if r["fam"] == "r3" and r["part"] == "val" and r["f"]]
    res["r3_val_P_sell"] = cell(sv, "sell")
    res["truncated_windows"] = sum(r["x_trunc"] for r in rows)
    (OUT / "prints_check.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "prints_check_rows.jsonl").write_text("".join(json.dumps({k: v for k, v in r.items()}) + "\n" for r in rows))
    print(json.dumps(res, indent=1, default=str)[:6000])
    return res


if __name__ == "__main__":
    a = sys.argv[1:] or ["report"]
    if a[0] == "fetch":
        fetch(int(a[1]) if len(a) > 1 else 200)
    else:
        report()
