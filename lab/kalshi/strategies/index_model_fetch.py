"""index_model: extend the hourly index ladders (KXINXU / KXNASDAQ100U) back in time, cheaply.

1. Settled markets with close in [from, to) via /markets?series_ticker=..&status=settled&min_close_ts&max_close_ts
   -> data/kalshi_lab/strategies/index_model/markets_<S>.jsonl (same fields as data/kalshi_lab/markets).
2. 1-minute candles over [close - 66 min, close] for the strikes within +-BAND of the index level known 65 minutes
   before the close (Yahoo 5-minute bars; ES/NQ futures scaled by the last cash/futures ratio when the cash index is
   not trading yet). The strike band is fixed before the decision window starts, so the selection uses no future data.
   Consecutive events of a day share one /markets/candlesticks call while markets x span-minutes <= 9500.
   -> data/kalshi_lab/strategies/index_model/candles_<S>.jsonl (same format as data/kalshi_lab/candles).
Every Kalshi call is counted in data/kalshi_lab/strategies/index_model/kalshi_calls.json.
Usage: python -m lab.kalshi.strategies.index_model_fetch SERIES YYYY-MM-DD YYYY-MM-DD [band] [max_calls]"""
from __future__ import annotations
import bisect, datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import K
from lab.kalshi.fetch import compact, ts
from lab.kalshi.strategies.index_model_book import kcall

OUT = Path("data/kalshi_lab/strategies/index_model"); Y5 = OUT / "yahoo5m"
CASH = {"KXINXU": ("GSPC", "ES_F"), "KXNASDAQ100U": ("NDX", "NQ_F")}
WIN = 66            # minutes of candles before the close
MAX_CANDLES = 9500


def load_bars(name: str) -> tuple[list[int], list[list[float]]]:
    d = json.loads((Y5 / f"{name}.json").read_text()); k = sorted(int(x) for x in d)
    return k, [d[str(x)] for x in k]


def level_at(series: str, t: int) -> float | None:
    """Index level known at time t: close of the last complete 5-minute cash bar, else futures x (cash/futures) ratio."""
    ck, cv = BARS[CASH[series][0]]; fk, fv = BARS[CASH[series][1]]
    i = bisect.bisect_right(ck, t - 300) - 1          # bar starting <= t-300 is complete at t
    if i >= 0 and t - ck[i] <= 600:
        return cv[i][3]
    j = bisect.bisect_right(fk, t - 300) - 1
    if j < 0 or i < 0:
        return None
    jc = bisect.bisect_right(fk, ck[i]) - 1             # futures bar at the last cash bar
    if jc < 0:
        return None
    return fv[j][3] * cv[i][3] / fv[jc][3]


BARS: dict = {}


def list_settled(series: str, t0: int, t1: int) -> list[dict]:
    out = []; cursor = ""
    while True:
        d = kcall(f"{K}/markets?series_ticker={series}&status=settled&min_close_ts={t0}&max_close_ts={t1}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        for m in d.get("markets") or []:
            if m.get("result") not in ("yes", "no"):
                continue
            out.append({"t": m["ticker"], "e": m["event_ticker"], "series": series, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
                        "exp": ts(m.get("expected_expiration_time")), "result": m["result"], "type": m.get("strike_type"), "floor": m.get("floor_strike"),
                        "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"), "vol": float(m.get("volume_fp") or m.get("volume") or 0)})
        cursor = d.get("cursor") or ""
        print(series, "listed", len(out), flush=True)
        if not cursor or not d.get("markets"):
            return out


def run(series: str, d0: dt.date, d1: dt.date, band: float, max_calls: int) -> None:
    for n in CASH[series]:
        BARS[n] = load_bars(n)
    mf = OUT / f"markets_{series}.jsonl"; cf = OUT / f"candles_{series}.jsonl"
    t0 = int(dt.datetime(d0.year, d0.month, d0.day, tzinfo=dt.timezone.utc).timestamp())
    t1 = int(dt.datetime(d1.year, d1.month, d1.day, tzinfo=dt.timezone.utc).timestamp())
    known = {json.loads(l)["t"]: json.loads(l) for l in mf.open()} if mf.exists() else {}
    if not any(t0 <= m["close"] < t1 for m in known.values()):
        for m in list_settled(series, t0, t1):
            known[m["t"]] = m
        mf.write_text("".join(json.dumps(m) + "\n" for m in sorted(known.values(), key=lambda m: (m["close"] or 0, m["t"]))))
    have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
    ev = defaultdict(list)
    for m in known.values():
        if t0 <= m["close"] < t1 and m["t"] not in have and m["floor"] is not None:
            ev[(m["close"], m["e"])].append(m)
    # strikes inside the band around the level known WIN minutes before the close
    todo = []
    for (c, e), ms in sorted(ev.items()):
        s0 = level_at(series, c - WIN * 60)
        if s0 is None:
            print("no level", e); continue
        sel = [m for m in ms if abs(m["floor"] / s0 - 1) <= band]
        if sel:
            todo.append((c, e, sel))
    print(series, "events", len(todo), "markets", sum(len(x[2]) for x in todo), flush=True)
    calls = 0; i = 0
    with cf.open("a") as fh:
        while i < len(todo) and calls < max_calls:
            grp = [todo[i]]
            while i + len(grp) < len(todo):
                nxt = todo[i + len(grp)]
                same_day = dt.datetime.utcfromtimestamp(nxt[0]).date() == dt.datetime.utcfromtimestamp(grp[0][0]).date()
                span = (nxt[0] - (grp[0][0] - WIN * 60)) // 60 + 1
                if not same_day or (sum(len(g[2]) for g in grp) + len(nxt[2])) * span > MAX_CANDLES:
                    break
                grp.append(nxt)
            lo = grp[0][0] - WIN * 60; hi = grp[-1][0]
            tick = [m["t"] for g in grp for m in g[2]]
            d = kcall(f"{K}/markets/candlesticks?market_tickers={','.join(tick)}&start_ts={lo}&end_ts={hi}&period_interval=1")
            calls += 1
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            if not got:
                print("empty response", grp[0][1], flush=True); i += len(grp); continue
            for c, e, sel in grp:
                for m in sel:
                    rows = [r for r in compact(got.get(m["t"], [])) if c - WIN * 60 <= r[0] <= c + 60]
                    fh.write(json.dumps({"t": m["t"], "c": rows}) + "\n")
            print(series, "call", calls, [g[1] for g in grp], len(tick), flush=True)
            i += len(grp)


if __name__ == "__main__":
    a = sys.argv[1:]
    run(a[0], dt.date.fromisoformat(a[1]), dt.date.fromisoformat(a[2]), float(a[3]) if len(a) > 3 else 0.005, int(a[4]) if len(a) > 4 else 999)
