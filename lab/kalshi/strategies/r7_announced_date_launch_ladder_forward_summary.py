"""r7_announced_date_launch_ladder_forward_summary: score the forward paper log (no Kalshi calls).

Reads <OUT>/forward.jsonl (written by r7_announced_date_launch_ladder_forward_logger) and writes <OUT>/summary.json.
Only rows with dry != true count. A trade = a filled 'entry' row joined with its 'settle' row (taker NO at the logged
fill price, fee ceil-to-cent on a 10-lot). The unit of independence is the product = Kalshi event_ticker.

Forward-only gate (preregistration.json; every row must hold):
  G1 trades >= 40 and independent products >= 40
  G2 mean return per $ >= +10%
  G3 Beta bound: one-sided 95% Clopper-Pearson lower bound of the product win rate (a product wins if all its trades
     won), turned into a per-$ return at the average price and fee, > 0          (amendment (d))
  G4 mean without the 3 best trades > 0
  G5 both halves (trades split at the median entry time) > 0
  G6 median capacity at the signal price >= $5 (contracts at the best YES bid x NO price)
  G7 zero integrity errors (wrong side, out-of-band or out-of-window entries, duplicate entries, unparseable rows)
  G8 clustered t (by product, sample SD) >= z(0.05 / K) with K the lab count on the review day (docs row 3, (k))
Kill (checked in time order of settlement): a 2nd losing product before 30 products have settled; any integrity error.
Administrative stop: 18 months after the first forward pass with < 40 settled products -> stop, verdict inconclusive.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_summary [--K 20175] [--quiet]"""
from __future__ import annotations

import json
import math
import os
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r7_announced_date_launch_ladder_forward_common as C  # noqa: E402

K_DEFAULT = 20175          # lab K after round 6 (bar t >= 4.57); the review must use the K recorded on its day
MIN_TRADES, MIN_PRODUCTS, MIN_MEAN = 40, 40, 0.10
KILL_LOSSES, KILL_BEFORE = 2, 30
ADMIN_STOP_S = int(18 * 30.44 * C.DAY)


def out_dir() -> Path:
    from lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_api import OUT
    return Path(os.environ.get("R7_OUT", str(OUT)))


def zbar(K: int) -> float:
    p = 0.05 / max(K, 1)
    lo, hi = 0.0, 12.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2)) > p:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def load(out: Path) -> tuple[list[dict], int]:
    rows, bad = [], 0
    f = out / "forward.jsonl"
    if f.exists():
        for line in f.open():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return rows, bad


def trades_of(rows: list[dict]) -> tuple[list[dict], list[str], list[dict]]:
    errors: list[str] = []
    entries: dict[str, dict] = {}
    settles: dict[str, dict] = {}
    for r in rows:
        if r.get("dry"):
            continue
        if r["type"] == "entry" and r.get("filled"):
            t = r["ticker"]
            if t in entries:
                errors.append(f"duplicate entry {t}")
                continue
            sig, px = r.get("signal_no"), r.get("px")
            if sig is None or not (C.BAND_LO - 1e-9 <= sig <= C.BAND_HI + 1e-9):
                errors.append(f"entry {t} signal {sig} out of band")
            if px is None or not (0 < px < 1):
                errors.append(f"entry {t} bad fill price {px}")
            h = (r.get("D", 0) - r["ts"])
            if not (C.H_MIN - 60 <= h <= C.H_MAX + 60):
                errors.append(f"entry {t} outside the D window ({h / 3600:.1f} h)")
            if C.et_now(r["ts"]).hour != C.DECISION_HOUR_ET:
                errors.append(f"entry {t} outside the decision hour")
            entries[t] = r
        elif r["type"] == "settle":
            if r["ticker"] in settles:
                continue
            settles[r["ticker"]] = r
    trades, open_pos = [], []
    for t, e in entries.items():
        s = settles.get(t)
        if not s:
            open_pos.append({"ticker": t, "event": e["event"], "px": e["px"], "D_iso": e.get("D_iso")}); continue
        won = s["result"] == "no"
        if won != s.get("won", won):
            errors.append(f"settle {t}: won flag disagrees with result")
        trades.append({"t": t, "e": e["event"], "s": e["series"], "kind": e.get("kind"), "px": e["px"], "won": won,
                       "ret": C.ret_per_dollar(e["px"], won), "entry_ts": e["ts"], "settle_ts": s["ts"],
                       "cap_usd": e.get("cap_usd_at_signal"), "cap_contracts": e.get("cap_contracts_at_signal"),
                       "fee": C.fee_per_contract(e["px"]), "h_to_D": e.get("h_to_D")})
    for t in settles:
        if t not in entries:
            errors.append(f"settle without entry {t}")
    return trades, errors, open_pos


def stats(tr: list[dict], K: int) -> dict:
    if not tr:
        return {"n": 0}
    ev = defaultdict(list)
    for r in tr:
        ev[r["e"]].append(r)
    em = [st.mean(x["ret"] for x in v) for v in ev.values()]
    ne = len(em)
    sd = st.stdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne)) if sd > 0 else (float("inf") if ne > 1 and st.mean(em) > 0 else float("nan"))
    rs = sorted((r["ret"] for r in tr), reverse=True)
    srt = sorted(tr, key=lambda r: r["entry_ts"])
    mid = len(srt) // 2
    h1, h2 = srt[:mid], srt[mid:]
    k_ev = sum(1 for v in ev.values() if all(x["won"] for x in v))
    px = st.mean(r["px"] for r in tr); fe = st.mean(r["fee"] for r in tr)
    caps = sorted(r["cap_usd"] for r in tr if r.get("cap_usd") is not None)
    span_w = max((max(r["entry_ts"] for r in tr) - min(r["entry_ts"] for r in tr)) / (7 * C.DAY), 1.0)
    return {"n": len(tr), "products": ne, "series": len({r["s"] for r in tr}), "kinds": sorted({r["kind"] or "?" for r in tr}),
            "trade_losses": sum(not r["won"] for r in tr), "product_losses": ne - k_ev,
            "win": round(sum(r["won"] for r in tr) / len(tr), 4), "avg_px": round(px, 4),
            "ret_per_dollar": round(st.mean(r["ret"] for r in tr), 4), "t_by_product": round(t, 3) if math.isfinite(t) else t,
            "bar_t": zbar(K), "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in h1), 4) if h1 else None,
            "half2": round(st.mean(r["ret"] for r in h2), 4) if h2 else None,
            "beta_lb_ret_products": round(C.beta_bound_ret(k_ev, ne, px, fe), 4),
            "median_capacity_usd": caps[len(caps) // 2] if caps else None,
            "median_capacity_contracts": sorted(r["cap_contracts"] or 0 for r in tr)[len(tr) // 2],
            "trades_per_week": round(len(tr) / span_w, 3), "avg_fee_per_contract": round(fe, 4)}


def kill_check(tr: list[dict]) -> dict:
    """Walk products in order of their last settlement: kill if the 2nd losing product arrives before 30 have settled."""
    ev = defaultdict(list)
    for r in tr:
        ev[r["e"]].append(r)
    order = sorted(ev, key=lambda e: max(x["settle_ts"] for x in ev[e]))
    losses = 0
    for i, e in enumerate(order, 1):
        if not all(x["won"] for x in ev[e]):
            losses += 1
            if losses >= KILL_LOSSES and i <= KILL_BEFORE:
                return {"killed": True, "at_product": i, "event": e, "losses": losses}
    return {"killed": False, "losing_products": losses, "settled_products": len(order)}


def summarize(out: Path, K: int = K_DEFAULT) -> dict:
    rows, bad = load(out)
    trades, errors, open_pos = trades_of(rows)
    if bad:
        errors.append(f"{bad} unparseable rows")
    s = stats(trades, K)
    kill = kill_check(trades)
    live = [r for r in rows if not r.get("dry")]
    first = min((r["ts"] for r in live if r["type"] == "pass"), default=None)
    looks = [r for r in live if r["type"] == "look" and not r.get("missing")]
    first_looks = {}
    for r in looks:
        first_looks.setdefault(r["ticker"], r)
    admin_stop = bool(first and live and (max(r["ts"] for r in live) - first) > ADMIN_STOP_S and s.get("products", 0) < MIN_PRODUCTS)
    g = {}
    if s.get("n"):
        g = {"G1_n_trades_and_products": s["n"] >= MIN_TRADES and s["products"] >= MIN_PRODUCTS,
             "G2_mean_ge_10pct": s["ret_per_dollar"] >= MIN_MEAN,
             "G3_beta_bound_gt_0": s["beta_lb_ret_products"] > 0,
             "G4_wo3_gt_0": (s["ret_wo3"] or -1) > 0,
             "G5_both_halves_gt_0": (s["half1"] or -1) > 0 and (s["half2"] or -1) > 0,
             "G6_capacity_ge_5usd": (s["median_capacity_usd"] or 0) >= 5,
             "G7_no_integrity_errors": not errors,
             "G8_t_ge_bar": isinstance(s["t_by_product"], float) and s["t_by_product"] >= s["bar_t"]}
    verdict = ("KILLED" if kill["killed"] or errors else "PASS" if g and all(g.values()) else
               "STOPPED_UNDERPOWERED" if admin_stop else "RUNNING")
    res = {"name": "r7_announced_date_launch_ladder_forward", "K": K, "verdict": verdict, "stats": s, "gate": g, "kill": kill,
           "integrity_errors": errors, "open_positions": open_pos,
           "first_forward_pass": C.et_now(first).isoformat(timespec="seconds") if first else None,
           "passes": sum(1 for r in live if r["type"] == "pass"),
           "looks": len(looks), "rungs_looked": len(first_looks),
           "first_look_in_band_share": round(sum(1 for r in first_looks.values() if r.get("in_band")) / len(first_looks), 3) if first_looks else None,
           "entries": sum(1 for r in live if r["type"] == "entry"),
           "dry_rows_ignored": sum(1 for r in rows if r.get("dry")),
           "trades": trades}
    (out / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    a = sys.argv
    K = int(a[a.index("--K") + 1]) if "--K" in a else K_DEFAULT
    r = summarize(out_dir(), K)
    if "--quiet" not in a:
        print(json.dumps({k: v for k, v in r.items() if k != "trades"}, indent=1, default=str))
