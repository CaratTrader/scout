"""r4_earnings_seeded_book_48h_forward: does cancelling the earnings-call C1E maker order 48 h after posting keep the
edge (round 3: fills within 48 h of posting +42%/$, later fills +7%, seen post hoc)?

C1E (frozen in data/kalshi_lab/strategies/r3_earnings_call_mentions/preregistration.json): at the first poll >= open + 1 h
of each new KXEARNINGSMENTION* market, if 0 < yes_bid < yes_ask < 1 and the spread is >= 2c, rest SELL-YES (= buy NO at
q = 1 - s) at s = yes_ask - 0.01 for floor(5 / q) contracts; cancel before the call. Makers pay no fee (quadratic).
  arm A  C1E as frozen (cancel before the call)
  arm B  the same order cancelled at min(post + 48 h, arm A's cancel)
  arm C  arm B in a book capped at 10 concurrent orders/positions, earliest-listed first
Fills (gate amendment c): trade prints strictly through the order price (yes_price > s), after post + 120 s and up to
the cancel, size-weighted against N; any-print (yes_price >= s) reported separately.

Parts (no Kalshi calls here; data from r4_earnings_seeded_book_48h_forward_data and round 3's caches):
  insample   round 3's 46 live-tier events (2026-08-05..10-08): where the 48 h split was found. CONTAMINATED context
             only. Candle fills (hourly/daily candles wholly inside the window), contract counts are volume proxies.
  archive    fresh sample: one random market from each of 95 archive events (first close 2025-10-01..2026-08-04),
             never priced by round 3. Exact trade-print fills. Events ordered by first close: discovery = first 70%,
             validation = last 30%. Variant grid on discovery only; <= 3 frozen candidates judged once on validation.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward insample|discovery|validate|all"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_data import OUT, cancel_a_cov, order_of, bounds, resolved, POST_GAP
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_stats import size_weighted, equal_dollar, binom_lb, replay_cap, profile_sim

SPLIT = 0.7
H_B = 48
P_HAT = 0.362            # C1E validation filled-NO win rate (round 3), the live-profile Kelly input
VARIANTS: list[str] = []


# ------------------------------------------------------------------------------------------------ archive (fresh) sample
def load_archive():
    plan = json.loads((OUT / "plan.json").read_text())["rows"]
    C = {}
    for l in (OUT / "candles.jsonl").open():
        x = json.loads(l); C[x["t"]] = x["c"]
    T = {}
    tf = OUT / "trades.jsonl"
    if tf.exists():
        for l in tf.open():
            x = json.loads(l)
            if x["t"] in T and T[x["t"]].get("pages"):
                T[x["t"]]["prints"] += x["prints"]; T[x["t"]]["cursor"] = x["cursor"]; T[x["t"]]["pages"] += 1
                T[x["t"]]["lo_cov"] = min(T[x["t"]]["lo_cov"], x["lo"])
            else:
                x["pages"] = 1; x["lo_cov"] = x["lo"]; T[x["t"]] = x
    return plan, C, T


def fill_from_prints(prints: list, s: float, lo: float, hi: float, N: int) -> dict:
    """prints [ts, yes_price, count, taker_side, is_block]; window lo < ts <= hi; block trades excluded."""
    w = sorted((p for p in prints if lo < p[0] <= hi and not p[4]), key=lambda p: p[0])
    thr = [p for p in w if p[1] > s + 1e-9]
    anyp = [p for p in w if p[1] >= s - 1e-9]
    return {"through": sum(p[2] for p in thr), "any": sum(p[2] for p in anyp),
            "t_through": thr[0][0] if thr else None, "t_any": anyp[0][0] if anyp else None}


def archive_orders(delay_h: int = 0) -> tuple[list[dict], dict]:
    plan, C, T = load_archive()
    out, skip = [], defaultdict(int)
    for r in plan:
        cs = C.get(r["t"])
        if not cs:
            skip["no_candles"] += 1; continue
        CA = cancel_a_cov(r, cs)
        o = order_of(r, cs, delay_h, CA)
        if "s" not in o:
            skip[o["skip"]] += 1; continue
        tr = T.get(r["t"])
        out.append({"e": r["e"], "t": r["t"], "series": r["series"], "open": r["open"], "post": o["P"], "s": o["s"], "q": round(1 - o["s"], 4),
                    "N": o["N"], "ask": o["ask"], "bid": o["bid"], "cancel_A": CA, "tclose": r["first_close"], "won": r["result"] == "no",
                    "prints": tr["prints"] if tr else None, "truncated": bool(tr and tr["cursor"]), "cs": cs,
                    "days_to_cancel": round((CA - o["P"]) / 86400, 2)})
    return out, dict(skip)


def fill_of(o: dict, C: float, measure: str) -> tuple[float | None, str]:
    """Filled contracts in (post + 120 s, C]: exact trade prints when fetched (/historical/trades), else the candle
    bounds when they pin the number down, else None (unresolved; the order is dropped from that cell and counted)."""
    if o["prints"] is not None and not o["truncated"]:
        fm = fill_from_prints(o["prints"], o["s"], o["post"] + POST_GAP, C, o["N"])
        return float(min(o["N"], fm[measure])), "prints"
    L, U = bounds(o["cs"], o["post"], C, o["s"], at=(measure == "any"))
    f = resolved(L, U, o["N"])
    return f, ("candles" if f is not None else "unresolved")


def arm_rows(orders: list[dict], horizon_h: float | None, measure: str = "through", band=None) -> list[dict]:
    rows = []
    for o in orders:
        if band and not (band[0] <= o["q"] < band[1]):
            continue
        C = o["cancel_A"] if horizon_h is None else min(o["post"] + horizon_h * 3600, o["cancel_A"])
        f, src = fill_of(o, C, measure)
        rows.append({"e": o["e"], "t": o["t"], "f": f, "q": o["q"], "won": o["won"], "tclose": o["tclose"], "src": src,
                     "t_fill": None, "post": o["post"], "open": o["open"], "cancel": C, "settle": o["tclose"] + 3600,
                     "N": o["N"], "N_through": f})
    return rows


def describe(rows_all: list[dict]) -> dict:
    rows = [r for r in rows_all if r["f"] is not None]
    sw = size_weighted([dict(r) for r in rows]); eq = equal_dollar(rows); lb = binom_lb(rows)
    posted = len(rows); filled = sum(r["f"] > 0 for r in rows)
    unf = [r["won"] for r in rows if r["f"] == 0]; fl = [r["won"] for r in rows if r["f"] > 0]
    src = defaultdict(int)
    for r in rows_all:
        src[r["src"]] += 1
    return {"posted": posted, "unresolved_dropped": len(rows_all) - posted, "filled_orders": filled,
            "fill_rate": round(filled / posted, 3) if posted else None, "fill_source": dict(src),
            "size_weighted": sw, "equal_dollar": eq, "binomial": lb,
            "filled_NO_win": round(st.mean(fl), 3) if fl else None, "unfilled_NO_win": round(st.mean(unf), 3) if unf else None}


def split_events(orders: list[dict]) -> tuple[set, set]:
    ev = sorted({(o["tclose"], o["e"]) for o in orders})
    cut = int(round(len(ev) * SPLIT))
    return {e for _, e in ev[:cut]}, {e for _, e in ev[cut:]}


HORIZONS = (6, 12, 24, 48, 72, 120, None)
BANDS = {"all": None, "q<0.25": (0.0, 0.25), "q>=0.25": (0.25, 1.01)}


def discovery() -> dict:
    res = {"grid": {}}
    base, skip = archive_orders(0)
    disc, val = split_events(base)
    res["sample"] = {"orders_posted": len(base), "skipped": skip, "events_disc": len(disc), "events_val": len(val),
                     "truncated_prints": sum(o["truncated"] for o in base),
                     "median_spread_at_post": st.median(o["ask"] - o["bid"] for o in base),
                     "median_q": st.median(o["q"] for o in base),
                     "share_placeholder_book": round(st.mean((o["ask"] - o["bid"]) >= 0.30 for o in base), 3)}
    for dh in (0, 2):
        O = base if dh == 0 else archive_orders(2)[0]
        Od = [o for o in O if o["e"] in disc]
        for H in HORIZONS:
            for bn, b in BANDS.items():
                for meas in ("through", "any"):
                    name = f"delay{dh}|H{H if H else 'A'}|{bn}|{meas}"
                    VARIANTS.append(name)
                    d = describe(arm_rows(Od, H, meas, b))
                    res["grid"][name] = d
    res["variants_examined"] = len(VARIANTS)
    print(json.dumps(res["sample"]))
    print("DISCOVERY (size-weighted through-only unless noted)")
    for k, v in res["grid"].items():
        if k.endswith("through") and "|all|" in k:
            s = v["size_weighted"]; e = v["equal_dollar"]
            print(f"  {k:28s} posted {v['posted']:3d} filled {v['filled_orders']:3d} sw {s.get('ret_per_dollar', float('nan')):+.3f} t {s.get('t', float('nan')):5.2f} "
                  f"wo3 {s.get('ret_wo3', float('nan')):+.3f} eq {e.get('ret', float('nan')):+.3f} win {s.get('win', float('nan'))} px {s.get('avg_px')} "
                  f"medf {s.get('median_filled_contracts')} | NOwin filled {v['filled_NO_win']} unfilled {v['unfilled_NO_win']}")
    (OUT / "discovery.json").write_text(json.dumps(res, indent=1, default=str))
    return res


FROZEN = {
    "B48": {"delay_h": 0, "horizon_h": 48, "band": None,
            "rule": "arm B (pre-specified by the round-3 lead): C1E order cancelled at min(post + 48 h, C1E cancel)"},
    "A": {"delay_h": 0, "horizon_h": None, "band": None, "rule": "arm A: C1E as frozen (cancel before the call)"},
}


def validate() -> dict:
    base, _ = archive_orders(0)
    disc, val = split_events(base)
    out = {}
    for name, f in FROZEN.items():
        O = base if f["delay_h"] == 0 else archive_orders(f["delay_h"])[0]
        for part, sel in (("validation", val), ("discovery", disc), ("fresh_all", disc | val)):
            rows = arm_rows([o for o in O if o["e"] in sel], f["horizon_h"], "through", f["band"])
            d = describe(rows)
            d_any = describe(arm_rows([o for o in O if o["e"] in sel], f["horizon_h"], "any", f["band"]))
            d["any_print_size_weighted"] = d_any["size_weighted"]
            days = (max(o["tclose"] for o in O if o["e"] in sel) - min(o["tclose"] for o in O if o["e"] in sel)) / 86400
            d["fills_per_day_in_sample"] = round(d["filled_orders"] / days, 3) if days else None
            out[f"{name}|{part}"] = d
            s = d["size_weighted"]
            print(f"{name:4s} {part:10s} posted {d['posted']:3d} filled {d['filled_orders']:3d} sw {s.get('ret_per_dollar', float('nan')):+.3f} t {s.get('t', float('nan')):5.2f} "
                  f"wo3 {s.get('ret_wo3', float('nan')):+.3f} h {s.get('half1')}/{s.get('half2')} eq {d['equal_dollar'].get('ret', float('nan')):+.3f} "
                  f"win {s.get('win')} px {s.get('avg_px')} medf {s.get('median_filled_contracts')} LB {d['binomial'].get('ret_at_lo95')} "
                  f"any-sw {d['any_print_size_weighted'].get('ret_per_dollar')}")
    # paired B - A on the same orders (fresh sample): event-level difference of size-weighted pnl per $ staked
    rowsA = {r["t"]: r for r in arm_rows(base, None)}; rowsB = {r["t"]: r for r in arm_rows(base, H_B)}
    late = [dict(rowsA[t], f=rowsA[t]["f"] - rowsB[t]["f"]) for t in rowsA
            if rowsA[t]["f"] is not None and rowsB[t]["f"] is not None and rowsA[t]["f"] - rowsB[t]["f"] > 0]
    out["late_increment_A_minus_B_fresh_all"] = describe(late)
    (OUT / "validation.json").write_text(json.dumps(out, indent=1, default=str))
    return out


# ------------------------------------------------------------------------------------------------ round-3 live tier (in sample)
def insample() -> dict:
    """Arms A/B/C on round 3's cached live-tier data (where the 48 h split was found): candle-based fills."""
    from lab.kalshi.strategies import r3_earnings_call_mentions_repro as RP
    markets, hourly, daily, _ = RP.load_raw()
    ev = RP.universe(markets, hourly)
    rows = []
    for e, ms in ev.items():
        ce = RP.locate_call_cascade(ms, hourly)
        if ce is None:
            continue
        mc = min(RP.iso(m["close_time"]) for m in ms); last = max(RP.iso(m["close_time"]) for m in ms)
        CA = RP.cancel_time(ce, mc, "rule")
        for m in ms:
            tk = m["ticker"]; op = RP.iso(m["open_time"])
            t0 = -(-(op + 3600) // 3600) * 3600
            if t0 >= CA:
                continue
            cs = hourly.get(tk, [])
            q = RP.quote_at(cs, t0)
            if q is None or q[0] < op or t0 - q[0] > 48 * 3600:
                continue
            ask, bid = q[1][0], q[1][1]
            if ask is None or bid is None or not (0 < bid < ask < 1) or ask - bid < 0.02 - 1e-9:
                continue
            s = round(ask - 0.01, 2); N = int(5 // (1 - s))
            rows.append({"e": e, "t": tk, "open": op, "post": t0, "s": s, "q": round(1 - s, 4), "N": N, "cancel_A": CA, "tclose": mc,
                         "settle": last + 3600, "won": m["result"] == "no", "hr": cs, "dy": daily.get(tk, [])})
    out = {}
    for arm, H in (("A", None), ("B48", 48)):
        for meas in ("through", "any"):
            R = []
            for o in rows:
                C = o["cancel_A"] if H is None else min(o["post"] + H * 3600, o["cancel_A"])
                first = None; vol = 0.0; cover = set()
                hit = (lambda x, s_=o["s"]: x is not None and x > s_ + 1e-9) if meas == "through" else (lambda x, s_=o["s"]: x is not None and x >= s_ - 1e-9)
                for end, c in o["hr"]:                       # hourly candles wholly inside (post, cancel]
                    if end - 3600 >= o["post"] and end <= C:
                        cover.add(end)
                        if hit(c[2]):
                            first = end if first is None else min(first, end); vol += c[4]
                for end, c in o["dy"]:                       # daily candles wholly inside, volume only where no hourly cover
                    if end - 86400 >= o["post"] and end <= C and hit(c[2]):
                        first = end if first is None else min(first, end)
                        if not any(end - 86400 < h <= end for h in cover):
                            vol += c[4]
                f = min(o["N"], vol) if first is not None else 0.0
                R.append({"e": o["e"], "t": o["t"], "f": f, "q": o["q"], "won": o["won"], "tclose": o["tclose"], "t_fill": first,
                          "post": o["post"], "open": o["open"], "cancel": C, "settle": o["settle"], "N_through": f})
            for r in R:
                r["hit"] = r["t_fill"] is not None
            eq = equal_dollar([dict(r, f=1 if r["hit"] else 0) for r in R])
            sw = size_weighted([dict(r) for r in R if r["f"] >= 1])
            d = {"posted": len(R), "filled": sum(r["hit"] for r in R), "equal_dollar": eq, "size_weighted_volume_proxy": sw,
                 "filled_NO_win": round(st.mean(r["won"] for r in R if r["hit"]), 3), "unfilled_NO_win": round(st.mean(r["won"] for r in R if not r["hit"]), 3)}
            if meas == "through":
                for cap in (10,):
                    taken = replay_cap(R, cap)
                    d[f"capped{cap}"] = {"orders": len(taken), "equal_dollar": equal_dollar([dict(r, f=1 if r["hit"] else 0) for r in taken]),
                                         "size_weighted_volume_proxy": size_weighted([dict(r) for r in taken if r["f"] >= 1])}
                if arm == "B48":
                    taken = replay_cap(R, 10)
                    d["owner_profile_on_capped10"] = profile_sim([dict(r, N_through=int(r["f"])) for r in taken], P_HAT)
            out[f"{arm}|{meas}"] = d
            print(f"INSAMPLE {arm:4s} {meas:7s} posted {d['posted']} filled {d['filled']} eq {eq.get('ret', float('nan')):+.3f} t {eq.get('t', float('nan')):.2f} "
                  f"wo3 {eq.get('ret_wo3', float('nan')):+.3f} | sw-proxy {sw.get('ret_per_dollar', float('nan')):+.3f} t {sw.get('t', float('nan')):.2f}")
            if "capped10" in d:
                c = d["capped10"]
                print(f"   capped10: orders {c['orders']} eq {c['equal_dollar'].get('ret', float('nan')):+.3f} sw {c['size_weighted_volume_proxy'].get('ret_per_dollar', float('nan')):+.3f}")
            if "owner_profile_on_capped10" in d:
                print("   owner profile:", json.dumps(d["owner_profile_on_capped10"]))
    (OUT / "insample.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "all"
    if a in ("insample", "all"):
        insample()
    if a in ("discovery", "all"):
        discovery()
    if a in ("validate", "all"):
        validate()
