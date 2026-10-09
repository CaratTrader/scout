"""Shared, frozen definitions for r4_long_dated_archive_census (backtest census, forward logger, summaries).

Scheduled deadline S (leak-free: only text fixed at listing): rules_primary "before <date>" -> that date 00:00 UTC;
"by/on/through/until <date>" -> next day 00:00 UTC; "before YYYY" -> Jan 1 YYYY; else a DDMONYY ticker segment -> next
day; else "in/during YYYY" -> Jan 1 of YYYY+1; else close_time when the market cannot close early; else none.
(Same rule as the round-3 adversarial reproducer, r3_long_dated_longshot_no_repro.deadline, re-implemented.)
Eligible market: binary, settled yes/no, S - open >= 45 days, not a multivariate combo.

Cells (pre-registered in data/kalshi_lab/strategies/r4_long_dated_archive_census/census_preregistration.json):
  C1: NO, signal price in [0.80, 0.92), D in {60, 30, 14}; one row per (market, D)    (frozen round-3 C1)
  B : NO, signal price in [0.70, 0.97), any D in {60, 45, 30, 21, 14}; first qualifying entry per market
  M : NO, signal price in [0.50, 0.70), any D; first qualifying entry per market         (flagged: seen in round 3)
Clock: t = S - D days; signal = last hourly candle with end <= t (age <= 72 h); fill = taker 1 - yes_bid of the last
candle <= t + 1 h (age <= 72 h); the market must be open at t and its close later than the fill.
Fee: Kalshi taker 0.07 * multiplier * p * (1 - p) per contract, rounded up to the cent on a $5 order."""
from __future__ import annotations

import calendar
import hashlib
import math
import re
import statistics as st
import time
from collections import defaultdict

DAY = 86400
DGRID = (60, 45, 30, 21, 14)
MAXAGE_H = 72.0
STAKE = 5.0
CELLS = {
    "C1": {"side": "NO", "lo": 0.80, "hi": 0.92, "D": (60, 30, 14), "first_entry": False},
    "B": {"side": "NO", "lo": 0.70, "hi": 0.97, "D": DGRID, "first_entry": True},
    "M": {"side": "NO", "lo": 0.50, "hi": 0.70, "D": DGRID, "first_entry": True},
}
MACRO_CATS = ("Economics", "Financials", "Crypto", "Commodities")
MACRO_TOKENS = ("FED", "GDP", "RATECUT", "RECSS", "CPI", "PCE", "JOBS", "PAYROLL", "UNEMP", "INFL", "TREAS", "INX", "NASDAQ",
                "BTC", "ETH", "OIL", "WTI", "GOLD", "POWELL")

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
MONTHS["sept"] = 9
DATE_RX = re.compile(r"\b(before|by|on|through|until)\s+(?:the end of\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
YEAR_BEFORE = re.compile(r"\bbefore\s+(\d{4})\b", re.I)
YEAR_IN = re.compile(r"\b(?:in|during)\s+(\d{4})\b", re.I)
TICK_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")


def ts(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "")[:19]
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def day0(y: int, m: int, d: int) -> int:
    return calendar.timegm((y, m, d, 0, 0, 0))


def deadline(m: dict) -> tuple[int | None, str]:
    rules = m.get("rules_primary") or ""
    x = DATE_RX.search(rules)
    if x and x.group(2).lower() in MONTHS:
        try:
            base = day0(int(x.group(4)), MONTHS[x.group(2).lower()], int(x.group(3)))
        except Exception:
            base = None
        if base:
            return (base if x.group(1).lower() == "before" else base + DAY), "rules:" + x.group(1).lower()
    x = YEAR_BEFORE.search(rules)
    if x:
        return day0(int(x.group(1)), 1, 1), "rules:before-year"
    for seg in reversed(m["ticker"].split("-")):
        x = TICK_DATE.match(seg)
        if x and x.group(2).lower() in MONTHS:
            try:
                return day0(2000 + int(x.group(1)), MONTHS[x.group(2).lower()], int(x.group(3))) + DAY, "ticker"
            except Exception:
                pass
    x = YEAR_IN.search(rules)
    if x:
        return day0(int(x.group(1)) + 1, 1, 1), "rules:in-year"
    if not m.get("can_close_early"):
        return ts(m.get("close_time")), "close(no-early)"
    return None, "none"


def series_of(m: dict) -> str:
    return m["event_ticker"].split("-")[0]


def is_macro(series: str, cat: str | None) -> bool:
    s = series.removeprefix("KX")
    return (cat in MACRO_CATS) or any(tok in s for tok in MACRO_TOKENS)


def eligible_row(m: dict, cat: str | None, settled_only: bool = True) -> dict | None:
    """Compact row for an eligible market, else None. Uses listing fields; result is carried for scoring only."""
    if m.get("market_type") not in (None, "binary"):
        return None
    if settled_only and m.get("result") not in ("yes", "no"):
        return None
    if m["ticker"].startswith("KXMVE") or m["event_ticker"].startswith("KXMVE"):
        return None
    S, how = deadline(m)
    op, cl = ts(m.get("open_time")), ts(m.get("close_time"))
    if S is None or op is None or cl is None or S - op < 45 * DAY:
        return None
    ser = series_of(m)
    return {"t": m["ticker"], "e": m["event_ticker"], "series": ser, "cat": cat, "open": op, "close": cl, "S": S, "S_how": how,
            "result": m.get("result"), "early": bool(m.get("can_close_early")), "settled": ts(m.get("settlement_ts")),
            "macro": is_macro(ser, cat)}


def tradeable(r: dict) -> bool:
    """Some decision time t = S - D days falls while the market is listed and before its close (listing fields + close)."""
    return any(r["S"] - D * DAY >= r["open"] and r["S"] - D * DAY + 3600 < r["close"] for D in DGRID)


def fee_pc(p: float, mult: float = 1.0) -> float:
    n = max(1, int(STAKE // max(p, 0.01)))
    return math.ceil(round(0.07 * mult * n * p * (1 - p) * 100, 9)) / 100 / n


def quote(c: list, t: int, maxage_h: float = MAXAGE_H):
    """Last candle row (ts, yes_ask, yes_bid, vol) with ts <= t and age <= maxage_h, else None."""
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > maxage_h * 3600:
        return None
    return best


def trades(markets: list[dict], C: dict, delay_h: int = 1, maxage_h: float = MAXAGE_H, fee_mult: dict | None = None) -> list[dict]:
    out = []
    for m in markets:
        c = C.get(m["t"])
        if not c:
            continue
        mult = (fee_mult or {}).get(m["series"], 1.0)
        for D in DGRID:
            t = m["S"] - D * DAY
            f = t + delay_h * 3600
            if t < m["open"] or f >= m["close"]:
                continue
            qs, qf = quote(c, t, maxage_h), quote(c, f, maxage_h)
            if not qs or not qf:
                continue
            for side, ps, px, won in (("NO", 1 - qs[2], 1 - qf[2], m["result"] == "no"), ("YES", qs[1], qf[1], m["result"] == "yes")):
                if not (0.01 <= px <= 0.99):
                    continue
                pnl = (1.0 if won else 0.0) - px - fee_pc(px, mult)
                out.append({"t": m["t"], "e": m["e"], "series": m["series"], "cat": m.get("cat"), "macro": m.get("macro"), "D": D,
                            "side": side, "ps": round(ps, 4), "px": round(px, 4), "won": won, "pnl": pnl, "ret": pnl / px,
                            "t_close": m["close"], "fill_ts": f, "S": m["S"],
                            "quarter": time.strftime("%Y", time.gmtime(f)) + "Q" + str((time.gmtime(f).tm_mon - 1) // 3 + 1)})
    return out


def select(T: list[dict], cell: str) -> list[dict]:
    c = CELLS[cell]
    x = [r for r in T if r["side"] == c["side"] and c["lo"] <= r["ps"] < c["hi"] and r["D"] in c["D"]]
    if c["first_entry"]:
        best = {}
        for r in sorted(x, key=lambda r: r["fill_ts"]):
            best.setdefault(r["t"], r)
        x = list(best.values())
    return sorted(x, key=lambda r: r["fill_ts"])


def first_per_event(rows: list[dict]) -> list[dict]:
    best = {}
    for r in sorted(rows, key=lambda r: r["fill_ts"]):
        best.setdefault(r["e"], r)
    return list(best.values())


def binom_tail_ge(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound on a binomial proportion: the p with P(X >= k | p) = alpha."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if binom_tail_ge(k, n, mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-30 else 1e-30)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(c) > 1e-30 else 1e30
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1 + aa / c if abs(c) > 1e-30 else 1e30
        de = d * c; h *= de
        if abs(de - 1) < 1e-12:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def beta_ppf(q: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clustered_t(rows: list[dict], key: str) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[r[key]].append(r["ret"])
    m = [st.mean(v) for v in g.values()]
    sd = st.pstdev(m) if len(m) > 1 else 0.0
    t = st.mean(m) / (sd / math.sqrt(len(m))) if len(m) > 2 and sd > 0 else None
    return {"clusters": len(m), "cluster_mean": round(st.mean(m), 4) if m else None, "t": round(t, 2) if t is not None else None}


def bounds_on_events(rows: list[dict]) -> dict:
    """Gate amendment (d): one trade per unique event (its first entry); exact binomial (Clopper-Pearson) and Jeffreys
    Beta one-sided 95% lower bounds on the win rate, mapped to a per-$ return at the realised prices and fees."""
    ev = first_per_event(rows)
    n = len(ev)
    if not n:
        return {"n_events": 0}
    k = sum(r["won"] for r in ev)
    inv = st.mean(1 / r["px"] for r in ev)
    # cost per $ = (px + fee) / px, and pnl = won - px - fee, so px + fee = won - pnl
    cost = st.mean((r["won"] - r["pnl"]) / r["px"] for r in ev)

    def ret_at(q: float) -> float:
        return q * inv - cost
    cp = cp_lower(k, n)
    jf = beta_ppf(0.05, k + 0.5, n - k + 0.5)
    px = st.mean(r["px"] for r in ev)
    fee = st.mean(r["won"] - r["pnl"] - r["px"] for r in ev)
    be10 = 1.10 * px + fee   # win rate needed for +10%/$ at the average price
    return {"n_events": n, "wins": k, "losses": n - k, "win": round(k / n, 4), "avg_px": round(px, 4), "ret_events": round(st.mean(r["ret"] for r in ev), 4),
            "cp_win_lo95": round(cp, 4), "ret_at_cp_lo95": round(ret_at(cp), 4), "jeffreys_win_lo95": round(jf, 4), "ret_at_jeffreys_lo95": round(ret_at(jf), 4),
            "win_needed_for_+10pct": round(be10, 4), "p_obs_or_better_if_true_+10pct": round(binom_tail_ge(k, n, min(be10, 1.0)), 4) if be10 < 1 else None,
            "win_needed_for_0pct": round(px + fee, 4), "p_obs_or_better_if_true_0pct": round(binom_tail_ge(k, n, px + fee), 4)}


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["fill_ts"])
    h = len(srt) // 2
    ev = clustered_t(rows, "e")
    return {"n": len(rows), "markets": len({r["t"] for r in rows}), "events": ev["clusters"], "series": len({r["series"] for r in rows}),
            "win": round(sum(r["won"] for r in rows) / len(rows), 4), "loss_rate": round(1 - sum(r["won"] for r in rows) / len(rows), 4),
            "avg_px": round(st.mean(r["px"] for r in rows), 4), "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4),
            "t": ev["t"], "t_series": clustered_t(rows, "series")["t"], "t_quarter": clustered_t(rows, "quarter")["t"],
            "series_clusters": clustered_t(rows, "series")["clusters"], "quarter_clusters": clustered_t(rows, "quarter")["clusters"],
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in srt[:h]), 4) if h else None, "half2": round(st.mean(r["ret"] for r in srt[h:]), 4),
            "bounds": bounds_on_events(rows)}


def seeded_pick(rows: list[dict], per_event: int, seed: str) -> list[dict]:
    """At most per_event markets per event, chosen by sha1(seed + ticker): listing fields only."""
    by = defaultdict(list)
    for r in rows:
        by[r["e"]].append(r)
    out = []
    for e in sorted(by):
        out += sorted(by[e], key=lambda r: hashlib.sha1((seed + r["t"]).encode()).hexdigest())[:per_event]
    return out
