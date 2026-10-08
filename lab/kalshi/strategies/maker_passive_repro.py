"""Adversarial reproduction of the maker_passive validation claim, written from its description only (the original
code was not read).

Claimed frozen candidates (validation = last 30% of each family's events by close time):
  A  rain NO maker: NO mid 0.65-0.97 at close-600m, bid = NO bid - 5c, live until close (no maker fee)
  B  indexH NO favourite dip: NO mid 0.85-0.97 at close-30m, bid = NO bid - 10c, live until close
  C  in-play favourite dip (sports+tennis+esports, YES side): YES mid 0.85-0.97 at exp-120m, bid = 75% of mid,
     live 120 min, maker fee where the series charges one (0.25 x 0.07 p(1-p) x fee_multiplier)

Fill model (as described): a resting bid fills at its own limit only if the opposite side trades THROUGH it inside the
order's life: YES bid b fills when a later candle has yes_ask_low < b; NO bid b fills when yes_bid_high > 1 - b.
Held to settlement. Return per $ = (won - b - maker_fee) / b. t clustered by event (lab.kalshi.calib.cell_stats).

Robustness switches (each one an execution-detail ambiguity in the description):
  post_delay   minutes between the decision quote and the order going live (0 or 1)
  past_close   include candles stamped after the close (the post-close "cleared book" candle)
  split        'event' (last 30% of events by event close) or 'market' (calib-style unique market close times)
  rounding     limit price rounding: 'round' to the cent or 'floor'
  incl_hi      mid band upper bound inclusive
Usage: python -m lab.kalshi.strategies.maker_passive_repro [grid]"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import FAMILY, quote, cell_stats, MK, CD

OUT = Path("data/kalshi_lab/strategies/maker_passive/repro")
FEE_INFO = {s["ticker"]: (s.get("fee_type"), float(s.get("fee_multiplier") or 1))
            for s in json.load(open("data/kalshi_lab/series_all.json"))["series"]}

RULES = {
    "A_rain_NO_d05": dict(fams=("rain",), side="NO", lo=0.65, hi=0.97, anchor="close", off=-600, price=("under", 0.05), life=None),
    "B_indexH_NOfav_d10": dict(fams=("indexH",), side="NO", lo=0.85, hi=0.97, anchor="close", off=-30, price=("under", 0.10), life=None),
    "C_inplay_fav_f25": dict(fams=("sports", "tennis", "esports"), side="YES", lo=0.85, hi=0.97, anchor="exp", off=-120, price=("frac", 0.75), life=120),
}
CLAIM = {"A_rain_NO_d05": dict(n=35, events=13, ret=0.0601, t=2.58, posted=60),
         "B_indexH_NOfav_d10": dict(n=34, events=16, ret=-0.1458, t=-0.72, posted=111),
         "C_inplay_fav_f25": dict(n=16, events=16, ret=-0.0573, t=-0.31, posted=142)}

_cache: dict = {}


def load(series: str) -> list[tuple[dict, list]]:
    if series not in _cache:
        mf, cf = MK / f"{series}.jsonl", CD / f"{series}.jsonl"
        rows = []
        if mf.exists() and cf.exists():
            meta = {m["t"]: m for m in map(json.loads, mf.open())}
            for line in cf.open():
                x = json.loads(line); m = meta.get(x["t"])
                if m and x["c"] and m.get("close"):
                    rows.append((m, x["c"]))
        _cache[series] = rows
    return _cache[series]


def maker_fee(series: str, p: float) -> float:
    ft, mult = FEE_INFO.get(series, ("quadratic", 1.0))
    return 0.25 * 0.07 * p * (1 - p) * mult if ft == "quadratic_with_maker_fees" else 0.0


def cents(x: float, mode: str) -> float:
    return math.floor(x * 100 + 1e-9) / 100 if mode == "floor" else round(x * 100) / 100


def simulate(name: str, post_delay: int = 0, past_close: bool = False, rounding: str = "round", incl_hi: bool = True) -> list[dict]:
    r = RULES[name]; out = []
    for s, fam in FAMILY.items():
        if fam not in r["fams"]:
            continue
        for m, c in load(s):
            t = (m["close"] if r["anchor"] == "close" else m["exp"]) + r["off"] * 60
            if r["anchor"] == "exp" and (not m.get("exp") or t + 60 >= m["close"]):
                continue   # already closed at decision time (known at t), no order possible
            q = quote(c, t)
            if not q:
                continue
            ask, bid = q
            if r["side"] == "YES":
                mid, side_bid = (ask + bid) / 2, bid
            else:
                mid, side_bid = 1 - (ask + bid) / 2, 1 - ask
            if not (r["lo"] <= mid and (mid <= r["hi"] if incl_hi else mid < r["hi"])):
                continue
            kind, k = r["price"]
            b = cents(side_bid - k if kind == "under" else mid * k, rounding)
            if not (0.01 <= b <= 0.99):
                continue
            start = t + post_delay * 60
            end = m["close"] if r["life"] is None else min(m["close"], start + r["life"] * 60)
            if past_close and r["life"] is None:
                end = m["close"] + 120
            fill = None
            for row in c:
                ts, a_lo, b_hi = row[0], row[3], row[4]
                if ts <= start or ts > end:
                    continue
                if r["side"] == "YES" and a_lo is not None and a_lo < b - 1e-9:
                    fill = row; break
                if r["side"] == "NO" and b_hi is not None and b_hi > 1 - b + 1e-9:
                    fill = row; break
            won = (m["result"] == "yes") == (r["side"] == "YES")
            f = maker_fee(s, b)
            out.append({"fam": fam, "s": s, "tk": m["t"], "e": m["e"], "t_close": m["close"], "px": b, "won": won, "filled": fill is not None,
                        "fill_ts": fill[0] if fill else None, "fill_vol": fill[5] if fill else None, "mid": mid,
                        "ret": ((1.0 if won else 0.0) - b - f) / b})
    return out


def cutoffs(fams: tuple, mode: str) -> dict:
    cut = {}
    for fam in fams:
        ms = [m for s, f in FAMILY.items() if f == fam for m, _ in load(s)]
        if mode == "event":
            ev = defaultdict(int)
            for m in ms:
                ev[m["e"]] = max(ev[m["e"]], m["close"])
            closes = sorted(ev.values())
        else:
            closes = sorted({m["close"] for m in ms})
        cut[fam] = closes[int(len(closes) * 0.7)] if closes else 0
    return cut


def evaluate(rows: list[dict], cut: dict, mode: str, period: str = "val") -> dict:
    ev_close = defaultdict(int)   # event close from ALL markets of the event (not only those with an order)
    for fam in {r["fam"] for r in rows}:
        for s, f in FAMILY.items():
            if f == fam:
                for m, _ in load(s):
                    ev_close[m["e"]] = max(ev_close[m["e"]], m["close"])
    key = (lambda r: ev_close[r["e"]]) if mode == "event" else (lambda r: r["t_close"])
    sel = [r for r in rows if (key(r) >= cut[r["fam"]]) == (period == "val")]
    fills = [r for r in sel if r["filled"]]
    res = {"posted": len(sel), "fills": len(fills), "fill_rate": len(fills) / len(sel) if sel else None}
    if len(fills) >= 3:
        res.update(cell_stats(fills))
        mid = sorted(r["t_close"] for r in fills)[len(fills) // 2]
        h1 = [r["ret"] for r in fills if r["t_close"] < mid]; h2 = [r["ret"] for r in fills if r["t_close"] >= mid]
        res["half1"] = st.mean(h1) if h1 else None; res["half2"] = st.mean(h2) if h2 else None
        span = (max(r["t_close"] for r in sel) - min(r["t_close"] for r in sel)) / 86400
        res["trades_per_day"] = len(fills) / span if span > 0 else None
        res["median_fill_candle_volume"] = st.median(r["fill_vol"] for r in fills)
        unf = [r for r in sel if not r["filled"]]
        res["win_unfilled"] = sum(r["won"] for r in unf) / len(unf) if unf else None
        res["ret_if_all_posted_filled"] = st.mean(r["ret"] for r in sel)
        res["by_family"] = {f: cell_stats([r for r in fills if r["fam"] == f]) for f in sorted({r["fam"] for r in fills})
                            if sum(1 for r in fills if r["fam"] == f) >= 3}
    return res


def run(opts: dict) -> dict:
    out = {}
    for name, r in RULES.items():
        rows = simulate(name, opts["post_delay"], opts["past_close"], opts["rounding"], opts["incl_hi"])
        cut = cutoffs(r["fams"], opts["split"])
        out[name] = {"validation": evaluate(rows, cut, opts["split"], "val"), "discovery": evaluate(rows, cut, opts["split"], "disc")}
    return out


# Settings that reproduce the claimed numbers exactly (found by the grid below; every other combination is within ~3pp):
# order live from decision + 1 min, last 30% of EVENTS by event close, band upper bound exclusive, limit floored to the cent.
BASE = dict(post_delay=1, past_close=False, split="event", rounding="floor", incl_hi=False)


def fmt(v: dict) -> str:
    if "n" not in v:
        return f"posted={v['posted']} fills={v['fills']}"
    return (f"posted={v['posted']:4d} n={v['n']:3d} ev={v['events']:3d} win={v['win']:.0%} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:+.2f} "
            f"wo3={v['ret_wo3']:+.1%} halves={v['half1']:+.1%}/{v['half2']:+.1%} vol={v['median_fill_candle_volume']:.0f}")


def diagnostics() -> dict:
    """Extra checks on A (the only positive validation row). Diagnostics on the frozen rule's neighbourhood are reported,
    never used to pick a new candidate."""
    from lab.kalshi.calib import fee
    o = BASE; rows = simulate("A_rain_NO_d05", o["post_delay"], o["past_close"], o["rounding"], o["incl_hi"])
    cut = cutoffs(("rain",), o["split"]); evc = defaultdict(int)
    for s_, f in FAMILY.items():
        if f == "rain":
            for m, _ in load(s_):
                evc[m["e"]] = max(evc[m["e"]], m["close"])
    val = [r for r in rows if evc[r["e"]] >= cut["rain"]]; fills = sorted((r for r in val if r["filled"]), key=lambda r: r["t_close"])
    data = {m["t"]: (m, c) for s_, f in FAMILY.items() if f == "rain" for m, c in load(s_)}
    taker = {"val": [], "disc": []}
    for r in rows:   # same posting rule, but take NO at 1 - yes_bid one minute after the decision
        m, c = data[r["tk"]]; q = quote(c, m["close"] - 600 * 60 + 60)
        if q and 0.01 <= 1 - q[1] <= 0.99:
            px = 1 - q[1]
            taker["val" if evc[r["e"]] >= cut["rain"] else "disc"].append({"e": r["e"], "px": px, "won": r["won"], "ret": ((1.0 if r["won"] else 0.0) - px - fee(px)) / px})
    k = len(fills) // 2
    out = {"A_full_sample": cell_stats([r for r in rows if r["filled"]]),
           "A_val_halves_by_fill_count": [st.mean(r["ret"] for r in fills[:k]), st.mean(r["ret"] for r in fills[k:])],
           "A_val_volume_confirmed": cell_stats([r for r in fills if (r["fill_vol"] or 0) > 0]),
           "A_taker_same_rule": {p: cell_stats(v) for p, v in taker.items()}, "A_neighbourhood_validation": {}}
    base = dict(RULES["A_rain_NO_d05"])
    for off in (-480, -600, -720):
        for d in (0.03, 0.05, 0.07):
            RULES["_x"] = {**base, "off": off, "price": ("under", d)}
            rr = simulate("_x", o["post_delay"], o["past_close"], o["rounding"], o["incl_hi"])
            out["A_neighbourhood_validation"][f"close{off}m_under{int(d*100)}c"] = {
                "disc": {k_: v for k_, v in evaluate(rr, cut, o["split"], "disc").items() if k_ in ("n", "ret", "t")},
                "val": {k_: v for k_, v in evaluate(rr, cut, o["split"], "val").items() if k_ in ("n", "ret", "t", "half1", "half2")}}
    RULES.pop("_x", None)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    grid = "grid" in sys.argv
    variants = [BASE]
    if grid:
        for k, alts in (("post_delay", [1]), ("past_close", [True]), ("split", ["market"]), ("rounding", ["floor"]), ("incl_hi", [False])):
            for a in alts:
                variants.append({**BASE, k: a})
        variants.append({**BASE, "post_delay": 1, "split": "market"})
    allres = []
    for o in variants:
        res = run(o); allres.append({"opts": o, "res": res})
        print("\nOPTIONS", o)
        for name, v in res.items():
            c = CLAIM[name]
            print(f"  {name:20s} VAL  {fmt(v['validation'])}   [claim posted={c['posted']} n={c['n']} ev={c['events']} ret={c['ret']:+.1%} t={c['t']:+.2f}]")
            print(f"  {'':20s} DISC {fmt(v['discovery'])}")
    (OUT / ("grid.json" if grid else "repro_base.json")).write_text(json.dumps(allres, indent=1, default=str))
    if not grid:
        d = diagnostics(); (OUT / "diagnostics.json").write_text(json.dumps(d, indent=1, default=str))
        print("\nDIAGNOSTICS (A)", json.dumps({k: d[k] for k in ("A_full_sample", "A_val_halves_by_fill_count", "A_taker_same_rule")}, default=str))


if __name__ == "__main__":
    main()
