"""Learning database: the bot's own closed trades, classified and fed back as vetoes.

Every closed fill becomes a (pattern -> outcome) observation. Patterns with enough
samples and a proven negative expectancy are refused on the next entry. A prior
seeded from the 30-day backtest replay covers patterns live trading has not
sampled yet; live observations override the prior once they reach MIN_LIVE.

Deterministic and data-driven — no model calls. Disable with LESSONS=0.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LEARN_DIR = ROOT / "learning"
LESSONS_PATH = LEARN_DIR / "lessons.json"
POSTMORTEMS_PATH = LEARN_DIR / "postmortems.jsonl"

PRICE_BANDS = ((0.05, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.92))
MIN_LIVE = 6          # live samples needed before a live stat outranks the prior
VETO_RET = -0.10      # mean return per $1 staked below this = refuse the pattern

# Prior from backtest/results.json (30d replay, half_spread=0.01, n=7949).
# Regenerate with learning/postmortem.py --rebuild-prior after a new backtest.
_BACKTEST_PRIOR = {
    "ask_0.05-0.15": {"n": 326, "win_rate": 0.077, "ret_per_$": -0.357},
    "ask_0.15-0.25": {"n": 551, "win_rate": 0.223, "ret_per_$": 0.045},
    "ask_0.25-0.40": {"n": 2042, "win_rate": 0.356, "ret_per_$": 0.014},
    "ask_0.40-0.60": {"n": 3961, "win_rate": 0.652, "ret_per_$": 0.281},
    "ask_0.60-0.80": {"n": 821, "win_rate": 0.866, "ret_per_$": 0.262},
    "ask_0.80-0.92": {"n": 248, "win_rate": 0.964, "ret_per_$": 0.116},
}


def price_band(price: float) -> str:
    for lo, hi in PRICE_BANDS:
        if lo <= price < hi:
            return f"ask_{lo:.2f}-{hi:.2f}"
    return "ask_out_of_band"


def classify(entry_price: float, won: bool, edge: float | None) -> str:
    """Failure/success mode for the postmortem record."""
    if won:
        return "as_modeled"
    if entry_price < 0.25:
        return "cheap_longshot_vs_momentum"   # model tail overconfident; book was right
    if entry_price < 0.60:
        return "coin_flip_variance"           # near-even entry lost on noise
    return "favorite_upset"                   # high-prob side failed anyway


def paired_trades(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    entries: dict[str, dict] = {}
    out: list[dict[str, Any]] = []
    for f in ledger.get("fills") or []:
        side = str(f.get("side") or "")
        if side.startswith("CLOSE_"):
            e = entries.get(str(f.get("market_id")))
            if not e:
                continue
            price = float(e.get("price") or 0)
            stake = float(e.get("stake") or 0)
            pnl = float(f.get("pnl") or 0)
            edge = None
            reason = str(e.get("reason") or "")
            if "edge=" in reason:
                try:
                    edge = float(reason.split("edge=")[1].split()[0])
                except ValueError:
                    edge = None
            out.append({
                "ts": f.get("ts"),
                "market_id": f.get("market_id"),
                "question": f.get("question"),
                "side": side.replace("CLOSE_", ""),
                "entry_price": price,
                "stake": stake,
                "fee": float(e.get("fee") or 0),
                "edge_claimed": edge,
                "pnl": pnl,
                "won": pnl > 0,
                "ret_per_$": round(pnl / stake, 4) if stake else 0.0,
                "pattern": price_band(price),
                "mode": classify(price, pnl > 0, edge),
            })
        elif side in {"YES", "NO", "BOTH"}:
            entries[str(f.get("market_id"))] = f
    return out


def build_stats(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for t in trades:
        s = stats.setdefault(t["pattern"], {"n": 0, "wins": 0, "ret_sum": 0.0})
        s["n"] += 1
        s["wins"] += 1 if t["won"] else 0
        s["ret_sum"] += t["ret_per_$"]
    return {
        k: {
            "n": s["n"],
            "win_rate": round(s["wins"] / s["n"], 4),
            "ret_per_$": round(s["ret_sum"] / s["n"], 4),
        }
        for k, s in stats.items()
    }


def refresh(ledger: dict[str, Any]) -> dict[str, Any]:
    """Rebuild lessons.json + postmortems.jsonl from the ledger. Idempotent."""
    LEARN_DIR.mkdir(exist_ok=True)
    trades = paired_trades(ledger)
    live = build_stats(trades)
    doc = {
        "updated_at": int(time.time()),
        "live": live,
        "backtest_prior": _BACKTEST_PRIOR,
        "rules": {"min_live": MIN_LIVE, "veto_ret_per_$": VETO_RET},
    }
    LESSONS_PATH.write_text(json.dumps(doc, indent=1))
    POSTMORTEMS_PATH.write_text(
        "".join(json.dumps(t) + "\n" for t in trades), encoding="utf-8"
    )
    return doc


def _live_overrides_prior(live: dict[str, Any], prior: dict[str, Any] | None) -> bool:
    """A handful of live trades must not overturn thousands of prior samples on
    noise alone. Live wins when its record is statistically incompatible with
    the prior's win rate (one-sided binomial, p < 0.05) or simply large."""
    if not prior:
        return True
    n = int(live["n"])
    if n >= 30:
        return True
    wins = round(live["win_rate"] * n)
    p = float(prior["win_rate"])
    # P(X <= wins) under Binomial(n, p)
    from math import comb

    tail = sum(comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(wins + 1))
    return tail < 0.05


_SOURCE_CACHE: dict[str, Any] = {"path": None, "mtime": None, "ledger": None}


def source_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """LESSONS_SOURCE=<ledger.json> makes a bot learn from another ledger's record —
    the live bot from the paper bot's 280+ trades instead of its own handful, so it
    inherits the vetoes paper already paid for (2026-09-09: live lost twice in the
    0.40-0.60 band that paper had learned to refuse)."""
    src = os.getenv("LESSONS_SOURCE")
    if not src:
        return ledger
    path = Path(src)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return ledger
    if _SOURCE_CACHE["path"] != src or _SOURCE_CACHE["mtime"] != mtime:
        try:
            _SOURCE_CACHE.update({"path": src, "mtime": mtime, "ledger": json.loads(path.read_text())})
        except Exception:
            return _SOURCE_CACHE["ledger"] or ledger
    return _SOURCE_CACHE["ledger"] or ledger


def lesson_veto(candidate: dict[str, Any], ledger: dict[str, Any]) -> str | None:
    """Refuse entries whose pattern has a proven negative record."""
    if (os.getenv("LESSONS") or "1").strip().lower() in {"0", "false", "off"}:
        return None
    if candidate.get("kind") not in {"crypto_lag", "dip_arb"}:
        return None
    price = float(candidate.get("price") or 0)
    pattern = price_band(price)
    doc = refresh(source_ledger(ledger))
    live = doc["live"].get(pattern)
    prior = _BACKTEST_PRIOR.get(pattern)
    if live and live["n"] >= MIN_LIVE and _live_overrides_prior(live, prior):
        src, stat = "live", live
    else:
        src, stat = "prior", prior or {}
    if stat and stat.get("ret_per_$", 0) < VETO_RET:
        return f"lesson:{pattern}:{src}(n={stat['n']},ret={stat['ret_per_$']:+.2f}/$)"
    return None
