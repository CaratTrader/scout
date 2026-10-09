"""Shared definitions for r5_announced_date_slip: universe filter, issuer family, deadline parser, candle access.

Deadline D of a rung is parsed from the market title / yes_sub_title / rules text written at listing (never from the
close time, which moves to the actual event time when the event happens early):
  'by <Mon d, yyyy>'      -> the day after, 00:00 US/Eastern (approximated as 04:00 UTC)
  'before <Mon d, yyyy>'  -> that day 04:00 UTC
  'before <ISO ts>'       -> that instant
  'before <Mon yyyy>'     -> the 1st of that month, 04:00 UTC
  'before <yyyy>'         -> Jan 1, 04:00 UTC
  'on <Mon d, yyyy>'      -> exact-day bucket: D = next day 04:00 UTC (kind 'on')
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r5_announced_date_slip"
DAY = 86400
MON = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MON.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})

# issuer family of each series (the "at least 3 issuer families" requirement)
FAMILY = {
    "KXSPACEXSTARSHIP": "spacex_starship", "SPACEXSTARSHIP": "spacex_starship", "KXSPCXLAUNCH": "spacex_other",
    "KXARTEMISII": "nasa", "KXNEXTVULCAN": "ula",
    "GTA6": "rockstar", "KXGTA6": "rockstar", "KXGTA6ONTIME": "rockstar", "KXGTATRAILER": "rockstar",
    "KXSWITCH2RELEASE": "nintendo", "VISIONPRO": "apple", "KXVISIONPRO": "apple", "KXIPHONERELEASE": "apple",
    "KXGPT": "openai", "KXO3RELEASE": "openai", "KXGEMINI": "google", "KXCLAUDE5": "anthropic", "KXDEEPSEEKR2RELEASE": "deepseek",
    "VULTURES": "music", "KXVULTURES": "music", "KXSPOTIFYALBUMRELEASEDATEKANYE": "music", "KXSPOTIFYALBUMRELEASEDATEDRAKE": "music",
    "KXMEDIARELEASEICEMAN": "music", "KXALBUMRELEASEDATEASAP": "music", "KXSPOTIFYALBUMRELEASEDATEJACKBOYS2": "music",
    "KXSPOTIFYALBUMRELEASEDATEBABYBOI": "music", "KXALBUMRELEASEDATE": "music", "KXALBUMRELEASEDATEUZI": "music",
    "KXMEDIARELEASEADDTRAILER": "marvel", "KXSPOTIFYWRAPPEDRELEASE": "spotify",
}
# excluded although listed: not a date question about one issuer item (multi-artist 'this year' lists, launch-outcome
# props, intraday launch-time ladders, 'this year' yes/no lists)
EXCLUDE_SERIES = {"KXSPOTIFYALBUMRELEASE", "NEWALBUM", "KXNEWALBUM", "KXSTARSHIPLAUNCH", "KXSTARSHIP", "KXJACKBOYS2"}


def _d(y: int, mo: int, d: int, h: int = 4) -> int:
    return int(dt.datetime(y, mo, d, h, tzinfo=dt.timezone.utc).timestamp())


DATE_RE = re.compile(r"\b(by|before|on)\s+(?:the\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
ISO_RE = re.compile(r"\bbefore\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", re.I)
MONTH_RE = re.compile(r"\b(by|before)\s+([A-Za-z]{3,9}),?\s+(\d{4})\b", re.I)
YEAR_RE = re.compile(r"\bbefore\s+(\d{4})\b", re.I)


def deadline(m: dict) -> tuple[int | None, str]:
    for txt in (m.get("title") or "", m.get("rules") or "", m.get("sub") or ""):
        x = ISO_RE.search(txt)
        if x:
            return int(dt.datetime.fromisoformat(x.group(1)).replace(tzinfo=dt.timezone.utc).timestamp()), "iso"
        x = DATE_RE.search(txt)
        if x and x.group(2).lower() in MON:
            kind = x.group(1).lower()
            t0 = _d(int(x.group(4)), MON[x.group(2).lower()], int(x.group(3)))
            return (t0 + DAY, kind) if kind in ("by", "on") else (t0, kind)
        x = MONTH_RE.search(txt)
        if x and x.group(2).lower() in MON:
            y, mo = int(x.group(3)), MON[x.group(2).lower()]
            if x.group(1).lower() == "by":
                mo += 1
                if mo == 13:
                    y, mo = y + 1, 1
            return _d(y, mo, 1), "month"
        x = YEAR_RE.search(txt)
        if x:
            return _d(int(x.group(1)), 1, 1), "year"
    sub = (m.get("sub") or "")
    x = re.search(r"Before\s+([A-Za-z]{3,9})\s+(\d{1,2})", sub)
    if x and x.group(1).lower() in MON and m.get("latest_exp"):
        y = dt.datetime.utcfromtimestamp(m["latest_exp"]).year
        return _d(y, MON[x.group(1).lower()], int(x.group(2))), "sub"
    return None, "none"


def universe(markets: dict) -> list[dict]:
    out = []
    for m in markets.values():
        s = m["series"]
        if s in EXCLUDE_SERIES or s not in FAMILY or m.get("result") not in ("yes", "no"):
            continue
        D, src = deadline(m)
        if D is None:
            continue
        out.append({**m, "D": D, "D_src": src, "fam": FAMILY[s]})
    return out


def f(x):
    return None if x is None else float(x)


def rows_of(cs: list[dict]) -> list[list]:
    """Raw candlesticks (live: *_dollars keys; archive: plain keys) -> [[end_ts, yes_ask_close, yes_bid_close, yes_bid_high, vol]]."""
    out = []
    for c in cs:
        a, b = c.get("yes_ask") or {}, c.get("yes_bid") or {}
        ask = a.get("close_dollars", a.get("close"))
        bid = b.get("close_dollars", b.get("close"))
        bh = b.get("high_dollars", b.get("high"))
        out.append([int(c["end_period_ts"]), f(ask), f(bid), f(bh), float(c.get("volume_fp") or c.get("volume") or 0)])
    out.sort()
    return out


def load_candles() -> dict[str, list[list]]:
    p = OUT / "candles.jsonl"
    d: dict[str, list[list]] = {}
    if p.exists():
        for l in p.open():
            x = json.loads(l)
            if len(x["c"]) >= len(d.get(x["t"], [])):
                d[x["t"]] = x["c"]
    return d


H = 3600
HORIZONS = (7, 3)   # decision at D - h days; fill at the first hourly quote in [t + 1 h, t + 2 h]


def tradeable(m: dict, h: int) -> bool:
    t = m["D"] - h * DAY
    return m["open"] < t and m["close"] > t + H


def fetch_set(U: list[dict]) -> dict[str, dict]:
    """Rungs whose candles the rule needs: every rung tradeable at D - h, plus every rung of the same event that is
    open at that decision time with an earlier deadline (it could be the target instead)."""
    by_e: dict[str, list[dict]] = {}
    for m in U:
        by_e.setdefault(m["e"], []).append(m)
    need: dict[str, dict] = {}
    for m in U:
        for h in HORIZONS:
            if not tradeable(m, h):
                continue
            t = m["D"] - h * DAY
            need[m["t"]] = m
            for o in by_e[m["e"]]:
                if o["D"] < m["D"] and o["D"] > t and o["open"] < t and o["close"] > t:
                    need[o["t"]] = o
    return need
