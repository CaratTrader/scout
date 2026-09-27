"""Paper trader for Kalshi daily-high-temperature ladders (US-legal venue), sharing the observation engine and
rules of scout/us_temp_paper.py (R0 certain, R1x after the 00Z maximum or the NWS intraday climate report, R2 fade).

Kalshi markets per city-day: 'less' (max <= cap-1), 'between' (floor..cap inclusive) and 'greater' (max >= floor+1)
strikes; results settle on The Weather Company's value for the NWS CLI station, which matched the NWS report on
304 of 305 backtested days. Public API only (no keys): market lists give top-of-book, order books give size.
Paced at ~1 request/s (the API returns 429 above that).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.request
import zoneinfo
from pathlib import Path
from typing import Any

from scout import us_temp_paper as U

K = "https://api.elections.kalshi.com/trade-api/v2"
# series -> (city key for the shared engine / climate report, ICAO station, tz)
SERIES = {
    "KXHIGHNY": ("nyc", "KNYC", "America/New_York"), "KXHIGHCHI": ("mdw", "KMDW", "America/Chicago"), "KXHIGHMIA": ("mia", "KMIA", "America/New_York"),
    "KXHIGHLAX": ("lax", "KLAX", "America/Los_Angeles"), "KXHIGHTSFO": ("sfo", "KSFO", "America/Los_Angeles"), "KXHIGHTBOS": ("bos", "KBOS", "America/New_York"),
    "KXHIGHTDC": ("dca", "KDCA", "America/New_York"), "KXHIGHPHIL": ("phl", "KPHL", "America/New_York"), "KXHIGHTATL": ("atl", "KATL", "America/New_York"),
    "KXHIGHDEN": ("den", "KDEN", "America/Denver"), "KXHIGHAUS": ("aus", "KAUS", "America/Chicago"), "KXHIGHTDAL": ("dfw", "KDFW", "America/Chicago"),
    "KXHIGHTMIN": ("msp", "KMSP", "America/Chicago"), "KXHIGHTPHX": ("phx", "KPHX", "America/Phoenix"), "KXHIGHTSEA": ("sea", "KSEA", "America/Los_Angeles"),
    "KXHIGHTLV": ("las", "KLAS", "America/Los_Angeles"), "KXHIGHTSAN": ("san", "KSAN", "America/Los_Angeles"),
}
if os.getenv("KTEMP_SERIES"):
    SERIES = {k: v for k, v in SERIES.items() if k in os.getenv("KTEMP_SERIES", "").split(",")}
LEDGER = Path(os.getenv("KTEMP_LEDGER") or "data/ledger_kalshi_temp.json")
JOURNAL = Path(os.getenv("KTEMP_JOURNAL") or "data/kalshi_temp_journal.jsonl")
STATE = Path(os.getenv("KTEMP_STATE") or "data/kalshi_temp_state.json")
SNAPS = Path(os.getenv("KTEMP_SNAPS") or "data/kalshi_temp_snapshots.jsonl")
FEE = 0.07  # Kalshi taker fee coefficient (fee = 0.07 * C * p * (1-p))
_LAST_CALL = [0.0]


def kget(url: str, quiet: bool = False) -> Any:
    """Paced GET (>= 1.05 s between calls) with retries on 429."""
    for attempt in range(4):
        wait = 1.05 - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-kalshi-temp-paper", "Accept": "application/json"}), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1)); continue
            if not quiet:
                U.journal({"event": "error", "url": url[-80:], "err": f"HTTP {e.code}"})
            return None
        except Exception as exc:
            if not quiet:
                U.journal({"event": "error", "url": url[-80:], "err": str(exc)[:100]})
            return None
    return None


def synthetic_slug(m: dict[str, Any]) -> str:
    """Encode a Kalshi strike as the venue-neutral bucket grammar the shared engine parses (gteAltBf / ltXf / gteAf)."""
    t = m.get("strike_type")
    if t == "less":
        return f"k-{m['ticker']}-lt{int(m['cap_strike'])}f"
    if t == "greater":
        return f"k-{m['ticker']}-gte{int(m['floor_strike']) + 1}f"
    return f"k-{m['ticker']}-gte{int(m['floor_strike'])}lt{int(m['cap_strike'])}f"


def market_day(m: dict[str, Any], tz: zoneinfo.ZoneInfo) -> str:
    close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
    return (close.astimezone(tz) - dt.timedelta(hours=6)).date().isoformat()


_LADDERS: dict[str, list[dict[str, Any]]] = {}


def ladder(series: str, day: str, tz: zoneinfo.ZoneInfo) -> list[dict[str, Any]]:
    key = f"{series}:{day}"
    if key not in _LADDERS or time.time() - _LADDERS[key][0] > 900:
        d = kget(f"{K}/markets?series_ticker={series}&status=open&limit=50") or {}
        mk = [m for m in d.get("markets") or [] if market_day(m, tz) == day]
        _LADDERS[key] = (time.time(), mk)
    return _LADDERS[key][1]


def cents(v: Any) -> float | None:
    """Kalshi list endpoints report prices in cents (int) or as *_dollars strings; normalise to dollars."""
    try:
        if v is None:
            return None
        f = float(v)
        return f / 100 if f > 1.0 else f
    except (TypeError, ValueError):
        return None


def quotes(series: str, day: str, tz: zoneinfo.ZoneInfo) -> list[dict[str, Any]]:
    """Top-of-book for every bucket of today's ladder (one request per series, refreshed each poll)."""
    d = kget(f"{K}/markets?series_ticker={series}&status=open&limit=50") or {}
    out = []
    for m in d.get("markets") or []:
        if market_day(m, tz) != day:
            continue
        bid = cents(m.get("yes_bid_dollars") or m.get("yes_bid")); ask = cents(m.get("yes_ask_dollars") or m.get("yes_ask"))
        out.append({"slug": synthetic_slug(m), "ticker": m["ticker"], "bid": bid if bid else None, "ask": ask if ask else None, "bid_sz": None, "ask_sz": None,
                    "state": "OPEN", "close_time": m.get("close_time"), "title": m.get("title")})
    return out


def book_size(ticker: str, side: str) -> float:
    """Displayed size at the best level: buying YES takes the YES asks (= NO bids); buying NO takes the NO asks (= YES bids)."""
    ob = (kget(f"{K}/markets/{ticker}/orderbook", quiet=True) or {}).get("orderbook_fp") or {}
    levels = ob.get("no_dollars" if side == "YES" else "yes_dollars") or []
    if not levels:
        return 0.0
    best = max(levels, key=lambda lv: float(lv[0]))  # the highest NO bid caps the YES ask, and vice versa
    return float(best[1])


def settle(led: dict[str, Any]) -> list[dict[str, Any]]:
    done = []
    for pos in list(led["positions"]):
        end = pos.get("end_date")
        if end and dt.datetime.fromisoformat(end.replace("Z", "+00:00")) + dt.timedelta(hours=6) > dt.datetime.now(dt.timezone.utc):
            continue
        if time.time() < U._SETTLE_NEXT.get(pos["ticker"], 0):
            continue
        U._SETTLE_NEXT[pos["ticker"]] = time.time() + 900
        m = (kget(f"{K}/markets/{pos['ticker']}", quiet=True) or {}).get("market") or {}
        if m.get("result") not in ("yes", "no"):
            continue
        settle_yes = 1.0 if m["result"] == "yes" else 0.0
        payoff = pos["shares"] * (settle_yes if pos["side"] == "YES" else 1 - settle_yes)
        pnl = round(payoff - pos["stake"] - pos["fee"], 4)
        led["cash"] = round(led["cash"] + payoff, 4); led["positions"].remove(pos)
        fill = {**pos, "settled": dt.datetime.now(dt.timezone.utc).isoformat(), "settle_yes": settle_yes, "pnl": pnl, "won": pnl > 0}
        led["fills"].append(fill); done.append(fill); U.journal({"event": "settle", **fill})
    return done


def poll(led: dict[str, Any], now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    n_c = n_f = 0; notes = []
    snap: dict[str, Any] = {"ts": now.timestamp(), "updated": now.isoformat(), "venue": "kalshi", "config": {**U.CFG, "z00_local_hour": U.Z00_LOCAL_HOUR}, "cities": {}}
    for series, (city, station, tzname) in SERIES.items():
        tz = zoneinfo.ZoneInfo(tzname); now_local = now.astimezone(tz); day = now_local.date().isoformat()
        cs = snap["cities"][city] = {"station": station, "tz": tzname, "series": series, "local_time": now_local.strftime("%H:%M"), "day": day, "active": False, "note": ""}
        if now_local.hour < 9:
            cs["note"] = "waits until 09:00 local"; continue
        buckets = quotes(series, day, tz)
        if not buckets:
            cs["note"] = "no open Kalshi markets for today"; continue
        ob = U.observed(station, tz, now)
        if not ob:
            cs["note"] = "no METAR observations yet today"; continue
        rep = U.cli_intraday(city, day) if U.CFG["cli_obs"] and now_local.hour >= 15 else None  # api.weather.gov location = station id (BOS, NYC, ...)
        ob["cli"] = rep
        if rep and rep["max"] >= ob["max"] - 0.5:
            if rep["max"] > ob["max"]:
                ob["max"] = rep["max"]; ob["t_max"] = now_local.replace(hour=rep["asof_min"] // 60, minute=rep["asof_min"] % 60, second=0, microsecond=0)
            ob["readings"].append({"time": f"{rep['asof_min']//60:02d}:{rep['asof_min']%60:02d}", "f": rep["max"], "src": "NWS climate report", "used": True})
        ev = U.evaluate(buckets, ob, now_local, city=city)
        by_slug = {b["slug"]: b for b in buckets}
        for r in ev["rows"]:
            r["ticker"] = by_slug[r["slug"]]["ticker"]; r["label"] = r["label"] + f" ({r['ticker'].split('-')[-1]})"
        cs.update({"active": True, "observed": {**ev["flags"], "t_max": ob["t_max"].strftime("%H:%M"), "n_readings": ob["n"]}, "readings": ob.get("readings", []), "buckets": ev["rows"],
                   "n_buckets_open": len(buckets), "candidates": sum(1 for r in ev["rows"] if r["candidate"])})
        fl = ev["flags"]
        for tag, cond in (("cli_report", bool(fl.get("cli_report"))), ("early_window", bool(fl.get("cli_window"))), ("z00_window", bool(ob.get("has_00z")))):
            k = f"{city}:{day}:{tag}"
            if cond and k not in U._SEEN:
                U._SEEN.add(k); U.journal({"event": tag, "venue": "kalshi", "city": city, "day": day, "max": fl["max"], "latest": fl["latest"], "report": fl.get("cli_report")})
        holds = next((r for r in ev["rows"] if r["status"] == "holds the max"), None)
        try:
            with SNAPS.open("a") as fh:
                fh.write(json.dumps({"ts": round(now.timestamp()), "city": city, "lt": now_local.strftime("%H:%M"), "max": fl["max"], "latest": fl["latest"], "fall": fl["fall_f"], "since": fl["since_max_min"],
                                     "peak": fl["peak_passed"], "z00": bool(ob.get("has_00z")), "cli": fl.get("cli_report"), "early": fl.get("cli_window"), "cands": cs["candidates"],
                                     "holds": holds and {"b": holds["label"], "bid": holds["bid"], "ask": holds["ask"]}, "book": [(r["label"], r["bid"], r["ask"]) for r in ev["rows"] if (r["bid"] or 0) >= 0.05]}) + "\n")
        except Exception:
            pass
        held_keys = {(p["slug"], p["side"]) for p in led["positions"]}
        cands = [{k: r[k] for k in ("rule", "slug", "side", "px", "size", "why")} | {"ticker": r["ticker"]} for r in ev["rows"] if r["candidate"]]
        for c in cands:
            if (c["slug"], c["side"]) in held_keys:
                continue
            c["size"] = book_size(c["ticker"], c["side"])  # one order-book call per candidate, only when needed
            U.journal({"event": "signal", "venue": "kalshi", "city": city, **c, "max": round(ob["max"], 1), "latest": round(ob["latest"], 1)})
            meta = {"city": city, "day": day, "ticker": c["ticker"], "venue": "kalshi", "end_date": by_slug[c["slug"]].get("close_time")}
            n_f += len(U.confirm_and_fill(led, [c], meta))
        n_c += len(cands)
        notes.append(f"{city} max={ob['max']:.0f} now={ob['latest']:.0f} cands={len(cands)}")
    settled = settle(led)
    led["updated"] = now.isoformat(); U.save_ledger(led)
    snap.update({"cash": led["cash"], "positions": led["positions"], "pending": led.get("pending", {}), "n_fills": len(led.get("fills", []))})
    try:
        tmp = STATE.with_suffix(".tmp"); tmp.write_text(json.dumps(snap, default=str)); tmp.replace(STATE)
    except Exception as exc:
        U.journal({"event": "error", "url": "state-file", "err": str(exc)[:120]})
    return f"kalshi-temp paper cash={led['cash']:.2f} pos={len(led['positions'])} cands={n_c} fills={n_f} settled={len(settled)} | " + " ".join(notes)


def main() -> None:
    U.LEDGER, U.JOURNAL, U.FEE = LEDGER, JOURNAL, FEE
    once = "--once" in sys.argv
    led = U.load_ledger()
    print(f"Kalshi temperature paper trader: {len(SERIES)} cities, stake ${U.CFG['stake']:.0f}, poll {U.CFG['poll_s']:.0f}s, R1x cap {U.CFG['r1x_max_ask']}, report trigger {'on' if U.CFG['cli_trigger'] else 'off'}", flush=True)
    while True:
        try:
            print(poll(led), flush=True)
        except Exception as exc:
            print(f"cycle error: {type(exc).__name__}: {exc}", flush=True); U.journal({"event": "cycle_error", "err": str(exc)[:200]})
        if once:
            break
        time.sleep(U.CFG["poll_s"])


if __name__ == "__main__":
    main()
