"""Polymarket comparison for r4_long_dated_archive_census (free gamma/CLOB data, no Kalshi calls, no key).

Fresh sample: markets of the round-3 Polymarket universe (r3_long_dated_longshot_no/poly/markets.jsonl: closed binary
Yes/No markets resolved 1/0, scheduled life >= 45 d, event volume >= $100k, politics/geopolitics/world/business/tech/
science/economy tags, sports/crypto dropped) that were NOT in the round-3 Polymarket sample, with a scheduled end in
the same calendar months as the Kalshi census sample. Stratified by month to match the Kalshi month shares, at most 2
markets per event, seeded (sha1('r4poly' + id)).
Prices: CLOB /prices-history?market=<YES token>&interval=max&fidelity=720 (12-hourly points), cached under
data/kalshi_lab/strategies/r4_long_dated_archive_census/poly/. Clock as round 3: signal = last point <= S - D days (age
<= 3 d); fill = first point in [t + 1 h, t + 24 h] and before the market closed; taker cost = point + 1c (half-spread
proxy) plus the Kalshi taker fee for comparability. Same frozen cells C1 / B / M as the Kalshi census.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_archive_census_poly [sample|prices|analyze] [N]"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_archive_census_api import OUT, ROOT  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_archive_census_common import CELLS, DAY, DGRID, fee_pc, select, stats  # noqa: E402

P = OUT / "poly"
PC = P / "cache"
R3P = ROOT / "data/kalshi_lab/strategies/r3_long_dated_longshot_no/poly"
CLOB = "https://clob.polymarket.com"


def get(url: str, pace: float = 1.2):
    PC.mkdir(parents=True, exist_ok=True)
    f = PC / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research", "Accept": "application/json"}), timeout=90) as r:
                txt = r.read().decode()
            time.sleep(pace)
            f.write_text(txt)
            return json.loads(txt)
        except Exception as e:
            print("retry", i, str(e)[:80], flush=True)
            time.sleep(5 * (i + 1))
    return None


def month(t: int) -> str:
    return time.strftime("%Y-%m", time.gmtime(t))


def sample(n: int = 500) -> list[dict]:
    K = [json.loads(l) for l in (OUT / "sample.jsonl").open()]
    share = Counter(month(r["S"]) for r in K)
    tot = sum(share.values())
    U = [json.loads(l) for l in (R3P / "markets.jsonl").open()]
    seen = {json.loads(l)["id"] for l in (R3P / "sample.jsonl").open()}
    pool = [r for r in U if r["id"] not in seen and month(r["S"]) in share]
    out = []
    for m, k in sorted(share.items()):
        quota = round(n * k / tot)
        cand = sorted([r for r in pool if month(r["S"]) == m], key=lambda r: hashlib.sha1(("r4poly" + r["id"]).encode()).hexdigest())
        per_ev = Counter()
        got = []
        for r in cand:
            if len(got) >= quota:
                break
            if per_ev[r["e"]] >= 2:
                continue
            per_ev[r["e"]] += 1
            got.append(r)
        out += got
    P.mkdir(parents=True, exist_ok=True)
    (P / "sample.jsonl").write_text("".join(json.dumps(r) + "\n" for r in out))
    print("pool", len(pool), "sample", len(out), "events", len({r["e"] for r in out}), Counter(month(r["S"]) for r in out).most_common())
    return out


def prices() -> None:
    S = [json.loads(l) for l in (P / "sample.jsonl").open()]
    f = P / "prices.jsonl"
    have = {json.loads(l)["id"] for l in f.open()} if f.exists() else set()
    with f.open("a") as g:
        for i, r in enumerate(S):
            if r["id"] in have:
                continue
            d = get(f"{CLOB}/prices-history?market={r['tok']}&interval=max&fidelity=720")
            h = (d or {}).get("history", [])
            g.write(json.dumps({"id": r["id"], "h": [[x["t"], x["p"]] for x in h]}) + "\n")
            g.flush()
            if i % 50 == 0:
                print(i, r["q"][:60], len(h), flush=True)


def trades() -> list[dict]:
    M = {json.loads(l)["id"]: json.loads(l) for l in (P / "sample.jsonl").open()}
    H = {json.loads(l)["id"]: sorted(json.loads(l)["h"]) for l in (P / "prices.jsonl").open()}
    T = []
    for i, r in M.items():
        h = H.get(i) or []
        if len(h) < 3:
            continue
        end = r["closed"] or r["S"]
        for D in DGRID:
            t = r["S"] - D * DAY
            if t < r["start"] or t + 3600 >= end:
                continue
            sig = [x for x in h if x[0] <= t]
            fil = [x for x in h if t + 3600 <= x[0] <= t + 24 * 3600]
            if not sig or t - sig[-1][0] > 3 * DAY or not fil or fil[0][0] >= end:
                continue
            for side, ps, pf, won in (("NO", 1 - sig[-1][1], 1 - fil[0][1], not r["yes_won"]), ("YES", sig[-1][1], fil[0][1], r["yes_won"])):
                px = min(pf + 0.01, 0.995)
                if not (0.02 <= px <= 0.99):
                    continue
                pnl = (1.0 if won else 0.0) - px - fee_pc(px)
                g = time.gmtime(fil[0][0])
                T.append({"t": i, "e": r["e"], "series": r["tag"], "D": D, "side": side, "ps": round(ps, 4), "px": round(px, 4), "won": won,
                          "pnl": pnl, "ret": pnl / px, "t_close": end, "fill_ts": fil[0][0], "S": r["S"],
                          "quarter": f"{g.tm_year}Q{(g.tm_mon - 1) // 3 + 1}"})
    return T


def analyze() -> dict:
    T = trades()
    out = {"markets_priced": len({r["t"] for r in T}), "cells": {}}
    lines = [f"Polymarket fresh sample: trade rows {len(T)}, markets with any row {out['markets_priced']}"]
    for c in CELLS:
        v = stats(select(T, c))
        out["cells"][c] = v
        b = v.get("bounds", {})
        lines.append(f"  {c:3s} n={v.get('n', 0):4d} ev={v.get('events', 0):4d} win={v.get('win', 0):.3f} px={v.get('avg_px', 0):.3f} "
                     f"ret={v.get('ret_per_dollar', 0):+.4f} t_ev={v.get('t')} t_ser={v.get('t_series')} wo3={v.get('ret_wo3')} "
                     f"halves={v.get('half1')}/{v.get('half2')} | events n={b.get('n_events')} win={b.get('win')} cp_ret_lo={b.get('ret_at_cp_lo95')}")
    txt = "\n".join(lines)
    print(txt)
    (P / "analysis.txt").write_text(txt + "\n")
    (P / "result.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    if w == "sample":
        sample(int(sys.argv[2]) if len(sys.argv) > 2 else 500)
    elif w == "prices":
        prices()
    elif w == "analyze":
        analyze()
