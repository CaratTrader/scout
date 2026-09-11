from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any, Iterable


def _strategy(reason: Any) -> str:
    raw = str(reason or "unknown").strip()
    return raw.split(" ", 1)[0] or "unknown"


def summarize_performance(
    ledger: dict[str, Any],
    journal_events: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Auditable realized results plus forward calibration from enriched events."""
    opens: dict[str, list[dict[str, Any]]] = defaultdict(list)
    closed: list[dict[str, Any]] = []
    resolved_by_id: dict[str, float] = {}
    for fill in ledger.get("fills") or []:
        market_id = str(fill.get("market_id") or "")
        side = str(fill.get("side") or "")
        if side.startswith("CLOSE_"):
            opened = opens[market_id].pop(0) if opens[market_id] else {}
            row = {
                "strategy": _strategy(opened.get("reason")),
                "pnl": float(fill.get("pnl") or 0),
                "cost_basis": float(opened.get("cost_basis") or opened.get("stake") or 0),
                "entry_fee": float(opened.get("fee") or fill.get("entry_fee") or 0),
            }
            closed.append(row)
            close_px = float(fill.get("price") or 0)
            if close_px in {0.0, 1.0}:
                resolved_by_id[market_id] = close_px
        else:
            opens[market_id].append(fill)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in closed:
        grouped[row["strategy"]].append(row)
    by_strategy = []
    for name, rows in sorted(grouped.items()):
        cost = sum(row["cost_basis"] for row in rows)
        pnl = sum(row["pnl"] for row in rows)
        by_strategy.append(
            {
                "strategy": name,
                "trades": len(rows),
                "wins": sum(row["pnl"] > 0 for row in rows),
                "pnl": round(pnl, 4),
                "fees": round(sum(row["entry_fee"] for row in rows), 4),
                "roi": round(pnl / cost, 4) if cost else None,
            }
        )

    latencies: list[float] = []
    brier_rows: list[tuple[float, float]] = []
    rejects: Counter[str] = Counter()
    for event in journal_events:
        kind = event.get("event")
        if kind == "execution_reject":
            rejects[str(event.get("reason") or "unknown").split(":", 1)[0]] += 1
        if kind != "submit":
            continue
        if event.get("signal_age_ms") is not None:
            latencies.append(float(event["signal_age_ms"]))
        market_id = str(event.get("id") or "")
        if market_id not in resolved_by_id or event.get("fair") is None:
            continue
        fair = float(event["fair"])
        if 0 <= fair <= 1:
            brier_rows.append((fair, resolved_by_id[market_id]))

    latencies.sort()
    p95_index = max(0, int(len(latencies) * 0.95 + 0.999999) - 1)
    total_pnl = sum(row["pnl"] for row in closed)
    total_cost = sum(row["cost_basis"] for row in closed)
    return {
        "closed_trades": len(closed),
        "wins": sum(row["pnl"] > 0 for row in closed),
        "losses": sum(row["pnl"] < 0 for row in closed),
        "realized_pnl": round(total_pnl, 4),
        "realized_roi": round(total_pnl / total_cost, 4) if total_cost else None,
        "recorded_entry_fees": round(sum(row["entry_fee"] for row in closed), 4),
        "by_strategy": by_strategy,
        "calibration": {
            "resolved_predictions": len(brier_rows),
            "brier": (
                round(mean((forecast - outcome) ** 2 for forecast, outcome in brier_rows), 6)
                if brier_rows
                else None
            ),
        },
        "execution": {
            "timed_submits": len(latencies),
            "median_signal_age_ms": latencies[len(latencies) // 2] if latencies else None,
            "p95_signal_age_ms": latencies[p95_index] if latencies else None,
            "max_signal_age_ms": max(latencies) if latencies else None,
            "rejects": dict(rejects),
        },
    }
