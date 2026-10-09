"""Validation data and the one-time validation run for r6_new_series_honeymoon (frozen.json was written first).

Validation universe (frozen before any fetch): honeymoon markets (opened < 28 d after the series' first market) of
  fresh   KXAAAGASD GA PA WA (launched 2026-09-01) and MI MA TN VA AZ (2026-09-09), from this task's settled lists;
  on-disk KXAAAGASD NC OH (09-01) SC (09-16), KXTRUMPAPPROVE (09-01), KXVHGCB (09-24), KXTRUTHSOCIALD (10-01) from r2,
          KXBIGGESTQUAKE (09-02, 1-minute candles, no fetch).
Strike selection (outcome-free, frozen): gas events keep strikes within 2.5c of the previous event's settled value
(known before the decision); a series' first event keeps every strike; other series keep every market.
Candles: batch /markets/candlesticks period 60 over [close - 9 h, close - 3 h] (<= 100 tickers, <= 9,500 market-hours).
Rules: frozen C1-C3 (r6_new_series_honeymoon.RULES cells), hourly convention (first hourly close >= t).
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_new_series_honeymoon_val fetch|run
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict

from lab.kalshi.strategies.r6_new_series_honeymoon_api import CACHE, OUT, calls_used, get
from lab.kalshi.strategies.r6_new_series_honeymoon_data import S, _jsonl, _rows, load_quake, ts

FRESH = {"KXAAAGASDGA", "KXAAAGASDPA", "KXAAAGASDWA", "KXAAAGASDMI", "KXAAAGASDMA", "KXAAAGASDTN", "KXAAAGASDVA", "KXAAAGASDAZ"}
ONDISK = {"KXAAAGASDNC", "KXAAAGASDOH", "KXAAAGASDSC", "KXTRUMPAPPROVE", "KXVHGCB", "KXTRUTHSOCIALD"}
FAM = {**{s: "GAS_STATE" for s in FRESH | {"KXAAAGASDNC", "KXAAAGASDOH", "KXAAAGASDSC"}}, "KXTRUMPAPPROVE": "TRUMPAPPROVE",
       "KXVHGCB": "VHGCB", "KXTRUTHSOCIALD": "TRUTHD"}
VM = os.path.join(OUT, "val_markets.jsonl")
VC = os.path.join(OUT, "val_candles.jsonl")
LO_H, HI_H = 9, 3


def fresh_markets() -> list[dict]:
    """Settled markets of the fresh series from the cached live listings (full objects carry expiration_value)."""
    out = {}
    for f in glob.glob(os.path.join(CACHE, "*.json")):
        d = json.load(open(f))
        for m in d.get("markets") or []:
            if "ticker" not in m:
                continue
            s = m["ticker"].split("-")[0]
            if s in FRESH and m.get("result") in ("yes", "no"):
                out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": s, "open": ts(m["open_time"]),
                                    "close": ts(m["close_time"]), "result": m["result"], "type": m.get("strike_type"),
                                    "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
                                    "xv": m.get("expiration_value"), "vol": float(m.get("volume_fp") or 0)}
    return list(out.values())


def universe() -> list[dict]:
    ms = fresh_markets() + [m for m in _jsonl(os.path.join(S, "r2_launch_window/markets.jsonl"))
                            if m["series"] in ONDISK and m.get("result") in ("yes", "no")]
    first = defaultdict(lambda: 1 << 62)
    for m in ms:
        first[m["series"]] = min(first[m["series"]], m["open"])
    by_ev = defaultdict(list)
    for m in ms:
        m["launch"] = first[m["series"]]; m["age_d"] = (m["open"] - m["launch"]) / 86400
        if m["age_d"] < 28:
            by_ev[(m["series"], m["e"])].append(m)
    # previous settled value per series (by event close)
    ev_close = {k: max(m["close"] for m in v) for k, v in by_ev.items()}
    xv = {}
    for m in ms:
        try:
            xv[m["e"]] = float(m.get("xv"))
        except (TypeError, ValueError):
            pass
    keep = []
    for s in {k[0] for k in by_ev}:
        evs = sorted((k for k in by_ev if k[0] == s), key=lambda k: ev_close[k])
        prev = None
        for k in evs:
            v = by_ev[k]
            if FAM[s] == "GAS_STATE" and prev is not None and prev in xv:
                p = xv[prev]
                v = [m for m in v if m.get("floor") is not None and abs(float(m["floor"]) - p) <= 0.025 + 1e-9]
            keep += v
            prev = k[1]
    return keep


def fetch() -> None:
    ms = sorted(universe(), key=lambda m: m["close"])
    with open(VM, "w") as fh:
        for m in ms:
            fh.write(json.dumps(m) + "\n")
    have = {x["t"] for x in _jsonl(VC)}
    todo = [m for m in ms if m["t"] not in have]
    print("validation markets", len(ms), "to fetch", len(todo), "calls so far", calls_used(), flush=True)
    i = 0
    with open(VC, "a") as fh:
        while i < len(todo):
            batch = [todo[i]]; lo, hi = todo[i]["close"] - LO_H * 3600, todo[i]["close"] - HI_H * 3600
            while i + len(batch) < len(todo) and len(batch) < 100:
                m2 = todo[i + len(batch)]
                lo2, hi2 = min(lo, m2["close"] - LO_H * 3600), max(hi, m2["close"] - HI_H * 3600)
                if (len(batch) + 1) * ((hi2 - lo2) // 3600 + 1) > 9500:
                    break
                batch.append(m2); lo, hi = lo2, hi2
            i += len(batch)
            d = get(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            if not got:
                print("batch failed", batch[0]["t"], str(d)[:200], flush=True); continue
            g = lambda c, k: (float(c[k]["close_dollars"]) if (c.get(k) or {}).get("close_dollars") is not None else None)
            for m in batch:
                c = [[int(x["end_period_ts"]), g(x, "yes_ask"), g(x, "yes_bid"), float(x.get("volume_fp") or 0)] for x in got.get(m["t"], [])]
                fh.write(json.dumps({"t": m["t"], "c": c}) + "\n")
            print("batch", len(batch), "span h", (hi - lo) // 3600, "calls", calls_used(), flush=True)


def val_rows() -> list[dict]:
    ms = {m["t"]: m for m in _jsonl(VM)}
    cand = {x["t"]: _rows(x["c"]) for x in _jsonl(VC)}
    r2c = {x["t"]: _rows(x["c"]) for x in _jsonl(os.path.join(S, "r2_launch_window/candles_h.jsonl")) if x["t"] in ms}
    out = []
    for t, m in ms.items():
        c = sorted(set(cand.get(t, [])) | set(r2c.get(t, [])))
        out.append({"t": t, "e": m["e"], "series": m["series"], "fam": FAM[m["series"]], "grp": "NEW", "open": m["open"],
                    "close": m["close"], "result": m["result"], "c": c, "res": "hour", "vol": m.get("vol", 0),
                    "launch": m["launch"], "age_d": m["age_d"]})
    q = load_quake()
    lq = min(x["open"] for x in q)
    for m in q:
        m["launch"] = lq; m["age_d"] = (m["open"] - lq) / 86400
        if m["age_d"] < 28:
            out.append(m)
    return out


def run() -> dict:
    from lab.kalshi.strategies.r6_new_series_honeymoon import gen_trades, stats
    fz = json.load(open(os.path.join(OUT, "frozen.json")))
    cells = {k: v["rule"] for k, v in fz["candidates"].items()}
    rows = val_rows()
    T = [r for r in gen_trades(rows) if r["arm"] == "H"]
    res = {}
    for k, cell in cells.items():
        x = [r for r in T if r["cell"] == cell]
        st_ = stats(x)
        st_["by_family"] = {f: {kk: vv for kk, vv in stats([r for r in x if r["fam"] == f]).items() if kk in ("n", "events", "ret", "t_event", "avg_px")}
                            for f in sorted({r["fam"] for r in x})}
        st_["fresh_only"] = {kk: vv for kk, vv in stats([r for r in x if r["series"] in FRESH]).items() if kk in ("n", "events", "ret", "t_event", "t_famdate", "avg_px")}
        days = sorted({r["day"] for r in x})
        st_["trades_per_day"] = round(len(x) / max(1, (max(r["close"] for r in x) - min(r["close"] for r in x)) / 86400), 2) if x else 0
        st_["days"] = len(days)
        res[k] = {"rule": cell, **st_}
    with open(os.path.join(OUT, "val_trades.jsonl"), "w") as fh:
        for r in T:
            if r["cell"] in cells.values():
                fh.write(json.dumps(r) + "\n")
    json.dump(res, open(os.path.join(OUT, "validation.json"), "w"), indent=1)
    print(json.dumps(res, indent=1))
    return res


if __name__ == "__main__" and sys.argv[1] in ("fetch", "run"):
    {"fetch": fetch, "run": run}[sys.argv[1]]()


# ---- Diagnostic added after the validation run (not a candidate search): is the validation gas result a honeymoon
# effect? Same frozen rules on MATURE gas-state markets (age >= 28 d) of the earlier cohorts on the same calendar dates.
MATURE = {"KXAAAGASDTX", "KXAAAGASDCA", "KXAAAGASDFL", "KXAAAGASDNJ", "KXAAAGASDIL", "KXAAAGASDNY",
          "KXAAAGASDNC", "KXAAAGASDOH", "KXAAAGASDGA", "KXAAAGASDPA", "KXAAAGASDWA"}
MM = os.path.join(OUT, "mature_markets.jsonl")
MC = os.path.join(OUT, "mature_candles.jsonl")


def mature_universe() -> list[dict]:
    out = {}
    for f in glob.glob(os.path.join(CACHE, "*.json")):
        d = json.load(open(f))
        for m in d.get("markets") or []:
            if "ticker" not in m:
                continue
            s = m["ticker"].split("-")[0]
            if s in MATURE and m.get("result") in ("yes", "no"):
                out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": s, "open": ts(m["open_time"]),
                                    "close": ts(m["close_time"]), "result": m["result"], "floor": m.get("floor_strike"),
                                    "xv": m.get("expiration_value"), "vol": float(m.get("volume_fp") or 0)}
    for m in _jsonl(os.path.join(S, "r2_launch_window/markets.jsonl")):
        if m["series"] in MATURE and m.get("result") in ("yes", "no"):
            out.setdefault(m["t"], m)
    ms = list(out.values())
    first = defaultdict(lambda: 1 << 62)
    for m in ms:
        first[m["series"]] = min(first[m["series"]], m["open"])
    xv = {}
    for m in ms:
        try:
            xv[m["e"]] = float(m.get("xv"))
        except (TypeError, ValueError):
            pass
    by_ev = defaultdict(list)
    for m in ms:
        m["launch"] = first[m["series"]]; m["age_d"] = (m["open"] - m["launch"]) / 86400
        by_ev[(m["series"], m["e"])].append(m)
    ev_close = {k: max(m["close"] for m in v) for k, v in by_ev.items()}
    keep = []
    for s in MATURE:
        evs = sorted((k for k in by_ev if k[0] == s), key=lambda k: ev_close[k])
        for prev, k in zip(evs, evs[1:]):
            v = by_ev[k]
            if v[0]["age_d"] < 28 or prev[1] not in xv:
                continue
            keep += [m for m in v if m.get("floor") is not None and abs(float(m["floor"]) - xv[prev[1]]) <= 0.025 + 1e-9]
    return keep


def fetch_mature() -> None:
    global VM, VC
    ms = sorted(mature_universe(), key=lambda m: m["close"])
    with open(MM, "w") as fh:
        for m in ms:
            fh.write(json.dumps(m) + "\n")
    have = {x["t"] for x in _jsonl(MC)}
    todo = [m for m in ms if m["t"] not in have]
    print("mature markets", len(ms), "to fetch", len(todo), "calls so far", calls_used(), flush=True)
    i = 0
    g = lambda c, k: (float(c[k]["close_dollars"]) if (c.get(k) or {}).get("close_dollars") is not None else None)
    with open(MC, "a") as fh:
        while i < len(todo):
            batch = [todo[i]]; lo, hi = todo[i]["close"] - LO_H * 3600, todo[i]["close"] - HI_H * 3600
            while i + len(batch) < len(todo) and len(batch) < 100:
                m2 = todo[i + len(batch)]
                lo2, hi2 = min(lo, m2["close"] - LO_H * 3600), max(hi, m2["close"] - HI_H * 3600)
                if (len(batch) + 1) * ((hi2 - lo2) // 3600 + 1) > 9500:
                    break
                batch.append(m2); lo, hi = lo2, hi2
            i += len(batch)
            d = get(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            for m in batch:
                c = [[int(x["end_period_ts"]), g(x, "yes_ask"), g(x, "yes_bid"), float(x.get("volume_fp") or 0)] for x in got.get(m["t"], [])]
                fh.write(json.dumps({"t": m["t"], "c": c}) + "\n")
            print("batch", len(batch), "span h", (hi - lo) // 3600, "calls", calls_used(), flush=True)


def honeymoon_vs_mature_gas() -> dict:
    """Frozen cells on validation-period gas: H (validation honeymoon) vs M (earlier cohorts, age >= 28 d) on the SAME dates."""
    from lab.kalshi.strategies.r6_new_series_honeymoon import gen_trades, stats
    fz = json.load(open(os.path.join(OUT, "frozen.json")))
    cells = {k: v["rule"] for k, v in fz["candidates"].items()}
    Hrows = [r for r in val_rows() if r["fam"] == "GAS_STATE"]
    ms = {m["t"]: m for m in _jsonl(MM)}
    Mrows = []
    for x in _jsonl(MC):
        m = ms[x["t"]]
        Mrows.append({"t": m["t"], "e": m["e"], "series": m["series"], "fam": "GAS_STATE", "grp": "NEW", "open": m["open"],
                      "close": m["close"], "result": m["result"], "c": _rows(x["c"]), "res": "hour", "launch": m["launch"],
                      "age_d": m["age_d"]})
    TH = [r for r in gen_trades(Hrows) if r["arm"] == "H"]
    TM = [r for r in gen_trades(Mrows) if r["arm"] == "M"]
    out = {}
    for k, cell in cells.items():
        h = [r for r in TH if r["cell"] == cell]; m = [r for r in TM if r["cell"] == cell]
        common = {r["day"] for r in h} & {r["day"] for r in m}
        out[k] = {"rule": cell, "H_all": stats(h), "M_all": stats(m),
                  "H_common_days": stats([r for r in h if r["day"] in common]),
                  "M_common_days": stats([r for r in m if r["day"] in common]), "common_days": len(common)}
    json.dump(out, open(os.path.join(OUT, "gas_h_vs_m.json"), "w"), indent=1)
    for k, v in out.items():
        f = lambda s: f"n={s.get('n',0)} ev={s.get('events')} px={s.get('avg_px')} ret={s.get('ret')} t={s.get('t_event')}/{s.get('t_famdate')}"
        print(k, v["rule"], "| H common", f(v["H_common_days"]), "| M common", f(v["M_common_days"]), "| days", v["common_days"])
    return out


if __name__ == "__main__" and sys.argv[1] in ("fetch_mature", "gas_hm"):
    {"fetch_mature": fetch_mature, "gas_hm": honeymoon_vs_mature_gas}[sys.argv[1]]()
