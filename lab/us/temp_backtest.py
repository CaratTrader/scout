"""Backtest of the observed-max temperature lock on Polymarket US daily-high ladders.

Data: METAR temps (IEM, local time), ladders (all buckets + outcomes), 1-minute YES ask/bid per bucket.
Rules, evaluated on a 15-minute decision grid (12:00-22:45 local), filled at the first stored price >= t+delay:
  R0  dead buckets: bucket entirely below the running max M  -> buy NO (certain), or the open top bucket once M >= its floor -> buy YES (certain)
  R1  winner lock : after the peak (t >= 15:00, M - T >= F, >= 45 min since M) buy YES on the bucket that contains M
                     (margin: only if M+1 is in the same bucket, because the official report can exceed hourly METAR by ~1F)
  R2  fade        : same trigger; buy NO on buckets whose floor >= M+2 while their YES bid is still >= 0.15
Fees: taker 0.0695*p*(1-p) per share. One trade per bucket per rule per day. Truth = market resolution.
Usage: python -m lab.us.temp_backtest [delay_min]
"""
from __future__ import annotations
import csv, datetime as dt, json, math, re, statistics as st, sys, zoneinfo
from collections import defaultdict
from pathlib import Path

TZ = {"sfo": "America/Los_Angeles", "lax": "America/Los_Angeles", "mdw": "America/Chicago", "nyc": "America/New_York", "mia": "America/New_York"}
STN = {"sfo": "SFO", "lax": "LAX", "mdw": "MDW", "nyc": "NYC", "mia": "MIA"}
FEE = 0.0695
USE_6HR = True
def fee(p): return FEE * p * (1 - p)

def bounds(slug: str) -> tuple[float, float]:
    """Venue slug convention (verified on 2,543 resolved markets against the NWS report): "gte68lt69f" is the
    68-69F bucket (inclusive), the bottom "lt68f" is 67F or below (strict), the top "gte76f" is 76F or more."""
    lo = re.search(r"gte(\d+)", slug); hi = re.search(r"lt(\d+)", slug)
    if lo and hi:
        return float(lo.group(1)), float(hi.group(1))
    if hi:
        return -1e9, float(hi.group(1)) - 1
    return (float(lo.group(1)) if lo else -1e9), 1e9

def _six_hour_max_f(raw: str) -> float | None:
    """METAR remark 1sTTT = 6-hour maximum temperature in tenths C (s=1 negative). Reported at 00/06/12/18Z from
    continuous sensing, so it is the true maximum of the period, unlike the hourly reading."""
    m = re.search(r"\bRMK\b.*?\b1([01])(\d{3})\b", raw or "")
    if not m:
        return None
    c = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1)
    return round(c * 9 / 5 + 32)


def metar() -> dict[str, dict[str, list[tuple[int, float]]]]:
    """station -> local day -> sorted (minute_of_day, tmpf). Observations before 01:00 local are dropped: the NWS
    climate day runs midnight-to-midnight local *standard* time, i.e. 01:00-01:00 during daylight saving (all of
    Apr-Sep). If the raw METAR text is available, the 6-hour maximum group is added as an extra observation."""
    out: dict[str, dict[str, list[tuple[int, float]]]] = defaultdict(lambda: defaultdict(list))
    for st_ in STN.values():
        f = Path(f"data/lab/us/asos_raw/{st_}.csv")
        if not f.exists():
            f = Path(f"data/lab/us/asos/{st_}.csv")
        if not f.exists():
            continue
        for r in csv.DictReader(open(f)):
            if r.get("tmpf") in (None, "M", ""):
                continue
            day, hm = r["valid"][:10], r["valid"][11:16]
            minute = int(hm[:2]) * 60 + int(hm[3:])
            if minute < 60:
                continue
            out[st_][day].append((minute, float(r["tmpf"]), False))
            mx = _six_hour_max_f(r.get("metar") or "") if USE_6HR else None
            if mx is not None and minute >= 7 * 60 and mx >= float(r["tmpf"]) - 1:  # window (t-6h, t] inside the 01:00-01:00 climate day
                window = [x for m_, x, tr in out[st_][day] if not tr and minute - 360 <= m_ <= minute]
                if window and mx > max(window) + 3.0:   # a computed max far above every hourly reading in its window = sensor artefact
                    continue
                out[st_][day].append((minute, float(mx), True))
    for s in out:
        for d in out[s]:
            out[s][d].sort()
    return out

def series(slug: str, tz: zoneinfo.ZoneInfo) -> list[tuple[int, float, float]]:
    f = Path(f"data/lab/us/temp_series/{slug}.json")
    if not f.exists():
        return []
    h = json.loads(f.read_text()).get("history") or []
    out = []
    for p in h:
        lt = dt.datetime.fromtimestamp(p["timestamp"], tz); out.append((lt.hour * 60 + lt.minute, float(p["longPrice"]), 1 - float(p["shortPrice"])))
    return out

def robust_max(past: list[tuple], tol: float = 2.5) -> tuple[float, int]:
    """Highest reading that is either a 6-hour maximum group (trusted) or an hourly reading corroborated by a
    neighbour within 90 minutes at >= reading - tol. A lone hourly spike (KNYC 2026-08-27 15:51, 80F in heavy rain
    with the maintenance flag, neighbours 76-77, official high 77) is ignored; sparse data is accepted as is."""
    for row in sorted(past, key=lambda r: (-r[1], -r[0])):
        m, x = row[0], row[1]; trusted = len(row) > 2 and row[2]
        neigh = [r[1] for r in past if r[0] != m and abs(r[0] - m) <= 90]
        if trusted or not neigh or max(neigh) >= x - tol:
            return x, m
    row = past[-1]
    return row[1], row[0]


def quote_at(ser, minute):  # first stored observation at or after `minute` (we act on what the book shows after our delay)
    for m, a, b in ser:
        if m >= minute:
            return a, b
    return None

def main():
    delay = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    F_LIST = (1, 2, 3); cap = 0.90
    lp = Path("data/lab/us/temp_ladders.json")
    if lp.exists():
        lad = json.load(open(lp))
    else:  # fall back to the crawl while the ladder fetch is still running
        lad = defaultdict(list)
        for m in json.load(open("data/lab/us/markets_all.json")):
            if m.get("category") == "climate" and m["slug"].startswith("tc-temp-"):
                p_ = m["slug"].split("-"); lad[f"{p_[2][:3]}:{'-'.join(p_[3:6])}"].append(m)
    MET = metar(); dmax = json.load(open("data/lab/us/asos/cli_high.json"))
    dmax = {{"KSFO": "SFO", "KLAX": "LAX", "KMDW": "MDW", "KNYC": "NYC", "KMIA": "MIA"}[k]: v for k, v in dmax.items()}
    trades = []; gap = []; days_used = 0; days_skipped = defaultdict(int)
    for key, mk in lad.items():
        city, day = key.split(":"); stn = STN[city]; tz = zoneinfo.ZoneInfo(TZ[city])
        obs = MET.get(stn, {}).get(day)
        if not obs or len(obs) < 12:
            days_skipped["no_metar"] += 1; continue
        buckets = []
        for m in mk:
            try:
                pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except Exception:
                pr = []
            if len(pr) != 2 or pr[0] == pr[1]:
                continue
            lo, hi = bounds(m["slug"]); buckets.append({"slug": m["slug"], "lo": lo, "hi": hi, "won": pr[0] > pr[1], "ser": series(m["slug"], tz)})
        if not buckets or not any(b["won"] for b in buckets):
            days_skipped["no_resolution"] += 1; continue
        if not any(b["ser"] for b in buckets):
            days_skipped["no_prices"] += 1; continue
        days_used += 1
        final_M = max(r[1] for r in obs); truth = dmax.get(stn, {}).get(day)
        if truth is not None:
            gap.append(truth - final_M)
        done = set()
        for t in range(12 * 60, 23 * 60, 15):
            past = [r for r in obs if r[0] <= t]
            if not past:
                continue
            M, tM = robust_max(past); T = min(r[1] for r in past if r[0] == past[-1][0] and not (len(r) > 2 and r[2])) if any(not (len(r) > 2 and r[2]) for r in past if r[0] == past[-1][0]) else past[-1][1]
            peak_passed = t >= 15 * 60 and (M - T) >= 1 and (t - tM) >= 45
            for b in buckets:
                q = quote_at(b["ser"], t + delay)
                if not q:
                    continue
                ask, bid = q; no_ask = 1 - bid
                # R0 certain trades
                if ("R0", b["slug"]) not in done:
                    if b["hi"] < M and no_ask <= 0.97 and no_ask >= 0.02:
                        done.add(("R0", b["slug"])); trades.append({"rule": "R0", "F": 0, "city": city, "day": day, "side": "NO", "px": no_ask, "pnl": (0 if b["won"] else 1) - no_ask - fee(no_ask), "won": not b["won"], "t": t, "slug": b["slug"]})
                    elif b["hi"] >= 1e8 and M >= b["lo"] and ask <= 0.97 and ask >= 0.02:
                        done.add(("R0", b["slug"])); trades.append({"rule": "R0", "F": 0, "city": city, "day": day, "side": "YES", "px": ask, "pnl": (1 if b["won"] else 0) - ask - fee(ask), "won": b["won"], "t": t, "slug": b["slug"]})
                z00 = {"sfo": 17, "lax": 17, "mdw": 19, "nyc": 20, "mia": 20}[city] * 60 + 5   # first grid minute after the 00Z report
                if USE_6HR and t >= z00:
                    rM = int(M + 0.5)
                    if ("R1x", b["slug"]) not in done:
                        if b["lo"] <= rM <= b["hi"] and 0.02 <= ask <= 0.90:
                            done.add(("R1x", b["slug"])); trades.append({"rule": "R1x", "F": 0, "city": city, "day": day, "side": "YES", "px": ask, "pnl": (1 if b["won"] else 0) - ask - fee(ask), "won": b["won"], "t": t, "slug": b["slug"]})
                        elif not (b["lo"] <= rM <= b["hi"]) and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                            done.add(("R1x", b["slug"])); trades.append({"rule": "R1x", "F": 0, "city": city, "day": day, "side": "NO", "px": no_ask, "pnl": (0 if b["won"] else 1) - no_ask - fee(no_ask), "won": not b["won"], "t": t, "slug": b["slug"]})
                for F in F_LIST:
                    trig = peak_passed and (M - T) >= F
                    if not trig:
                        continue
                    if ("R1", F, b["slug"]) not in done and b["lo"] <= M <= b["hi"] and (M + 1 <= b["hi"] or b["hi"] >= 1e8) and 0.02 <= ask <= cap:
                        done.add(("R1", F, b["slug"])); trades.append({"rule": "R1", "F": F, "city": city, "day": day, "side": "YES", "px": ask, "pnl": (1 if b["won"] else 0) - ask - fee(ask), "won": b["won"], "t": t, "slug": b["slug"]})
                    for Gm in (2, 3, 4):
                        if ("R2", F, Gm, b["slug"]) not in done and b["lo"] >= M + Gm and bid >= 0.15 and no_ask >= 0.02:
                            done.add(("R2", F, Gm, b["slug"])); trades.append({"rule": "R2", "F": F, "G": Gm, "city": city, "day": day, "side": "NO", "px": no_ask, "pnl": (0 if b["won"] else 1) - no_ask - fee(no_ask), "won": not b["won"], "t": t, "slug": b["slug"]})
    print(f"city-days used {days_used}, skipped {dict(days_skipped)}; delay {delay} min")
    if gap:
        c = defaultdict(int)
        for g in gap: c[int(round(g))] += 1
        print("official daily max minus hourly-METAR max (F):", dict(sorted(c.items())))
    def rep(rows, label):
        if not rows: print(f"{label:22s} n=0"); return
        p = [r["pnl"] for r in rows]; px = [r["px"] for r in rows]; stake = sum(px); win = sum(1 for r in rows if r["won"]) / len(rows)
        tst = st.mean(p) / st.pstdev(p) * math.sqrt(len(p)) if len(p) > 2 and st.pstdev(p) > 0 else float("nan")
        print(f"{label:22s} n={len(rows):4d} win={win:4.0%} avg px={st.mean(px):.3f} pnl/share={st.mean(p)*100:+6.1f}c per$={sum(p)/stake:+.3f} t={tst:5.1f} trades/day={len(rows)/max(days_used,1):.2f}")
    print("\nRULE RESULTS (P&L per share after taker fee; per$ = profit per dollar staked)")
    rep([r for r in trades if r["rule"] == "R0"], "R0 certain (NO/YES)")
    rep([r for r in trades if r["rule"] == "R1x"], "R1x after 00Z max (all)")
    rep([r for r in trades if r["rule"] == "R1x" and r["side"] == "YES"], "  R1x YES winner bucket")
    rep([r for r in trades if r["rule"] == "R1x" and r["side"] == "NO"], "  R1x NO other buckets")
    for F in F_LIST:
        rep([r for r in trades if r["rule"] == "R1" and r["F"] == F], f"R1 winner lock F>={F}")
    for Gm in (2, 3, 4):
        for F in (1, 2):
            rep([r for r in trades if r["rule"] == "R2" and r["F"] == F and r.get("G") == Gm], f"R2 fade F>={F} floor>=M+{Gm}")
    print("\nR1 F>=2 by city:"); [rep([r for r in trades if r["rule"] == "R1" and r["F"] == 2 and r["city"] == c], f"  {c}") for c in TZ]
    print("R1x by month:"); [rep([r for r in trades if r["rule"] == "R1x" and r["day"][:7] == mo], f"  {mo}") for mo in sorted({r["day"][:7] for r in trades})]
    print("R0 by month:"); [rep([r for r in trades if r["rule"] == "R0" and r["day"][:7] == mo], f"  {mo}") for mo in sorted({r["day"][:7] for r in trades})]
    print("R2 F>=1 floor>=M+3 by city:"); [rep([r for r in trades if r["rule"] == "R2" and r["F"] == 1 and r.get("G") == 3 and r["city"] == c], f"  {c}") for c in TZ]
    print("R1 F>=2 by entry price bucket:"); [rep([r for r in trades if r["rule"] == "R1" and r["F"] == 2 and lo <= r["px"] < lo + 0.2], f"  px {lo:.1f}-{lo+0.2:.1f}") for lo in (0.0, 0.2, 0.4, 0.6, 0.8)]
    with open(f"data/lab/us/temp_trades_d{delay}.jsonl", "w") as fh:
        for r in trades: fh.write(json.dumps(r) + "\n")
    print("saved", f"data/lab/us/temp_trades_d{delay}.jsonl")

if __name__ == "__main__":
    main()
