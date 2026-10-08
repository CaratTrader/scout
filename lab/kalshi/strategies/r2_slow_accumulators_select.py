"""Ex-ante sampling of archived markets for candle downloads (one call per market): for every complete event, the
'greater' strike whose climatological P(YES) at the market's own open (month-to-date then) is closest to 0.5.
Events whose best strike is below 0.05 or above 0.95 are skipped (no information). Uses no outcome data."""
from __future__ import annotations
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_slow_accumulators_data import load_markets, station_of
from lab.kalshi.strategies.r2_slow_accumulators_model import p_yes, mtd, ym, TZ

LIVE_FROM = (2026, 8)   # events from Aug 2026 on have live (batch) candles: full ladders


def events() -> dict[str, list[dict]]:
    ev = defaultdict(list)
    for m in load_markets().values():
        if m["e"].endswith("26OCT"):
            continue
        ev[m["e"]].append(m)
    return ev


def pick(L: list[dict]) -> tuple[dict, float] | None:
    best = None
    for m in L:
        if m["type"] != "greater" or m["floor"] is None:
            continue
        st = station_of(m); y, mo = ym(m["e"])
        if not st:
            continue
        od = dt.datetime.fromtimestamp(m["open"], ZoneInfo(TZ[st])).date()
        day = 1 if (od.year, od.month) < (y, mo) else od.day
        if (od.year, od.month) > (y, mo):
            continue
        have = mtd(st, y, mo, day)
        if have is None:
            continue
        p = p_yes(st, y, mo, day, float(m["floor"]), have)
        if p is None:
            continue
        if best is None or abs(p - 0.5) < abs(best[1] - 0.5):
            best = (m, p)
    if best and abs(best[1] - 0.5) <= 0.45:
        return best
    return None


def selected() -> list[str]:
    out = []
    for e, L in sorted(events().items(), key=lambda kv: max(m["close"] for m in kv[1])):
        if ym(e) >= LIVE_FROM:
            continue
        b = pick(L)
        if b:
            out.append(b[0]["t"])
    return out


if __name__ == "__main__":
    s = selected()
    print(len(s)); print(s)
