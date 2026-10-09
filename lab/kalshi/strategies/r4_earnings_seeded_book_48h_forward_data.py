"""Data for the r4_earnings_seeded_book_48h_forward archive check (fresh events the round-3 48 h split never saw).

The round-3 '48 h' finding (C1E fills within 48 h of posting +42%/$, later fills +7%) was seen post hoc on the 46
live-tier events 2026-08-05..10-08. Events settled before 2026-08-05 are in Kalshi's archive tier and were used by
round 3 for word base rates only (markets_hist.jsonl), never for prices. They are a fresh sample for arm B.

  plan            seeded random sample: one market per archive event (first close 2025-10-01..2026-08-04, not in the
                  round-3 live set), chosen from listing fields only -> plan.json  (no Kalshi call)
  candles N       hourly /historical/markets/{t}/candlesticks over [open - 1 h, min(first close + 1 h, open + 70 d)]
                  for planned markets (<= N calls)                         -> candles.jsonl
  trades N [B|A]  /historical/trades prints over (post + 120 s, cancel_A] for every planned order whose candles show a
                  trade-price high at or above the order level in that window (<= N calls; orders without such a
                  candle provably have zero fill, through-only or any-print) -> trades.jsonl
Every Kalshi call goes through r4_earnings_seeded_book_48h_forward_api.kget (bot-idle fetch, counted, cached).
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_data plan|candles N|trades N"""
from __future__ import annotations
import datetime as dt, json, math, random, re, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_earnings_seeded_book_48h_forward_api import kget, research_used, OUT

R3 = Path("data/kalshi_lab/strategies/r3_earnings_call_mentions")
ET = ZoneInfo("America/New_York")
SEED = 20261008
N_EVENTS = 95
FROM, TO = "2025-10-01T00:00:00Z", "2026-08-05T00:00:00Z"
MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)
POST_GAP = 120


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def h1_of(op: int) -> int:
    return int(math.ceil((op + 3600) / 3600.0) * 3600)


def ticker_date(e: str):
    g = re.search(r"-(\d\d)([A-Z]{3})(\d\d)$", e)
    return dt.date(2000 + int(g[1]), MON[g[2]], int(g[3])) if g else None


def cancel_a(e: str, first_close: int, cs: list | None = None) -> int:
    """Backtest proxy for C1E's pre-call cancel (the settled record overwrites occurrence_datetime, so the schedule a
    live logger reads is not archived). 07:00 ET on the call day D, where D = the event ticker's date if the first
    close falls 0-2 days after it, else the ET date of (first close - 6 h) (most closes come 0.5-5 h after the call).
    Guard for late closes (up to 27 h after the call): if the market's busiest hour in the 30 h before the first close
    starts before that cancel, the call is taken to start in that hour and the cancel moves to 07:00 ET of its day (or
    2 h before it if that is earlier). Never later than first close - 1 h. The guard only moves the cancel earlier.
    07:00 ET equals C1E's morning-call cancel; for afternoon calls it is 8 h before C1E's 15:00 ET (conservative)."""
    td = ticker_date(e)
    fcd = dt.datetime.fromtimestamp(first_close, ET).date()
    D = td if (td is not None and 0 <= (fcd - td).days <= 2) else dt.datetime.fromtimestamp(first_close - 6 * 3600, ET).date()
    c = int(dt.datetime(D.year, D.month, D.day, 7, 0, tzinfo=ET).timestamp())
    if cs:
        w = [x for x in cs if first_close - 30 * 3600 < x[0] <= first_close + 3600 and x[5] > 0]
        if w:
            V = max(w, key=lambda x: x[5])[0] - 3600              # start of the busiest hour
            if V < c:
                d2 = dt.datetime.fromtimestamp(V, ET).date()
                c = min(int(dt.datetime(d2.year, d2.month, d2.day, 7, 0, tzinfo=ET).timestamp()), V - 7200)
    return min(c, first_close - 3600)


def cancel_a_cov(r: dict, cs: list) -> int:
    """cancel_a, also capped at the end of the candle coverage (markets listed > 70 days before the first close are
    observed only to open + 70 d; the call is then not visible to the late-close guard)."""
    c = cancel_a(r["e"], r["first_close"], cs)
    if r["hi"] < r["first_close"] + 3600:
        c = min(c, r["hi"])
    return c


def bounds(cs: list, P: int, C: int, s: float, at: bool = False) -> tuple[float, float]:
    """Through-only (at=False: price > s; at=True: price >= s) contract volume in (P + 120 s, C] from hourly candles:
    lower bound L = volume of hours wholly inside (P + 1 h, C] whose LOW trade price qualifies (every print in them
    qualifies); upper bound U = volume of every hour overlapping (P, C] whose HIGH qualifies."""
    eps = -1e-9 if at else 1e-9
    L = U = 0.0
    for c in cs:
        if c[0] > P and c[0] - 3600 < C and c[3] is not None and c[3] > s + eps:
            U += c[5]
        if c[0] - 3600 >= P + 3600 and c[0] <= C and c[4] is not None and c[4] > s + eps:
            L += c[5]
    return L, U


def resolved(L: float, U: float, N: int) -> float | None:
    """Filled contracts min(N, through volume) when the candle bounds pin it down, else None."""
    if U == 0:
        return 0.0
    if L >= N:
        return float(N)
    if abs(L - U) < 1e-9:
        return float(min(N, L))
    return None


def plan() -> dict:
    H = [json.loads(l) for l in (R3 / "markets_hist.jsonl").open()]
    live = {json.loads(l)["event_ticker"] for l in (R3 / "markets_live.jsonl").open()}
    ev = defaultdict(list)
    for m in H:
        if m["event_ticker"].startswith("KXEARNINGSMENTION") and m["event_ticker"] not in live and m["result"] in ("yes", "no"):
            ev[m["event_ticker"]].append(m)
    elig = []
    for e, ms in ev.items():
        fc = min(ts(m["close_time"]) for m in ms)
        if ts(FROM) <= fc < ts(TO):
            elig.append(e)
    elig.sort()
    rng = random.Random(SEED)
    pick = sorted(rng.sample(elig, min(N_EVENTS, len(elig))))
    rows = []
    for e in pick:
        ms = sorted(ev[e], key=lambda m: m["ticker"])
        m = rng.choice(ms)                       # listing fields only: uniform over the event's markets
        fc = min(ts(x["close_time"]) for x in ev[e])
        op = ts(m["open_time"])
        P = h1_of(op)
        CA = cancel_a(e, fc)                     # preliminary (no candles yet); the analysis recomputes it with candles
        rows.append({"e": e, "t": m["ticker"], "series": m["series"], "word": m.get("word"), "open": op, "post": P,
                     "first_close": fc, "cancel_A": CA, "n_markets_event": len(ev[e]), "result": m["result"],
                     "lo": op // 3600 * 3600 - 3600, "hi": min(fc + 3600, op + 70 * 86400)})
    out = {"written": "plan built from listing fields only (r3 markets_hist.jsonl); seed %d" % SEED, "eligible_events": len(elig),
           "sampled": len(rows), "rows": rows}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(json.dumps(out, indent=1))
    print(f"eligible {len(elig)} sampled {len(rows)}; post after cancel_A: {sum(r['post'] >= r['cancel_A'] for r in rows)}")
    return out


def _px(side, key="close"):
    if not side:
        return None
    v = side.get(key + "_dollars", side.get(key))
    if v is None:
        return None
    v = float(v)
    return v / 100 if v > 1.0001 else v


def parse(lst: list) -> list:
    """[end_ts, ask_close, bid_close, price_high, price_low, volume, bid_high, ask_low]"""
    out = []
    for c in lst:
        pr = c.get("price") or {}
        out.append([int(c["end_period_ts"]), _px(c.get("yes_ask")), _px(c.get("yes_bid")), _px(pr, "high"), _px(pr, "low"),
                    float(c.get("volume_fp") or c.get("volume") or 0), _px(c.get("yes_bid"), "high"), _px(c.get("yes_ask"), "low")])
    return sorted(out)


def candles(max_calls: int) -> None:
    P = json.loads((OUT / "plan.json").read_text())["rows"]
    cf = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
    start = research_used()
    for r in P:
        if r["t"] in have:
            continue
        if research_used() - start >= max_calls:
            print("max_calls reached"); break
        d = kget(f"/historical/markets/{r['t']}/candlesticks?start_ts={r['lo']}&end_ts={r['hi']}&period_interval=60")
        c = parse(d.get("candlesticks") or [])
        with cf.open("a") as fh:
            fh.write(json.dumps({"t": r["t"], "e": r["e"], "ok": bool(d), "c": c}) + "\n")
        print(r["t"], len(c), "candles; research calls", research_used(), flush=True)


def quote_at(cs: list, t: int, op: int, max_age: int = 3 * 3600):
    best = None
    for c in cs:
        if c[0] <= t:
            best = c
        else:
            break
    if best is None or best[0] < op or t - best[0] > max_age:
        return None
    return best


def order_of(r: dict, cs: list, delay_h: int, CA: int) -> dict | None:
    """C1E order at post = H1 (+ delay): s = yes_ask - 0.01 if 0 < bid < ask < 1 and spread >= 2c."""
    P = r["post"] + delay_h * 3600
    if P >= CA:
        return {"skip": "post_after_cancel"}
    q = quote_at(cs, P, r["open"])
    if q is None or q[1] is None or q[2] is None:
        return {"skip": "no_quote"}
    ask, bid = q[1], q[2]
    if not (0 < bid < ask < 1) or ask - bid < 0.02 - 1e-9:
        return {"skip": "no_signal", "ask": ask, "bid": bid}
    s = round(ask - 0.01, 2)
    return {"P": P, "s": s, "N": int(5 // (1 - s)), "ask": ask, "bid": bid}


def candle_through(cs: list, P: int, C: int, s: float, at: bool = False) -> bool:
    """Any hourly candle overlapping (P, C] with a trade-price high above s (at=True: at or above s)."""
    return any(c[0] > P and c[0] - 3600 < C and c[3] is not None and c[3] > s + (-1e-9 if at else 1e-9) for c in cs)


def trades(max_calls: int, which: str = "B") -> None:
    """/historical/trades prints over (post + 120 s, cancel_A] (the live /markets/trades endpoint returns nothing for
    archive markets: 26 such calls came back empty, kept in trades_markets_endpoint_empty.jsonl). which='B' fetches
    the orders whose candles show a print at or above s inside the arm-B window first (the hypothesis), then
    which='A' the remaining orders with such a print only later in the arm-A window."""
    P = {r["t"]: r for r in json.loads((OUT / "plan.json").read_text())["rows"]}
    C = {json.loads(l)["t"]: json.loads(l)["c"] for l in (OUT / "candles.jsonl").open()}
    tf = OUT / "trades.jsonl"
    have = {json.loads(l)["t"] for l in tf.open()} if tf.exists() else set()
    start = research_used(); todo = []
    for t, r in P.items():
        cs = C.get(t)
        if not cs or t in have:
            continue
        CA = cancel_a_cov(r, cs)
        inB = inA = unres = False
        for dh in (0, 2):                         # base entry and the +2 h entry-delay variant share one window
            o = order_of(r, cs, dh, CA)
            if o and "s" in o:
                inB |= candle_through(cs, o["P"], min(o["P"] + 48 * 3600, CA), o["s"], at=True)
                inA |= candle_through(cs, o["P"], CA, o["s"], at=True)
                for H in (6, 12, 24, 48, 72, 120, None):
                    Cn = CA if H is None else min(o["P"] + H * 3600, CA)
                    unres |= resolved(*bounds(cs, o["P"], Cn, o["s"]), o["N"]) is None
        if (which == "B" and inB) or (which == "A" and inA and not inB) or (which == "U" and unres):
            todo.append((r["post"], t))
    print(f"{which}: {len(todo)} orders to fetch")
    for _, t in sorted(todo):
        r = P[t]
        if research_used() - start >= max_calls:
            print("max_calls reached"); break
        lo, hi = r["post"] + POST_GAP, cancel_a_cov(r, C[t])
        d = kget(f"/historical/trades?ticker={t}&min_ts={lo}&max_ts={hi}&limit=1000")
        pr = [[ts(x.get("created_time")), round(float(x.get("yes_price_dollars") or 0), 4), float(x.get("count_fp") or x.get("count") or 0),
               x.get("taker_side"), bool(x.get("is_block_trade"))] for x in d.get("trades") or []]
        with tf.open("a") as fh:
            fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "ok": "trades" in d, "cursor": bool(d.get("cursor")), "which": which, "prints": pr}) + "\n")
        print(t, len(pr), "prints", "TRUNCATED" if d.get("cursor") else "", "research calls", research_used(), flush=True)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "plan":
        plan()
    elif a[0] == "candles":
        candles(int(a[1]))
    elif a[0] == "trades":
        trades(int(a[1]), a[2] if len(a) > 2 else "B")
