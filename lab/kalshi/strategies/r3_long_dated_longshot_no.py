"""r3_long_dated_longshot_no: is the longshot-YES overpricing large enough in long-dated retail markets (Politics,
Science/Tech, Companies, Economics) that buying NO at 0.80-0.92 between 60 and 14 days before the scheduled deadline
earns >= +10% per trade after fees?

Data (lab/kalshi/strategies/r3_long_dated_longshot_no_data.py): 17 high-volume long-dated series chosen from the
/series volume ranking; archive + live listings; seeded random sample of <= 2 markets per event among binary markets
with a scheduled life >= 45 d; hourly candles over [deadline - 62 d, deadline].

Clock (leak-free): S = scheduled deadline parsed from text fixed at listing (title date, event-ticker date, scheduled
announcement), never the actual close (it moves when a market closes early on YES). Decision at the hour boundary
H = floor(S - D days); signal = quote of the last hourly candle <= H (carried forward <= CF h; Kalshi emits a candle
only when the quote or a trade changes); fill = taker at the quote <= H + 1 h (NO at 1 - yes_bid, YES at yes_ask);
the market must still be open at H + 1 h. Fee: Kalshi taker 0.07 p (1 - p), rounded up to the cent on a 6-contract
($5) order. Hold to settlement. Return per $ = pnl / price; per $-month = sum(pnl) / sum(price x months held).
Split: events ordered by their latest close; discovery = first 70%, validation = last 30%.
External check of the same rule at scale (no Kalshi calls): r3_long_dated_longshot_no_poly.py (Polymarket).
Result (2026-10-08): Kalshi discovery +16%/$ on 11 trades / 8 events with no loss, validation 3 trades; the same
rule on 222 Polymarket trades / 162 events earns +0.6%/$ (win 88.3% at avg price 0.869). Verdict: dead.
Usage: python -m lab.kalshi.strategies.r3_long_dated_longshot_no"""
from __future__ import annotations
import hashlib, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_long_dated_longshot_no_api import OUT, CACHE, used

DAY, H1 = 86400, 3600
SPLIT = 0.7
DGRID = (60, 45, 30, 21, 14)
BANDS = ((0.70, 0.80), (0.80, 0.86), (0.86, 0.92), (0.80, 0.92), (0.92, 0.97))
CF = 72          # hours a quote may be carried forward


def fee(p: float, n: int = 6) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def cached(path: str) -> dict:
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    return json.loads(f.read_text()) if f.exists() else {}


def _px(c: dict, side: str) -> float | None:
    x = c.get(side) or {}
    v = x.get("close", x.get("close_dollars"))
    return float(v) if v is not None else None


def load() -> list[dict]:
    """Sampled markets with hourly candles [ts, yes_ask, yes_bid, volume], re-parsed from the raw cached responses."""
    out = []
    for l in (OUT / "sample.jsonl").open():
        r = json.loads(l)
        lo = max(r["open"], r["S"] - 62 * DAY) - 3600; hi = min(r["close"], r["S"]) + 3600
        if r["tier"] == "hist":
            cs = cached(f"/historical/markets/{r['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60").get("candlesticks", [])
        else:
            mk = cached(f"/markets/candlesticks?market_tickers={r['t']}&start_ts={lo}&end_ts={hi}&period_interval=60").get("markets", [])
            cs = mk[0].get("candlesticks", []) if mk else []
        r["c"] = sorted([c["end_period_ts"], _px(c, "yes_ask"), _px(c, "yes_bid"), float(c.get("volume") or c.get("volume_fp") or 0)] for c in cs)
        out.append(r)
    return out


def quote(c: list, t: int, cf: int = CF):
    best = None
    for row in c:
        if row[0] <= t:
            best = row
        else:
            break
    if not best or t - best[0] > cf * H1 or best[1] is None or best[2] is None:
        return None
    return best[1], best[2]


def trades(M: list[dict], cf: int = CF, delay_h: int = 1) -> list[dict]:
    T = []
    for r in M:
        settle = r.get("settled") or r["close"]
        for D in DGRID:
            H = (r["S"] - D * DAY) // H1 * H1; F = H + delay_h * H1
            if H < r["open"] or F >= r["close"]:
                continue                                 # not listed yet / already closed (e.g. resolved YES early)
            qs, qf = quote(r["c"], H, cf), quote(r["c"], F, cf)
            if not qs or not qf:
                continue
            for side, ps, pf, won in (("NO", 1 - qs[1], 1 - qf[1], r["result"] == "no"),
                                      ("YES", qs[0], qf[0], r["result"] == "yes")):
                if not (0.02 <= pf <= 0.99):
                    continue
                pnl = (1.0 if won else 0.0) - pf - fee(pf)
                months = max((settle - F) / (30 * DAY), 1 / 30)
                T.append({"t": r["t"], "e": r["e"], "series": r["series"].removeprefix("KX"), "cat": r["cat"], "D": D,
                          "side": side, "ps": ps, "px": pf, "won": won, "pnl": pnl, "ret": pnl / pf, "months": months,
                          "t_close": r["ev_close"], "fill_ts": F})
    return T


def stats(rows: list[dict], key: str = "e") -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for x in rows:
        ev[x[key]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))) if len(em) > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((x["ret"] for x in rows), reverse=True)
    mid = sorted(x["t_close"] for x in rows)[len(rows) // 2]
    h1 = [x["ret"] for x in rows if x["t_close"] < mid]; h2 = [x["ret"] for x in rows if x["t_close"] >= mid]
    return {"n": len(rows), "events": len(em), "win": sum(x["won"] for x in rows) / len(rows), "avg_px": st.mean(x["px"] for x in rows),
            "ret_per_dollar": st.mean(x["ret"] for x in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "per_dollar_month": sum(x["pnl"] for x in rows) / sum(x["px"] * x["months"] for x in rows),
            "avg_months": st.mean(x["months"] for x in rows),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
            "ev_mean": st.mean(em)}


def fmt(k: str, v: dict) -> str:
    if not v.get("n"):
        return f"  {k:34s} n=0"
    return (f"  {k:34s} n={v['n']:3d} ev={v['events']:3d} win={v['win']:.0%} px={v['avg_px']:.3f} ret={v['ret_per_dollar']:+.1%} "
            f"t={v['t']:5.2f} wo3={v['ret_wo3']:+.1%} $mo={v['per_dollar_month']:+.1%} hold={v['avg_months']:.1f}mo "
            f"halves={v['half1']:+.1%}/{v['half2']:+.1%}")


def cell(rows, side, band, D=None, uniq=False):
    x = [r for r in rows if r["side"] == side and band[0] <= r["ps"] < band[1] and (D is None or r["D"] in D)]
    if uniq:                                             # one trade per market: the earliest qualifying decision time
        seen = {}
        for r in sorted(x, key=lambda r: -r["D"]):
            seen.setdefault(r["t"], r)
        x = list(seen.values())
    return x


def main() -> None:
    M = load()
    evc = defaultdict(int)
    for r in M:
        evc[r["e"]] = max(evc[r["e"]], r["close"])
    for r in M:
        r["ev_close"] = evc[r["e"]]
    events = sorted(evc, key=lambda e: evc[e]); cut = evc[events[int(len(events) * SPLIT)]]
    T = trades(M)
    disc = [x for x in T if x["t_close"] < cut]; val = [x for x in T if x["t_close"] >= cut]
    lines = [f"markets {len(M)} events {len(events)}; trade rows {len(T)} (disc {len(disc)}, val {len(val)}); "
             f"split at event close {cut} ({__import__('datetime').datetime.utcfromtimestamp(cut):%Y-%m-%d})"]
    cells = {}
    lines.append("\nDISCOVERY cells (side, band on signal price, decision D days before the scheduled deadline)")
    for side in ("NO", "YES"):
        for band in BANDS:
            for D in [(d,) for d in DGRID] + [(60, 30, 14), DGRID]:
                k = f"{side} {band[0]:.2f}-{band[1]:.2f} D{'/'.join(map(str, D))}"
                v = stats(cell(disc, side, band, D)); cells[k] = v
                if v.get("n", 0) >= 5:
                    lines.append(fmt(k, v))
            k = f"{side} {band[0]:.2f}-{band[1]:.2f} first-entry D60..14"
            v = stats(cell(disc, side, band, None, uniq=True)); cells[k] = v
            if v.get("n", 0) >= 5:
                lines.append(fmt(k, v))
    # sensitivity of the headline cell to the carry-forward cap and the fill delay
    head = ("NO", (0.80, 0.92), (60, 30, 14))
    for cf, dl in ((24, 1), (240, 1), (72, 24)):
        Tx = [x for x in trades(M, cf, dl) if x["t_close"] < cut]
        k = f"NO 0.80-0.92 D60/30/14 cf{cf}h delay{dl}h"; v = stats(cell(Tx, *head)); cells[k] = v; lines.append(fmt(k, v))
    v = stats(cell(disc, *head), key="series"); lines.append(fmt("NO 0.80-0.92 D60/30/14 clustered by SERIES", v))
    lines.append("\nHEADLINE (pre-specified): NO 0.80-0.92 at D in {60,30,14}, by series (discovery)")
    by_s = defaultdict(list)
    for x in cell(disc, *head):
        by_s[x["series"]].append(x)
    for s, xs in sorted(by_s.items()):
        lines.append(fmt(s, stats(xs)))
    lines.append("\nALL DISCOVERY TRADES of the headline cell")
    for x in sorted(cell(disc, *head), key=lambda x: x["fill_ts"]):
        lines.append(f"  {x['t']:34s} D{x['D']:2d} ps={x['ps']:.2f} px={x['px']:.2f} won={x['won']} ret={x['ret']:+.2f} hold={x['months']:.1f}mo")
    # frozen candidates (chosen before looking at validation): the pre-specified headline, plus the two best discovery
    # cells with n >= 10 among NO-side cells
    cands = ["NO 0.80-0.92 D60/30/14"]
    best = sorted([(k, v) for k, v in cells.items() if k.startswith("NO") and "cf" not in k and v.get("n", 0) >= 10 and k not in cands],
                  key=lambda kv: -kv[1]["ret_per_dollar"])
    cands += [k for k, _ in best[:2]]
    lines.append("\nVALIDATION (frozen candidates, evaluated once)")
    vres = []
    for k in cands:
        side, b, rest = k.split(" ", 2)
        band = tuple(map(float, b.split("-")))
        if rest.startswith("first-entry"):
            rows = cell(val, side, band, None, uniq=True)
        else:
            rows = cell(val, side, band, tuple(map(int, rest[1:].split("/"))))
        v = stats(rows); v["rule"] = k; vres.append(v); lines.append(fmt(k, v))
        for x in sorted(rows, key=lambda x: x["fill_ts"]):
            lines.append(f"      {x['t']:34s} D{x['D']:2d} ps={x['ps']:.2f} px={x['px']:.2f} won={x['won']} ret={x['ret']:+.2f}")
    lines.append("\nFULL SAMPLE (discovery + validation), headline cell, for context only")
    lines.append(fmt("NO 0.80-0.92 D60/30/14 all", stats(cell(T, *head))))
    lines.append(fmt("NO 0.80-0.92 first-entry all", stats(cell(T, "NO", (0.80, 0.92), None, uniq=True))))
    lines.append(fmt("NO 0.92-0.97 D60/30/14 all", stats(cell(T, "NO", (0.92, 0.97), (60, 30, 14)))))
    lines.append(fmt("NO 0.70-0.80 D60/30/14 all", stats(cell(T, "NO", (0.70, 0.80), (60, 30, 14)))))
    lines.append(f"\nKalshi calls used (whole task): {used()}")
    txt = "\n".join(lines); print(txt)
    (OUT / "analysis.txt").write_text(txt)
    (OUT / "cells.json").write_text(json.dumps({"discovery": cells, "validation": vres, "candidates": cands}, indent=1, default=str))
    (OUT / "trades.jsonl").write_text("".join(json.dumps(x) + "\n" for x in T))


if __name__ == "__main__":
    main()
