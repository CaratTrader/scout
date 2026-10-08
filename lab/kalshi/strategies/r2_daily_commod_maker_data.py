"""r2_daily_commod_maker data: settled KXGOLDD / KXNATGASD / KXWTI ladders since 2026-08-01 (the live /markets endpoint
keeps ~68 days) and 1-minute candles WITH trade prints over the maker window, cached under
data/kalshi_lab/strategies/r2_daily_commod_maker/.

  markets  - one /markets?status=settled call per 1,000 markets and series -> markets_<S>.jsonl
             {t, e, open, close, result, type, floor, cap, vol, xv (expiration_value, the settlement value)}
  candles  - /markets/candlesticks batches (<= 35 markets x 271 minutes < 10,000 candles) over [close-390m, close-120m]
             for the strikes a generous model (k = 1.6, Yahoo 5-minute futures minus the previous settlement's basis)
             puts between 4% and 96% at some 5-minute decision time in [close-6h, close-2h]. The selection uses only the
             futures path, never Kalshi prices or results, and is wider than any rule we test (k <= 1.5, p in 5-95%),
             so no strike a rule could quote is left out. KXNATGASD lists strikes every 0.005: only the 0.01 grid is
             fetched (outcome-blind halving of near-duplicate strikes, to fit the call budget). -> candles_<S>.jsonl
             {"t": ticker, "c": [[ts, ask_c, bid_c, ask_lo, ask_hi, bid_lo, bid_hi, px_lo, px_hi, vol], ...]}
             (ts = end of the minute; px_* are trade prints, None when nothing traded that minute)
  book     - live order books of the open near-money strikes (capacity) -> books_<date>.json
Every Kalshi call goes through lab.us.data_refresh.fetch (bot idle window, 429 retries) and is counted in calls.json.
Usage: python -m lab.kalshi.strategies.r2_daily_commod_maker_data [markets|candles|book] [--dry]"""
from __future__ import annotations
import bisect, datetime as dt, json, math, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r2_daily_commod_maker")
YH = Path("data/kalshi_lab/strategies/commod_model/yahoo")
SERIES = {"KXGOLDD": "GC=F", "KXNATGASD": "NG=F", "KXWTI": "CL=F"}
MIN_CLOSE = int(dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc).timestamp())
W_LO, W_HI = 390, 120          # candle window, minutes before close
MAX_BATCH = 35                  # 35 x 271 = 9,485 candles per call
SEL_K, SEL_P = 1.6, 0.04         # strike selection (generous model, see module doc)
N = __import__("statistics").NormalDist()


def calls_add(n: int = 1) -> int:
    f = OUT / "calls.json"; c = json.loads(f.read_text()) if f.exists() else {"kalshi": 0}
    c["kalshi"] += n; f.write_text(json.dumps(c)); return c["kalshi"]


def kget(url: str) -> dict:
    txt = fetch(url, pace=1.15); calls_add()
    try:
        return json.loads(txt or "{}")
    except Exception:
        return {}


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def fnum(x):
    try:
        return float(x)
    except Exception:
        return None


def list_markets() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for s in SERIES:
        out, cursor = {}, ""
        while True:
            d = kget(f"{K}/markets?series_ticker={s}&status=settled&min_close_ts={MIN_CLOSE}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets") or []:
                if m.get("result") not in ("yes", "no"):
                    continue
                out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
                                    "result": m["result"], "type": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
                                    "vol": fnum(m.get("volume_fp") or m.get("volume")) or 0.0, "xv": fnum(m.get("expiration_value"))}
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
        rows = sorted(out.values(), key=lambda m: (m["close"] or 0, m["t"]))
        (OUT / f"markets_{s}.jsonl").write_text("".join(json.dumps(m) + "\n" for m in rows))
        print(s, "markets", len(rows), "events", len({m["e"] for m in rows}), flush=True)


def load_markets(s: str) -> list[dict]:
    f = OUT / f"markets_{s}.jsonl"
    return [json.loads(l) for l in f.open()] if f.exists() else []


class Fut5:
    """Yahoo 5-minute bars [start, o, h, l, c, v]; a bar's close is known at start + 300 (+ lag)."""
    def __init__(self, sym: str):
        b = json.loads((YH / f"{sym.replace('=', '_')}_5m.json").read_text())
        self.ts = [x[0] for x in b]; self.c = [x[4] for x in b]

    def idx(self, t: int, lag: int = 0, max_age: int = 1800) -> int | None:
        i = bisect.bisect_right(self.ts, t - 300 - lag) - 1
        return i if i >= 0 and t - 300 - lag - self.ts[i] <= max_age else None

    def px(self, t: int, lag: int = 0, max_age: int = 1800) -> float | None:
        i = self.idx(t, lag, max_age); return self.c[i] if i is not None else None

    def sig1(self, t: int, lag: int = 0, n: int = 24) -> float | None:
        """Per-minute sd from the last n 5-minute close-to-close changes known at t (gaps over 30 min skipped)."""
        i = self.idx(t, lag)
        if i is None or i < n + 1:
            return None
        r = [self.c[j] - self.c[j - 1] for j in range(i - n + 1, i + 1) if self.ts[j] - self.ts[j - 1] <= 1800]
        return math.sqrt(sum(x * x for x in r) / len(r) / 5) if len(r) >= n // 2 else None


def events(s: str) -> list[tuple[str, int, list[dict]]]:
    ev = {}
    for m in load_markets(s):
        ev.setdefault(m["e"], []).append(m)
    return sorted(((e, ms[0]["close"], ms) for e, ms in ev.items()), key=lambda x: x[1])


def settle_value(ms: list[dict]) -> float | None:
    """Settlement value: expiration_value if published, else the midpoint of (highest YES floor, lowest NO floor]."""
    xv = [m["xv"] for m in ms if m.get("xv") is not None]
    if xv:
        return xv[0]
    ys = [m["floor"] for m in ms if m["result"] == "yes" and m["floor"] is not None]
    ns = [m["floor"] for m in ms if m["result"] == "no" and m["floor"] is not None]
    return (max(ys) + min(ns)) / 2 if ys and ns else None


def select(s: str) -> list[tuple[str, int, list[str]]]:
    """(event, close, near-money tickers) chosen from the futures path only (see module doc)."""
    F = Fut5(SERIES[s]); evs = events(s); out = []
    for i, (e, T, ms) in enumerate(evs):
        if i == 0:
            continue
        sv = settle_value(evs[i - 1][2]); fp = F.px(evs[i - 1][1])
        if sv is None or fp is None:
            continue
        basis = fp - sv; keep = set()
        for mins in range(360, 119, -5):
            t = T - mins * 60; f = F.px(t); sg = F.sig1(t)
            if f is None or not sg:
                continue
            S = f - basis
            for m in ms:
                if m["floor"] is None:
                    continue
                if s == "KXNATGASD" and round(m["floor"] * 1000) % 10:
                    continue
                p = N.cdf((S - m["floor"]) / (SEL_K * sg * math.sqrt(mins)))
                if SEL_P <= p <= 1 - SEL_P:
                    keep.add(m["t"])
        if keep:
            out.append((e, T, sorted(keep)))
    return out


def compact(cs: list[dict]) -> list[list]:
    def g(c, k, sub):
        v = (c.get(k) or {}).get(sub)
        return float(v) if v is not None else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask", "close_dollars"), g(c, "yes_bid", "close_dollars"), g(c, "yes_ask", "low_dollars"),
             g(c, "yes_ask", "high_dollars"), g(c, "yes_bid", "low_dollars"), g(c, "yes_bid", "high_dollars"), g(c, "price", "low_dollars"),
             g(c, "price", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def candles(dry: bool = False) -> None:
    for s in SERIES:
        cf = OUT / f"candles_{s}.jsonl"
        have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
        sel = select(s); todo = [(e, T, [t for t in tks if t not in have]) for e, T, tks in sel]
        todo = [x for x in todo if x[2]]
        n_calls = sum(math.ceil(len(x[2]) / MAX_BATCH) for x in todo)
        print(s, "events", len(sel), "to fetch", len(todo), "markets", sum(len(x[2]) for x in todo), "calls", n_calls, flush=True)
        if dry:
            continue
        with cf.open("a") as fh:
            for e, T, tks in todo:
                lo, hi = T - W_LO * 60, T - W_HI * 60
                for j in range(0, len(tks), MAX_BATCH):
                    b = tks[j:j + MAX_BATCH]
                    d = kget(f"{K}/markets/candlesticks?market_tickers={','.join(b)}&start_ts={lo}&end_ts={hi}&period_interval=1")
                    got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
                    if not got:
                        print("  failed", e, flush=True); continue
                    for tk in b:
                        fh.write(json.dumps({"t": tk, "c": compact(got.get(tk, []))}) + "\n")
                fh.flush()
        print(s, "done; kalshi calls so far", json.loads((OUT / "calls.json").read_text())["kalshi"], flush=True)


def book() -> None:
    """Order books of the open near-money strikes of each series (capacity at the touch)."""
    snap = {"ts": int(time.time()), "series": {}}
    for s in SERIES:
        d = kget(f"{K}/markets?series_ticker={s}&status=open&limit=200")
        ms = [m for m in d.get("markets") or [] if fnum(m.get("yes_bid_dollars")) and fnum(m.get("yes_ask_dollars"))]
        ms = [m for m in ms if 0.10 <= (fnum(m["yes_bid_dollars"]) + fnum(m["yes_ask_dollars"])) / 2 <= 0.90]
        ms.sort(key=lambda m: abs((fnum(m["yes_bid_dollars"]) + fnum(m["yes_ask_dollars"])) / 2 - 0.5))
        rows = []
        for m in ms[:4]:
            ob = kget(f"{K}/markets/{m['ticker']}/orderbook")
            rows.append({"ticker": m["ticker"], "close": m.get("close_time"), "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars"),
                         "ob": ob})
        snap["series"][s] = {"n_open": len(d.get("markets") or []), "books": rows}
    (OUT / f"books_{time.strftime('%Y%m%d_%H%M', time.gmtime())}.json").write_text(json.dumps(snap))
    print("books saved", {s: len(v["books"]) for s, v in snap["series"].items()})


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "markets"
    {"markets": list_markets, "candles": lambda: candles("--dry" in sys.argv), "book": book}[what]()
