"""Does the NWS afternoon Daily Climate Report (issued ~16:25 EDT Miami, ~16:35 EDT NYC, ~16:35 CDT Chicago,
~17:25 PDT SF, ~18:35 PDT LA) let us act earlier than the 00Z METAR group?
For each city-day: parse the intraday issuance (VALID TODAY AS OF hh:mm, TODAY MAXIMUM x), compare with the final
official high, check the METAR peak state at issuance time, and simulate R1c = R1x triggered at issuance+2 min
when the peak has passed. Usage: python -m lab.us.cli_backtest"""
from __future__ import annotations
import datetime as dt, json, re, statistics as st, zoneinfo
from collections import defaultdict
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import lab.us.temp_backtest as B

CLI_DIR = Path("data/lab/us/cli_intraday"); PIL = {"mia": "CLIMIA", "nyc": "CLINYC", "mdw": "CLIMDW", "sfo": "CLISFO", "lax": "CLILAX"}
MON = {m: i for i, m in enumerate(["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}


def parse_cli(text: str) -> dict | None:
    m = re.search(r"SUMMARY FOR (\w+) (\d{1,2}) (\d{4})", text)
    v = re.search(r"VALID (TODAY|YESTERDAY)? ?AS OF (\d{3,4}) (AM|PM)", text)
    mx = re.search(r"\n\s*MAXIMUM\s+(-?\d+)R?\s+(\d{1,2}:\d{2} [AP]M)?", text)
    if not (m and mx):
        return None
    day = dt.date(int(m.group(3)), MON.get(m.group(1).upper(), 0) or 1, int(m.group(2)))
    asof = None
    if v:
        hhmm = v.group(2).zfill(4); h = int(hhmm[:2]) % 12 + (12 if v.group(3) == "PM" else 0); asof = h * 60 + int(hhmm[2:])
    return {"day": day.isoformat(), "asof_min": asof, "today": bool(v and v.group(1) == "TODAY") or ("TODAY" in text.split("TEMPERATURE")[1][:80] if "TEMPERATURE" in text else False),
            "max": float(mx.group(1)), "max_time": mx.group(2)}


def intraday_reports() -> dict[tuple[str, str], dict]:
    """(city, day) -> earliest intraday issuance that reports TODAY's maximum."""
    out: dict[tuple[str, str], dict] = {}
    for f in sorted(CLI_DIR.glob("CLI*_*.txt")):
        pil, day, iss = f.stem.split("_"); city = next(c for c, p in PIL.items() if p == pil)
        rep = parse_cli(f.read_text())
        if not rep or not rep["today"] or rep["day"] != day or rep["asof_min"] is None:
            continue
        key = (city, day)
        if key not in out or rep["asof_min"] < out[key]["asof_min"]:
            out[key] = {**rep, "issued": iss}
    return out


def main() -> None:
    reps = intraday_reports(); cli = json.load(open("data/lab/us/asos/cli_high.json")); c2s = {"sfo": "KSFO", "lax": "KLAX", "mdw": "KMDW", "nyc": "KNYC", "mia": "KMIA"}
    MET = B.metar(); lad = json.load(open("data/lab/us/temp_ladders.json"))
    print(f"intraday reports parsed: {len(reps)} city-days")
    eq = defaultdict(lambda: [0, 0]); eq_peak = defaultdict(lambda: [0, 0]); asof = defaultdict(list); trades = []
    for (city, day), r in reps.items():
        H = cli[c2s[city]].get(day)
        if H is None:
            continue
        asof[city].append(r["asof_min"])
        same = r["max"] == H
        eq[city][0] += 1; eq[city][1] += same
        obs = MET.get(B.STN[city], {}).get(day) or []
        past = [o for o in obs if o[0] <= r["asof_min"]]
        if not past:
            continue
        M, tM = B.robust_max(past); T = past[-1][1]
        peak = (r["asof_min"] >= 15 * 60) and (M - T) >= 2 and (r["asof_min"] - tM) >= 60
        if peak:
            eq_peak[city][0] += 1; eq_peak[city][1] += same
        # simulate R1c at issuance + 2 min (decision grid: first stored quote after that minute)
        mk = lad.get(f"{city}:{day}")
        if not mk or not peak:
            continue
        tz = zoneinfo.ZoneInfo(B.TZ[city]); t = r["asof_min"] + 2; rM = int(r["max"] + 0.5)
        for m in mk:
            try:
                pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except Exception:
                continue
            if len(pr) != 2 or pr[0] == pr[1]:
                continue
            won = pr[0] > pr[1]; lo, hi = B.bounds(m["slug"]); q = B.quote_at(B.series(m["slug"], tz), t)
            if not q:
                continue
            ask, bid = q; no_ask = 1 - bid
            if lo <= rM <= hi and 0.02 <= ask <= 0.90:
                trades.append({"city": city, "day": day, "side": "YES", "px": ask, "pnl": (1 if won else 0) - ask - B.fee(ask), "won": won, "t": t})
            elif not (lo <= rM <= hi) and bid >= 0.10 and 0.02 <= no_ask <= 0.97:
                trades.append({"city": city, "day": day, "side": "NO", "px": no_ask, "pnl": (0 if won else 1) - no_ask - B.fee(no_ask), "won": not won, "t": t})
    print("\ncity  issued (median local)  intraday max == final high | when METAR says the peak has passed")
    for c in ("mia", "nyc", "mdw", "sfo", "lax"):
        n, k = eq[c]; n2, k2 = eq_peak[c]; a = sorted(asof[c])
        if n:
            print(f"{c:4s}  {a[len(a)//2]//60:02d}:{a[len(a)//2]%60:02d}                 {k}/{n} = {k/n:.0%}                    {k2}/{n2} = {(k2/n2 if n2 else 0):.0%}")
    if trades:
        p = [x["pnl"] for x in trades]; t_ = st.mean(p) / st.pstdev(p) * len(p) ** .5 if st.pstdev(p) else float("nan")
        print(f"\nR1c (R1x triggered by the intraday report + peak passed): n={len(trades)} win={sum(x['won'] for x in trades)/len(trades):.0%} avg px={st.mean(x['px'] for x in trades):.2f} pnl/sh={st.mean(p)*100:+.1f}c per$={sum(p)/sum(x['px'] for x in trades):+.2f} t={t_:.1f}")
        for c in ("mia", "nyc", "mdw", "sfo", "lax"):
            v = [x for x in trades if x["city"] == c]
            if v: print(f"   {c}: n={len(v)} win={sum(x['won'] for x in v)/len(v):.0%} pnl/sh={st.mean(x['pnl'] for x in v)*100:+.1f}c")
        json.dump(trades, open("data/lab/us/cli_trades.json", "w"))


if __name__ == "__main__":
    main()
