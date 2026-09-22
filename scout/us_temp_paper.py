"""Paper trader for Polymarket US daily-high-temperature ladders (observed-max lock, US-legal venue).

Rules (see docs/US_RESEARCH.md 2b, backtested on 553 city-days):
  R0  a bucket whose top is below the observed running max is dead -> buy NO; the open top bucket is certain once
      the running max reaches its floor -> buy YES. Certain modulo sensor errors (46/46 in the backtest).
  R2  after the afternoon peak (>= PEAK_HOUR local, temperature >= PEAK_FALL below the max for >= PEAK_MIN_SINCE
      minutes) buckets whose floor is >= max + R2_MARGIN are faded (buy NO) while their YES bid is still >= R2_MIN_BID.
      Margin 3F covers the gap between hourly METAR and the official report (0/1/2F on 35/55/10% of days).
  R1  (off by default) buy the bucket that contains the max and max+1 after the peak.
Observed max = today's METAR temperatures (T-group tenths, plus the 6-hourly maximum remark group) from 01:00 local,
because the NWS climate day is midnight-to-midnight local standard time. Paper fills use the catchable rule: a quote
must be present on two consecutive polls; the fill takes the later price and no more than the displayed size.
Paper only: no API keys, reads public endpoints. Settlement from /markets/{slug}/settlement.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
import zoneinfo
from pathlib import Path
from typing import Any

US = "https://gateway.polymarket.us/v1"
AWC = "https://aviationweather.gov/api/data/metar"
CITIES = {"sfo": ("KSFO", "America/Los_Angeles"), "lax": ("KLAX", "America/Los_Angeles"), "mdw": ("KMDW", "America/Chicago"),
          "nyc": ("KNYC", "America/New_York"), "mia": ("KMIA", "America/New_York")}
LEDGER = Path(os.getenv("USTEMP_LEDGER") or "data/ledger_us_temp.json")
JOURNAL = Path(os.getenv("USTEMP_JOURNAL") or "data/us_temp_journal.jsonl")
FEE = 0.0695


def env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


CFG = {
    "poll_s": env_f("USTEMP_POLL_S", 60), "stake": env_f("USTEMP_STAKE_USD", 25), "cash": env_f("USTEMP_CASH", 500),
    "r0_max_ask": env_f("USTEMP_MAX_ASK_R0", 0.97), "r2_margin": env_f("USTEMP_R2_MARGIN", 3), "r2_min_bid": env_f("USTEMP_R2_MIN_BID", 0.15),
    "peak_hour": env_f("USTEMP_PEAK_HOUR", 15), "peak_fall": env_f("USTEMP_PEAK_FALL", 1), "peak_min_since": env_f("USTEMP_PEAK_MIN_SINCE", 45),
    "confirm": int(env_f("USTEMP_CONFIRM_POLLS", 2)), "r1": int(env_f("USTEMP_R1", 0)), "r1_max_ask": env_f("USTEMP_R1_MAX_ASK", 0.80),
    "r1x": int(env_f("USTEMP_R1X", 1)), "r1x_max_ask": env_f("USTEMP_R1X_MAX_ASK", 0.90), "r1x_min_bid": env_f("USTEMP_R1X_MIN_BID", 0.10),
}
Z00_LOCAL_HOUR = {"sfo": 17, "lax": 17, "mdw": 19, "nyc": 20, "mia": 20}  # local hour of the 00Z report during daylight saving


def get(url: str, timeout: float = 20) -> Any:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-us-temp-paper"}), timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as exc:  # network blips are routine; the next poll retries
        journal({"event": "error", "url": url[-80:], "err": str(exc)[:120]})
        return None


def journal(ev: dict[str, Any]) -> None:
    ev = {"ts": time.time(), **ev}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps(ev) + "\n")


def fee(p: float, shares: float) -> float:
    return round(FEE * p * (1 - p) * shares, 4)


# ------------------------------------------------------------------ buckets
def bounds(slug: str) -> tuple[float, float]:
    """Venue convention: gte68lt69f = 68-69F inclusive, lt68f = 67F or below, gte76f = 76F or more."""
    lo = re.search(r"gte(\d+)", slug); hi = re.search(r"lt(\d+)", slug)
    if lo and hi:
        return float(lo.group(1)), float(hi.group(1))
    if hi:
        return -1e9, float(hi.group(1)) - 1
    return (float(lo.group(1)) if lo else -1e9), 1e9


# ------------------------------------------------------------------ METAR
def metar_temps_f(raw: str) -> list[float]:
    """[current reading F, optional 6-hour maximum F]. Current = T-group tenths (fallback TT/TD group); the 6-hour
    maximum remark (1sTTT, reported at 00/06/12/18Z) is the true max of the *previous six hours* from continuous
    sensing. Callers must check that those six hours lie inside the climate day before using it."""
    out: list[float] = []
    m = re.search(r"\bT([01])(\d{3})[01]\d{3}\b", raw)
    if m:
        c = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1); out.append(c * 9 / 5 + 32)
    else:
        m = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", raw)
        if m:
            c = float(m.group(1).replace("M", "-")); out.append(c * 9 / 5 + 32)
    m = re.search(r"\bRMK\b.*?\b1([01])(\d{3})\b", raw)
    if m and out:
        c = int(m.group(2)) / 10.0 * (-1 if m.group(1) == "1" else 1); out.append(c * 9 / 5 + 32)
    return out


def observed(station: str, tz: zoneinfo.ZoneInfo, now: dt.datetime, fetch=None) -> dict[str, Any] | None:
    """Today's (climate-day) running max, latest temperature and time of the max, from aviationweather.gov."""
    fetch = fetch or (lambda: get(f"{AWC}?ids={station}&format=json&hours=30"))
    rows = fetch() or []
    obs: list[tuple[dt.datetime, float]] = []; has_00z = False
    day_start = now.astimezone(tz).replace(hour=1, minute=0, second=0, microsecond=0)
    for r in rows:
        raw = r.get("rawOb") or ""
        t = r.get("reportTime") or r.get("obsTime")
        try:
            ts = dt.datetime.fromtimestamp(float(t), dt.timezone.utc) if isinstance(t, (int, float)) else dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        lt = ts.astimezone(tz)
        if lt < day_start or lt > now.astimezone(tz):
            continue
        temps = metar_temps_f(raw)
        if temps:
            obs.append((lt, temps[0], False))
        if len(temps) > 1 and lt - dt.timedelta(hours=6) >= day_start:  # 6-hour window entirely inside today's climate day
            window = [f for lt2, f, tr in obs if not tr and lt - dt.timedelta(hours=6) <= lt2 <= lt]
            if window and temps[1] > max(window) + 3.0:  # far above every hourly reading in its window: sensor artefact (KNYC 2026-08-27)
                continue
            obs.append((lt, temps[1], True))                             # trusted: a computed maximum, not a sensor spike
            if ts.hour == 0 or ts.hour == 23:  # the 00Z report (23:5x-00:0xZ) carries the afternoon maximum
                has_00z = True
    if not obs:
        return None
    obs.sort()
    # corroborated max: a reading counts only if another reading within 90 minutes is >= it - 2.5F (lone sensor spikes
    # such as KNYC 2026-08-27, 80F in heavy rain with the maintenance flag while the official high was 77, are ignored)
    def ok(lt, f, trusted):
        if trusted:
            return True
        neigh = [f2 for lt2, f2, _ in obs if lt2 != lt and abs((lt2 - lt).total_seconds()) <= 5400]
        return not neigh or max(neigh) >= f - 2.5
    good = [(lt, f) for lt, f, tr in obs if ok(lt, f, tr)] or [(obs[-1][0], obs[-1][1])]
    mx = max(f for _, f in good); t_max = max(lt for lt, f in good if f == mx)
    latest = [f for lt, f, tr in obs if lt == obs[-1][0] and not tr] or [obs[-1][1]]
    return {"max": mx, "t_max": t_max, "latest": min(latest), "n": len(obs), "last_obs": obs[-1][0], "has_00z": has_00z}


# ------------------------------------------------------------------ signals
def signals(buckets: list[dict[str, Any]], ob: dict[str, Any], now_local: dt.datetime, cfg=CFG, city: str = "") -> list[dict[str, Any]]:
    """buckets: [{slug, bid, ask, bid_sz, ask_sz}] (YES prices). Returns candidate trades.
    R1x: once the 00Z six-hour maximum has been received (ob["has_00z"]), the day's high is known on ~98% of days
    (backtest: 722/735 station-days exact); buy the bucket holding it and fade every other bucket."""
    M = ob["max"]; T = ob["latest"]; r_m = int(M + 0.5)  # official report rounds to whole F
    since = (now_local - ob["t_max"]).total_seconds() / 60
    peak_passed = now_local.hour >= cfg["peak_hour"] and (M - T) >= cfg["peak_fall"] and since >= cfg["peak_min_since"]
    after_00z = bool(ob.get("has_00z")) and now_local.hour >= Z00_LOCAL_HOUR.get(city, 20)
    out = []
    for b in buckets:
        lo, hi = bounds(b["slug"]); bid, ask = b.get("bid"), b.get("ask")
        no_ask = (1 - bid) if bid else None
        if cfg["r1x"] and after_00z and lo <= r_m <= hi and ask and 0.02 <= ask <= cfg["r1x_max_ask"]:
            out.append({"rule": "R1x", "slug": b["slug"], "side": "YES", "px": round(ask, 3), "size": b.get("ask_sz") or 0, "why": f"00Z max {r_m} in bucket"})
        elif cfg["r1x"] and after_00z and not (lo <= r_m <= hi) and bid and bid >= cfg["r1x_min_bid"] and no_ask and 0.02 <= no_ask <= cfg["r0_max_ask"]:
            out.append({"rule": "R1x", "slug": b["slug"], "side": "NO", "px": round(no_ask, 3), "size": b.get("bid_sz") or 0, "why": f"00Z max {r_m} outside bucket"})
        elif hi + 0.5 <= M and no_ask and 0.02 <= no_ask <= cfg["r0_max_ask"]:
            out.append({"rule": "R0", "slug": b["slug"], "side": "NO", "px": round(no_ask, 3), "size": b.get("bid_sz") or 0, "why": f"top {hi:.0f} < max {M:.1f}"})
        elif hi >= 1e8 and M >= lo - 0.45 and ask and 0.02 <= ask <= cfg["r0_max_ask"]:
            out.append({"rule": "R0", "slug": b["slug"], "side": "YES", "px": round(ask, 3), "size": b.get("ask_sz") or 0, "why": f"max {M:.1f} >= floor {lo:.0f}"})
        elif peak_passed and lo >= r_m + cfg["r2_margin"] and bid and bid >= cfg["r2_min_bid"] and no_ask and no_ask >= 0.02:
            out.append({"rule": "R2", "slug": b["slug"], "side": "NO", "px": round(no_ask, 3), "size": b.get("bid_sz") or 0, "why": f"floor {lo:.0f} >= max {r_m}+{cfg['r2_margin']:.0f}, peak passed"})
        elif cfg["r1"] and peak_passed and lo <= r_m and r_m + 1 <= hi and ask and 0.02 <= ask <= cfg["r1_max_ask"]:
            out.append({"rule": "R1", "slug": b["slug"], "side": "YES", "px": round(ask, 3), "size": b.get("ask_sz") or 0, "why": f"bucket holds max {r_m} and {r_m+1}, peak passed"})
    return out


# ------------------------------------------------------------------ ledger
def load_ledger() -> dict[str, Any]:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {"cash": CFG["cash"], "start_cash": CFG["cash"], "positions": [], "fills": [], "pending": {}, "created": dt.datetime.now(dt.timezone.utc).isoformat()}


def save_ledger(led: dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp"); tmp.write_text(json.dumps(led, indent=1)); tmp.replace(LEDGER)


def confirm_and_fill(led: dict[str, Any], cands: list[dict[str, Any]], meta: dict[str, Any], cfg=CFG) -> list[dict[str, Any]]:
    """Catchable rule: the same (slug, side, rule) must be seen on `confirm` consecutive polls with a price no worse
    than the first sighting; fill at the latest price, size capped by the displayed size."""
    fills = []
    held = {(p["slug"], p["side"]) for p in led["positions"]}
    seen = {c["slug"] + "|" + c["side"] for c in cands}
    for key in list(led["pending"].keys()):
        if key not in seen:
            del led["pending"][key]
    for c in cands:
        key = c["slug"] + "|" + c["side"]
        if (c["slug"], c["side"]) in held:
            continue
        p = led["pending"].get(key)
        if not p:
            led["pending"][key] = {"n": 1, "px": c["px"], "rule": c["rule"]}; continue
        if c["px"] > p["px"] + 0.005:  # got worse: restart the confirmation
            led["pending"][key] = {"n": 1, "px": c["px"], "rule": c["rule"]}; continue
        p["n"] += 1
        if p["n"] < cfg["confirm"]:
            continue
        shares = round(min(cfg["stake"] / c["px"], float(c["size"] or 0)), 2)
        if shares < 1 or led["cash"] < shares * c["px"]:
            journal({"event": "skip", "reason": "size" if shares < 1 else "cash", **c, **meta}); del led["pending"][key]; continue
        stake = round(shares * c["px"], 4); f = fee(c["px"], shares)
        pos = {"slug": c["slug"], "side": c["side"], "px": c["px"], "shares": shares, "stake": stake, "fee": f, "rule": c["rule"], "why": c["why"],
               "opened": dt.datetime.now(dt.timezone.utc).isoformat(), **meta}
        led["cash"] = round(led["cash"] - stake - f, 4); led["positions"].append(pos); fills.append(pos); del led["pending"][key]
        journal({"event": "fill", **pos})
    return fills


def settle(led: dict[str, Any]) -> list[dict[str, Any]]:
    done = []
    for pos in list(led["positions"]):
        end = pos.get("end_date")
        if end and dt.datetime.fromisoformat(end.replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc):
            continue
        s = get(f"{US}/markets/{pos['slug']}/settlement")
        val = s.get("settlement") if isinstance(s, dict) else None
        if val is None:
            continue
        settle_px = float(val)  # YES value at settlement (1 or 0)
        payoff = pos["shares"] * (settle_px if pos["side"] == "YES" else 1 - settle_px)
        pnl = round(payoff - pos["stake"] - pos["fee"], 4)
        led["cash"] = round(led["cash"] + payoff, 4); led["positions"].remove(pos)
        fill = {**pos, "settled": dt.datetime.now(dt.timezone.utc).isoformat(), "settle_yes": settle_px, "pnl": pnl, "won": pnl > 0}
        led["fills"].append(fill); done.append(fill); journal({"event": "settle", **fill})
    return done


# ------------------------------------------------------------------ loop
def bbo(slug: str) -> dict[str, Any] | None:
    d = get(f"{US}/markets/{slug}/bbo")
    md = (d or {}).get("marketData") if isinstance(d, dict) else None
    if not md:
        return None
    def v(x):
        try:
            return float(x["value"]) if isinstance(x, dict) else (float(x) if x not in (None, "") else None)
        except (TypeError, ValueError, KeyError):
            return None
    def sz(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0
    return {"slug": slug, "bid": v(md.get("bestBid")), "ask": v(md.get("bestAsk")), "bid_sz": sz(md.get("bidShares")), "ask_sz": sz(md.get("askShares")), "state": md.get("state")}


_LADDERS: dict[str, list[dict[str, Any]]] = {}


def ladder(city: str, day: str) -> list[dict[str, Any]]:
    key = f"{city}:{day}"
    if key not in _LADDERS:
        ev = (get(f"{US}/events?slug=temp-{city}high-{day}") or {}).get("events") or []
        _LADDERS[key] = [{"slug": m["slug"], "end_date": m.get("endDate")} for m in (ev[0].get("markets") if ev else [])]
    return _LADDERS[key]


def poll(led: dict[str, Any], now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    n_c = n_f = 0; notes = []
    for city, (station, tzname) in CITIES.items():
        tz = zoneinfo.ZoneInfo(tzname); now_local = now.astimezone(tz); day = now_local.date().isoformat()
        if now_local.hour < 9:
            continue
        mk = ladder(city, day)
        if not mk:
            continue
        ob = observed(station, tz, now)
        if not ob:
            continue
        buckets = []
        for m in mk:
            q = bbo(m["slug"])
            if q and q.get("state", "").endswith("OPEN"):
                buckets.append(q)
        cands = signals(buckets, ob, now_local, city=city)
        for c in cands:
            journal({"event": "signal", "city": city, **c, "max": round(ob["max"], 1), "latest": round(ob["latest"], 1)})
        meta = {"city": city, "day": day, "end_date": next((m["end_date"] for m in mk if m["slug"] == (cands[0]["slug"] if cands else "")), None)}
        for c in cands:
            c_meta = dict(meta, end_date=next((m["end_date"] for m in mk if m["slug"] == c["slug"]), None))
            n_f += len(confirm_and_fill(led, [c], c_meta))
        n_c += len(cands)
        notes.append(f"{city} max={ob['max']:.0f} now={ob['latest']:.0f} cands={len(cands)}")
    settled = settle(led)
    led["updated"] = now.isoformat(); save_ledger(led)
    return f"us-temp paper cash={led['cash']:.2f} pos={len(led['positions'])} cands={n_c} fills={n_f} settled={len(settled)} | " + " ".join(notes)


def main() -> None:
    once = "--once" in sys.argv
    led = load_ledger()
    print(f"US temperature paper trader: stake ${CFG['stake']:.0f}, poll {CFG['poll_s']:.0f}s, R0<= {CFG['r0_max_ask']}, R2 margin {CFG['r2_margin']:.0f}F, R1 {'on' if CFG['r1'] else 'off'}", flush=True)
    while True:
        try:
            print(poll(led), flush=True)
        except Exception as exc:
            print(f"cycle error: {type(exc).__name__}: {exc}", flush=True); journal({"event": "cycle_error", "err": str(exc)[:200]})
        if once:
            break
        time.sleep(CFG["poll_s"])


if __name__ == "__main__":
    main()
