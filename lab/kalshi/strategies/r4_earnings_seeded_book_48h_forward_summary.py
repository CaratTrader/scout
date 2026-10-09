"""r4_earnings_seeded_book_48h_forward_summary: score the forward paper log of the earnings seeded-book maker (no Kalshi calls).

Reads data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward/forward.jsonl and trades/<ticker>.json (written by
r4_earnings_seeded_book_48h_forward_logger) and writes summary.json next to them. Rules: preregistration.json.
  arm A  C1E: window (post + 120 s, cancel_A], cancel_A = the causal pre-call cancel logged in the 'settle' row
  arm B  window (post + 120 s, min(post + 48 h, cancel_A)]                                      <- PRIMARY
  arm C  arm B in a book of <= 10 concurrent orders/positions, earliest-listed first; a slot is held until cancel_B if
         the order never filled, else until the event's last market close + 1 h. Orders still waiting for settlement
         hold their slot (their fills are not known yet), so arm C is final only when every earlier event settled.
  live   the owner's profile on arm B's orders: <= 3 concurrent, stake min(quarter-Kelly on p = 0.362 shrunk 50% toward
         the price, $5), halt after 3 losses in a row / $10 daily / $15 cumulative loss.
Fill measures (filled contracts = min(N, qualifying contracts)): through (yes_price > s; gate amendment c, PRIMARY),
any (>= s), strict (through + prints at s whose taker sold YES), queue (probed orders: strict + YES-taker prints at s
beyond the size resting at our price at post). P&L per contract +s if NO, -(1 - s) if YES; no fee (quadratic series).
Universe: the 115 frozen series (PRIMARY); all KXEARNINGSMENTION* series reported beside it.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_summary [--K 17500] [--out DIR] [--quiet]"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_logger import fill_metrics, POST_GAP_S, H_B_S
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_stats import size_weighted, equal_dollar, binom_lb, replay_cap, profile_sim

OUT = Path("data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward")
MEASURES = ("through", "any", "strict", "queue")
CAP_C = 10
P_HAT = 0.362
MIN_EVENTS = 60
KILL_EVENTS = 30


def load(out: Path) -> dict:
    R = defaultdict(list)
    f = out / "forward.jsonl"
    if f.exists():
        for l in f.open():
            r = json.loads(l); R[r["type"]].append(r)
    return R


def build(out: Path, R: dict) -> tuple[list[dict], list[dict]]:
    """(complete orders with fills per arm and measure, pending orders)."""
    settle = {r["e"]: r for r in R["settle"]}
    fills = {r["ticker"]: r for r in R["fill"]}
    probes = {p["ticker"]: p for p in R["probe"] if p["phase"] == "post"}
    done, pending = [], []
    for e in R["entry"]:
        if not e.get("posted"):
            continue
        S = settle.get(e["e"]); F = fills.get(e["ticker"])
        o = {"t": e["ticker"], "e": e["e"], "series": e["series"], "frozen": e.get("frozen", False), "post": e["ts"], "open": e["open"],
             "s_c": e["s_c"], "N": e["N"], "q": round(1 - e["s_c"] / 100, 4), "ask_c": e["ask_c"], "bid_c": e["bid_c"], "lag_s": e["lag_s"]}
        if not (S and F):
            pending.append(o); continue
        res = (S["markets"].get(e["ticker"]) or [""])[0]
        if res not in ("yes", "no"):
            continue                                           # void
        CA = S["cancel_A"]; CB = min(o["post"] + H_B_S, CA)
        tf = out / "trades" / f"{e['ticker']}.json"
        prints = json.loads(tf.read_text())["prints"] if tf.exists() else []
        qa = (probes.get(e["ticker"]) or {}).get("size_at_our_price")
        closes = [v[1] for v in S["markets"].values() if v[1]]
        o.update({"won": res == "no", "first_close": S["first_close"], "settle": (max(closes) if closes else S["first_close"]) + 3600,
                  "cancel_A": CA, "cancel_B": CB, "probed": qa is not None,
                  "A": fill_metrics(prints, o["s_c"], o["N"], o["post"] + POST_GAP_S, CA, qa),
                  "B": fill_metrics(prints, o["s_c"], o["N"], o["post"] + POST_GAP_S, CB, qa),
                  "cancel_after_close": CA > S["first_close"] - 1800})
        done.append(o)
    return done, pending


def rows_for(orders: list[dict], arm: str, measure: str) -> list[dict]:
    out = []
    for o in orders:
        fm = o[arm]
        if measure == "queue" and "queue_cnt" not in fm:
            continue
        cnt = fm[f"{measure}_cnt"]
        tkey = "t_first_through" if measure in ("through", "strict", "queue") else "t_first_any"
        out.append({"e": o["e"], "t": o["t"], "f": min(o["N"], cnt), "q": o["q"], "won": o["won"], "tclose": o["first_close"],
                    "t_fill": fm[tkey], "post": o["post"], "open": o["open"], "cancel": o["cancel_B" if arm == "B" else "cancel_A"],
                    "settle": o["settle"], "N_through": min(o["N"], cnt)})
    return out


def cell(rows: list[dict]) -> dict:
    fl = [r for r in rows if r["f"] > 0]
    return {"orders": len(rows), "events_with_orders": len({r["e"] for r in rows}), "filled_orders": len(fl),
            "fill_rate": round(len(fl) / len(rows), 3) if rows else None,
            "size_weighted": size_weighted([dict(r) for r in rows]), "equal_dollar": equal_dollar(rows), "binomial": binom_lb(rows),
            "filled_NO_win": round(st.mean(r["won"] for r in fl), 3) if fl else None,
            "unfilled_NO_win": round(st.mean(r["won"] for r in rows if r["f"] == 0), 3) if len(fl) < len(rows) else None}


def capped(done: list[dict], pending: list[dict]) -> list[dict]:
    """Arm C over arm B's through fills; pending orders hold their slot indefinitely (unknown fill)."""
    R = rows_for(done, "B", "through")
    for o in pending:
        R.append({"e": o["e"], "t": o["t"], "f": None, "q": o["q"], "won": None, "tclose": None, "t_fill": o["post"], "post": o["post"],
                  "open": o["open"], "cancel": float("inf"), "settle": float("inf"), "N_through": 0})
    return replay_cap(R, CAP_C)


def gate(c: dict, K: int, ref: dict | None, events_settled: int, done: list[dict]) -> dict:
    sw = c["size_weighted"]; z = NormalDist().inv_cdf(1 - 0.05 / K)
    g = {"K": K, "z": round(z, 2),
         "row1_n_fills>=40": sw.get("n", 0) >= 40, "run_length_events>=60": events_settled >= MIN_EVENTS,
         "row2_sw_through>=0.10": (sw.get("ret_per_dollar") or -9) >= 0.10,
         "row3_t>=z": (sw.get("t") if sw.get("t") == sw.get("t") else -9) >= z if sw.get("n") else False,
         "row3d_binomial_lb>0": bool(c["binomial"].get("lb_above_0")),
         "row4_wo3>0": (sw.get("ret_wo3") if sw.get("ret_wo3") == sw.get("ret_wo3") else -9) > 0 if sw.get("n") else False,
         "row5_halves>0": bool(sw.get("half1") is not None and sw.get("half2") is not None and sw["half1"] > 0 and sw["half2"] > 0),
         "row6_median_filled_contracts>=5": (sw.get("median_filled_contracts") or 0) >= 5,
         "row7_zero_wrong_side": not any(not (o["bid_c"] < o["s_c"] < o["ask_c"]) for o in done),
         "row7_zero_cancel_after_close": not any(o["cancel_after_close"] for o in done)}
    if ref and sw.get("n"):
        se = abs(sw["ret_per_dollar"] / sw["t"]) if sw.get("t") and sw["t"] == sw["t"] and sw["t"] != 0 else float("inf")
        g["row7_mean_not_below_ref_minus_2se"] = sw["ret_per_dollar"] >= ref["ret_per_dollar"] - 2 * se
        g["row7_ref"] = ref
    g["PASS"] = all(v for k, v in g.items() if k.startswith(("row", "run")) and isinstance(v, bool))
    return g


def main(out: Path = OUT, K: int = 17500, quiet: bool = False) -> dict:
    R = load(out)
    done, pending = build(out, R)
    P = json.loads((out / "preregistration.json").read_text()) if (out / "preregistration.json").exists() else {}
    ref = (P.get("backtest_reference") or {}).get("arm_B_fresh_archive_size_weighted_through")
    res = {"passes": len(R["pass"]), "listings": len(R["listing"]), "entries": len(R["entry"]),
           "posted": sum(1 for e in R["entry"] if e.get("posted")), "missed": len(R["missed"]),
           "skip_reasons": dict(defaultdict(int, {k: sum(1 for e in R["entry"] if not e.get("posted") and e.get("reason") == k)
                                                  for k in {e.get("reason") for e in R["entry"] if not e.get("posted")}})),
           "complete_orders": len(done), "pending_orders": len(pending),
           "median_entry_spread_c": st.median(e["ask_c"] - e["bid_c"] for e in R["entry"] if e.get("posted")) if res_posted(R) else None,
           "median_entry_lag_min": round(st.median(e["lag_s"] for e in R["entry"] if e.get("posted")) / 60, 1) if res_posted(R) else None}
    for uni in ("frozen", "all"):
        D = [o for o in done if o["frozen"]] if uni == "frozen" else done
        ev = len({o["e"] for o in D})
        res[uni] = {"settled_events_with_orders": ev}
        for arm in ("A", "B"):
            for m in MEASURES:
                res[uni][f"{arm}|{m}"] = cell(rows_for(D, arm, m))
        Pd = [o for o in pending if o["frozen"]] if uni == "frozen" else pending
        C = capped(D, Pd)
        Cdone = [r for r in C if r["f"] is not None]
        res[uni]["C|through"] = cell(Cdone); res[uni]["C|through"]["slots_held_by_pending"] = len(C) - len(Cdone)
        res[uni]["live_profile_on_B"] = profile_sim([r for r in rows_for(D, "B", "through")], P_HAT)
        b = res[uni]["B|through"]["size_weighted"]; a = res[uni]["A|through"]["size_weighted"]
        res[uni]["B_minus_A_sw_through"] = round(b["ret_per_dollar"] - a["ret_per_dollar"], 4) if b.get("n") and a.get("n") else None
        res[uni]["kill_check"] = {"applies": ev >= KILL_EVENTS, "B_sw_through": b.get("ret_per_dollar"),
                                  "KILL": bool(ev >= KILL_EVENTS and b.get("n") and b["ret_per_dollar"] < 0)}
        res[uni]["gate_B"] = gate(res[uni]["B|through"], K, ref, ev, D)
    (out / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    if not quiet:
        print(f"passes {res['passes']} listings {res['listings']} posted {res['posted']} complete {len(done)} pending {len(pending)} missed {res['missed']}")
        for uni in ("frozen", "all"):
            x = res[uni]
            print(f"[{uni}] settled events with orders {x['settled_events_with_orders']}  kill {x['kill_check']}  gate PASS {x['gate_B']['PASS']}")
            for k in ("A|through", "B|through", "C|through", "B|any", "B|queue"):
                c = x[k]; s = c["size_weighted"]
                print(f"   {k:10s} orders {c['orders']:4d} filled {c['filled_orders']:4d} sw {s.get('ret_per_dollar')} t {s.get('t')} wo3 {s.get('ret_wo3')} "
                      f"halves {s.get('half1')}/{s.get('half2')} LB {c['binomial'].get('ret_at_lo95')}")
            print("   live profile:", json.dumps(x["live_profile_on_B"]))
    return res


def res_posted(R: dict) -> bool:
    return any(e.get("posted") for e in R["entry"])


def selftest() -> None:
    """Runs the logger's offline self-test, keeps its files, and scores them: one event, order AAA filled 4 contracts
    through inside 48 h and 14 by the arm-A cancel, resolved YES (a loss); order CCC never filled."""
    import shutil, tempfile
    from lab.kalshi.strategies import r4_earnings_seeded_book_48h_forward_logger as LG
    keep = Path(tempfile.mkdtemp(prefix="r4e_sum_"))
    try:
        LG.selftest(keep)
        r = main(keep, 17500, quiet=True)
        b = r["frozen"]["B|through"]; a = r["frozen"]["A|through"]
        assert b["orders"] == 2 and b["filled_orders"] == 1 and a["filled_orders"] == 1, (a, b)
        assert b["size_weighted"]["ret_per_dollar"] == -1.0 and b["size_weighted"]["staked"] == round(4 * 0.16, 2), b
        assert a["size_weighted"]["staked"] == round(14 * 0.16, 2), a
        assert r["frozen"]["C|through"]["orders"] == 2 and r["frozen"]["gate_B"]["row7_zero_wrong_side"]
        print("summary selftest OK")
    finally:
        shutil.rmtree(keep)


if __name__ == "__main__":
    a = sys.argv
    if "--selftest" in a:
        selftest(); sys.exit()
    K = int(a[a.index("--K") + 1]) if "--K" in a else 17500
    out = Path(a[a.index("--out") + 1]) if "--out" in a else OUT
    main(out, K, "--quiet" in a)
