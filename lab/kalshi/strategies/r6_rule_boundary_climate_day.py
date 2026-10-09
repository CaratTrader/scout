"""r6_rule_boundary_climate_day: does the Kalshi daily-high market price the calendar-day proxy instead of the CLI's
local-standard-time climate day?

Rule boundary. The NWS climate report (CLI), which settles KXHIGH*, runs midnight-to-midnight local STANDARD time. Under
daylight saving time the climate day for date D is 01:00 D .. 00:59 D+1 local clock time, so
  E ("early"): the 00:00-00:59 hour of D belongs to D-1. On a cold-front morning the calendar-day max of D sits in
     that hour and overstates the CLI max of D;
  L ("late"):  the 00:00-00:59 hour of D+1 belongs to D. A warm night or a warm front can set D's CLI max after
     the clock has passed midnight, while the market is still open (KXHIGH markets close at 00:00 LST = 01:00 LDT).

Steps (zero Kalshi calls; all data on disk):
  1. census:  every DST city-day with a settled Kalshi event: METAR max over the calendar day vs over the LST climate
     day, mapped to that event's brackets; how often they differ, and which one the settled CLI value agrees with.
  2. trade tests (only if the census finds >= 40 bracket-changing city-days): rules decidable at t from the observed
     readings, filled as taker one minute later at the on-disk candle quote. Discovery = first 70% of events by close
     time, validation = last 30%, parameters chosen on discovery only.

Run: .venv/bin/python -m lab.kalshi.strategies.r6_rule_boundary_climate_day [census|trade|all]
Outputs: data/kalshi_lab/strategies/r6_rule_boundary_climate_day/
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics as st
import sys
from collections import Counter, defaultdict

from lab.kalshi.strategies.r6_rule_boundary_climate_day_data import (OUT, STD_OFF_H, bracket_of, events, interval, is_dst,
                                                                      rnd, station_obs, yes_ticker)

H = 3600


def windows(e: dict) -> dict:
    """UTC bounds of the LST climate day and the wall-clock calendar day of event e."""
    d = dt.date.fromisoformat(e["day"]); off = STD_OFF_H[e["tz"]]
    lst0 = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp()) - off * H
    dst = is_dst(e["tz"], e["day"])
    cal0 = lst0 - (H if dst else 0)
    return {"lst0": lst0, "lst1": lst0 + 24 * H, "cal0": cal0, "cal1": cal0 + 24 * H, "dst": dst}


def wmax(obs: list[dict], a: int, b: int, use_x6: bool = True, use_hf: bool = False) -> float | None:
    """Max F over observations with a <= u < b: METAR point readings, 6-h max groups lying wholly inside, and
    (optionally) 5-minute whole-C readings."""
    best = None
    for o in obs:
        if o["u"] < a - 6 * H:
            continue
        if o["u"] >= b + 1:
            break
        if a <= o["u"] < b and o["f"] is not None and (o["kind"] == "metar" or use_hf):
            best = o["f"] if best is None else max(best, o["f"])
        if use_x6 and o["x6"] is not None and o["u"] - 6 * H >= a - 60 and o["u"] <= b + 60:
            best = o["x6"] if best is None else max(best, o["x6"])
    return best


def obs_slice(obs: list[dict], a: int, b: int) -> list[dict]:
    lo, hi = 0, len(obs)
    while lo < hi:   # first index with u >= a - 6h
        mid = (lo + hi) // 2
        if obs[mid]["u"] < a - 6 * H:
            lo = mid + 1
        else:
            hi = mid
    out = []
    for o in obs[lo:]:
        if o["u"] > b + 6 * H:
            break
        out.append(o)
    return out


def census() -> dict:
    EV = events()
    rows = []
    for et, e in EV.items():
        w = windows(e)
        if not w["dst"] or e["cli"] is None:
            continue
        obs = obs_slice(station_obs(e["stn"], e["tz"]), w["cal0"], w["lst1"])
        if not obs:
            continue
        early = wmax(obs, w["cal0"], w["lst0"], use_x6=False)
        rest = wmax(obs, w["lst0"], w["cal1"], use_x6=True)
        late = wmax(obs, w["cal1"], w["lst1"], use_x6=False)
        calx = wmax(obs, w["cal0"], w["cal1"]); lstx = wmax(obs, w["lst0"], w["lst1"])
        calx5 = wmax(obs, w["cal0"], w["cal1"], use_hf=True); lstx5 = wmax(obs, w["lst0"], w["lst1"], use_hf=True)
        x24 = [o["x24"] for o in obs if o["x24"] is not None and w["lst1"] - 40 * 60 <= o["u"] <= w["lst1"] + 20 * 60]
        n_rest = sum(1 for o in obs if w["lst0"] <= o["u"] < w["cal1"] and o["kind"] == "metar")
        if calx is None or lstx is None or n_rest < 12:
            continue
        yt = yes_ticker(e)
        r = {"e": et, "stn": e["stn"], "day": e["day"], "close": e["close"], "cli": e["cli"], "early": early, "rest": rest, "late": late,
             "cal": calx, "lst": lstx, "cal5": calx5, "lst5": lstx5, "x24": x24[-1] if x24 else None,
             "b_cal": bracket_of(e, calx), "b_lst": bracket_of(e, lstx), "b_cli": bracket_of(e, e["cli"]), "yes": yt,
             "b_cal5": bracket_of(e, calx5), "b_lst5": bracket_of(e, lstx5)}
        r["type"] = ("E" if early is not None and rnd(early) > rnd(max(rest or -999, late or -999)) else "") + \
                    ("L" if late is not None and rnd(late) > rnd(max(rest or -999, early or -999)) else "")
        rows.append(r)
    rows.sort(key=lambda r: r["close"])
    diff = [r for r in rows if r["b_cal"] != r["b_lst"]]
    deg = [r for r in rows if rnd(r["cal"]) != rnd(r["lst"])]
    agree = lambda xs, k: sum(1 for r in xs if r[k] == r["b_cli"])
    by_type = Counter(("E" if rnd(r["cal"]) > rnd(r["lst"]) else "L") for r in diff)
    summ = {
        "dst_city_days": len(rows), "events_with_yes": sum(1 for r in rows if r["yes"]),
        "cli_bracket_equals_settled_yes": sum(1 for r in rows if r["yes"] and r["b_cli"] == r["yes"]),
        "days_max_differs_by_ge1F": len(deg),
        "days_bracket_differs": len(diff), "bracket_differs_by_direction": dict(by_type),
        "on_differing_days_cli_bracket_equals_lst": agree(diff, "b_lst"), "on_differing_days_cli_bracket_equals_cal": agree(diff, "b_cal"),
        "all_days_cli_bracket_equals_lst": agree(rows, "b_lst"), "all_days_cli_bracket_equals_cal": agree(rows, "b_cal"),
        "all_days_cli_bracket_equals_lst5": agree(rows, "b_lst5"), "all_days_cli_bracket_equals_cal5": agree(rows, "b_cal5"),
        "x24_available": sum(1 for r in rows if r["x24"] is not None),
        "x24_bracket_equals_cli": sum(1 for r in rows if r["x24"] is not None and bracket_of(events()[r["e"]], r["x24"]) == r["b_cli"]),
        "cli_minus_lst_F": dict(Counter(rnd(r["cli"]) - rnd(r["lst"]) for r in rows).most_common(9)),
        "cli_minus_cal_F": dict(Counter(rnd(r["cli"]) - rnd(r["cal"]) for r in rows).most_common(9)),
        "differing_by_station": dict(Counter(r["stn"] for r in diff).most_common()),
        "differing_by_month": dict(sorted(Counter(r["day"][:7] for r in diff).items())),
        "dst_days_by_station": dict(Counter(r["stn"] for r in rows).most_common()),
    }
    return {"summary": summ, "differing_days": diff, "rows": rows}


def boundary_cases() -> dict:
    """Bracket-changing boundary days measured with every reading (METAR, 6-h groups, 5-minute rows):
    late = the 00:00-00:59 hour of D+1 sets a higher bracket than D's 01:00-24:00; early = the 00:00-00:59 hour of D
    sets a higher bracket than D's LST climate day. Which bracket did the CLI settle on?"""
    EV = events(); late, early = [], []
    for et, e in EV.items():
        w = windows(e)
        if not w["dst"] or e["cli"] is None:
            continue
        obs = obs_slice(station_obs(e["stn"], e["tz"]), w["cal0"], w["lst1"])
        if not obs:
            continue
        day = wmax(obs, w["lst0"], w["cal1"], use_x6=True, use_hf=True)
        lt = wmax(obs, w["cal1"], w["lst1"], use_x6=False, use_hf=True)
        ea = wmax(obs, w["cal0"], w["lst0"], use_x6=False, use_hf=True)
        if day is None:
            continue
        if lt is not None and rnd(lt) > rnd(day) and bracket_of(e, lt) != bracket_of(e, day):
            late.append({"e": et, "day_max": day, "late_max": lt, "cli": e["cli"]})
        if ea is not None and rnd(ea) > rnd(day) and bracket_of(e, ea) != bracket_of(e, day):
            early.append({"e": et, "lst_day_max": day, "early_max": ea, "cli": e["cli"],
                          "cli_follows": "lst" if bracket_of(e, e["cli"]) == bracket_of(e, day) else
                                         "calendar" if bracket_of(e, e["cli"]) == bracket_of(e, ea) else "neither"})
    return {"late_hour_cases": late, "early_hour_cases": early,
            "early_cli_follows": dict(Counter(x["cli_follows"] for x in early))}


LAG = 5 * 60     # a METAR reading is usable 5 min after its timestamp
DELAY = 60       # taker fill one minute after the decision
GRID_H = [x / 2 for x in range(22, 49)]   # decision times 11:00 .. 24:00 local daylight time (candles start ~11:00)


def situations() -> list[dict]:
    """Decision-time states where the calendar-day max-so-far (which includes the 00:00-00:59 hour that belongs to
    the previous climate day) is in a higher bracket than the LST max-so-far. Decidable at t: readings with
    u + LAG <= t only; 6-h max groups only once reported and wholly inside the LST day."""
    EV = events(); out = []
    for et, e in EV.items():
        w = windows(e)
        if not w["dst"] or e["cli"] is None or yes_ticker(e) is None:
            continue
        obs = obs_slice(station_obs(e["stn"], e["tz"]), w["cal0"], w["lst1"])
        for hh in GRID_H:
            t = w["cal0"] + int(hh * H)
            if t + DELAY >= e["close"]:
                continue
            early = [o["f"] for o in obs if w["cal0"] <= o["u"] < w["lst0"] and o["kind"] == "metar" and o["f"] is not None and o["u"] + LAG <= t]
            rest = [o["f"] for o in obs if w["lst0"] <= o["u"] and o["u"] + LAG <= t and o["kind"] == "metar" and o["f"] is not None]
            rest += [o["x6"] for o in obs if o["x6"] is not None and o["u"] - 6 * H >= w["lst0"] - 60 and o["u"] + LAG <= t]
            if not early or not rest:
                continue
            mc, ml = max(max(early), max(rest)), max(rest)
            bc, bl = bracket_of(e, mc), bracket_of(e, ml)
            if bc is None or bl is None or bc == bl:
                continue
            out.append({"e": et, "stn": e["stn"], "day": e["day"], "close": e["close"], "hh": hh, "t": t, "m_cal": mc, "m_lst": ml,
                        "b_cal": bc, "b_lst": bl, "yes": yes_ticker(e)})
    return out


def trades(sit: list[dict], rule: str, cap: float | None) -> list[dict]:
    """A: YES on the bracket holding the LST max-so-far (the calendar proxy calls it dead).
    B: NO on the bracket holding the calendar max-so-far (the proxy calls it already reached).
    First qualifying decision time per event; fill at the quote one minute later (<= 30 min old)."""
    from lab.kalshi.strategies.r6_rule_boundary_climate_day_data import fee_order, quote
    done, out = set(), []
    for s in sorted(sit, key=lambda x: (x["e"], x["t"])):
        if s["e"] in done:
            continue
        tk = s["b_lst"] if rule == "A" else s["b_cal"]
        q = quote(tk, s["t"] + DELAY)
        if q is None:
            continue
        ask, bid = q
        px = ask if rule == "A" else 1 - bid
        if not (0.01 <= px <= 0.99) or (cap is not None and px > cap):
            continue
        won = (s["yes"] == tk) if rule == "A" else (s["yes"] != tk)
        f = fee_order(px, 10)
        out.append({**s, "rule": rule, "ticker": tk, "px": px, "won": won, "fee": f, "ret": ((1.0 if won else 0.0) - px - f) / px})
        done.add(s["e"])
    return out


def cstats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em) if len(em) > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(len(em))) if len(em) > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    mid = sorted(r["close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["close"] < mid]; h2 = [r["ret"] for r in rows if r["close"] >= mid]
    return {"n": len(rows), "events": len(em), "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None}


def trade_test() -> dict:
    sit = situations()
    ev_close = sorted({(s["close"], s["e"]) for s in sit})
    cut = ev_close[int(len(ev_close) * 0.7)][0] if ev_close else 0
    res = {"situations": len(sit), "situation_events": len(ev_close), "cut_close_ts": cut,
           "by_hour": dict(sorted(Counter(s["hh"] for s in sit).items())), "cells": {}}
    for rule in ("A", "B"):
        for cap in (None, 0.90):
            T = trades(sit, rule, cap)
            key = f"{rule}|cap={cap}"
            res["cells"][key] = {"discovery": cstats([r for r in T if r["close"] < cut]), "validation": cstats([r for r in T if r["close"] >= cut]),
                                 "all": cstats(T),
                                 "trades": [{k: r[k] for k in ("e", "hh", "ticker", "px", "won", "ret", "m_cal", "m_lst")} for r in T]}
    return res


def main(what: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if what in ("census", "all"):
        c = census()
        (OUT / "census.json").write_text(json.dumps({"summary": c["summary"], "differing_days": c["differing_days"]}, indent=1, default=str))
        (OUT / "census_rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in c["rows"]))
        print(json.dumps(c["summary"], indent=1))
        b = boundary_cases()
        (OUT / "boundary_cases.json").write_text(json.dumps(b, indent=1))
        print("late-hour bracket cases:", len(b["late_hour_cases"]), "early-hour bracket cases:", len(b["early_hour_cases"]), b["early_cli_follows"])
    if what in ("trade", "all"):
        r = trade_test()
        (OUT / "trade_test.json").write_text(json.dumps(r, indent=1))
        print("situations", r["situations"], "events", r["situation_events"], "by hour", r["by_hour"])
        for k, v in r["cells"].items():
            print(k, "DISC", v["discovery"], "\n      VAL ", v["validation"], "\n      ALL ", v["all"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")


def boundary_day_prices() -> dict:
    """DESCRIPTIVE ONLY (days selected on the outcome): on the early-hour boundary days, the market's quote at 11:00
    and 13:00 local daylight time for the calendar-day bracket and for the bracket the CLI settled on."""
    from lab.kalshi.strategies.r6_rule_boundary_climate_day_data import quote
    EV = events(); rows = []
    for c in boundary_cases()["early_hour_cases"]:
        e = EV[c["e"]]; w = windows(e); bc = bracket_of(e, c["early_max"]); by = yes_ticker(e)
        r = {"e": c["e"], "cli_follows": c["cli_follows"], "cal_bracket": bc, "cli_bracket": by}
        for hh in (11, 13, 16):
            t = w["cal0"] + hh * H
            qc, qy = quote(bc, t), quote(by, t)
            r[f"cal_yes_ask_{hh}h"] = qc[0] if qc else None
            r[f"cli_yes_bid_{hh}h"] = qy[1] if qy else None
        rows.append(r)
    lst = [r for r in rows if r["cli_follows"] == "lst"]
    def m(k):
        v = [r[k] for r in lst if r[k] is not None]
        return {"n": len(v), "mean": round(st.mean(v), 3) if v else None, "median": round(st.median(v), 3) if v else None,
                "share_cal_ask_le_0.20_or_cli_bid_ge_0.80": None}
    summ = {k: m(k) for k in ("cal_yes_ask_11h", "cli_yes_bid_11h", "cal_yes_ask_13h", "cli_yes_bid_13h", "cal_yes_ask_16h", "cli_yes_bid_16h")}
    v = [r for r in lst if r["cal_yes_ask_11h"] is not None]
    summ["cal_yes_ask_11h"]["share_cal_ask_le_0.20_or_cli_bid_ge_0.80"] = round(sum(r["cal_yes_ask_11h"] <= 0.20 for r in v) / len(v), 3) if v else None
    v = [r for r in lst if r["cli_yes_bid_11h"] is not None]
    summ["cli_yes_bid_11h"]["share_cal_ask_le_0.20_or_cli_bid_ge_0.80"] = round(sum(r["cli_yes_bid_11h"] >= 0.80 for r in v) / len(v), 3) if v else None
    return {"rows": rows, "summary_on_cli_follows_lst_days": summ}


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "prices":
    p = boundary_day_prices()
    (OUT / "boundary_day_prices.json").write_text(json.dumps(p, indent=1))
    for r in p["rows"]:
        print(r)
    print(json.dumps(p["summary_on_cli_follows_lst_days"], indent=1))
