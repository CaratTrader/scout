from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import GROK_API_LOCK, GROK_CACHE_PATH
from .risk import looks_round_prob


def grok_eligible(market: dict[str, Any]) -> bool:
    ask = float(market["yes_ask"])
    days = market.get("days_to_end")
    if days is not None and days <= 0:
        return False
    return 0.04 <= ask <= 0.96


_SLOW_SPORTS = (
    "us open",
    "tour championship",
    "masters",
    "wimbledon",
    "world cup",
    "super bowl",
    "nba finals",
    "stanley cup",
)


def is_same_day_sport(market: dict[str, Any]) -> bool:
    """Tonight's game, not a season future. Lineup cards move these books."""
    days = market.get("days_to_end")
    if days is None or float(days) > 1.5:
        return False
    q = (market.get("question") or "").lower()
    if any(tag in q for tag in _SLOW_SPORTS):
        return False
    sport = any(
        tag in q
        for tag in (
            "mlb",
            "nba",
            "nfl",
            "nhl",
            "ufc",
            "mls",
            " vs ",
            "vs.",
            "over ",
            "under ",
            "o/u",
            "spread",
            "moneyline",
            "total runs",
            "total points",
            "total goals",
        )
    )
    return sport


def survival_mode(ledger: dict[str, Any] | None) -> bool:
    if not ledger:
        return False
    return float(ledger.get("cash") or 0) < 15.0

def grok_research_eligible(market: dict[str, Any], *, survival: bool = False) -> bool:
    """Same-day books Grok can check on X before the CLOB reprices. Not 45-day politics."""
    ask = float(market["yes_ask"])
    days = market.get("days_to_end")
    if days is None or days <= 0:
        return False
    q = (market.get("question") or "").lower()
    if any(tag in q for tag in _SLOW_SPORTS):
        return False
    if any(tag in q for tag in ("divorce", "retire", "play for")):
        return False
    if is_same_day_sport(market):
        return 0.08 <= ask <= 0.88
    if survival:
        return False
    return days <= 2.0 and 0.08 <= ask <= 0.75


def grok_priority(market: dict[str, Any]) -> tuple[float, float, float, float]:
    """Same-day sports first, then soonest expiry."""
    ask = float(market["yes_ask"])
    days = float(market.get("days_to_end") or 99)
    sport = 0.0 if is_same_day_sport(market) else 1.0
    return (sport, days, abs(ask - 0.45), -float(market.get("volume_24h") or 0))


def score_with_grok(
    markets: list[dict[str, Any]],
    model: str,
    base_url: str,
    *,
    cache_path: Path = GROK_CACHE_PATH,
    ttl_seconds: int = 600,
    intel_brief: str = "",
) -> dict[str, dict[str, Any]]:
    if not markets:
        return {}
    cache = _load_cache(cache_path)
    now = time.time()
    out: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    for market in markets:
        cached = cache.get(market["id"])
        cached_ask = float((cached or {}).get("market_ask", -1))
        price_fresh = abs(cached_ask - float(market["yes_ask"])) <= 0.02
        if cached and now - cached.get("ts", 0) < ttl_seconds and price_fresh:
            row = dict(cached)
            row.pop("ts", None)
            row.pop("market_ask", None)
            out[market["id"]] = row
        else:
            missing.append(market)
    if missing:
        fresh = _ask_grok(missing, model, base_url, intel_brief=intel_brief)
        for market_id, row in fresh.items():
            source = next((m for m in missing if m["id"] == market_id), None)
            cache[market_id] = {
                **row,
                "ts": now,
                "market_ask": float(source["yes_ask"]) if source else -1,
            }
            out[market_id] = row
        _save_cache(cache, cache_path)
    return out


def _ask_grok(
    markets: list[dict[str, Any]],
    model: str,
    base_url: str,
    *,
    intel_brief: str = "",
) -> dict[str, dict[str, Any]]:
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return {}
    gap = int(os.getenv("GROK_EVERY_SECONDS") or "1800")
    now = time.time()
    last = 0.0
    if GROK_API_LOCK.exists():
        try:
            last = float(json.loads(GROK_API_LOCK.read_text()).get("ts") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            last = 0.0
    if now - last < gap:
        print(f"grok api skipped ({int(gap - (now - last))}s cooldown — xAI not called)")
        return {}
    GROK_API_LOCK.parent.mkdir(parents=True, exist_ok=True)
    GROK_API_LOCK.write_text(json.dumps({"ts": now}) + "\n")
    try:
        from openai import OpenAI
    except ImportError:
        return {}

    payload = [
        {
            "id": m["id"],
            "question": m["question"],
            "yes_ask": m["yes_ask"],
            "no_ask": m["no_ask"],
            "end_date": m["end_date"],
            "days_to_end": m.get("days_to_end"),
            "volume_24h": round(m["volume_24h"], 2),
            "url": m.get("url", ""),
        }
        for m in markets
    ]
    prompt = (
        "You are Grok scoring Polymarket binaries that resolve in under 24 hours.\n"
        "Use x_search first, then web_search. You do not size or place orders.\n"
        "Skip every BTC/ETH/SOL 5m and 15m Up/Down. Those are a separate tape.\n"
        "The edge is same-day sports: MLB/NBA/NFL/NHL totals, moneylines, spreads. "
        "Front-run lineup cards, starting pitchers, scratches, weather, and injury posts "
        "from team/beat accounts on X before the book reprices. That is stale_news.\n"
        "Also flag (cheap_convex) 8-25c that a primary source already made true, and "
        "(related_inconsistency) two listed markets that cannot all be true.\n"
        "Skip tournament winners, season futures, 2028 dust, celebrity gossip, and "
        "any politics that cannot resolve today.\n"
        "Only flag a gap if |p_yes - yes_ask| >= 0.07 after a primary source. "
        "already_priced=true if the book already moved.\n"
        "Confidence is evidence quality, not enthusiasm. Do not hedge to 0.5.\n"
        "edge_type: cheap_convex | stale_news | related_inconsistency | none\n"
        "Need TWO independent sources on every flag. If a claim cannot be sourced, edge_type=none.\n"
        "Score EVERY market. Return ONLY JSON array:\n"
        '[{"id":"...","p_yes":0.0,"confidence":0.0,"already_priced":false,'
        '"edge_type":"none","thesis":"why this prints","sources":["url"]}]\n'
        f"Live intel (RSS dual-source + Kalshi venue B):\n{intel_brief or 'none'}\n"
        f"Markets:\n{json.dumps(payload)}"
    )
    client = OpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = client.responses.create(
            model=model,
            input=[{"role": "user", "content": prompt}],
            tools=[{"type": "web_search"}, {"type": "x_search"}],
        )
    except Exception as exc:
        print("grok request failed:", type(exc).__name__, str(exc)[:240])
        return {}
    text = getattr(resp, "output_text", None) or ""
    parsed = _parse_scores(text)
    if not parsed:
        print("grok returned no parseable scores, chars=", len(text))
    return parsed


def _parse_scores(text: str) -> dict[str, dict[str, Any]]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return {}
    try:
        rows = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            continue
        try:
            p = float(row.get("p_yes"))
            conf = float(row.get("confidence") or 0)
        except (TypeError, ValueError):
            continue
        if not 0.0 < p < 1.0:
            continue
        sources = row.get("sources") or []
        if not isinstance(sources, list):
            sources = [str(sources)]
        edge_type = str(row.get("edge_type") or "none").strip().lower()
        if edge_type not in {
            "stale_news",
            "related_inconsistency",
            "long_tail",
            "cheap_convex",
            "endgame",
            "crypto_lag",
            "none",
        }:
            edge_type = "none"
        already = row.get("already_priced")
        already_priced = already is True or str(already).lower() in {"true", "1", "yes"}
        if looks_round_prob(p):
            conf = round(conf * 0.8, 4)
        out[str(row["id"])] = {
            "p_yes": p,
            "confidence": conf,
            "already_priced": already_priced,
            "edge_type": edge_type,
            "thesis": str(row.get("thesis") or ""),
            "sources": [str(s) for s in sources][:5],
        }
    return out


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_cache(cache: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2) + "\n")
