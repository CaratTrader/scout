"""Cross-venue study helpers: Polymarket US (thin, US-legal) vs Polymarket global (deep) for the same game.

US slug = <prefix>-<global event slug>[-<outcome>], prefixes seen: aec (moneyline, 2-outcome), atc (single
team "Will X win", YES/NO), asc (spread), tsc (total), astatc (player prop), tec/pec (futures).
US price history: longPrice = YES best ask, shortPrice = NO best ask (= 1 - YES best bid), 1-minute points via
INTERVAL_LIVE (15 min before start -> end) or custom <=24h ranges; global: CLOB prices-history 1-min (last trade).
"""
from __future__ import annotations

import json
import math
import statistics as st
import time
import urllib.request
from typing import Any

US = "https://gateway.polymarket.us/v1"
G = "https://gamma-api.polymarket.com"
C = "https://clob.polymarket.com"
UA = {"User-Agent": "scout-research"}


def get(url: str, tries: int = 3) -> Any:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(1 + i)
        except Exception:
            time.sleep(1 + i)
    return None


def us_series(slug: str, game_start: float, pre_hours: float = 6.0) -> dict[int, tuple[float, float, float]]:
    """minute -> (mid, yes_ask, yes_bid) from the US venue, pre-game window + in-game."""
    out: dict[int, tuple[float, float, float]] = {}
    live = (get(f"{US}/price-history?symbol={slug}&fixedInterval=INTERVAL_LIVE&fidelity=1") or {}).get("history", [])
    pre = (get(f"{US}/price-history?symbol={slug}&timestamp.startTimestamp={int(game_start - pre_hours * 3600)}&timestamp.endTimestamp={int(game_start)}&fidelity=1") or {}).get("history", [])
    for p in pre + live:
        ask = float(p["longPrice"]); bid = 1.0 - float(p["shortPrice"])
        if not (0 < ask <= 1) or not (0 <= bid < 1):
            continue
        out[int(p["timestamp"]) // 60 * 60] = ((ask + bid) / 2, ask, bid)
    return out


def global_match(us_market: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Return (global market, index of the global outcome that is the US YES side)."""
    parts = us_market["slug"].split("-")
    us_outs = json.loads(us_market.get("outcomes") or "[]")
    for cand in ("-".join(parts[1:]), "-".join(parts[1:-1])):
        ev = get(f"{G}/events?slug={cand}")
        if not ev:
            continue
        mkts = ev[0].get("markets") or []
        typ = us_market.get("sportsMarketTypeV2") or ""
        want = {"SPORTS_MARKET_TYPE_MONEYLINE": "moneyline", "SPORTS_MARKET_TYPE_SPREAD": "spreads", "SPORTS_MARKET_TYPE_TOTAL": "totals"}.get(typ)
        pool = [m for m in mkts if (m.get("sportsMarketType") == want)] if want else mkts
        # single-team US markets ("Will X win"): the global market whose slug ends with the same token
        if parts[0] in ("atc",) and len(parts) > 2:
            tail = parts[-1]
            pool2 = [m for m in mkts if (m.get("slug") or "").endswith("-" + tail)]
            pool = pool2 or pool
        if not pool:
            continue
        gm = pool[0]
        g_outs = json.loads(gm.get("outcomes") or "[]")
        idx = 0
        if us_outs and len(us_outs) == 2 and us_outs[0].lower() not in ("yes", "no"):
            for i, o in enumerate(g_outs):
                if o.strip().lower() == us_outs[0].strip().lower():
                    idx = i
        return gm, idx
    return None, 0


def global_series(gm: dict[str, Any], idx: int, start: float, end: float) -> dict[int, float]:
    toks = json.loads(gm["clobTokenIds"])
    h = (get(f"{C}/prices-history?market={toks[idx]}&startTs={int(start)}&endTs={int(end)}&fidelity=1") or {}).get("history", [])
    return {int(p["t"]) // 60 * 60: float(p["p"]) for p in h}


def corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b)); a = a[:n]; b = b[:n]
    if n < 5:
        return float("nan")
    ma = sum(a) / n; mb = sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a)); sb = math.sqrt(sum((x - mb) ** 2 for x in b))
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (sa * sb) if sa and sb else float("nan")


def lead_lag(us: dict[int, tuple[float, float, float]], gl: dict[int, float], t0: float | None = None, t1: float | None = None, lags=(0, 1, 2, 3, 5)) -> dict[str, Any]:
    ts = sorted(t for t in set(us) & set(gl) if (t0 is None or t >= t0) and (t1 is None or t < t1))
    if len(ts) < 10:
        return {"n": len(ts)}
    diffs = [us[t][0] - gl[t] for t in ts]; spr = [us[t][1] - us[t][2] for t in ts]
    gc = [gl[ts[i + 1]] - gl[ts[i]] for i in range(len(ts) - 1)]
    uc = [us[ts[i + 1]][0] - us[ts[i]][0] for i in range(len(ts) - 1)]
    out = {"n": len(ts), "diff_mean": st.mean(diffs), "diff_sd": st.pstdev(diffs), "gt3c": sum(1 for d in diffs if abs(d) > 0.03) / len(diffs),
           "gt5c": sum(1 for d in diffs if abs(d) > 0.05) / len(diffs), "spread_med": st.median(spr)}
    for k in lags:
        out[f"g_leads_{k}"] = corr(gc[: len(gc) - k] if k else gc, uc[k:])
        out[f"us_leads_{k}"] = corr(uc[: len(uc) - k] if k else uc, gc[k:])
    return out


# ---------------------------------------------------------------- robust matcher (slug first, then league+date+outcomes)
_TAGS = {"mlb": ["mlb"], "nfl": ["nfl"], "nba": ["nba"], "nhl": ["nhl"], "cfb": ["cfb", "college-football", "ncaaf"], "cbb": ["cbb", "college-basketball", "ncaab"],
         "atp": ["atp", "tennis"], "wta": ["wta", "tennis"], "mls": ["mls"], "ufc": ["ufc", "mma"], "cs2": ["cs2", "counter-strike"], "wnba": ["wnba"]}
_EVENT_CACHE: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch == " ").strip()


def _global_events(league: str, day: str) -> list[dict[str, Any]]:
    key = (league, day)
    if key in _EVENT_CACHE:
        return _EVENT_CACHE[key]
    d0 = day
    import datetime as _dt
    d1 = (_dt.date.fromisoformat(day) + _dt.timedelta(days=2)).isoformat()
    dm = (_dt.date.fromisoformat(day) - _dt.timedelta(days=1)).isoformat()
    out: list[dict[str, Any]] = []
    for tag in _TAGS.get(league, [league]):
        for closed in ("true", "false"):
            ev = get(f"{G}/events?tag_slug={tag}&closed={closed}&start_date_min={dm}T00:00:00Z&start_date_max={d1}T00:00:00Z&limit=500") or []
            out += ev
        if out:
            break
    _EVENT_CACHE[key] = out
    return out


def global_match2(us_market: dict[str, Any]) -> tuple[dict[str, Any] | None, int, str]:
    gm, idx = global_match(us_market)
    if gm:
        return gm, idx, "slug"
    parts = us_market["slug"].split("-")
    league = parts[1] if len(parts) > 2 else ""
    us_outs = [_norm(o) for o in json.loads(us_market.get("outcomes") or "[]")]
    gs = (us_market.get("gameStartTime") or "")[:10]
    if not gs or len(us_outs) != 2:
        return None, 0, "no-key"
    for e in _global_events(league, gs):
        for m in e.get("markets") or []:
            if m.get("sportsMarketType") not in ("moneyline", None):
                continue
            g_outs = [_norm(o) for o in json.loads(m.get("outcomes") or "[]")]
            if len(g_outs) != 2:
                continue
            # full-name equality, or nickname containment (US uses nicknames for college teams)
            def same(a: str, b: str) -> bool:
                return a == b or (len(a) > 3 and (a in b or b in a)) or a.split()[-1] == b.split()[-1]
            if same(us_outs[0], g_outs[0]) and same(us_outs[1], g_outs[1]):
                return m, 0, "outcomes"
            if same(us_outs[0], g_outs[1]) and same(us_outs[1], g_outs[0]):
                return m, 1, "outcomes-swapped"
    return None, 0, "unmatched"
