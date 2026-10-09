"""Statistics shared by the r4_earnings_seeded_book_48h_forward backtest, forward summary and self-tests (stdlib only).

Maker fills: an order i sells YES at s_i (= buys NO at q_i = 1 - s_i), f_i contracts filled (f_i = min(N_i, qualifying
print contracts)). Stake w_i = f_i q_i dollars, pnl_i = f_i (1 - q_i) if NO else -f_i q_i (no maker fee: the
KXEARNINGSMENTION* series are fee_type 'quadratic').
  size_weighted(rows)   R = sum pnl / sum w, cluster-robust (by event) ratio t, R without the 3 best fills, halves
                        by the event's first close, median filled contracts
  equal_dollar(rows)    each filled order counts once (ret = pnl / w), lab.kalshi.calib.cell_stats (t of event means)
  binom_lb(rows)        one-sided 95% exact (Clopper-Pearson / Beta) lower bound on the win rate over UNIQUE events
                        (first filled order of each event) and the per-$ return it implies at the mean price
  replay_cap(orders, cap)  deterministic capped book: orders in post order (ties: earlier listing, then ticker) take a
                        slot if fewer than cap are occupied; an order holds its slot until its cancel if it never
                        filled, else until its event settles"""
from __future__ import annotations
import math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import cell_stats


def size_weighted(rows: list[dict]) -> dict:
    """rows: dicts with e, f (filled contracts > 0), q (NO price), won (NO won), tclose (event first close)."""
    rows = [r for r in rows if r["f"] > 0]
    if not rows:
        return {"n": 0}
    for r in rows:
        r["w"] = r["f"] * r["q"]
        r["pnl"] = r["f"] * (1 - r["q"]) if r["won"] else -r["f"] * r["q"]
    W = sum(r["w"] for r in rows); R = sum(r["pnl"] for r in rows) / W
    g = defaultdict(float)
    for r in rows:
        g[r["e"]] += r["pnl"] - R * r["w"]
    G = len(g)
    se = math.sqrt(G / (G - 1) * sum(v * v for v in g.values())) / W if G > 1 else float("nan")
    t = R / se if se and se > 0 else float("nan")
    best = sorted(rows, key=lambda r: -r["pnl"])
    rest = best[3:]
    wo3 = sum(r["pnl"] for r in rest) / sum(r["w"] for r in rest) if rest else float("nan")
    mid = sorted(r["tclose"] for r in rows)[len(rows) // 2]
    h = []
    for sel in ([r for r in rows if r["tclose"] < mid], [r for r in rows if r["tclose"] >= mid]):
        h.append(round(sum(r["pnl"] for r in sel) / sum(r["w"] for r in sel), 4) if sel else None)
    return {"n": len(rows), "events": G, "win": round(st.mean(r["won"] for r in rows), 4),
            "avg_px": round(sum(r["q"] * r["f"] for r in rows) / sum(r["f"] for r in rows), 4),
            "ret_per_dollar": round(R, 4), "t": round(t, 2), "ret_wo3": round(wo3, 4), "half1": h[0], "half2": h[1],
            "staked": round(W, 2), "pnl": round(sum(r["pnl"] for r in rows), 2),
            "median_filled_contracts": st.median(r["f"] for r in rows)}


def equal_dollar(rows: list[dict]) -> dict:
    rows = [r for r in rows if r["f"] > 0]
    if not rows:
        return {"n": 0}
    x = [{"e": r["e"], "ret": ((1 - r["q"]) / r["q"]) if r["won"] else -1.0, "won": r["won"], "px": r["q"]} for r in rows]
    v = cell_stats(x)
    return {k: (round(v[k], 4) if isinstance(v[k], float) else v[k]) for k in ("n", "events", "win", "px", "ret", "t", "ret_wo3")}


def _binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Bin(n, p)."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided (1 - alpha) Clopper-Pearson lower bound = Beta(alpha; k, n - k + 1) quantile."""
    if k == 0 or n == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(80):
        m = (lo + hi) / 2
        if _binom_sf(k, n, m) < alpha:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def binom_lb(rows: list[dict], order_key: str = "t_fill") -> dict:
    """Gate amendment (d): exact one-sided 95% lower bound on the per-$ return over unique events."""
    first = {}
    for r in sorted((r for r in rows if r["f"] > 0), key=lambda r: (r.get(order_key) or 0, r.get("t", ""))):
        first.setdefault(r["e"], r)
    fm = list(first.values())
    if not fm:
        return {"events": 0}
    k = sum(r["won"] for r in fm); n = len(fm); q = st.mean(r["q"] for r in fm)
    lo = cp_lower(k, n)
    return {"events": n, "wins": k, "win": round(k / n, 4), "avg_px": round(q, 4), "win_lo95": round(lo, 4),
            "ret_at_lo95": round((lo - q) / q, 4), "ret_point": round((k / n - q) / q, 4), "lb_above_0": (lo - q) / q > 0}


def replay_cap(orders: list[dict], cap: int) -> list[dict]:
    """orders: dicts with post, open, t, cancel (arm's cancel), t_fill (first qualifying print time or None),
    settle (event settlement/first-close time). Returns the subset a book of `cap` concurrent slots would hold."""
    taken = []
    for o in sorted(orders, key=lambda o: (o["post"], o.get("open", 0), o["t"])):
        busy = 0
        for x in taken:
            filled_by = x["t_fill"] is not None and x["t_fill"] <= x["cancel"]
            release = x["settle"] if filled_by else x["cancel"]
            if x["post"] <= o["post"] < release:
                busy += 1
        if busy < cap:
            taken.append(o)
    return taken


def profile_sim(orders: list[dict], p_hat: float, bank0: float = 50.0, cap: int = 3, max_stake: float = 5.0,
                halt_streak: int = 3, day_loss: float = 10.0, cum_loss: float = 15.0) -> dict:
    """Owner's live profile on a list of arm-C orders (each with post, open, t, cancel, t_fill, settle, q, N_through,
    won): at most `cap` open orders/positions; stake = min(quarter-Kelly on p_hat shrunk 50% toward the price, $5) of
    the current bankroll; halt after `halt_streak` losses in a row (settlement order), a $day_loss loss in one UTC day
    or a $cum_loss cumulative loss. The simulation stops at the first halt (the owner reviews before restarting) and
    also reports how often the 3-loss halt would fire if every halt were followed by an immediate restart."""
    bank = bank0; taken = []; events = []
    for o in sorted(orders, key=lambda o: (o["post"], o.get("open", 0), o["t"])):
        busy = 0
        for x in taken:
            filled_by = x["t_fill"] is not None and x["t_fill"] <= x["cancel"]
            if x["post"] <= o["post"] < (x["settle"] if filled_by else x["cancel"]):
                busy += 1
        if busy >= cap:
            continue
        q = o["q"]; ps = 0.5 * p_hat + 0.5 * q
        f = max(0.0, (ps - q) / (1 - q)) * 0.25
        stake = min(f * bank, max_stake)
        n = int(stake // q) if q > 0 else 0
        if n < 1:
            continue
        x = dict(o); x["n"] = n; taken.append(x)
    fills = []
    for x in taken:
        k = min(x["n"], x["N_through"])
        if k > 0 and x["t_fill"] is not None:
            pnl = k * (1 - x["q"]) if x["won"] else -k * x["q"]
            fills.append((x["settle"], pnl, x["won"], x["t"], k, x["q"]))
    fills.sort()
    streak = 0; halts = 0; first_halt = None; cum = 0.0; day = defaultdict(float); stopped_pnl = None
    for i, (ts_, pnl, won, t, k, q) in enumerate(fills):
        cum += pnl; day[int(ts_ // 86400)] += pnl
        streak = 0 if won else streak + 1
        reason = None
        if streak >= halt_streak:
            reason = "3_losses_in_a_row"; streak = 0
        elif day[int(ts_ // 86400)] <= -day_loss:
            reason = "daily_loss"
        elif cum <= -cum_loss:
            reason = "cumulative_loss"
        if reason:
            halts += 1
            if first_halt is None:
                first_halt = {"after_fills": i + 1, "reason": reason, "pnl_at_halt": round(cum, 2), "ts": ts_}
                stopped_pnl = round(cum, 2)
    return {"orders_taken": len(taken), "fills": len(fills), "pnl_no_halt": round(sum(f[1] for f in fills), 2),
            "staked": round(sum(f[4] * f[5] for f in fills), 2), "halts_if_restarted": halts, "first_halt": first_halt,
            "pnl_until_first_halt": stopped_pnl if stopped_pnl is not None else round(sum(f[1] for f in fills), 2)}
