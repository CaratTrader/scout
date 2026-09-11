from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

_UA = "scout-agent/0.2"


def recent_aggression(token_id: str, *, since_s: float = 5.0, timeout: int = 4) -> float:
    """USD lifted on this token in the last few seconds (Fadi smart-money confirm)."""
    if not token_id:
        return 0.0
    url = f"https://data-api.polymarket.com/trades?asset_id={token_id}&limit=30"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return 0.0
    if not isinstance(rows, list):
        return 0.0
    cutoff = time.time() - since_s
    bought = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = row.get("timestamp") or row.get("match_time") or row.get("createdAt") or 0
        try:
            stamp = float(ts)
        except (TypeError, ValueError):
            continue
        if stamp > 1e12:
            stamp /= 1000.0
        if stamp < cutoff:
            continue
        side = str(row.get("side") or "").upper()
        if side not in {"BUY", ""}:
            continue
        size = float(row.get("size") or 0)
        price = float(row.get("price") or 0)
        bought += size * price if price > 0 else size
    return round(bought, 2)


def leaderboard_wallets(limit: int = 10, timeout: int = 6) -> list[dict[str, Any]]:
    """DAY PnL leaderboard with Fadi-style quality filters. Best-effort."""
    url = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=DAY&orderBy=PNL&limit=25"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("leaderboard") or []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pnl = float(row.get("pnl") or row.get("pnl_day") or 0)
        wr = float(row.get("winRate") or row.get("win_rate") or 0)
        if wr > 1:
            wr /= 100.0
        vol = float(row.get("vol") or row.get("volume") or 0)
        if pnl < 500 or wr < 0.55:
            continue
        out.append(
            {
                "proxy": row.get("proxyWallet") or row.get("user") or row.get("address") or "",
                "pnl": pnl,
                "win_rate": wr,
                "volume": vol,
            }
        )
        if len(out) >= limit:
            break
    return out
