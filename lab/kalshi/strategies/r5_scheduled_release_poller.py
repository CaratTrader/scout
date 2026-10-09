"""r5_scheduled_release_poller: does a home poller of a scheduled web release beat Kalshi's repricing by enough to buy the
already-decided side at <= 0.90 a minute later?

Backtestable leg: the Billboard 200 Sunday reveal (billboard.com post, normally Sun 15:00:0x ET) vs Kalshi KXTOPALBUM,
which stays open until Sun 23:59 ET. First serve = WordPress date_gmt of the first qualifying post (frozen title rule,
r5_scheduled_release_poller_parse.py) + POLL_S (worst case of a 15 s poll). Decision = the parse of that title (no
settlement data). Fill = taker, `delay` seconds after first serve, at the WORSE of (a) the quote as of the fill second
(last 1-minute candle end <= t_fill, or the open of the next candle when nothing printed in between) and (b) the
close of the minute containing t_fill; YES at yes_ask, NO at 1 - yes_bid; carried quotes must be <= 30 min old; fee = ceil(0.07 p (1-p) * 10 contracts) per 10-lot order; held to settlement.

Other legs (House clerk XML, SCOTUS slip opinions, Netflix Top 10, Hot 100) have no recoverable posting times or close
before the publication; see r5_scheduled_release_poller_data.py and the forward logger.

Split: events ordered by close; discovery = first 70%, validation = last 30%. Variants (all counted): delay in {30, 60,
120} s x cap in {0.80, 0.90, 0.95} x sides in {YES only, YES + NO} = 18. At most 3 frozen candidates go to validation.
Outputs: data/kalshi_lab/strategies/r5_scheduled_release_poller/{trades.json, lags.json, result.json}
Usage: python -m lab.kalshi.strategies.r5_scheduled_release_poller"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.calib import cell_stats  # noqa: E402

OUT = ROOT / "data/kalshi_lab/strategies/r5_scheduled_release_poller"
POLL_S = 15
MAX_AGE = 30 * 60
CONTRACTS = 10
DELAYS = (30, 60, 120)
CAPS = (0.80, 0.90, 0.95)
SIDES = ("yes", "both")
FROZEN = {"delay": 60, "cap": 0.90, "sides": "both"}      # the round-4 lead's rule, fixed before this study
K_LAB = 18078 + 18 + 4                                     # lab K before this family + this family's variants (+ lag cells)


def fee_pc(p: float) -> float:
    """Per-contract taker fee for a 10-lot order, rounded up to the cent per order."""
    return math.ceil(round(0.07 * p * (1 - p) * CONTRACTS * 100, 6)) / 100 / CONTRACTS


def asof(c: list, t: float):
    """Book state at second t: the last candle ending <= t (if <= 30 min old); else, when nothing printed between t and
    the next candle, that candle's open (Kalshi prints a candle only when something changes, so its open is the state
    that held at t). Returns a candle-shaped row [ts, ask, bid, ...] or None."""
    best = None; nxt = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            nxt = r
            break
    if best is not None and t - best[0] <= MAX_AGE:
        return best
    if nxt is not None and len(nxt) > 9 and nxt[0] - 60 >= t and nxt[8] is not None and nxt[9] is not None:
        return [t, nxt[8], nxt[9]] + [None] * 5
    return None


def minute_close(c: list, t: float):
    """Candle of the minute containing t (end_ts = ceil to minute), if one was printed; else the as-of state."""
    end = int(math.ceil(t / 60.0) * 60)
    for r in c:
        if r[0] == end:
            return r
        if r[0] > end:
            break
    return asof(c, t)


def side_px(r, side: str) -> float | None:
    if r is None:
        return None
    if side == "yes":
        return r[1] if r[1] is not None and 0 < r[1] < 1 else None
    return (1 - r[2]) if r[2] is not None and 0 < r[2] < 1 else None


def fill_px(c: list, t: float, side: str) -> float | None:
    a, b = side_px(asof(c, t), side), side_px(minute_close(c, t), side)
    if a is None and b is None:
        return None
    return max(x for x in (a, b) if x is not None)


def lag_to(c: list, t0: float, side: str, lvl: float = 0.97) -> float | None:
    """Seconds after t0 until the decided side's taker price is >= lvl (minute resolution: candle end time)."""
    r0 = asof(c, t0)
    if r0 is not None and (side_px(r0, side) or 0) >= lvl:
        return 0.0
    for r in c:
        if r[0] > t0 and (side_px(r, side) or 0) >= lvl:
            return r[0] - t0
    return None


def clopper_lo(k: int, n: int, alpha: float = 0.05) -> float:
    if n == 0:
        return 0.0
    if k == n:
        return alpha ** (1 / n)
    if k == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(60):
        p = (lo + hi) / 2
        tail = sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
        lo, hi = (p, hi) if tail < alpha else (lo, p)
    return lo


def load():
    ev = json.loads((OUT / "b200_events.json").read_text())
    cd = {}
    for l in (OUT / "candles.jsonl").open():
        d = json.loads(l)
        cd[d["t"]] = sorted(d["c"], key=lambda r: r[0])
    return ev, cd


def trades_for(r: dict, cd: dict, delay: int, cap: float, sides: str) -> list[dict]:
    out = []
    if not r["t_art"]:
        return out
    t_serve = r["t_art"] + POLL_S
    t_fill = t_serve + delay
    legs = ([(r["yes"], "yes")] if r["yes"] else []) + ([(t, "no") for t in r["no"]] if sides == "both" else [])
    res = {m["t"]: m["result"] for m in r["markets"]}
    vol = {m["t"]: m.get("vol") for m in r["markets"]}
    for tk, side in legs:
        c = cd.get(tk)
        if not c:
            continue
        px = fill_px(c, t_fill, side)
        if px is None or px > cap:
            continue
        won = res.get(tk) == side
        f = fee_pc(px)
        pnl = (1.0 if won else 0.0) - px - f
        mvol = sum(x[7] for x in c if t_fill - 60 <= x[0] <= t_fill + 120)
        out.append({"e": r["e"], "t": tk, "side": side, "px": round(px, 4), "fee": f, "won": won, "ret": pnl / px,
                    "close": r["close"], "t_art": r["t_art"], "t_fill": t_fill, "vol_fill_3m": mvol, "vol_life": vol.get(tk),
                    "pre_px": side_px(asof(c, r["t_art"] - 120), side)})
    return out


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    s = cell_stats(rows)
    ev = defaultdict(list)
    for x in rows:
        ev[x["e"]].append(x)
    k = sum(1 for v in ev.values() if all(x["won"] for x in v))
    p_lo = clopper_lo(k, len(ev))
    s["binom_lo_ret"] = st.mean((p_lo - x["px"] - x["fee"]) / x["px"] for x in rows)
    s["t_if_nan"] = "zero-variance or <3 events" if not (s["t"] == s["t"]) else None
    return s


def halves(rows: list[dict]) -> tuple[float | None, float | None]:
    if not rows:
        return None, None
    es = sorted({x["close"] for x in rows})
    mid = es[len(es) // 2] if len(es) > 1 else es[0]
    a = [x["ret"] for x in rows if x["close"] < mid]; b = [x["ret"] for x in rows if x["close"] >= mid]
    return (st.mean(a) if a else None), (st.mean(b) if b else None)


def main() -> None:
    ev, cd = load()
    ev = sorted([r for r in ev if r["t_art"]], key=lambda r: r["close"])
    cut = ev[int(round(0.7 * len(ev))) - 1]["close"]
    disc = [r for r in ev if r["close"] <= cut]; val = [r for r in ev if r["close"] > cut]

    # ---------------------------------------------------------------- lag table (descriptive, all events)
    lags = []
    for r in ev:
        legs = ([(r["yes"], "yes")] if r["yes"] else []) + [(t, "no") for t in r["no"]]
        best = None
        for tk, side in legs:
            c = cd.get(tk)
            if not c:
                continue
            pre = side_px(asof(c, r["t_art"] - 120), side)
            if pre is None:
                continue
            gap = 1 - pre
            if best is None or gap > best["gap"]:
                path = {f"{k:+d}s": side_px(asof(c, r["t_art"] + k), side) for k in (-1800, -600, -120, 0, 60, 120, 180, 300, 600, 1200)}
                best = {"e": r["e"], "t": tk, "side": side, "gap": round(gap, 3), "pre": pre, "path": path,
                        "lag97_s": lag_to(c, r["t_art"], side), "lag90_s": lag_to(c, r["t_art"], side, 0.90),
                        "pre_move_30m": (None if path["-1800s"] is None else round(pre - path["-1800s"], 3)),
                        "t_art_et": r["title"][:0] or None}
        if best:
            lags.append(best)
    surprise = [x for x in lags if x["gap"] >= 0.10]
    lag_summary = {
        "events_with_book": len(lags), "surprise_events_gap_ge_10c": len(surprise),
        "median_lag97_after_article_s": (st.median([x["lag97_s"] for x in surprise if x["lag97_s"] is not None]) if surprise else None),
        "median_lag90_after_article_s": (st.median([x["lag90_s"] for x in surprise if x["lag90_s"] is not None]) if surprise else None),
        "share_repriced_97_before_first_serve": (sum(1 for x in surprise if x["lag97_s"] is not None and x["lag97_s"] <= POLL_S) / len(surprise)) if surprise else None,
        "share_lag97_gt_75s": (sum(1 for x in surprise if x["lag97_s"] is None or x["lag97_s"] > POLL_S + 60) / len(surprise)) if surprise else None,
        "pre_move_toward_result_30m_median": (st.median([x["pre_move_30m"] for x in surprise if x["pre_move_30m"] is not None]) if surprise else None),
    }

    # ---------------------------------------------------------------- discovery grid
    grid = []
    for d in DELAYS:
        for cap in CAPS:
            for sd in SIDES:
                rows = [x for r in disc for x in trades_for(r, cd, d, cap, sd)]
                s = stats(rows)
                grid.append({"delay": d, "cap": cap, "sides": sd, **{k: s.get(k) for k in ("n", "events", "win", "px", "ret", "t", "ret_wo3", "binom_lo_ret")}})
    ok = [g for g in grid if (g["n"] or 0) >= 3]
    best = max(ok, key=lambda g: (g["ret"] if g["t"] != g["t"] or g["t"] is None else g["ret"]), default=None)

    # ---------------------------------------------------------------- validation (<= 3 frozen candidates)
    cands = [("A frozen round-4 rule: delay 60 s, cap 0.90, YES+NO", FROZEN)]
    if best and (best["delay"], best["cap"], best["sides"]) != (FROZEN["delay"], FROZEN["cap"], FROZEN["sides"]):
        cands.append((f"B discovery-best: delay {best['delay']} s, cap {best['cap']}, {best['sides']}",
                      {"delay": best["delay"], "cap": best["cap"], "sides": best["sides"]}))
    cands.append(("C slow reaction: delay 120 s, cap 0.90, YES+NO", {"delay": 120, "cap": 0.90, "sides": "both"}))
    cands = cands[:3]
    span_days = max(1.0, (val[-1]["close"] - val[0]["close"]) / 86400) if val else 1.0
    validation, all_val_rows = [], {}
    for name, p in cands:
        rows = [x for r in val for x in trades_for(r, cd, p["delay"], p["cap"], p["sides"])]
        s = stats(rows); h1, h2 = halves(rows)
        caps = sorted(x["vol_fill_3m"] for x in rows)
        validation.append({"rule": name, "n": s.get("n", 0), "events": s.get("events", 0), "win": s.get("win"),
                           "avg_px": s.get("px"), "ret_per_dollar": s.get("ret"), "t": s.get("t"),
                           "ret_wo3": s.get("ret_wo3"), "half1": h1, "half2": h2, "binom_lo_ret": s.get("binom_lo_ret"),
                           "median_capacity_contracts": (st.median(caps) if caps else None),
                           "trades_per_day": (s.get("n", 0) / span_days)})
        all_val_rows[name] = rows
    # full-sample (descriptive) for the frozen rule
    full = [x for r in ev for x in trades_for(r, cd, FROZEN["delay"], FROZEN["cap"], FROZEN["sides"])]
    (OUT / "trades.json").write_text(json.dumps({"frozen_full_sample": full, "validation": all_val_rows}, indent=1))
    (OUT / "lags.json").write_text(json.dumps({"summary": lag_summary, "events": lags}, indent=1))
    out = {"split_cut_close": cut, "n_events_with_article": len(ev), "n_disc": len(disc), "n_val": len(val),
           "grid_discovery": grid, "discovery_best": best, "validation": validation,
           "frozen_full_sample": stats(full), "lag_summary": lag_summary, "K_lab": K_LAB,
           "t_bar": None}
    from statistics import NormalDist
    out["t_bar"] = NormalDist().inv_cdf(1 - 0.05 / K_LAB)
    (OUT / "analysis.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({k: out[k] for k in ("n_events_with_article", "n_disc", "n_val", "discovery_best", "validation",
                                          "frozen_full_sample", "lag_summary", "t_bar")}, indent=1, default=str))


if __name__ == "__main__":
    main()
