"""Data for r4_long_dated_archive_census: frame, seeded sample, hourly candles.

frame():   eligible markets (common.eligible_row) from every cached listing on disk (any researcher, read as data) plus
           this researcher's new /historical/markets listings of the series in new_series.json (chosen by the
           pre-registered volume rule, census_preregistration.json); minus the 218 round-3 markets and every event
           that contains one of them; tradeable only.
sample():  at most 2 markets per event by sha1('r4census' + ticker).
candles(): events in sha1('r4order' + event) order; archive markets one /historical/markets/{t}/candlesticks call each,
           live-tier markets (settled after the 2026-08-08 archive cutoff) batched on /markets/candlesticks (<= 9,000
           hourly candles per call). Stops at STOP calls so the forward logger keeps its share of the budget.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_archive_census_data [frame|candles] [stop]"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_archive_census_api import OUT, ROOT, cached_markets, kget, used  # noqa: E402
from lab.kalshi.strategies.r4_long_dated_archive_census_common import DAY, eligible_row, seeded_pick, tradeable, ts  # noqa: E402

R3 = ROOT / "data/kalshi_lab/strategies/r3_long_dated_longshot_no"
CUTOFF = 1786147200          # 2026-08-08 00:00 UTC: archive tier holds markets settled before this
STOP = 168


def catalog() -> tuple[dict, dict]:
    cat, mult = {}, {}
    for s in json.loads((ROOT / "data/kalshi_lab/series_all.json").read_text())["series"]:
        cat[s["ticker"]] = s.get("category")
        mult[s["ticker"]] = float(s.get("fee_multiplier") or 1)
    return cat, mult


def catof(cat: dict, ser: str):
    return cat.get(ser) or cat.get(ser.removeprefix("KX")) or cat.get("KX" + ser)


def seen_markets() -> set[str]:
    seen = {json.loads(l)["t"] for l in (R3 / "sample.jsonl").open()}
    for f in (R3 / "calls.log", R3 / "repro/calls.log"):
        for l in f.open():
            x = re.search(r"/historical/markets/([^/]+)/candlesticks", l) or re.search(r"market_tickers=([^&]+)", l)
            if x:
                seen.update(x.group(1).split(","))
    return seen


def frame() -> list[dict]:
    cat, _ = catalog()
    mk = cached_markets()
    seen = seen_markets()
    seen_ev = {mk[t]["event_ticker"] for t in seen if t in mk}
    rows = []
    for m in mk.values():
        r = eligible_row(m, catof(cat, m["event_ticker"].split("-")[0]))
        if not r or r["t"] in seen or r["e"] in seen_ev or not tradeable(r):
            continue
        st_ = r["settled"] or r["close"]
        r["tier"] = "live" if st_ >= CUTOFF + 2 * DAY else "hist"
        rows.append(r)
    return rows


def window(r: dict) -> tuple[int, int]:
    lo = max(r["open"], r["S"] - 63 * DAY) // 3600 * 3600
    hi = min(r["close"], r["S"] - 14 * DAY + 2 * 3600)
    return lo, hi


def sample() -> list[dict]:
    F = frame()
    S = seeded_pick(F, 2, "r4census")
    (OUT / "frame.jsonl").write_text("".join(json.dumps(r) + "\n" for r in F))
    (OUT / "sample.jsonl").write_text("".join(json.dumps(r) + "\n" for r in S))
    print(f"frame {len(F)} markets / {len({r['e'] for r in F})} events; sample {len(S)} / {len({r['e'] for r in S})} events; "
          f"tiers {Counter(r['tier'] for r in S)}; cats {Counter(r['cat'] for r in S).most_common()}")
    return S


def _parse(lst: list[dict]) -> list[list]:
    out = []
    for c in lst:
        def px(side):
            x = c.get(side) or {}
            v = x.get("close_dollars", x.get("close"))
            if v is None:
                return None
            v = float(v)
            return v / 100 if v > 1.0001 else v
        a, b = px("yes_ask"), px("yes_bid")
        if a is None or b is None:
            continue
        out.append([int(c["end_period_ts"]), a, b, float(c.get("volume_fp") or c.get("volume") or 0)])
    return sorted(out)


def candles(stop: int = STOP) -> None:
    S = [json.loads(l) for l in (OUT / "sample.jsonl").open()]
    f = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in f.open()} if f.exists() else set()
    evs = sorted({r["e"] for r in S}, key=lambda e: hashlib.sha1(("r4order" + e).encode()).hexdigest())
    by = {}
    for r in S:
        by.setdefault(r["e"], []).append(r)
    queue: list[dict] = []

    def flush(g):
        if not queue:
            return
        lo = min(window(r)[0] for r in queue); hi = max(window(r)[1] for r in queue)
        d = kget(f"/markets/candlesticks?market_tickers={','.join(r['t'] for r in queue)}&start_ts={lo}&end_ts={hi}&period_interval=60")
        got = {x.get("market_ticker"): x.get("candlesticks", []) for x in d.get("markets", [])}
        for r in queue:
            g.write(json.dumps({"t": r["t"], "c": _parse(got.get(r["t"], [])), "err": d.get("_error"), "via": "live_batch"}) + "\n")
        g.flush()
        print(f"  live batch {len(queue)} markets -> {sum(len(v) for v in got.values())} candles, calls {used()}", flush=True)
        queue.clear()

    for e in by:
        by[e].sort(key=lambda r: hashlib.sha1(("r4census" + r["t"]).encode()).hexdigest())
    order = [r for k in (0, 1) for e in evs for r in by[e][k:k + 1]]   # round 1: first market per event; round 2: second
    with f.open("a") as g:
        for r in order:
            if True:
                if r["t"] in have:
                    continue
                if used() + 1 + (1 if queue else 0) > stop:
                    flush(g); print("stop: budget share reached", used()); return
                lo, hi = window(r)
                if hi <= lo:
                    continue
                if r["tier"] == "hist":
                    d = kget(f"/historical/markets/{r['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
                    c = _parse(d.get("candlesticks", []))
                    g.write(json.dumps({"t": r["t"], "c": c, "err": d.get("_error"), "via": "hist"}) + "\n"); g.flush()
                    print(f"{r['t']:44s} {len(c):5d} candles  calls {used()}", flush=True)
                else:
                    if queue:   # one batch shares one window: load = markets x union span (hours)
                        ulo = min([lo] + [window(q)[0] for q in queue]); uhi = max([hi] + [window(q)[1] for q in queue])
                        if (len(queue) + 1) * ((uhi - ulo) // 3600 + 1) > 9000 or len(queue) >= 8:
                            flush(g)
                    queue.append(r)
        flush(g)
    print("done, calls", used())


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "frame"
    if w == "frame":
        sample()
    elif w == "candles":
        candles(int(sys.argv[2]) if len(sys.argv) > 2 else STOP)
