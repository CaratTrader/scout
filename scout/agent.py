from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any

from . import crypto_lag as crypto_lag_module
from .config import JOURNAL_PATH, SCAN_PATH, Settings, settings_from_env
from .execute import (
    LiveDisabled,
    ReconcileRequired,
    execute,
    live_cancel,
    live_order_state,
    live_position_balance,
    live_redeem,
    live_rest_buy,
    live_sell,
    rest_fallback_applies,
)
from .crypto_lag import (
    crypto_settle_price,
    is_crypto_updown,
    is_short_updown_window,
    iter_crypto_events,
    parse_window,
    score_crypto_windows,
    start_chainlink_stream,
)
from .gamma import iter_markets, iter_near_term_markets, liquid_enough, normalize_market
from .grok_fair import (
    grok_priority,
    grok_research_eligible,
    is_same_day_sport,
    score_with_grok,
    survival_mode,
)
from .ledger import (
    cancel_order,
    close_position,
    fill_order,
    load_ledger,
    mark_price,
    mark_to_market,
    maybe_halt,
    paper_maker_fillable,
    record_order,
    save_ledger,
)
from .intel import attach_intel, cached_intel, intel_brief, kalshi_prior
from .risk import grok_dump, occupied_ids, veto
from .signals import attach_sizing, build_candidates


_GROK_WORKER_LOCK = threading.Lock()
_GROK_WORKER: threading.Thread | None = None
_GROK_RESULT: dict[str, Any] | None = None


def crypto_only() -> bool:
    """CRYPTO_ONLY=1 skips the general Gamma scan so a cycle takes about a second.
    The late-window lock profile needs that: it must re-evaluate every few seconds
    inside the last 45 s of a window, and the full scan alone takes ~20 s."""
    return (os.getenv("CRYPTO_ONLY") or "0").strip().lower() in {"1", "true", "on", "yes"}


_CYCLE_NO = 0


def quiet_cycles() -> int:
    """QUIET_CYCLES=N: print and journal the routine per-cycle lines only every Nth cycle.
    Cycles with fills, posts, exits, vetoes, errors or a halt always print. Needed once the
    loop runs every 2 s (the lock profile); 0/1 keeps the paper default of every cycle."""
    try:
        return max(1, int(os.getenv("QUIET_CYCLES") or 1))
    except ValueError:
        return 1


def _verbose_cycle() -> bool:
    return _CYCLE_NO % quiet_cycles() == 0


def _cycle_has_activity(report: dict[str, Any]) -> bool:
    return bool(
        report.get("fills") or report.get("posts") or report.get("exits") or report.get("vetoes")
        or report.get("errors") or report.get("halt_reason")
    )


def collect_universe(settings: Settings) -> list[dict[str, Any]]:
    universe: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in ([] if crypto_only() else iter_markets(settings)):
        market = normalize_market(raw)
        if not market or not liquid_enough(market, settings, crypto=is_crypto_updown(market)):
            continue
        if is_crypto_updown(market):
            win = parse_window(market)
            if not is_short_updown_window(win):
                continue
        if market["id"] in seen:
            continue
        seen.add(market["id"])
        universe.append(market)
    for event in iter_crypto_events(settings):
        if event.get("closed"):
            continue
        parent = [{"id": event.get("id"), "slug": event.get("slug")}]
        for raw in event.get("markets") or []:
            raw = dict(raw)
            raw.setdefault("events", parent)
            market = normalize_market(raw)
            if not market:
                continue
            market["event_slug"] = event.get("slug") or ""
            market["slug"] = market.get("slug") or event.get("slug") or ""
            win = parse_window(market)
            if not is_short_updown_window(win):
                continue
            if not liquid_enough(market, settings, crypto=True):
                continue
            if market["id"] in seen:
                continue
            seen.add(market["id"])
            universe.append(market)
    universe.sort(key=lambda m: (0 if is_crypto_updown(m) else 1, -m["volume_24h"]))
    return universe

def hydrate_open_markets(
    ledger: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    settings: Settings,
) -> None:
    from .gamma import _get_json_any, normalize_market

    for pos in ledger.get("positions") or []:
        mid = str(pos.get("market_id") or "")
        if mid in by_id:
            continue
        slug = pos.get("slug") or pos.get("event_slug")
        if not slug:
            continue
        try:
            rows = _get_json_any(
                f"https://gamma-api.polymarket.com/markets?slug={slug}",
                settings.user_agent,
                timeout=8,
            )
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = rows.get("markets") or [rows]
        if not isinstance(rows, list):
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            market = normalize_market(raw)
            if not market:
                continue
            by_id[market["id"]] = market
            by_id[mid] = market


def _grok_shortlist(pool: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Soon, 12–65¢, Grok can check a fact before the market dies."""
    ranked = sorted(pool, key=grok_priority)
    return ranked[:n]


def collect_grok_pool(settings: Settings, ledger: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    survive = survival_mode(ledger)
    horizon = 2
    sources = list(iter_near_term_markets(settings, days=horizon)) + list(iter_markets(settings))
    for raw in sources:
        market = normalize_market(raw)
        if not market or is_crypto_updown(market):
            continue
        if market["spread"] <= 0 or market["spread"] > 0.20 or market["liquidity"] < 80:
            continue
        days = market.get("days_to_end")
        if days is None or float(days) > max(2.0, settings.grok_max_days):
            continue
        if not grok_research_eligible(market, survival=survive):
            continue
        if market["id"] in seen:
            continue
        seen.add(market["id"])
        pool.append(market)
    pool.sort(key=grok_priority)
    return pool


def journal(event: dict[str, Any]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL_PATH.open("a") as fh:
        fh.write(json.dumps(event) + "\n")


def _run_grok_worker(settings: Settings, ledger_snapshot: dict[str, Any]) -> None:
    global _GROK_RESULT, _GROK_WORKER
    started_at = time.time()
    try:
        pool = collect_grok_pool(settings, ledger_snapshot)
        intel = cached_intel()
        attach_intel(pool, intel)
        shortlist = _grok_shortlist(pool, settings.grok_top)
        scores: dict[str, dict[str, Any]] = {}
        if shortlist:
            scores.update(
                score_with_grok(
                    shortlist,
                    settings.grok_model,
                    settings.xai_base_url,
                    ttl_seconds=settings.grok_every_seconds,
                    intel_brief=intel_brief(intel),
                )
            )
            for row in shortlist:
                prior = kalshi_prior(row)
                existing = scores.get(row["id"]) or {}
                if prior and existing.get("edge_type") in {None, "none", ""}:
                    scores[row["id"]] = prior
        result: dict[str, Any] = {
            "started_at": started_at,
            "completed_at": time.time(),
            "pool_count": len(pool),
            "shortlist": shortlist,
            "scores": scores,
            "confirmed_count": len(intel.get("confirmed") or []),
            "kalshi_count": len(intel.get("kalshi") or []),
        }
    except Exception as exc:
        result = {
            "started_at": started_at,
            "completed_at": time.time(),
            "pool_count": 0,
            "shortlist": [],
            "scores": {},
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
    with _GROK_WORKER_LOCK:
        _GROK_RESULT = result
        _GROK_WORKER = None


def _start_grok_worker(settings: Settings, ledger: dict[str, Any]) -> bool:
    global _GROK_WORKER
    with _GROK_WORKER_LOCK:
        if _GROK_WORKER is not None or _GROK_RESULT is not None:
            return False
        worker = threading.Thread(
            target=_run_grok_worker,
            args=(settings, copy.deepcopy(ledger)),
            name="scout-grok",
            daemon=True,
        )
        _GROK_WORKER = worker
        worker.start()
        return True


def _take_grok_result() -> dict[str, Any] | None:
    global _GROK_RESULT
    with _GROK_WORKER_LOCK:
        result = _GROK_RESULT
        _GROK_RESULT = None
        return result


def _grok_worker_running() -> bool:
    with _GROK_WORKER_LOCK:
        return _GROK_WORKER is not None


def _refresh_crypto_books(markets: list[dict[str, Any]], client: Any = None) -> int:
    """Replace Gamma discovery quotes with current executable CLOB quotes.

    Websocket book cache first (sub-second, no round trip); REST fallback while
    the stream warms up or if it drops."""
    from . import streams

    crypto = [market for market in markets if is_crypto_updown(market)]
    for market in crypto:
        market["clob_fresh"] = False
    tokens = [
        token
        for market in crypto
        for token in (market.get("yes_token"), market.get("no_token"))
        if token
    ]
    if not tokens:
        return 0
    streams.start_streams()
    streams.set_book_tokens(tokens)
    ws_refreshed = 0
    for market in crypto:
        yes = streams.book(str(market.get("yes_token") or ""))
        no = streams.book(str(market.get("no_token") or ""))
        if not yes or not no:
            continue
        yes_ask, no_ask = float(yes["ask"]), float(no["ask"])
        # A side with no asks at all (decided window: nobody offers the loser) streams as 0.
        # The book is still executable on the other side, so offer the empty side at 0.999
        # instead of dropping to the REST fallback for every token.
        if not (0 < yes_ask < 1) and 0 < no_ask < 1:
            yes_ask = 0.999
        elif not (0 < no_ask < 1) and 0 < yes_ask < 1:
            no_ask = 0.999
        if not (0 < yes_ask < 1 and 0 < no_ask < 1):
            continue
        market.update(
            {
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "yes_bid": float(yes["bid"]),
                "no_bid": float(no["bid"]),
                "yes_ask_size": float(yes.get("ask_size") or 0),
                "no_ask_size": float(no.get("ask_size") or 0),
                "tick_size": float(yes.get("tick_size") or 0.01),
                "clob_fresh": True,
                "clob_quoted_at": float(yes["quoted_at"]),
                # when the book last actually changed (quoted_at only says the stream is alive)
                "clob_event_ts": max(float(yes.get("event_ts") or 0), float(no.get("event_ts") or 0)),
            }
        )
        ws_refreshed += 1
    if ws_refreshed == len(crypto):
        return ws_refreshed
    owned = client is None
    if client is None:
        from polymarket import PublicClient

        client = PublicClient()
    try:
        books = client.get_order_books(token_ids=tokens)
    except Exception as exc:
        print("CLOB quote refresh failed:", type(exc).__name__, str(exc)[:160])
        return 0
    finally:
        if owned:
            try:
                client.close()
            except Exception:
                pass
    by_token = {str(book.token_id): book for book in books}
    refreshed = 0
    for market in crypto:
        yes = by_token.get(str(market.get("yes_token") or ""))
        no = by_token.get(str(market.get("no_token") or ""))
        if not yes or not no or (not yes.asks and not no.asks):
            continue
        # Near the end of a decided window one side often has no asks at all (nobody
        # offers the loser). That book is still executable on the other side: treat the
        # empty side as offered at 0.999 with no size so the favourite can be scored.
        yes_ask = min(float(level.price) for level in yes.asks) if yes.asks else 0.999
        no_ask = min(float(level.price) for level in no.asks) if no.asks else 0.999
        yes_bid = max((float(level.price) for level in yes.bids), default=0.0)
        no_bid = max((float(level.price) for level in no.bids), default=0.0)
        if not (0 < yes_ask < 1 and 0 < no_ask < 1):
            continue
        yes_cap = yes_ask + 0.02
        no_cap = no_ask + 0.02
        yes_ask_size = sum(float(level.size) for level in yes.asks if float(level.price) <= yes_cap)
        no_ask_size = sum(float(level.size) for level in no.asks if float(level.price) <= no_cap)
        market.update(
            {
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "yes_bid": yes_bid,
                "no_bid": no_bid,
                "yes_ask_size": yes_ask_size,
                "no_ask_size": no_ask_size,
                "tick_size": float(yes.tick_size),
                "clob_fresh": True,
                "clob_quoted_at": time.time(),
                "clob_event_ts": time.time(),
            }
        )
        refreshed += 1
    return sum(1 for market in crypto if market.get("clob_fresh"))

def hunt_tape(markets: list[dict[str, Any]], ledger: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """3s DipArb + complete-set. The 20s Gamma loop cannot see a 15% panic print."""
    from .dip_arb import global_tape, size_complete_set
    from .execute import LiveDisabled, execute
    from .smart_money import recent_aggression

    crypto = [market for market in markets if is_crypto_updown(market)]
    if not crypto or ledger.get("halted"):
        return []
    tape = global_tape()
    taken: list[dict[str, Any]] = []
    deadline = time.time() + max(1.0, float(settings.tape_seconds))
    last_beat = 0.0
    shocks = pairs = ticks = 0
    from polymarket import PublicClient

    client = PublicClient()
    try:
        while time.time() < deadline:
            if ledger.get("halted"):
                break
            ticks += 1
            now = time.time()
            _refresh_crypto_books(crypto, client)
            cash = float(ledger.get("cash") or 0)
            for market in crypto:
                tape.observe(market, now)
                market["_cash"] = cash
                sig = tape.signal(market, now, settings)
                if not sig:
                    continue
                if sig["kind"] == "dip_arb":
                    shocks += 1
                else:
                    pairs += 1
                cand = dict(market)
                cand.update(sig)
                if cand["stake"] < settings.min_trade:
                    shares, stake = size_complete_set(cash, cand["yes_ask"], cand["no_ask"], settings)
                    cand["shares"], cand["stake"] = shares, stake
                if cand["stake"] < settings.min_trade:
                    print(f"  tape skip dust {cand.get('slug', '')[:36]} edge={cand['edge']}")
                    continue
                token = cand.get("yes_token") if cand.get("first_side") == "YES" else cand.get("no_token")
                flow = recent_aggression(str(token or ""), since_s=5.0)
                if flow >= 25:
                    cand["stake"] = round(
                        min(cand["stake"] * 1.15, cash * settings.max_fraction, cash), 4
                    )
                    cand["thesis"] += f" flow=${flow:.0f}"
                reason = veto(cand, ledger, settings)
                if reason:
                    print(f"  tape veto {reason} {str(cand.get('slug') or '')[:40]}")
                    continue
                try:
                    fill = execute(ledger, cand, settings)
                except (LiveDisabled, RuntimeError, ValueError) as exc:
                    print(f"  tape miss {type(exc).__name__}: {exc}")
                    if isinstance(exc, ReconcileRequired):
                        ledger["entry_pause_until"] = time.time() + 10.0
                        return taken
                    continue
                taken.append(fill)
                print(f"  TAPE {fill['side']} ${fill['stake']:.2f} {cand['thesis'][:110]}")
                journal(
                    {
                        "ts": time.time(),
                        "event": "tape",
                        "kind": cand["kind"],
                        "id": cand["id"],
                        "stake": cand["stake"],
                        "edge": cand["edge"],
                        "thesis": cand["thesis"],
                    }
                )
                return taken
            if now - last_beat >= 2.5:
                fresh = sum(1 for market in crypto if market.get("clob_fresh"))
                window_s = settings.dip_window_ms / 1000.0
                best_drop, best_sum = 0.0, 9.0
                for market in crypto:
                    if not market.get("clob_fresh"):
                        continue
                    drop, raw = tape.peek(market, now, window_s)
                    best_drop = max(best_drop, drop)
                    if 0 < raw < best_sum:
                        best_sum = raw
                print(
                    f"  tape ticks={ticks} fresh={fresh} shock={shocks} pair={pairs} "
                    f"drop={best_drop:.0%} sum={best_sum:.3f} cash={cash:.2f}"
                )
                last_beat = now
            time.sleep(max(0.12, float(settings.tape_interval)))
    finally:
        try:
            client.close()
        except Exception:
            pass
    return taken


def _consecutive_crypto_losses(ledger: dict[str, Any]) -> int:
    """Only this session. Yesterday's losers must not halt the first overnight cycle."""
    started = str(ledger.get("session_started_at") or "")
    if not started:
        return 0
    n = 0
    for fill in reversed(ledger.get("fills") or []):
        if not str(fill.get("side") or "").startswith("CLOSE_"):
            continue
        ts = str(fill.get("ts") or "")
        if ts < started:
            break
        if not (is_crypto_updown(fill) or "up or down" in str(fill.get("question") or "").lower()):
            continue
        if float(fill.get("pnl") or 0) >= 0:
            break
        n += 1
    return n


def _apply_risk_state(ledger: dict[str, Any], equity: float, settings: Settings) -> None:
    """Session drawdown control based on realized bankroll, not transient marks."""
    cash = float(ledger.get("cash") or 0)
    if int(ledger.get("risk_model_version") or 0) < 2:
        # Migrate from the old marked-equity peak, which could be inflated by a
        # temporary 5m quote. The currently reconciled account starts the new regime.
        baseline = equity if ledger.get("positions") else cash
        ledger.update(
            {
                "risk_model_version": 2,
                "risk_baseline_cash": baseline,
                "realized_peak_cash": baseline,
            }
        )
        if ledger.get("halt_reason") == "daily loss limit":
            ledger["halted"] = False
            ledger["halt_reason"] = ""

    baseline = float(ledger.get("risk_baseline_cash") or cash)
    peak = float(ledger.get("realized_peak_cash") or baseline)
    if not ledger.get("positions"):
        # A CLOB deposit is not a $2 mark bump. Rebase so profit-lock cannot fire on the add.
        # Threshold is above the $4–6 clip so a green 5m cannot look like a refill.
        if cash >= peak + 12.0:
            from .ledger import utc_now

            print(f"deposit detected, session baseline {baseline:.2f} -> {cash:.2f}")
            baseline = cash
            peak = cash
            ledger["risk_baseline_cash"] = cash
            ledger["realized_peak_cash"] = cash
            ledger["session_started_at"] = utc_now()
        else:
            peak = max(peak, cash)
            ledger["realized_peak_cash"] = peak
    ledger["last_equity"] = equity
    runup = max(0.0, peak - baseline)
    locked_floor = baseline + runup * settings.profit_lock_fraction
    ledger["locked_equity_floor"] = round(locked_floor, 4)
    streak = _consecutive_crypto_losses(ledger)
    ledger["crypto_loss_streak"] = streak
    if peak - equity >= settings.max_session_drawdown:
        ledger["halted"] = True
        ledger["halt_reason"] = f"session drawdown: peak={peak:.2f} equity={equity:.2f}"
    # Ignore tiny CLOB sync bumps (a $2 peak must not lock a $4 clip). PROFIT_LOCK_FRACTION<=0 disables the lock.
    elif settings.profit_lock_fraction > 0 and runup >= 10.0 and equity < locked_floor:
        ledger["halted"] = True
        ledger["halt_reason"] = f"profit lock: floor={locked_floor:.2f} equity={equity:.2f}"
    elif streak >= settings.max_consecutive_crypto_losses and not ledger.get("positions"):
        ledger["halted"] = True
        ledger["halt_reason"] = f"crypto loss streak: {streak}"


def grok_skip_reason(
    ledger: dict[str, Any],
    scores: dict[str, dict[str, Any]],
    settings: Settings,
    *,
    use_grok: bool,
    now: float | None = None,
) -> str | None:
    """Grok hunts checkable books. Short crypto up/down is not in the pool."""
    if not use_grok:
        return "off"
    if ledger.get("halted"):
        return "halted"
    last = float(ledger.get("last_grok_ts") or 0)
    elapsed = (now if now is not None else time.time()) - last
    if elapsed < settings.grok_every_seconds:
        return f"cooldown {int(settings.grok_every_seconds - elapsed)}s"
    return None


def _execute_candidate_batch(
    candidates: list[dict[str, Any]],
    ledger: dict[str, Any],
    settings: Settings,
    *,
    held: set[str],
    taken: list[dict[str, Any]],
    vetoes: list[dict[str, str]],
    errors: list[str],
) -> None:
    for cand in candidates:
        if settings.live and _entry_paused(ledger):
            vetoes.append({"id": cand["id"], "reason": "entry_pause", "kind": cand.get("kind", "")})
            continue
        reason = veto(cand, ledger, settings, occupied=held) or _cap_lock_stake(cand, ledger, settings)
        age = max(0.0, time.time() - float(cand.get("signal_ts") or time.time()))
        common = {
            "id": cand["id"],
            "kind": cand.get("kind"),
            "side": cand.get("side"),
            "price": cand.get("price"),
            "fair": cand.get("fair"),
            "edge": cand.get("edge"),
            "signal_ts": cand.get("signal_ts"),
            "signal_age_ms": round(age * 1000),
            "window_end": cand.get("window_end"),
            "oracle_open": cand.get("oracle_open"),
            "oracle_spot": cand.get("oracle_spot"),
            "exchange_spot": cand.get("exchange_spot"),
            "exchange_basis_bps": cand.get("exchange_basis_bps"),
            "coinbase_basis_bps": cand.get("coinbase_basis_bps"),
            "raw_model_p_yes": cand.get("raw_model_p_yes"),
            "market_p_yes": cand.get("market_p_yes"),
            "model_weight": cand.get("model_weight"),
            "thesis": cand.get("thesis"),
        }
        if reason:
            vetoes.append({"id": cand["id"], "reason": reason, "kind": cand.get("kind", "")})
            journal({"ts": time.time(), "event": "veto", "reason": reason, **common})
            continue
        try:
            fill = execute(ledger, cand, settings)
            taken.append(fill)
            held.add(cand["id"])
            if settings.live:
                save_ledger(ledger)  # a real fill must survive any later exception in this cycle
            journal(
                {
                    "ts": time.time(),
                    "event": "submit",
                    "stake": cand.get("stake"),
                    "fill_price": fill.get("price"),
                    "fill_shares": fill.get("shares"),
                    "fee": fill.get("fee"),
                    **common,
                }
            )
        except (LiveDisabled, RuntimeError, ValueError) as exc:
            message = f"{cand['id']}: {exc}"
            errors.append(message)
            journal(
                {
                    "ts": time.time(),
                    "event": "execution_reject",
                    "reason": str(exc),
                    **common,
                }
            )
            if settings.live and "restricted in your region" in str(exc).lower():
                # Geoblock is an exit-IP state that lasts minutes to hours: stop hammering the
                # venue (and stop resting bids) for a minute, then probe again.
                ledger["entry_pause_until"] = time.time() + 60.0
                journal({"ts": time.time(), "event": "entry_pause", "reason": "geoblock", "seconds": 60})
                continue
            if settings.live and rest_fallback_applies(cand, exc):
                # The offer was gone before our taker order landed: rest a bid at the
                # ceiling instead (fee 0). Tracked in ledger["orders"]; _working fills it.
                try:
                    raw = live_rest_buy(cand, settings)
                    order = record_order(
                        ledger,
                        market=cand,
                        side=cand["side"],
                        stake=round(float(raw["rest_size"]) * float(raw["rest_price"]), 4),
                        price=float(raw["rest_price"]),
                        shares=float(raw["rest_size"]),
                        reason=f"lock rest edge={cand.get('edge')}",
                        mode="live",
                        settings=settings,
                        raw=raw,
                    )
                    held.add(cand["id"])
                    save_ledger(ledger)
                    print(f"  REST {cand['side']:4} {order['shares']} sh @ {order['price']}  {str(cand.get('question'))[:60]}")
                    journal({"ts": time.time(), "event": "post", "price": order["price"], "shares": order["shares"], "venue_id": order.get("venue_id"), **common})
                except (LiveDisabled, RuntimeError, ValueError) as rest_exc:
                    errors.append(f"{cand['id']}: rest: {rest_exc}")
                    journal({"ts": time.time(), "event": "rest_reject", "reason": str(rest_exc), **common})
            if isinstance(exc, ReconcileRequired):
                ledger["entry_pause_until"] = time.time() + 10.0
                break


def lock_hunt_seconds() -> float:
    """LOCK_HUNT_SECONDS=N: after the normal scan, watch the windows inside (or about to enter)
    the TWAP-lock band every ~0.3 s for up to N seconds and buy the moment the favourite is
    offered inside the band. Offers at 0.97-0.99 on a decided window live for seconds; a 2-5 s
    cycle loses that race (first live attempt: "no orders found to match"). 0 = off (paper)."""
    try:
        return max(0.0, float(os.getenv("LOCK_HUNT_SECONDS") or 0))
    except ValueError:
        return 0.0


def hunt_locks(
    crypto_mkts: list[dict[str, Any]],
    ledger: dict[str, Any],
    settings: Settings,
    *,
    held: set[str],
    taken: list[dict[str, Any]],
    vetoes: list[dict[str, str]],
    errors: list[str],
    client: Any = None,
) -> int:
    """Sub-second watch of the lock-eligible windows; returns the number of watch ticks."""
    from .twap_lock import MAX_SECONDS, MIN_SECONDS

    budget = lock_hunt_seconds()
    if budget <= 0 or ledger.get("halted"):
        return 0
    now = time.time()
    eligible: list[tuple[float, dict[str, Any]]] = []
    for market in crypto_mkts:
        win = parse_window(market)
        if not win:
            continue
        left = float(win["end"]) - now
        if MIN_SECONDS - 1.0 <= left <= MAX_SECONDS + budget:
            eligible.append((float(win["end"]), market))
    if not eligible:
        return 0
    deadline = min(now + budget, max(end for end, _ in eligible) - MIN_SECONDS + 1.0)
    markets = [m for _, m in eligible]
    owned = client is None
    if client is None:
        from polymarket import PublicClient

        client = PublicClient()
    was_verbose = crypto_lag_module.VERBOSE
    crypto_lag_module.VERBOSE = False
    ticks = 0
    last_try: dict[str, float] = {}
    try:
        while time.time() < deadline and not ledger.get("halted"):
            if settings.live and _entry_paused(ledger):
                break
            ticks += 1
            _refresh_crypto_books(markets, client)
            scores = {
                mid: v for mid, v in score_crypto_windows(markets, settings).items() if v.get("edge_type") == "twap_lock"
            }
            now = time.time()
            scores = {mid: v for mid, v in scores.items() if mid not in held and now - last_try.get(mid, 0.0) >= 1.0}
            if scores:
                cands = [
                    row
                    for row in attach_sizing(build_candidates(markets, settings, scores), float(ledger["cash"]), settings)
                    if row.get("kind") == "crypto_lag"
                ]
                for cand in cands:
                    last_try[cand["id"]] = now
                if cands:
                    _execute_candidate_batch(cands, ledger, settings, held=held, taken=taken, vetoes=vetoes, errors=errors)
                # locks with no offer inside the band produce no candidate at all: rest instead
                by_id = {m["id"]: m for m in markets}
                with_cand = {c["id"] for c in cands}
                for mid, score in scores.items():
                    if mid in with_cand or mid in held or mid in by_id and _entry_paused(ledger):
                        continue
                    if mid in by_id:
                        _rest_on_no_offer(by_id[mid], score, ledger, settings, held=held, vetoes=vetoes, errors=errors)
            time.sleep(0.3)
    finally:
        crypto_lag_module.VERBOSE = was_verbose
        if owned:
            try:
                client.close()
            except Exception:
                pass
    return ticks


def cycle(settings: Settings | None = None, use_grok: bool = True) -> dict[str, Any]:
    global _CYCLE_NO
    settings = settings or settings_from_env()
    _CYCLE_NO += 1
    crypto_lag_module.VERBOSE = _verbose_cycle()
    ledger = load_ledger(settings)
    if settings.live and not ledger.get("clob_synced"):
        from .execute import probe_live

        info = probe_live(settings)
        cash = round(float(info["collateral_pUSD"]), 4)
        ledger["mode"] = "live"
        ledger["cash"] = cash
        ledger["starting_bankroll"] = cash
        ledger["positions"] = []
        ledger["orders"] = []
        ledger["clob_synced"] = True
        print(f"live bankroll synced from CLOB pUSD={cash:.2f}")
    universe = collect_universe(settings)
    by_id = {m["id"]: m for m in universe}
    hydrate_open_markets(ledger, by_id, settings)

    posted_fills = _working(ledger, by_id, settings)
    exits = _exits(ledger, by_id, settings)
    if settings.live:
        try:
            from .execute import probe_live

            clob = round(float(probe_live(settings)["collateral_pUSD"]), 4)
            ledger["cash"] = clob
        except Exception as exc:
            print("clob sync failed:", type(exc).__name__, str(exc)[:120])
    equity = mark_to_market(ledger, by_id)
    maybe_halt(ledger, equity, settings)
    _apply_risk_state(ledger, equity, settings)

    scores: dict[str, dict[str, Any]] = {}
    grok_score_count = 0
    taken_early: list[dict[str, Any]] = []
    crypto_scores: dict[str, dict[str, Any]] = {}
    if not ledger.get("halted"):
        crypto_mkts = [m for m in universe if is_crypto_updown(m)]
        refreshed = _refresh_crypto_books(crypto_mkts)
        taken_early = hunt_tape(crypto_mkts, ledger, settings)
        crypto_scores = score_crypto_windows(crypto_mkts, settings)
        scores.update(crypto_scores)
        lock_ids = [mid for mid, v in crypto_scores.items() if v.get("edge_type") == "twap_lock"]
        if _verbose_cycle() or lock_ids:
            print(
                f"crypto 5m/15m {len(crypto_mkts)} clob_fresh={refreshed} fav_hits="
                f"{sum(1 for v in crypto_scores.values() if v.get('edge_type')=='crypto_lag')} "
                f"locks={len(lock_ids)}"
            )
        for mid in lock_ids:
            # One line per lock so "why no trade" is answerable from the log: the favourite's
            # ask must sit inside the TWAP_LOCK band for a candidate to exist at all.
            m = by_id.get(mid) or {}
            v = crypto_scores[mid]
            print(
                f"  lock {v.get('asset', '?')} left={v.get('seconds_left_at_signal', 0):.0f}s p_up={v.get('p_yes', 0):.3f} "
                f"yes_ask={m.get('yes_ask')} no_ask={m.get('no_ask')}"
            )

    # Short-window signals execute before any research work. A Grok request can
    # take minutes; it is deliberately never on the crypto order's critical path.
    taken: list[dict[str, Any]] = list(taken_early) if not ledger.get("halted") else []
    vetoes: list[dict[str, str]] = []
    errors: list[str] = []
    held = occupied_ids(ledger)
    crypto_candidates: list[dict[str, Any]] = []
    if not ledger.get("halted") and crypto_scores:
        crypto_candidates = [
            row
            for row in attach_sizing(
                build_candidates(universe, settings, crypto_scores),
                float(ledger["cash"]),
                settings,
            )
            if row.get("kind") == "crypto_lag"
        ]
        _execute_candidate_batch(
            crypto_candidates,
            ledger,
            settings,
            held=held,
            taken=taken,
            vetoes=vetoes,
            errors=errors,
        )
    if not ledger.get("halted") and lock_hunt_seconds() > 0:
        hunt_ticks = hunt_locks(
            [m for m in universe if is_crypto_updown(m)], ledger, settings,
            held=held, taken=taken, vetoes=vetoes, errors=errors,
        )
        if hunt_ticks and (_verbose_cycle() or taken or errors):
            print(f"  lock hunt: {hunt_ticks} ticks")

    # Consume a completed background research job, then optionally launch the
    # next one. Neither operation waits for xAI or the slow research universe.
    grok_shortlist: list[dict[str, Any]] = []
    grok_result = _take_grok_result() if use_grok else None
    if grok_result:
        if grok_result.get("error"):
            print(f"grok worker error: {grok_result['error']}")
        grok_shortlist = list(grok_result.get("shortlist") or [])
        grok_scores = dict(grok_result.get("scores") or {})
        scores.update(grok_scores)
        grok_score_count = len(grok_shortlist)
        print(
            f"grok ready {grok_score_count} / eligible {grok_result.get('pool_count', 0)} "
            f"holt={grok_result.get('confirmed_count', 0)} kalshi={grok_result.get('kalshi_count', 0)}"
        )
        for market in grok_shortlist:
            if market["id"] not in by_id:
                universe.append(market)
                by_id[market["id"]] = market
        journal(
            {
                "ts": time.time(),
                "event": "grok_scores",
                "n": grok_score_count,
                "ids": list(grok_scores),
                "latency_s": round(
                    float(grok_result.get("completed_at") or time.time())
                    - float(grok_result.get("started_at") or time.time()),
                    3,
                ),
            }
        )

    skip_grok = grok_skip_reason(ledger, scores, settings, use_grok=use_grok)
    if skip_grok is None and not _grok_worker_running():
        if _start_grok_worker(settings, ledger):
            ledger["last_grok_ts"] = time.time()
            print("grok research started in background")
    elif use_grok:
        status = "working" if _grok_worker_running() else skip_grok
        if status and status != "off":
            print(f"grok idle ({status})")

    cash = float(ledger["cash"])
    late_candidates = [
        row
        for row in attach_sizing(build_candidates(universe, settings, scores), cash, settings)
        if row.get("kind") != "crypto_lag" and not (settings.live and row.get("side") == "BOTH")
    ]
    candidates = crypto_candidates + late_candidates
    if grok_shortlist:
        flagged_ids = {c["id"] for c in candidates}
        for row in grok_shortlist:
            if row["id"] in flagged_ids:
                continue
            scored = scores.get(row["id"]) or {}
            why = scored.get("edge_type") or "no_score"
            if scored.get("already_priced"):
                why = "already_priced"
            elif float(scored.get("confidence") or 0) < settings.min_confidence:
                why = f"low_conf={scored.get('confidence')}"
            print(f"  grok miss {why}  {row['question'][:64]}")
    if not ledger.get("halted"):
        _execute_candidate_batch(
            late_candidates,
            ledger,
            settings,
            held=held,
            taken=taken,
            vetoes=vetoes,
            errors=errors,
        )

    equity = mark_to_market(ledger, by_id)
    maybe_halt(ledger, equity, settings)
    _apply_risk_state(ledger, equity, settings)
    save_ledger(ledger)
    report = {
        "scanned": len(universe),
        "flagged": len(candidates),
        "grok_scored": grok_score_count,
        "fills": [row for row in posted_fills if row.get("side")],
        "posts": taken,
        "cancels": [row for row in posted_fills if row.get("cancelled")],
        "exits": exits,
        "vetoes": vetoes[:20],
        "errors": errors,
        "halted": ledger.get("halted"),
        "halt_reason": ledger.get("halt_reason", ""),
        "cash": ledger["cash"],
        "equity": equity,
        "positions": len(ledger["positions"]),
        "working": len(ledger.get("orders") or []),
        "mode": "live" if settings.live else "paper",
        "candidates": candidates[:25],
        "universe_sample": universe[:5],
    }
    SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCAN_PATH.write_text(json.dumps(report, indent=2) + "\n")
    if _verbose_cycle() or _cycle_has_activity(report):
        journal(
            {
                "ts": time.time(),
                "event": "cycle",
                **{k: report[k] for k in ("scanned", "flagged", "exits", "equity", "mode", "working")},
                "fills": len(report["fills"]),
                "posts": len(taken),
                "vetoes": len(vetoes),
            }
        )
    return report


def _working(ledger: dict[str, Any], by_id: dict[str, dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    filled: list[dict[str, Any]] = []
    mode = "live" if settings.live else "paper"
    for order in list(ledger.get("orders") or []):
        if settings.live:
            if order.get("mode") == "live" and order.get("venue_id"):
                filled.extend(_working_live(ledger, order, settings))
            continue
        market = by_id.get(order["market_id"])
        if not market:
            continue
        if not settings.live and paper_maker_fillable(order, market):
            filled.append(fill_order(ledger, order, fill_price=float(order["price"]), mode=mode, settings=settings))
            continue
        ask = float(market.get("yes_ask") if order["side"] == "YES" else market.get("no_ask") or 0)
        if ask and ask > float(order["price"]) + 0.05:
            filled.append(cancel_order(ledger, order, reason="price_away"))
    return filled


_VENUE_TEARDOWN_S = 600.0  # the CLOB removes a closed window's book within minutes; after this nothing can fill


def _working_live(ledger: dict[str, Any], order: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    """Poll one resting live order: book what matched (always — the venue already did),
    cancel what the window outlived, and never forget an order whose cancel failed."""
    win = parse_window(order)
    now = time.time()
    ended = bool(win and now >= float(win["end"]))
    long_gone = bool(win and now >= float(win["end"]) + _VENUE_TEARDOWN_S)
    try:
        status, matched, px = live_order_state(str(order["venue_id"]), settings)
    except LiveDisabled as exc:
        text = str(exc).lower()
        if long_gone and ("404" in text or "not found" in text or "no orderbook" in text):
            out = [cancel_order(ledger, order, reason="venue_closed")]
            save_ledger(ledger)
            return out
        print("order status:", str(exc)[:120])
        return []
    shares = float(order["shares"])
    done = status.upper() in {"MATCHED", "FILLED"} or matched >= shares - 1e-6
    if matched > 0 and (done or ended):
        if matched < shares - 1e-6:
            unfilled = shares - matched
            refund = round(unfilled * float(order["price"]), 4)
            ledger["cash"] = round(float(ledger["cash"]) + refund, 4)
            order["shares"] = round(matched, 4)
            order["stake"] = round(float(order["stake"]) - refund, 4)
        fill = fill_order(ledger, order, fill_price=px or float(order["price"]), mode="live", settings=settings,
                          raw={"order_id": order["venue_id"], "status": status, "size_matched": matched})
        save_ledger(ledger)
        return [fill]
    if status.upper() in {"CANCELED", "CANCELLED", "EXPIRED"}:
        out = [cancel_order(ledger, order, reason=status.lower())]
        save_ledger(ledger)
        return out
    if ended:
        try:
            live_cancel(str(order["venue_id"]), settings)
        except LiveDisabled as exc:
            order["cancel_failed_at"] = now
            order["cancel_error"] = str(exc)[:120]
            if not long_gone:
                return []  # keep tracking it; retry next cycle rather than orphan a live order
        out = [cancel_order(ledger, order, reason="window_end")]
        save_ledger(ledger)
        return out
    return []


def _lock_exposure(ledger: dict[str, Any]) -> float:
    """Dollars currently at risk in lock trades: open positions plus resting orders."""
    total = 0.0
    for pos in ledger.get("positions") or []:
        if pos.get("edge_type") == "twap_lock":
            total += float(pos.get("stake") or 0)
    for order in ledger.get("orders") or []:
        if order.get("edge_type") == "twap_lock":
            total += float(order.get("stake") or 0)
    return total


def lock_max_exposure_frac() -> float:
    """LOCK_MAX_EXPOSURE_FRAC: cap on total lock dollars at risk as a fraction of cash (all
    assets together — seven windows ending on the same second are one bet, not seven).
    Default 1.0 = no cap beyond the per-trade sizer."""
    try:
        return max(0.0, min(1.0, float(os.getenv("LOCK_MAX_EXPOSURE_FRAC") or 1.0)))
    except ValueError:
        return 1.0


def _cap_lock_stake(cand: dict[str, Any], ledger: dict[str, Any], settings: Settings) -> str | None:
    """Shrink a lock candidate's stake to the remaining exposure budget; return a veto reason if none is left."""
    if cand.get("edge_type") != "twap_lock":
        return None
    frac = lock_max_exposure_frac()
    if frac >= 1.0:
        return None
    allowed = frac * float(ledger.get("cash") or 0) - _lock_exposure(ledger)
    if allowed < settings.min_crypto_stake - 1e-9:
        return "lock_exposure"
    if float(cand.get("stake") or 0) > allowed:
        cand["stake"] = round(allowed, 2)
        price = float(cand.get("price") or 0)
        if price > 0:
            cand["shares"] = round(cand["stake"] / price, 4)
    return None


def _entry_paused(ledger: dict[str, Any]) -> bool:
    return time.time() < float(ledger.get("entry_pause_until") or 0)


_REST_BACKOFF: dict[str, float] = {}  # market id -> do not retry a resting bid before this time


def _rest_on_no_offer(
    market: dict[str, Any],
    score: dict[str, Any],
    ledger: dict[str, Any],
    settings: Settings,
    *,
    held: set[str],
    vetoes: list[dict[str, str]],
    errors: list[str],
) -> bool:
    """The normal endgame: the lock is decisive and nobody offers the favourite inside the band
    (99 % of live lock observations). The taker path can never fire there, so with
    LOCK_REST_AT_CAP rest the small post-only bid directly, at the same ceiling, provided the
    book agrees with the arithmetic (favourite bid at or above the band floor)."""
    from .execute import rest_at_cap_enabled
    from .twap_lock import ASK_CAP, ASK_FLOOR

    if not (settings.live and rest_at_cap_enabled()) or market["id"] in held:
        return False
    if any(o.get("market_id") == market["id"] for o in ledger.get("orders") or []):
        return False
    p = float(score.get("p_yes") or 0)
    side = "YES" if p >= 0.5 else "NO"
    fair = p if side == "YES" else 1.0 - p
    ask = float(market.get("yes_ask" if side == "YES" else "no_ask") or 0)
    bid = float(market.get("yes_bid" if side == "YES" else "no_bid") or 0)
    if 0 < ask <= ASK_CAP:
        return False  # a real offer exists; the taker path handles it
    if bid < ASK_FLOOR:
        return False  # the book disagrees with the arithmetic: never rest into a disagreement
    cash = float(ledger.get("cash") or 0)
    stake = round(min(settings.max_crypto_stake, cash * settings.crypto_bankroll_frac), 2)
    cand = dict(
        market,
        kind="crypto_lag",
        edge_type="twap_lock",
        side=side,
        price=ASK_CAP,          # the bid we intend to rest; the band check and _rest_price see this
        limit_price=ASK_CAP,
        fair=fair,
        edge=round(fair - ASK_CAP, 4),
        stake=stake,
        shares=round(stake / ASK_CAP, 4),
        signal_ts=float(score.get("signal_ts") or time.time()),
        asset=score.get("asset") or market.get("asset") or "",
        window_start=score.get("window_start"),
        window_end=score.get("window_end"),
        thesis=score.get("thesis") or "",
        taker=False,
    )
    if time.time() < _REST_BACKOFF.get(market["id"], 0.0):
        return False  # a recent attempt on this market failed; do not retry every 0.3 s tick
    reason = veto(cand, ledger, settings, occupied=held) or _cap_lock_stake(cand, ledger, settings)
    common = {"id": cand["id"], "kind": "crypto_lag", "side": side, "price": ASK_CAP, "fair": fair, "edge": cand["edge"],
              "signal_ts": cand["signal_ts"], "window_end": cand.get("window_end"), "thesis": cand["thesis"]}
    if reason:
        _REST_BACKOFF[market["id"]] = time.time() + 30.0
        vetoes.append({"id": cand["id"], "reason": reason, "kind": "crypto_lag"})
        journal({"ts": time.time(), "event": "veto", "reason": reason, **common})
        return False
    # the exposure cap can leave less than the venue minimum (5 shares): skip quietly, once
    rest_cap = _f_env("LOCK_REST_MAX_USD", 5.0)
    min_shares = max(float(market.get("min_order_size") or 0), 5.0)
    if min(float(cand["stake"]), rest_cap) / ASK_CAP < min_shares - 1e-9:
        _REST_BACKOFF[market["id"]] = time.time() + 60.0
        journal({"ts": time.time(), "event": "rest_skip", "reason": f"stake {min(float(cand['stake']), rest_cap):.2f} below {min_shares:g} shares at {ASK_CAP}", **common})
        return False
    try:
        raw = live_rest_buy(cand, settings)
    except (LiveDisabled, RuntimeError, ValueError) as exc:
        _REST_BACKOFF[market["id"]] = time.time() + 60.0
        errors.append(f"{cand['id']}: rest: {exc}")
        journal({"ts": time.time(), "event": "rest_reject", "reason": str(exc), **common})
        return False
    order = record_order(
        ledger,
        market=cand,
        side=side,
        stake=round(float(raw["rest_size"]) * float(raw["rest_price"]), 4),
        price=float(raw["rest_price"]),
        shares=float(raw["rest_size"]),
        reason=f"lock rest (no offer) edge={cand['edge']}",
        mode="live",
        settings=settings,
        raw=raw,
    )
    held.add(cand["id"])
    save_ledger(ledger)
    print(f"  REST {side:4} {order['shares']} sh @ {order['price']} (no offer)  {str(market.get('question'))[:60]}")
    journal({"ts": time.time(), "event": "post", "price": order["price"], "shares": order["shares"], "venue_id": order.get("venue_id"), "no_offer": True, **common})
    return True


def _exits(ledger: dict[str, Any], by_id: dict[str, dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    mode = "live" if settings.live else "paper"
    for pos in list(ledger["positions"]):
        market = by_id.get(pos["market_id"])
        px = None
        reason = None
        crypto = is_crypto_updown(pos) or is_crypto_updown(market or {})
        window = parse_window(pos) if crypto else None
        if crypto and pos["side"] == "BOTH":
            window_over = bool(window and int(time.time()) >= int(window["end"]) + 5)
            if window_over:
                px, reason = 1.0, "complete_set"
        elif crypto:
            settled = crypto_settle_price(
                pos,
                user_agent=settings.user_agent,
                prefer_gamma=True,
                allow_proxy=not settings.live,
            )
            if settled is not None:
                px, reason = settled, "window_end"
            # Once the window has ended, its CLOB can disappear before Gamma
            # publishes resolution. Never attempt a stop/take sale on that book.
            elif window and int(time.time()) >= int(window["end"]):
                continue
        if px is None and pos["side"] in {"YES", "NO"} and not crypto:
            mark = mark_price(pos, market) if market else float(pos["entry_price"])
            entry = float(pos["entry_price"])
            dumped = grok_dump(pos, mark, settings)
            if dumped:
                px, reason = dumped
            elif market and entry >= 0.25 and mark <= entry * (1.0 - settings.stop_loss_pct):
                px, reason = mark, "stop"
            elif market and mark >= settings.take_profit_price:
                px, reason = mark, "take"
            elif survival_mode(ledger) and "grok" in str(pos.get("reason") or "").lower():
                days = (market or {}).get("days_to_end")
                sport_now = is_same_day_sport({**pos, "days_to_end": days if days is not None else 99})
                if not sport_now:
                    px, reason = mark, "survival_dump"
        if px is None or reason is None:
            continue
        try:
            if settings.live:
                if reason in {"window_end", "complete_set"} and px > 0:
                    # The venue may auto-credit resolved shares before our next
                    # cycle. Redeem only if the conditional balance still exists.
                    if live_position_balance(pos, settings) > 0:
                        live_redeem(pos, settings)
                elif reason not in {"window_end", "complete_set"}:
                    live_sell(pos, px, settings)
            closed.append(
                close_position(
                    ledger,
                    pos,
                    price=px,
                    reason=reason,
                    mode=mode,
                    # Live proceeds are reconciled from CLOB collateral below.
                    credit_cash=not settings.live,
                )
            )
        except LiveDisabled as exc:
            closed.append({"skipped": str(exc), "market_id": pos["market_id"]})
    return closed


def run_loop(settings: Settings | None = None, *, once: bool = False, use_grok: bool = True) -> None:
    settings = settings or settings_from_env()
    start_chainlink_stream()
    if settings.live:
        print(
            "tape: 15% / 3s DipArb + complete-set (YES+NO < 95c after fees, depth filtered). "
            f"xAI {'ON' if use_grok else 'OFF'}. Halt ${settings.max_session_drawdown:.0f} DD."
        )
        try:
            from .smart_money import leaderboard_wallets

            whales = leaderboard_wallets(5)
            for row in whales[:5]:
                print(
                    f"  smart {str(row.get('proxy') or '')[:10]} pnl={row['pnl']:.0f} "
                    f"wr={row['win_rate']:.0%}"
                )
        except Exception as exc:
            print("smart-money leaderboard skip:", type(exc).__name__)
    errors = 0
    while True:
        try:
            report = cycle(settings, use_grok=use_grok)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if once:
                raise
            errors += 1
            wait = min(120, settings.loop_seconds * min(errors, 6))
            print(f"cycle error #{errors}: {type(exc).__name__}: {str(exc)[:160]} — retry in {wait}s")
            time.sleep(wait)
            continue
        errors = 0
        if once or _verbose_cycle() or _cycle_has_activity(report):
            print_cycle(report)
        if once:
            return
        if report.get("halted"):
            if settings.live:
                print("halted — leaving the loop. inspect data/ledger_live.json before restarting.")
                return
            if not _paper_halt_wait(settings):
                print("halted — campaign target reached. leaving the loop.")
                return
        time.sleep(settings.loop_seconds)


def _paper_halt_wait(settings: Settings) -> bool:
    """Paper only: hold through a protective halt, then start a fresh risk
    session after a cooldown. Returns False only for the campaign-target halt
    (that one is a win, not a protection — a human decides what's next).
    Exits keep settling every cycle while we wait; only entries are blocked."""
    from .ledger import utc_now

    ledger = load_ledger(settings)
    reason = str(ledger.get("halt_reason") or "")
    if reason.startswith("campaign target"):
        return False
    now = time.time()
    halted_at = float(ledger.get("halted_at") or 0)
    cooldown = 60.0 * float(_f_env("PAPER_HALT_RESET_MIN", 30.0))
    if halted_at <= 0:
        ledger["halted_at"] = now
        save_ledger(ledger)
        print(f"halt armed ({reason}) — paper auto-reset in {cooldown / 60:.0f}m")
        return True
    if now - halted_at < cooldown:
        return True
    cash = float(ledger.get("cash") or 0)
    ledger.update(
        {
            "halted": False,
            "halt_reason": "",
            "halted_at": 0,
            "risk_baseline_cash": cash,
            "realized_peak_cash": cash,
            "locked_equity_floor": cash,
            "session_started_at": utc_now(),
            "crypto_loss_streak": 0,
        }
    )
    save_ledger(ledger)
    print(f"paper auto-reset after halt ({reason}) — new session baseline ${cash:.2f}")
    return True


def _f_env(name: str, default: float) -> float:
    import os

    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def print_cycle(report: dict[str, Any]) -> None:
    print(
        f"{report['mode']} scanned={report['scanned']} flagged={report['flagged']} "
        f"grok={report['grok_scored']} posts={len(report.get('posts') or [])} "
        f"fills={len(report['fills'])} vetoes={len(report.get('vetoes') or [])} "
        f"working={report.get('working', 0)} exits={len(report['exits'])} "
        f"cash={report['cash']:.2f} equity={report['equity']:.2f} pos={report['positions']} "
        f"halted={report['halted']}"
    )
    if report["halt_reason"]:
        print("halt:", report["halt_reason"])
    for err in report["errors"]:
        print("err:", err)
    for fill in report["fills"]:
        print(f"  FILL {fill['side']:4} ${fill['stake']:.2f} @ {fill['price']}  {fill['question'][:80]}")
    for post in report.get("posts") or []:
        print(f"  POST {post['side']:4} ${post['stake']:.2f} @ {post['price']}  {post['question'][:80]}")
    for veto_row in (report.get("vetoes") or [])[:5]:
        print(f"  veto {veto_row['kind']} {veto_row['reason']} {veto_row['id']}")
    if not report["fills"] and not report.get("posts") and report["candidates"]:
        top = report["candidates"][0]
        print(f"  top unfilled {top['kind']} {top['side']} edge={top['edge']:.3f} stake={top['stake']}")
    elif report.get("positions"):
        print(
            f"  holding {report['positions']}  cash={report['cash']:.2f} equity={report['equity']:.2f} "
            f"— hunting 5m/15m tape (dip + complete-set)"
        )
    elif not report["candidates"]:
        print(
            f"  no trade this cycle  (cash={report['cash']:.2f} pos={report['positions']} "
            f"— if cash is 0 the book is full, not 'no edge')"
        )
