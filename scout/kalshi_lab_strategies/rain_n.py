"""rain_n - KXRAIN ("rain today in <city>"): at params.hour local, if the settlement station's METARs show no measurable
precipitation so far in the climate day and no precipitation in the last three reports, buy NO at 1 - yes_bid when that
price is within [0.02, params.cap]. One decision per city and day (ledger "decided")."""
from __future__ import annotations

import datetime as dt
import re
import zoneinfo
from typing import Any

from .base import Strategy

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


class RainN(Strategy):
    series = ("KXRAIN",)
    one_signal_per_market = False   # one decision per city and day already (ledger "decided")

    def _due(self, now: dt.datetime) -> list[tuple[str, str, zoneinfo.ZoneInfo, str]]:
        """Cities at params.hour local (first half hour) not yet decided today."""
        decided = set(self.ledger.get("decided", [])); out = []
        for code, stn in RAIN_STATIONS.items():
            tz = zoneinfo.ZoneInfo(TZ[stn]); loc = now.astimezone(tz)
            key = f"{code}:{loc.date()}"
            if loc.hour == self.params["hour"] and loc.minute < 30 and key not in decided:
                out.append((code, stn, tz, key))
        return out

    def series_now(self, now: dt.datetime) -> list[str]:
        return ["KXRAIN"] if self._due(now) else []

    def external_now(self, now: dt.datetime) -> dict[str, dict[str, Any]]:
        due = self._due(now)
        return {"metar": {"stations": sorted({stn for _, stn, _, _ in due})}} if due else {}

    def decide(self, now: dt.datetime, market_quotes: dict[str, dict[str, dict] | None], external_data: dict[str, Any]) -> list[dict[str, Any]]:
        due = self._due(now)
        if not due:
            return []
        p = self.params; led = self.ledger; obs = external_data.get("metar"); mk = market_quotes.get("KXRAIN"); out = []
        if obs is None or mk is None:
            return []   # retry on the next poll (nothing marked decided)
        for code, stn, tz, key in due:
            st = rain_state(obs.get(stn, []), tz, now)
            ticker = f"KXRAIN-{st['day'].strftime('%y%b%d').upper()}-{code}"
            led["decided"].append(key); m = mk.get(ticker)
            if not m:
                self.notes.append(f"{code}: no open market"); continue
            if st["n"] < 3 or st["measurable"] or any(st["recent_wet"]):
                self.notes.append(f"{code}: skip ({'rain so far' if st['measurable'] else 'wet recent reports' if any(st['recent_wet']) else 'too few reports'})"); continue
            bid = float(m.get("yes_bid_dollars") or 0); px = round(1 - bid, 4)
            if not (0.02 <= px <= p["cap"]) or bid <= 0:
                self.notes.append(f"{code}: NO price {px:.2f} outside 0.02-{p['cap']}"); continue
            out.append({"ticker": ticker, "side": "NO", "max_price": px, "why": f"no measurable rain by {p['hour']}:00, last 3 reports dry",
                        "label": code, "close": m.get("close_time"), "log": {"close": m.get("close_time")}})
        led["decided"] = led["decided"][-400:]
        return out


STRATEGY = RainN
