"""Launch census for r6_new_series_honeymoon (counted Kalshi calls, cached under r6_new_series_honeymoon/api_cache).

Universe: the 717 daily/weekly/hourly series of data/kalshi_lab/series_all.json, ranked by lifetime volume (volume_fp
from the cached /series?include_volume listings of earlier rounds).
  hist(S): /historical/markets?series_ticker=S&limit=1000 (newest first). No cursor -> the oldest market in the page is
           the series' first archived market (exact launch if the series started before the 2026-08-09 archive cutoff);
           cursor -> launch is older than the oldest market on the page. Empty -> nothing settled before the cutoff.
  live(S): /markets?series_ticker=S&status=settled&min_close_ts=2026-08-20&limit=1000 -> first live-tier market.
Outputs census.json (one row per probed series) and fresh_markets.jsonl (settled markets of fresh launches).
Usage: .venv/bin/python -m lab.kalshi.strategies.r6_new_series_honeymoon_census hist N | live S1,S2,..
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys

from lab.kalshi.strategies.r6_new_series_honeymoon_api import OUT, ROOT, calls_used, get

LIVE_LO = int(dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc).timestamp())
CF = os.path.join(OUT, "census.json")
FM = os.path.join(OUT, "fresh_markets.jsonl")


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def volumes() -> dict:
    vol = {}
    for f in glob.glob(os.path.join(ROOT, "data/kalshi_lab/strategies/*/api_cache/**/*.json"), recursive=True):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("series"), list):
            for x in d["series"]:
                if "volume_fp" in x:
                    vol[x["ticker"]] = fnum(x.get("volume_fp"))
    return vol


def universe() -> list[dict]:
    S = json.load(open(os.path.join(ROOT, "data/kalshi_lab/series_all.json")))["series"]
    vol = volumes()
    tg = [{"s": x["ticker"], "freq": x.get("frequency"), "cat": x.get("category"), "fee": x.get("fee_type"),
           "title": x.get("title"), "vol": vol.get(x["ticker"], 0.0)} for x in S if x.get("frequency") in ("daily", "weekly", "hourly")]
    return sorted(tg, key=lambda r: -r["vol"])


def load() -> dict:
    return json.load(open(CF)) if os.path.exists(CF) else {}


def save(c: dict) -> None:
    json.dump(c, open(CF, "w"), indent=1)


def summarize(ms: list[dict]) -> dict:
    if not ms:
        return {"markets": 0}
    o = sorted(ts(m["open_time"]) for m in ms)
    first = o[0]
    ev = {}
    for m in ms:
        ev.setdefault(m["event_ticker"], []).append(m)
    def vol_window(lo_d, hi_d):
        v = [sum(fnum(m.get("volume_fp")) for m in x) for x in ev.values()
             if lo_d <= (min(ts(m["open_time"]) for m in x) - first) / 86400 < hi_d]
        return (round(sum(v) / len(v)), len(v)) if v else (None, 0)
    return {"markets": len(ms), "events": len(ev), "first_open": dt.datetime.utcfromtimestamp(first).isoformat(),
            "last_close": dt.datetime.utcfromtimestamp(max(ts(m["close_time"]) for m in ms)).isoformat(),
            "ev_vol_w1_4": vol_window(0, 28), "ev_vol_w5_8": vol_window(28, 56), "ev_vol_w9p": vol_window(56, 1e9)}


def hist(series: list[str]) -> None:
    c = load()
    for s in series:
        k = f"hist|{s}"
        if k in c:
            continue
        d = get(f"/historical/markets?series_ticker={s}&limit=1000")
        ms = d.get("markets") or []
        r = summarize(ms); r["cursor"] = bool(d.get("cursor")) and len(ms) >= 1000
        c[k] = r; save(c)
        print(s, r, "| calls", calls_used(), flush=True)


def live(series: list[str]) -> None:
    c = load()
    for s in series:
        k = f"live|{s}"
        if k in c:
            continue
        d = get(f"/markets?series_ticker={s}&status=settled&min_close_ts={LIVE_LO}&limit=1000")
        ms = d.get("markets") or []
        r = summarize(ms); r["cursor"] = bool(d.get("cursor"))
        c[k] = r; save(c)
        with open(FM, "a") as fh:
            for m in ms:
                fh.write(json.dumps({"t": m["ticker"], "e": m["event_ticker"], "series": s, "open": ts(m["open_time"]),
                                     "close": ts(m["close_time"]), "result": m.get("result"), "type": m.get("strike_type"),
                                     "floor": m.get("floor_strike"), "cap": m.get("cap_strike"), "sub": m.get("yes_sub_title"),
                                     "vol": fnum(m.get("volume_fp"))}) + "\n")
        print(s, r, "| calls", calls_used(), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "hist":
        hist(sys.argv[2].split(","))
    elif cmd == "live":
        live(sys.argv[2].split(","))
    elif cmd == "universe":
        for r in universe()[: int(sys.argv[2]) if len(sys.argv) > 2 else 50]:
            print(r["s"], r["freq"], r["cat"], round(r["vol"]))
