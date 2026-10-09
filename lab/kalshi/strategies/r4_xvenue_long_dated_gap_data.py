"""Data for r4_xvenue_long_dated_gap (run after the mapping is frozen).

plan   : price-blind selection of the pairs to price. Per Kalshi event: every pair whose Kalshi hourly candles are
         already cached (r3 researchers' caches, any window covering ours), then pairs in seeded-random order until
         the event has CAP pairs. Live-tier events (closed after the 2026-08-08 archive cutoff) take all pairs, since
         one event-candlesticks call returns every market. Written to fetch_plan.json and frozen with the mapping.
kalshi : hourly candles. Archive: one /historical/markets/{t}/candlesticks call per market over
         [w0 - 2 h, min(w1 + 1 d, close) + 1 h]. Live: one /series/{s}/events/{e}/candlesticks call per event.
poly   : Polymarket CLOB prices-history, hourly (fidelity=60) in <= 10-day startTs/endTs chunks, same window.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_xvenue_long_dated_gap_data [plan|kalshi|poly]"""
from __future__ import annotations
import json, random, re, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_api import kget, cached, used, OUT, R3
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_poly import hourly
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_map import MAP, ARCHIVE_CUT, DAY

CAP = 3
SEED = 20261008
PLAN = OUT / "fetch_plan.json"
KC = OUT / "kalshi_candles.jsonl"
PH = OUT / "poly_hourly.jsonl"
H = 3600


def r3_coverage() -> dict[str, list[tuple[int, int, str]]]:
    cov = defaultdict(list)
    for f in (R3 / "calls.log", R3 / "repro/calls.log"):
        if not f.exists():
            continue
        for line in f.open():
            p = line.rstrip("\n").split("\t")
            if len(p) == 3 and "candlesticks" in p[2] and int(p[1]) > 100:
                m = re.search(r"/markets/([^/]+)/candlesticks\?start_ts=(\d+)&end_ts=(\d+)", p[2]) or \
                    re.search(r"market_tickers=([^&]+)&start_ts=(\d+)&end_ts=(\d+)", p[2])
                if m:
                    cov[m.group(1)].append((int(m.group(2)), int(m.group(3)), p[2]))
    return cov


def plan() -> dict:
    doc = json.loads(MAP.read_text())
    P = [r for r in doc["pairs"] if r["window_ok"]]
    cov = r3_coverage()
    byev = defaultdict(list)
    for r in P:
        byev[r["e"]].append(r)
    rng = random.Random(SEED)
    sel, live = [], {}
    for e in sorted(byev):
        rs = sorted(byev[e], key=lambda r: r["k"])
        if any((r["k_close"] or 0) >= ARCHIVE_CUT for r in rs) and not all(r["k"] in cov for r in rs):
            live[e] = {"series": rs[0]["k"].split("-")[0], "t0": min(r["w0"] for r in rs) - 2 * H,
                       "t1": max(min(r["w1"] + DAY, r["k_close"]) for r in rs) + H, "markets": [r["k"] for r in rs]}
            sel += [dict(r, src="live_event") for r in rs]
            continue
        c = [r for r in rs if r["k"] in cov]
        u = [r for r in rs if r["k"] not in cov]
        rng.shuffle(u)
        sel += [dict(r, src="r3_cache") for r in c] + [dict(r, src="fetch") for r in u[:max(0, CAP - len(c))]]
    out = {"cap": CAP, "seed": SEED, "n_pairs": len(sel), "n_events": len({r["e"] for r in sel}),
           "n_fetch": sum(r["src"] == "fetch" for r in sel), "live_events": live, "pairs": [r["k"] for r in sel],
           "src": {r["k"]: r["src"] for r in sel}}
    PLAN.write_text(json.dumps(out, indent=1))
    print({k: v for k, v in out.items() if k not in ("pairs", "src", "live_events")}, "live events", len(live))
    return out


def _parse(lst: list[dict]) -> list[list]:
    def px(side, key="close"):
        if not side:
            return None
        v = side.get(key + "_dollars", side.get(key))
        if v is None:
            return None
        v = float(v)
        return v / 100 if v > 1.0001 else v
    out = []
    for c in lst or []:
        a, b = px(c.get("yes_ask")), px(c.get("yes_bid"))
        if a is None or b is None:
            continue
        vol = float(c.get("volume_fp") or c.get("volume") or 0)
        oi = float(c.get("open_interest_fp") or c.get("open_interest") or 0)
        out.append([int(c["end_period_ts"]), a, b, vol, oi])
    return sorted(out)


def kalshi(budget_stop: int = 180) -> None:
    pl = json.loads(PLAN.read_text())
    M = {r["k"]: r for r in json.loads(MAP.read_text())["pairs"]}
    cov = r3_coverage()
    have = {}
    if KC.exists():
        for line in KC.open():
            x = json.loads(line); have[x["t"]] = x
    # live events first (one call each)
    for e, ev in pl["live_events"].items():
        if all(t in have for t in ev["markets"]):
            continue
        d = kget(f"/series/{ev['series']}/events/{e}/candlesticks?start_ts={ev['t0']}&end_ts={ev['t1']}&period_interval=60")
        tick = (d or {}).get("market_tickers") or []
        for t, lst in zip(tick, (d or {}).get("market_candlesticks") or []):
            if t in ev["markets"]:
                have[t] = {"t": t, "src": "live_event", "c": _parse(lst)}
        print("live", e, {t: len(have.get(t, {}).get("c", [])) for t in ev["markets"]}, "used", used(), flush=True)
    for t in pl["pairs"]:
        if t in have:
            continue
        r = M[t]
        if pl["src"][t] == "r3_cache":
            best = sorted(cov[t], key=lambda x: -(min(x[1], r["w1"]) - max(x[0], r["w0"])))[0]
            d = cached(best[2])
            lst = (d or {}).get("candlesticks") if "candlesticks" in (d or {}) else \
                next((x["candlesticks"] for x in (d or {}).get("markets", []) if x.get("market_ticker", t) == t), [])
            have[t] = {"t": t, "src": "r3_cache", "c": _parse(lst), "range": best[:2]}
            continue
        if pl["src"][t] != "fetch":
            continue
        if used() >= budget_stop:
            print("budget stop at", used()); break
        a = r["w0"] - 2 * H
        b = min(r["w1"] + DAY, r["k_close"]) + H
        d = kget(f"/historical/markets/{t}/candlesticks?start_ts={a}&end_ts={b}&period_interval=60")
        have[t] = {"t": t, "src": "fetch", "c": _parse((d or {}).get("candlesticks") or []), "range": [a, b]}
        print(t, len(have[t]["c"]), "used", used(), flush=True)
    KC.write_text("".join(json.dumps(x) + "\n" for x in have.values()))
    print("kalshi candle files", len(have), "nonempty", sum(1 for x in have.values() if x["c"]), "calls used", used())


def poly() -> None:
    pl = json.loads(PLAN.read_text())
    M = {r["k"]: r for r in json.loads(MAP.read_text())["pairs"]}
    out = {}
    for i, t in enumerate(pl["pairs"]):
        r = M[t]
        a = r["w0"] - DAY
        b = min(r["w1"] + 2 * DAY, max(r["k_close"] or 0, r["w0"] + DAY) + DAY)
        h = hourly(r["p_tok"], a, b)
        out[t] = {"t": t, "p_id": r["p_id"], "h": h}
        if i % 25 == 0:
            print(i, t, len(h), flush=True)
    PH.write_text("".join(json.dumps(x) + "\n" for x in out.values()))
    print("poly series", len(out), "nonempty", sum(1 for x in out.values() if x["h"]))


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "plan"
    {"plan": plan, "kalshi": kalshi, "poly": poly}[w]()
