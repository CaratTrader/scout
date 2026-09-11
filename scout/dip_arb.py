from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .config import Settings
from .crypto_lag import is_short_updown_window, parse_window
from .math_risk import taker_fee_per_share


def pair_net_cost(yes_ask: float, no_ask: float, fee_rate: float) -> float:
    """USD to buy one complete set as a taker, including Polymarket fees."""
    return (
        yes_ask
        + no_ask
        + taker_fee_per_share(yes_ask, fee_rate)
        + taker_fee_per_share(no_ask, fee_rate)
    )


def pair_net_edge(yes_ask: float, no_ask: float, fee_rate: float) -> float:
    if yes_ask <= 0 or no_ask <= 0:
        return 0.0
    return round(1.0 - pair_net_cost(yes_ask, no_ask, fee_rate), 6)


def size_complete_set(cash: float, yes_ask: float, no_ask: float, settings: Settings) -> tuple[float, float]:
    """Matched shares and total cash outlay (asks + fees)."""
    net = pair_net_cost(yes_ask, no_ask, settings.taker_fee_rate)
    if net <= 0 or cash <= 0:
        return 0.0, 0.0
    cap = min(
        cash * settings.max_fraction,
        settings.max_crypto_stake,
        cash - 0.25,
        cash * 0.55,
    )
    shares = cap / net
    if shares * min(yes_ask, no_ask) < settings.min_trade:
        shares = settings.min_trade / max(min(yes_ask, no_ask), 1e-6)
    outlay = round(shares * net, 4)
    if outlay > cash or shares <= 0:
        return 0.0, 0.0
    return round(shares, 4), outlay


class QuoteTape:
    """In-memory 3s CLOB tape. DipArb is a tape strategy; Gamma snapshots are too slow."""

    def __init__(self, maxlen: int = 80) -> None:
        self._buf: dict[str, deque[tuple[float, float, float, float, float]]] = defaultdict(
            lambda: deque(maxlen=maxlen)
        )

    def observe(self, market: dict[str, Any], now: float) -> None:
        if market.get("clob_fresh") is False:
            return
        yes = float(market.get("yes_ask") or 0)
        no = float(market.get("no_ask") or 0)
        if not (0 < yes < 1 and 0 < no < 1):
            return
        self._buf[str(market["id"])].append(
            (
                now,
                yes,
                no,
                float(market.get("yes_ask_size") or 0),
                float(market.get("no_ask_size") or 0),
            )
        )

    def quote_ago(self, market_id: str, now: float, window_s: float) -> tuple[float, float] | None:
        rows = self._buf.get(str(market_id))
        if not rows:
            return None
        target = now - window_s
        chosen: tuple[float, float, float, float, float] | None = None
        for row in rows:
            if row[0] <= target:
                chosen = row
            else:
                break
        if chosen is None:
            return None
        if now - chosen[0] < window_s * 0.6:
            return None
        return chosen[1], chosen[2]

    def peek(self, market: dict[str, Any], now: float, window_s: float) -> tuple[float, float]:
        yes = float(market.get("yes_ask") or 0)
        no = float(market.get("no_ask") or 0)
        drop = 0.0
        ago = self.quote_ago(str(market.get("id") or ""), now, window_s)
        if ago:
            if ago[0] > 0:
                drop = max(drop, (ago[0] - yes) / ago[0])
            if ago[1] > 0:
                drop = max(drop, (ago[1] - no) / ago[1])
        return drop, yes + no

    def signal(self, market: dict[str, Any], now: float, settings: Settings) -> dict[str, Any] | None:
        win = parse_window(market)
        if not is_short_updown_window(win) or win is None:
            return None
        if now < win["start"] - 2 or win["end"] - now < 20:
            return None
        yes = float(market.get("yes_ask") or 0)
        no = float(market.get("no_ask") or 0)
        yes_sz = float(market.get("yes_ask_size") or 0)
        no_sz = float(market.get("no_ask_size") or 0)
        min_sz = settings.min_book_shares
        if yes_sz < min_sz or no_sz < min_sz:
            return None
        net_edge = pair_net_edge(yes, no, settings.taker_fee_rate)
        raw_sum = yes + no
        if raw_sum >= settings.pair_sum_target or net_edge < 0.01:
            pair_ok = False
        else:
            pair_ok = True

        window_s = settings.dip_window_ms / 1000.0
        ago = self.quote_ago(str(market["id"]), now, window_s)
        drop_yes = drop_no = 0.0
        if ago:
            if ago[0] > 0:
                drop_yes = (ago[0] - yes) / ago[0]
            if ago[1] > 0:
                drop_no = (ago[1] - no) / ago[1]
        shock = max(drop_yes, drop_no) >= settings.dip_threshold
        surge_yes = -drop_yes if drop_yes < 0 else 0.0
        surge_no = -drop_no if drop_no < 0 else 0.0
        surge = max(surge_yes, surge_no) >= settings.dip_threshold

        kind = None
        first = "YES" if yes <= no else "NO"
        drop = max(drop_yes, drop_no)
        if shock and pair_ok:
            kind = "dip_arb"
            first = "YES" if drop_yes >= drop_no else "NO"
        elif surge and pair_ok:
            # Fadi surge: other side just exploded, lift the lagged cheap leg as pair.
            kind = "dip_arb"
            first = "NO" if surge_yes >= surge_no else "YES"
            drop = max(surge_yes, surge_no)
        elif pair_ok and net_edge >= settings.completeness_min_edge:
            kind = "completeness"

        if not kind:
            return None
        shares, stake = size_complete_set(
            float(market.get("_cash") or 0) or 0.0,
            yes,
            no,
            settings,
        )
        # Caller fills cash; return the rest even if shares is 0 so it can size later.
        return {
            "kind": kind,
            "side": "BOTH",
            "signal_ts": now,
            "window_start": int(win["start"]),
            "window_end": int(win["end"]),
            "first_side": first,
            "yes_ask": yes,
            "no_ask": no,
            "price": round(raw_sum, 4),
            "fair": 1.0,
            "edge": round(net_edge, 4),
            "confidence": 0.95 if kind == "dip_arb" else 0.9,
            "drop": round(drop, 4),
            "edge_type": kind,
            "thesis": (
                f"{kind} {market.get('slug') or ''} pair={yes:.3f}+{no:.3f} "
                f"net_edge={net_edge:.3f} drop={drop:.1%} first={first} "
                f"depth={yes_sz:.0f}/{no_sz:.0f}"
            ),
            "taker": True,
            "shares": shares,
            "stake": stake,
            "asset": (win or {}).get("asset") or market.get("asset") or "",
        }


_TAPE = QuoteTape()


def global_tape() -> QuoteTape:
    return _TAPE
