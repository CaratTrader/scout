"""KXAAAGASD ("US average regular gas price above $X on day D+1 per AAA", 17 half-cent strikes, trading on day D
until 23:59 ET, i.e. before AAA publishes D+1). Retail gas prices trend: daily changes are strongly autocorrelated.
Momentum model, fitted walk-forward (only days before the decision day): change(D+1) ~ N(phi * change(D), sigma^2),
phi and sigma by least squares on the past changes (min MIN_HIST of them). AAA values are recovered from settled
strikes (highest YES strike < value <= lowest NO strike, half-cent resolution).
Trade: at the decision time, buy the side whose model probability beats its taker price by >= EDGE, one trade per
strike. Exploratory family (counted in K). Usage: python -m lab.kalshi.gas [edge] [minutes_before_close]"""
from __future__ import annotations
import collections, datetime as dt, json, math, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.kalshi.calib import quote, fee, cell_stats

ROOT = Path("data/kalshi_lab"); MIN_HIST = 8


def values() -> dict[dt.date, float]:
    ms = [json.loads(l) for l in (ROOT / "markets" / "KXAAAGASD.jsonl").open()]
    by = collections.defaultdict(list)
    for m in ms:
        by[m["e"]].append(m)
    out = {}
    for e, v in by.items():
        ys = [m["floor"] for m in v if m["result"] == "yes"]; ns = [m["floor"] for m in v if m["result"] == "no"]
        if ys and ns and min(ns) - max(ys) <= 0.0051:
            out[dt.datetime.strptime(e.split("-")[1], "%y%b%d").date()] = (max(ys) + min(ns)) / 2
    return out


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def main(edge: float = 0.10, tau: int = 240) -> None:
    V = values(); days = sorted(V)
    ms = {json.loads(l)["t"]: json.loads(l) for l in (ROOT / "markets" / "KXAAAGASD.jsonl").open()}
    cd = {json.loads(l)["t"]: json.loads(l)["c"] for l in (ROOT / "candles" / "KXAAAGASD.jsonl").open()}
    trades = []
    for i, d1 in enumerate(days):
        d0 = d1 - dt.timedelta(days=1); dm = d1 - dt.timedelta(days=2)
        if d0 not in V or dm not in V:
            continue
        past = [(V[a] - V[a - dt.timedelta(days=1)], V[a + dt.timedelta(days=1)] - V[a]) for a in days
                if a + dt.timedelta(days=1) < d1 and a - dt.timedelta(days=1) in V and a + dt.timedelta(days=1) in V]
        if len(past) < MIN_HIST:
            continue
        sxx = sum(x * x for x, _ in past)
        phi = sum(x * y for x, y in past) / sxx if sxx > 0 else 0.0
        sig = max(0.002, math.sqrt(sum((y - phi * x) ** 2 for x, y in past) / len(past)))
        mu = V[d0] + phi * (V[d0] - V[dm])
        for t, m in ms.items():
            if m["e"] != f"KXAAAGASD-{d1.strftime('%y%b%d').upper()}" or t not in cd:
                continue
            q = quote(cd[t], m["close"] - tau * 60 + 60)
            if not q:
                continue
            ask, bid = q; p = 1 - ncdf((m["floor"] - mu) / sig); won = m["result"] == "yes"
            if 0.02 <= ask <= 0.98 and p - ask >= edge:
                trades.append({"e": m["e"], "t_close": m["close"], "side": "YES", "px": ask, "won": won, "ret": ((1.0 if won else 0.0) - ask - fee(ask)) / ask, "p": p})
            elif 0.02 <= 1 - bid <= 0.98 and (1 - p) - (1 - bid) >= edge:
                px = 1 - bid; trades.append({"e": m["e"], "t_close": m["close"], "side": "NO", "px": px, "won": not won, "ret": ((0.0 if won else 1.0) - px - fee(px)) / px, "p": 1 - p})
    if not trades:
        print("no trades"); return
    v = cell_stats(trades)
    print(f"gas momentum edge>={edge:.2f} at close-{tau}m: n={v['n']} days={v['events']} win={v['win']:.0%} px={v['px']:.3f} ret/$={v['ret']:+.1%} t(day-clustered)={v['t']:.2f} wo3={v['ret_wo3']:+.1%}")
    for side in ("YES", "NO"):
        x = [r for r in trades if r["side"] == side]
        if x:
            s = cell_stats(x); print(f"   {side}: n={s['n']} win={s['win']:.0%} px={s['px']:.3f} ret/$={s['ret']:+.1%}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.10, int(sys.argv[2]) if len(sys.argv) > 2 else 240)
