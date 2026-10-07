"""Historical Kalshi data for the strategy lab: settled markets of the chosen series and their 1-minute candles over a
decision window. Incremental (markets already stored are skipped) and synced to the paper bot's idle window, so it can
run every night from the learning loop.

Decision windows (no look-ahead):
  "close"    - markets with a fixed close time (15-minute / hourly / daily series): [close - W, close].
  "expected" - sports: close_time is when a winner was declared (unknown in advance), so the window is anchored on
               expected_expiration_time, which is published with the market: [exp - W, min(close, exp + 2 h)].
Storage: data/kalshi_lab/markets/<series>.jsonl (metadata + result) and data/kalshi_lab/candles/<series>.jsonl
  {"t": ticker, "c": [[ts, yes_ask, yes_bid, ask_low, bid_high, volume], ...]} (close values unless noted)
Usage: python -m lab.kalshi.fetch [series,...] [--days N]"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.us.data_refresh import fetch, K

ROOT = Path("data/kalshi_lab"); MK = ROOT / "markets"; CD = ROOT / "candles"
# series -> (lookback days, window minutes, anchor)
PLAN = {
    **{s: (14, 16, "close") for s in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXGOLD15M", "KXWTI15M", "KXSILVER15M")},
    **{s: (14, 70, "close") for s in ("KXBTCD", "KXETHD", "KXINXU", "KXNASDAQ100U", "KXTEMPMIAH", "KXTEMPNYCHS", "KXTEMPCHIHS", "KXTEMPLAXHS")},
    "KXRAIN": (45, 18 * 60, "close"),
    **{s: (30, 6 * 60, "close") for s in ("KXAAAGASD", "KXWTI", "KXGOLDD", "KXNATGASD")},
    **{s: (30, 6 * 60, "expected") for s in ("KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME", "KXUEFANLGAME", "KXCS2GAME")},
    **{s: (10, 6 * 60, "expected") for s in ("KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXITFMATCH", "KXWTAMATCH", "KXATPMATCH", "KXWTACHALLENGERMATCH")},
}
ORDER = ["KXRAIN", "KXBTC15M", "KXETH15M", "KXATPCHALLENGERMATCH", "KXITFWMATCH", "KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME",
         "KXBTCD", "KXINXU", "KXNASDAQ100U", "KXTEMPMIAH", "KXTEMPNYCHS", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXGOLD15M", "KXWTI15M", "KXSILVER15M",
         "KXETHD", "KXTEMPCHIHS", "KXTEMPLAXHS", "KXAAAGASD", "KXWTI", "KXGOLDD", "KXNATGASD", "KXUEFANLGAME", "KXCS2GAME", "KXITFMATCH", "KXWTAMATCH",
         "KXATPMATCH", "KXWTACHALLENGERMATCH"]
HOURLY_EVENTS_PER_DAY = 4   # KXBTCD / KXETHD list ~180-290 strikes per hourly event: sample 4 events a day (by time, not outcome)
MAX_TICKERS = 100
MAX_CANDLES = 9500


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def list_settled(series: str, days: int) -> list[dict]:
    min_close = int(time.time() - days * 86400); out = []; cursor = ""
    while True:
        d = json.loads(fetch(f"{K}/markets?series_ticker={series}&status=settled&min_close_ts={min_close}&limit=1000" + (f"&cursor={cursor}" if cursor else ""), pace=1.15) or "{}")
        for m in d.get("markets") or []:
            if m.get("result") not in ("yes", "no"):
                continue
            out.append({"t": m["ticker"], "e": m["event_ticker"], "series": series, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
                        "exp": ts(m.get("expected_expiration_time")), "result": m["result"], "type": m.get("strike_type"), "floor": m.get("floor_strike"),
                        "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"), "vol": float(m.get("volume_fp") or m.get("volume") or 0)})
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets"):
            return out


def window(m: dict, plan: tuple) -> tuple[int, int] | None:
    days, w, anchor = plan
    if anchor == "close":
        return (m["close"] - w * 60, m["close"]) if m["close"] else None
    if not m["exp"] or not m["close"]:
        return None
    end = min(m["close"], m["exp"] + 2 * 3600); start = m["exp"] - w * 60
    return (start, end) if end > start else None


def compact(cs: list[dict]) -> list[list]:
    f = lambda c, k, sub: float((c.get(k) or {}).get(sub) or 0) if (c.get(k) or {}).get(sub) is not None else None
    return [[int(c["end_period_ts"]), f(c, "yes_ask", "close_dollars"), f(c, "yes_bid", "close_dollars"), f(c, "yes_ask", "low_dollars"),
             f(c, "yes_bid", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def run(series_list: list[str], days_override: int | None = None) -> None:
    MK.mkdir(parents=True, exist_ok=True); CD.mkdir(parents=True, exist_ok=True)
    for s in series_list:
        plan = PLAN[s] if days_override is None else (days_override,) + PLAN[s][1:]
        have = set()
        cf = CD / f"{s}.jsonl"
        if cf.exists():
            have = {json.loads(l)["t"] for l in cf.open()}
        known = {}
        mf = MK / f"{s}.jsonl"
        if mf.exists():
            known = {json.loads(l)["t"]: json.loads(l) for l in mf.open()}
        for m in list_settled(s, plan[0]):
            known[m["t"]] = m
        mf.write_text("".join(json.dumps(m) + "\n" for m in sorted(known.values(), key=lambda m: (m["close"] or 0, m["t"]))))
        todo = [m for m in known.values() if m["t"] not in have and window(m, plan)]
        if s in ("KXBTCD", "KXETHD", "KXBTC", "KXETH"):   # sample events by clock hour, never by outcome
            keep_hours = set(range(0, 24, 24 // HOURLY_EVENTS_PER_DAY))
            todo = [m for m in todo if dt.datetime.fromtimestamp(m["close"], dt.timezone.utc).hour in keep_hours]
        todo = [m for m in todo if (window(m, plan)[1] - window(m, plan)[0]) // 60 + 1 <= MAX_CANDLES]
        todo.sort(key=lambda m: window(m, plan)[0])
        n_calls = 0; n_c = 0
        with cf.open("a") as fh:
            i = 0
            while i < len(todo):
                batch = [todo[i]]; lo, hi = window(todo[i], plan)
                while i + len(batch) < len(todo) and len(batch) < MAX_TICKERS:
                    w2 = window(todo[i + len(batch)], plan)
                    # the endpoint refuses requests over 10,000 candles, counted as markets x span in minutes
                    if (len(batch) + 1) * ((max(hi, w2[1]) - min(lo, w2[0])) // 60 + 1) > MAX_CANDLES:
                        break
                    batch.append(todo[i + len(batch)]); lo, hi = min(lo, w2[0]), max(hi, w2[1])
                i += len(batch)
                d = json.loads(fetch(f"{K}/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=1", pace=1.15) or "{}")
                n_calls += 1
                got = {}
                for x in d.get("markets") or []:
                    got[x.get("market_ticker") or x.get("ticker")] = x.get("candlesticks") or []
                if not got:
                    continue   # failed: retried on the next run
                for m in batch:
                    w = window(m, plan); c = [r for r in compact(got.get(m["t"], [])) if w[0] <= r[0] <= w[1] + 60]
                    fh.write(json.dumps({"t": m["t"], "c": c}) + "\n"); n_c += len(c)
        print(f"{s}: markets {len(known)}, fetched {len(todo)} in {n_calls} calls, candles {n_c}", flush=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else None
    if days is not None and str(days) in args:
        args.remove(str(days))
    run(args[0].split(",") if args else ORDER, days)
