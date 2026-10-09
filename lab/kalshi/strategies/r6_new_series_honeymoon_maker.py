"""Maker check on exact trade prints, honeymoon books only (part iii of the plan). Design frozen before any print was fetched.

Sample: seeded random (seed 6), one market per event, 25 events from the fresh validation gas states (KXAAAGASD GA PA WA
MI MA TN VA AZ, weeks 1-4) and 25 events from the new weather cities (KXHIGHT/KXLOWT SAN EWR TTN SDF, weeks 1-4).
Eligible at post time (close - 8 h, from candles): two-sided book, spread >= 4c, mid 0.15-0.85.
Orders (both posted at close - 8 h, cancelled at close - 3 h): BUY YES at yes_bid + 1c, and BUY NO at (1 - yes_ask) + 1c
(= offering YES at ask - 1c). Fill: through-only (gate amendment c) = a print strictly through the order price after
post + 120 s and before the cancel (YES buy at b: a print with yes_price < b; NO buy at q: a print with yes_price > 1 - q).
Maker fee 0 (these series are fee_type 'quadratic'). Equal-$ return per filled order, held to settlement.
Also reported: any-print fills (a print AT the order price) and fill rates. 1 /markets/trades call per market.
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_new_series_honeymoon_maker
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import statistics as st
from collections import defaultdict

from lab.kalshi.strategies.r6_new_series_honeymoon import decide, stats
from lab.kalshi.strategies.r6_new_series_honeymoon_api import OUT, calls_used, get
from lab.kalshi.strategies.r6_new_series_honeymoon_data import load_weather_high, load_weather_low, ts
from lab.kalshi.strategies.r6_new_series_honeymoon_val import FRESH, val_rows

POST_H, CANCEL_H = 8, 3


def eligible(m: dict):
    q = decide(m, POST_H)
    if not q:
        return None
    a, b = q[0][0], q[0][1]
    if 0 < b and a < 1 and a - b >= 0.04 and 0.15 <= (a + b) / 2 <= 0.85:
        return a, b
    return None


def sample() -> list[dict]:
    gas = [m for m in val_rows() if m["series"] in FRESH]
    wx = []
    for loader in (load_weather_high, load_weather_low):
        xs = [m for m in loader() if m["grp"] == "NEW"]
        first = defaultdict(lambda: 1 << 62)
        for m in xs:
            first[m["series"]] = min(first[m["series"]], m["open"])
        for m in xs:
            m["age_d"] = (m["open"] - first[m["series"]]) / 86400
            if m["age_d"] < 28:
                wx.append(m)
    rnd = random.Random(6)
    out = []
    for pool in (gas, wx):
        by_ev = defaultdict(list)
        for m in pool:
            q = eligible(m)
            if q:
                m["post_ask"], m["post_bid"] = q
                by_ev[m["e"]].append(m)
        evs = sorted(by_ev)
        rnd.shuffle(evs)
        for e in evs[:25]:
            out.append(rnd.choice(sorted(by_ev[e], key=lambda m: m["t"])))
    return out


def main() -> None:
    S = sample()
    rows = []
    for m in S:
        post = m["close"] - POST_H * 3600; cancel = m["close"] - CANCEL_H * 3600
        d = get(f"/markets/trades?ticker={m['t']}&min_ts={post}&max_ts={cancel}&limit=1000")
        prints = [(ts(x["created_time"]), float(x["yes_price_dollars"]), float(x.get("count_fp") or 0)) for x in d.get("trades") or []]
        prints = [p for p in prints if post + 120 <= p[0] < cancel]
        won_yes = m["result"] == "yes"
        b, a = m["post_bid"], m["post_ask"]
        for side, px in (("YES", round(b + 0.01, 2)), ("NO", round(1 - a + 0.01, 2))):
            if side == "YES":
                thru = [p for p in prints if p[1] < px - 1e-9]; at = [p for p in prints if abs(p[1] - px) < 1e-9]
            else:
                thru = [p for p in prints if p[1] > 1 - px + 1e-9]; at = [p for p in prints if abs(p[1] - (1 - px)) < 1e-9]
            won = won_yes if side == "YES" else not won_yes
            rows.append({"t": m["t"], "e": m["e"], "fam": "GAS_STATE" if m["series"] in FRESH else "WX_NEW", "side": side,
                         "px": px, "spread": round(a - b, 3), "n_prints": len(prints), "thru": len(thru) > 0,
                         "thru_qty": sum(p[2] for p in thru), "at": len(at) > 0, "won": won,
                         "ret": ((1.0 if won else 0.0) - px) / px, "fd": m["e"], "close": m["close"],
                         "day": dt.datetime.utcfromtimestamp(m["close"] - 6 * 3600).strftime("%Y-%m-%d")})
        print(m["t"], "prints", len(prints), "| calls", calls_used(), flush=True)
    res = {"orders": len(rows), "markets": len(S)}
    for fam in ("ALL", "GAS_STATE", "WX_NEW"):
        x = [r for r in rows if fam == "ALL" or r["fam"] == fam]
        thru = [r for r in x if r["thru"]]; anyp = [r for r in x if r["thru"] or r["at"]]
        res[fam] = {"orders": len(x), "fill_rate_thru": round(len(thru) / len(x), 3) if x else None,
                    "fill_rate_any": round(len(anyp) / len(x), 3) if x else None,
                    "thru_fills": stats(thru), "any_print_fills": stats(anyp),
                    "unfilled_win_rate": round(st.mean(r["won"] for r in x if not r["thru"]), 3) if [r for r in x if not r["thru"]] else None,
                    "filled_win_rate": round(st.mean(r["won"] for r in thru), 3) if thru else None,
                    "markets_with_any_print": sum(1 for r in x if r["n_prints"] > 0) // 2}
    with open(os.path.join(OUT, "maker_orders.jsonl"), "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    json.dump(res, open(os.path.join(OUT, "maker.json"), "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
