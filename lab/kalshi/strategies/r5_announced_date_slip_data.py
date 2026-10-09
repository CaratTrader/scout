"""Data for r5_announced_date_slip (cached under data/kalshi_lab/strategies/r5_announced_date_slip/).

Universe (fixed before any price was looked at): Kalshi series whose catalog title asks WHEN an issuer (company,
agency, artist, studio) will release / launch a specific product, flight, album, game, model or show, i.e. a date
the issuer itself announces. Excluded by construction: counts, appointments, government statistics, court decisions,
IPO filings, weather. SERIES below is that list, in priority order (catalog volume only used to order the calls).

  lists      /historical/markets?series_ticker=S (archive, settled before 2026-08-08) for every series, plus
             /markets?series_ticker=S&status=settled for series active near the archive cutoff, merged with any
             listing other researchers already cached -> markets.jsonl (normalised rows)
  candles    hourly candles per selected rung -> candles.jsonl  {"t", "c": [[end_ts, yes_ask, yes_bid, vol], ...]}
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_announced_date_slip_data lists|candles [...]"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_announced_date_slip_api import OUT, cached_markets, kget, used  # noqa: E402

MF = OUT / "markets.jsonl"
CF = OUT / "candles.jsonl"
CUTOFF = 1786147200  # 2026-08-08 00:00 UTC

SERIES = [
    # space (issuer NET dates)
    "KXSPACEXSTARSHIP", "SPACEXSTARSHIP", "KXSTARSHIPLAUNCH", "KXARTEMISII", "KXBOLAUNCH", "KXRKLBLAUNCH", "KXNEXTVULCAN",
    "KXSTARSHIP", "KXSPCXLAUNCH",
    # games / hardware
    "KXGTA6", "GTA6", "KXGTA6ONTIME", "KXGTAONLINE", "KXGTATRAILER", "KXSWITCH2RELEASE", "KXIPHONERELEASE", "KXVISIONPRO",
    # AI models
    "KXGPT", "KXGPT5RELEASE", "KXGEMINI", "KXCLAUDE5", "KXDEEPSEEKR2RELEASE", "KXO3RELEASE",
    # music
    "KXSPOTIFYALBUMRELEASEDATEKANYE", "KXVULTURES", "VULTURES", "KXALBUMRELEASEDATE", "KXSPOTIFYALBUMRELEASEDATEDRAKE",
    "KXMEDIARELEASEICEMAN", "KXSPOTIFYALBUMRELEASE", "KXALBUMRELEASEDATEASAP", "KXJACKBOYS2",
    "KXSPOTIFYALBUMRELEASEDATEJACKBOYS2", "KXSPOTIFYALBUMRELEASEDATEBABYBOI", "KXNEWALBUM", "KXALBUMRELEASEDATEUZI",
    # media
    "KXMEDIARELEASEST", "KXMEDIARELEASEADDTRAILER", "KXSPOTIFYWRAPPEDRELEASE", "KXMOVIERELEASEDATE",
]


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def norm(m: dict, tier: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0], "tier": tier,
            "open": ts(m.get("open_time")), "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")),
            "latest_exp": ts(m.get("latest_expiration_time")), "settle_ts": ts(m.get("settlement_ts")),
            "created": ts(m.get("created_time")), "result": m.get("result"), "status": m.get("status"),
            "type": m.get("strike_type"), "title": m.get("title"), "sub": m.get("yes_sub_title"),
            "early": bool(m.get("can_close_early")), "vol": float(m.get("volume_fp") or m.get("volume") or 0),
            "rules": (m.get("rules_primary") or "")[:600], "rules2": (m.get("rules_secondary") or "")[:300]}


def load_markets() -> dict[str, dict]:
    return {json.loads(l)["t"]: json.loads(l) for l in MF.open()} if MF.exists() else {}


def save_markets(out: dict) -> None:
    MF.write_text("".join(json.dumps(m) + "\n" for m in sorted(out.values(), key=lambda m: (m["series"], m["e"], m["t"]))))


def lists(series: list[str], live: bool = False) -> None:
    out = load_markets()
    for t, m in cached_markets().items():   # listings other researchers already paid for
        if m["event_ticker"].split("-")[0] in SERIES and t not in out:
            out[t] = norm(m, "cached")
    for s in series:
        paths = [f"/markets?series_ticker={s}&status=settled&limit=1000"] if live else [f"/historical/markets?series_ticker={s}&limit=1000"]
        for p in paths:
            cursor = ""
            for _ in range(3):
                d = kget(p + (f"&cursor={cursor}" if cursor else ""))
                for m in d.get("markets") or []:
                    if "ticker" in m:
                        r = norm(m, "live" if live else "hist")
                        if t_ok(r, out.get(m["ticker"])):
                            out[m["ticker"]] = r
                cursor = d.get("cursor") or ""
                if not cursor or not d.get("markets"):
                    break
        ms = [m for m in out.values() if m["series"] == s]
        closes = [m["close"] for m in ms if m["close"]]
        print(f"{s:36s} markets {len(ms):4d} events {len({m['e'] for m in ms}):3d} closes "
              f"{dt.datetime.utcfromtimestamp(min(closes)).date() if closes else None}..{dt.datetime.utcfromtimestamp(max(closes)).date() if closes else None}"
              f" | calls {used()}", flush=True)
    save_markets(out)


def t_ok(new: dict, old: dict | None) -> bool:
    return old is None or (new["result"] in ("yes", "no") and old.get("result") not in ("yes", "no")) or old.get("tier") == "cached"




LOOKBACK = 190 * 86400   # candle window per rung: [max(open, D - 190 d), D - 2 d] (history for the 0.40 rule + both fills)
RESERVE = 6              # calls kept back for live order books / open listings


def window(m: dict) -> tuple[int, int]:
    lo = max(m["open"], m["D"] - LOOKBACK)
    hi = min(m["D"] - 2 * 86400, m["close"] + 3600)
    return (lo // 3600) * 3600, (hi // 3600 + 1) * 3600


def candles(dry: bool = False) -> None:
    import hashlib
    from lab.kalshi.strategies.r5_announced_date_slip_api import BUDGET, cached_hist_candles
    from lab.kalshi.strategies.r5_announced_date_slip_common import fetch_set, rows_of, universe
    U = universe(load_markets())
    need = fetch_set(U)
    have = {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()
    other = cached_hist_candles(("KXSPACEX", "SPACEX", "GTA", "KX", "VULTURES", "VISIONPRO"))
    with CF.open("a") as fh:
        # 1. other researchers' cached archive candles (free); kept only if they cover the needed window
        for t, m in need.items():
            if t in have or t not in other:
                continue
            r = rows_of(other[t]); lo, hi = window(m)
            if r and r[0][0] <= lo + 3 * 86400 and r[-1][0] >= min(hi - 86400, m["close"] - 3600):
                fh.write(json.dumps({"t": t, "c": r, "src": "other_cache"}) + "\n"); have.add(t)
        # 2. live tier: batched /markets/candlesticks (<= 9,500 hourly candles per call)
        live = sorted([m for t, m in need.items() if t not in have and m["close"] >= CUTOFF], key=lambda m: window(m)[0])
        i = 0
        while i < len(live):
            batch = [live[i]]; lo, hi = window(live[i])
            while i + len(batch) < len(live) and len(batch) < 50:
                l2, h2 = window(live[i + len(batch)])
                L, Hh = min(lo, l2), max(hi, h2)
                if (len(batch) + 1) * ((Hh - L) // 3600 + 1) > 9500:
                    break
                batch.append(live[i + len(batch)]); lo, hi = L, Hh
            i += len(batch)
            print("live batch", len(batch), "span h", (hi - lo) // 3600, flush=True)
            if dry:
                continue
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in batch)}&start_ts={lo}&end_ts={hi}&period_interval=60")
            got = {}
            for x in d.get("markets") or []:
                got[x.get("market_ticker") or x.get("ticker")] = x.get("candlesticks") or []
            for t, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                got[t] = cs
            if not got:
                print("  batch failed", str(d)[:300], flush=True)
            for m in batch:
                if m["t"] in got:
                    fh.write(json.dumps({"t": m["t"], "c": rows_of(got[m["t"]]), "src": "live_batch"}) + "\n"); have.add(m["t"])
        # 3. archive tier: one call per rung, whole events in sha1('r5ads' + event) order until the reserve
        hist = [m for t, m in need.items() if t not in have and m["close"] < CUTOFF]
        ev: dict[str, list[dict]] = {}
        for m in hist:
            ev.setdefault(m["e"], []).append(m)
        order = sorted(ev, key=lambda e: hashlib.sha1(("r5ads" + e).encode()).hexdigest())
        print("archive rungs", len(hist), "events", len(order), "calls used", used(), flush=True)
        for e in order:
            if dry:
                continue
            if used() + len(ev[e]) > BUDGET - RESERVE:
                print("  skip event (budget)", e, len(ev[e]), flush=True); continue
            for m in sorted(ev[e], key=lambda m: m["D"]):
                lo, hi = window(m)
                d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
                fh.write(json.dumps({"t": m["t"], "c": rows_of(d.get("candlesticks") or []), "src": "hist", "err": d.get("_error")}) + "\n")
                fh.flush()
            print("  event", e, len(ev[e]), "| calls", used(), flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "lists":
        lists(sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] != "all" else SERIES, live="--live" in sys.argv)
    elif cmd == "candles":
        candles(dry="--dry" in sys.argv)
