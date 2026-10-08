"""Shared maker-fill + timestamp helper (r2_infra_timestamp_fill_audit). Required for R2-2, R2-7, R2-8 maker backtests.

Facts this module encodes (measured in r2_infra_timestamp_fill_audit, see data/kalshi_lab/strategies/
r2_infra_timestamp_fill_audit/result.json):
  * A 1-minute candle stamped end_period_ts = T aggregates the trade prints of (T-60, T] exactly (volume, price
    open/close/high/low match the prints at shift 0 s; a 2 s shift already breaks the match). Its quote close is the
    book at ~T (see QUOTE_CLOSE_OFFSET_S). => a backtest that decides at t may use candles with T <= t, and an order
    that goes live at t_live may only be judged on candles with T - 60 >= t_live (the minute containing t_live mixes
    prints from before the order existed).
  * REST /markets quotes lag the trade prints (REST_STALENESS_S); a live bot must treat the quote as ~that old.
  * Fill logic for a resting order, YES-price terms (YES bid at b; a NO bid at nb is a YES offer at a = 1 - nb):
      certain fill  - a print strictly through the level (YES bid: print < b; YES offer: print > a), or the opposite
                      quote reaching the level (YES bid: yes_ask <= b; YES offer: yes_bid >= a): the book cannot be
                      crossed while our order rests, so the whole level (us included) was consumed;
      at-level      - a print exactly at the level: a taker hit the queue at our price. If our order CREATED the level
                      (strictly inside the spread when it went live) we are first in the queue -> fill (up to the
                      print size). If we JOINED an existing level, the fill depends on the queue ahead (unknown in
                      candles): use P_FILL_JOIN_TOUCH or the print simulator with a queue estimate.
  * Kalshi print semantics: taker_side 'yes' = taker bought YES at yes_price (lifted the YES ask = hit resting NO
    bids); taker_side 'no' = taker sold YES / bought NO (hit resting YES bids). Verified: 'yes' prints sit 1 tick
    above simultaneous 'no' prints.

Usage in a backtest:
    from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_fill import norm_candle, maker_fill
    cs = sorted((norm_candle(c) for c in candles), key=lambda c: c["T"])   # API dict, lab compact row, commod row
    r = maker_fill("yes", b, t_live, t_end, cs, queue=None)   # queue=None -> DEFAULT_QUEUE for the series, or pass
                                                              # the displayed size at your level (orderbook) / 0 / 1e12
    # r = {'status': 'filled'|'unfilled'|'crossed', 'fill_ts', 'kind', 'created'}; 'crossed' = taker fill + fee
  Rules: decide at t with candles T <= t; an order live from t_live is judged on candles with T - 60 >= t_live;
  hold-to-settlement return per $ = (win - px) / px, maker fee 0 on 'quadratic' series, 0.25 x taker formula x
  fee_multiplier on 'quadratic_with_maker_fees'. Report Q=0 and Q=inf bounds next to the default queue.
"""
from __future__ import annotations
import bisect, datetime as dt

EPS = 1e-9
# --- measured constants (r2_infra_timestamp_fill_audit, 2026-10-08; details in result.json) ---
CANDLE_TRADE_WINDOW = (-60, 0)       # prints of (T-60, T]: volume matches the prints exactly at shift 0 in 100% of
                                     # 2,336 (weather) / 1,013 (rain) / 196 (INXU) active minutes; +-2 s breaks it
QUOTE_CLOSE_OFFSET_S = 0             # quote close = book at T (+-1 s): first revealing print after T+s matches best at s=0
REST_STALENESS_S = {"median": 9.6, "p10": 3.4, "p90": 17.0, "max": 22.2}   # /markets (and /markets/{t}): per-URL cache
REST_CACHE_TTL_S = 15                # refreshed every ~10-15 s, so the age is a 0..15 s sawtooth plus ~1-3 s
ORDERBOOK_STALENESS_S = 1            # /markets/{t}/orderbook is live (best match at lag 0..-1 s, 94-96%)
DEFAULT_QUEUE = {"KXHIGH": 50, "KXRAIN": 25, "KXGOLDD": 100, "KXWTI": 100, "KXNATGASD": 100, "KXBTCD": 800}  # live top sizes, rough


def candles_have_prints(cs: list[dict]) -> bool:
    """Maker simulations need the candle trade fields (price low/high). The lab's compact rows
    [T, ask, bid, ask_lo, bid_hi, vol] do not have them: there the quote-only rule found 55-76% of the real fills and
    biased the filled-order return by -12 to -14 points (rain). Refetch candles with the API 'price' block."""
    return any(c.get("px_lo") is not None or c.get("px_hi") is not None for c in cs)


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def norm_prints(raw: list[dict]) -> list[tuple]:
    """/markets/trades rows -> sorted [(t, yes_price, taker_side, count)]."""
    out = []
    for x in raw:
        p = x.get("yes_price_dollars")
        if p is None and x.get("yes_price") is not None:
            p = x["yes_price"] / 100
        out.append((ts(x["created_time"]), float(p), x.get("taker_side"), float(x.get("count_fp", x.get("count")) or 0)))
    return sorted(out)


def norm_candle(c) -> dict:
    """-> {'T', 'ask', 'bid', 'ask_lo', 'ask_hi', 'bid_lo', 'bid_hi', 'px_lo', 'px_hi', 'vol'} (None when unknown).
    Accepts: API candlestick dict; lab compact [T, ask, bid, ask_lo, bid_hi, vol]; r2_daily_commod_maker row
    [T, ask_c, bid_c, ask_lo, ask_hi, bid_lo, bid_hi, px_lo, px_hi, vol]."""
    if isinstance(c, dict):
        def f(k, s):
            v = (c.get(k) or {}).get(s)
            return float(v) if v is not None else None
        vol = float(c.get("volume_fp") or c.get("volume") or 0)
        return {"T": c["end_period_ts"], "ask": f("yes_ask", "close_dollars"), "bid": f("yes_bid", "close_dollars"),
                "ask_lo": f("yes_ask", "low_dollars"), "ask_hi": f("yes_ask", "high_dollars"), "bid_lo": f("yes_bid", "low_dollars"),
                "bid_hi": f("yes_bid", "high_dollars"), "px_lo": f("price", "low_dollars") if vol > 0 else None,
                "px_hi": f("price", "high_dollars") if vol > 0 else None, "vol": vol}
    if len(c) == 6:
        return {"T": c[0], "ask": c[1], "bid": c[2], "ask_lo": c[3], "ask_hi": None, "bid_lo": None, "bid_hi": c[4], "px_lo": None, "px_hi": None, "vol": c[5]}
    if len(c) == 10:
        return {"T": c[0], "ask": c[1], "bid": c[2], "ask_lo": c[3], "ask_hi": c[4], "bid_lo": c[5], "bid_hi": c[6], "px_lo": c[7], "px_hi": c[8], "vol": c[9]}
    raise ValueError("unknown candle row format")


def quote_at(cs: list[dict], t: float, max_age: float = 1800) -> tuple[float, float] | None:
    """(yes_ask, yes_bid) = close of the last candle with T <= t (the book at T), if <= max_age old."""
    i = bisect.bisect_right([c["T"] for c in cs], t) - 1
    if i < 0 or t - cs[i]["T"] > max_age or cs[i]["ask"] is None or cs[i]["bid"] is None:
        return None
    return cs[i]["ask"], cs[i]["bid"]


def _hits(side: str, level: float, p: tuple) -> int:
    """How a print relates to an order resting at `level` (YES terms).
    0 no contact; 1 aggressive at our level (a taker hit the queue at our price: YES bid <- taker sold YES ('no');
    YES offer <- taker bought YES ('yes')); 2 strictly through our level (any taker side: the level, us included, was
    gone); 3 counterfactual cross at our level: the print shows a resting order on the OTHER side at our price
    (YES bid at b, but someone bought YES from an offer at b), impossible while our order rests -> that order's owner
    would have traded with us (unless it was post-only)."""
    t, px, taker, n = p
    if side == "yes":
        if px < level - EPS:
            return 2
        if abs(px - level) < EPS:
            return 1 if taker == "no" else 3
        return 0
    if px > level + EPS:
        return 2
    if abs(px - level) < EPS:
        return 1 if taker == "yes" else 3
    return 0


def fill_from_prints(side: str, level: float, t_live: float, t_end: float, prints: list[tuple], size: float = 10.0,
                     queue_ahead: float = 0.0, count_cross: bool = True) -> dict:
    """Time-priority fill of an order resting at `level` (YES terms) from t_live to t_end, from exact trade prints.
    queue_ahead: contracts ahead of us at our level when we went live (0 if our order created the level). Orders ahead
    are assumed never to cancel (pessimistic for a join; exact for a created level).
    Fill events: aggressive prints at our level consume the queue ahead, then fill us; a print strictly through our
    level fills us completely; a counterfactual cross (resting opposite order at our price, see _hits == 3) fills us
    completely if count_cross. Returns first/full fill times, filled qty and the kind of the first fill."""
    q = queue_ahead; got = 0.0; first = full = kind = None
    i = bisect.bisect_right([p[0] for p in prints], t_live)
    for p in prints[i:]:
        if p[0] > t_end:
            break
        h = _hits(side, level, p)
        if h == 2 or (h == 3 and count_cross):
            got = size; k = "through" if h == 2 else "cross"
        elif h == 1:
            take = p[3]; k = "at_level"
            if q > 0:
                used = min(q, take); q -= used; take -= used
            got = min(size, got + take)
        else:
            continue
        if got > 0 and first is None:
            first = p[0]; kind = k
        if got >= size - EPS:
            full = p[0]; break
    return {"first_fill_ts": first, "full_fill_ts": full, "filled_qty": got, "kind": kind}


def candle_fill(side: str, level: float, t_live: float, t_end: float, cs: list[dict], created: bool | None = None,
                touch_rule: str = "auto") -> dict:
    """Candle-only fill decision for an order resting at `level` (YES terms) from t_live to t_end.
    Uses only candles wholly after t_live (T - 60 >= t_live) and with T <= t_end.
    created: True if our order was strictly inside the spread when it went live (we are first in the queue); None ->
             derived from the candle quote at t_live.
    touch_rule: 'auto' (at-level print = fill iff created), 'strict' (never), 'always' (always).
    Returns {'status': 'crossed'|'filled'|'unfilled', 'fill_ts', 'kind': 'through'|'quote'|'touch'|None, 'created'}.
    'crossed': the order was marketable at t_live -> treat as a taker fill at the ask (YES) / bid (NO side) + fee."""
    q = quote_at(cs, t_live)
    if q:
        ask, bid = q
        if side == "yes" and ask is not None and ask <= level + EPS and ask > 0:
            return {"status": "crossed", "fill_ts": t_live, "kind": None, "created": False, "px": ask}
        if side == "no" and bid is not None and bid >= level - EPS and bid > 0:
            return {"status": "crossed", "fill_ts": t_live, "kind": None, "created": False, "px": bid}
        if created is None:
            created = (bid < level - EPS) if side == "yes" else (ask > level + EPS)
    elif created is None:
        created = False
    use_touch = touch_rule == "always" or (touch_rule == "auto" and created)
    for c in cs:
        if c["T"] - 60 < t_live - EPS:
            continue
        if c["T"] > t_end + EPS:
            break
        if side == "yes":
            thr = c["px_lo"] is not None and c["px_lo"] < level - EPS
            quo = c["ask_lo"] is not None and 0 < c["ask_lo"] <= level + EPS
            tch = c["px_lo"] is not None and abs(c["px_lo"] - level) < EPS
        else:
            thr = c["px_hi"] is not None and c["px_hi"] > level + EPS
            quo = c["bid_hi"] is not None and c["bid_hi"] >= level - EPS and c["bid_hi"] > 0
            tch = c["px_hi"] is not None and abs(c["px_hi"] - level) < EPS
        if thr or quo or (use_touch and tch):
            return {"status": "filled", "fill_ts": c["T"], "kind": "through" if thr else ("quote" if quo else "touch"), "created": created}
    return {"status": "unfilled", "fill_ts": None, "kind": None, "created": created}


def candle_queue_fill(side: str, level: float, t_live: float, t_end: float, cs: list[dict], queue: float,
                      size: float = 10.0) -> float | None:
    """JOINED level (queue ahead = `queue` contracts): certain fills (print through / opposite quote at our level) at
    once; otherwise the candle volume of minutes whose print range touches our level without going through counts as
    at-level volume, and we fill when it exceeds queue + size. Agreed with the print-exact simulation on 96-100% of
    weather orders for queue in {0, 5, 25, 100, 500, inf}."""
    cum = 0.0
    for c in cs:
        if c["T"] - 60 < t_live - EPS:
            continue
        if c["T"] > t_end + EPS:
            break
        if side == "yes":
            if (c["px_lo"] is not None and c["px_lo"] < level - EPS) or (c["ask_lo"] is not None and 0 < c["ask_lo"] <= level + EPS):
                return c["T"]
            if c["px_lo"] is not None and abs(c["px_lo"] - level) < EPS:
                cum += c["vol"] or 0
        else:
            if (c["px_hi"] is not None and c["px_hi"] > level + EPS) or (c["bid_hi"] is not None and 0 < c["bid_hi"] and c["bid_hi"] >= level - EPS):
                return c["T"]
            if c["px_hi"] is not None and abs(c["px_hi"] - level) < EPS:
                cum += c["vol"] or 0
        if cum >= queue + size - EPS:
            return c["T"]
    return None


def maker_fill(side: str, level: float, t_live: float, t_end: float, cs: list[dict], queue: float | None = None,
               series: str | None = None, size: float = 10.0) -> dict:
    """The shared fill model. side 'yes' = YES bid at `level`; side 'no' = NO bid at 1 - level (YES offer at level).
    created level (strictly inside the spread at t_live): first touch or through (exact: we are first in the queue).
    joined/deeper level: certain fills + candle at-level volume beyond `queue` (default DEFAULT_QUEUE[series prefix]).
    'crossed': marketable at t_live -> a taker fill at the touch, pay the taker fee."""
    base = candle_fill(side, level, t_live, t_end, cs, touch_rule="auto")
    if base["status"] == "crossed" or base["created"]:
        return base
    if queue is None:
        queue = next((v for k, v in DEFAULT_QUEUE.items() if series and series.startswith(k)), 100)
    ft = candle_queue_fill(side, level, t_live, t_end, cs, queue, size)
    return {"status": "filled" if ft else "unfilled", "fill_ts": ft, "kind": "queue" if ft else None, "created": False, "queue": queue}
