"""Lead checks, round 4.

1. Seeded-book maker, cross-family pool (0 Kalshi calls). The only theme with positive out-of-sample point
   estimates in more than one family is "sell YES 1c inside Kalshi's seeded listing book on mention markets,
   cancel before the appearance". Four samples, four slightly different frozen rules:
     r2 mention maker C1 (68 events, through-only, verifier-B corrected), r3 earnings C1E validation (14 events,
     look-ahead-fixed reproduction), r4 earnings arm A on 79 archive events round 3 never priced, r4 single-
     appearance V1 (pre-registered, 29 filled events). Stouffer's combination is DESCRIPTIVE ONLY: the rules differ,
     the pooling is post hoc, and r2 / r4-single may share a few series. It answers "is there anything here worth
     a forward test", not "does it pass the gate".
2. Award-show sibling-lag feasibility probe (34 Kalshi calls, cached under lead_round4/api_cache). Round 4's only
   lag evidence was thin House per-member markets staying stale 9-28 min after the vote. Is that a general
   "thin siblings lag the headline sibling" effect in live-announced multi-outcome events? Emmys 2026 (live tier,
   batch candles): trigger T = first 1-min candle in which any sibling's yes_bid close >= 0.98 (observable at T);
   decision T+1 min, taker fill at T+2 min. Opportunity = a loser sibling's yes_bid >= 0.10 (NO <= 0.90) or the
   winner's yes_ask <= 0.90 at T+2. Descriptive; Emmys 2026 is now burned as discovery data for any round-5 test.

Run: .venv/bin/python -m lab.kalshi.strategies.lead_round4   (no network unless the cache is missing)
Output: data/kalshi_lab/strategies/lead_round4/lead_checks.json
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT = os.path.join(ROOT, "data/kalshi_lab/strategies/lead_round4")
S = os.path.join(ROOT, "data/kalshi_lab/strategies")


def _j(p: str):
    return json.load(open(os.path.join(S, p)))


def seeded_meta() -> dict:
    e4 = _j("r4_earnings_seeded_book_48h_forward/validation.json")
    s4 = _j("r4_single_appearance_seeded_books/validation.json")
    a_fresh = e4["A|fresh_all"]["size_weighted"]
    a_val = e4["A|validation"]
    v1_pool = s4["pooled"]["V1_P_sell_thr"]
    rows = [
        {"sample": "r2 mention maker C1, through-only (verifier B corrected; source lead_round2/result.json)",
         "events": 68, "ret": 0.113, "t": 1.55, "filled_vs_unfilled_NO_win": "n/a"},
        {"sample": "r3 earnings C1E validation, bulk-jump cancel (source lead_round3/result.json)",
         "events": 14, "ret": 0.828, "t": 2.18, "filled_vs_unfilled_NO_win": "0.26-0.36 vs 0.54-0.70"},
        {"sample": "r4 earnings arm A, fresh archive events never priced in r3 (size-weighted)",
         "events": a_fresh["events"], "ret": a_fresh["ret_per_dollar"], "t": a_fresh["t"],
         "filled_vs_unfilled_NO_win": f"{e4['A|fresh_all']['filled_NO_win']} vs {e4['A|fresh_all']['unfilled_NO_win']}"},
        {"sample": "r4 single-appearance V1 (pre-registered C1S), all filled events",
         "events": v1_pool["events"], "ret": v1_pool["ret_per_dollar"], "t": v1_pool["t"],
         "filled_vs_unfilled_NO_win": f"{v1_pool['filled_NO_win']} vs {v1_pool['unfilled_NO_win']}"},
    ]
    st_all = sum(r["t"] for r in rows) / math.sqrt(len(rows))
    st_no_r2 = sum(r["t"] for r in rows[1:]) / math.sqrt(len(rows) - 1)
    # how many events a lottery of this shape needs for t >= 4.54: per-event sd of per-$ return for NO bought at
    # ~0.18 winning ~40% is about sqrt(.4*.6)*(1/.18) ~ 2.7
    sd = math.sqrt(0.4 * 0.6) / 0.18
    need = {f"true_mean_{m:+.2f}": round((4.54 * sd / m) ** 2) for m in (0.10, 0.30, 0.50)}
    return {
        "rows": rows,
        "stouffer_t_all4": round(st_all, 2),
        "stouffer_t_without_r2": round(st_no_r2, 2),
        "r4_earnings_A_last30pct_validation": {"n": a_val.get("size_weighted", a_val).get("n") if isinstance(a_val, dict) else None,
                                               "note": "A on the protocol validation slice: +11.2%/$, t 0.26, -57% without the 3 best; B48 -48.8%"},
        "events_needed_for_t_4.54": need,
        "reading": ("Every sample is positive and every one shows adverse selection (filled NO wins less than unfilled). "
                    "The descriptive combination is t ~3.0-3.4, below the 4.54 bar, from heterogeneous post-hoc-pooled rules. "
                    "The payoff is a lottery (NO ~0.18-0.40 winning 40-55%), so a gate-grade t needs several hundred to a few "
                    "thousand events. Forward test only; it cannot pass gate row 3 within a year."),
    }


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def emmy_probe() -> dict:
    M = {m["ticker"]: m for m in json.load(open(os.path.join(OUT, "emmy26_markets.json")))}
    C = json.load(open(os.path.join(OUT, "emmy26_candles.json")))
    f = lambda x: None if x is None else float(x)  # noqa: E731
    ev = defaultdict(list)
    for t in C:
        ev[M[t]["event_ticker"]].append(t)
    cats, opp = [], 0
    for e, tks in sorted(ev.items()):
        close = _ts(M[tks[0]]["close_time"])
        T, trig = None, None
        for t in tks:
            for k in C[t]:
                b = f(k["yes_bid"].get("close_dollars"))
                if b is not None and b >= 0.98 and k["end_period_ts"] < close:
                    if T is None or k["end_period_ts"] < T:
                        T, trig = k["end_period_ts"], t
                    break
        if T is None:
            cats.append({"event": e, "trigger": None, "note": "no sibling bid >= 0.98 inside the fetched window (creative-arts night; window starts after the announcement)"})
            continue

        def quote_at(t: str, ts: int):
            last = None
            for k in C[t]:
                if k["end_period_ts"] <= ts:
                    last = k
            if last is None:
                return None, None
            return f(last["yes_bid"].get("close_dollars")), f(last["yes_ask"].get("close_dollars"))

        losers_bid2, losers_bid1, win_ask2, loser_vol_above5c = [], [], None, 0.0
        for t in tks:
            b1, _ = quote_at(t, T + 60)
            b2, a2 = quote_at(t, T + 120)
            if M[t]["result"] == "yes":
                win_ask2 = a2
            else:
                losers_bid1.append(b1 or 0.0)
                losers_bid2.append(b2 or 0.0)
                for k in C[t]:
                    if k["end_period_ts"] > T + 60 and f(k["price"].get("high_dollars") or 0) and f(k["price"]["high_dollars"]) > 0.05:
                        loser_vol_above5c += float(k.get("volume_fp") or 0)
        o = (max(losers_bid2) >= 0.10) or (win_ask2 is not None and win_ask2 <= 0.90)
        opp += o
        cats.append({"event": e, "trigger": trig.split("-")[-1], "trigger_is_winner": M[trig]["result"] == "yes",
                     "T_utc": dt.datetime.fromtimestamp(T, dt.timezone.utc).strftime("%m-%d %H:%M"),
                     "minutes_T_to_kalshi_close": round((close - T) / 60, 1), "siblings": len(tks),
                     "max_loser_yes_bid_T+1": max(losers_bid1), "max_loser_yes_bid_T+2": max(losers_bid2),
                     "winner_yes_ask_T+2": win_ask2, "loser_contracts_traded_above_5c_after_T+1": round(loser_vol_above5c, 1),
                     "opportunity": o})
    n_trig = sum(1 for c in cats if c.get("trigger"))
    return {"categories": cats, "triggered": n_trig, "with_opportunity": opp,
            "reading": ("In every telecast category all siblings repriced inside the announcement minute (losers' bids to "
                        "0.00-0.01, winner to 0.99) although Kalshi kept the markets open 5-50 min longer. "
                        "No stale sibling quote survived to T+2 min. Live-televised announcements are swept within a minute; "
                        "the House per-member lag is about information not yet public, not about thin siblings.")}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    out = {"seeded_book_meta": seeded_meta(), "emmy26_sibling_lag_probe": emmy_probe()}
    json.dump(out, open(os.path.join(OUT, "lead_checks.json"), "w"), indent=1)
    m = out["seeded_book_meta"]
    for r in m["rows"]:
        print(f"{r['sample'][:70]:70s} ev={r['events']:3d} ret={r['ret']:+.3f} t={r['t']:.2f} filled/unfilled NO win {r['filled_vs_unfilled_NO_win']}")
    print("Stouffer all4", m["stouffer_t_all4"], "without r2", m["stouffer_t_without_r2"], "events needed", m["events_needed_for_t_4.54"])
    p = out["emmy26_sibling_lag_probe"]
    for c in p["categories"]:
        print(c)
    print("triggered", p["triggered"], "opportunities", p["with_opportunity"])


if __name__ == "__main__":
    main()
