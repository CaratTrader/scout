"""Independent reproduction of the r2_mentions_baserate maker claim (written from the claim text only).

Claim: in Kalshi "will <speaker> say <word>" markets, at the first top-of-hour H >= open_time + 1 h (no market of
the event closed yet, 0 < bid, ask < 1, ask - bid >= 2c) rest a sell-YES (= buy-NO) order at s = ask - 0.01; fill on
a later trade printed at yes_price >= s, cancel when the event's first market closes; no fee (series fee_type
'quadratic' charges makers nothing). C1 = every market, C2/C3 = only if a frozen base-rate blend gives the NO
an EV >= 20% / 10%. Split: events ordered by close time, first 70% discovery, last 30% validation.

Data: the raw Kalshi API responses cached by the other researcher (data/kalshi_lab/strategies/r2_mentions_baserate/
api_cache/*.json: /markets, /historical/markets, hourly /markets/candlesticks) are parsed here from scratch; their
processed jsonl files and code are not used. A few responses are re-fetched to check the cache is genuine, and
/markets/trades prints are fetched for a random sample of validation orders to test the hourly-candle fill model.

Usage (from the repo root):
  .venv/bin/python -m lab.kalshi.strategies.r2_mentions_baserate_repro run        # backtest + sensitivities
  .venv/bin/python -m lab.kalshi.strategies.r2_mentions_baserate_repro verify     # re-fetch 2 cached responses
  .venv/bin/python -m lab.kalshi.strategies.r2_mentions_baserate_repro trades N   # trade prints for N val orders
  .venv/bin/python -m lab.kalshi.strategies.r2_mentions_baserate_repro tradefill  # exact-time fill audit
"""
from __future__ import annotations
import json, math, os, random, re, statistics as st, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

SRC = Path("data/kalshi_lab/strategies/r2_mentions_baserate/api_cache")
OUT = Path("data/kalshi_lab/strategies/r2_mentions_baserate/repro")
TR = OUT / "trades"
EXCLUDE = {"KXTRUMPSAY"}
GROUP = {"KXTRUMPMENTIONB": "KXTRUMPMENTION"}
# frozen blend coefficients as stated in the claim
B0, BM, BB = -0.09174520607273995, 0.9701165898132638, 0.385610892255977
CALLS = 0


def ts(s: str | None) -> int | None:
    if not s:
        return None
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fnum(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def wkey(m: dict) -> str:
    w = (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""
    alts = sorted(re.sub(r"[^a-z0-9]", "", a.lower()) for a in w.split("/"))
    return "/".join(a for a in alts if a)


def logit(p: float) -> float:
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


# ---------------------------------------------------------------- load raw responses
def load():
    mk, cd = {}, defaultdict(dict)
    for f in sorted(os.listdir(SRC)):
        j = json.load(open(SRC / f))
        if "market_candlesticks" in j:                       # /series/../events/../candlesticks
            for tk, cs in zip(j["market_tickers"], j["market_candlesticks"]):
                for c in cs or []:
                    cd[tk][c["end_period_ts"]] = c
        elif "markets" in j and "cursor" not in j:           # /markets/candlesticks (batched)
            for m in j["markets"]:
                for c in m["candlesticks"]:
                    cd[m["market_ticker"]][c["end_period_ts"]] = c
        elif "markets" in j:                                 # /markets or /historical/markets listings
            for m in j["markets"]:
                if m.get("result") in ("yes", "no"):
                    mk[m["ticker"]] = m
    candles = {}
    for tk, d in cd.items():
        rows = []
        for e in sorted(d):
            c = d[e]
            rows.append((e, fnum((c.get("yes_ask") or {}).get("close_dollars")), fnum((c.get("yes_bid") or {}).get("close_dollars")),
                         fnum((c.get("price") or {}).get("high_dollars")), fnum(c.get("volume_fp")) or 0.0))
        candles[tk] = rows
    return mk, candles


# ---------------------------------------------------------------- base rates
class BaseRates:
    def __init__(self, mk: dict):
        self.rows = defaultdict(list)       # group -> [(close_ts, event, wkey, yes)]
        for m in mk.values():
            s = m["ticker"].split("-")[0]
            if s in EXCLUDE:
                continue
            self.rows[GROUP.get(s, s)].append((ts(m["close_time"]), m["event_ticker"], wkey(m), m["result"] == "yes"))
        for g in self.rows:
            self.rows[g].sort()

    def p(self, group: str, word: str, event: str, H: int) -> tuple[float, int]:
        K = N = k = n = 0
        for c, e, w, y in self.rows.get(group, ()):
            if c > H - 3600:
                break
            if e == event:
                continue
            N += 1; K += y
            if w == word:
                n += 1; k += y
        p0 = (K + 1) / (N + 2)
        return (k + 3 * p0) / (n + 3), n


# ---------------------------------------------------------------- orders
def quote_at(rows, H, carry):
    best = None
    for r in rows:
        if r[0] <= H:
            best = r
        else:
            break
    if best is None or (not carry and best[0] != H) or (carry and H - best[0] > carry):
        return None
    return best


def build_orders(mk, candles, carry=0, first_any=False, entry_shift=0):
    """One order per market at its first eligible hour. carry: seconds a candle may be carried forward to H.
    first_any: evaluate only the first hour >= open+1h (skip the market if conditions fail there)."""
    events = defaultdict(list)
    for tk, m in mk.items():
        events[m["event_ticker"]].append(m)
    br = BaseRates(mk)
    orders = []
    for ev, ms in events.items():
        s0 = ms[0]["ticker"].split("-")[0]
        if s0 in EXCLUDE or not any(m["ticker"] in candles for m in ms):
            continue
        first_close = min(ts(m["close_time"]) for m in ms)
        last_close = max(ts(m["close_time"]) for m in ms)
        for m in ms:
            rows = candles.get(m["ticker"])
            if not rows:
                continue
            op, cl = ts(m["open_time"]), ts(m["close_time"])
            H = ((op + 3600 + 3599) // 3600) * 3600
            got = None; k_el = 0
            while H < first_close and H < cl:
                q = quote_at(rows, H, carry)
                ok = q is not None and q[1] is not None and q[2] is not None and q[2] > 0 and q[1] < 1 and q[1] - q[2] >= 0.02 - 1e-9
                if ok:
                    if k_el == entry_shift:
                        got = (H, q); break
                    k_el += 1
                elif first_any:
                    break
                H += 3600
            if not got:
                continue
            H, q = got
            ask, bid = q[1], q[2]
            s = round(ask - 0.01, 4)
            group = GROUP.get(s0, s0)
            pbr, nbr = br.p(group, wkey(m), ev, H)
            mid = (ask + bid) / 2
            pm = sig(B0 + BM * logit(mid) + BB * logit(pbr))
            ev_no = ((1 - pm) - (1 - s)) / (1 - s)
            # fills: candles ending after H up to the end of the hour containing the event's first close
            stop = min(first_close, cl)
            stop_h = ((stop + 3599) // 3600) * 3600
            fill = None; through = 0.0; pre = None; last_q = None
            for r in rows:
                if r[0] <= H or r[0] > stop_h:
                    continue
                if r[3] is not None and r[3] >= s - 1e-9:
                    last_q = r[0]
                    if fill is None:
                        fill = r
                    through = max(through, r[3] - s)
                    if pre is None and r[0] <= (stop // 3600) * 3600:
                        pre = r
            orders.append({"t": m["ticker"], "e": ev, "series": s0, "H": H, "ask": ask, "bid": bid, "s": s, "px": round(1 - s, 4),
                           "no_won": m["result"] == "no", "pm": pm, "pbr": pbr, "nbr": nbr, "ev_no": ev_no,
                           "first_close": first_close, "close": cl, "ev_last_close": last_close, "ev_first_close": first_close,
                           "fill_ts": fill[0] if fill else None, "fill_vol": fill[4] if fill else None,
                           "fill_through": round(through, 4) if fill else None, "fill_pre_ts": pre[0] if pre else None, "fill_last_ts": last_q,
                           "max_hi_after": max([r[3] for r in rows if H < r[0] <= stop_h and r[3] is not None], default=None)})
    return orders


def split(orders, key="ev_last_close", frac=0.7):
    evc = {}
    for o in orders:
        evc[o["e"]] = o[key]
    evs = sorted(evc, key=lambda e: (evc[e], e))
    cut = int(len(evs) * frac)
    disc = set(evs[:cut])
    return disc, evs, evc


def stats(rows, w=None):
    """rows: dicts with ret, e, won, px, ts. Equal-$ mean per fill, t clustered by event (calib.cell_stats form and
    a cluster-robust form for the pooled mean)."""
    if not rows:
        return {"n": 0}
    rets = [r["ret"] for r in rows]
    n = len(rows)
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    ne = len(em)
    t_evmean = st.mean(em) / (st.pstdev(em) / math.sqrt(ne)) if ne > 2 and st.pstdev(em) > 0 else float("nan")
    mu = st.mean(rets)
    sc = [sum(x - mu for x in v) for v in ev.values()]
    se = math.sqrt(sum(x * x for x in sc) * ne / max(ne - 1, 1)) / n
    rs = sorted(rets, reverse=True)
    srt = sorted(rows, key=lambda r: r["ts"])
    h = len(srt) // 2
    return {"n": n, "events": ne, "win": round(sum(r["won"] for r in rows) / n, 4), "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(mu, 4), "t_cluster_pooled": round(mu / se, 2) if se > 0 else None, "t_calib_evmean": round(t_evmean, 2),
            "ret_wo3": round(st.mean(rs[3:]), 4) if n > 3 else None,
            "half1": round(st.mean(r["ret"] for r in srt[:h]), 4) if h else None, "half2": round(st.mean(r["ret"] for r in srt[h:]), 4) if h else None}


def evaluate(orders, disc, through_min=0.0, pre_only=False, vol_min=0.0, label=""):
    out = {}
    for arm, f in (("C1_maker_no_all", lambda o: True), ("C2_maker_no_br20", lambda o: o["ev_no"] >= 0.20), ("C3_maker_no_br10", lambda o: o["ev_no"] >= 0.10)):
        for part in ("discovery", "validation"):
            rows = []; posted = 0
            for o in orders:
                if (o["e"] in disc) != (part == "discovery") or not f(o):
                    continue
                posted += 1
                ft = o["fill_pre_ts"] if pre_only else o["fill_ts"]
                if ft is None or o["fill_through"] < through_min - 1e-9 or (o["fill_vol"] or 0) < vol_min:
                    continue
                pnl = (1.0 if o["no_won"] else 0.0) - o["px"]
                rows.append({"ret": pnl / o["px"], "e": o["e"], "won": o["no_won"], "px": o["px"], "ts": ft, "vol": o["fill_vol"]})
            s = stats(rows); s["posted"] = posted
            if rows:
                s["median_fill_hour_volume"] = st.median(r["vol"] for r in rows)
            out[f"{arm}|{part}"] = s
    return out


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    mk, candles = load()
    print("settled markets", len(mk), "markets with candles", len(candles))
    orders = build_orders(mk, candles)
    disc, evs, evc = split(orders)
    print("events with orders", len(evs), "discovery", len(disc), "cut", datetime.fromtimestamp(evc[evs[len(disc)]], timezone.utc))
    val_ev = [e for e in evs if e not in disc]
    vdays = (evc[val_ev[-1]] - evc[val_ev[0]]) / 86400
    res = {"main": evaluate(orders, disc)}
    # split by the event's first close instead of last close
    d2, evs2, evc2 = split(orders, "ev_first_close")
    res["split_by_first_close"] = evaluate(orders, d2)
    res["through_ge_1c"] = evaluate(orders, disc, through_min=0.01)
    res["through_ge_2c"] = evaluate(orders, disc, through_min=0.02)
    res["through_ge_2c_vol20"] = evaluate(orders, disc, through_min=0.02, vol_min=20)
    res["through_ge_3c_vol50"] = evaluate(orders, disc, through_min=0.03, vol_min=50)
    res["pre_first_close_hour_only"] = evaluate(orders, disc, pre_only=True)
    res["carry_forward_quote_6h"] = evaluate(build_orders(mk, candles, carry=6 * 3600), disc)
    res["first_hour_only"] = evaluate(build_orders(mk, candles, first_any=True), disc)
    res["second_eligible_hour"] = evaluate(build_orders(mk, candles, entry_shift=1), disc)
    res["meta"] = {"events_with_orders": len(evs), "discovery_events": len(disc), "validation_events": len(val_ev),
                   "validation_span_days": round(vdays, 2), "orders": len(orders)}
    json.dump(res, open(OUT / "backtest.json", "w"), indent=1)
    with open(OUT / "orders.jsonl", "w") as f:
        for o in orders:
            f.write(json.dumps({**o, "part": "disc" if o["e"] in disc else "val"}) + "\n")
    for k in ("main", "through_ge_1c", "through_ge_2c", "pre_first_close_hour_only", "split_by_first_close"):
        print("==", k)
        for kk, v in res[k].items():
            print(f"  {kk:34s} n={v.get('n')} ev={v.get('events')} win={v.get('win')} px={v.get('avg_px')} ret={v.get('ret_per_dollar')} "
                  f"t={v.get('t_cluster_pooled')}/{v.get('t_calib_evmean')} wo3={v.get('ret_wo3')} h={v.get('half1')},{v.get('half2')} posted={v.get('posted')}")
    for k in ("carry_forward_quote_6h", "first_hour_only", "second_eligible_hour", "through_ge_2c_vol20", "through_ge_3c_vol50"):
        v = res[k]["C1_maker_no_all|validation"]; v2 = res[k]["C2_maker_no_br20|validation"]
        print(f"== {k}: C1 val n={v.get('n')} ret={v.get('ret_per_dollar')} t={v.get('t_cluster_pooled')} | C2 val n={v2.get('n')} ret={v2.get('ret_per_dollar')} t={v2.get('t_cluster_pooled')}")
    print(res["meta"])


# ---------------------------------------------------------------- live API (budgeted)
def api(path: str) -> str:
    global CALLS
    from lab.us.data_refresh import fetch, K
    CALLS += 1
    txt = fetch(K + path, pace=1.15)
    with open(OUT / "calls.log", "a") as f:
        f.write(f"{int(time.time())}\t{len(txt)}\t{path}\n")
    return txt


def verify():
    """Re-fetch one cached candlestick batch subset and one settled-market listing; compare with the cache."""
    mk, candles = load()
    rnd = random.Random(7)
    tks = rnd.sample(sorted(t for t in candles if candles[t]), 3)
    rep = {}
    for tk in tks:
        rows = candles[tk]
        a, b = rows[0][0] - 3600, rows[-1][0]
        j = json.loads(api(f"/markets/candlesticks?market_tickers={tk}&start_ts={a}&end_ts={b}&period_interval=60") or "{}")
        fresh = {c["end_period_ts"]: c for m in j.get("markets", []) for c in m["candlesticks"]}
        same = diff = 0
        for r in rows:
            c = fresh.get(r[0])
            if not c:
                diff += 1; continue
            fa = fnum((c.get("yes_ask") or {}).get("close_dollars")); fb = fnum((c.get("yes_bid") or {}).get("close_dollars"))
            fh = fnum((c.get("price") or {}).get("high_dollars"))
            if (fa, fb, fh) == (r[1], r[2], r[3]):
                same += 1
            else:
                diff += 1
        rep[tk] = {"cached": len(rows), "fresh": len(fresh), "same": same, "diff": diff}
    ev = mk[tks[0]]["event_ticker"]
    j = json.loads(api(f"/markets?event_ticker={ev}&limit=200") or "{}")
    res_same = sum(1 for m in j.get("markets", []) if m["ticker"] in mk and mk[m["ticker"]]["result"] == m.get("result")
                   and mk[m["ticker"]]["close_time"] == m.get("close_time"))
    rep["event_check"] = {"event": ev, "fresh": len(j.get("markets", [])), "same_result_and_close": res_same}
    json.dump(rep, open(OUT / "verify.json", "w"), indent=1)
    print(json.dumps(rep, indent=1), "calls", CALLS)


def trades_targeted(n_other: int):
    """Census of the validation C1 'marginal' candle fills (highest later print s or s+1c) plus a seeded random sample
    of n_other other validation fills. Window: [H, end of the last candle with a print >= s] for marginal fills (all
    qualifying prints), [H, end of the first such candle] for the others (only the first qualifying print matters)."""
    TR.mkdir(parents=True, exist_ok=True)
    orders = [json.loads(l) for l in open(OUT / "orders.jsonl")]
    val = [o for o in orders if o["part"] == "val" and o["fill_ts"] is not None]
    marg = [o for o in val if o["fill_through"] < 0.015]
    other = [o for o in val if o["fill_through"] >= 0.015]
    pick = [(o, "marginal") for o in marg] + [(o, "other") for o in random.Random(11).sample(other, min(n_other, len(other)))]
    for o, kind in pick:
        fp = TR / f"{o['t']}.json"
        if fp.exists():
            x = json.load(open(fp))
            if x.get("kind"):
                continue
        hi = o["fill_last_ts"] if kind == "marginal" else o["fill_ts"]
        allt, cursor = [], ""
        for _ in range(4):
            txt = api(f"/markets/trades?ticker={o['t']}&min_ts={o['H']}&max_ts={hi}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            j = json.loads(txt or "{}")
            allt += j.get("trades", [])
            cursor = j.get("cursor") or ""
            if not cursor:
                break
        json.dump({"order": o, "kind": kind, "window": [o["H"], hi],
                   "trades": [[t["created_time"], fnum(t.get("yes_price_dollars")), fnum(t.get("count_fp")), t.get("taker_side")] for t in allt],
                   "complete": not cursor}, open(fp, "w"))
        if CALLS >= 175:
            print("budget stop"); break
    print("calls", CALLS, "marginal", len(marg), "other", len(other))


def trades(nsample: int):
    """Trade prints for a random sample of validation C1 orders (seeded), from posting time H to the event's first
    close (+ the rest of that hour), so the exact-time fill model can be compared with the hourly-candle one."""
    TR.mkdir(parents=True, exist_ok=True)
    orders = [json.loads(l) for l in open(OUT / "orders.jsonl")]
    val = [o for o in orders if o["part"] == "val"]
    rnd = random.Random(20261008)
    pick = rnd.sample(val, min(nsample, len(val)))
    for o in pick:
        fp = TR / f"{o['t']}.json"
        if fp.exists():
            continue
        hi = ((min(o["first_close"], o["close"]) + 3599) // 3600) * 3600
        allt, cursor = [], ""
        for _ in range(3):
            txt = api(f"/markets/trades?ticker={o['t']}&min_ts={o['H']}&max_ts={hi}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            j = json.loads(txt or "{}")
            allt += j.get("trades", [])
            cursor = j.get("cursor") or ""
            if not cursor:
                break
        json.dump({"order": o, "trades": [[t["created_time"], fnum(t.get("yes_price_dollars")), fnum(t.get("count_fp")), t.get("taker_side")] for t in allt],
                   "complete": not cursor}, open(fp, "w"))
    print("calls", CALLS)


def tradefill():
    """Exact-time fill audit on the sampled validation orders.
    Model A (candle): the hourly-candle rule used in the backtest.
    Model B (exact): post at H + 120 s; fills from prints with yes_price >= s after posting and strictly before the
    event's first close; filled contracts = min(N, sum of counts), N = floor(5 / (1 - s)).
    Model C (exact, through): as B but only prints with yes_price > s (the level traded through: no queue risk)."""
    files = sorted(TR.glob("*.json"))
    rows = {"A_candle": [], "B_exact_touch": [], "B_sizeweighted": [], "C_exact_through": [], "C_sizeweighted": [], "posted": []}
    detail = []
    for fp in files:
        x = json.load(open(fp)); o = x["order"]
        px = o["px"]; s = o["s"]; N = max(1, math.floor(5 / px))
        pnl = (1.0 if o["no_won"] else 0.0) - px
        base = {"e": o["e"], "won": o["no_won"], "px": px, "ret": pnl / px}
        rows["posted"].append({**base, "ts": o["H"]})
        if o["fill_ts"] is not None:
            rows["A_candle"].append({**base, "ts": o["fill_ts"]})
        post = o["H"] + 120; stop = o["first_close"]
        touch = sum(c for t_, p, c, sd in x["trades"] if p is not None and p >= s - 1e-9 and post <= ts(t_) < stop)
        thru = sum(c for t_, p, c, sd in x["trades"] if p is not None and p > s + 1e-9 and post <= ts(t_) < stop)
        ft = min([ts(t_) for t_, p, c, sd in x["trades"] if p is not None and p >= s - 1e-9 and post <= ts(t_) < stop], default=None)
        if touch > 0:
            fr = min(1.0, touch / N)
            rows["B_exact_touch"].append({**base, "ts": ft})
            rows["B_sizeweighted"].append({**base, "ts": ft, "w": fr})
        if thru > 0:
            rows["C_exact_through"].append({**base, "ts": ft})
            rows["C_sizeweighted"].append({**base, "ts": ft, "w": min(1.0, thru / N)})
        detail.append({"t": o["t"], "s": s, "no_won": o["no_won"], "candle_fill": o["fill_ts"] is not None, "touch_contracts": round(touch, 2),
                       "through_contracts": round(thru, 2), "N": N, "complete": x["complete"]})
    out = {"orders_sampled": len(files)}
    for k, v in rows.items():
        if k.endswith("sizeweighted"):
            W = sum(r["w"] for r in v)
            out[k] = {"fills": len(v), "contract_weight": round(W, 2), "ret_per_dollar": round(sum(r["w"] * r["ret"] for r in v) / W, 4) if W else None}
        else:
            out[k] = stats(v)
    agree = sum(1 for d in detail if d["candle_fill"] == (d["touch_contracts"] > 0))
    out["candle_vs_exact_agree"] = f"{agree}/{len(detail)}"
    out["candle_fill_but_no_exact_print"] = sum(1 for d in detail if d["candle_fill"] and d["touch_contracts"] == 0)
    out["exact_print_but_no_candle_fill"] = sum(1 for d in detail if not d["candle_fill"] and d["touch_contracts"] > 0)
    json.dump({"summary": out, "detail": detail}, open(OUT / "tradefill.json", "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parents[3])
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run()
    elif cmd == "verify":
        verify()
    elif cmd == "trades":
        trades(int(sys.argv[2]) if len(sys.argv) > 2 else 100)
    elif cmd == "targeted":
        trades_targeted(int(sys.argv[2]) if len(sys.argv) > 2 else 45)
    elif cmd == "tradefill":
        tradefill()


def extra():
    """Concentration and selection diagnostics on the candle backtest (orders.jsonl from `run`)."""
    orders = [json.loads(l) for l in open(OUT / "orders.jsonl")]
    out = {}

    def rowsof(sel, fill=True):
        r = []
        for o in sel:
            if fill and o["fill_ts"] is None:
                continue
            pnl = (1.0 if o["no_won"] else 0.0) - o["px"]
            r.append({"ret": pnl / o["px"], "e": o["e"], "won": o["no_won"], "px": o["px"], "ts": o["fill_ts"] or o["H"]})
        return r
    allf = [o for o in orders]
    out["C1_all_parts_filled"] = stats(rowsof(allf))
    out["C1_val_if_every_posted_order_filled"] = stats(rowsof([o for o in orders if o["part"] == "val"], fill=False))
    out["C1_val_unfilled_orders_counterfactual"] = stats(rowsof([o for o in orders if o["part"] == "val" and o["fill_ts"] is None], fill=False))
    # touch class: highest later print exactly s, s+1c, >= s+2c
    for lab_, lo, hi in (("touch_eq_s", -1e-9, 0.005), ("thru_1c", 0.005, 0.015), ("thru_ge_2c", 0.015, 9)):
        sel = [o for o in orders if o["part"] == "val" and o["fill_ts"] is not None and lo <= o["fill_through"] < hi]
        out[f"C1_val_{lab_}"] = stats(rowsof(sel))
    by = defaultdict(list)
    for o in orders:
        if o["part"] == "val":
            by[o["series"]].append(o)
    out["C1_val_by_series"] = {k: stats(rowsof(v)) for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))}
    bands = ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))
    out["C1_val_by_no_price"] = {f"{a}-{b}": stats(rowsof([o for o in orders if o["part"] == "val" and a <= o["px"] < b])) for a, b in bands}
    wk = defaultdict(list)
    for o in orders:
        if o["fill_ts"] is not None:
            wk[datetime.fromtimestamp(o["ev_last_close"], timezone.utc).strftime("%G-W%V")].append(o)
    out["C1_by_week"] = {k: {"n": len(v), "ret": round(st.mean(((1.0 if o["no_won"] else 0.0) - o["px"]) / o["px"] for o in v), 4)} for k, v in sorted(wk.items())}
    # leave-one-series-out on validation
    out["C1_val_drop_trumpmention"] = stats(rowsof([o for o in orders if o["part"] == "val" and o["series"] not in ("KXTRUMPMENTION", "KXTRUMPMENTIONB")]))
    out["C1_val_drop_worldnews"] = stats(rowsof([o for o in orders if o["part"] == "val" and o["series"] != "KXWORLDNEWSMENTION"]))
    sp = [o["ask"] - o["bid"] for o in orders if o["part"] == "val"]
    out["val_decision_spread_median"] = st.median(sp)
    json.dump(out, open(OUT / "extra.json", "w"), indent=1)
    for k, v in out.items():
        if isinstance(v, dict) and "n" in v:
            print(f"{k:45s} n={v['n']} ev={v.get('events')} win={v.get('win')} px={v.get('avg_px')} ret={v.get('ret_per_dollar')} t={v.get('t_cluster_pooled')}")
        elif isinstance(v, dict):
            print(k)
            for kk, vv in v.items():
                print(f"   {kk:28s} {vv if 'events' not in vv else (vv['n'], vv['events'], vv['win'], vv['avg_px'], vv['ret_per_dollar'], vv['t_cluster_pooled'])}")
        else:
            print(k, v)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "extra":
    extra()


def tradefill2():
    """Exact-time fill audit on the targeted sample (census of marginal candle fills + random other fills).
    Exact model: order live from H + 120 s (hourly poll + 2 min) until the event's first close (exact second);
    qualifying prints: yes_price >= s ('touch') or yes_price > s ('through', no queue risk at all);
    filled contracts = min(N, sum of qualifying counts), N = floor(5 / (1 - s)).
    Re-weights the validation C1 (and C2/C3) candle result: other fills keep their sampled exact-fill rate,
    marginal fills enter with their own exact outcome (equal-$ if any contract fills, or size-weighted)."""
    orders = [json.loads(l) for l in open(OUT / "orders.jsonl")]
    val = [o for o in orders if o["part"] == "val" and o["fill_ts"] is not None]
    aud = {}
    for fp in TR.glob("*.json"):
        x = json.load(open(fp))
        if not x.get("kind"):
            continue
        o = x["order"]; s = o["s"]; N = max(1, math.floor(5 / o["px"]))
        post, stop = o["H"] + 120, o["first_close"]
        q = [(ts(t_), p, c, sd) for t_, p, c, sd in x["trades"] if p is not None and post <= ts(t_) < stop]
        touch = sum(c for _, p, c, _ in q if p >= s - 1e-9)
        thru = sum(c for _, p, c, _ in q if p > s + 1e-9)
        early = sum(c for t_, p, c, sd in x["trades"] if p is not None and p >= s - 1e-9 and ts(t_) < post)
        aud[o["t"]] = {"kind": x["kind"], "s": s, "N": N, "touch": touch, "thru": thru, "early_only": touch == 0 and early > 0,
                       "complete": x["complete"], "no_won": o["no_won"], "px": o["px"], "fill_through": o["fill_through"]}
    marg = {t: a for t, a in aud.items() if a["kind"] == "marginal"}
    oth = {t: a for t, a in aud.items() if a["kind"] == "other"}
    oth_fill_rate = st.mean(1.0 if a["touch"] > 0 else 0.0 for a in oth.values()) if oth else 1.0
    oth_frac = st.mean(min(1.0, a["touch"] / a["N"]) for a in oth.values()) if oth else 1.0
    summ = {"marginal_audited": len(marg), "other_audited": len(oth),
            "marginal_exact_touch_filled": sum(a["touch"] > 0 for a in marg.values()),
            "marginal_exact_through_filled": sum(a["thru"] > 0 for a in marg.values()),
            "marginal_only_print_before_posting": sum(a["early_only"] for a in marg.values()),
            "marginal_mean_fill_fraction_touch": round(st.mean(min(1.0, a["touch"] / a["N"]) for a in marg.values()), 3) if marg else None,
            "marginal_mean_fill_fraction_through": round(st.mean(min(1.0, a["thru"] / a["N"]) for a in marg.values()), 3) if marg else None,
            "marginal_win_rate": round(st.mean(a["no_won"] for a in marg.values()), 3) if marg else None,
            "other_exact_fill_rate": round(oth_fill_rate, 3), "other_mean_fill_fraction": round(oth_frac, 3),
            "incomplete_windows": sum(not a["complete"] for a in aud.values())}
    arms = {"C1_maker_no_all": lambda o: True, "C2_maker_no_br20": lambda o: o["ev_no"] >= 0.20, "C3_maker_no_br10": lambda o: o["ev_no"] >= 0.10}
    res = {}
    for arm, f in arms.items():
        for model in ("candle", "exact_touch_any", "exact_touch_sizeweighted", "exact_through_any", "exact_through_sizeweighted"):
            rows = []
            for o in val:
                if not f(o):
                    continue
                ret = ((1.0 if o["no_won"] else 0.0) - o["px"]) / o["px"]
                w = 1.0
                if model != "candle":
                    if o["fill_through"] < 0.015:
                        a = marg.get(o["t"])
                        if a is None:
                            continue
                        c = a["thru"] if "through" in model else a["touch"]
                        w = min(1.0, c / a["N"]) if "sizeweighted" in model else (1.0 if c > 0 else 0.0)
                    else:
                        w = oth_frac if "sizeweighted" in model else oth_fill_rate
                if w <= 0:
                    continue
                rows.append({"ret": ret, "w": w, "e": o["e"], "won": o["no_won"], "px": o["px"], "ts": o["fill_ts"]})
            W = sum(r["w"] for r in rows)
            mu = sum(r["w"] * r["ret"] for r in rows) / W
            ev = defaultdict(float)
            for r in rows:
                ev[r["e"]] += r["w"] * (r["ret"] - mu)
            ne = len(ev); se = math.sqrt(sum(v * v for v in ev.values()) * ne / (ne - 1)) / W
            srt = sorted(rows, key=lambda r: r["ts"]); h = len(srt) // 2
            hm = [sum(r["w"] * r["ret"] for r in p) / sum(r["w"] for r in p) for p in (srt[:h], srt[h:])]
            rs = sorted(rows, key=lambda r: -r["ret"])[3:]
            res[f"{arm}|{model}"] = {"fills": len(rows), "weight": round(W, 1), "events": ne, "ret_per_dollar": round(mu, 4), "t_cluster": round(mu / se, 2),
                                     "ret_wo3": round(sum(r["w"] * r["ret"] for r in rs) / sum(r["w"] for r in rs), 4), "half1": round(hm[0], 4), "half2": round(hm[1], 4)}
    json.dump({"summary": summ, "reweighted_validation": res, "audit": aud}, open(OUT / "tradefill.json", "w"), indent=1)
    print(json.dumps(summ, indent=1))
    for k, v in res.items():
        print(f"{k:45s} {v}")


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "tradefill2":
    tradefill2()
