"""Adversarial reproduction of r4_nested_deadline_term_structure, written from the claim's text only (no code reused).

Claim: on nested 'X happens before D1 < D2 < ...' ladders, the near rung's YES bid sits above kappa x the probability
implied by a constant hazard fitted to the far rungs; buying NO on that rung as a taker beats the plain long-dated NO
band B (NO 0.70-0.97 at D in {60,45,30,21,14} d, first entry per market) on the same markets.

Data: raw Kalshi API responses already cached by the lab (every data/kalshi_lab/strategies/*/api_cache/*.json and
*/repro/api_cache/*.json: market lists and hourly candlesticks), parsed here independently, plus a small number of
fresh spot-check calls (--verify) through lab.us.data_refresh.fetch.

Rule (from the frozen spec):
  daily 14:00 UTC snapshot (last hourly candle with end_period_ts <= t); ladder = Kalshi event, rungs with a deadline D
  parsed from rules_primary / yes_sub_title (never from close_time, which moves to the early close on YES);
  thinned ladder (greedy from the earliest D, kept deadlines >= 7 d apart); near rung = open thinned rung with the
  smallest D and tau >= 1 d; far rungs = 3 nearest open thinned rungs beyond it with tau <= 365 d and ask-bid <= 0.15;
  y = -ln(1-mid) = lambda tau ('ls' = least squares through 0 over the far rungs, 'next' = nearest far rung only);
  q = 1 - exp(-lambda tau_near); signal when near yes_bid >= kappa q, tau_near in [1,60] d and 1-yes_bid in [lo,hi);
  fill NO at the 15:00 UTC quote (1 - yes_bid), limit = decision NO + 0.05, a market closing inside the fill hour fills
  at the decision price; fee 0.07 p (1-p) rounded up per 6-contract order; first (filled) signal per market.
Split: ladders with an evaluable near-rung market, ordered by the latest close of their settled rungs; first 70%
discovery, last 30% validation (the claim's split), plus alternative splits that do not depend on the outcome.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_nested_deadline_term_structure_repro [--verify N]
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import math
import re
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data/kalshi_lab/strategies/r4_nested_deadline_term_structure/repro"
CACHES = sorted(glob.glob(str(ROOT / "data/kalshi_lab/strategies/*/api_cache"))) + \
    sorted(glob.glob(str(ROOT / "data/kalshi_lab/strategies/*/repro/api_cache")))
SERIES = ["KXBONDIOUT", "KXGABBARDOUT", "KXKASHOUT", "KXNOEMOUT", "KXHEGSETHOUT", "KXLEAVEWALZ", "KXGOVTFUND",
          "KXSHUTDOWNBYDATE", "KXDHSFUNDING", "KXSENATEREC", "KXFISAEXTEND", "KXTRUMPCHINA", "KXWARSHNOM",
          "KXLEAVELISACOOK", "KXSWALWELLOUT", "KXLEAVEPOWELLGOV", "KXPAHLAVIVISITA", "KXVOTEFUNDING",
          "KXTARIFFDECISIONRELEASE", "KXLUTNICKOUT", "KXAGCONF", "KXUSAIRANAGREEMENT", "KXHORMUZNORM", "KXDHSFUND",
          "KXGREENLAND", "KXALIENS", "KXTRUMPOUT27", "KXSPACEXSTARSHIP", "SPACEXSTARSHIP", "KXPRESNOMFEDCHAIR",
          "LEAVEPOWELL", "KXCRYPTOSTRUCTURE", "OAIAGI"]
DAY = 86400
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MON_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


# ------------------------------------------------------------------ raw data
def iso(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fnum(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def candle_quote(c: dict) -> tuple[float | None, float | None, float]:
    a, b = c.get("yes_ask") or {}, c.get("yes_bid") or {}
    ask = fnum(a.get("close", a.get("close_dollars")))
    bid = fnum(b.get("close", b.get("close_dollars")))
    vol = fnum(c.get("volume", c.get("volume_fp"))) or 0.0
    return ask, bid, vol


def series_of(t: str) -> str:
    return t.split("-")[0]


def load_pool() -> tuple[dict, dict]:
    """markets {ticker: raw market dict (most final version)}, candles {ticker: {end_ts: (ask, bid)}} (hourly only)."""
    want = set(SERIES)
    mk: dict[str, dict] = {}
    cd: dict[str, dict[int, tuple]] = defaultdict(dict)

    def add_candles(tk: str, cs: list) -> None:
        if series_of(tk) not in want or not cs:
            return
        ts = sorted(c["end_period_ts"] for c in cs)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if gaps and min(gaps) != 3600:
            return                                    # not hourly (daily or 1-minute): ignore
        for c in cs:
            cd[tk][int(c["end_period_ts"])] = candle_quote(c)

    rank = {"finalized": 3, "settled": 3, "determined": 2, "closed": 1}
    for d in CACHES:
        for f in glob.glob(d + "/*.json"):
            try:
                x = json.load(open(f))
            except Exception:
                continue
            if not isinstance(x, dict):
                continue
            if "candlesticks" in x and "ticker" in x:
                add_candles(x["ticker"], x["candlesticks"])
            elif isinstance(x.get("markets"), list):
                for m in x["markets"]:
                    if "candlesticks" in m:
                        add_candles(m.get("market_ticker", ""), m["candlesticks"])
                    elif "ticker" in m and series_of(m["ticker"]) in want:
                        old = mk.get(m["ticker"])
                        key = (rank.get(m.get("status"), 0), m.get("updated_time") or "")
                        if old is None or key > (rank.get(old.get("status"), 0), old.get("updated_time") or ""):
                            mk[m["ticker"]] = m
    return mk, cd


# ------------------------------------------------------------------ deadlines
def et_midnight(y: int, mo: int, d: int) -> int:
    """Unix time of 00:00 America/New_York on that date (US DST rule since 2007)."""
    def nth_sunday(year, month, n):
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    day = dt.date(y, mo, d)
    dst = nth_sunday(y, 3, 2) < day <= nth_sunday(y, 11, 1)
    return int(dt.datetime(y, mo, d, tzinfo=dt.timezone.utc).timestamp()) + (4 if dst else 5) * 3600


def parse_deadline(m: dict) -> tuple[int | None, str]:
    rules = (m.get("rules_primary") or "").lower()
    sub = (m.get("yes_sub_title") or "").lower()
    for src, txt in (("rules", rules), ("sub", sub)):
        txt = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", txt)
        mm = re.search(r"\bbefore " + MON_RE + r"\.? (\d{1,2}),? (\d{4})", txt)
        if mm:
            return et_midnight(int(mm.group(3)), MONTHS[mm.group(1)[:3]], int(mm.group(2))), src + ":before_date"
        mm = re.search(r"\bby " + MON_RE + r"\.? (\d{1,2}),? (\d{4})", txt)
        if mm:
            d = dt.date(int(mm.group(3)), MONTHS[mm.group(1)[:3]], int(mm.group(2))) + dt.timedelta(days=1)
            return et_midnight(d.year, d.month, d.day), src + ":by_date+1"
        mm = re.search(r"\bbefore " + MON_RE + r",? (\d{4})", txt)
        if mm:
            return et_midnight(int(mm.group(2)), MONTHS[mm.group(1)[:3]], 1), src + ":before_month"
        mm = re.search(r"\bbefore (\d{4})\b", txt)
        if mm:
            return et_midnight(int(mm.group(1)), 1, 1), src + ":before_year"
    return None, "none"


# ------------------------------------------------------------------ helpers
def fee_per_contract(p: float, n: int = 6) -> float:
    return math.ceil(round(n * 0.07 * p * (1 - p) * 100, 9)) / 100 / n


def quote_at(c: dict, keys: list, t: int) -> tuple[float, float] | None:
    """Last hourly candle with end_period_ts <= t (carry forward; Kalshi emits a candle when the book or trades change)."""
    import bisect
    i = bisect.bisect_right(keys, t) - 1
    if i < 0:
        return None
    ask, bid = c[keys[i]][:2]
    if ask is None or bid is None:
        return None
    return ask, bid


def clopper_lower(k: int, n: int, alpha: float = 0.05) -> float:
    if k == 0:
        return 0.0
    def tail(p):  # P(X >= k | p)
        return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["ev"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    ne = len(em)
    sd = st.pstdev(em) if ne > 1 else 0.0
    t = st.mean(em) / (sd / math.sqrt(ne)) if ne > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["t_entry"])
    h = len(srt) // 2
    ev_win = sum(1 for v in ev.values() if st.mean(v) > 0)
    plo = clopper_lower(ev_win, ne)
    avg_px = st.mean(r["px"] for r in rows)
    avg_fee = st.mean(r["fee"] for r in rows)
    return {"n": len(rows), "events": ne, "win": round(sum(r["won"] for r in rows) / len(rows), 4),
            "avg_px": round(avg_px, 4), "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4),
            "evmean": round(st.mean(em), 4), "t": round(t, 3) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in srt[:h]), 4) if h else None,
            "half2": round(st.mean(r["ret"] for r in srt[h:]), 4) if len(srt) - h else None,
            "beta_events": ne, "beta_event_wins": ev_win, "beta_win_lo95": round(plo, 4),
            "beta_lower_bound_ret": round(plo / avg_px - 1 - avg_fee / avg_px, 4)}


# ------------------------------------------------------------------ build ladders and snapshots
def build(mk: dict, cd: dict, thin_with_candles: bool = False) -> tuple[dict, dict]:
    rung = {}
    for t, m in mk.items():
        D, src = parse_deadline(m)
        if D is None:
            continue
        res = (m.get("result") or "").lower()
        settled = m.get("status") in ("finalized", "settled", "determined") and res in ("yes", "no")
        rung[t] = {"t": t, "ev": m["event_ticker"], "D": D, "src": src, "open": iso(m.get("open_time")),
                   "close": iso(m.get("close_time")), "res": res if settled else "", "settled": settled}
    lad = defaultdict(list)
    for r in rung.values():
        lad[r["ev"]].append(r)
    ladders = {}
    for e, rs in lad.items():
        rs.sort(key=lambda r: r["D"])
        pool = [r for r in rs if (not thin_with_candles) or r["t"] in cd]
        kept, last = [], None
        for r in pool:
            if last is None or r["D"] - last >= 7 * DAY:
                kept.append(r); last = r["D"]
        if len(kept) >= 2:
            ladders[e] = kept
    return ladders, rung


def snapshots(ladders: dict, cd: dict) -> dict:
    """{ev: [snapshot dicts]} with near rung, its quote, far rungs and hazards at each daily 14:00 UTC."""
    keys = {t: sorted(c) for t, c in cd.items()}
    out = {}
    for e, rs in ladders.items():
        t0 = min(r["open"] for r in rs if r["open"]) if any(r["open"] for r in rs) else None
        t1 = max(r["close"] for r in rs if r["close"])
        if t0 is None:
            continue
        d0 = dt.datetime.utcfromtimestamp(t0).replace(hour=14, minute=0, second=0, tzinfo=dt.timezone.utc)
        t = int(d0.timestamp())
        if t < t0:
            t += DAY
        snaps = []
        while t <= min(t1, 1791504000 + 2 * DAY):
            opn = []
            for r in rs:
                if r["t"] not in cd or r["open"] is None or not (r["open"] <= t < r["close"]):
                    continue
                q = quote_at(cd[r["t"]], keys[r["t"]], t)
                if q is None:
                    continue
                opn.append((r, q))
            cands = [(r, q) for r, q in opn if (r["D"] - t) / DAY >= 1]
            if cands:
                near, nq = min(cands, key=lambda x: x[0]["D"])
                tau_n = (near["D"] - t) / DAY
                far = []
                for r, q in sorted(opn, key=lambda x: x[0]["D"]):
                    tau = (r["D"] - t) / DAY
                    if r["D"] <= near["D"] or tau > 365:
                        continue
                    ask, bid = q
                    if ask - bid > 0.15 or ask <= 0 or bid < 0:
                        continue
                    mid = (ask + bid) / 2
                    if mid >= 1:
                        continue
                    far.append((tau, -math.log(1 - mid)))
                    if len(far) == 3:
                        break
                lam = {}
                if far:
                    lam["ls"] = sum(y * x for x, y in far) / sum(x * x for x, y in far)
                    lam["next"] = far[0][1] / far[0][0]
                snaps.append({"t": t, "near": near, "tau": tau_n, "ask": nq[0], "bid": nq[1], "lam": lam, "nfar": len(far)})
            t += DAY
        out[e] = snaps
    return out


def fill(r: dict, cd: dict, keys: dict, t: int, dec_no: float, limit_add: float = 0.05) -> tuple[float, str] | None:
    tf = t + 3600
    if r["close"] is not None and r["close"] <= tf:
        return dec_no, "closed_in_fill_hour"
    q = quote_at(cd[r["t"]], keys[r["t"]], tf)
    if q is None:
        return None
    px = 1 - q[1]
    if px >= 1 or px > dec_no + limit_add + 1e-9:
        return None
    return px, "15utc"


def trade_row(e: str, r: dict, t: int, px: float, how: str, bid: float, tau: float, q: float | None, rule: str) -> dict:
    fe = fee_per_contract(px)
    won = r["res"] == "no"
    return {"rule": rule, "ev": e, "t": r["t"], "t_entry": t, "date": dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
            "tau": round(tau, 2), "bid": bid, "q": None if q is None else round(q, 4), "px": round(px, 4), "fee": fe,
            "won": won, "ret": ((1.0 if won else 0.0) - px - fe) / px, "fill": how, "series": series_of(r["t"])}


def run_rules(snaps: dict, cd: dict, rung: dict, strict_first: bool = False) -> tuple[dict, dict]:
    keys = {t: sorted(c) for t, c in cd.items()}
    near_mk = defaultdict(set)                      # ev -> near-rung market tickers (tau 1-60, settled)
    for e, ss in snaps.items():
        for s in ss:
            if 1 <= s["tau"] <= 60 and s["near"]["settled"]:
                near_mk[e].add(s["near"]["t"])
    trades = defaultdict(list)
    # signal rules
    for kappa in (1.25, 1.5, 2.0):
        for hz in ("ls", "next"):
            for lo in (0.30, 0.50):
                name = f"S k{kappa} {hz} NO{lo:.2f}-0.97"
                for e, ss in snaps.items():
                    done = set()
                    for s in ss:
                        r = s["near"]
                        if not r["settled"] or r["t"] in done or hz not in s["lam"]:
                            continue
                        if not (1 <= s["tau"] <= 60):
                            continue
                        q = 1 - math.exp(-s["lam"][hz] * s["tau"])
                        dec_no = 1 - s["bid"]
                        if not (s["bid"] >= kappa * q and lo <= dec_no < 0.97):
                            continue
                        f = fill(r, cd, keys, s["t"], dec_no)
                        if strict_first:
                            done.add(r["t"])
                        if f is None:
                            continue
                        done.add(r["t"])
                        trades[name].append(trade_row(e, r, s["t"], f[0], f[1], s["bid"], s["tau"], q, name))
    # band B on the near-rung markets
    for e, tks in near_mk.items():
        for tk in sorted(tks):
            r = rung[tk]
            for k in (60, 45, 30, 21, 14):
                # first daily 14:00 UTC snapshot with tau <= k
                tD = r["D"] - k * DAY
                d = dt.datetime.utcfromtimestamp(tD).replace(hour=14, minute=0, second=0, tzinfo=dt.timezone.utc)
                t = int(d.timestamp())
                if t < tD:
                    t += DAY
                if r["open"] is None or not (r["open"] <= t < r["close"]) or tk not in cd:
                    continue
                qq = quote_at(cd[tk], keys[tk], t)
                if qq is None:
                    continue
                dec_no = 1 - qq[1]
                if not (0.70 <= dec_no < 0.97):
                    continue
                f = fill(r, cd, keys, t, dec_no)
                if f is None:
                    if strict_first:
                        break
                    continue
                trades["B"].append(trade_row(e, r, t, f[0], f[1], qq[1], (r["D"] - t) / DAY, None, "B"))
                break
    return trades, near_mk


def split_events(near_mk: dict, rung: dict, ladders: dict, frac: float = 0.7) -> tuple[set, set, int]:
    evs = [e for e, v in near_mk.items() if v]
    last = {e: max(r["close"] for r in ladders[e] if r["settled"]) for e in evs}
    order = sorted(evs, key=lambda e: last[e])
    k = int(len(order) * frac)
    cut = last[order[k]] if k < len(order) else None
    return set(order[:k]), set(order[k:]), cut


def fmt(name: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {name:<34} n=0"
    return (f"  {name:<34} n={s['n']:>3} ev={s['events']:>3} win={s['win']:.0%} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.1%} "
            f"evmean={s['evmean']:+.1%} t={s['t'] if s['t'] is not None else float('nan'):5.2f} wo3={(s['ret_wo3'] if s['ret_wo3'] is not None else float('nan')):+.1%} "
            f"halves {s['half1'] if s['half1'] is not None else float('nan'):+.1%}/{s['half2'] if s['half2'] is not None else float('nan'):+.1%} "
            f"betaLB={s['beta_lower_bound_ret']:+.1%}")


def verify(n: int) -> None:
    """Fresh 1-minute candles around n validation entries: is the cached hourly quote at 14:00/15:00 UTC the real one?"""
    from lab.us.data_refresh import fetch, K
    rows = [json.loads(l) for l in open(OUT / "trades.jsonl")]
    want = ["S k1.5 ls NO0.50-0.97", "S k1.25 ls NO0.30-0.97", "B"]
    pick, seen = [], set()
    for name in want:
        for r in sorted((r for r in rows if r["rule"] == name and r["part"] == "val"), key=lambda r: -abs(r["ret"])):
            if r["t"] not in seen and len(pick) < n:
                pick.append(r); seen.add(r["t"])
    mk, _ = load_pool()
    out = []
    for r in pick:
        t0, t1 = r["t_entry"] - 3 * 3600, r["t_entry"] + 3600 + 60
        hist = (iso(mk[r["t"]].get("close_time")) or 0) < iso("2026-08-08T00:00:00Z")
        url = (f"{K}/historical/markets/{r['t']}/candlesticks?start_ts={t0}&end_ts={t1}&period_interval=1" if hist else
               f"{K}/markets/candlesticks?market_tickers={r['t']}&start_ts={t0}&end_ts={t1}&period_interval=1")
        txt = fetch(url, pace=1.15)
        try:
            x = json.loads(txt)
        except Exception:
            out.append({"t": r["t"], "err": txt[:200]}); continue
        cs = x.get("candlesticks") if "candlesticks" in x else (x.get("markets") or [{}])[0].get("candlesticks", [])
        q = {c["end_period_ts"]: candle_quote(c) for c in cs or []}
        ks = sorted(q)
        def at(t):
            i = max([k for k in ks if k <= t], default=None)
            return None if i is None else {"ts": i, "ask": q[i][0], "bid": q[i][1]}
        vol = sum(v[2] for v in q.values())
        rec = {"rule": r["rule"], "t": r["t"], "date": r["date"], "cached_dec_bid": r["bid"], "cached_fill_px": r["px"],
               "fresh_14utc": at(r["t_entry"]), "fresh_15utc": at(r["t_entry"] + 3600), "fresh_minute_candles": len(ks),
               "fresh_volume_4h": vol}
        f15 = rec["fresh_15utc"]
        rec["fresh_fill_px"] = None if not f15 or f15["bid"] is None else round(1 - f15["bid"], 4)
        out.append(rec); print(json.dumps(rec))
    (OUT / "verify.json").write_text(json.dumps(out, indent=1))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    if args[:1] == ["--verify"]:
        verify(int(args[1]) if len(args) > 1 else 8)
        return
    mk, cd = load_pool()
    log = []
    P = lambda *a: (print(*a), log.append(" ".join(str(x) for x in a)))
    P(f"pool: {len(mk)} markets in {len(SERIES)} series, {len(cd)} tickers with hourly candles, caches {len(CACHES)}")
    # deadline audit: parsed D vs close_time on markets that did NOT close early (result no / still active)
    bad = []
    for t, m in mk.items():
        D, src = parse_deadline(m)
        c = iso(m.get("close_time"))
        if D and c and (m.get("result") == "no" or m.get("status") == "active") and abs(D - c) > 1.5 * DAY:
            bad.append((t, src, dt.datetime.utcfromtimestamp(D).isoformat(), m.get("close_time")))
    P(f"deadline audit: {sum(1 for m in mk.values() if parse_deadline(m)[0])} parsed, {len(bad)} NO/active markets with |D - close| > 1.5 d")
    for b in bad[:15]:
        P("   ", *b)

    variants = {"main": dict(thin_with_candles=False, strict_first=False),
                "strict_first_signal": dict(thin_with_candles=False, strict_first=True),
                "thin_among_rungs_with_candles": dict(thin_with_candles=True, strict_first=False)}
    result = {}
    for vname, opt in variants.items():
        ladders, rung = build(mk, cd, opt["thin_with_candles"])
        snaps = snapshots(ladders, cd)
        trades, near_mk = run_rules(snaps, cd, rung, opt["strict_first"])
        disc, val, cut = split_events(near_mk, rung, ladders)
        P(f"\n===== variant {vname}: ladders {len(ladders)}, events with near-rung markets {len(disc) + len(val)} "
          f"(disc {len(disc)}, val {len(val)}); split at latest settled close {dt.datetime.utcfromtimestamp(cut).isoformat() if cut else None}")
        P("  validation events:", ", ".join(sorted(val)))
        P("  near-rung markets: disc", sum(len(near_mk[e]) for e in disc), "val", sum(len(near_mk[e]) for e in val))
        res_v = {}
        for part, evset in (("DISCOVERY", disc), ("VALIDATION", val)):
            P(f" {part}")
            for name in ["B"] + sorted(k for k in trades if k != "B"):
                rows = [r for r in trades[name] if r["ev"] in evset]
                s = stats(rows)
                if name != "B" and s.get("n"):
                    sb = stats([r for r in trades["B"] if r["ev"] in evset])
                    s["incr_vs_B"] = round(s["ret_per_dollar"] - sb["ret_per_dollar"], 4) if sb.get("n") else None
                P(fmt(name, s) + (f" incr {s['incr_vs_B']:+.1%}" if s.get("incr_vs_B") is not None else ""))
                res_v[f"{part}|{name}"] = s
        # alternative split: by each trade's market close (per market, not per ladder), cut at the 70% quantile of
        # near-rung market closes; and by entry date
        allm = sorted({(rung[t]["close"], t) for e in near_mk for t in near_mk[e]})
        cut_m = allm[int(len(allm) * 0.7)][0]
        P(f" ALT SPLIT by near-rung market close (cut {dt.datetime.utcfromtimestamp(cut_m).date()}), validation part:")
        for name in ("B", "S k1.25 ls NO0.30-0.97", "S k1.5 next NO0.30-0.97", "S k1.5 ls NO0.50-0.97"):
            rows = [r for r in trades[name] if rung[r["t"]]["close"] >= cut_m]
            s = stats(rows); P(fmt(name, s)); res_v[f"ALTVAL_marketclose|{name}"] = s
        P(" ALL (discovery + validation):")
        for name in ("B", "S k1.25 ls NO0.30-0.97", "S k1.5 next NO0.30-0.97", "S k1.5 ls NO0.50-0.97"):
            s = stats(trades[name]); P(fmt(name, s)); res_v[f"ALL|{name}"] = s
            ns = stats([r for r in trades[name] if "STARSHIP" not in r["series"]]); P(fmt(name + " non-Starship", ns))
            res_v[f"ALL_nonStarship|{name}"] = ns
        # outcome mix of ladders by split side: share of ladders where the event happened (some rung resolved YES)
        for part, evset in (("disc", disc), ("val", val)):
            hap = sum(1 for e in evset if any(r["res"] == "yes" for r in ladders[e]))
            P(f"  {part}: ladders where the event happened (any rung YES): {hap}/{len(evset)}")
            res_v[f"{part}_event_happened"] = [hap, len(evset)]
        if vname == "main":
            C = ("S k1.25 ls NO0.30-0.97", "S k1.5 next NO0.30-0.97", "S k1.5 ls NO0.50-0.97")
            # paired increment: S trade minus the B trade on the same market
            bmap = {r["t"]: r for r in trades["B"]}
            for part, evset in (("VAL", val), ("ALL", None)):
                for name in C:
                    d = [r["ret"] - bmap[r["t"]]["ret"] for r in trades[name] if r["t"] in bmap and (evset is None or r["ev"] in evset)]
                    P(f"  paired S-B on same market {part} {name}: n={len(d)} mean diff {st.mean(d) if d else float('nan'):+.1%}")
                    res_v[f"paired_{part}|{name}"] = {"n": len(d), "mean_diff": round(st.mean(d), 4) if d else None}
            # entry-time split: the same 70% cut, on the decision time of every near-rung snapshot (tau 1-60)
            ts_all = sorted(s["t"] for ss in snaps.values() for s in ss if 1 <= s["tau"] <= 60 and s["near"]["settled"])
            cut_t = ts_all[int(len(ts_all) * 0.7)]
            P(f" ALT SPLIT by entry time (cut {dt.datetime.utcfromtimestamp(cut_t).date()}), validation part:")
            for name in ("B",) + C:
                s = stats([r for r in trades[name] if r["t_entry"] >= cut_t]); P(fmt(name, s)); res_v[f"ALTVAL_entrytime|{name}"] = s
            # capacity proxy: contracts traded on the near rung in the 24 h before the decision (hourly candle volume)
            for name in ("B",) + C:
                v = []
                for r in trades[name]:
                    if r["ev"] in val:
                        v.append(sum(x[2] for ts, x in cd[r["t"]].items() if r["t_entry"] - DAY < ts <= r["t_entry"]))
                med = st.median(v) if v else None
                P(f"  capacity proxy {name}: median 24h volume before entry = {med} contracts (n={len(v)})")
                res_v[f"cap24h|{name}"] = med
            with open(OUT / "trades.jsonl", "w") as f:
                for name, rows in trades.items():
                    for r in rows:
                        r2 = dict(r); r2["part"] = "val" if r["ev"] in val else "disc"
                        f.write(json.dumps(r2) + "\n")
            P("\n MAIN validation trades of the three frozen candidates and B:")
            for name in ("B", "S k1.25 ls NO0.30-0.97", "S k1.5 next NO0.30-0.97", "S k1.5 ls NO0.50-0.97"):
                P(f"  -- {name}")
                for r in sorted((r for r in trades[name] if r["ev"] in val), key=lambda r: r["t_entry"]):
                    P(f"     {r['t']:<40} {r['date']} tau={r['tau']:5.1f} bid={r['bid']:.2f} q={r['q'] if r['q'] is not None else float('nan'):.3f} px={r['px']:.2f} won={r['won']} ret={r['ret']:+.2f} {r['fill']}")
        result[vname] = {"validation_events": sorted(val), "discovery_events": sorted(disc), "cut": cut, "stats": res_v}
    (OUT / "repro_result.json").write_text(json.dumps(result, indent=1))
    (OUT / "repro.txt").write_text("\n".join(log) + "\n")


if __name__ == "__main__":
    main()
