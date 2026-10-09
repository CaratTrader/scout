"""Adversarial reproduction of r4_long_dated_maker_no (written from the claim text only; none of the claimant's code is used).

Claim: on long-dated Kalshi binaries (scheduled life >= 45 d), at D in {60,45,30} days before the scheduled deadline S,
if the NO taker price (1 - yes_bid) is in [0.70, 0.95), rest a maker order selling YES at yes_ask - 0.01 (= a NO bid 1c
better than the best NO bid) for 7 days; C1 posts once (needs spread >= 2c), C2/C3 re-price daily at the then-current
ask-1c while NO taker is in [0.70, 0.98) and spread >= 2c; C3 = C2 with signal band [0.80, 0.92).
Fill = through-only: an hourly candle wholly after post + 120 s (so the first hour after each post is dropped) whose YES
trade-price high is strictly above our ask. Settlement to the market result. Maker fee 0 for 'quadratic' series,
ceil-to-cent 0.25 * 0.07 * mult * C * p * (1 - p) for 'quadratic_with_maker_fees'; $5 orders (C = floor(5 / p)).

Data (read as raw Kalshi API responses only, 0 new calls unless --refetch):
  known  = hourly candles cached by round 3 (r3_long_dated_longshot_no api_cache + repro/api_cache)
  fresh  = hourly candles cached by the r4 maker researcher (raw responses only)
  census = hourly candles cached by r4_long_dated_archive_census (raw responses only), minus known/fresh
Metadata (rules_primary, open/close, result) from every market listing cached under data/**/api_cache.
Split: known events ordered by event close (max close of their markets), discovery = first 70%, validation = last 30%;
claimant's validation = known-val U fresh U census. Also reported: strict time split (only events closing after the cut).

Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_maker_no_repro [--maxage 72]
"""
from __future__ import annotations

import calendar
import datetime as dt
import glob
import json
import math
import os
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r4_long_dated_maker_no/repro"
SETS = {
    "known": ["data/kalshi_lab/strategies/r3_long_dated_longshot_no/api_cache",
              "data/kalshi_lab/strategies/r3_long_dated_longshot_no/repro/api_cache"],
    "fresh": ["data/kalshi_lab/strategies/r4_long_dated_maker_no/api_cache"],
    "census": ["data/kalshi_lab/strategies/r4_long_dated_archive_census/api_cache"],
}
DAY = 86400
H = 3600
DS = (60, 45, 30)
WINDOW_D = 7
STAKE = 5.0
EPS = 1e-9

# ----------------------------------------------------------------------------------------------- loading


def _f(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_candle(c: dict) -> tuple:
    """(end_ts, yes_bid_close, yes_ask_close, trade_high, volume) for both the historical and the live response shape."""
    yb, ya, pr = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
    bid = _f(yb.get("close", yb.get("close_dollars")))
    ask = _f(ya.get("close", ya.get("close_dollars")))
    hi = _f(pr.get("high", pr.get("high_dollars")))
    vol = _f(c.get("volume", c.get("volume_fp"))) or 0.0
    return (int(c["end_period_ts"]), bid, ask, hi, vol)


def load_candles() -> tuple[dict, dict]:
    cand, origin = {}, {}
    for name, dirs in SETS.items():
        for d in dirs:
            for f in sorted(os.listdir(ROOT / d)):
                if not f.endswith(".json"):
                    continue
                j = json.load(open(ROOT / d / f))
                items = []
                if isinstance(j, dict) and "candlesticks" in j and "ticker" in j:
                    items.append((j["ticker"], j["candlesticks"]))
                if isinstance(j, dict) and isinstance(j.get("markets"), list):
                    for m in j["markets"]:
                        if isinstance(m, dict) and "market_ticker" in m and "candlesticks" in m:
                            items.append((m["market_ticker"], m["candlesticks"]))
                for t, cs in items:
                    rows = sorted(norm_candle(c) for c in cs)
                    if not rows:
                        continue
                    if t in cand and len(cand[t]) >= len(rows):
                        origin.setdefault(t, set()).add(name)
                        continue
                    cand[t] = rows
                    origin.setdefault(t, set()).add(name)
    return cand, origin


def load_meta(tickers: set) -> dict:
    meta = {}
    for d in sorted(set(glob.glob(str(ROOT / "data/**/api_cache"), recursive=True))):
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            try:
                j = json.load(open(os.path.join(d, f)))
            except Exception:
                continue
            if not (isinstance(j, dict) and isinstance(j.get("markets"), list)):
                continue
            for m in j["markets"]:
                if not (isinstance(m, dict) and m.get("ticker") in tickers and "rules_primary" in m):
                    continue
                old = meta.get(m["ticker"])
                if old is None or (m.get("result") in ("yes", "no") and old.get("result") not in ("yes", "no")):
                    meta[m["ticker"]] = m
    return meta


def load_fees() -> dict:
    s = json.load(open(ROOT / "data/kalshi_lab/series_all.json"))
    lst = s["series"] if isinstance(s, dict) else s
    return {x["ticker"]: (x.get("fee_type") or "quadratic", float(x.get("fee_multiplier") if x.get("fee_multiplier") is not None else 1)) for x in lst}


def iso(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        tz = tail[tail.find("+"):] if "+" in tail else ""
        s = head + tz
    return int(dt.datetime.fromisoformat(s).timestamp())


# ----------------------------------------------------------------------------------------------- deadline

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
DATE_RE = re.compile(r"\b(before|by|on|through|until|and|of|for|after|between|to|from)?\s*(?:the\s+end\s+of\s+)?"
                     r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
MONYEAR_RE = re.compile(r"\b(before|by|in|until|through|during)\s+(?:the\s+end\s+of\s+)?"
                        r"(january|february|march|april|may|june|july|august|september|october|november|december|"
                        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{4})\b", re.I)
YEAR_RE = re.compile(r"\b(before|in|by the end of|by end of|during|by)\s+(?:(?:q([1-4])\s+)?(\d{4})(?:\s+q([1-4]))?)\b", re.I)
QTR_RE = re.compile(r"\bq([1-4])\s+(\d{4})\b|\b(\d{4})\s+q([1-4])\b", re.I)
TICK_RE = re.compile(r"(?:^|-)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(?=$|-)")


def utc(y, mo, d) -> int:
    return calendar.timegm((y, mo, d, 0, 0, 0))


def next_month(y, mo) -> int:
    return utc(y + 1, 1, 1) if mo == 12 else utc(y, mo + 1, 1)


def deadline(m: dict) -> tuple[int | None, str]:
    """Scheduled deadline from listing-time text, in this order:
    1. rules date with a day ('before D' -> D 00:00 UTC; 'by/on/through D' -> D + 1 day; the latest such date);
    2. rules month-year ('before July 2026' -> Jul 1; 'by/in July 2026' -> Aug 1);
    3. the last YYMONDD token of the ticker (+1 day) (market date is more specific than the event date);
    4. rules quarter / year ('before 2026' -> Jan 1 2026; 'in 2025' -> Jan 1 2026; 'Q3 2021' -> Oct 1 2021; the latest);
    5. close_time only if the market cannot close early. The actual close of an early-closing market is never used."""
    txt = m.get("rules_primary") or ""
    best = None
    for mt in DATE_RE.finditer(txt):
        prep, mon, d, y = (mt.group(1) or "").lower(), mt.group(2).lower(), int(mt.group(3)), int(mt.group(4))
        if prep in ("after", "from"):
            continue
        mo = MONTHS.get(mon[:3])
        try:
            ts = utc(y, mo, d) + (0 if prep == "before" else DAY)
        except Exception:
            continue
        best = ts if best is None or ts > best else best
    if best:
        return best, "rules_date"
    for mt in MONYEAR_RE.finditer(txt):
        prep, mo, y = mt.group(1).lower(), MONTHS.get(mt.group(2).lower()[:3]), int(mt.group(3))
        ts = utc(y, mo, 1) if prep == "before" else next_month(y, mo)
        best = ts if best is None or ts > best else best
    if best:
        return best, "rules_month"
    toks = TICK_RE.findall(m["ticker"])
    if toks:
        y, mon, d = toks[-1]
        try:
            return utc(2000 + int(y), MONTHS[mon.lower()], int(d)) + DAY, "ticker"
        except Exception:
            pass
    how = None
    for mt in QTR_RE.finditer(txt):
        q, y = (int(mt.group(1)), int(mt.group(2))) if mt.group(1) else (int(mt.group(4)), int(mt.group(3)))
        ts = next_month(y, q * 3)
        if best is None or ts > best:
            best, how = ts, "rules_quarter"
    if best:
        return best, how
    for mt in YEAR_RE.finditer(txt):
        prep, y = mt.group(1).lower(), int(mt.group(3))
        ts = utc(y, 1, 1) if prep == "before" else utc(y + 1, 1, 1)
        if best is None or ts > best:
            best, how = ts, "rules_year"
    if best:
        return best, how
    if m.get("can_close_early") is False:
        return iso(m.get("close_time")), "close_noearly"
    return None, "none"


# ----------------------------------------------------------------------------------------------- execution


def quote(rows: list, t: int, maxage: int):
    """(end_ts, bid, ask) of the last candle ending at or before t with both quotes, if not older than maxage s."""
    best = None
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    while i >= 0:
        r = rows[i]
        if r[1] is not None and r[2] is not None:
            best = r
            break
        i -= 1
    if best is None or t - best[0] > maxage:
        return None
    return best[0], best[1], best[2]


def fee_order(p: float, c: int, ftype: str, mult: float, maker: bool) -> float:
    if maker:
        if ftype != "quadratic_with_maker_fees":
            return 0.0
        raw = 0.25 * 0.07 * mult * c * p * (1 - p)
    else:
        raw = 0.07 * mult * c * p * (1 - p)
    return math.ceil(raw * 100 - 1e-6) / 100 if raw > 0 else 0.0


def pnl_row(p: float, win: bool, ftype: str, mult: float, maker: bool) -> dict:
    c = max(1, int(math.floor(STAKE / p + 1e-9)))
    fe = fee_order(p, c, ftype, mult, maker)
    pnl = c * (1.0 if win else 0.0) - c * p - fe
    return {"px": round(p, 4), "contracts": c, "fee": fe, "pnl": pnl, "cost": c * p, "ret": pnl / (c * p), "won": win}


def through(rows: list, a: float, start: int, end: int):
    """First candle with end in [start, end] whose trade high > a; plus the volume of all such candles (upper bound)."""
    first, vol = None, 0.0
    for r in rows:
        if r[0] < start:
            continue
        if r[0] > end:
            break
        if r[3] is not None and r[3] > a + EPS:
            if first is None:
                first = r[0]
            vol += r[4]
    return first, vol


def print_through(prints: list, a: float, start: int, end: int):
    """First trade print with ts in [start, end) and YES price strictly above a; total contracts printed through a."""
    first, vol = None, 0.0
    for ts, px, cnt in prints:
        if ts < start:
            continue
        if ts >= end:
            break
        if px > a + EPS:
            if first is None:
                first = ts
            vol += cnt
    return first, vol


def covered(cov: list, start: int, end: int) -> bool:
    return any(lo <= start + 1 and hi >= end - 1 for lo, hi in cov)


def orders_for(arm: str, t: int, rows: list, cl: int, maxage: int, bid: float, ask: float) -> list[tuple]:
    """Planned orders (post_ts, ask_price, cancel_ts). C1: one order for 7 d; C2/C3: one order per day at that day's ask-1c."""
    if arm == "C1":
        if ask - bid < 0.02 - EPS:
            return []
        return [(t, round(ask - 0.01, 4), min(t + WINDOW_D * DAY, cl))]
    out = []
    for k in range(WINDOW_D):
        tk_ = t + k * DAY
        if tk_ + H >= cl:
            break
        qk = quote(rows, tk_, maxage)
        if not qk:
            continue
        nb = 1 - qk[1]
        if (0.70 - EPS <= nb < 0.98 - EPS) and qk[2] - qk[1] >= 0.02 - EPS:
            out.append((tk_, round(qk[2] - 0.01, 4), min(tk_ + DAY, cl)))
    return out


def simulate(mk: dict, rows: list, maxage: int, drop_first_hour: bool = True, refresh_stop: bool = False,
             prints: list | None = None, cov: list | None = None) -> list[dict]:
    out = []
    S, op, cl = mk["S"], mk["open"], mk["close"]
    win = mk["result"] == "no"
    lag = 2 * H if drop_first_hour else H
    for D in DS:
        t = S - D * DAY
        if not (op <= t and t + H < cl):
            continue
        q = quote(rows, t, maxage)
        if not q:
            continue
        _, bid, ask = q
        no_taker = 1 - bid
        base = {"t": mk["t"], "e": mk["e"], "series": mk["series"], "set": mk["set"], "D": D, "t_sig": t, "S": S,
                "no_taker": round(no_taker, 4), "spread": round(ask - bid, 4), "result": mk["result"], "ev_close": mk["ev_close"]}
        q1 = quote(rows, t + H, maxage)   # taker on the same signal: NO at 1 - yes_bid of the next hourly quote
        tk = pnl_row(1 - q1[1], win, mk["ftype"], mk["mult"], maker=False) if q1 and 0.01 <= 1 - q1[1] <= 0.99 else None
        for arm in ("C1", "C2", "C3"):
            lo, hi = (0.80, 0.92) if arm == "C3" else (0.70, 0.95)
            if not (lo - EPS <= no_taker < hi - EPS):
                continue
            od = orders_for(arm, t, rows, cl, maxage, bid, ask)
            if refresh_stop and arm != "C1" and od:   # variant: stop for good at the first day the conditions fail
                keep = [od[0]]
                for o in od[1:]:
                    if o[0] - keep[-1][0] == DAY:
                        keep.append(o)
                    else:
                        break
                od = keep
            r = dict(base, arm=arm, taker=tk, eligible=bool(od), posts=len(od), filled=False)
            for k, (pt, a, cx) in enumerate(od):
                first, vol = through(rows, a, pt + lag, cx)
                if first is not None:
                    r.update(filled=True, post_ask=a, post_ts=pt, fill_ts=first, through_vol=vol, fill_order=k)
                    r["maker"] = pnl_row(1 - a, win, mk["ftype"], mk["mult"], maker=True)
                    break
            # independent check on raw trade prints where they were cached: through-only after post + 120 s
            if prints is not None and od:
                # same exclusion as the candle model: trades from post + 1 h + 120 s (post + 120 s if the first hour is kept)
                pl = H + 120 if drop_first_hour else 180
                ok = all(covered(cov, pt + pl, cx) for pt, a, cx in od)
                pf = None
                if ok:
                    for k, (pt, a, cx) in enumerate(od):
                        f1, v1 = print_through(prints, a, pt + pl, cx)
                        if f1 is not None:
                            pf = {"fill_ts": f1, "post_ask": a, "through_contracts": v1, "order": k}
                            break
                r["print_check"] = {"covered": ok, "filled": pf is not None if ok else None, "fill": pf}
                if ok and pf:
                    r["maker_print"] = pnl_row(1 - pf["post_ask"], win, mk["ftype"], mk["mult"], maker=True)
            out.append(r)
    return out


# ----------------------------------------------------------------------------------------------- statistics


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact (Clopper-Pearson) lower bound on a binomial rate."""
    if k <= 0:
        return 0.0

    def tail(p):  # P(X >= k | p)
        return 1 - sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k))
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def clustered_t(rs: list[dict], key: str, by: str) -> float | None:
    g = defaultdict(list)
    for r in rs:
        g[r[by]].append(r[key]["ret"])
    m = [st.mean(v) for v in g.values()]
    if len(m) < 3 or st.pstdev(m) == 0:
        return None
    return round(st.mean(m) / (st.pstdev(m) / math.sqrt(len(m))), 2)


def stats(rows: list[dict], key: str = "maker") -> dict:
    rs = [r for r in rows if r.get(key)]
    if not rs:
        return {"n": 0}
    x = [r[key] for r in rs]
    ev = defaultdict(list)
    for r in rs:
        ev[r["e"]].append(r[key]["ret"])
    em = [st.mean(v) for v in ev.values()]
    rets = sorted((v["ret"] for v in x), reverse=True)
    times = sorted(r["t_sig"] for r in rs)
    mid = times[len(times) // 2]
    h1 = [r[key]["ret"] for r in rs if r["t_sig"] < mid]
    h2 = [r[key]["ret"] for r in rs if r["t_sig"] >= mid]
    # amendment (d): one trade per unique event (first by signal time); exact one-sided 95% lower bound on the win
    # rate, mapped to a per-$ return at the average price and fee of those trades
    first = {}
    for r in sorted(rs, key=lambda r: r["t_sig"]):
        first.setdefault(r["e"], r)
    fu = list(first.values()); k = sum(r[key]["won"] for r in fu); n = len(fu)
    plo = cp_lower(k, n)
    apx = st.mean(r[key]["px"] for r in fu); afee = st.mean(r[key]["fee"] / r[key]["contracts"] for r in fu)
    lb = (plo - apx - afee) / apx
    return {"n": len(rs), "events": len(em), "markets": len({r["t"] for r in rs}), "series": len({r["series"] for r in rs}),
            "win": round(sum(v["won"] for v in x) / len(x), 4), "avg_px": round(st.mean(v["px"] for v in x), 4),
            "ret_per_dollar": round(st.mean(v["ret"] for v in x), 4),
            "ret_size_weighted": round(sum(v["pnl"] for v in x) / sum(v["cost"] for v in x), 4),
            "ev_mean": round(st.mean(em), 4), "t": clustered_t(rs, key, "e"), "t_series": clustered_t(rs, key, "series"),
            "ret_wo3": round(st.mean(rets[3:]), 4) if len(rets) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "uniq_events": n, "uniq_losses": n - k, "win_lo95_events": round(plo, 4), "binomial_lb_ret": round(lb, 4)}


def summarize(rows: list[dict], arm: str) -> dict:
    a = [r for r in rows if r["arm"] == arm]
    el = [r for r in a if r["eligible"]]
    fi = [r for r in el if r["filled"]]
    uf = [r for r in el if not r["filled"]]
    s = stats(fi, "maker")
    caps = sorted(r.get("through_vol") or 0 for r in fi)
    # print audit (raw trade prints cached by the r4 maker researcher): only orders whose whole life is covered
    pc = [r for r in el if (r.get("print_check") or {}).get("covered")]
    agree = Counter((bool(r["filled"]), bool(r["print_check"]["filled"])) for r in pc)
    sp = stats([r for r in pc if r.get("maker_print")], "maker_print")
    return {"signals": len(a), "maker_eligible": len(el), "fills": len(fi),
            "fill_rate": round(len(fi) / len(el), 4) if el else None,
            "win_filled": s.get("win"), "win_unfilled": round(sum(r["result"] == "no" for r in uf) / len(uf), 4) if uf else None,
            "n_unfilled": len(uf), "maker": s,
            "taker_on_filled": stats([r for r in fi if r.get("taker")], "taker"),
            "taker_on_maker_eligible": stats([r for r in el if r.get("taker")], "taker"),
            "taker_all_signals": stats([r for r in a if r.get("taker")], "taker"),
            "median_through_volume_contracts_candle_upper": caps[len(caps) // 2] if caps else None,
            "print_audit": {"orders_covered": len(pc), "candle_fill_vs_print_fill": {f"candle={c},print={p}": v for (c, p), v in agree.items()},
                            "maker_on_prints_covered_only": sp}}


# ----------------------------------------------------------------------------------------------- main


def build(maxage: int):
    cand, origin = load_candles()
    meta = load_meta(set(cand))
    fees = load_fees()
    mk = {}
    why = Counter()
    for t, rows in cand.items():
        m = meta.get(t)
        if m is None:
            why["no_meta"] += 1; continue
        if t.startswith("KXMVE") or m.get("market_type", "binary") != "binary":
            why["not_binary"] += 1; continue
        if m.get("result") not in ("yes", "no"):
            why["no_result"] += 1; continue
        S, how = deadline(m)
        op, cl = iso(m.get("open_time")), iso(m.get("close_time"))
        if S is None or op is None or cl is None:
            why["no_deadline"] += 1; continue
        if S - op < 45 * DAY:
            why["life_lt_45d"] += 1; continue
        o = origin[t]
        st_ = "known" if "known" in o else ("fresh" if "fresh" in o else "census")
        series = m.get("series_ticker") or m["event_ticker"].split("-")[0]
        ft, mult = fees.get(series, ("quadratic", 1.0))
        mk[t] = {"t": t, "e": m["event_ticker"], "series": series, "open": op, "close": cl, "S": S, "S_how": how,
                 "result": m["result"], "set": st_, "ftype": ft, "mult": mult, "early": m.get("can_close_early")}
    # event close and the known split
    evc = defaultdict(int)
    for x in mk.values():
        evc[x["e"]] = max(evc[x["e"]], x["close"])
    for x in mk.values():
        x["ev_close"] = evc[x["e"]]
    kev = sorted({x["e"] for x in mk.values() if x["set"] == "known"}, key=lambda e: evc[e])
    cut = evc[kev[int(len(kev) * 0.7)]]
    return cand, mk, cut, why


def load_prints() -> tuple[dict, dict]:
    """Raw /historical/trades and /markets/trades responses cached on disk (data only), with request windows from calls.log."""
    prints, cov = defaultdict(dict), defaultdict(list)
    for d in sorted(set(glob.glob(str(ROOT / "data/kalshi_lab/strategies/*/api_cache")))):
        for f in os.listdir(d):
            try:
                j = json.load(open(os.path.join(d, f)))
            except Exception:
                continue
            if isinstance(j, dict) and isinstance(j.get("trades"), list):
                for x in j["trades"]:
                    ts = iso(x.get("created_time"))
                    px = _f(x.get("yes_price_dollars", x.get("yes_price")))
                    if px is not None and px > 1.5:
                        px /= 100
                    prints[x["ticker"]][x.get("trade_id") or (ts, px)] = (ts, px, _f(x.get("count_fp", x.get("count"))) or 0)
        log = Path(d).parent / "calls.log"
        if log.exists():
            for line in open(log):
                u = line.rstrip("\n").split("\t")[-1]
                if "/trades?" in u and "min_ts=" in u and "cursor=" not in u:
                    qs = dict(kv.split("=", 1) for kv in u.split("?", 1)[1].split("&") if "=" in kv)
                    if "ticker" in qs and "max_ts" in qs:
                        cov[qs["ticker"]].append((int(qs["min_ts"]), int(qs["max_ts"])))
    # a window counts as covered only if it was not truncated (fewer than 1000 prints, or a cursor page followed)
    return {t: sorted(v.values()) for t, v in prints.items()}, cov


def run(maxage: int = 72 * H, drop_first_hour: bool = True, refresh_stop: bool = False, verbose: bool = True):
    cand, mk, cut, why = build(maxage)
    prints, cov = load_prints()
    rows = []
    for t, x in mk.items():
        rows.extend(simulate(x, cand[t], maxage, drop_first_hour, refresh_stop,
                             prints.get(t, []) if t in cov else None, cov.get(t)))
    for r in rows:
        r["phase"] = ("disc" if r["ev_close"] < cut else "val") if r["set"] == "known" else "oos"
    groups = {
        "discovery_known": [r for r in rows if r["set"] == "known" and r["phase"] == "disc"],
        "validation_union": [r for r in rows if r["phase"] in ("val", "oos")],
        "val_known": [r for r in rows if r["set"] == "known" and r["phase"] == "val"],
        "val_fresh": [r for r in rows if r["set"] == "fresh"],
        "val_census": [r for r in rows if r["set"] == "census"],
        "val_strict_time_split": [r for r in rows if r["ev_close"] >= cut],
    }
    res = {"cut_event_close": cut, "cut_iso": dt.datetime.utcfromtimestamp(cut).isoformat() + "Z",
           "markets_by_set": dict(Counter(x["set"] for x in mk.values())),
           "events_by_set": {s: len({x["e"] for x in mk.values() if x["set"] == s}) for s in ("known", "fresh", "census")},
           "known_events_disc_val": [len({x["e"] for x in mk.values() if x["set"] == "known" and x["ev_close"] < cut}),
                                     len({x["e"] for x in mk.values() if x["set"] == "known" and x["ev_close"] >= cut})],
           "excluded": dict(why), "S_how": dict(Counter(x["S_how"] for x in mk.values())),
           "maxage_h": maxage / H, "drop_first_hour": drop_first_hour, "refresh_stop": refresh_stop, "groups": {}}
    for g, rs in groups.items():
        res["groups"][g] = {arm: summarize(rs, arm) for arm in ("C1", "C2", "C3")}
    if verbose:
        print(f"cut {res['cut_iso']}  markets {res['markets_by_set']} events {res['events_by_set']} known disc/val events {res['known_events_disc_val']}")
        print(f"excluded {res['excluded']}  S_how {res['S_how']}")
        for g in groups:
            for arm in ("C1", "C2", "C3"):
                s = res["groups"][g][arm]; m = s["maker"]; tk = s["taker_on_filled"]; ta = s["taker_all_signals"]
                if not m.get("n"):
                    print(f"  {g:22s} {arm} signals {s['signals']} eligible {s['maker_eligible']} fills 0"); continue
                te = s["taker_on_maker_eligible"]; pa = s["print_audit"]
                print(f"  {g:22s} {arm} sig {s['signals']:3d} elig {s['maker_eligible']:3d} fills {m['n']:3d} ev {m['events']:3d} ser {m['series']:3d} win {m['win']:.3f} "
                      f"px {m['avg_px']:.3f} eq$ {m['ret_per_dollar']:+.4f} sw {m['ret_size_weighted']:+.4f} t {m['t']} tS {m['t_series']} wo3 {m['ret_wo3']} "
                      f"h {m['half1']}/{m['half2']} lb {m['binomial_lb_ret']:+.3f} ({m['uniq_events']}ev/{m['uniq_losses']}L) unf_win {s['win_unfilled']} (n {s['n_unfilled']}) "
                      f"tk_fill {tk.get('ret_per_dollar')} tk_elig {te.get('ret_per_dollar')} tk_all {ta.get('ret_per_dollar')} (n {ta.get('n')}) "
                      f"prints {pa['orders_covered']} {pa['candle_fill_vs_print_fill']}")
    return res, rows, mk


def main():
    maxage = 72
    if "--maxage" in sys.argv:
        maxage = int(sys.argv[sys.argv.index("--maxage") + 1])
    OUT.mkdir(parents=True, exist_ok=True)
    res, rows, mk = run(maxage * H)
    (OUT / "repro_validation.json").write_text(json.dumps(res, indent=1, default=str))
    with open(OUT / "repro_rows.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    with open(OUT / "repro_markets.jsonl", "w") as f:
        for x in mk.values():
            f.write(json.dumps(x) + "\n")
    sens = {}
    for lab, kw in (("maxage24", dict(maxage=24 * H)), ("maxage2", dict(maxage=2 * H)),
                    ("keep_first_hour", dict(maxage=maxage * H, drop_first_hour=False)),
                    ("refresh_stop_on_exit", dict(maxage=maxage * H, refresh_stop=True))):
        print(f"\n--- sensitivity {lab}")
        r2, _, _ = run(verbose=True, **kw)
        sens[lab] = {g: {a: {"fills": v[a]["fills"], **{k: v[a]["maker"].get(k) for k in ("ret_per_dollar", "ret_size_weighted", "t", "win", "events")}}
                         for a in v} for g, v in r2["groups"].items()}
    (OUT / "repro_sensitivity.json").write_text(json.dumps(sens, indent=1))


if __name__ == "__main__":
    main()
