"""Weather observed-max lock: Polymarket daily "highest temperature in <city>" markets.

Every one of these markets (except Hong Kong, which uses the HK Observatory) resolves on NOAA's
WRH timeseries for one airport station — i.e. the station's METAR readings, "highest reading under
the Temp column for all times on this day". Those readings are public in real time
(aviationweather.gov). Once the afternoon maximum has physically been reached and the temperature
has fallen away from it, the bucket containing the running maximum is (nearly) certain, yet the
market keeps pricing it well below $1 for an hour or more. Backtest 2026-09-01..11, 16 cities, 173
city-days (docs/STRATEGY_RESEARCH.md section 13b): at 17-18h local, 0 late-maximum days in
14 of 16 cities and winner buy-prints averaging 0.6-0.9, i.e. +0.1 to +0.6 per dollar.

Rule (all env-tunable): after WEATHER_LOCK_HOUR local, when the latest reading is at least
WEATHER_MARGIN below the day's running max and the max is at least WEATHER_MIN_GAP_MIN old, buy
the bucket containing the running max if its best ask is within [WEATHER_MIN_ASK, WEATHER_MAX_ASK].
Hold to resolution (next day). Paper by default; WEATHER_LIVE=1 places real FAK orders.

    .venv/bin/python -m scout.weather_lock            # loop (paper unless WEATHER_LIVE=1)
    .venv/bin/python -m scout.weather_lock --once     # one scan, print decisions
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import DATA_DIR, settings_from_env
from .crypto_lag import _get_json

LEDGER = DATA_DIR / "ledger_weather.json"
JOURNAL = DATA_DIR / "weather_journal.jsonl"
FEE_RATE = 0.05  # weather markets: taker 0.05 * p * (1-p) per share

CITY_TZ = {
    "hong-kong": "Asia/Hong_Kong", "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
    "toronto": "America/Toronto", "chicago": "America/Chicago", "miami": "America/New_York", "seattle": "America/Los_Angeles",
    "guangzhou": "Asia/Shanghai", "shenzhen": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "chengdu": "Asia/Shanghai", "sao-paulo": "America/Sao_Paulo", "atlanta": "America/New_York", "nyc": "America/New_York",
    "houston": "America/Chicago", "dallas": "America/Chicago", "austin": "America/Chicago", "seoul": "Asia/Seoul",
    "busan": "Asia/Seoul", "san-francisco": "America/Los_Angeles", "los-angeles": "America/Los_Angeles",
    "london": "Europe/London", "paris": "Europe/Paris", "tokyo": "Asia/Tokyo", "munich": "Europe/Berlin",
    "amsterdam": "Europe/Amsterdam", "denver": "America/Denver", "singapore": "Asia/Singapore", "karachi": "Asia/Karachi",
    "ankara": "Europe/Istanbul", "istanbul": "Europe/Istanbul", "taipei": "Asia/Taipei", "milan": "Europe/Rome",
    "rome": "Europe/Rome", "madrid": "Europe/Madrid", "barcelona": "Europe/Madrid", "dubai": "Asia/Dubai",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new-delhi": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne", "mexico-city": "America/Mexico_City",
    "bogota": "America/Bogota", "lima": "America/Lima", "santiago": "America/Santiago", "cairo": "Africa/Cairo",
    "lagos": "Africa/Lagos", "johannesburg": "Africa/Johannesburg", "moscow": "Europe/Moscow", "bangkok": "Asia/Bangkok",
    "jakarta": "Asia/Jakarta", "manila": "Asia/Manila", "kuala-lumpur": "Asia/Kuala_Lumpur", "ho-chi-minh-city": "Asia/Ho_Chi_Minh",
    "hanoi": "Asia/Ho_Chi_Minh", "riyadh": "Asia/Riyadh", "tel-aviv": "Asia/Jerusalem", "warsaw": "Europe/Warsaw",
    "berlin": "Europe/Berlin", "lisbon": "Europe/Lisbon", "dublin": "Europe/Dublin", "stockholm": "Europe/Stockholm",
    "oslo": "Europe/Oslo", "copenhagen": "Europe/Copenhagen", "helsinki": "Europe/Helsinki", "vienna": "Europe/Vienna",
    "zurich": "Europe/Zurich", "prague": "Europe/Prague", "budapest": "Europe/Budapest", "athens": "Europe/Athens",
    "phoenix": "America/Phoenix", "las-vegas": "America/Los_Angeles", "philadelphia": "America/New_York",
    "boston": "America/New_York", "washington": "America/New_York", "washington-dc": "America/New_York",
    "minneapolis": "America/Chicago", "montreal": "America/Toronto", "vancouver": "America/Vancouver",
    "calgary": "America/Edmonton", "honolulu": "Pacific/Honolulu", "anchorage": "America/Anchorage",
    "osaka": "Asia/Tokyo", "nairobi": "Africa/Nairobi", "auckland": "Pacific/Auckland", "brisbane": "Australia/Brisbane",
    "perth": "Australia/Perth", "san-diego": "America/Los_Angeles", "portland": "America/Los_Angeles",
    "salt-lake-city": "America/Denver", "detroit": "America/Detroit", "nashville": "America/Chicago",
    "new-orleans": "America/Chicago", "tampa": "America/New_York", "orlando": "America/New_York", "charlotte": "America/New_York",
    "kansas-city": "America/Chicago", "st-louis": "America/Chicago", "boise": "America/Boise", "sacramento": "America/Los_Angeles",
}
_SLUG = re.compile(r"^highest-temperature-in-(?P<city>[a-z\-]+?)-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$")
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


# ------------------------------------------------------------------ pure pieces (tested)
def parse_bucket(title: str) -> tuple[float, float] | None:
    """'77°F or below' -> (-inf, 77); '78-79°F' -> (78, 79); '33°C' -> (33, 33); '35°C or higher' -> (35, inf)."""
    nums = [int(x) for x in re.findall(r"\d+", title or "")]
    if not nums:
        return None
    low = title.lower()
    if "below" in low or "or less" in low:
        return (-math.inf, float(nums[0]))
    if "higher" in low or "above" in low or "or more" in low:
        return (float(nums[0]), math.inf)
    return (float(nums[0]), float(nums[-1]))


def bucket_unit(titles: list[str]) -> str:
    joined = " ".join(titles)
    return "F" if "°F" in joined or "F" in joined and "°C" not in joined else "C"


def reading_in_unit(temp_c: float, unit: str) -> int:
    """NOAA's Temp column: METAR Celsius converted to whole degrees F (US) or whole degrees C.
    Round half up, the way the page displays it."""
    value = temp_c * 9.0 / 5.0 + 32.0 if unit == "F" else temp_c
    return int(math.floor(value + 0.5))


def day_readings(obs: list[dict[str, Any]], tz: ZoneInfo, local_date: date, unit: str) -> list[tuple[datetime, int]]:
    """(local time, reading) for every METAR/SPECI of that local calendar day, oldest first."""
    out: list[tuple[datetime, int]] = []
    for row in obs:
        temp = row.get("temp")
        stamp = row.get("reportTime") or row.get("obsTime")
        if temp is None or stamp is None:
            continue
        try:
            when = datetime.fromtimestamp(float(stamp), tz=timezone.utc) if isinstance(stamp, (int, float)) else datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        local = when.astimezone(tz)
        if local.date() != local_date:
            continue
        out.append((local, reading_in_unit(float(temp), unit)))
    out.sort(key=lambda r: r[0])
    return out


def decide(
    readings: list[tuple[datetime, int]],
    now_local: datetime,
    *,
    lock_hour: float,
    margin: int,
    min_gap_min: float,
    max_obs_age_min: float = 90.0,
) -> dict[str, Any]:
    """Return {'buy': value or None, 'why': str, ...} for the running-max bucket."""
    if not readings:
        return {"buy": None, "why": "no readings"}
    hour = now_local.hour + now_local.minute / 60.0
    running_max = max(v for _, v in readings)
    t_max = next(t for t, v in readings if v == running_max)
    latest_t, latest = readings[-1]
    info = {"running_max": running_max, "t_max": t_max.strftime("%H:%M"), "latest": latest, "latest_t": latest_t.strftime("%H:%M"), "n": len(readings)}
    if hour < lock_hour:
        return {"buy": None, "why": f"before lock hour {lock_hour:g}", **info}
    if (now_local - latest_t).total_seconds() > max_obs_age_min * 60:
        return {"buy": None, "why": "stale observations", **info}
    if latest > running_max - margin:
        return {"buy": None, "why": f"latest {latest} within {margin} of max {running_max}", **info}
    if (now_local - t_max).total_seconds() < min_gap_min * 60:
        return {"buy": None, "why": "max too recent", **info}
    return {"buy": running_max, "why": "locked", **info}


def market_for_value(markets: list[dict[str, Any]], value: float) -> dict[str, Any] | None:
    for m in markets:
        rng = parse_bucket(m.get("groupItemTitle") or m.get("question") or "")
        if rng and rng[0] <= value <= rng[1]:
            return m
    return None


def parse_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """City, local date, station (from the NOAA resolution URL), tz, unit; None if not tradeable."""
    match = _SLUG.match(event.get("slug") or "")
    if not match:
        return None
    city = match.group("city")
    tz_name = CITY_TZ.get(city)
    if not tz_name:
        return None
    markets = [m for m in event.get("markets") or [] if not m.get("closed")]
    if not markets:
        return None
    desc = markets[0].get("description") or ""
    site = re.search(r"timeseries\?site=([a-z0-9]{3,4})", desc, re.I)
    if not site:
        return None  # not NOAA-resolved (e.g. Hong Kong Observatory): the feed would not match the source
    local_date = date(int(match.group("year")), _MONTHS.get(match.group("month"), 0) or 1, int(match.group("day")))
    return {
        "slug": event["slug"], "city": city, "tz": ZoneInfo(tz_name), "date": local_date, "station": site.group(1).upper(),
        "unit": bucket_unit([m.get("groupItemTitle") or "" for m in markets]), "markets": markets, "event_id": event.get("id"),
    }


# ------------------------------------------------------------------ feeds
def fetch_events(ua: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for offset in range(0, 800, 100):
        rows = _get_json(f"https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100&offset={offset}&order=volume24hr&ascending=false", ua, timeout=20)
        if not rows:
            break
        out.extend(e for e in rows if str(e.get("slug") or "").startswith("highest-temperature-in-"))
    return out


_METAR_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def fetch_metar(station: str, ua: str, ttl: float = 240.0) -> list[dict[str, Any]]:
    cached = _METAR_CACHE.get(station)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    rows = _get_json(f"https://aviationweather.gov/api/data/metar?ids={station}&hours=30&format=json", ua, timeout=20)
    rows = rows if isinstance(rows, list) else []
    _METAR_CACHE[station] = (time.time(), rows)
    return rows


def best_ask(client: Any, token: str) -> tuple[float | None, float]:
    book = client.get_order_book(token_id=token)
    asks = getattr(book, "asks", None) or []
    if not asks:
        return None, 0.0
    levels = sorted((float(l.price), float(l.size)) for l in asks)
    return levels[0][0], levels[0][1]


# ------------------------------------------------------------------ ledger
def load_ledger(live: bool) -> dict[str, Any]:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            pass
    return {"mode": "live" if live else "paper", "cash": 100.0, "positions": [], "closed": [], "fills": []}


def save_ledger(ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, default=str))
    tmp.replace(LEDGER)


def journal(event: dict[str, Any]) -> None:
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), **event}, default=str) + "\n")


# ------------------------------------------------------------------ the loop
def scan(ledger: dict[str, Any], *, live: bool, client: Any, trading: Any, ua: str, verbose: bool = True) -> list[dict[str, Any]]:
    lock_hour = _f("WEATHER_LOCK_HOUR", 17.0)
    min_gap = _f("WEATHER_MIN_GAP_MIN", 60.0)
    max_ask = _f("WEATHER_MAX_ASK", 0.90)
    min_ask = _f("WEATHER_MIN_ASK", 0.05)
    stake_usd = _f("WEATHER_STAKE_USD", 10.0)
    max_open = int(_f("WEATHER_MAX_OPEN", 5))
    actions: list[dict[str, Any]] = []
    held = {p["market_id"] for p in ledger["positions"]} | {p["market_id"] for p in ledger["closed"][-400:]}
    for event in fetch_events(ua):
        info = parse_event(event)
        if not info:
            continue
        now_local = datetime.now(info["tz"])
        if now_local.date() != info["date"]:
            continue
        if any(m["id"] in held for m in info["markets"]):
            continue
        try:
            obs = fetch_metar(info["station"], ua)
        except Exception as exc:
            journal({"event": "metar_error", "city": info["city"], "station": info["station"], "error": str(exc)[:120]})
            continue
        readings = day_readings(obs, info["tz"], info["date"], info["unit"])
        # WEATHER_MARGIN units below the running max (1 in the station's unit by default: the
        # refined backtest's best cells; 2 waits so long the bucket is already at 0.99)
        margin = int(_f("WEATHER_MARGIN", 1))
        verdict = decide(readings, now_local, lock_hour=lock_hour, margin=margin, min_gap_min=min_gap)
        row = {"event": "decision", "city": info["city"], "station": info["station"], "unit": info["unit"], "local": now_local.strftime("%H:%M"), **verdict}
        if verdict["buy"] is None:
            if verbose and readings and now_local.hour >= lock_hour - 1:
                print(f"  {info['city']:<14} {row['local']} max={verdict.get('running_max')}@{verdict.get('t_max')} latest={verdict.get('latest')} -> {verdict['why']}")
            journal(row)
            continue
        market = market_for_value(info["markets"], verdict["buy"])
        if not market:
            journal({**row, "why": "no bucket market"}); continue
        try:
            tokens = json.loads(market.get("clobTokenIds") or "[]")
        except Exception:
            tokens = []
        if not tokens:
            journal({**row, "why": "no token"}); continue
        yes_token = tokens[0]
        try:
            ask, ask_size = best_ask(client, yes_token)
        except Exception as exc:
            journal({**row, "why": f"book error {str(exc)[:80]}"}); continue
        row.update({"bucket": market.get("groupItemTitle"), "market_id": market["id"], "ask": ask, "ask_size": ask_size})
        if ask is None or not (min_ask <= ask <= max_ask):
            row["why"] = f"ask {ask} outside [{min_ask}, {max_ask}]"
            print(f"  {info['city']:<14} {row['local']} LOCKED max={verdict['running_max']} bucket={row['bucket']} ask={ask} -> skip ({row['why']})")
            journal(row); continue
        if len(ledger["positions"]) >= max_open:
            row["why"] = "max open positions"; journal(row); continue
        stake = round(min(stake_usd, ask * max(ask_size, 0) if ask_size else stake_usd), 2)
        if stake < 1.0:
            row["why"] = "no size at the ask"; journal(row); continue
        fill_price, shares, raw = ask, round(stake / ask, 4), {"paper": True}
        if live:
            from .execute import LiveDisabled, _order_payload, _taker_buy  # noqa: WPS433

            try:
                raw = _order_payload(_taker_buy(trading, yes_token, stake, max_price=min(0.99, round(ask + 0.01, 3))))
                making, taking = float(raw.get("making_amount") or 0), float(raw.get("taking_amount") or 0)
                if making > 0 and taking > 0:
                    stake, shares, fill_price = round(making, 4), round(taking, 4), round(making / taking, 4)
                else:
                    row["why"] = "no fill"; journal({**row, "raw": str(raw)[:300]}); continue
            except LiveDisabled as exc:
                row["why"] = f"order rejected: {str(exc)[:120]}"; journal(row); print("  order rejected:", str(exc)[:120]); continue
        fee = round(shares * FEE_RATE * fill_price * (1 - fill_price), 4)
        pos = {"market_id": market["id"], "condition_id": market.get("conditionId"), "token": yes_token, "city": info["city"], "date": str(info["date"]),
               "bucket": market.get("groupItemTitle"), "price": fill_price, "shares": shares, "stake": stake, "fee": fee, "mode": "live" if live else "paper",
               "opened_at": datetime.now(timezone.utc).isoformat(), "running_max": verdict["running_max"], "t_max": verdict["t_max"], "latest": verdict["latest"], "raw": str(raw)[:300]}
        ledger["positions"].append(pos)
        ledger["cash"] = round(float(ledger["cash"]) - stake - fee, 4)
        ledger["fills"].append(pos)
        save_ledger(ledger)
        journal({**row, "action": "buy", "price": fill_price, "shares": shares, "stake": stake, "fee": fee})
        actions.append(pos)
        print(f"  BUY {info['city']:<12} {row['bucket']} @ {fill_price} ${stake} ({'LIVE' if live else 'paper'}) max={verdict['running_max']}@{verdict['t_max']} latest={verdict['latest']}")
    return actions


def settle(ledger: dict[str, Any], *, live: bool, trading: Any, ua: str) -> None:
    for pos in list(ledger["positions"]):
        try:
            m = _get_json(f"https://gamma-api.polymarket.com/markets/{pos['market_id']}", ua, timeout=20)
        except Exception:
            continue
        if not isinstance(m, dict) or not m.get("closed"):
            continue
        try:
            won = float(json.loads(m.get("outcomePrices") or "[0]")[0]) > 0.5
        except Exception:
            continue
        payout = round(float(pos["shares"]), 4) if won else 0.0
        if live and won:
            from .execute import LiveDisabled, live_redeem

            try:
                live_redeem({"market_id": pos["market_id"]}, settings_from_env())
            except LiveDisabled as exc:
                journal({"event": "redeem_failed", "market_id": pos["market_id"], "error": str(exc)[:120]})
        pnl = round(payout - float(pos["stake"]) - float(pos["fee"]), 4)
        ledger["cash"] = round(float(ledger["cash"]) + payout, 4)
        ledger["positions"].remove(pos)
        ledger["closed"].append({**pos, "won": won, "payout": payout, "pnl": pnl, "closed_at": datetime.now(timezone.utc).isoformat()})
        save_ledger(ledger)
        journal({"event": "settle", "city": pos["city"], "date": pos["date"], "bucket": pos["bucket"], "won": won, "pnl": pnl})
        print(f"  SETTLE {pos['city']:<12} {pos['date']} {pos['bucket']} {'WON' if won else 'LOST'} pnl {pnl:+.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    live = (os.getenv("WEATHER_LIVE") or "0").strip() == "1"
    settings = settings_from_env()
    from polymarket import PublicClient

    client = PublicClient()
    trading = None
    if live:
        from .execute import trading_client

        trading = trading_client(settings)
    ledger = load_ledger(live)
    ledger["mode"] = "live" if live else "paper"
    poll = _f("WEATHER_POLL_S", 300.0)
    print(f"weather lock {'LIVE' if live else 'paper'} · lock hour {_f('WEATHER_LOCK_HOUR', 17.0):g} · ask {_f('WEATHER_MIN_ASK', 0.05)}-{_f('WEATHER_MAX_ASK', 0.90)} · stake ${_f('WEATHER_STAKE_USD', 10.0):g} · poll {poll:g}s", flush=True)
    while True:
        started = time.time()
        try:
            settle(ledger, live=live, trading=trading, ua=settings.user_agent)
            scan(ledger, live=live, client=client, trading=trading, ua=settings.user_agent)
        except Exception as exc:
            print("cycle error:", type(exc).__name__, str(exc)[:160], flush=True)
        open_n = len(ledger["positions"])
        closed = ledger["closed"]
        print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z open={open_n} closed={len(closed)} won={sum(1 for c in closed if c['won'])} pnl={sum(c['pnl'] for c in closed):+.2f} cash={ledger['cash']:.2f}", flush=True)
        if args.once:
            return
        time.sleep(max(5.0, poll - (time.time() - started)))


if __name__ == "__main__":
    main()
