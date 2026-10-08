"""rain_history: observation rules on Kalshi daily rain markets (KXRAIN), judged walk-forward.

Rules (pre-registered in data/kalshi_lab/strategies/rain_history/prereg.json):
  Y  qualifying precip already in a METAR -> buy YES (KXRAIN: measurable >= 0.01 in; KXRAINNYC: any, trace counts)
  N  late-day dry (no qualifying precip so far, no precip/convection in the last 3 h, optional dew-point depression and
     pressure-tendency filters) -> buy NO
  T  trace-only so far and precip ended -> buy NO (KXRAIN: Trace settles NO)
Fill: taker at the quote one minute after the decision (YES at yes_ask, NO at 1 - yes_bid), quote <= 30 min old,
fee = ceil-to-cent of 0.07 p (1-p) on a 10-contract order. Equal-$ return per trade; t clustered by event (date).

Usage:
  python -m lab.kalshi.strategies.rain_history discovery            # all variants, discovery events only
  python -m lab.kalshi.strategies.rain_history validation           # frozen variants (FROZEN), last 24 KXRAIN events, once
  python -m lab.kalshi.strategies.rain_history archive              # frozen variants on the archived KXRAIN dates (holdout)
  python -m lab.kalshi.strategies.rain_history nyc                  # frozen variants on KXRAINNYC (robustness, trace = YES)"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote
from lab.kalshi.strategies.rain_history_obs import first_time, local_ts, state
from lab.kalshi.strategies.rain_history_data import CODE2STN, load_markets, load_candles

OUT = Path("data/kalshi_lab/strategies/rain_history")
DISK = Path("data/kalshi_lab/candles/KXRAIN.jsonl")
N_DISC = 56          # first 56 of 80 KXRAIN events (prereg amendment 1)
WINDOW_H = 18        # candles cover the last 18 h before close


def fee10(p: float) -> float:
    """Per-contract fee of a 10-contract taker order, rounded up to the cent."""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def variants() -> dict[str, dict]:
    v = {}
    for cap in (0.85, 0.90, 0.95, 0.97):
        v[f"Y_cap{cap:.2f}"] = {"g": "Y", "cap": cap}
    for h in (14, 16, 18, 20, 22):
        for D in (0, 10):
            for P in (0, 1):
                for cap in (0.85, 0.90, 0.95):
                    v[f"N_h{h}_D{D}_P{P}_cap{cap:.2f}"] = {"g": "N", "h": h, "D": D, "P": P, "cap": cap}
    for h in (16, 18, 20, 22):
        for cap in (0.90, 0.97):
            v[f"T_h{h}_cap{cap:.2f}"] = {"g": "T", "h": h, "cap": cap}
    # price-floor slice chosen on discovery (frozen candidate C2; counted in VARIANTS_EXAMINED)
    v["N_h16_band0.80-0.95"] = {"g": "N", "h": 16, "D": 0, "P": 0, "cap": 0.95, "floor": 0.80}
    return v


FROZEN = ["N_h16_D0_P0_cap0.90", "N_h16_band0.80-0.95", "N_h14_D10_P0_cap0.90"]
VARIANTS_EXAMINED = 88          # 72 grid + Y price diagnostic + 8 region + 6 band slices + 1 pooled-hours check
K_LAB = 1186 + VARIANTS_EXAMINED  # lab-wide cells (data/kalshi_lab/K.json) plus this family


def candles() -> dict[str, list]:
    c = {}
    if DISK.exists():
        for l in DISK.open():
            x = json.loads(l); c[x["t"]] = x["c"]
    c.update(load_candles())
    return c


def station_day(m: dict) -> tuple[str | None, dt.date]:
    code = m["t"].split("-")[-1] if m["series"] == "KXRAIN" else "NYC"
    return CODE2STN.get(code), dt.datetime.strptime(m["t"].split("-")[1], "%y%b%d").date()


def mk_trade(m, name, side, t_dec, px, cs):
    won = (m["result"] == "yes") == (side == "YES")
    vol60 = sum(r[5] for r in cs if t_dec - 1800 <= r[0] <= t_dec + 1800)
    return {"v": name, "t": m["t"], "e": m["e"], "t_close": m["close"], "t_dec": t_dec, "side": side, "px": px, "won": won,
            "ret": ((1.0 if won else 0.0) - px - fee10(px)) / px, "vol60": vol60, "mvol": m["vol"]}


def trades_for(m: dict, cs: list, V: dict) -> list[dict]:
    stn, day = station_day(m)
    if not stn:
        return []
    kind = "meas" if m["series"] == "KXRAIN" else "any"
    lo = m["close"] - WINDOW_H * 3600
    out = []
    # Y
    fm = first_time(stn, day, kind)
    if fm is not None and lo <= fm and fm + 60 < m["close"]:
        q = quote(cs, fm + 60)
        if q:
            ask = q[0]
            for name, p in V.items():
                if p["g"] == "Y" and 0.02 <= ask <= p["cap"]:
                    out.append(mk_trade(m, name, "YES", fm, ask, cs))
    # N and T
    for h in (14, 16, 18, 20, 22):
        t = local_ts(stn, day, h)
        if t < lo or t + 60 >= m["close"]:
            continue
        s = state(stn, day, t)
        q = quote(cs, t + 60)
        if not q:
            continue
        no = round(1 - q[1], 4)
        base_n = (not s["any"] if kind == "any" else not s["meas"]) and s["n3"] >= 2 and not s["unsettled3"]
        base_t = kind == "meas" and s["any"] and not s["meas"] and s["n2"] >= 1 and not s["wet2"]
        for name, p in V.items():
            if p.get("h") != h or not (p.get("floor", 0.30) <= no <= p["cap"]):
                continue
            if p["g"] == "N" and base_n:
                if p["D"] and (s["dd"] is None or s["dd"] < p["D"]):
                    continue
                if p["P"] and (s["dp3"] is None or s["dp3"] < 0):
                    continue
                out.append(mk_trade(m, name, "NO", t, no, cs))
            elif p["g"] == "T" and base_t:
                out.append(mk_trade(m, name, "NO", t, no, cs))
    return out


def stats(rows: list[dict]) -> dict:
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em); mean = st.mean(r["ret"] for r in rows)
    sd = st.pstdev(em) if n_e > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(n_e)) if n_e > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    mid = sorted(r["t_close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
    days = (max(r["t_close"] for r in rows) - min(r["t_close"] for r in rows)) / 86400 + 1
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "avg_px": st.mean(r["px"] for r in rows),
            "ret": mean, "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
            "trades_per_day": len(rows) / days, "med_vol60": st.median(r["vol60"] for r in rows)}


def split_events(mk: dict) -> tuple[set, set]:
    ev = defaultdict(list)
    for m in mk.values():
        if m["series"] == "KXRAIN":
            ev[m["e"]].append(m["close"])
    es = sorted(ev, key=lambda e: min(ev[e]))
    return set(es[:N_DISC]), set(es[N_DISC:])


def build(series: str, events: set | None, V: dict) -> tuple[list[dict], int]:
    mk = load_markets(); cd = candles(); out = []; n_m = 0
    for m in mk.values():
        if m["series"] != series or (events is not None and m["e"] not in events) or m["t"] not in cd:
            continue
        n_m += 1
        out.extend(trades_for(m, cd[m["t"]], V))
    return out, n_m


def fmt(name, v):
    return (f"{name:28s} n={v['n']:4d} ev={v['events']:3d} win={v['win']:.0%} px={v['avg_px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} "
            f"wo3={v['ret_wo3']:+.1%} h={v['half1']:+.1%}/{v['half2']:+.1%} /day={v['trades_per_day']:.1f} vol60={v['med_vol60']:.0f}")


def discovery() -> None:
    mk = load_markets(); disc, _ = split_events(mk); V = variants()
    T, n_m = build("KXRAIN", disc, V)
    by = defaultdict(list)
    for r in T:
        by[r["v"]].append(r)
    print(f"DISCOVERY: {len(disc)} events, {n_m} markets with candles, {len(T)} trades, variants {len(V)}")
    res = {}
    for name in V:
        rows = by.get(name, [])
        if len(rows) >= 3:
            res[name] = stats(rows); print(fmt(name, res[name]))
        else:
            print(f"{name:28s} n={len(rows)}")
    (OUT / "discovery.json").write_text(json.dumps(res, indent=1))
    (OUT / "discovery_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in T))


# archived KXRAIN dates (07-15..08-07): second holdout after the freeze (amendment 2)


def evaluate(names: list[str], sample: str) -> dict:
    """sample: 'validation' (last 24 KXRAIN events), 'archive' (archived KXRAIN dates, holdout), 'nyc' (KXRAINNYC robustness)."""
    mk = load_markets(); disc, val = split_events(mk); V = {k: v for k, v in variants().items() if k in names}
    if sample == "validation":
        T, n_m = build("KXRAIN", val, V)
    elif sample == "archive":
        arch = {e for e in disc if dt.datetime.strptime(e.split("-")[1], "%y%b%d").date() <= dt.date(2026, 8, 7)}
        T, n_m = build("KXRAIN", arch, V)
    else:
        T, n_m = build("KXRAINNYC", None, V)
    z = NormalDist().inv_cdf(1 - 0.05 / K_LAB); zf = NormalDist().inv_cdf(1 - 0.05 / VARIANTS_EXAMINED)
    print(f"{sample.upper()}: markets with candles {n_m}, trades {len(T)}; gate bar t>={z:.2f} (lab K={K_LAB}); family-only bar {zf:.2f}")
    res = {}
    for name in names:
        rows = [r for r in T if r["v"] == name]
        if len(rows) >= 3:
            res[name] = stats(rows); print(fmt(name, res[name]))
        else:
            print(name, "n =", len(rows)); res[name] = {"n": len(rows)}
    (OUT / f"{sample}.json").write_text(json.dumps(res, indent=1))
    (OUT / f"{sample}_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in T))
    return res


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "discovery":
        discovery()
    elif a[0] in ("validation", "archive", "nyc"):
        evaluate(a[1:] or FROZEN, a[0])
