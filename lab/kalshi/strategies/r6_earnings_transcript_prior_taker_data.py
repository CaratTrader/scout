"""r6_earnings_transcript_prior_taker: Kalshi data assembly (markets, results, hourly quotes) from the round-3/round-4
caches, plus counted archive candle fetches for extra markets.

Sources (0 Kalshi calls):
  r3_earnings_call_mentions/markets_hist.jsonl, markets_live.jsonl   settled earnings-mention markets (word, result)
  r3_earnings_call_mentions/api_cache/*.json                         live-tier market records (occurrence_datetime)
  r3_earnings_call_mentions/candles_W2.jsonl   hourly candles [first close - 52 h, last close + 1 h], all words of 46
                                               live-tier events (Aug-Oct 2026)
  r4_earnings_seeded_book_48h_forward/candles.jsonl   hourly lifetime candles, one random word in each of 95 archive
                                               events (Oct 2025 - Aug 2026), chosen by round 4 from listing fields only
Extra (counted, this module): /historical/markets/{ticker}/candlesticks hourly over [D - 3 h, D + 3 h] for archive
markets chosen by decision-time fields only (see choose_extra in the main module).

Quote rows are normalised to [end_ts, yes_ask_close, yes_bid_close, volume]. An hourly candle's close is the book at
end_ts (rows exist for hours without trades)."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_earnings_transcript_prior_taker_api import OUT, kget  # noqa: E402

R3 = Path("data/kalshi_lab/strategies/r3_earnings_call_mentions")
R4 = Path("data/kalshi_lab/strategies/r4_earnings_seeded_book_48h_forward")
EXTRA = OUT / "candles_extra.jsonl"
PFX = "KXEARNINGSMENTION"


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def wkey(w: str) -> str:
    parts = sorted(p.strip() for p in re.sub(r"[^a-z0-9/+() ]", "", w.lower()).split("/") if p.strip())
    return "/".join(parts)


def company_name(title: str) -> str:
    m = re.search(r"What will (.*?) say (during|on)", title or "")
    return m.group(1).strip() if m else ""


def load_markets() -> dict:
    allm = {}
    for f in ("markets_hist.jsonl", "markets_live.jsonl"):
        for l in (R3 / f).open():
            m = json.loads(l)
            allm[m["ticker"]] = m
    occ = {}
    rules = {}
    for f in (R3 / "api_cache").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        for m in d.get("markets") or []:
            if m.get("ticker", "").startswith(PFX):
                if m.get("occurrence_datetime"):
                    occ[m["ticker"]] = ts(m["occurrence_datetime"])
                if m.get("rules_primary"):
                    rules[m["ticker"]] = m["rules_primary"]
                if m["ticker"] not in allm:
                    allm[m["ticker"]] = m | {"series": m["event_ticker"].split("-")[0], "word": (m.get("custom_strike") or {}).get("Word")}
    out = {}
    for t, m in allm.items():
        w = m.get("word") or (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""
        out[t] = {"t": t, "e": m["event_ticker"], "series": m["series"], "s": m["series"].replace(PFX, ""), "w": w, "wk": wkey(w),
                  "y": {"yes": True, "no": False}.get(m.get("result")), "vol": float(m.get("volume_fp") or 0),
                  "open": ts(m.get("open_time")), "close": ts(m.get("close_time")), "occ": occ.get(t),
                  "name": company_name(m.get("title", "")), "rules": rules.get(t)}
    return out


def events(ms: dict) -> dict:
    ev = defaultdict(lambda: {"tickers": []})
    for m in ms.values():
        e = ev[m["e"]]
        e["e"] = m["e"]; e["s"] = m["s"]; e["tickers"].append(m["t"])
        e["open"] = min(e.get("open") or 1e12, m["open"] or 1e12)
        e["close1"] = min(e.get("close1") or 1e12, m["close"] or 1e12)
        if m["name"]:
            e.setdefault("names", set()).add(m["name"])
        if m["occ"]:
            e.setdefault("occs", []).append(m["occ"])
    for e in ev.values():
        e["settled"] = all(ms[t]["y"] is not None for t in e["tickers"])
        names = sorted(e.get("names") or [], key=len, reverse=True)
        e["name"] = names[0] if names else ""
    return dict(ev)


def load_quotes() -> dict:
    q = {}
    for l in (R3 / "candles_W2.jsonl").open():
        x = json.loads(l)
        for t, rows in x["c"].items():
            q[t] = sorted([[r[0], r[1], r[2], r[8]] for r in rows])
    for l in (R4 / "candles.jsonl").open():
        x = json.loads(l)
        if x.get("c"):
            q.setdefault(x["t"], sorted([[r[0], r[1], r[2], r[5]] for r in x["c"]]))
    if EXTRA.exists():
        for l in EXTRA.open():
            x = json.loads(l)
            if x.get("c"):
                q.setdefault(x["t"], sorted([[r[0], r[1], r[2], r[3]] for r in x["c"]]))
    return q


def _px(v):
    if v is None:
        return None
    v = float(v)
    return v / 100 if v > 1.0001 else v


def fetch_extra(jobs: list[tuple[str, int, int]], max_calls: int) -> int:
    """jobs: (ticker, lo, hi). One /historical/markets/{t}/candlesticks call each (hourly)."""
    have = {json.loads(l)["t"] for l in EXTRA.open()} if EXTRA.exists() else set()
    n = 0
    for t, lo, hi in jobs:
        if t in have:
            continue
        if n >= max_calls:
            break
        d = kget(f"/historical/markets/{t}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
        n += 1
        rows = []
        for c in d.get("candlesticks") or []:
            a = c.get("yes_ask") or {}; b = c.get("yes_bid") or {}
            rows.append([int(c["end_period_ts"]), _px(a.get("close_dollars", a.get("close"))), _px(b.get("close_dollars", b.get("close"))),
                         float(c.get("volume_fp") or c.get("volume") or 0)])
        with EXTRA.open("a") as fh:
            fh.write(json.dumps({"t": t, "lo": lo, "hi": hi, "ok": bool(d), "c": sorted(rows)}) + "\n")
        print(t, len(rows), "candles", flush=True)
    return n


def plan_extra(ms: dict, ev: dict, seed: int = 6, min_open: int = 1743465600) -> list[tuple[str, int, int]]:
    """One uniformly random word market (seeded) from every settled archive event that has no cached candles and opened
    on/after 2025-04-01, lifetime hourly window [open - 1 h, close + 1 h]. Chosen from listing fields only, before any
    price or prior is looked at (the same design as round 4's 95-event sample)."""
    import random
    q = load_quotes()
    rng = random.Random(seed)
    jobs = []
    for e in sorted(ev.values(), key=lambda e: (e["open"], e["e"])):
        if not e["settled"] or e["open"] < min_open or any(t in q for t in e["tickers"]):
            continue
        tks = sorted(t for t in e["tickers"] if ms[t]["open"] and ms[t]["close"] and ms[t]["y"] is not None)
        if not tks:
            continue
        t = rng.choice(tks)
        m = ms[t]
        jobs.append((t, (m["open"] // 3600) * 3600 - 3600, (m["close"] // 3600) * 3600 + 3600))
    return jobs
