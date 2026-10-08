"""weather - mirror of the Kalshi temperature paper bot (data/ledger_kalshi_temp.json, R0 + R2m): its fills opened since
START, re-sized to this bankroll's stake rule (no minimum stake, at most the bot's contracts), settled when the bot settles
them. No Kalshi calls."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .base import Strategy, fetcher

SRC = Path("data/ledger_kalshi_temp.json")
START = "2026-10-07"   # lab start: the mirror counts fills opened from this day


@fetcher("kalshi_temp_ledger", ttl=0)
def kalshi_temp_ledger(path: str) -> dict[str, Any] | None:
    """The temperature bot's paper ledger (a local file; None while it does not exist)."""
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


class WeatherMirror(Strategy):
    min_stake = 0.0
    settle_on_kalshi = False
    one_signal_per_market = False   # each bot fill is mirrored once already (ledger "mirrored")

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self._done: dict[str, dict] = {}

    def external_now(self, now: dt.datetime) -> dict[str, dict[str, Any]]:
        return {"kalshi_temp_ledger": {"path": str(SRC)}}

    def decide(self, now: dt.datetime, market_quotes: dict[str, dict[str, dict] | None], external_data: dict[str, Any]) -> list[dict[str, Any]]:
        bot = external_data.get("kalshi_temp_ledger"); self._done = {}
        if bot is None:
            self.notes.append("no source ledger"); return []
        led = self.ledger; out = []
        for f in bot.get("positions", []) + bot.get("fills", []):
            k = f"{f['ticker']}|{f['side']}"
            if f.get("opened", "")[:10] < START or k in led["mirrored"]:
                continue
            led["mirrored"].append(k)
            out.append({"ticker": f["ticker"], "side": f["side"], "max_price": f["px"], "why": f.get("why", ""), "size": f["shares"],
                        "opened": f["opened"], "close": f.get("end_date"), "log": {"rule": f.get("rule")}})
        self._done = {f"{f['ticker']}|{f['side']}": f for f in bot.get("fills", []) if "won" in f}
        return out

    def result(self, pos: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
        f = self._done.get(f"{pos['ticker']}|{pos['side']}")
        return {"won": f["won"], "settled": f.get("settled") or now.isoformat()} if f else None

    def summary(self) -> str:
        led = self.ledger
        return f"{self.name}: cash ${led['cash']:.2f} open {len(led['positions'])} settled {len(led['fills'])}" + "".join(f"; {n}" for n in self.notes)


STRATEGY = WeatherMirror
