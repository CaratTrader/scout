"""Kalshi daily-high temperature ladders: same observed-max rules as lab/us/temp_backtest.py, on Kalshi's 1-minute
candlesticks (yes bid/ask close per minute) and settled results. Strikes: 'less' cap X -> max <= X-1; 'between'
floor..cap inclusive; 'greater' floor X -> max >= X+1. Usage: python -m lab.us.kalshi_backtest [delay_min]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys, zoneinfo
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lab.us.temp_backtest as B
from lab.us.cli_backtest import intraday_reports

KD = Path("data/lab/us/kalshi")
SERIES = {"KXHIGHNY": ("NYC", "KNYC", "America/New_York"), "KXHIGHCHI": ("MDW", "KMDW", "America/Chicago"), "KXHIGHMIA": ("MIA", "KMIA", "America/New_York"),
          "KXHIGHLAX": ("LAX", "KLAX", "America/Los_Angeles"), "KXHIGHTSFO": ("SFO", "KSFO", "America/Los_Angeles"), "KXHIGHTBOS": ("BOS", "KBOS", "America/New_York")}
Z00 = {"NYC": 20, "MIA": 20, "BOS": 20, "MDW": 19, "LAX": 17, "SFO": 17}
FEE = 0.07
def fee(p): return FEE * p * (1 - p)


def interval(m: dict) -> tuple[float, float]:
    t = m.get("strike_type")
    if t == "less":
        return -1e9, float(m["cap_strike"]) - 1
    if t == "greater":
        return float(m["floor_strike"]) + 1, 1e9
    return float(m["floor_strike"]), float(m["cap_strike"])


def candles(ticker: str, tz: zoneinfo.ZoneInfo) -> list[tuple[int, float, float]]:
    f = KD / f"{ticker}.json"
    if not f.exists():
        return []
    out = []
    for c in json.loads(f.read_text()):
        try:
            ask = float(c["yes_ask"]["close_dollars"]); bid = float(c["yes_bid"]["close_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        lt = dt.datetime.fromtimestamp(int(c["end_period_ts"]), tz)
        out.append((lt.hour * 60 + lt.minute, ask, bid))
    return out


def quote_at(ser, minute, max_age: int = 90):
    """Candles are sparse (only minutes with activity), so a quote persists until the next candle: use the last
    candle at or before `minute`, provided it is not older than max_age minutes."""
    best = None
    for m, a, b in ser:
        if m <= minute:
            best = (m, a, b)
        else:
            break
    if best and minute - best[0] <= max_age:
        return best[1], best[2]
    return None


def main():
    delay = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    B.STN["bos"] = "BOS"; B.TZ["bos"] = "America/New_York"
    M = json.load(open(KD / "markets.json")); MET = B.metar(); cli = json.load(open("data/lab/us/asos/cli_high.json"))
    REP = intraday_reports()  # (city, day) -> {asof_min, max}; cities keyed sfo/lax/mdw/nyc/mia (+bos when archived)
    CITY = {"NYC": "nyc", "MDW": "mdw", "MIA": "mia", "SFO": "sfo", "LAX": "lax", "BOS": "bos"}
    days = defaultdict(list)
    for t, m in M.items():
        if m.get("result") not in ("yes", "no") or m["series"] not in SERIES:
            continue
        stn, icao, tzn = SERIES[m["series"]]
        # market day = local date of close_time minus 1 (close is 05:00Z next day)
        close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")); day = (close.astimezone(zoneinfo.ZoneInfo(tzn)) - dt.timedelta(hours=6)).date().isoformat()
        days[(stn, day)].append(m)
    print(f"Kalshi ladders: {len(days)} station-days, {sum(len(v) for v in days.values())} markets")
    trades = []; used = 0; skipped = defaultdict(int); truth = defaultdict(int)
    for (stn, day), mk in days.items():
        icao = next(v[1] for v in SERIES.values() if v[0] == stn); tzn = next(v[2] for v in SERIES.values() if v[0] == stn); tz = zoneinfo.ZoneInfo(tzn)
        obs = MET.get(stn, {}).get(day)
        if not obs or len(obs) < 12:
            skipped["no_metar"] += 1; continue
        # resolution check: Kalshi expiration_value vs NWS CLI high
        ev = next((float(m["expiration_value"]) for m in mk if m.get("expiration_value")), None); H = cli.get(icao, {}).get(day)
        if ev is not None and H is not None:
            truth[int(round(ev - H))] += 1
        rows = []
        for m in mk:
            ser = candles(m["ticker"], tz)
            if ser:
                lo, hi = interval(m); rows.append({"m": m, "lo": lo, "hi": hi, "won": m["result"] == "yes", "ser": ser})
        if not rows:
            skipped["no_candles"] += 1; continue
        used += 1; done = set()
        for t in range(12 * 60, 23 * 60 + 30, 15):
            past = [r for r in obs if r[0] <= t]
            if not past:
                continue
            Mx, tM = B.robust_max(past); T = min(r[1] for r in past if r[0] == past[-1][0]); rM = int(Mx + 0.5)
            peak = t >= 15 * 60 and (Mx - T) >= 1 and (t - tM) >= 45
            after00 = t >= Z00[stn] * 60 + 5 and any(len(r) > 2 and r[2] and r[0] >= Z00[stn] * 60 - 40 for r in past)
            rep = REP.get((CITY[stn], day))
            rep_in = bool(rep) and t >= rep["asof_min"] + 5
            if rep_in and rep["max"] > Mx:
                Mx = rep["max"]; rM = int(Mx + 0.5)   # the report's max-so-far is official and beats hourly METAR
            fall = Mx - T; since = t - tM
            cli_gate = rep_in and t >= 15 * 60 and fall >= 2 and since >= 60
            for b in rows:
                q = quote_at(b["ser"], t + delay)
                if not q:
                    continue
                ask, bid = q; no_ask = 1 - bid
                key = b["m"]["ticker"]
                def rec(rule, side, px, won):
                    trades.append({"rule": rule, "stn": stn, "day": day, "side": side, "px": px, "pnl": (1 if won else 0) - px - fee(px), "won": won, "t": t, "ticker": key})
                holds = b["lo"] <= rM <= b["hi"]
                if cli_gate and not after00 and ("R1c", key) not in done:
                    if holds and 0.02 <= ask <= 0.95:
                        done.add(("R1c", key)); rec("R1c", "YES", ask, b["won"])
                    elif not holds and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                        done.add(("R1c", key)); rec("R1c", "NO", no_ask, not b["won"])
                for F, S in ((1, 45), (2, 60), (3, 60)):
                    tag = f"R1_f{F}"
                    if (tag, key) not in done and t >= 15 * 60 and fall >= F and since >= S and holds and (rM + 1 <= b["hi"] or b["hi"] >= 1e8) and 0.02 <= ask <= 0.90:
                        done.add((tag, key)); rec(tag, "YES", ask, b["won"])
                if after00 and ("R1x", key) not in done:
                    if b["lo"] <= rM <= b["hi"] and 0.02 <= ask <= 0.95:
                        done.add(("R1x", key)); rec("R1x", "YES", ask, b["won"])
                    elif not (b["lo"] <= rM <= b["hi"]) and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                        done.add(("R1x", key)); rec("R1x", "NO", no_ask, not b["won"])
                if ("R0", key) not in done:
                    if b["hi"] + 0.5 <= Mx and 0.02 <= no_ask <= 0.97:
                        done.add(("R0", key)); rec("R0", "NO", no_ask, not b["won"])
                    elif b["hi"] >= 1e8 and Mx >= b["lo"] - 0.45 and 0.02 <= ask <= 0.97:
                        done.add(("R0", key)); rec("R0", "YES", ask, b["won"])
                if peak and ("R2", key) not in done and b["lo"] >= rM + 3 and bid >= 0.15 and no_ask >= 0.02:
                    done.add(("R2", key)); rec("R2", "NO", no_ask, not b["won"])
    print(f"station-days used {used}, skipped {dict(skipped)}; Kalshi settlement minus NWS CLI high: {dict(sorted(truth.items()))}; delay {delay} min")
    def rep(rows, label):
        if not rows: print(f"{label:26s} n=0"); return
        p = [r["pnl"] for r in rows]; stake = sum(r["px"] for r in rows)
        tst = st.mean(p) / st.pstdev(p) * math.sqrt(len(p)) if len(p) > 2 and st.pstdev(p) > 0 else float("nan")
        print(f"{label:26s} n={len(rows):4d} win={sum(r['won'] for r in rows)/len(rows):4.0%} avg px={st.mean(r['px'] for r in rows):.3f} pnl/sh={st.mean(p)*100:+6.1f}c per$={sum(p)/stake:+.3f} t={tst:5.1f} trades/day={len(rows)/max(used,1):.2f}")
    print("\nRULES (P&L per contract after 7% taker fee)")
    for rule in ("R0", "R1x", "R1c", "R2", "R1_f1", "R1_f2", "R1_f3"):
        rep([r for r in trades if r["rule"] == rule], rule)
        if rule in ("R1x", "R1c"):
            rep([r for r in trades if r["rule"] == rule and r["side"] == "YES"], f"  {rule} YES"); rep([r for r in trades if r["rule"] == rule and r["side"] == "NO"], f"  {rule} NO")
    print("\nR1c by station:"); [rep([r for r in trades if r["rule"] == "R1c" and r["stn"] == s], f"  {s}") for s in Z00]
    print("R1c by entry price:"); [rep([r for r in trades if r["rule"] == "R1c" and lo <= r["px"] < lo + 0.2], f"  px {lo:.1f}-{lo+0.2:.1f}") for lo in (0.0, 0.2, 0.4, 0.6, 0.8)]
    print("R1_f2 by entry price:"); [rep([r for r in trades if r["rule"] == "R1_f2" and lo <= r["px"] < lo + 0.2], f"  px {lo:.1f}-{lo+0.2:.1f}") for lo in (0.0, 0.2, 0.4, 0.6, 0.8)]
    print("\nby station (all rules):"); [rep([r for r in trades if r["stn"] == s], f"  {s}") for s in Z00]
    print("by month (all rules):"); [rep([r for r in trades if r["day"][:7] == mo], f"  {mo}") for mo in sorted({r["day"][:7] for r in trades})]
    print("by entry price (all rules):"); [rep([r for r in trades if lo <= r["px"] < lo + 0.2], f"  px {lo:.1f}-{lo+0.2:.1f}") for lo in (0.0, 0.2, 0.4, 0.6, 0.8)]
    with open(KD / f"trades_d{delay}.jsonl", "w") as fh:
        for r in trades: fh.write(json.dumps(r) + "\n")

if __name__ == "__main__":
    main()
