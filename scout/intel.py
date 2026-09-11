"""HOLT + BRAM lite.

WorldMonitor's REST needs a key. Same job, free feeds: independent RSS
(two origins) plus Kalshi same-day sports prices vs Polymarket.
A claim that cannot be sourced on two origins does not ship.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

_UA = "scout-agent/0.2 (worldmonitor fusion; contact ops)"
_RSS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
)
_KALSHI = "https://api.elections.kalshi.com"
_GAME_SERIES = (
    "KXMLBGAME",
    "KXMLBTOTAL",
    "KXMLBSPREAD",
    "KXNBAGAME",
    "KXNBATOTAL",
    "KXNBASPREAD",
    "KXNFLGAME",
    "KXNFLTOTAL",
    "KXNFLSPREAD",
    "KXNHLGAME",
    "KXNHLTOTAL",
    "KXNHLSPREAD",
)
_STOP = {
    "the", "and", "for", "will", "win", "wins", "vs", "over", "under", "total",
    "runs", "points", "spread", "moneyline", "game", "today", "tonight",
}
_TEAM = {
    "yankees": "nyy", "nyy": "nyy", "astros": "hou", "houston": "hou",
    "dodgers": "lad", "lad": "lad", "redsox": "bos", "boston": "bos",
    "mets": "nym", "nym": "nym", "phillies": "phi", "philadelphia": "phi",
    "braves": "atl", "atlanta": "atl", "cubs": "chc", "chicago": "chc",
    "guardians": "cle", "cleveland": "cle", "orioles": "bal", "baltimore": "bal",
    "rays": "tb", "tampa": "tb", "bluejays": "tor", "toronto": "tor",
    "white sox": "cws", "cws": "cws", "tigers": "det", "detroit": "det",
    "twins": "min", "minnesota": "min", "royals": "kc", "kansas": "kc",
    "white": "cws", "sox": "bos", "giants": "sf", "padres": "sd",
    "diamondbacks": "az", "dbacks": "az", "arizona": "az",
    "rockies": "col", "colorado": "col", "rangers": "tex", "texas": "tex",
    "angels": "laa", "athletics": "ath", "mariners": "sea", "seattle": "sea",
    "cardinals": "stl", "stlouis": "stl", "brewers": "mil", "milwaukee": "mil",
    "pirates": "pit", "pittsburgh": "pit", "reds": "cin", "cincinnati": "cin",
    "nationals": "wsh", "washington": "wsh", "marlins": "mia", "miami": "mia",
    "new york y": "nyy", "new york m": "nym", "los angeles d": "lad",
    "los angeles a": "laa", "los angeles c": "lac",
}


def _get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _get_json(url: str, timeout: int = 12) -> Any:
    return json.loads(_get(url, timeout=timeout))


def _tokens(text: str) -> set[str]:
    q = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", q)
    out: set[str] = set()
    for word in words:
        if word in _STOP or len(word) < 3:
            continue
        out.add(_TEAM.get(word, word))
    for phrase, code in _TEAM.items():
        if " " in phrase and phrase in q:
            out.add(code)
    return out


def market_kind(text: str) -> str:
    q = (text or "").lower()
    if "spread" in q or "wins by" in q:
        return "spread"
    if any(tag in q for tag in ("over ", "under ", "total", "o/u", "runs scored")):
        return "total"
    return "ml"


def rss_headlines(limit: int = 16) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for url in _RSS:
        origin = "bbc" if "bbc" in url else "nyt"
        try:
            xml = _get(url, timeout=8)
        except Exception:
            continue
        titles = re.findall(
            r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml, re.I | re.S
        )
        for title in titles[1:limit]:
            clean = re.sub(r"<[^>]+>", "", title).strip()
            key = clean.lower()
            if len(clean) < 18 or key in seen:
                continue
            seen.add(key)
            rows.append({"title": clean, "origin": origin})
    return rows[:40]


def kalshi_same_day(horizon_hours: float = 36.0) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for series in _GAME_SERIES:
        url = (
            f"{_KALSHI}/trade-api/v2/events?limit=8&status=open"
            f"&series_ticker={series}&with_nested_markets=true"
        )
        try:
            payload = _get_json(url, timeout=10)
        except Exception:
            continue
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                close = str(market.get("close_time") or "")
                try:
                    end = datetime.fromisoformat(close.replace("Z", "+00:00"))
                except ValueError:
                    continue
                hours = (end - now).total_seconds() / 3600.0
                if hours < -1 or hours > horizon_hours:
                    continue
                ask = float(market.get("yes_ask_dollars") or 0)
                if not (0.05 <= ask <= 0.95):
                    continue
                title = str(event.get("title") or "") + " " + str(market.get("title") or "")
                out.append(
                    {
                        "title": title.strip(),
                        "event": event.get("title") or "",
                        "yes_ask": ask,
                        "kind": market_kind(title),
                        "series": series,
                        "ticker": market.get("ticker") or "",
                        "hours": round(hours, 2),
                    }
                )
    return out


def dual_source_headlines(headlines: list[dict[str, str]]) -> list[str]:
    """Titles that show up, in substance, on two independent origins."""
    by_origin: dict[str, list[set[str]]] = {}
    for row in headlines:
        by_origin.setdefault(row["origin"], []).append(_tokens(row["title"]))
    origins = list(by_origin)
    if len(origins) < 2:
        return []
    confirmed: list[str] = []
    for row in headlines:
        toks = _tokens(row["title"])
        if len(toks) < 3:
            continue
        other = [o for o in origins if o != row["origin"]]
        hit = False
        for origin in other:
            for other_toks in by_origin[origin]:
                if len(toks & other_toks) >= 3:
                    hit = True
                    break
            if hit:
                break
        if hit:
            confirmed.append(row["title"])
    # unique, keep order
    seen: set[str] = set()
    out: list[str] = []
    for title in confirmed:
        if title in seen:
            continue
        seen.add(title)
        out.append(title)
    return out[:12]


def match_kalshi(poly_question: str, kalshi_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    poly_toks = _tokens(poly_question)
    poly_kind = market_kind(poly_question)
    if len(poly_toks) < 2:
        return None
    best: dict[str, Any] | None = None
    best_n = 1
    for row in kalshi_rows:
        if row["kind"] != poly_kind:
            continue
        n = len(poly_toks & _tokens(row["title"]))
        if n > best_n:
            best_n = n
            best = row
    if best is None or best_n < 2:
        return None
    return best


def kalshi_gap(poly_yes: float, kalshi_yes: float) -> float:
    return round(kalshi_yes - poly_yes, 4)


def load_intel() -> dict[str, Any]:
    headlines = rss_headlines()
    confirmed = dual_source_headlines(headlines)
    kalshi = kalshi_same_day()
    return {
        "ts": time.time(),
        "headlines": headlines,
        "confirmed": confirmed,
        "kalshi": kalshi,
    }

_INTEL: dict[str, Any] = {"ts": 0.0, "data": None}


def cached_intel(ttl: float = 120.0) -> dict[str, Any]:
    now = time.time()
    if _INTEL["data"] is not None and now - float(_INTEL["ts"] or 0) < ttl:
        return _INTEL["data"]
    data = load_intel()
    _INTEL["ts"] = now
    _INTEL["data"] = data
    return data


def intel_brief(intel: dict[str, Any]) -> str:
    lines = ["Independent RSS (need two origins):"]
    for title in intel.get("confirmed") or []:
        lines.append(f"- {title}")
    if len(lines) == 1:
        lines.append("- none corroborated")
    lines.append("Kalshi same-day sports (venue B price):")
    for row in (intel.get("kalshi") or [])[:12]:
        lines.append(
            f"- {row['title'][:80]} kalshi_yes={row['yes_ask']:.2f} {row['hours']}h"
        )
    if len(intel.get("kalshi") or []) == 0:
        lines.append("- none")
    return "\n".join(lines)


def attach_intel(markets: list[dict[str, Any]], intel: dict[str, Any]) -> None:
    kalshi_rows = intel.get("kalshi") or []
    confirmed = intel.get("confirmed") or []
    for market in markets:
        q = str(market.get("question") or "")
        hit = match_kalshi(q, kalshi_rows)
        if hit:
            gap = kalshi_gap(float(market.get("yes_ask") or 0), float(hit["yes_ask"]))
            market["kalshi_yes"] = hit["yes_ask"]
            market["kalshi_gap"] = gap
            market["kalshi_title"] = hit["title"]
        news_hits = [title for title in confirmed if len(_tokens(q) & _tokens(title)) >= 2]
        if news_hits:
            market["intel_news"] = news_hits[:2]


def kalshi_prior(market: dict[str, Any]) -> dict[str, Any] | None:
    """BRAM: treat Kalshi ask as a second-venue fair if the gap is real."""
    gap = float(market.get("kalshi_gap") or 0)
    kalshi_yes = float(market.get("kalshi_yes") or 0)
    if abs(gap) < 0.07 or not (0.05 < kalshi_yes < 0.95):
        return None
    return {
        "p_yes": kalshi_yes,
        "confidence": 0.72,
        "already_priced": False,
        "edge_type": "related_inconsistency",
        "thesis": (
            f"Kalshi {market.get('kalshi_title', '')[:60]} yes={kalshi_yes:.2f} "
            f"vs Poly {float(market.get('yes_ask') or 0):.2f} gap={gap:+.3f}"
        ),
        "sources": [
            "https://kalshi.com",
            f"kalshi:{market.get('kalshi_title', '')[:80]}",
        ],
    }
