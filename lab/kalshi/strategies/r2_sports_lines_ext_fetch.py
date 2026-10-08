"""Data for r2_sports_lines_ext: settled Kalshi spread / total markets and their 1-minute candles around the scheduled
start, plus the call counter (budget: 200 Kalshi calls for the whole study).

All Kalshi calls go through lab.us.data_refresh.fetch (bot idle window, 429 retries, pace 1.15 s).
Outputs (all under data/kalshi_lab/strategies/r2_sports_lines_ext/):
  markets/<SERIES>.jsonl   one settled market per line (ticker, event, strike, sub, times, result, volume)
  candles/<SERIES>.jsonl   {"t": ticker, "c": [[ts, yes_ask, yes_bid, ask_low, bid_high, volume], ...]}
  kalshi_calls.txt         running count of Kalshi requests made by this study

Usage: python -m lab.kalshi.strategies.r2_sports_lines_ext_fetch markets [SERIES,...]
       python -m lab.kalshi.strategies.r2_sports_lines_ext_fetch candles   (needs the target list from the main module)"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r2_sports_lines_ext")
MKD = OUT / "markets"; CDD = OUT / "candles"; CALLS = OUT / "kalshi_calls.txt"
SERIES = ["KXNFLTOTAL", "KXNFLSPREAD", "KXNCAAFTOTAL", "KXNCAAFSPREAD", "KXMLBTOTAL", "KXMLBSPREAD",
          "KXNHLTOTAL", "KXNHLSPREAD", "KXUEFANLTOTAL", "KXUEFANLSPREAD"]
BUDGET = 200
MAX_CANDLES = 9500


def calls() -> int:
    return int(CALLS.read_text().strip() or 0) if CALLS.exists() else 0


def kget(url: str) -> dict:
    n = calls()
    if n >= BUDGET:
        raise SystemExit(f"Kalshi call budget exhausted ({n})")
    txt = fetch(url, pace=1.15)
    CALLS.write_text(str(n + 1))
    try:
        return json.loads(txt or "{}")
    except Exception:
        return {}


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def list_settled(series: str, days: int = 33) -> list[dict]:
    min_close = int(time.time() - days * 86400); out = []; cursor = ""
    while True:
        d = kget(f"{K}/markets?series_ticker={series}&status=settled&min_close_ts={min_close}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        for m in d.get("markets") or []:
            if m.get("result") not in ("yes", "no"):
                continue
            out.append({"t": m["ticker"], "e": m["event_ticker"], "series": series, "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
                        "exp": ts(m.get("expected_expiration_time")), "occ": ts(m.get("occurrence_datetime")), "result": m["result"],
                        "type": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"),
                        "title": m.get("title"), "vol": float(m.get("volume_fp") or m.get("volume") or 0), "rules": (m.get("rules_primary") or "")[:300]})
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets"):
            return out


def markets(series: list[str]) -> None:
    MKD.mkdir(parents=True, exist_ok=True)
    for s in series:
        f = MKD / f"{s}.jsonl"
        if f.exists():
            print(s, "cached", sum(1 for _ in f.open())); continue
        ms = list_settled(s)
        f.write_text("".join(json.dumps(m) + "\n" for m in sorted(ms, key=lambda m: (m["close"] or 0, m["t"]))))
        print(s, len(ms), "markets,", len({m["e"] for m in ms}), "events; calls so far", calls(), flush=True)


def compact(cs: list[dict]) -> list[list]:
    f = lambda c, k, sub: float((c.get(k) or {}).get(sub) or 0) if (c.get(k) or {}).get(sub) is not None else None
    return [[int(c["end_period_ts"]), f(c, "yes_ask", "close_dollars"), f(c, "yes_bid", "close_dollars"), f(c, "yes_ask", "low_dollars"),
             f(c, "yes_bid", "high_dollars"), float(c.get("volume_fp") or c.get("volume") or 0)] for c in cs]


def candles(targets: list[dict], max_calls: int | None = None) -> None:
    """targets: [{"s": series, "t": ticker, "lo": ts, "hi": ts}] - fetched in batches of tickers sharing one [lo, hi] span,
    skipping tickers already stored."""
    CDD.mkdir(parents=True, exist_ok=True)
    have = set()
    for f in CDD.glob("*.jsonl"):
        have |= {json.loads(l)["t"] for l in f.open()}
    todo = sorted([x for x in targets if x["t"] not in have], key=lambda x: x["lo"])
    i = 0; made = 0
    while i < len(todo):
        if max_calls is not None and made >= max_calls:
            break
        batch = [todo[i]]; lo, hi = todo[i]["lo"], todo[i]["hi"]
        while i + len(batch) < len(todo) and len(batch) < 100:
            w = todo[i + len(batch)]
            if (len(batch) + 1) * ((max(hi, w["hi"]) - min(lo, w["lo"])) // 60 + 1) > MAX_CANDLES:
                break
            batch.append(w); lo, hi = min(lo, w["lo"]), max(hi, w["hi"])
        i += len(batch)
        d = kget(f"{K}/markets/candlesticks?market_tickers={','.join(x['t'] for x in batch)}&start_ts={lo}&end_ts={hi}&period_interval=1")
        made += 1
        got = {}
        for x in d.get("markets") or []:
            got[x.get("market_ticker") or x.get("ticker")] = x.get("candlesticks") or []
        if not got:
            print("empty batch", batch[0]["t"], flush=True); continue
        by_s: dict[str, list] = {}
        for x in batch:
            c = [r for r in compact(got.get(x["t"], [])) if x["lo"] <= r[0] <= x["hi"] + 60]
            by_s.setdefault(x["s"], []).append(json.dumps({"t": x["t"], "c": c}) + "\n")
        for s, lines in by_s.items():
            with (CDD / f"{s}.jsonl").open("a") as fh:
                fh.writelines(lines)
        print(f"batch {made}: {len(batch)} tickers span {(hi - lo) // 60} min; calls so far {calls()}", flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "markets"
    if what == "markets":
        markets(sys.argv[2].split(",") if len(sys.argv) > 2 else SERIES)
