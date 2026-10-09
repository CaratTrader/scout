"""Adversarial reproduction of r6_new_series_honeymoon (validation only), written from the claim text, not from its code.

Claim (frozen candidates, validation = honeymoon markets of recurring series launched 2026-09-01..10-01):
  C1 FAV[0.55,0.90] at close-4h, spread <= 6c   (buy the side the mid favours at its taker price)
  C2 LSNO[0.85,0.97] at close-4h, spread <= 6c  (buy NO at 1 - yes_bid)
  C3 FAV[0.70,0.90] at close-8h, spread <= 6c
Honeymoon = market opened < 28 d after the series' first market.

Data are rebuilt here from RAW Kalshi API responses cached on disk (r6 and r2_launch_window api_cache: settled
/markets pages and hourly /markets/candlesticks batches), plus the on-disk quake minute candles
(quake_entertain). Nothing is read from the claimant's processed files (val_*.jsonl) except for a final
trade-by-trade diff.

Fill conventions examined (all reported):
  A  "claim convention": hourly data -> decide and fill on the first hourly candle ending >= t (the quote 1 min
     after t for the usual :59 closes); minute data (quake) -> decide on quote <= t (<= 30 min old), fill on quote
     <= t+60 s; anchor = the market's (actual) close time.
  B  strict hourly: decide on the last hourly candle ending <= t (no future data at all), fill on the first candle
     ending >= t+60 s (<= 60 min after t).
  L  leak-free anchor for early-closing markets (KXBIGGESTQUAKE closes the moment a qualifying quake prints):
     anchor = scheduled end (expected_expiration_time), and no trade if the market had already closed by the fill.
Fee: 0.07 p (1-p) per contract, rounded up to the cent per 10-contract order.
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_new_series_honeymoon_repro [--spot N]"""
from __future__ import annotations
import datetime as dt, hashlib, json, math, os, random, statistics as st, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
S = ROOT / "data/kalshi_lab/strategies"
R6 = S / "r6_new_series_honeymoon"; R2 = S / "r2_launch_window"; QK = S / "quake_entertain"
OUT = R6 / "repro"
KB = "https://api.elections.kalshi.com/trade-api/v2"

VAL_SERIES = ["KXAAAGASDNC", "KXAAAGASDOH", "KXAAAGASDGA", "KXAAAGASDPA", "KXAAAGASDWA",       # launched 09-01
              "KXAAAGASDMI", "KXAAAGASDMA", "KXAAAGASDTN", "KXAAAGASDVA", "KXAAAGASDAZ",       # 09-09
              "KXAAAGASDSC", "KXTRUMPAPPROVE", "KXBIGGESTQUAKE", "KXVHGCB", "KXTRUTHSOCIALD"]
MATURE_GAS = ["KXAAAGASDTX", "KXAAAGASDCA", "KXAAAGASDFL", "KXAAAGASDNJ", "KXAAAGASDIL", "KXAAAGASDNY"]   # launched 08-23/25
HONEY_D = 28
CANDS = {"C1": ("FAV", 0.55, 0.90, 4), "C2": ("LSNO", 0.85, 0.97, 4), "C3": ("FAV", 0.70, 0.90, 8)}
MAX_SPREAD = 0.06


def fam(series: str) -> str:
    return "GAS_STATE" if series.startswith("KXAAAGASD") and len(series) == 11 else \
        {"KXBIGGESTQUAKE": "QUAKE", "KXTRUMPAPPROVE": "TRUMPAPPROVE", "KXVHGCB": "VHGCB", "KXTRUTHSOCIALD": "TRUTHSOCIAL"}.get(series, series)


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def fee(p: float) -> float:
    """Per-contract taker fee for a 10-lot, rounded up to the cent per order."""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- raw data
def cache_items():
    """(url, parsed json) for every cached raw API response in the r6 and r2_launch_window caches."""
    for l in open(R6 / "calls.log"):
        u = l.split()[2]; p = R6 / "api_cache" / (hashlib.sha1(u.encode()).hexdigest()[:24] + ".json")
        if p.exists():
            yield u, json.load(open(p))
    for l in open(R2 / "calls.log"):
        u = KB + l.rstrip("\n").split("\t")[2]
        for v in (l.rstrip("\n").split("\t")[2], u):
            p = R2 / "api_cache" / (hashlib.sha1(v.encode()).hexdigest() + ".json")
            if p.exists():
                yield u, json.load(open(p)); break


def load():
    markets: dict[str, dict] = {}; hourly: dict[str, dict[int, tuple]] = defaultdict(dict); conflicts = 0; n_resp = 0
    for u, d in cache_items():
        n_resp += 1
        if "/markets?" in u and isinstance(d, dict) and "markets" in d:
            for m in d["markets"]:
                if m.get("status") not in ("finalized", "settled") or m.get("result") not in ("yes", "no"):
                    continue
                markets[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0],
                                        "open": ts(m["open_time"]), "close": ts(m["close_time"]),
                                        "exp": ts(m.get("expected_expiration_time")), "result": m["result"]}
        elif "/markets/candlesticks?" in u and isinstance(d, dict) and "markets" in d and "period_interval=60" in u:
            for x in d["markets"]:
                for c in x.get("candlesticks", []):
                    a, b = f(c.get("yes_ask", {}).get("close_dollars")), f(c.get("yes_bid", {}).get("close_dollars"))
                    if a is None or b is None:
                        continue
                    old = hourly[x["market_ticker"]].get(c["end_period_ts"])
                    if old and old != (a, b):
                        conflicts += 1
                    hourly[x["market_ticker"]][c["end_period_ts"]] = (a, b)
    minute: dict[str, list] = {}
    for l in open(QK / "quake_markets.jsonl"):
        m = json.loads(l)
        if m["result"] in ("yes", "no"):
            markets[m["t"]] = {k: m[k] for k in ("t", "e", "series", "open", "close", "exp", "result")}
    for l in open(QK / "quake_candles.jsonl"):
        x = json.loads(l); minute[x["t"]] = [(r[0], r[1], r[2]) for r in x["c"] if r[1] is not None and r[2] is not None]
    hourly = {k: sorted(v.items()) for k, v in hourly.items()}
    return markets, hourly, minute, {"raw_responses": n_resp, "hourly_conflicts": conflicts}


# ---------------------------------------------------------------- quotes
def q_hour_first_ge(c, t, max_after=3600):
    for e, q in c:
        if e >= t:
            return (e, *q) if e - t <= max_after else None
    return None


def q_hour_last_le(c, t, max_age=3600):
    best = None
    for e, q in c:
        if e <= t:
            best = (e, *q)
        else:
            break
    return best if best and t - best[0] <= max_age else None


def q_min_le(c, t, max_age=1800):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    return best if best and t - best[0] <= max_age else None


def decide(kind, lo, hi, dq, fq):
    """dq: decision quote (ts, ask, bid); fq: fill quote. Returns (side, px, spread) or None."""
    _, a, b = dq; spread = a - b
    if spread > MAX_SPREAD + 1e-9 or a <= 0 or b < 0 or a > 1:
        return None
    _, fa, fb = fq
    if kind == "FAV":
        mid = (a + b) / 2
        if mid > 0.5:
            side, px = "YES", fa
        elif mid < 0.5:
            side, px = "NO", round(1 - fb, 4)
        else:
            return None
    else:
        side, px = "NO", round(1 - fb, 4)
    if not (lo - 1e-9 <= px <= hi + 1e-9) or not (0 < px < 1):
        return None
    return side, px, round(spread, 4)


def trades(markets, hourly, minute, series_list, conv: str, honey: bool | None):
    launch = {}
    for m in markets.values():
        launch[m["series"]] = min(launch.get(m["series"], 1e18), m["open"])
    out = []
    for m in markets.values():
        s = m["series"]
        if s not in series_list:
            continue
        age = (m["open"] - launch[s]) / 86400
        is_h = age < HONEY_D
        if honey is not None and is_h != honey:
            continue
        for cid, (kind, lo, hi, tau) in CANDS.items():
            early = m["exp"] is not None and m["close"] < m["exp"] - 120 and s == "KXBIGGESTQUAKE"
            anchor = m["exp"] if (conv == "L" and s == "KXBIGGESTQUAKE") else m["close"]
            t = anchor - tau * 3600
            if t < m["open"]:
                continue
            if conv == "L" and t + 60 >= m["close"]:
                continue        # already closed (quake printed) before we could trade
            if m["t"] in minute:
                c = minute[m["t"]]; dq = q_min_le(c, t); fq = q_min_le(c, t + 60)
            elif m["t"] in hourly:
                c = hourly[m["t"]]
                if conv in ("A", "L"):
                    dq = fq = q_hour_first_ge(c, t)
                else:
                    dq = q_hour_last_le(c, t); fq = q_hour_first_ge(c, t + 60)
            else:
                continue
            if not dq or not fq or fq[0] >= m["close"] + 60:
                continue
            r = decide(kind, lo, hi, dq, fq)
            if not r:
                continue
            side, px, spread = r
            won = (m["result"] == "yes") == (side == "YES")
            out.append({"cid": cid, "t": m["t"], "e": m["e"], "series": s, "fam": fam(s), "close": m["close"], "age_d": round(age, 2),
                        "honey": is_h, "early_closed": early,
                        "day": dt.datetime.fromtimestamp(m["close"] - 4 * 3600, dt.timezone.utc).strftime("%Y-%m-%d"),
                        "side": side, "px": px, "spread": spread, "won": won, "ret": ((1.0 if won else 0.0) - px - fee(px)) / px})
    return out


# ---------------------------------------------------------------- stats
def binom_cdf(k, n, p):
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def beta_lb(k, n, alpha=0.05):
    """One-sided exact (Clopper-Pearson) lower bound on p."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if 1 - binom_cdf(k - 1, n, mid) < alpha:    # P(X >= k | mid) < alpha -> mid too small
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clustered_t(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[r[key] if isinstance(key, str) else key(r)].append(r["ret"])
    em = [st.mean(v) for v in g.values()]
    if len(em) < 3:
        return float("nan"), len(em)
    sd = st.stdev(em)
    return (st.mean(em) / (sd / math.sqrt(len(em))) if sd > 0 else float("nan")), len(em)


def stats(rows):
    if not rows:
        return {"n": 0}
    rows = sorted(rows, key=lambda r: r["close"])
    n = len(rows); rets = [r["ret"] for r in rows]
    t_e, ne = clustered_t(rows, "e")
    t_fd, nfd = clustered_t(rows, lambda r: r["fam"] + "|" + r["day"])
    t_day, nday = clustered_t(rows, "day")
    rs = sorted(rets, reverse=True)
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r)
    ev_mean = sorted(((st.mean(x["ret"] for x in v), k) for k, v in ev.items()), reverse=True)
    drop = {k for _, k in ev_mean[:3]}
    wo3ev = [r["ret"] for r in rows if r["e"] not in drop]
    mid = rows[n // 2]["close"]
    h1 = [r["ret"] for r in rows if r["close"] < mid]; h2 = [r["ret"] for r in rows if r["close"] >= mid]
    win = sum(r["won"] for r in rows) / n; px = st.mean(r["px"] for r in rows)
    k_ev = round(win * ne); lb = beta_lb(k_ev, ne)
    fee_avg = st.mean(fee(r["px"]) for r in rows)
    days = len({r["day"] for r in rows})
    return {"n": n, "events": ne, "fd_clusters": nfd, "day_clusters": nday, "win": round(win, 4), "avg_px": round(px, 4),
            "ret": round(st.mean(rets), 4), "t_event": round(t_e, 2), "t_famdate": round(t_fd, 2), "t_day": round(t_day, 2),
            "ret_wo3": round(st.mean(rs[3:]), 4) if n > 3 else None, "ret_wo3ev": round(st.mean(wo3ev), 4) if wo3ev else None,
            "beta_lb_ret": round((lb - px - fee_avg) / px, 4), "half1": round(st.mean(h1), 4) if h1 else None,
            "half2": round(st.mean(h2), 4) if h2 else None, "days": days, "trades_per_day": round(n / max(days, 1), 2),
            "med_spread": st.median(r["spread"] for r in rows)}


def by(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[r[key]].append(r)
    return {k: {kk: v for kk, v in stats(x).items() if kk in ("n", "events", "avg_px", "ret", "t_event", "win")} for k, x in sorted(g.items())}


# ---------------------------------------------------------------- spot check against the live API
def spot_check(markets, hourly, n_days=3, seed=7):
    """Re-fetch hourly candles (one batch per close-day, <= 12 tickers) and market metadata for a random sample of
    cached honeymoon validation tickers, and compare with the cache. Also snapshot live books of open honeymoon gas
    markets for capacity. Every Kalshi call goes through lab.us.data_refresh.fetch(pace=1.15)."""
    from lab.us.data_refresh import fetch
    rnd = random.Random(seed)
    byday = defaultdict(list)
    for t, m in markets.items():
        if m["series"] in VAL_SERIES and t in hourly:
            byday[m["close"]].append(t)
    days = rnd.sample(sorted(byday), n_days)
    calls = 0; cmp_ = 0; diff = 0; meta_cmp = 0; meta_diff = 0; detail = []; sample = []
    for dclose in days:
        g = rnd.sample(byday[dclose], min(12, len(byday[dclose]))); sample += g
        lo = dclose - 14 * 3600; hi = dclose + 120
        txt = fetch(f"{KB}/markets/candlesticks?market_tickers={','.join(g)}&start_ts={lo}&end_ts={hi}&period_interval=60", pace=1.15); calls += 1
        for x in json.loads(txt or "{}").get("markets", []):
            mine = dict(hourly.get(x["market_ticker"], []))
            for c in x.get("candlesticks", []):
                e = c["end_period_ts"]
                if e in mine:
                    a, b = f(c["yes_ask"].get("close_dollars")), f(c["yes_bid"].get("close_dollars")); cmp_ += 1
                    if (a, b) != mine[e]:
                        diff += 1; detail.append([x["market_ticker"], e, [a, b], list(mine[e])])
    for t in rnd.sample(sample, 6):
        txt = fetch(f"{KB}/markets/{t}", pace=1.15); calls += 1
        m = json.loads(txt or "{}").get("market", {}); meta_cmp += 1
        if m.get("result") != markets[t]["result"] or ts(m.get("close_time")) != markets[t]["close"]:
            meta_diff += 1; detail.append([t, "result/close", m.get("result"), m.get("close_time"), markets[t]["result"]])
    books = []
    txt = fetch(f"{KB}/markets?series_ticker=KXAAAGASDSC&status=open&limit=200", pace=1.15); calls += 1
    op = json.loads(txt or "{}").get("markets", [])
    op = [m for m in op if m.get("yes_bid_dollars") and m.get("yes_ask_dollars")]
    # markets whose mid favours one side at 0.55-0.97 (the signal zone of C1-C3)
    zone = [m for m in op if 0.55 <= max((float(m["yes_bid_dollars"]) + float(m["yes_ask_dollars"])) / 2,
                                           1 - (float(m["yes_bid_dollars"]) + float(m["yes_ask_dollars"])) / 2) <= 0.97]
    for m in rnd.sample(zone, min(3, len(zone))):
        txt = fetch(f"{KB}/markets/{m['ticker']}/orderbook", pace=1.15); calls += 1
        ob = json.loads(txt or "{}").get("orderbook_fp") or json.loads(txt or "{}").get("orderbook") or {}
        books.append({"ticker": m["ticker"], "yes_bid": m["yes_bid_dollars"], "yes_ask": m["yes_ask_dollars"], "book": ob})
    return {"calls": calls, "days": [dt.datetime.fromtimestamp(d, dt.timezone.utc).isoformat() for d in days], "tickers": sample,
            "candles_compared": cmp_, "candle_diffs": diff, "markets_compared": meta_cmp, "market_meta_diffs": meta_diff,
            "detail": detail[:20], "books": books}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    markets, hourly, minute, info = load()
    res = {"data": info, "conventions": {}, "frozen_candidates": {k: f"{v[0]}[{v[1]:.2f},{v[2]:.2f}] close-{v[3]}h spread<=6c" for k, v in CANDS.items()}}
    launch = defaultdict(lambda: 1e18)
    for m in markets.values():
        launch[m["series"]] = min(launch[m["series"]], m["open"])
    res["launch"] = {s: dt.datetime.fromtimestamp(launch[s], dt.timezone.utc).isoformat() for s in VAL_SERIES + MATURE_GAS if s in launch}
    res["markets_per_series"] = {s: sum(1 for m in markets.values() if m["series"] == s) for s in VAL_SERIES + MATURE_GAS}
    res["hourly_cov_honey"] = {}
    for s in VAL_SERIES:
        hm = [m for m in markets.values() if m["series"] == s and (m["open"] - launch[s]) / 86400 < HONEY_D]
        res["hourly_cov_honey"][s] = [len(hm), sum(1 for m in hm if m["t"] in hourly or m["t"] in minute)]
    all_rows = {}
    for conv in ("A", "B", "L"):
        rows = trades(markets, hourly, minute, VAL_SERIES, conv, True)
        all_rows[conv] = rows
        cres = {}
        for cid in CANDS:
            x = [r for r in rows if r["cid"] == cid]
            cres[cid] = {**stats(x), "by_family": by(x, "fam"),
                         "ex_gas": stats([r for r in x if r["fam"] != "GAS_STATE"]),
                         "gas_only": stats([r for r in x if r["fam"] == "GAS_STATE"]),
                         "early_closed_quake_trades": sum(r["early_closed"] for r in x)}
        res["conventions"][conv] = cres
    # mature comparison on the same dates (gas states launched 08-23/25, markets aged >= 28 d; plus the H states after day 28)
    vdays = {r["day"] for r in all_rows["A"]}
    mrows = [r for r in trades(markets, hourly, minute, MATURE_GAS + VAL_SERIES, "A", False) if r["day"] in vdays and r["fam"] == "GAS_STATE"]
    res["mature_gas_same_dates"] = {cid: stats([r for r in mrows if r["cid"] == cid]) for cid in CANDS}
    mrowsB = [r for r in trades(markets, hourly, minute, MATURE_GAS + VAL_SERIES, "B", False) if r["day"] in vdays and r["fam"] == "GAS_STATE"]
    res["mature_gas_same_dates_convB"] = {cid: stats([r for r in mrowsB if r["cid"] == cid]) for cid in CANDS}
    res["gas_honey_by_state_convA"] = {cid: by([r for r in all_rows["A"] if r["cid"] == cid and r["fam"] == "GAS_STATE"], "series") for cid in CANDS}
    nat = []   # mature national gas series on disk (1-minute candles): same rules, same validation dates
    try:
        meta = {m["t"]: m for m in map(json.loads, open(ROOT / "data/kalshi_lab/markets/KXAAAGASD.jsonl"))}
        for l in open(ROOT / "data/kalshi_lab/candles/KXAAAGASD.jsonl"):
            x = json.loads(l); m = meta.get(x["t"])
            if not m or m["result"] not in ("yes", "no"):
                continue
            c = [(r[0], r[1], r[2]) for r in x["c"] if r[1] is not None and r[2] is not None]
            for cid, (kind, lo, hi, tau) in CANDS.items():
                t = m["close"] - tau * 3600; dq = q_min_le(c, t); fq = q_min_le(c, t + 60)
                if not dq or not fq:
                    continue
                r = decide(kind, lo, hi, dq, fq)
                if not r:
                    continue
                won = (m["result"] == "yes") == (r[0] == "YES"); day = dt.datetime.fromtimestamp(m["close"] - 4 * 3600, dt.timezone.utc).strftime("%Y-%m-%d")
                nat.append({"cid": cid, "e": m["e"], "fam": "NATGAS", "day": day, "close": m["close"], "px": r[1], "spread": r[2], "won": won,
                            "ret": ((1.0 if won else 0.0) - r[1] - fee(r[1])) / r[1]})
    except FileNotFoundError:
        pass
    res["national_KXAAAGASD_val_dates"] = {cid: stats([r for r in nat if r["cid"] == cid and r["day"] in vdays]) for cid in CANDS}
    res["national_KXAAAGASD_all_dates"] = {cid: stats([r for r in nat if r["cid"] == cid]) for cid in CANDS}
    # diff against the claimant's trade list
    claim = defaultdict(set)
    for l in open(R6 / "val_trades.jsonl"):
        x = json.loads(l)
        claim[x["cell"]].add((x["t"], x["side"], round(x["px"], 4)))
    cellname = {"C1": "FAV[0.55,0.90]|tau4|s<=6c", "C2": "LSNO[0.85,0.97]|tau4|s<=6c", "C3": "FAV[0.70,0.90]|tau8|s<=6c"}
    res["trade_diff_vs_claim_convA"] = {}
    for cid in CANDS:
        mine = {(r["t"], r["side"], round(r["px"], 4)) for r in all_rows["A"] if r["cid"] == cid}
        theirs = claim[cellname[cid]]
        mt = {x[0] for x in mine}; tt = {x[0] for x in theirs}
        res["trade_diff_vs_claim_convA"][cid] = {"mine": len(mine), "theirs": len(theirs), "same_ticker": len(mt & tt),
                                                 "identical_side_px": len(mine & theirs), "only_mine": sorted(mt - tt)[:15],
                                                 "only_theirs": sorted(tt - mt)[:15], "n_only_mine": len(mt - tt), "n_only_theirs": len(tt - mt)}
    if "--spot" in sys.argv:
        res["spot_check"] = spot_check(markets, hourly, int(sys.argv[sys.argv.index("--spot") + 1]))
    with open(OUT / "repro_trades.jsonl", "w") as fh:
        for conv, rows in all_rows.items():
            for r in rows:
                fh.write(json.dumps({"conv": conv, **r}) + "\n")
    (OUT / "repro.json").write_text(json.dumps(res, indent=1, default=str))
    for conv in ("A", "B", "L"):
        print(f"== convention {conv}")
        for cid, v in res["conventions"][conv].items():
            print(f"  {cid} n={v['n']} ev={v['events']} win={v['win']} px={v['avg_px']} ret={v['ret']:+.4f} t_ev={v['t_event']} "
                  f"t_fd={v['t_famdate']} t_day={v['t_day']} wo3={v['ret_wo3']} wo3ev={v['ret_wo3ev']} lb={v['beta_lb_ret']} "
                  f"h={v['half1']}/{v['half2']} tpd={v['trades_per_day']}")
            print("     ", {k: (x['n'], x['ret']) for k, x in v["by_family"].items()}, "| ex-gas", v["ex_gas"].get("n"), v["ex_gas"].get("ret"))
    for key in ("mature_gas_same_dates", "mature_gas_same_dates_convB", "national_KXAAAGASD_val_dates", "national_KXAAAGASD_all_dates"):
        print(key, {k: (v.get("n"), v.get("ret"), v.get("t_event"), v.get("t_day")) for k, v in res[key].items()})
    for cid in CANDS:
        print("gas by state", cid, {k: (v["n"], v["ret"]) for k, v in res["gas_honey_by_state_convA"][cid].items()})
    print("diff", json.dumps(res["trade_diff_vs_claim_convA"])[:1500])
    print("launch", res["launch"]); print("coverage", res["hourly_cov_honey"]); print(info)
    if "spot_check" in res:
        print("spot", {k: v for k, v in res["spot_check"].items() if k != "tickers"})


if __name__ == "__main__":
    main()
