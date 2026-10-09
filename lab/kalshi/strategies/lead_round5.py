"""Lead checks, round 5.

1. K update and gate bar after round 5 (0 calls).
2. Power arithmetic (0 calls): how many independent events a strategy of a given price profile needs to reach the gate
   bar at a true +10%/$ mean. This is what makes rare-event families (Billboard reveals, election nights, seeded-book
   lotteries) structurally unable to pass, and points round 6 at favourite-side, daily-frequency series.
3. Observable-state daily/weekly series census (counted Kalshi calls, cached under lead_round5/api_cache).
   Round 2 found the daily Spotify/Netflix chart series dead (no settled markets in 70 days). Kalshi also lists
   daily/weekly series that settle on a snapshot or count of a PUBLIC live source (Apple top-free RSS, Steam top
   sellers, OpenRouter token rankings, FlightAware delay counts, NY Fed ON RRP, Truth Social / X post counts,
   Federal Register, White House lid calls). Question for round 6: which of them are active, how much volume
   do they carry, and is the winner already priced >= 0.90 hours before close (nothing to buy) or still uncertain
   in the last hours (a slow poller of the source could matter)?
   Step A: one settled-markets listing per series (last ~68 days). Step B: hourly event candles for the 2 most
   recent events of each active series. DESCRIPTIVE ONLY: it picks the winner with the outcome, so it is a census
   of where uncertainty lives, not a rule; every series looked at is burned as discovery data for round 6.

Run: .venv/bin/python -m lab.kalshi.strategies.lead_round5
Output: data/kalshi_lab/strategies/lead_round5/lead_checks.json
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics as st
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

from lab.kalshi.strategies.lead_round5_api import OUT, calls_used, get

ET = ZoneInfo("America/New_York")
NOW = 1791518400  # 2026-10-09 00:00 UTC, fixed so the cache keys are stable

ROUND5 = {  # builder-reported variants_examined (task input; r5_seeded_book result.json nests it)
    "r5_seeded_book_placeholder_split": 134,
    "r5_scheduled_release_poller": 22,
    "r5_midterms_2026_thin_race_markets": 24,
    "r5_kalshi_cross_series_follower": 111,
    "r5_announced_date_slip": 100,
    "r5_mention_speaker_base_rate_maker_filter": 29,
}
VERIFIER = {  # cells the verifiers added (counted from their issue lists)
    "placeholder V1 (S2 definition, exact prints, locator swap, Beta bounds)": 8,
    "placeholder V2 (event jackknife, exact prints, frozen-forward cancel, bootstrap)": 8,
    "base-rate V1 (4 clusterings, full/marginal fill split, size proxy)": 10,
    "base-rate V2 (day clustering, marginal re-weight, S1 selection, full-fill cells)": 10,
    "base-rate extras undercounted by the builder (per V2)": 25,
}
K_BEFORE = 18078

SERIES = [
    "KXAPPRANKFREE", "KXAPPRANKFREE2", "KXAPPRANKFREESECOND", "KXTOPSELLERS", "KXTOKENUSED", "KXTOP3AID",
    "KXFLIGHTJFK", "KXFLIGHTORD", "KXFLIGHTLAX", "KXORDDLY", "KXONRRP", "KXPMLA", "KXFULLLIDBEFORE8PM",
    "KXTRUTHSOCIAL", "KXELONTWEETS", "KXEOWEEK", "KXBILLSCOUNTWEEKLY", "KXYTDAILYTOPVIDEO",
]


def zbar(K: int) -> float:
    p = 0.05 / K
    lo, hi = 0.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * math.erfc(mid / math.sqrt(2)) > p:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)


def k_update() -> dict:
    b = sum(ROUND5.values())
    v = sum(VERIFIER.values())
    lead = 6  # power table + census metrics (descriptive)
    tot = b + v + lead
    K = K_BEFORE + tot
    return {"builders": ROUND5, "builders_total": b, "verifiers": VERIFIER, "verifiers_total": v, "lead": lead,
            "round5_total": tot, "K_before": K_BEFORE, "K_after": K, "bar_t": zbar(K)}


def power(bar: float) -> list:
    """Events needed for t >= bar at mean +10%/$ with equal-$ stakes, sd per $ from a Bernoulli payoff at price p."""
    rows = []
    for p, label in [(0.92, "favourite NO/YES at 0.92"), (0.85, "favourite at 0.85"), (0.70, "favourite at 0.70"),
                     (0.50, "coin flip at 0.50"), (0.30, "underdog at 0.30"), (0.18, "seeded-book NO lottery at 0.18")]:
        q = min(0.999, p * 1.10 + 0.07 * p * (1 - p))  # win rate needed for +10%/$ after the taker fee
        if q >= 1:
            rows.append({"profile": label, "price": p, "win_needed": None, "events_needed": None}); continue
        sd = math.sqrt(q * (1 - q)) / p
        n = math.ceil((bar * sd / 0.10) ** 2)
        rows.append({"profile": label, "price": p, "win_needed": round(q, 3), "sd_per_dollar": round(sd, 2),
                     "events_needed": n, "events_needed_t2": math.ceil((2.0 * sd / 0.10) ** 2)})
    return rows


def ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fnum(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def census_listing() -> dict:
    out = {}
    lo = NOW - 68 * 86400
    for s in SERIES:
        d = get(f"/markets?series_ticker={s}&status=settled&min_close_ts={lo}&limit=1000")
        ms = d.get("markets") or []
        ev = defaultdict(list)
        for m in ms:
            ev[m["event_ticker"]].append(m)
        evvol = sorted(sum(fnum(m.get("volume_fp")) for m in x) for x in ev.values())
        hours = Counter(dt.datetime.fromtimestamp(ts(m["close_time"]), ET).strftime("%a %H:%M") for m in ms)
        gap = [(ts(m["expected_expiration_time"]) - ts(m["close_time"])) / 3600 for m in ms
               if m.get("expected_expiration_time") and m.get("close_time")]
        out[s] = {
            "markets": len(ms), "events": len(ev), "truncated": bool(d.get("cursor")) and len(ms) >= 1000,
            "total_volume": round(sum(evvol)), "median_event_volume": round(st.median(evvol)) if evvol else 0,
            "yes_share": round(sum(m.get("result") == "yes" for m in ms) / len(ms), 3) if ms else None,
            "close_slots_ET": hours.most_common(3),
            "median_h_close_to_expected_exp": round(st.median(gap), 2) if gap else None,
            "can_close_early_share": round(sum(bool(m.get("can_close_early")) for m in ms) / len(ms), 2) if ms else None,
            "rules_sample": (ms[0].get("rules_primary") or "")[:400] if ms else "",
            "recent_events": [e for e, _ in sorted(ev.items(), key=lambda kv: -max(ts(m["close_time"]) for m in kv[1]))[:2]],
            "_ev": {e: [{"t": m["ticker"], "r": m.get("result"), "c": ts(m["close_time"]), "o": ts(m["open_time"]),
                         "v": fnum(m.get("volume_fp"))} for m in x] for e, x in ev.items()},
        }
    return out


def census_paths(lst: dict, min_events: int = 8, min_med_vol: int = 2000) -> dict:
    """For each active series: hourly event candles for the 2 most recent events. Winner = settled YES market
    (or, for single-market binary events, the side that won). Metrics: winner's bid/ask at close-24h, -6h, -2h, -1h,
    and the last hour before close in which the winner's yes_bid was still < 0.90 (lock time)."""
    res = {}
    act = sorted((s for s, r in lst.items() if r["events"] >= min_events and r["median_event_volume"] >= min_med_vol),
                 key=lambda s: -lst[s]["total_volume"])[:10]
    for s in act:
        row = lst[s]
        res[s] = []
        for e in row["recent_events"]:
            ms = row["_ev"][e]
            c = max(m["c"] for m in ms)
            lo = max(min(m["o"] for m in ms), c - 30 * 3600) // 3600 * 3600
            d = get(f"/series/{s}/events/{e}/candlesticks?start_ts={lo}&end_ts={c + 3600}&period_interval=60")
            paths = {}
            for tk, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                paths[tk] = [(int(x["end_period_ts"]), fnum((x.get("yes_bid") or {}).get("close_dollars")),
                              fnum((x.get("yes_ask") or {}).get("close_dollars")), fnum(x.get("volume_fp") or x.get("volume")))
                             for x in cs]
            yes = [m for m in ms if m["r"] == "yes"]
            if len(ms) == 1:
                w, side = ms[0], ("yes" if ms[0]["r"] == "yes" else "no")
            elif yes:
                w, side = yes[0], "yes"
            else:
                res[s].append({"event": e, "note": "no YES market"}); continue
            p = paths.get(w["t"], [])

            def at(h):  # winner-side (bid, ask) as of close - h hours, carried forward
                q = [x for x in p if x[0] <= c - h * 3600]
                if not q:
                    return None
                b, a = q[-1][1], q[-1][2]
                return (round(b, 2), round(a, 2)) if side == "yes" else (round(1 - a, 2), round(1 - b, 2))
            wb = [(x[0], x[1] if side == "yes" else 1 - x[2]) for x in p]
            unl = [t for t, b in wb if b < 0.90 and t <= c]
            lock_h = round((c - max(unl)) / 3600, 1) if unl else (round((c - wb[0][0]) / 3600, 1) if wb else None)
            res[s].append({"event": e, "markets": len(ms), "winner": w["t"], "side": side,
                           "event_volume": round(sum(m["v"] for m in ms)),
                           "last_vol_hours": sum(1 for x in p if x[3] > 0 and x[0] > c - 6 * 3600),
                           "winner_bid_ask_at": {f"-{h}h": at(h) for h in (24, 6, 2, 1)},
                           "hours_locked_ge90_before_close": lock_h})
    return res


def main() -> None:
    k = k_update()
    out = {"K": k, "power_at_plus10": power(k["bar_t"])}
    lst = census_listing()
    out["census_paths"] = census_paths(lst)
    for v in lst.values():
        v.pop("_ev", None)
    out["census_listing"] = lst
    out["kalshi_calls_used"] = calls_used()
    os.makedirs(OUT, exist_ok=True)
    json.dump(out, open(os.path.join(OUT, "lead_checks.json"), "w"), indent=1)
    print(json.dumps({"K": k, "power": out["power_at_plus10"]}, indent=1))
    for s, v in lst.items():
        print(f"{s:22s} ev {v['events']:3d} mk {v['markets']:4d} medvol {v['median_event_volume']:8d} "
              f"slots {v['close_slots_ET'][:2]} gap {v['median_h_close_to_expected_exp']}")
    print(json.dumps(out["census_paths"], indent=1))
    print("calls", calls_used())


if __name__ == "__main__":
    main()


def truth_history() -> dict:
    """Follow-up (2 calls): how deep is the KXTRUTHSOCIAL archive, and is the X-post sibling active?"""
    out = {}
    h = get("/historical/markets?series_ticker=KXTRUTHSOCIAL&limit=1000")
    ms = h.get("markets") or []
    ev = defaultdict(list)
    for m in ms:
        ev[m["event_ticker"]].append(m)
    vols = sorted(sum(fnum(m.get("volume_fp")) for m in x) for x in ev.values())
    closes = sorted(ts(m["close_time"]) for m in ms) if ms else []
    out["KXTRUTHSOCIAL_archive"] = {
        "markets": len(ms), "events": len(ev), "more_pages": bool(h.get("cursor")),
        "first_close": dt.datetime.utcfromtimestamp(closes[0]).date().isoformat() if closes else None,
        "last_close": dt.datetime.utcfromtimestamp(closes[-1]).date().isoformat() if closes else None,
        "median_event_volume": round(st.median(vols)) if vols else 0}
    p = get(f"/markets?series_ticker=KXPOTUSTWEETS&status=settled&min_close_ts={NOW - 68 * 86400}&limit=1000")
    pm = p.get("markets") or []
    out["KXPOTUSTWEETS_recent"] = {"markets": len(pm), "events": len({m["event_ticker"] for m in pm}),
                                   "total_volume": round(sum(fnum(m.get("volume_fp")) for m in pm))}
    return out


if __name__ == "__main__" and os.environ.get("LEAD5_TRUTH"):
    th = truth_history()
    f = os.path.join(OUT, "lead_checks.json")
    d = json.load(open(f))
    d["truth_history"] = th
    d["kalshi_calls_used"] = calls_used()
    json.dump(d, open(f, "w"), indent=1)
    print(json.dumps(th, indent=1), "calls", calls_used())
