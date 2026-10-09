"""r6_thin_book_sweep_follow_fade: data layer (0 Kalshi calls).

Normalises every 1-minute candle set already on disk into one market list and detects sweep triggers WITHOUT looking at
any outcome. The trigger rule below is frozen before any outcome was loaded into the analysis (see SPEC; the analysis
module reads results only after triggers.jsonl is written).

Sources (all 1-minute candles, 0 new calls):
  weather  data/lab/us/kalshi/<ticker>.json (+ markets.json)              KXHIGH* 24 cities, 2025-07..2026-10
  lab      data/kalshi_lab/candles/<S>.jsonl (+ markets/<S>.jsonl)          daily commodities, rain, sports, tennis, esports
           (15-minute and hourly series are excluded a priori: their 16/70-min windows cannot hold the 60-min history the
           trigger needs, and round 1 showed them professionally quoted)
  rain     strategies/rain_history, strategies/microstructure (arch), strategies/r2_launch_window (nyc)
  commod   strategies/r2_daily_commod_maker candles_<S>.jsonl
  mentions strategies/r4_single_appearance_seeded_books (period-1 rows only)
  election strategies/r5_midterms_2026_thin_race_markets
  official strategies/r4_official_posting_lag_study (House/Senate/SCOTUS count markets)
  charts   strategies/r5_scheduled_release_poller (Billboard 200 around the article), strategies/r2_chart_markets min_*.json
  awards   strategies/lead_round4 emmy26 (Emmy night)

Normalised candle row: [end_ts, yes_ask_close, yes_bid_close, volume].
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import os
from collections import Counter
from pathlib import Path

ROOT = Path("/Users/roomyhome/Tradeinc")
S = ROOT / "data/kalshi_lab/strategies"
OUT = S / "r6_thin_book_sweep_follow_fade"

SPEC = {
    "frozen_at": "2026-10-09 (before any outcome of a trigger was looked at)",
    "trigger": {
        "move_c": 0.15,            # |yes_ask or yes_bid change| vs the previous quote, in the direction of the mid change
        "vol_mult": 10.0,          # minute volume >= vol_mult * median per-minute volume of the trailing 6 h (zeros for silent minutes)
        "vol_floor": 10.0,         # ... and >= 10 contracts (the 6-h median of a thin book is 0)
        "trail_s": 6 * 3600,
        "min_history_s": 3600,     # >= 60 min of candle coverage before the trigger minute
        "prev_max_age_s": 1800,    # the previous quote must be <= 30 min old
        "prior_vol_max": 5000.0,   # contracts traded before the trigger minute (final volume - candle volume from the trigger on)
        "one_per_market": "first trigger",
    },
    "fill": {"delay_s": 120, "max_quote_age_s": 1800, "px_range": [0.02, 0.98], "stake_usd": 5.0,
             "fee": "ceil-to-cent of 0.07*p*(1-p)*n per order, n=floor(5/p) contracts"},
    "arms": {"FOLLOW": "buy the side the mid moved toward", "FADE": "buy the other side"},
    "hold": "to settlement; 30-min mark (exit at the bid) and mid move t+2..t+30 are diagnostics",
    "split": "events ordered by their first trigger time; discovery = first 70%, validation = last 30%",
    "kill_on_discovery": "median further mid move t+2..t+30 in the arm's direction < 3c, or arm mean return per $ < +5%",
}
LAB_SERIES = ["KXRAIN", "KXAAAGASD", "KXWTI", "KXGOLDD", "KXNATGASD", "KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME",
              "KXNCAAFGAME", "KXUEFANLGAME", "KXCS2GAME", "KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH",
              "KXATPMATCH", "KXWTACHALLENGERMATCH"]


def iso(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def fnum(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def category(series: str) -> str:
    s = series.upper()
    if s.startswith("KXHIGH"):
        return "weather"
    if "RAIN" in s:
        return "rain"
    if s in ("KXAAAGASD", "KXWTI", "KXGOLDD", "KXNATGASD"):
        return "daily_commod"
    if s.endswith("MATCH"):
        return "tennis"
    if s == "KXCS2GAME":
        return "esports"
    if s.endswith("GAME"):
        return "sports"
    if "MENTION" in s or "SAY" in s:
        return "mentions"
    if s.startswith("HOUSE") or s.startswith("SENATE") or s.startswith("GOV") and "SHUT" not in s:
        return "elections"
    if "TOPALBUM" in s or "NETFLIX" in s or "HOT100" in s or "TOPSONG" in s or "SPOTIFY" in s:
        return "charts"
    if "EMMY" in s or "OSCAR" in s or "GRAMMY" in s:
        return "awards"
    return "official"


def raw_rows(cs: list[dict]) -> list[list]:
    out = []
    for c in cs:
        a, b = c.get("yes_ask") or {}, c.get("yes_bid") or {}
        out.append([int(c["end_period_ts"]), fnum(a.get("close_dollars", a.get("close"))), fnum(b.get("close_dollars", b.get("close"))),
                    fnum(c.get("volume_fp", c.get("volume"))) or 0.0])
    out.sort()
    return out


def pick(rows: list[list], vi: int) -> list[list]:
    out = []
    for r in rows:
        ask, bid = fnum(r[1]), fnum(r[2])
        out.append([int(r[0]), ask, bid, fnum(r[vi]) or 0.0])
    out.sort()
    return out


def jl(p: Path):
    if not p.exists():
        return
    with p.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def lab_meta() -> dict:
    """Lab-format market metadata from every markets file on disk, keyed by ticker."""
    meta = {}
    files = [ROOT / f"data/kalshi_lab/markets/{s}.jsonl" for s in LAB_SERIES]
    files += [S / "rain_history/markets.jsonl", S / "microstructure/arch_markets.jsonl", S / "r5_midterms_2026_thin_race_markets/markets.jsonl",
              S / "r4_official_posting_lag_study/markets.jsonl", S / "r2_chart_markets/markets.jsonl", S / "r2_chart_markets/markets_hist.jsonl"]
    files += sorted((S / "r2_daily_commod_maker").glob("markets_*.jsonl"))
    for f in files:
        for m in jl(f):
            t = m.get("t")
            if not t or m.get("result") not in ("yes", "no"):
                continue
            meta[t] = {"t": t, "e": m.get("e") or t.rsplit("-", 1)[0], "series": m.get("series") or t.split("-")[0],
                       "open": m.get("open"), "close": m.get("close"), "result": m["result"], "vol": fnum(m.get("vol"))}
    for m in jl(S / "r4_single_appearance_seeded_books/markets.jsonl"):
        if m.get("result") in ("yes", "no"):
            meta[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": m.get("series") or m["ticker"].split("-")[0],
                                 "open": iso(m.get("open_time")), "close": iso(m.get("close_time")), "result": m["result"],
                                 "vol": fnum(m.get("volume_fp"))}
    em = S / "lead_round4/emmy26_markets.json"
    if em.exists():
        for m in json.loads(em.read_text()):
            if m.get("result") in ("yes", "no"):
                meta[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0],
                                     "open": iso(m.get("open_time")), "close": iso(m.get("close_time")), "result": m["result"],
                                     "vol": fnum(m.get("volume_fp"))}
    be = S / "r5_scheduled_release_poller/b200_events.json"
    if be.exists():
        for ev in json.loads(be.read_text()):
            for m in ev.get("markets") or []:
                if m.get("result") in ("yes", "no") and m["t"] not in meta:
                    meta[m["t"]] = {"t": m["t"], "e": ev["e"], "series": "KXTOPALBUM", "open": m.get("open"), "close": ev.get("close"),
                                    "result": m["result"], "vol": fnum(m.get("vol"))}
    return meta


def candle_sources():
    """Yield (src, ticker, rows, lo) for every non-weather 1-minute candle set."""
    for s in LAB_SERIES:
        for x in jl(ROOT / f"data/kalshi_lab/candles/{s}.jsonl"):
            yield "lab", x["t"], pick(x.get("c") or [], 5), None
    for x in jl(S / "rain_history/candles.jsonl"):
        yield "rain_history", x["t"], pick(x.get("c") or [], 5), None
    for x in jl(S / "microstructure/arch_candles.jsonl"):
        yield "micro_arch", x["t"], pick(x.get("c") or [], 5), None
    for x in jl(S / "r2_launch_window/nyc_candles.jsonl"):
        yield "nyc_rain", x["t"], pick(x.get("c") or [], 5), None
    for f in sorted((S / "r2_daily_commod_maker").glob("candles_*.jsonl")):
        for x in jl(f):
            yield "commod_maker", x["t"], pick(x.get("c") or [], 9), None
    for x in jl(S / "r4_single_appearance_seeded_books/candles.jsonl"):
        if x.get("period") == 1:
            yield "single_app", x["t"], pick(x.get("c") or [], 9), x.get("lo")
    for x in jl(S / "r5_midterms_2026_thin_race_markets/candles.jsonl"):
        yield "midterms", x["t"], pick(x.get("c") or [], 5), x.get("lo")
    for x in jl(S / "r4_official_posting_lag_study/candles.jsonl"):
        yield "official", x["t"], pick(x.get("c") or [], 5), x.get("lo")
    for x in jl(S / "r5_scheduled_release_poller/candles.jsonl"):
        yield "release_poller", x["t"], pick(x.get("c") or [], 7), x.get("lo")
    for f in sorted((S / "r2_chart_markets").glob("min_*.json")):
        for t, cs in json.loads(f.read_text()).items():
            yield "chart_min", t, raw_rows(cs), None
    em = S / "lead_round4/emmy26_candles.json"
    if em.exists():
        for t, cs in json.loads(em.read_text()).items():
            yield "emmy", t, raw_rows(cs), None


def weather_markets():
    kd = ROOT / "data/lab/us/kalshi"
    M = json.loads((kd / "markets.json").read_text())
    for t, m in M.items():
        p = kd / f"{t}.json"
        if m.get("result") not in ("yes", "no") or not p.exists():
            continue
        try:
            cs = json.loads(p.read_text())
        except Exception:
            continue
        rows = raw_rows(cs) if isinstance(cs, list) else []
        meta = {"t": t, "e": m["event_ticker"], "series": m["series"], "open": iso(m.get("open_time")), "close": iso(m.get("close_time")),
                "result": m["result"], "vol": fnum(m.get("volume_fp") or m.get("volume"))}
        yield "weather", meta, rows


# ---------------------------------------------------------------- trigger detection (no outcome used)

def median_trailing(vols: dict, ts: int, lo: int, trail: int) -> float:
    start = max(lo, ts - trail)
    n = max(0, (ts - start) // 60)                 # covered minutes strictly before the trigger minute
    if n == 0:
        return 0.0
    vals = [v for k, v in vols.items() if start < k < ts and v > 0]
    if len(vals) * 2 <= n:
        return 0.0                                 # at least half the minutes were silent
    full = sorted(vals + [0.0] * (n - len(vals)))
    return full[n // 2]


def detect(meta: dict, rows: list[list], lo_decl, T: dict | None = None, refractory_s: int | None = None):
    """First sweep trigger of a market, or None. Uses candles <= trigger ts plus the market's final volume figure only to
    reconstruct the cumulative volume that Kalshi shows live at the trigger time (prior volume).
    With refractory_s set, returns the list of ALL triggers at least refractory_s apart (secondary variant)."""
    T = T or SPEC["trigger"]
    found = []
    rows = [r for r in rows if r[0] is not None]
    if len(rows) < 3:
        return None
    lo = min(rows[0][0], lo_decl) if lo_decl else rows[0][0]
    vols = {r[0]: r[3] for r in rows}
    total_c = sum(r[3] for r in rows)
    suffix = total_c
    F = meta.get("vol")
    prev = None
    close = meta.get("close") or 10 ** 12
    for i, r in enumerate(rows):
        ts, ask, bid, v = r
        if prev is not None and ask is not None and bid is not None and v >= T["vol_floor"] and ts - lo >= T["min_history_s"] \
                and ts - prev[0] <= T["prev_max_age_s"] and ts + SPEC["fill"]["delay_s"] < close:
            pa, pb = prev[1], prev[2]
            da, db = ask - pa, bid - pb
            dmid = (da + db) / 2
            big = max(abs(da), abs(db))
            if big >= T["move_c"] - 1e-9 and abs(dmid) > 1e-9:
                d = 1 if dmid > 0 else -1
                leg_ok = (da * d >= T["move_c"] - 1e-9) or (db * d >= T["move_c"] - 1e-9)
                if leg_ok:
                    med = median_trailing(vols, ts, lo, T["trail_s"])
                    if v >= max(T["vol_floor"], T["vol_mult"] * med):
                        if F is not None and F + 1e-6 >= total_c:
                            prior = F - suffix            # suffix = candle volume from this minute on
                        else:
                            prior = total_c - suffix
                        if prior < T["prior_vol_max"]:
                            win = [x for x in rows if ts - 3600 <= x[0] <= ts + 2 * 3600]
                            last = rows[-1]
                            hit = {"ts": ts, "dir": d, "prev": prev[:3], "trig": [ts, ask, bid], "vol": v, "med6h": med,
                                   "prior_vol": prior, "prior_src": "final-minus-after" if F is not None and F + 1e-6 >= total_c else "cum-candles",
                                   "hist_min": (ts - lo) // 60, "c": win, "last": last}
                            if refractory_s is None:
                                return hit
                            if not found or ts - found[-1]["ts"] >= refractory_s:
                                found.append(hit)
        suffix -= v
        if ask is not None and bid is not None:
            prev = r
    return found if refractory_s is not None else None


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meta = lab_meta()
    best = {}
    stats = Counter()
    for src, t, rows, lo in candle_sources():
        stats["rows_" + src] += 1
        m = meta.get(t)
        if not m:
            stats["nometa_" + src] += 1
            continue
        if t not in best or len(rows) > len(best[t][1]):
            best[t] = (src, rows, lo)
    trig = []
    scanned = Counter()
    for t, (src, rows, lo) in best.items():
        m = meta[t]; cat = category(m["series"]); scanned[cat] += 1
        r = detect(m, rows, lo)
        if r:
            r.update({"t": t, "e": m["e"], "series": m["series"], "cat": cat, "src": src, "close": m["close"], "fvol": m["vol"]})
            trig.append(r)
    wn = 0
    for src, m, rows in weather_markets():
        wn += 1; scanned["weather"] += 1
        r = detect(m, rows, None)
        if r:
            r.update({"t": m["t"], "e": m["e"], "series": m["series"], "cat": "weather", "src": src, "close": m["close"], "fvol": m["vol"]})
            trig.append(r)
    # outcomes are attached in a separate file so the trigger list itself carries none
    res = {t: meta[t]["result"] for t in (x["t"] for x in trig) if t in meta}
    wm = json.loads((ROOT / "data/lab/us/kalshi/markets.json").read_text())
    for x in trig:
        if x["t"] not in res and x["t"] in wm:
            res[x["t"]] = wm[x["t"]]["result"]
    with gzip.open(OUT / "triggers.jsonl.gz", "wt") as fh:
        for x in sorted(trig, key=lambda z: z["ts"]):
            fh.write(json.dumps(x) + "\n")
    (OUT / "outcomes.json").write_text(json.dumps(res))
    (OUT / "scan_stats.json").write_text(json.dumps({"spec": SPEC, "sources": stats, "markets_scanned": scanned,
                                                     "triggers": Counter(x["cat"] for x in trig), "n_trig": len(trig)}, indent=1))
    print("scanned", dict(scanned), "triggers", Counter(x["cat"] for x in trig), "sources", dict(stats))


VARIANTS = {
    # (B) the motivating retail-event anecdotes (Billboard, House, midterms, Emmys, mentions): no prior-volume cap, no history need
    "B_events_nocap": {"cats": ("mentions", "elections", "official", "charts", "awards"),
                       "T": {"prior_vol_max": 1e12, "min_history_s": 0}, "refractory_s": None},
    # (C) every trigger of the primary universe, at least 60 min apart within a market
    "C_all_refractory60": {"cats": None, "T": {}, "refractory_s": 3600},
}


def build_variant(name: str) -> None:
    V = VARIANTS[name]
    T = dict(SPEC["trigger"], **V["T"])
    meta = lab_meta(); best = {}
    for src, t, rows, lo in candle_sources():
        if t in meta and (t not in best or len(rows) > len(best[t][1])):
            best[t] = (src, rows, lo)
    trig = []
    def add(m, cat, src, hit):
        hit.update({"t": m["t"], "e": m["e"], "series": m["series"], "cat": cat, "src": src, "close": m["close"], "fvol": m["vol"]})
        trig.append(hit)
    for t, (src, rows, lo) in best.items():
        m = meta[t]; cat = category(m["series"])
        if V["cats"] and cat not in V["cats"]:
            continue
        r = detect(m, rows, lo, T, V["refractory_s"])
        for h in (r if isinstance(r, list) else ([r] if r else [])):
            add(m, cat, src, h)
    if not V["cats"] or "weather" in V["cats"]:
        for src, m, rows in weather_markets():
            r = detect(m, rows, None, T, V["refractory_s"])
            for h in (r if isinstance(r, list) else ([r] if r else [])):
                add(m, "weather", src, h)
    with gzip.open(OUT / f"triggers_{name}.jsonl.gz", "wt") as fh:
        for x in sorted(trig, key=lambda z: z["ts"]):
            fh.write(json.dumps(x) + "\n")
    res = json.loads((OUT / "outcomes.json").read_text())
    wm = json.loads((ROOT / "data/lab/us/kalshi/markets.json").read_text())
    for x in trig:
        if x["t"] not in res:
            res[x["t"]] = meta[x["t"]]["result"] if x["t"] in meta else wm.get(x["t"], {}).get("result")
    (OUT / "outcomes.json").write_text(json.dumps(res))
    print(name, len(trig), Counter(x["cat"] for x in trig))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        build_variant(sys.argv[1])
    else:
        build()
