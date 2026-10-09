"""r4_long_dated_maker_no: resting NO bids (sell YES at yes_ask - 1c) on long-dated Kalshi retail questions.

Hypothesis. Long-dated narrative markets (shutdowns, Starship, LLM rankings, pardons, GDP/Fed by-date questions) are
zero-maker-fee ('quadratic') or 0.25x-maker-fee series with 2-6c spreads and often no professional market maker. A
resting NO bid that improves the NO side by 1c (sell YES at yes_ask - 0.01), posted when the NO taker price is in
[0.70, 0.95) at D in {60, 45, 30} days before the scheduled deadline and left for W days, should add (spread - 1c)
plus the taker fee to the long-dated NO premium found in round 3 (taker +3-13%), without paying for it in adverse
selection, because holds last weeks and fills come from slow retail YES buyers rather than informed flow.

Data. Discovery = the 209 eligible markets whose raw hourly candles round 3 cached (r4_long_dated_maker_no_data),
ordered by event close: first 70% of events. Validation = last 30% of those events PLUS a fresh mini-census of markets
never fetched before (new long-dated series, seeded sample of <= 2 markets per event from listing fields only),
which was fetched only after the candidates were frozen (frozen.json). Fill model: see r4_long_dated_maker_no_core.
Trade-print audit: /historical/trades (or /markets/trades) over each audited order's window: exact through-only,
size-weighted fill fractions (amendment c), checks of the candle fill rule.

Stages (each writes under data/kalshi_lab/strategies/r4_long_dated_maker_no/):
  --discover        grid on discovery only; writes discovery.json and frozen.json (<= 3 candidates)      0 calls
  --fresh N         list new long-dated series and fetch hourly candles of a seeded sample (<= N calls)
  --audit N         trade prints for orders of the frozen candidates (<= N calls)
  --validate        evaluates the frozen candidates once; writes validation.json and result.json
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_maker_no --discover"""
from __future__ import annotations
import hashlib, itertools, json, math, re, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r3_long_dated_longshot_no_repro as R
from lab.kalshi.strategies import r4_long_dated_maker_no_core as X
from lab.kalshi.strategies.r4_long_dated_maker_no_api import OUT, cached, total_used, BudgetExhausted
from lab.kalshi.strategies.r4_long_dated_maker_no_data import load_known, fee_info, fetch_market_candles, trades_window, parse_raw

TASK_BUDGET = 200
BANDS = {"B70_95": (0.70, 0.95), "B70_80": (0.70, 0.80), "B80_95": (0.80, 0.95), "B80_92": (0.80, 0.92), "B86_95": (0.86, 0.95)}
DSETS = {"D60_45_30": (60, 45, 30), "D60": (60,), "D45": (45,), "D30": (30,)}
WS = (3, 7, 14)
ORDERS = ("static_through", "static_any", "refresh_through")


# ------------------------------------------------------------------------------------------------ rule evaluation
def evaluate(rows: list[dict], fees: dict, band, Ds, W: int, order: str, wkey: str | None = None) -> dict:
    """Signals, maker fills (with returns), unfilled signals and the taker comparator on the same signals."""
    lo, hi = band
    sig = [r for r in rows if lo <= r["ps"] < hi and r["D"] in Ds]
    elig, fills, unf = [], [], []
    for r in sig:
        if order.startswith("static"):
            if r.get("s") is None:
                continue
            elig.append(r)
            mode = order.split("_")[1]
            f = r.get(f"f_{mode}_{W}")
            if f:
                x = dict(r, ret=r["ret_m"], px=r["px_m"], ts=f, fill_ts=f)
                n = r["N"]
                x["w_lo"] = min(n, r[f"vt_lo_{W}"]) / n
                x["w_hi"] = min(n, r[f"vt_hi_{W}"]) / n
                if wkey and r.get(wkey) is not None:
                    x["w_print"] = r[wkey]
                fills.append(x)
            else:
                unf.append(r)
        else:
            ftype, mult = fees.get(r["series"], ("quadratic", 1.0))
            ok = any(x is not None and x[1] is not None and lo <= x[0] < hi + 0.03 for x in r["days"][:W])
            if not ok:
                continue
            elig.append(r)
            f = X.refresh_fill(r, lo, hi, W, ftype, mult)
            if f:
                fills.append(dict(r, ret=f["ret_m"], px=f["px_m"], ts=f["fill_ts"], fill_ts=f["fill_ts"]))
            else:
                unf.append(r)
    taker_all = [dict(r, ret=r["ret_t"], px=r["px_t"], ts=r["t_post"]) for r in sig]
    taker_elig = [dict(r, ret=r["ret_t"], px=r["px_t"], ts=r["t_post"]) for r in elig]
    taker_filled = [dict(r, ret=r["ret_t"], px=r["px_t"]) for r in fills]
    res = {"signals": len(sig), "maker_eligible": len(elig), "fills": len(fills),
           "fill_rate": round(len(fills) / len(elig), 4) if elig else None,
           "win_filled": round(st.mean(r["won"] for r in fills), 4) if fills else None,
           "win_unfilled": round(st.mean(r["won"] for r in unf), 4) if unf else None,
           "n_unfilled": len(unf),
           "maker": X.stats(fills, w="w_lo" if order.startswith("static") else None),
           "taker_all_signals": X.stats(taker_all), "taker_on_maker_eligible": X.stats(taker_elig), "taker_on_filled": X.stats(taker_filled)}
    if fills and order.startswith("static"):
        res["maker"]["ret_sw_candle_lo"] = _sw(fills, "w_lo"); res["maker"]["ret_sw_candle_hi"] = _sw(fills, "w_hi")
        pr = [x for x in fills if "w_print" in x]
        if pr:
            res["maker"]["print_audited"] = len(pr); res["maker"]["ret_sw_print"] = _sw(pr, "w_print")
            res["maker"]["ret_eq_print_through"] = round(st.mean(x["ret"] for x in pr if x["w_print"] > 0), 4) if any(x["w_print"] > 0 for x in pr) else None
    # per-signal $ economics: equal $ committed per maker-eligible signal; unfilled = 0
    if elig:
        res["per_signal_maker"] = round(sum(r["ret"] for r in fills) / len(elig), 4)
        res["per_signal_taker"] = round(st.mean(r["ret_t"] for r in elig), 4)
        # paired: on the filled signals, maker minus taker (price improvement) and taker-on-filled minus taker-on-eligible (selection)
        if fills:
            res["paired_improvement_on_fills"] = round(st.mean(r["ret"] - r["ret_t"] for r in fills), 4)
            res["selection_effect"] = round(st.mean(r["ret_t"] for r in fills) - st.mean(r["ret_t"] for r in elig), 4)
    return res


def _sw(rows, w):
    s = sum(r[w] for r in rows)
    return round(sum(r["ret"] * r[w] for r in rows) / s, 4) if s > 0 else None


def all_rows():
    M, C = load_known()
    F = fee_info()
    rows = X.simulate(M, C, F, Ws=WS)
    cut, evc = X.split_cut(rows, M)
    for r in rows:
        r["period"] = "disc" if evc[r["e"]] < cut else "val"
    return M, C, F, rows, cut


def discover() -> None:
    M, C, F, rows, cut = all_rows()
    disc = [r for r in rows if r["period"] == "disc"]
    grid = {}
    for (bn, band), (dn, Ds), W, order in itertools.product(BANDS.items(), DSETS.items(), WS, ORDERS):
        grid[f"{bn}|{dn}|W{W}|{order}"] = evaluate(disc, F, band, Ds, W, order)
    K = len(grid)
    # selection (discovery only): a candidate must have >= 25 fills, maker equal-$ >= +10%, candle-lower-bound size-weighted
    # >= +10%, no adverse selection (filled win >= unfilled win - 5 pts) and beat the taker on the same signals per
    # signal. Rank by the event-clustered t of the maker fills, but C1 is the pre-specified test-plan rule.
    def ok(v):
        m = v["maker"]
        return (v["fills"] >= 25 and m.get("ret_per_dollar", -9) >= 0.10 and (m.get("ret_sw_candle_lo") or m.get("ret_per_dollar", -9)) >= 0.10
                and (v["win_unfilled"] is None or v["win_filled"] >= v["win_unfilled"] - 0.05) and v.get("per_signal_maker", -9) > v.get("per_signal_taker", 9))
    ranked = sorted(((k, v) for k, v in grid.items() if ok(v)), key=lambda kv: -(kv[1]["maker"].get("t") or 0))
    # C1 = the test-plan rule and C2 = its daily-refresh form (the hypothesis' 'refreshed once a day') are pre-specified;
    # only C3 is data-selected: the top of the discovery screen (by event-clustered t) that is neither C1 nor C2.
    c1 = "B70_95|D60_45_30|W7|static_through"
    c2 = "B70_95|D60_45_30|W7|refresh_through"
    c3 = next((k for k, v in ranked if k not in (c1, c2)), None)
    frozen = {"written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "split_cut_event_close": cut,
              "note": "frozen on discovery (first 70% of the known-209 events by close); validation not yet computed. "
                      "C1 test-plan rule, C2 its daily-refresh form (both pre-specified), C3 discovery-selected.",
              "candidates": {"C1": c1, "C2": c2, "C3": c3},
              "discovery": {k: grid[k] for k in (c1, c2, c3) if k}}
    frozen["variants_examined_discovery_grid"] = K
    (OUT / "discovery.json").write_text(json.dumps({"K": K, "cut": cut, "grid": grid}, indent=1, default=str))
    if (OUT / "frozen.json").exists():
        print("frozen.json exists: not overwritten (candidates are frozen)")
    else:
        (OUT / "frozen.json").write_text(json.dumps(frozen, indent=1))
    print(f"discovery rows {len(disc)} (events {len({r['e'] for r in disc})}), grid K={K}, passing the discovery screen {len(ranked)}")
    for k, v in ranked[:15]:
        m = v["maker"]
        print(f"  {k:40} fills={v['fills']:3d}/{v['maker_eligible']:3d} winF={v['win_filled']} winU={v['win_unfilled']} mk={m['ret_per_dollar']:+.3f} t={m['t']} swlo={m.get('ret_sw_candle_lo')} "
              f"tk_same={v['taker_on_maker_eligible'].get('ret_per_dollar')} perSig m/t={v['per_signal_maker']}/{v['per_signal_taker']}")
    print("C1 (pre-specified):", json.dumps({k: grid[c1][k] for k in ("signals", "maker_eligible", "fills", "fill_rate", "win_filled", "win_unfilled", "per_signal_maker", "per_signal_taker", "paired_improvement_on_fills", "selection_effect")}))
    print("frozen:", json.loads((OUT / "frozen.json").read_text())["candidates"])


# ------------------------------------------------------------------------------------------------ fresh mini-census
FRESH_SERIES_N = 22          # unlisted long-dated series (frequency one_off/annual/custom, 5 categories), top by volume
FRESH_SEED = "r4_long_dated_maker_no"


def fresh_series() -> list[str]:
    listed = set()
    for u in R.listing_urls():
        x = re.search(r"series_ticker=([A-Z0-9]+)", u)
        if x:
            listed.add(x.group(1))
    rows = []
    for c in R.CATS:
        for s in (R.raw(f"/series?category={c.replace(' ', '%20')}&include_volume=true") or {}).get("series", []):
            if s.get("frequency") in ("one_off", "annual", "custom") and s["ticker"] not in listed:
                rows.append((float(s.get("volume_fp") or 0), s["ticker"], c))
    rows.sort(reverse=True)
    return [(t, c) for _, t, c in rows[:FRESH_SERIES_N]]


def fresh_universe(allow_fetch: bool) -> dict:
    out = {}
    for ser, cat in fresh_series():
        u = f"/historical/markets?series_ticker={ser}&limit=1000"
        d = cached(u, TASK_BUDGET) if allow_fetch else _cache_only(u)
        for m in (d or {}).get("markets", []):
            if m.get("market_type") != "binary" or m.get("result") not in ("yes", "no"):
                continue
            S, how = R.deadline(m)
            op, cl = R.ts(m.get("open_time")), R.ts(m.get("close_time"))
            if S is None or op is None or cl is None or S - op < 45 * R.DAY:
                continue
            if not any(S - D * R.DAY >= op and S - D * R.DAY + 3600 < cl for D in (60, 45, 30)):
                continue
            out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": ser, "cat": cat, "open": op, "close": cl, "S": S, "S_how": how,
                                "result": m["result"], "early_flag": bool(m.get("can_close_early")), "hist": True, "src": "fresh",
                                "vol": float(m.get("volume_fp") or m.get("volume") or 0)}
    return out


def _cache_only(u):
    from lab.us.data_refresh import K
    f = OUT / "api_cache" / f"{hashlib.sha1((K + u).encode()).hexdigest()}.json"
    return json.loads(f.read_text()) if f.exists() else None


def fresh_sample(U: dict, known: set) -> list[dict]:
    """<= 2 markets per event (seeded hash of the ticker, listing fields only), events in seeded-hash order."""
    by = defaultdict(list)
    for m in U.values():
        if m["t"] not in known:
            by[m["e"]].append(m)
    evs = sorted(by, key=lambda e: hashlib.sha1((FRESH_SEED + e).encode()).hexdigest())
    out = []
    for e in evs:
        out += sorted(by[e], key=lambda m: hashlib.sha1((FRESH_SEED + m["t"]).encode()).hexdigest())[:2]
    return out


def fresh(max_calls: int) -> None:
    assert (OUT / "frozen.json").exists(), "freeze candidates first (--discover)"
    start = total_used()
    U = fresh_universe(True)
    M, _ = load_known()
    sample = fresh_sample(U, set(M))
    got = 0
    for m in sample:
        if total_used() - start >= max_calls:
            break
        try:
            c = fetch_market_candles(m, TASK_BUDGET)
        except BudgetExhausted:
            break
        got += bool(c)
    log = {"series": fresh_series(), "eligible_markets": len(U), "eligible_events": len({m['e'] for m in U.values()}), "sampled": len(sample),
           "fetched_with_candles": got, "calls_this_stage": total_used() - start}
    (OUT / "fresh_log.json").write_text(json.dumps(log, indent=1))
    print(log)


def load_fresh() -> tuple[dict, dict]:
    U = fresh_universe(False)
    M0, _ = load_known()
    from lab.us.data_refresh import K
    M, C = {}, {}
    for m in fresh_sample(U, set(M0)):
        t = m["t"]
        start = max(m["open"], m["S"] - 62 * R.DAY) // 3600 * 3600
        end = min(m["close"], m["S"])
        d = _cache_only(f"/historical/markets/{t}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60")
        if d and d.get("candlesticks"):
            c = parse_raw(d["candlesticks"])
            if c:
                M[t] = m; C[t] = c
    return M, C


# ------------------------------------------------------------------------------------------------ trade-print audit
AUDIT_PAGES = 2


def frozen_cands() -> dict:
    fz = json.loads((OUT / "frozen.json").read_text())
    out = {}
    for name, key in fz["candidates"].items():
        bn, dn, w, order = key.split("|")
        out[name] = {"key": key, "band": BANDS[bn], "Ds": DSETS[dn], "W": int(w[1:]), "order": order}
    return out


def oos_rows() -> tuple[list[dict], dict]:
    """Out-of-sample rows: validation period of the known 209 plus every fresh market (never fetched before)."""
    M, C, F, rows, cut = all_rows()
    Mf, Cf = load_fresh()
    fr = X.simulate(Mf, Cf, F, Ws=WS)
    for r in fr:
        r["period"] = "fresh_pre_cut" if r["close"] < cut else "fresh_post_cut"
    val = [r for r in rows if r["period"] == "val"]
    return val + fr, F


def candidate_signals(rows: list[dict], c: dict) -> list[dict]:
    lo, hi = c["band"]
    out = []
    for r in rows:
        if not (lo <= r["ps"] < hi and r["D"] in c["Ds"]):
            continue
        if c["order"].startswith("static"):
            if r.get("s") is not None:
                out.append(r)
        elif any(x is not None and x[1] is not None and lo <= x[0] < hi + 0.03 for x in r["days"][:c["W"]]):
            out.append(r)
    return out


def audit_key(r: dict) -> str:
    return f"{r['t']}|{r['D']}"


def print_window(r: dict, W: int) -> tuple[int, int]:
    return r["t_post"] + 120, int(min(r["t_post"] + W * X.DAY, r["close"], r["S"]))


def audit(max_calls: int, census: bool = False) -> None:
    """Prints for the frozen candidates' out-of-sample orders. census=True: the census markets' C1 orders only
    (round 2 of the audit, after the first audit showed candle and print fills agree on 28/28 C1 orders)."""
    start = total_used()
    rows, F = oos_rows()
    cands = frozen_cands()
    names = ("C1", "C2", "C3")
    if census:
        M, _ = load_known(); Mf, _ = load_fresh()
        Mc, Cc = load_census(set(M) | set(Mf))
        rows = X.simulate(Mc, Cc, F, Ws=WS); names = ("C1",)
    todo, seen = [], set()
    for name in names:
        for r in sorted(candidate_signals(rows, cands[name]), key=lambda r: r["t_post"]):
            k = audit_key(r)
            if k not in seen:
                seen.add(k); todo.append((r, max(c["W"] for c in cands.values())))
    done = 0
    for r, W in todo:
        if total_used() - start >= max_calls - (AUDIT_PAGES - 1):
            break
        lo, hi = print_window(r, W)
        try:
            pr = trades_window(r["t"], r["hist"], lo, hi, TASK_BUDGET, pages=AUDIT_PAGES)
        except BudgetExhausted:
            break
        done += pr is not None
    log = {"orders_to_audit": len(todo), "audited_this_run": done, "calls_this_stage": total_used() - start, "census": census}
    (OUT / ("audit_census_log.json" if census else "audit_log.json")).write_text(json.dumps(log, indent=1))
    print(log)


def prints_for(r: dict, W: int):
    """Cached prints of the audit window (no call)."""
    lo, hi = print_window(r, W)
    base = "/historical/trades" if r["hist"] else "/markets/trades"
    out, cur, trunc = [], "", False
    import calendar
    for i in range(AUDIT_PAGES):
        d = _cache_only(f"{base}?ticker={r['t']}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={cur}" if cur else ""))
        if d is None:
            return None if i == 0 else (sorted(out), True)
        for x in d.get("trades") or []:
            sx = x["created_time"].replace("Z", "")
            sec = calendar.timegm(time.strptime(sx[:19], "%Y-%m-%dT%H:%M:%S"))
            out.append((sec, float(x.get("yes_price_dollars") or 0), float(x.get("count_fp") or x.get("count") or 0), x.get("taker_side"), bool(x.get("is_block_trade"))))
        cur = d.get("cursor") or ""
        if not cur or not d.get("trades"):
            return sorted(out), False
    return sorted(out), True


def print_fill(prints: list, s: float, N: int, lo: float, hi: float) -> dict:
    win = [p for p in prints if lo < p[0] <= hi and not p[4]]
    thr = sum(p[2] for p in win if p[1] > s + 1e-9)
    anyc = sum(p[2] for p in win if p[1] >= s - 1e-9)
    t1 = next((p[0] for p in win if p[1] > s + 1e-9), None)
    return {"through_cnt": thr, "any_cnt": anyc, "f_through": min(N, thr) / N, "f_any": min(N, anyc) / N, "t_first_through": t1, "n_prints": len(win)}


# ------------------------------------------------------------------------------------------------ census markets (data only)
CENSUS = Path("/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/r4_long_dated_archive_census")


def load_census(exclude: set) -> tuple[dict, dict]:
    """Markets of the parallel census researcher with raw hourly candles on disk (0 calls). Metadata from any cached
    listing on disk (deadline parser of round 3); eligibility as ours: binary, settled, S - open >= 45 d."""
    if not (CENSUS / "calls.log").exists():
        return {}, {}
    from lab.kalshi.strategies.r4_long_dated_archive_census_api import cached_markets
    raw_m = cached_markets()
    cat = {}
    for f in (CENSUS / "api_cache").glob("*.json"):
        pass
    M, C = {}, {}
    for line in (CENSUS / "calls.log").read_text().splitlines():
        p = line.split("\t")
        if len(p) < 4 or "candlesticks" not in p[3]:
            continue
        path = p[3]
        f = CENSUS / "api_cache" / (hashlib.sha1(path.encode()).hexdigest() + ".json")
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        if "/historical/markets/" in path:
            lst = [(path.split("/historical/markets/")[1].split("/")[0], d.get("candlesticks") or [])]
        else:
            lst = [(x.get("market_ticker"), x.get("candlesticks") or []) for x in d.get("markets", [])]
        for t, cs in lst:
            if not t or t in exclude or t in M:
                continue
            m = raw_m.get(t)
            if not m or m.get("market_type", "binary") != "binary" or m.get("result") not in ("yes", "no"):
                continue
            S, how = R.deadline(m)
            op, cl = R.ts(m.get("open_time")), R.ts(m.get("close_time"))
            if S is None or op is None or cl is None or S - op < 45 * R.DAY:
                continue
            c = parse_raw(cs)
            if not c:
                continue
            ser = t.split("-")[0]
            M[t] = {"t": t, "e": m["event_ticker"], "series": ser, "open": op, "close": cl, "S": S, "S_how": how, "result": m["result"],
                    "hist": "/historical/" in path, "src": "census"}
            C[t] = c
    return M, C


# ------------------------------------------------------------------------------------------------ validation (once)
def print_eval(r: dict, c: dict, fees: dict):
    """Print-based through-only fill of one order (None if its prints were not audited)."""
    W = max(cc["W"] for cc in frozen_cands().values())
    got = prints_for(r, W)
    if got is None:
        return None
    prints, trunc = got
    ftype, mult = fees.get(r["series"], ("quadratic", 1.0))
    end = min(r["t_post"] + c["W"] * X.DAY, r["close"], r["S"])
    if c["order"].startswith("static"):
        f = print_fill(prints, r["s"], r["N"], r["t_post"] + 120, end)
        f.update(px=r["px_m"], ret=r["ret_m"], truncated=trunc)
        return f
    lo_b, hi_b = c["band"]
    for d, x in enumerate(r["days"][:c["W"]]):
        if x is None:
            continue
        ps_d, sd, _ = x
        if sd is None or not (lo_b <= ps_d < hi_b + 0.03):
            continue
        td = r["t_post"] + d * X.DAY
        pm = round(1 - sd, 4); nm = X.n_contracts(pm)
        f = print_fill(prints, sd, nm, td + 120, min(td + X.DAY, end))
        if f["f_through"] > 0:
            f.update(px=pm, ret=X.ret_of(r["won"], pm, X.fee_pc(pm, "maker", ftype, mult, nm)), day=d, truncated=trunc)
            return f
    return {"f_through": 0.0, "f_any": 0.0, "through_cnt": 0.0, "px": None, "ret": None, "truncated": trunc}


def judge(rows: list[dict], fees: dict, name: str, c: dict) -> dict:
    v = evaluate(rows, fees, c["band"], c["Ds"], c["W"], c["order"])
    sig = candidate_signals(rows, c)
    pe = []
    agree = {"both": 0, "candle_only": 0, "print_only": 0, "neither": 0}
    for r in sig:
        f = print_eval(r, c, fees)
        if f is None:
            continue
        if c["order"].startswith("static"):
            cf = r.get(f"f_through_{c['W']}") is not None
        else:
            ftype, mult = fees.get(r["series"], ("quadratic", 1.0))
            cf = X.refresh_fill(r, c["band"][0], c["band"][1], c["W"], ftype, mult) is not None
        pf = f["f_through"] > 0
        agree[("both" if cf else "print_only") if pf else ("candle_only" if cf else "neither")] += 1
        pe.append((r, f))
    # combined fill list over ALL signals: print fill where audited, else the candle through fill (weight: assumed full,
    # or the conservative candle-volume lower bound for static orders / 0 for refresh orders)
    audited = {audit_key(r): f for r, f in pe}
    comb = []
    for r in sig:
        f = audited.get(audit_key(r))
        if f is not None:
            if f["f_through"] > 0:
                comb.append(dict(r, ret=f["ret"], px=f["px"], w_full=f["f_through"], w_cons=f["f_through"], ts=r["t_post"], src_fill="print"))
            continue
        if c["order"].startswith("static"):
            if r.get(f"f_through_{c['W']}") is not None:
                n = r["N"]
                comb.append(dict(r, ret=r["ret_m"], px=r["px_m"], w_full=1.0, w_cons=min(n, r[f"vt_lo_{c['W']}"]) / n, ts=r["t_post"], src_fill="candle"))
        else:
            ftype, mult = fees.get(r["series"], ("quadratic", 1.0))
            g = X.refresh_fill(r, c["band"][0], c["band"][1], c["W"], ftype, mult)
            if g:
                comb.append(dict(r, ret=g["ret_m"], px=g["px_m"], w_full=1.0, w_cons=0.0, ts=r["t_post"], src_fill="candle"))
    comb_unf = [r for r in sig if audit_key(r) not in {audit_key(x) for x in comb}]
    combined = {"fills": len(comb), "from_prints": sum(x["src_fill"] == "print" for x in comb), "signals": len(sig),
                "win_filled": round(st.mean(x["won"] for x in comb), 4) if comb else None,
                "win_unfilled": round(st.mean(x["won"] for x in comb_unf), 4) if comb_unf else None, "n_unfilled": len(comb_unf),
                "maker_equal_dollar": X.stats(comb), "ret_sw_full": _sw(comb, "w_full") if comb else None, "ret_sw_conservative": _sw(comb, "w_cons") if comb else None,
                "taker_same_signals": X.stats([dict(r, ret=r["ret_t"], px=r["px_t"], ts=r["t_post"]) for r in sig]),
                "per_signal_maker": round(sum(x["ret"] * x["w_full"] for x in comb) / len(sig), 4) if sig else None,
                "per_signal_taker": round(st.mean(r["ret_t"] for r in sig), 4) if sig else None,
                "paired_maker_minus_taker_on_fills": round(st.mean(x["ret"] - x["ret_t"] for x in comb), 4) if comb else None,
                "fills_per_day": round(len(comb) / max(1.0, (max(x["t_post"] for x in comb) - min(x["t_post"] for x in comb)) / 86400), 4) if len(comb) > 1 else None,
                "median_fill_contracts_prints": st.median([f["through_cnt"] for _, f in pe if f["f_through"] > 0]) if any(f["f_through"] > 0 for _, f in pe) else None}
    filled = [dict(r, ret=f["ret"], px=f["px"], w=f["f_through"], ts=r["t_post"]) for r, f in pe if f["f_through"] > 0]
    unf = [r for r, f in pe if f["f_through"] == 0]
    out = {"candle": v, "combined": combined, "print_audit": {"audited": len(pe), "candle_vs_print_through": agree, "fills_through": len(filled),
                                        "fill_rate": round(len(filled) / len(pe), 4) if pe else None,
                                        "win_filled": round(st.mean(r["won"] for r in filled), 4) if filled else None,
                                        "win_unfilled": round(st.mean(r["won"] for r in unf), 4) if unf else None,
                                        "maker_equal_dollar": X.stats(filled), "ret_size_weighted": _sw(filled, "w") if filled else None,
                                        "median_through_contracts": st.median([f["through_cnt"] for _, f in pe if f["f_through"] > 0]) if filled else None,
                                        "mean_fill_fraction": round(st.mean(f["f_through"] for _, f in pe if f["f_through"] > 0), 4) if filled else None,
                                        "taker_same_signals": X.stats([dict(r, ret=r["ret_t"], px=r["px_t"], ts=r["t_post"]) for r, _ in pe])}}
    return out


def validate() -> None:
    fz = json.loads((OUT / "frozen.json").read_text())
    assert "validation_plan" in fz
    if (OUT / "validation.json").exists() and "--force" not in sys.argv:
        print("validation.json exists: the frozen candidates were already evaluated once"); return
    M, C, F, rows, cut = all_rows()
    Mf, Cf = load_fresh()
    fr = X.simulate(Mf, Cf, F, Ws=WS)
    Mc, Cc = load_census(set(M) | set(Mf))
    ce = X.simulate(Mc, Cc, F, Ws=WS)
    sets = {"known_val": [r for r in rows if r["period"] == "val"], "fresh": fr, "census": ce}
    sets["primary_union"] = sets["known_val"] + sets["fresh"] + sets["census"]
    cands = frozen_cands()
    res = {"written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sizes": {k: {"rows": len(v), "markets": len({r['t'] for r in v}),
           "events": len({r['e'] for r in v})} for k, v in sets.items()}, "census_markets_loaded": len(Mc), "results": {}}
    for name, c in cands.items():
        res["results"][name] = {"key": c["key"]}
        for sn, rs in sets.items():
            res["results"][name][sn] = judge(rs, F, name, c)
    (OUT / "validation.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["sizes"]))
    for name in cands:
        for sn in sets:
            v = res["results"][name][sn]["candle"]; m = v["maker"]; pa = res["results"][name][sn]["print_audit"]
            print(f"{name} {sn:14} sig={v['signals']:3d} elig={v['maker_eligible']:3d} fills={v['fills']:3d} rate={v['fill_rate']} winF={v['win_filled']} winU={v['win_unfilled']} "
                  f"mk={m.get('ret_per_dollar')} t={m.get('t')} wo3={m.get('ret_wo3')} h={m.get('half1')}/{m.get('half2')} swlo={m.get('ret_sw_candle_lo')} lo95={m.get('ret_at_win_lo95')} | "
                  f"tk_all={v['taker_all_signals'].get('ret_per_dollar')} tk_elig={v['taker_on_maker_eligible'].get('ret_per_dollar')} perSig m/t={v.get('per_signal_maker')}/{v.get('per_signal_taker')} "
                  f"| prints aud={pa['audited']} fills={pa['fills_through']} sw={pa['ret_size_weighted']} agree={pa['candle_vs_print_through']}")
            cb = res["results"][name][sn]["combined"]; me = cb["maker_equal_dollar"]
            print(f"      COMBINED fills={cb['fills']} (prints {cb['from_prints']}) winF={cb['win_filled']} winU={cb['win_unfilled']} (n={cb['n_unfilled']}) eq={me.get('ret_per_dollar')} t={me.get('t')} "
                  f"wo3={me.get('ret_wo3')} h={me.get('half1')}/{me.get('half2')} lo95={me.get('ret_at_win_lo95')} sw_full={cb['ret_sw_full']} sw_cons={cb['ret_sw_conservative']} "
                  f"taker_same={cb['taker_same_signals'].get('ret_per_dollar')} perSig m/t={cb['per_signal_maker']}/{cb['per_signal_taker']} paired={cb['paired_maker_minus_taker_on_fills']}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--discover" in a:
        discover()
    elif "--fresh" in a:
        fresh(int(a[a.index("--fresh") + 1]))
    elif "--audit-census" in a:
        audit(int(a[a.index("--audit-census") + 1]), census=True)
    elif "--audit" in a:
        audit(int(a[a.index("--audit") + 1]))
    elif "--validate" in a:
        validate()
