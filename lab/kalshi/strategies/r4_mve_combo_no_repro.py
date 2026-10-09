"""Adversarial reproduction of r4_mve_combo_no (Kalshi multivariate combos, NO taker at the RFQ print price).

Written from the claim's description only; none of the original analysis code is imported or copied. Inputs are the
RAW Kalshi API responses the original sampler cached (data/kalshi_lab/strategies/r4_mve_combo_no/api_cache: exchange-
wide /markets/trades windows and /markets?tickers= lookups), plus an optional recovery set of the ticker lookups that
came back empty in the original run (repro/api_cache, written by r4_mve_combo_no_repro_fetch.py). Those recovered
fills were never seen by the original analysis, so they are a free random hold-out.

Definitions (from the claim):
  fill      = one combo trade print (ticker starts KXMVE); taker side = taker_side; NO price = 1 - yes_price.
  day       = ET (UTC-4 in Sep/Oct) date of the print; split: discovery days < 2026-09-28 <= validation days.
  exclusions: yes price outside [0.005, 0.995]; no market record; not settled; scheduled expiry (expected_expiration_time)
            later than pull - 24 h (early-close bias guard).
  return    = (payout - px - fee) / px, payout = settlement value of the side, fee 0.07 p (1-p) per contract.
  C1 = NO taker, NO px in [0.50, 0.85); C2 = C1 with combo age < 120 s at the print; C3 = NO taker, NO px [0.85, 0.995].
Statistics: day-clustered t, window-clustered t, ticker-clustered t, mean w/o 3 best, halves, Clopper-Pearson lower
bound on unique combos, median print size.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_mve_combo_no_repro"""
from __future__ import annotations
import datetime as dt, json, math, os, statistics as st, sys
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "data/kalshi_lab/strategies/r4_mve_combo_no/api_cache"
OUT = ROOT / "data/kalshi_lab/strategies/r4_mve_combo_no/repro"
REC = OUT / "api_cache"
ET = dt.timezone(dt.timedelta(hours=-4))
CUT = "2026-09-28"
PULL_ORIG = 1791506181            # first call of the original pull (2026-10-09 00:36 UTC), from calls.log
SAMPLE_END = "2026-10-06T00:00:00Z"   # the 2026-10-06 00:40 probe window is not part of the 75-window sample


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fee(p: float, contracts: float | None = None) -> float:
    """Per-contract taker fee. With `contracts`, Kalshi's per-order round-up to the cent is applied."""
    f = 0.07 * p * (1 - p)
    if contracts is None:
        return f
    return math.ceil(round(f * contracts * 100, 9)) / 100 / contracts


def load(rec: bool):
    windows, markets, pull_of = [], {}, {}
    dirs = [(SRC, PULL_ORIG)] + ([(REC, None)] if rec and REC.exists() else [])
    for d, pull in dirs:
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            x = json.loads((d / f).read_text())
            if "trades" in x and d == SRC:
                tr = x["trades"]
                if len(tr) < 900 or max(t["created_time"] for t in tr) >= SAMPLE_END:
                    continue          # single-ticker probes and the post-sample probe window
                windows.append(tr)
            elif "markets" in x:
                p = pull if pull is not None else x.get("_fetched_at", 0)
                for m in x["markets"]:
                    if not m["ticker"].startswith("KXMVE"):
                        continue
                    old = markets.get(m["ticker"])
                    if old is None or m.get("updated_time", "") > old.get("updated_time", ""):
                        markets[m["ticker"]] = m; pull_of[m["ticker"]] = p
    return windows, markets, pull_of


def build(rec: bool = False, round_fee: bool = False, stake: float = 5.0):
    windows, markets, pull_of = load(rec)
    windows.sort(key=lambda tr: min(t["created_time"] for t in tr))
    seen, rows, skip = set(), [], Counter()
    for wi, tr in enumerate(windows):
        for t in tr:
            if not t["ticker"].startswith("KXMVE") or t["trade_id"] in seen:
                continue
            seen.add(t["trade_id"])
            yes = float(t["yes_price_dollars"]); side = t["taker_side"]
            if not (0.005 <= yes <= 0.995):
                skip["px_extreme"] += 1; continue
            m = markets.get(t["ticker"])
            if m is None:
                skip["no_market"] += 1; continue
            exp = ts(m["expected_expiration_time"])
            if exp > pull_of[t["ticker"]] - 86400:
                skip["exp_after_pull"] += 1; continue
            sv = m.get("settlement_value_dollars")
            if m["status"] not in ("finalized", "settled") or sv in (None, ""):
                skip["unsettled"] += 1; continue
            sv = float(sv)
            px = yes if side == "yes" else 1 - yes
            pay = sv if side == "yes" else 1 - sv
            n_c = max(1, math.floor(stake / px)) if round_fee else None
            f = fee(px, n_c)
            pt = ts(t["created_time"])
            legs = m.get("mve_selected_legs") or []
            rows.append({"tk": t["ticker"], "tid": t["trade_id"], "w": wi, "t": pt,
                         "day": dt.datetime.fromtimestamp(pt, ET).date().isoformat(), "side": side, "px": px,
                         "pay": pay, "won": pay > 0.5, "scalar": sv not in (0.0, 1.0), "ret": (pay - px - f) / px,
                         "cnt": float(t["count_fp"]), "age": pt - ts(m["created_time"]), "exp_h": (exp - pt) / 3600,
                         "nlegs": len(legs), "legev": sorted({l["event_ticker"] for l in legs}),
                         "rec": t["ticker"] in markets and pull_of[t["ticker"]] != PULL_ORIG})
    return rows, skip, len(windows)


def ct(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[r[key] if isinstance(key, str) else key(r)].append(r["ret"])
    em = [st.mean(v) for v in g.values()]
    if len(em) < 3 or st.pstdev(em) == 0:
        return float("nan"), len(em)
    return st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))), len(em)


def cp_lower(k: int, n: int, a: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound of a binomial rate."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        tail = sum(math.comb(n, j) * mid ** j * (1 - mid) ** (n - j) for j in range(k, n + 1))
        lo, hi = (mid, hi) if tail < a else (lo, mid)
    return lo


def stats(rows):
    if not rows:
        return {"n": 0}
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["t"]); mid = srt[len(srt) // 2]["t"]
    h1 = [r["ret"] for r in srt if r["t"] < mid]; h2 = [r["ret"] for r in srt if r["t"] >= mid]
    days = sorted({r["day"] for r in rows}); dmid = days[len(days) // 2] if days else ""
    d1 = [r["ret"] for r in rows if r["day"] < dmid]; d2 = [r["ret"] for r in rows if r["day"] >= dmid]
    # unique combos: one outcome per ticker (first print), binomial lower bound turned into a per-$ return
    first = {}
    for r in srt:
        first.setdefault(r["tk"], r)
    u = list(first.values()); k = sum(r["won"] for r in u); plb = cp_lower(k, len(u))
    ret_lb = st.mean((plb - r["px"] - fee(r["px"])) / r["px"] for r in u)
    t_day, n_day = ct(rows, "day"); t_w, n_w = ct(rows, "w"); t_tk, n_tk = ct(rows, "tk")
    t_leg, _ = ct(rows, lambda r: r["legev"][0] if r["legev"] else r["tk"])
    nan = float("nan")
    return {"n": len(rows), "events": n_day, "windows": n_w, "tickers": n_tk, "win": sum(r["won"] for r in rows) / len(rows),
            "avg_px": st.mean(r["px"] for r in rows), "ret": st.mean(rs), "t_day": t_day, "t_window": t_w, "t_ticker": t_tk,
            "t_firstleg_event": t_leg, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else nan,
            "half1": st.mean(h1) if h1 else nan, "half2": st.mean(h2) if h2 else nan,
            "dayhalf1": st.mean(d1) if d1 else nan, "dayhalf2": st.mean(d2) if d2 else nan,
            "median_cnt": st.median(r["cnt"] for r in rows), "unique_combos": len(u), "unique_wins": k,
            "binom_lb_ret": ret_lb, "scalar": sum(r["scalar"] for r in rows), "days": days}


CANDS = {
    "C1": lambda r: r["side"] == "no" and 0.50 <= r["px"] < 0.85,
    "C2": lambda r: r["side"] == "no" and 0.50 <= r["px"] < 0.85 and r["age"] < 120,
    "C3": lambda r: r["side"] == "no" and 0.85 <= r["px"] <= 0.995,
}


def fmt(s):
    if not s.get("n"):
        return "n=0"
    return (f"n={s['n']:4d} days={s['events']:2d} win={s['win']:.3f} px={s['avg_px']:.3f} ret={s['ret']:+.4f} "
            f"t_day={s['t_day']:5.2f} t_win={s['t_window']:5.2f} t_tk={s['t_ticker']:5.2f} wo3={s['ret_wo3']:+.4f} "
            f"h={s['half1']:+.4f}/{s['half2']:+.4f} dayh={s['dayhalf1']:+.4f}/{s['dayhalf2']:+.4f} cap={s['median_cnt']:.1f} "
            f"uniq={s['unique_combos']} LB={s['binom_lb_ret']:+.4f}")


def diagnostics(say):
    """Concentration and stationarity checks on C1 (original + recovered fills, fee rounded per $5 order)."""
    rows, _, _ = build(rec=True, round_fee=True)
    c1 = [r for r in rows if CANDS["C1"](r)]; val = [r for r in c1 if r["day"] >= CUT]; disc = [r for r in c1 if r["day"] < CUT]
    say("=== DIAGNOSTICS (C1, original + recovered, fee rounded per $5 order)")
    say(f"  VAL  {fmt(stats(val))}"); say(f"  DISC {fmt(stats(disc))}"); say(f"  POOL {fmt(stats(c1))}")
    par = {}
    def find(x):
        par.setdefault(x, x)
        while par[x] != x:
            par[x] = par[par[x]]; x = par[x]
        return x
    for r in c1:
        for e in r["legev"]:
            par[find("T:" + r["tk"])] = find("E:" + e)
    for r in c1:
        r["cl"] = find("T:" + r["tk"])
    for lab, s in (("VAL", val), ("DISC", disc), ("POOL", c1)):
        t, n = ct(s, "cl"); say(f"  {lab} t clustered by shared-leg components: {t:.2f} on {n} clusters")
    g = defaultdict(list)
    for r in val:
        g[r["day"]].append(r)
    say("  VAL by day: " + ", ".join(f"{d} n={len(v)} {st.mean(x['ret'] for x in v):+.3f} (rec {sum(x['rec'] for x in v)})" for d, v in sorted(g.items())))
    best = max(g, key=lambda k: sum(x["ret"] for x in g[k]))
    say(f"  VAL without best day {best}: {fmt(stats([r for r in val if r['day'] != best]))}")
    for lab, fn in (("age<2h", lambda r: r["age"] < 7200), ("age>=2h", lambda r: r["age"] >= 7200), ("exp<6h", lambda r: r["exp_h"] < 6),
                    ("exp>=6h", lambda r: r["exp_h"] >= 6), ("legs2", lambda r: r["nlegs"] == 2), ("legs3+", lambda r: r["nlegs"] >= 3)):
        sd, sv = stats([r for r in disc if fn(r)]), stats([r for r in val if fn(r)])
        say(f"  {lab:8s} disc ret {sd.get('ret', float('nan')):+.3f} (n={sd['n']}) | val ret {sv.get('ret', float('nan')):+.3f} (n={sv['n']})")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    lines, res = [], {}
    def say(x=""):
        print(x); lines.append(x)
    for label, kw in (("orig_data", {}), ("orig_data_fee_rounded_5usd", {"round_fee": True}), ("orig_plus_recovered", {"rec": True})):
        rows, skip, nw = build(**kw)
        days = sorted({r["day"] for r in rows})
        cut = days[int(len(days) * 0.7)] if days else ""
        say(f"=== {label}: windows {nw} rows {len(rows)} skipped {dict(skip)} days {len(days)} ({days[0]}..{days[-1]}) "
            f"70% cut by day = {cut} (fixed cut used: {CUT})")
        disc = [r for r in rows if r["day"] < CUT]; val = [r for r in rows if r["day"] >= CUT]
        say(f"  discovery {len(disc)} fills ({Counter(r['side'] for r in disc)}), validation {len(val)}")
        res[label] = {"skip": dict(skip), "rows": len(rows), "days": len(days), "cands": {}}
        for c, fn in CANDS.items():
            sd, sv = stats([r for r in disc if fn(r)]), stats([r for r in val if fn(r)])
            sp = stats([r for r in rows if fn(r)])
            say(f"  {c} disc  {fmt(sd)}"); say(f"  {c} VAL   {fmt(sv)}"); say(f"  {c} pool  {fmt(sp)}")
            res[label]["cands"][c] = {"disc": sd, "val": sv, "pooled": sp}
        if label == "orig_plus_recovered":
            recrows = [r for r in rows if r["rec"]]
            say(f"  recovered-only fills (never seen by the original analysis): {len(recrows)}")
            for c, fn in CANDS.items():
                s = stats([r for r in recrows if fn(r)])
                say(f"  {c} recovered-only {fmt(s)}"); res[label]["cands"][c]["recovered_only"] = s
                s = stats([r for r in recrows if fn(r) and r["day"] >= CUT])
                say(f"  {c} recovered-only VAL {fmt(s)}"); res[label]["cands"][c]["recovered_only_val"] = s
        if label == "orig_data":
            v1 = sorted([r for r in val if CANDS["C1"](r)], key=lambda r: r["t"])
            say("  C1 validation fills:")
            for r in v1:
                say(f"    {r['day']} w{r['w']:2d} {r['tk'][-40:]} px={r['px']:.4f} pay={r['pay']:.2f} ret={r['ret']:+.3f} cnt={r['cnt']:.1f} "
                    f"age_h={r['age']/3600:.1f} exp_h={r['exp_h']:.1f} legs={r['nlegs']}")
            dup = Counter((r["tk"], r["w"]) for r in v1)
            say(f"  C1 val prints per (ticker, window): {sorted(dup.values(), reverse=True)}")
            byday = defaultdict(list)
            for r in v1:
                byday[r["day"]].append(r["ret"])
            say("  C1 val by day: " + ", ".join(f"{d}: n={len(v)} {st.mean(v):+.3f}" for d, v in sorted(byday.items())))
            # one trade per combo (first print) - an RFQ split into several prints is one decision
            u = {}
            for r in v1:
                u.setdefault(r["tk"], r)
            say(f"  C1 VAL one-per-combo {fmt(stats(list(u.values())))}")
            res[label]["C1_val_one_per_combo"] = stats(list(u.values()))
            # sensitivity: split by combo scheduled expiry instead of print day (protocol: order events by close)
            ex = sorted(rows, key=lambda r: r["t"] + r["exp_h"] * 3600)
            cut_t = ex[int(len(ex) * 0.7)]["t"] + ex[int(len(ex) * 0.7)]["exp_h"] * 3600
            v2 = [r for r in rows if r["t"] + r["exp_h"] * 3600 >= cut_t and CANDS["C1"](r)]
            say(f"  C1 VAL if split by scheduled combo expiry (70% of fills): {fmt(stats(v2))}")
            res[label]["C1_val_split_by_expiry"] = stats(v2)
    diagnostics(say)
    (OUT / "repro.txt").write_text("\n".join(lines) + "\n")
    (OUT / "repro.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
