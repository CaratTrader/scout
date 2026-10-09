"""r5_seeded_book_placeholder_split: is the seeded-book maker profit confined to orders posted into Kalshi's mechanical
placeholder book (yes_bid <= 0.04 and yes_ask >= 0.79 at the post quote)?

Pre-registration (frozen before any split was computed): data/kalshi_lab/strategies/r5_seeded_book_placeholder_split/
preregistration.json. No Kalshi calls here: the four archive samples are rebuilt with each source's OWN code, read-only:
  r2   r2_mentions_baserate C1 (builder code; lead_round2 rule-consistent entries only), hourly candles, window B
  r3   r3_earnings_call_mentions C1E via the adversarial repro's simulate() logic (base settings)
  r4e  r4_earnings_seeded_book_48h_forward arm A, 79 fresh archive orders (exact prints or candle bounds)
  r4s  r4_single_appearance_seeded_books V1 (cell P, through-only) from orders.jsonl
Every posted order is tagged P (placeholder) or NP from its post quote only (ex ante). Fills are through-only (print
strictly above the sell price s, amendment c), f = min(N, through volume); exact prints where the source fetched them,
else the source's candle-volume proxy (an upper bound on f). The mirror buys YES at bid + 1c (print strictly below).
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_seeded_book_placeholder_split [run]"""
from __future__ import annotations
import datetime as dt, hashlib, json, math, os, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_stats import size_weighted, equal_dollar, binom_lb

OUT = Path("data/kalshi_lab/strategies/r5_seeded_book_placeholder_split")
S = Path("data/kalshi_lab/strategies")
EPS = 1e-9
STAKE = 5.0
CELLS: list[str] = []          # every cell examined (counted in K)


def is_P(bid: float, ask: float) -> bool:
    return bid <= 0.04 + EPS and ask >= 0.79 - EPS


def is_S2(bid: float, ask: float) -> bool:
    return bid <= 0.05 + EPS and ask >= 0.75 - EPS


def row(fam, e, t, part, tclose, post, bid, ask, s, f, f_any, no_won, mb=None, mf=None, series=None) -> dict:
    q = round(1 - s, 4)
    return {"fam": fam, "e": e, "t": t, "part": part, "tclose": tclose, "post": post, "t_fill": post, "bid": bid, "ask": ask,
            "P": is_P(bid, ask), "S2": is_S2(bid, ask), "s": s, "q": q, "N": int(STAKE // q), "f": f, "f_any": f_any,
            "won": no_won, "mb": mb, "mf": mf, "series": series or t.split("-")[0]}


# ------------------------------------------------------------------------------------------------ r2 mentions C1
def r2_rows() -> list[dict]:
    from lab.kalshi.strategies import r2_mentions_baserate as MB
    mk, events, C, pool = MB.load()
    ev_m = defaultdict(list)
    for m in mk:
        ev_m[m["event_ticker"]].append(m)
    lo = {e: (max(min(m["open_ts"] for m in x), max(m["close_ts"] for m in x) - 96 * 3600) // 3600) * 3600 - 3600
          for e, x in ev_m.items()}                      # the candle fetch-window start (lead_round2)
    BR = MB.BaseRates(pool); F = MB.load_full()
    exact = r2_exact_prints()
    out = []
    for part in ("disc", "val"):
        for seq in MB.candidates(mk, events, C, BR, part, "first"):
            if not seq:
                continue
            s0 = seq[0]; m = s0["m"]; e = m["event_ticker"]
            if m["open_ts"] < lo[e]:
                continue                                 # truncated entry (time fixed by the future last close): dropped
            ask, bid, H = s0["ask"], s0["bid"], s0["H"]
            sp = round(ask - 0.01, 2)
            if not sp > bid:                             # C1 posting condition of run_maker (side NO, thr -9)
                continue
            fc = events[e]["first_close"]; end = fc + 3600   # window B (conservative)
            N = int(STAKE // (1 - sp)); b = round(bid + 0.01, 2); Nb = int(STAKE // b) if b > 0 else 0
            ex = exact.get(m["ticker"])
            ex_end = ex["end"] if ex and ex["H"] == H else None
            thr = anyv = mv = 0.0
            if ex_end is not None:                       # exact prints over (H + 120 s, ex_end]
                for ts_, px, cnt in ex["prints"]:
                    if H + 120 < ts_ <= ex_end:
                        if px > sp + EPS:
                            thr += cnt
                        if px >= sp - EPS:
                            anyv += cnt
                        if b < ask - EPS and px < b - EPS:
                            mv += cnt
            hit, hit_any, mhit = (thr > 0, anyv > 0, mv > 0)
            for r in F.get(m["ticker"], []):
                if r[0] <= H or r[0] > end or r[0] > m["close_ts"] + 3600:
                    continue
                if ex_end is not None and r[0] <= ex_end:
                    continue                             # covered by the exact prints
                low, high, vol = r[5], r[6], r[4] or 0.0
                if high is not None and high > sp + EPS:
                    thr += vol; hit = True
                if high is not None and high >= sp - EPS:
                    anyv += vol; hit_any = True
                if b < ask - EPS and low is not None and low < b - EPS:
                    mv += vol; mhit = True
            f = float(min(N, thr)) if hit else 0.0
            fa = float(min(N, anyv)) if hit_any else 0.0
            mf = (float(min(Nb, mv)) if mhit else 0.0) if b < ask - EPS else None
            x = row("r2", e, m["ticker"], part, events[e]["last_close"], H, bid, ask, sp, f, fa, not m["y"],
                    b if b < ask - EPS else None, mf, m["series"])
            x["win_end"] = min(end, m["close_ts"] + 3600); x["exact_part"] = ex_end
            out.append(x)
    return out


def r2_exact_prints() -> dict:
    """Exact prints the r2 verifier fetched (repro/trades: window [H, first-fill candle end], complete pages only)."""
    out = {}
    d = S / "r2_mentions_baserate/repro/trades"
    for fn in sorted(os.listdir(d)) if d.exists() else []:
        x = json.load(open(d / fn))
        if not x.get("complete"):
            continue
        o = x["order"]; w = x["window"]
        pr = [(int(dt.datetime.fromisoformat(p[0].replace("Z", "+00:00")).timestamp()), float(p[1]), float(p[2])) for p in x["trades"]]
        out[o["t"]] = {"H": w[0], "end": w[1], "prints": pr}
    return out


# ------------------------------------------------------------------------------------------------ r3 earnings C1E
def r3_lows() -> tuple[dict, dict]:
    """Trade-price LOW per (ticker, candle end) from the r3 raw cache (the repro's loader keeps only the high)."""
    src = S / "r3_earnings_call_mentions"
    hourly, daily = defaultdict(dict), defaultdict(dict)
    for fn in sorted(os.listdir(src / "api_cache")):
        x = json.load(open(src / "api_cache" / fn))
        if isinstance(x, dict) and x.get("markets") and "candlesticks" in x["markets"][0]:
            ends = sorted({c["end_period_ts"] for m in x["markets"] for c in m["candlesticks"]})
            g = 0
            for a, b in zip(ends, ends[1:]):
                g = math.gcd(g, b - a)
            dst = daily if (g and g % 86400 == 0) else hourly
            for m in x["markets"]:
                for c in m["candlesticks"]:
                    lo_ = (c.get("price") or {}).get("low_dollars")
                    dst[m["market_ticker"]][c["end_period_ts"]] = None if lo_ in (None, "") else float(lo_)
    return hourly, daily


def r3_rows() -> list[dict]:
    from lab.kalshi.strategies import r3_earnings_call_mentions_repro as RP
    markets, hourly, daily, _ = RP.load_raw()
    ev = RP.universe(markets, hourly)
    disc, val, _ = RP.split(ev)
    hl, dl = r3_lows()
    out = []
    for e, ms in ev.items():
        call_end = RP.locate_call_cascade(ms, hourly, RP.JUMP_MIN)
        if call_end is None:
            continue
        min_close = min(RP.iso(m["close_time"]) for m in ms)
        cancel = RP.cancel_time(call_end, min_close, "rule")
        e_close = max(RP.iso(m["close_time"]) for m in ms)
        for m in ms:
            tk = m["ticker"]; op = RP.iso(m["open_time"])
            t0 = -(-(op + 3600) // 3600) * 3600
            if t0 >= cancel:
                continue
            cs = hourly.get(tk, [])
            q = RP.quote_at(cs, t0)
            if q is None or q[0] < op or t0 - q[0] > 48 * 3600:
                continue
            ask, bid = q[1][0], q[1][1]
            if ask is None or bid is None or not (0 < bid < ask < 1) or ask - bid < 0.02 - EPS:
                continue
            s = round(ask - 0.01, 2); N = math.floor(5 / (1 - s))
            b = round(bid + 0.01, 2); Nb = math.floor(5 / b); mir = b < ask - EPS
            thr = anyv = mv = 0.0; hit = hit_any = mhit = False; cover = set()
            for end, c in cs:
                if end - 3600 >= t0 and end <= cancel:
                    cover.add(end)
                    if c[2] is not None and c[2] > s + EPS:
                        hit = True; thr += c[4]
                    if c[2] is not None and c[2] >= s - EPS:
                        hit_any = True; anyv += c[4]
                    lw = hl.get(tk, {}).get(end)
                    if mir and lw is not None and lw < b - EPS:
                        mhit = True; mv += c[4]
            for end, c in daily.get(tk, []):
                if end - 86400 >= t0 and end <= cancel:
                    covered = any(end - 86400 < h <= end for h in cover)
                    lw = dl.get(tk, {}).get(end)
                    if c[2] is not None and c[2] > s + EPS:
                        hit = True; thr += 0 if covered else c[4]
                    if c[2] is not None and c[2] >= s - EPS:
                        hit_any = True; anyv += 0 if covered else c[4]
                    if mir and lw is not None and lw < b - EPS:
                        mhit = True; mv += 0 if covered else c[4]
            f = float(min(N, thr)) if hit else 0.0
            fa = float(min(N, anyv)) if hit_any else 0.0
            if hit and f == 0:
                f = 1.0                                   # a through print with no counted volume: at least 1 lot
            if hit_any and fa == 0:
                fa = 1.0
            mf = ((float(min(Nb, mv)) or 1.0) if mhit else 0.0) if mir else None
            x = row("r3", e, tk, "disc" if e in disc else "val", e_close, t0, bid, ask, s, f, fa, m["result"] == "no",
                    b if mir else None, mf)
            x["win_end"] = cancel; x["settled_ts"] = e_close
            out.append(x)
    return out


# ------------------------------------------------------------------------------------------------ r4 earnings arm A
def r4e_rows() -> list[dict]:
    from lab.kalshi.strategies import r4_earnings_seeded_book_48h_forward as R4
    from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_data import resolved, POST_GAP
    base, _ = R4.archive_orders(0)
    disc, val = R4.split_events(base)
    thr_rows = {r["t"]: r for r in R4.arm_rows(base, None, "through")}
    any_rows = {r["t"]: r for r in R4.arm_rows(base, None, "any")}
    out = []
    for o in base:
        C = o["cancel_A"]; b = round(o["bid"] + 0.01, 2); mir = b < o["ask"] - EPS; Nb = int(STAKE // b)
        mf = None
        if mir:
            if o["prints"] is not None and not o["truncated"]:
                v = sum(p[2] for p in o["prints"] if o["post"] + POST_GAP < p[0] <= C and not p[4] and p[1] < b - EPS)
                mf = float(min(Nb, v))
            else:                                        # candle bounds on prints below b ([end, ask, bid, hi, lo, vol, ...])
                L = U = 0.0
                for c in o["cs"]:
                    if c[0] > o["post"] and c[0] - 3600 < C and c[4] is not None and c[4] < b - EPS:
                        U += c[5]
                    if c[0] - 3600 >= o["post"] + 3600 and c[0] <= C and c[3] is not None and c[3] < b - EPS:
                        L += c[5]
                mf = resolved(L, U, Nb)
        out.append(row("r4e", o["e"], o["t"], "disc" if o["e"] in disc else "val", o["tclose"], o["post"], o["bid"], o["ask"],
                       o["s"], thr_rows[o["t"]]["f"], any_rows[o["t"]]["f"], o["won"], b if mir else None, mf, o["series"]))
    return out


# ------------------------------------------------------------------------------------------------ r4 single V1
def r4s_rows() -> list[dict]:
    from lab.kalshi.strategies import r4_single_appearance_seeded_books as R
    O = [json.loads(l) for l in (S / "r4_single_appearance_seeded_books/orders.jsonl").open()]
    disc, val, _ = R.split(O)
    out = []
    for o in O:
        if not o["posted"]:
            continue
        sc = R.score(o, "P", "sell", "thr"); sa = R.score(o, "P", "sell", "any"); mb = R.score(o, "P", "buy", "thr")
        f = float(sc["q"]) if sc else 0.0
        if sc and f == 0:
            f = 1.0
        fa = float(sa["q"]) if sa else 0.0
        if sa and fa == 0:
            fa = 1.0
        mir = o["b"] < o["ask"] - EPS
        mf = ((float(mb["q"]) or 1.0) if mb else 0.0) if mir else None
        x = row("r4s", o["e"], o["t"], "disc" if o["e"] in disc else "val", o["ev_last_close"], o["tq"], o["bid"], o["ask"],
                o["s"], f, fa, not o["yes"], o["b"] if mir else None, mf, o["series"])
        x["N"] = o["N"]; x["q"] = o["px"]
        out.append(x)
    return out


# ------------------------------------------------------------------------------------------------ statistics
def describe(rows: list[dict], label: str) -> dict:
    """All measures for one (sample, arm, part) cell. Counts 4 cells in K (sell sw, sell equal-$, any-print sw, mirror sw)."""
    CELLS.extend([label + "|sw_through", label + "|eq_through", label + "|sw_any", label + "|mirror_sw"])
    res = [r for r in rows if r["f"] is not None]
    filled = [dict(r) for r in res if r["f"] > 0]
    d = {"posted": len(rows), "resolved": len(res), "filled": len(filled), "events_posted": len({r["e"] for r in rows}),
         "fill_rate": round(len(filled) / len(res), 3) if res else None}
    d["sw_through"] = size_weighted([dict(r) for r in filled])
    d["eq_through"] = equal_dollar([dict(r) for r in filled])
    d["binomial_lb"] = binom_lb([dict(r) for r in filled])
    d["sw_any"] = size_weighted([dict(r, f=r["f_any"]) for r in res if r["f_any"]])
    fw = [r["won"] for r in res if r["f"] > 0]; uw = [r["won"] for r in res if r["f"] == 0]
    d["filled_NO_win"] = round(st.mean(fw), 3) if fw else None
    d["unfilled_NO_win"] = round(st.mean(uw), 3) if uw else None
    d["all_posted_NO_win"] = round(st.mean(r["won"] for r in rows), 3) if rows else None
    d["avg_post_q"] = round(st.mean(r["q"] for r in rows), 3) if rows else None
    mrows = [dict(e=r["e"], f=r["mf"], q=r["mb"], won=not r["won"], tclose=r["tclose"]) for r in rows if r["mb"] is not None and r["mf"]]
    d["mirror_sw"] = size_weighted(mrows)
    d["mirror_eq"] = equal_dollar([dict(m) for m in mrows])
    if filled:
        span = (max(r["tclose"] for r in rows) - min(r["tclose"] for r in rows)) / 86400
        d["fills_per_day"] = round(len(filled) / span, 2) if span > 0 else None
        d["median_filled_contracts"] = st.median(r["f"] for r in filled)
    return d


def line(k: str, d: dict) -> str:
    s = d["sw_through"]; m = d["mirror_sw"]; e = d["eq_through"]
    return (f"{k:22s} posted {d['posted']:4d} ev {d['events_posted']:3d} filled {d['filled']:4d} | sw {s.get('ret_per_dollar', float('nan')):+.3f} "
            f"t {s.get('t', float('nan')):5.2f} ev {s.get('events', 0):3d} wo3 {s.get('ret_wo3', float('nan')):+.3f} "
            f"h {s.get('half1')}/{s.get('half2')} | eq {e.get('ret', float('nan')):+.3f} t {e.get('t', float('nan')):.2f} | "
            f"NOwin f/u {d['filled_NO_win']}/{d['unfilled_NO_win']} | mirror sw {m.get('ret_per_dollar', float('nan')):+.3f} n {m.get('n', 0)} "
            f"| px {s.get('avg_px')} medf {d.get('median_filled_contracts')}")


def run() -> dict:
    pre = json.loads((OUT / "preregistration.json").read_text())
    rows = {"r2": r2_rows(), "r3": r3_rows(), "r4e": r4e_rows(), "r4s": r4s_rows()}
    rows["pooled"] = [r for k in ("r2", "r3", "r4e", "r4s") for r in rows[k]]
    res = {"preregistration_sha1": hashlib.sha1((OUT / "preregistration.json").read_bytes()).hexdigest(),
           "preregistration_written": pre["written_utc"], "samples": {}, "book_census": {}}
    for k, R in rows.items():
        res["book_census"][k] = {"posted": len(R), "P_share": round(st.mean(r["P"] for r in R), 3) if R else None,
                                 "S2_share": round(st.mean(r["S2"] for r in R), 3) if R else None,
                                 "median_bid": st.median(r["bid"] for r in R) if R else None,
                                 "median_ask": st.median(r["ask"] for r in R) if R else None,
                                 "events": len({r["e"] for r in R})}
        res["samples"][k] = {}
        for arm in ("P", "NP"):
            for part in ("disc", "val", "all"):
                sel = [r for r in R if (r["P"] if arm == "P" else not r["P"]) and (part == "all" or r["part"] == part)]
                res["samples"][k][f"{arm}|{part}"] = describe(sel, f"{k}|{arm}|{part}")
    res["secondary_S2_pooled"] = {}
    for arm in ("S2", "notS2"):
        for part in ("disc", "val", "all"):
            sel = [r for r in rows["pooled"] if (r["S2"] if arm == "S2" else not r["S2"]) and (part == "all" or r["part"] == part)]
            CELLS.append(f"pooled|{arm}|{part}|sw_through")
            d = describe(sel, "tmp"); del CELLS[-4:]
            res["secondary_S2_pooled"][f"{arm}|{part}"] = {k2: d[k2] for k2 in ("posted", "filled", "sw_through", "eq_through", "filled_NO_win", "unfilled_NO_win")}
    pa = res["samples"]["pooled"]
    P_all, NP_all = pa["P|all"]["sw_through"].get("ret_per_dollar"), pa["NP|all"]["sw_through"].get("ret_per_dollar")
    P_val, NP_val = pa["P|val"]["sw_through"].get("ret_per_dollar"), pa["NP|val"]["sw_through"].get("ret_per_dollar")
    c1 = P_all is not None and NP_all is not None and P_all > 0.20 and NP_all <= 0
    c2 = P_val is not None and NP_val is not None and P_val > 0 and P_val > NP_val
    res["decision"] = {"pooled_all_P": P_all, "pooled_all_NP": NP_all, "pooled_val_P": P_val, "pooled_val_NP": NP_val,
                       "lead_rule_P_gt_20pct_and_NP_le_0": c1, "validation_consistency": c2,
                       "split": "KEEP" if (c1 and c2) else "DROP"}
    res["cells_examined"] = len(CELLS)
    (OUT / "archive_split.json").write_text(json.dumps(res, indent=1, default=str))
    (OUT / "archive_rows.jsonl").write_text("".join(json.dumps({k: v for k, v in r.items()}) + "\n" for r in rows["pooled"]))
    for k in ("r2", "r3", "r4e", "r4s", "pooled"):
        print(f"== {k} census {res['book_census'][k]}")
        for kk, d in res["samples"][k].items():
            print("  " + line(kk, d))
    print("S2:", {k: (v["sw_through"].get("ret_per_dollar"), v["sw_through"].get("n")) for k, v in res["secondary_S2_pooled"].items()})
    print("DECISION", res["decision"], "cells", len(CELLS))
    return res


if __name__ == "__main__":
    run()
