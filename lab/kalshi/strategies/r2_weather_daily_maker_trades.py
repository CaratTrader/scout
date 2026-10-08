"""r2_weather_daily_maker: trade-print calibration of the candle fill model.
For a seeded sample of favourite orders (13:00 / 15:00 local, discovery 2026-08-08..08-19 plus a few validation-period
markets for coverage of the endpoint), fetch every trade print of the market in the order's 120-minute life via
/markets/trades (cached, counted; shared ~1 req/s limit through lab.us.data_refresh.fetch) and compare:
  * candle STRICT fill minute (trade low < price or ask low < price) vs the first print strictly below our price;
  * taker side of the prints at or below our price (a seller hitting bids = 'no' taker);
  * contracts printed at or below our price after placement (capacity for a ~$5 = 6-9 contract order).
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_weather_daily_maker_trades fetch|analyze"""
from __future__ import annotations
import datetime as dt, json, random, sys, zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.r2_weather_daily_maker import load, simulate, OUT
from lab.kalshi.strategies.weather_walkforward_data import SERIES

TD = OUT / "trades"
CALLS = OUT / "kalshi_calls.txt"
MAX_CALLS = 120


def calls() -> int:
    return int(CALLS.read_text()) if CALLS.exists() else 0


def kget(path: str) -> dict:
    from lab.us.data_refresh import fetch, K
    n = calls()
    if n >= MAX_CALLS:
        raise RuntimeError("Kalshi call budget for this helper exhausted")
    CALLS.write_text(str(n + 1))
    return json.loads(fetch(K + path, pace=1.15))


def midnight(stn: str, day: str) -> int:
    tzn = next(v[2] for v in SERIES.values() if v[0] == stn)
    d = dt.date.fromisoformat(day)
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=zoneinfo.ZoneInfo(tzn)).timestamp())


def sample():
    D = load()
    pool = []
    for d in D:
        if not d["prints"]:
            continue
        for T in (13 * 60, 15 * 60):
            r = simulate(d, T, 120)
            if r and r["fill"] != "cross":
                r["midnight"] = midnight(d["stn"], d["day"])
                pool.append(r)
    disc = [r for r in pool if r["part"] == "disc" and r["day"] >= "2026-08-08"]
    val = [r for r in pool if r["part"] == "val"]
    rnd = random.Random(20261008)
    return rnd.sample(disc, min(36, len(disc))) + rnd.sample(val, 8)


def fetch_all():
    TD.mkdir(parents=True, exist_ok=True)
    S = sample()
    for r in S:
        f = TD / f"{r['tk']}_{r['T']}.json"
        if f.exists():
            continue
        t0 = r["midnight"] + (r["T"] + 1) * 60 - 120; t1 = r["midnight"] + (r["T"] + 1 + 120) * 60 + 60
        trades, cursor, pages = [], "", 0
        while True:
            d = kget(f"/markets/trades?ticker={r['tk']}&min_ts={t0}&max_ts={t1}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            trades += d.get("trades") or []; cursor = d.get("cursor") or ""; pages += 1
            if not cursor or pages >= 3:
                break
        f.write_text(json.dumps({"order": {k: v for k, v in r.items() if k != "cl"}, "t0": t0, "t1": t1, "trades": trades}))
        print(r["tk"], r["T"], len(trades), "calls", calls(), flush=True)


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def price_c(tr) -> int:
    for k in ("yes_price_dollars", "yes_price"):
        if tr.get(k) is not None:
            v = float(tr[k])
            return int(round(v * 100)) if k.endswith("dollars") else int(v)
    raise KeyError("price")


def count(tr) -> float:
    for k in ("count_fp", "count"):
        if tr.get(k) is not None:
            return float(tr[k])
    return 0.0


def analyze():
    rows = []
    for f in sorted(TD.glob("*.json")):
        x = json.loads(f.read_text()); o = x["order"]; px = round(o["px"] * 100)
        place = o["midnight"] + (o["T"] + 1) * 60
        tr = sorted((ts(t["created_time"]), price_c(t), count(t), t.get("taker_side")) for t in x["trades"])
        tr = [t for t in tr if place < t[0] <= place + 120 * 60]
        first_strict = next((t for t in tr if t[1] < px), None)
        first_touch_sell = next((t for t in tr if t[1] <= px and t[3] == "no"), None)
        at_or_below = [t for t in tr if t[1] <= px]
        candle_fill = o["fill"] == "maker"
        cmin = o.get("fill_min")
        pmin = None if first_strict is None else (first_strict[0] - o["midnight"] + 59) // 60
        # contracts sold to bids at <= px in the first 30 min after the first strict print (capacity proxy)
        cap = sum(t[2] for t in tr if t[1] <= px and first_strict and first_strict[0] <= t[0] <= first_strict[0] + 1800)
        rows.append({"tk": o["tk"], "T": o["T"], "part": o["part"], "px": px, "spread": o["spread"], "won": o["won"],
                     "n_trades": len(tr), "candle_fill": candle_fill, "candle_min": cmin, "print_strict_min": pmin,
                     "print_touch_sell_min": None if first_touch_sell is None else (first_touch_sell[0] - o["midnight"] + 59) // 60,
                     "sides_at_or_below": {s: sum(t[2] for t in at_or_below if t[3] == s) for s in ("yes", "no")},
                     "cap_30min": round(cap, 1)})
    n = len(rows)
    agree = sum((r["candle_fill"] == (r["print_strict_min"] is not None)) for r in rows)
    cf_only = [r for r in rows if r["candle_fill"] and r["print_strict_min"] is None]
    pf_only = [r for r in rows if not r["candle_fill"] and r["print_strict_min"] is not None]
    lag = [r["candle_min"] - r["print_strict_min"] for r in rows if r["candle_fill"] and r["print_strict_min"] is not None]
    no_side = sum(r["sides_at_or_below"]["no"] for r in rows); yes_side = sum(r["sides_at_or_below"]["yes"] for r in rows)
    caps = sorted(r["cap_30min"] for r in rows if r["print_strict_min"] is not None)
    touch_earlier = [r for r in rows if r["print_touch_sell_min"] is not None and (r["print_strict_min"] is None or r["print_touch_sell_min"] < r["print_strict_min"])]
    res = {"orders": n, "agree_fill_vs_prints": agree, "candle_fill_without_print": len(cf_only), "print_fill_without_candle": len(pf_only),
           "candle_minus_print_fill_minute": {"median": sorted(lag)[len(lag) // 2] if lag else None, "max": max(lag) if lag else None, "min": min(lag) if lag else None},
           "contracts_at_or_below_px_by_taker_side": {"no (seller hit bid)": round(no_side), "yes (buyer lifted ask)": round(yes_side)},
           "orders_with_touch_sell_before_strict": len(touch_earlier),
           "capacity_contracts_30min_after_first_strict_print": {"median": caps[len(caps) // 2] if caps else None, "p25": caps[len(caps) // 4] if caps else None},
           "fill_rate_prints_strict": round(sum(r["print_strict_min"] is not None for r in rows) / max(1, n), 3),
           "rows": rows}
    (OUT / "trades_calibration.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1))
    for r in cf_only + pf_only:
        print("disagree", r["tk"], r["T"], r["px"], r["candle_min"], r["print_strict_min"], r["n_trades"])


if __name__ == "__main__":
    {"fetch": fetch_all, "analyze": analyze}[sys.argv[1]]()
