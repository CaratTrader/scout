"""Kalshi strategy lab: paper trading on live data with a simulated $50 bankroll per strategy (docs/KALSHI_LAB.md).

Strategies (parameters frozen in data/kalshi_lab/registry.json; only the nightly loop may change their status):
  rain_n  - KXRAIN ("rain today in <city>"): at HOUR local, if the settlement station's METARs show no measurable
            precipitation so far in the climate day and no precipitation in the last three reports, buy NO at
            1 - yes_bid when that price is within [0.02, cap].
  weather - mirror of the Kalshi temperature paper bot (data/ledger_kalshi_temp.json, R0 + R2m): its fills re-sized to
            this bankroll's stake rule. No extra API calls.
Every signal is also logged at a unit stake (signals.jsonl), so statistics do not depend on bankroll limits.
Sizing: stake = min(quarter-Kelly with the win rate shrunk halfway to the price, max_stake, cash), contracts capped by
the order-book depth at the price. Halts (bankroll only): 3 losses in a row, $10 daily loss, $15 cumulative loss.
Kalshi calls are made only in the temperature bot's idle window (shared ~1 request/s limit)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zoneinfo
from pathlib import Path
from typing import Any

K = "https://api.elections.kalshi.com/trade-api/v2"
AWC = "https://aviationweather.gov/api/data/metar"
ROOT = Path(os.getenv("KLAB_ROOT") or "data/kalshi_lab")
PAPER = ROOT / "paper"; REG = ROOT / "registry.json"; SIGNALS = ROOT / "signals.jsonl"; JOURNAL = ROOT / "lab_journal.jsonl"
BOT_LOG = Path("data/kalshi_temp.log")
POLL_S = float(os.getenv("KLAB_POLL_S") or 300)
FEE = 0.07
START = "2026-10-07"   # lab start: the weather mirror counts fills opened from this day
RAIN_STATIONS = {"ABQ": "ABQ", "ATL": "ATL", "AUS": "AUS", "BOS": "BOS", "CHI": "ORD", "CLL": "CLL", "CMH": "CMH", "DAL": "DFW", "DC": "DCA",
                 "DEN": "DEN", "EWR": "EWR", "HOU": "HOU", "LAX": "LAX", "LEX": "LEX", "LV": "LAS", "MIA": "MIA", "MIN": "MSP", "MKE": "MKE",
                 "NOLA": "MSY", "NYC": "NYC", "OKC": "OKC", "PHIL": "PHL", "PHX": "PHX", "PIT": "PIT", "PVD": "PVD", "SATX": "SAT", "SEA": "SEA",
                 "SFO": "SFO", "SGF": "SGF", "TTN": "TTN"}
TZ = {"ABQ": "America/Denver", "ATL": "America/New_York", "AUS": "America/Chicago", "BOS": "America/New_York", "ORD": "America/Chicago",
      "CLL": "America/Chicago", "CMH": "America/New_York", "DFW": "America/Chicago", "DCA": "America/New_York", "DEN": "America/Denver",
      "EWR": "America/New_York", "HOU": "America/Chicago", "LAX": "America/Los_Angeles", "LEX": "America/New_York", "LAS": "America/Los_Angeles",
      "MIA": "America/New_York", "MSP": "America/Chicago", "MKE": "America/Chicago", "MSY": "America/Chicago", "NYC": "America/New_York",
      "OKC": "America/Chicago", "PHL": "America/New_York", "PHX": "America/Phoenix", "PIT": "America/New_York", "PVD": "America/New_York",
      "SAT": "America/Chicago", "SEA": "America/Los_Angeles", "SFO": "America/Los_Angeles", "SGF": "America/Chicago", "TTN": "America/New_York"}
DEFAULT_REGISTRY = {
    "rain_n15": {"family": "rain", "status": "paper", "frozen": "2026-10-07", "params": {"hour": 15, "cap": 0.97, "q_hat": 0.81},
                 "source": "lab/kalshi/rain.py discovery 2026-10-07 (partial data): n=205, win 81%, +14%/$, t 1.8"},
    "weather": {"family": "weather", "status": "paper", "frozen": "2026-10-06", "params": {"q_hat": 0.89},
                "source": "lab/us/kalshi_backtest.py R0+R2m: 47 trades, 89% win, +19%/$, t($) 2.6"},
}
LIMITS = {"bankroll": 50.0, "max_stake": 5.0, "kelly": 0.25, "halt_streak": 3, "halt_day": 10.0, "halt_total": 15.0}


def journal(ev: dict[str, Any]) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), **ev}) + "\n")


def fee(px: float, n: float) -> float:
    return round(FEE * px * (1 - px) * n, 4)


# ------------------------------------------------------------------ data
_LAST = [0.0]


def kget(url: str) -> Any:
    """Kalshi GET in the temperature bot's idle window (polls start every 60 s and take ~20 s), >= 1.2 s apart."""
    for attempt in range(4):
        if BOT_LOG.exists() and time.time() - BOT_LOG.stat().st_mtime < 180:
            while time.time() - BOT_LOG.stat().st_mtime > 33:
                time.sleep(0.5)
        wait = 1.2 - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-kalshi-lab", "Accept": "application/json"}), timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (attempt + 1)); continue
            journal({"event": "error", "url": url[-80:], "err": f"HTTP {e.code}"}); return None
        except Exception as exc:
            journal({"event": "error", "url": url[-80:], "err": str(exc)[:100]}); return None
    return None


def depth(ticker: str, side: str, n: int = 5) -> list[list[float]] | None:
    d = kget(f"{K}/markets/{ticker}/orderbook")
    if d is None:
        return None
    ob = d.get("orderbook_fp") or {}
    lv = ob.get("no_dollars" if side == "YES" else "yes_dollars") or []
    return [[round(1 - float(p), 4), float(s)] for p, s in sorted(((float(p), float(s)) for p, s in lv), key=lambda x: -x[0])[:n]]


def metars(stations: list[str]) -> dict[str, list[dict]] | None:
    url = f"{AWC}?ids={','.join('K' + s for s in stations)}&format=json&hours=30"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-kalshi-lab"}), timeout=40) as r:
            rows = json.loads(r.read())
    except Exception as exc:
        journal({"event": "error", "url": "awc", "err": str(exc)[:100]}); return None
    out: dict[str, list[dict]] = {}
    for r in rows or []:
        out.setdefault(str(r.get("icaoId", ""))[1:], []).append(r)
    return out


def precip_in(raw: str) -> float | None:
    """Same parser as lab/kalshi/rain.py: max of hourly P and 6-hour 6RRRR groups in inches (0.0 = trace)."""
    if " RMK " not in f" {raw} ":
        return None
    toks = raw.split(" RMK ", 1)[1].split(); best = None; skip = 0
    for i, tok in enumerate(toks):
        if skip:
            skip -= 1; continue
        if tok == "PK" and i + 1 < len(toks) and toks[i + 1] == "WND":
            skip = 2; continue
        m = re.fullmatch(r"P(\d{4})", tok) or re.fullmatch(r"6(\d{4})", tok)
        if m:
            best = max(best or 0.0, int(m.group(1)) / 100.0)
    return best


WX = re.compile(r"\s(?:\+|-|VC)?(?:TS|SH)?(?:RA|DZ|SN|PL|GR|GS|UP)\w*")


def rain_state(rows: list[dict], tz: zoneinfo.ZoneInfo, now: dt.datetime) -> dict[str, Any]:
    loc = now.astimezone(tz); std = loc.utcoffset() - (loc.dst() or dt.timedelta(0))
    d = (now.astimezone(dt.timezone.utc) + std).date(); start = dt.datetime.combine(d, dt.time(0), tzinfo=dt.timezone(std))
    reps = []
    for r in rows:
        try:
            t = dt.datetime.fromtimestamp(float(r["obsTime"]), dt.timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if start <= t <= now:
            raw = r.get("rawOb") or ""; p = precip_in(raw)
            reps.append((t, p, bool(WX.search(" " + raw.split(" RMK")[0] + " ")) or (p is not None)))
    reps.sort()
    measurable = any(p is not None and p >= 0.01 and t - start >= dt.timedelta(minutes=70) for t, p, _ in reps)
    return {"day": d, "n": len(reps), "measurable": measurable, "recent_wet": [w for _, _, w in reps[-3:]]}


# ------------------------------------------------------------------ ledgers
def load(name: str) -> dict[str, Any]:
    f = PAPER / f"{name}.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"name": name, "cash": LIMITS["bankroll"], "start": LIMITS["bankroll"], "positions": [], "fills": [], "streak": 0, "halted": None, "decided": [], "mirrored": []}


def save(led: dict[str, Any]) -> None:
    PAPER.mkdir(parents=True, exist_ok=True); f = PAPER / f"{led['name']}.json"
    tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(led, indent=1)); tmp.replace(f)


def stake_for(led: dict[str, Any], px: float, q_hat: float) -> float:
    q = px + 0.5 * (q_hat - px)                      # shrink the measured win rate halfway to the price
    f = max(0.0, (q - px) / (1 - px)) * LIMITS["kelly"]
    return round(min(LIMITS["max_stake"], f * (led["cash"] + sum(p["stake"] for p in led["positions"])), led["cash"]), 2)


def halted(led: dict[str, Any]) -> str | None:
    today = dt.date.today().isoformat()
    day_pnl = sum(f["pnl"] for f in led["fills"] if f.get("settled", "")[:10] == today)
    total = sum(f["pnl"] for f in led["fills"])
    if led["streak"] >= LIMITS["halt_streak"]:
        return f"{led['streak']} losses in a row"
    if day_pnl <= -LIMITS["halt_day"]:
        return f"daily loss ${-day_pnl:.2f}"
    if total <= -LIMITS["halt_total"]:
        return f"cumulative loss ${-total:.2f}"
    return None


def signal(name: str, rec: dict[str, Any]) -> None:
    with SIGNALS.open("a") as fh:
        fh.write(json.dumps({"ts": time.time(), "strategy": name, **rec}) + "\n")


def open_position(led: dict[str, Any], ticker: str, side: str, px: float, q_hat: float, close: str | None, why: str, book: list | None) -> dict | None:
    stake = stake_for(led, px, q_hat)
    if stake < 0.5:
        return None
    size = sum(s for p, s in (book or []) if p <= px + 0.005) if book else 0.0
    n = round(min(stake / px, size), 2)
    if n < 1 or led["cash"] < n * px + fee(px, n):
        return None
    pos = {"ticker": ticker, "side": side, "px": px, "shares": n, "stake": round(n * px, 4), "fee": fee(px, n), "opened": dt.datetime.now(dt.timezone.utc).isoformat(),
           "close": close, "why": why}
    led["cash"] = round(led["cash"] - pos["stake"] - pos["fee"], 4); led["positions"].append(pos)
    journal({"event": "fill", "strategy": led["name"], **pos})
    return pos


def settle(led: dict[str, Any]) -> None:
    for pos in list(led["positions"]):
        if pos.get("close") and dt.datetime.fromisoformat(pos["close"].replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc):
            continue
        m = (kget(f"{K}/markets/{pos['ticker']}") or {}).get("market") or {}
        if m.get("result") not in ("yes", "no"):
            continue
        yes = m["result"] == "yes"; win = yes if pos["side"] == "YES" else not yes
        pnl = round((pos["shares"] if win else 0.0) - pos["stake"] - pos["fee"], 4)
        led["cash"] = round(led["cash"] + (pos["shares"] if win else 0.0), 4); led["positions"].remove(pos)
        led["streak"] = 0 if win else led["streak"] + 1
        led["fills"].append({**pos, "settled": dt.datetime.now(dt.timezone.utc).isoformat(), "won": win, "pnl": pnl})
        journal({"event": "settle", "strategy": led["name"], "ticker": pos["ticker"], "won": win, "pnl": pnl})


# ------------------------------------------------------------------ strategies
def run_rain(name: str, spec: dict[str, Any], now: dt.datetime) -> str:
    led = load(name); p = spec["params"]; notes = []
    due = []
    for code, stn in RAIN_STATIONS.items():
        tz = zoneinfo.ZoneInfo(TZ[stn]); loc = now.astimezone(tz)
        key = f"{code}:{loc.date()}"
        if loc.hour == p["hour"] and loc.minute < 30 and key not in led["decided"]:
            due.append((code, stn, tz, key))
    if due:
        obs = metars(sorted({stn for _, stn, _, _ in due}))
        mk = {}
        d = kget(f"{K}/markets?series_ticker=KXRAIN&status=open&limit=200")
        for m in (d or {}).get("markets") or []:
            mk[m["ticker"]] = m
        for code, stn, tz, key in due:
            if obs is None or d is None:
                break   # retry on the next poll (key not marked decided)
            st = rain_state(obs.get(stn, []), tz, now)
            ticker = f"KXRAIN-{st['day'].strftime('%y%b%d').upper()}-{code}"
            led["decided"].append(key); m = mk.get(ticker)
            if not m:
                notes.append(f"{code}: no open market"); continue
            if st["n"] < 3 or st["measurable"] or any(st["recent_wet"]):
                notes.append(f"{code}: skip ({'rain so far' if st['measurable'] else 'wet recent reports' if any(st['recent_wet']) else 'too few reports'})"); continue
            bid = float(m.get("yes_bid_dollars") or 0); px = round(1 - bid, 4)
            if not (0.02 <= px <= p["cap"]) or bid <= 0:
                notes.append(f"{code}: NO price {px:.2f} outside 0.02-{p['cap']}"); continue
            book = depth(ticker, "NO")
            signal(name, {"ticker": ticker, "side": "NO", "px": px, "close": m.get("close_time"), "book": book})
            if led["halted"] or halted(led):
                led["halted"] = led["halted"] or halted(led); notes.append(f"{code}: signal (bankroll halted: {led['halted']})"); continue
            pos = open_position(led, ticker, "NO", px, p["q_hat"], m.get("close_time"), f"no measurable rain by {p['hour']}:00, last 3 reports dry", book)
            notes.append(f"{code}: NO @ {px:.2f}" + (f" x{pos['shares']:.0f}" if pos else " (no size)"))
        led["decided"] = led["decided"][-400:]
    settle(led); save(led)
    return f"{name}: cash ${led['cash']:.2f} open {len(led['positions'])} " + "; ".join(notes)


def run_weather(name: str, spec: dict[str, Any], now: dt.datetime) -> str:
    """Mirror the temperature bot's fills (opened since START) at this bankroll's stake; settle when it settles."""
    led = load(name); src = Path("data/ledger_kalshi_temp.json")
    if not src.exists():
        return f"{name}: no source ledger"
    bot = json.loads(src.read_text())
    for f in bot.get("positions", []) + bot.get("fills", []):
        k = f"{f['ticker']}|{f['side']}"
        if f.get("opened", "")[:10] < START or k in led["mirrored"]:
            continue
        led["mirrored"].append(k)
        signal(name, {"ticker": f["ticker"], "side": f["side"], "px": f["px"], "rule": f.get("rule")})
        if led["halted"] or halted(led):
            led["halted"] = led["halted"] or halted(led); continue
        stake = stake_for(led, f["px"], spec["params"]["q_hat"]); n = round(min(stake / f["px"], f["shares"]), 2)
        if n >= 1 and led["cash"] >= n * f["px"] + fee(f["px"], n):
            pos = {"ticker": f["ticker"], "side": f["side"], "px": f["px"], "shares": n, "stake": round(n * f["px"], 4), "fee": fee(f["px"], n),
                   "opened": f["opened"], "close": f.get("end_date"), "why": f.get("why", "")}
            led["cash"] = round(led["cash"] - pos["stake"] - pos["fee"], 4); led["positions"].append(pos)
    done = {f"{f['ticker']}|{f['side']}": f for f in bot.get("fills", []) if "won" in f}
    for pos in list(led["positions"]):
        f = done.get(f"{pos['ticker']}|{pos['side']}")
        if f:
            win = f["won"]; pnl = round((pos["shares"] if win else 0.0) - pos["stake"] - pos["fee"], 4)
            led["cash"] = round(led["cash"] + (pos["shares"] if win else 0.0), 4); led["positions"].remove(pos)
            led["streak"] = 0 if win else led["streak"] + 1
            led["fills"].append({**pos, "settled": f.get("settled") or now.isoformat(), "won": win, "pnl": pnl})
    save(led)
    return f"{name}: cash ${led['cash']:.2f} open {len(led['positions'])} settled {len(led['fills'])}"


RUNNERS = {"rain": run_rain, "weather": run_weather}


def registry() -> dict[str, Any]:
    if not REG.exists():
        ROOT.mkdir(parents=True, exist_ok=True); REG.write_text(json.dumps(DEFAULT_REGISTRY, indent=1))
    return json.loads(REG.read_text())


def main() -> None:
    once = "--once" in sys.argv
    print(f"Kalshi lab paper: strategies {[k for k, v in registry().items() if v['status'] in ('paper', 'gate-pass')]}, bankroll ${LIMITS['bankroll']:.0f} each, poll {POLL_S:.0f}s", flush=True)
    while True:
        t0 = time.monotonic(); now = dt.datetime.now(dt.timezone.utc)
        for name, spec in registry().items():
            if spec.get("status") not in ("paper", "gate-pass", "monitor") or spec["family"] not in RUNNERS:
                continue
            try:
                print(now.strftime("%m-%d %H:%M"), RUNNERS[spec["family"]](name, spec, now), flush=True)
            except Exception as exc:
                print(f"{name} error: {type(exc).__name__}: {exc}", flush=True); journal({"event": "cycle_error", "strategy": name, "err": str(exc)[:200]})
        if once:
            break
        time.sleep(max(0.0, t0 + POLL_S - time.monotonic()))


if __name__ == "__main__":
    main()
