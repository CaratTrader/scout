"""Adversarial reproduction of r3_earnings_call_mentions, rule C1 (listing-time maker NO on KXEARNINGSMENTION*).

Written from the claim's description only; none of the original researcher's code is imported or copied.
Inputs are the RAW Kalshi API responses that researcher cached (data/kalshi_lab/strategies/r3_earnings_call_mentions/
api_cache/<sha1(path)[:24]>.json, request log calls.log). No new Kalshi requests are made.

C1 as described: at the first top-of-hour >= open_time + 1 h of each market, if 0 < yes_bid < yes_ask < 1 and the
spread is >= 2c, rest a SELL-YES (= BUY-NO at 1 - s) at s = yes_ask - 0.01. The order is cancelled before the call:
15:00 ET on the call day for afternoon calls, 07:00 ET for morning calls (also never later than one hour before the
detected call hour, nor after the first close of any market of the event). Filled if a trade printed at yes price
>= s (any-print) or > s (through-only) in a candle that lies wholly inside (post, cancel]. Maker fee 0 (quadratic).
Return per $ = ((1 if NO else 0) - (1 - s)) / (1 - s), equal stakes, t clustered by event (lab.kalshi.calib.cell_stats).

The call is located (as the claim says it had to be, because occurrence_datetime is overwritten after settlement) from
the first hourly price jump: the first hour in which >= JUMP_MIN markets of the event print a trade or a mid at >= 0.90
after their mid was <= 0.60 one-to-six hours before. This uses post-call data only to place the cancel (a proxy for
the schedule that a live bot reads before the call); robustness to the locator is reported.

Usage: .venv/bin/python -m lab.kalshi.strategies.r3_earnings_call_mentions_repro [run|calls|detail]"""
from __future__ import annotations
import hashlib, json, math, os, statistics as st, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import cell_stats

SRC = Path("data/kalshi_lab/strategies/r3_earnings_call_mentions")
OUT = SRC / "repro"
ET = ZoneInfo("America/New_York")
SPLIT = 0.7
JUMP_MIN = 2


def iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def et(t: int) -> datetime:
    return datetime.fromtimestamp(t, tz=ET)


def f(x):
    return None if x in (None, "") else float(x)


# ---------------------------------------------------------------- raw data
def load_raw():
    """markets (live /markets responses only), hourly and daily candles per ticker, trade prints per ticker."""
    path_of = {}
    for line in (SRC / "calls.log").open():
        p = line.rstrip("\n").split("\t")[2]
        path_of[hashlib.sha1(p.encode()).hexdigest()[:24]] = p
    markets, hourly, daily, trades = {}, defaultdict(dict), defaultdict(dict), defaultdict(list)
    for fn in sorted(os.listdir(SRC / "api_cache")):
        x = json.load(open(SRC / "api_cache" / fn))
        p = path_of.get(fn[:-5], "")
        if isinstance(x, dict) and x.get("markets") and "candlesticks" in x["markets"][0]:
            ends = sorted({c["end_period_ts"] for m in x["markets"] for c in m["candlesticks"]})
            g = 0
            for a, b in zip(ends, ends[1:]):
                g = math.gcd(g, b - a)
            per = 86400 if g and g % 86400 == 0 else 3600
            assert g % 3600 == 0, (fn, g)
            dst = daily if per == 86400 else hourly
            for m in x["markets"]:
                for c in m["candlesticks"]:
                    pr = c.get("price") or {}
                    dst[m["market_ticker"]][c["end_period_ts"]] = (
                        f(c["yes_ask"].get("close_dollars")), f(c["yes_bid"].get("close_dollars")),
                        f(pr.get("high_dollars")), f(pr.get("close_dollars")), f(c.get("volume_fp")) or 0.0,
                        f(c["yes_bid"].get("high_dollars")), f(c["yes_ask"].get("low_dollars")))
        elif isinstance(x, dict) and "trades" in x:
            for tr in x["trades"]:
                trades[tr["ticker"]].append(tr)
        elif isinstance(x, dict) and "markets" in x and p.startswith("/markets?"):
            for m in x["markets"]:
                markets[m["ticker"]] = m
    return markets, {k: sorted(v.items()) for k, v in hourly.items()}, {k: sorted(v.items()) for k, v in daily.items()}, trades


def universe(markets, hourly):
    ev = defaultdict(list)
    for m in markets.values():
        if m["event_ticker"].startswith("KXEARNINGSMENTION"):
            ev[m["event_ticker"]].append(m)
    out = {}
    for e, ms in ev.items():
        if all(m["result"] in ("yes", "no") for m in ms) and any(m["ticker"] in hourly for m in ms):
            out[e] = ms
    return out


# ---------------------------------------------------------------- call locator
def mid(c):
    a, b = c[0], c[1]
    if a is None or b is None or not (0 <= b < a <= 1):
        return None
    return (a + b) / 2


def locate_call(ms, hourly, jump_min=JUMP_MIN, hi_thr=0.90, lo_thr=0.60, lookback_h=30):
    """End ts of the first hour where >= jump_min markets go from mid <= lo_thr (1-6 h before) to a print/mid >= hi_thr,
    searched only in the lookback_h hours before the event's first market close (markets close 0.5-27 h after the call;
    earlier hours are dominated by the seeded placeholder book flickering)."""
    hits = defaultdict(int)
    first_close = min(iso(m["close_time"]) for m in ms)
    for m in ms:
        cs = hourly.get(m["ticker"], [])
        hist = []   # (end, mid) carried
        for end, c in cs:
            md = mid(c)
            prev = [v for (t, v) in hist if end - 6 * 3600 <= t <= end - 3600 and v is not None]
            hit_now = (c[2] is not None and c[2] >= hi_thr) or (md is not None and md >= hi_thr)
            if prev and min(prev) <= lo_thr and hit_now and first_close - lookback_h * 3600 <= end <= first_close + 3600:
                hits[end] += 1
            hist.append((end, md))
    for end in sorted(hits):
        if hits[end] >= jump_min:
            return end
    return None


CASCADE = ((2, 0.90, 0.60), (1, 0.90, 0.60), (1, 0.85, 0.70))


def locate_call_cascade(ms, hourly, jump_min=JUMP_MIN):
    """Strict locator first; looser settings only for events where it finds nothing."""
    casc = CASCADE if jump_min == JUMP_MIN else ((jump_min, 0.90, 0.60),) + CASCADE[1:]
    for jm, hi, lo in casc:
        ce = locate_call(ms, hourly, jm, hi, lo)
        if ce is not None:
            return ce
    return None


def cancel_time(call_end, min_close, mode="rule"):
    """15:00 ET on the call day (afternoon call) or 07:00 ET (morning), never later than call start - 1 h or the first close."""
    start = call_end - 3600            # the jump hour begins here; the call started at or before its end
    d = et(start)
    if d.hour >= 12:
        c = d.replace(hour=15, minute=0, second=0, microsecond=0)
    else:
        c = d.replace(hour=7, minute=0, second=0, microsecond=0)
    c = int(c.timestamp())
    if mode == "rule":
        c = min(c, start - 3600)
    elif mode == "rule_nocap":
        pass
    elif mode == "call_minus_1h":
        c = start - 3600
    elif mode == "hold":
        c = min_close
    return min(c, min_close)


# ---------------------------------------------------------------- C1 simulation
def quote_at(cs, t):
    best = None
    for end, c in cs:
        if end <= t:
            best = (end, c)
        else:
            break
    return best


def simulate(ev, hourly, daily, delay_h=0, cancel_mode="rule", jump_min=JUMP_MIN, spread_min=0.02):
    rows, skipped = [], defaultdict(int)
    calls = {}
    for e, ms in ev.items():
        call_end = locate_call_cascade(ms, hourly, jump_min)
        calls[e] = call_end
        if call_end is None:
            skipped["no_call_located"] += len(ms); continue
        min_close = min(iso(m["close_time"]) for m in ms)
        cancel = cancel_time(call_end, min_close, cancel_mode)
        e_close = max(iso(m["close_time"]) for m in ms)
        for m in ms:
            tk = m["ticker"]; op = iso(m["open_time"])
            t0 = -(-(op + 3600) // 3600) * 3600 + delay_h * 3600
            if t0 >= cancel:
                skipped["post_after_cancel"] += 1; continue
            cs = hourly.get(tk, [])
            q = quote_at(cs, t0)
            if q is None or q[0] < op:
                skipped["no_hourly_quote_at_post"] += 1; continue
            if t0 - q[0] > 48 * 3600:
                skipped["stale_quote"] += 1; continue
            ask, bid = q[1][0], q[1][1]
            if ask is None or bid is None or not (0 < bid < ask < 1) or ask - bid < spread_min - 1e-9:
                skipped["no_signal"] += 1; continue
            s = round(ask - 0.01, 2)
            n_ord = math.floor(5 / (1 - s))
            # fill evidence: candles wholly inside (t0, cancel]
            any_hi, thr_hi, vol_at, bid_cross, first_fill = False, False, 0.0, False, None
            hr_cover = set()
            for end, c in cs:
                if end - 3600 >= t0 and end <= cancel:
                    hr_cover.add(end)
                    if c[2] is not None and c[2] >= s - 1e-9:
                        any_hi = True; vol_at += c[4]; first_fill = first_fill or end
                    if c[2] is not None and c[2] > s + 1e-9:
                        thr_hi = True
                    if c[5] is not None and c[5] >= s - 1e-9:
                        bid_cross = True
            for end, c in daily.get(tk, []):
                if end - 86400 >= t0 and end <= cancel:
                    # skip days fully covered by hourly candles already counted (avoid double-counting volume)
                    covered = any(end - 86400 < h <= end for h in hr_cover)
                    if c[2] is not None and c[2] >= s - 1e-9:
                        any_hi = True; first_fill = first_fill or end
                        if not covered:
                            vol_at += c[4]
                    if c[2] is not None and c[2] > s + 1e-9:
                        thr_hi = True
            no_won = m["result"] == "no"
            px = 1 - s
            rows.append({"e": e, "t": tk, "word": m.get("yes_sub_title"), "t_close": e_close, "post": t0, "cancel": cancel,
                         "s": s, "px": round(px, 4), "won": no_won, "ret": ((1.0 if no_won else 0.0) - px) / px,
                         "fill_any": any_hi, "fill_thr": thr_hi, "bid_cross": bid_cross, "n_order": n_ord,
                         "vol_proxy": min(n_ord, vol_at), "first_fill": first_fill, "spread": round(ask - bid, 2)})
    return rows, skipped, calls


def split(ev):
    order = sorted(ev, key=lambda e: max(iso(m["close_time"]) for m in ev[e]))
    cut = int(len(order) * SPLIT)
    return set(order[:cut]), set(order[cut:]), order


def summarize(rows, label=""):
    if not rows:
        return {"n": 0}
    v = cell_stats(rows)
    srt = sorted(rows, key=lambda r: r["t_close"]); mid_t = srt[len(srt) // 2]["t_close"]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid_t]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid_t]
    v["half1"] = st.mean(h1) if h1 else None; v["half2"] = st.mean(h2) if h2 else None
    w = [r for r in rows if r.get("vol_proxy")]
    if w:
        stake = sum(r["vol_proxy"] * r["px"] for r in w)
        v["size_weighted_volproxy"] = sum(r["vol_proxy"] * ((1.0 if r["won"] else 0.0) - r["px"]) for r in w) / stake
    v["label"] = label
    return v


def main(arg="run"):
    OUT.mkdir(parents=True, exist_ok=True)
    markets, hourly, daily, trades = load_raw()
    ev = universe(markets, hourly)
    disc, val, order = split(ev)
    print(f"events {len(ev)} (disc {len(disc)}, val {len(val)}); markets {sum(len(v) for v in ev.values())}")
    print("validation events:", [e.replace('KXEARNINGSMENTION', '') for e in order if e in val])
    rows, skipped, calls = simulate(ev, hourly, daily)
    if arg == "calls":
        for e in order:
            ce = calls[e]; ms = ev[e]
            mc = min(iso(m["close_time"]) for m in ms)
            print(f"{e:40s} call_end {et(ce).strftime('%m-%d %H:%M ET') if ce else 'None':18s} first_close {et(mc).strftime('%m-%d %H:%M ET')}"
                  f"  cancel {et(cancel_time(ce, mc)).strftime('%m-%d %H:%M') if ce else '-'}  {'VAL' if e in val else 'disc'}")
        return
    print("skipped:", dict(skipped))
    res = {}
    for nm, sel in (("discovery", disc), ("validation", val), ("pooled", disc | val)):
        posted = [r for r in rows if r["e"] in sel]
        for fill in ("fill_any", "fill_thr"):
            fl = [r for r in posted if r[fill]]
            unf = [r for r in posted if not r[fill]]
            v = summarize(fl, f"{nm} {fill}")
            v["posted"] = len(posted); v["filled_NO_win"] = v.get("win"); v["unfilled_NO_win"] = (sum(r["won"] for r in unf) / len(unf)) if unf else None
            if fl:
                days = (max(r["t_close"] for r in fl) - min(r["t_close"] for r in fl)) / 86400
                v["median_vol_proxy_contracts"] = st.median(r["vol_proxy"] for r in fl)
            res[f"{nm}|{fill}"] = v
            print(f"{nm:10s} {fill:8s} posted {len(posted):4d} fills {v.get('n',0):4d} ev {v.get('events',0):3d} win {v.get('win',0):.3f} "
                  f"px {v.get('px',0):.3f} ret {v.get('ret',0):+.4f} t {v.get('t',float('nan')):5.2f} wo3 {v.get('ret_wo3',float('nan')):+.3f} "
                  f"h1 {v.get('half1') or 0:+.3f} h2 {v.get('half2') or 0:+.3f} unfilledNOwin {v['unfilled_NO_win'] or 0:.3f} "
                  f"sw {v.get('size_weighted_volproxy', float('nan')):+.3f}")
    (OUT / "rows_base.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (OUT / "base.json").write_text(json.dumps({"skipped": skipped, "results": res,
                                               "calls": {e: calls[e] for e in order}, "validation_events": sorted(val)}, indent=1))
    return res


def detail():
    """Adversarial diagnostics on the validation fills (run once, after the base validation number is recorded)."""
    markets, hourly, daily, trades = load_raw()
    ev = universe(markets, hourly)
    disc, val, order = split(ev)
    out = {}
    rows, _, calls = simulate(ev, hourly, daily)
    V = [r for r in rows if r["e"] in val and r["fill_any"]]
    D = [r for r in rows if r["e"] in disc and r["fill_any"]]
    # 1. per event
    print("PER EVENT (validation, any-print fills)")
    pe = {}
    for e in order:
        if e not in val:
            continue
        x = [r for r in V if r["e"] == e]; posted = [r for r in rows if r["e"] == e]
        if x:
            pe[e] = {"posted": len(posted), "fills": len(x), "no_win": sum(r["won"] for r in x), "mean_ret": st.mean(r["ret"] for r in x),
                     "sum_ret": sum(r["ret"] for r in x), "avg_px": st.mean(r["px"] for r in x)}
            print(f"  {e[17:]:16s} posted {len(posted):3d} fills {len(x):3d} NOwins {pe[e]['no_win']:3d} avgpx {pe[e]['avg_px']:.3f} mean {pe[e]['mean_ret']:+.3f} sum {pe[e]['sum_ret']:+.2f}")
    out["per_event_validation"] = pe
    em = sorted((v["mean_ret"] for v in pe.values()), reverse=True)
    print(f"  event means sorted: {[round(x, 2) for x in em]}")
    print(f"  equal-event mean {st.mean(em):+.3f}; without best 2 events {st.mean(em[2:]):+.3f}; events positive {sum(x > 0 for x in em)}/{len(em)}")
    out["event_mean"] = st.mean(em); out["event_mean_wo_best2"] = st.mean(em[2:])
    # 2. NO-price buckets
    print("NO-PRICE BUCKETS (fills)")
    bk = {}
    for nm, R in (("disc", D), ("val", V)):
        for lo, hi in ((0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 1.01)):
            x = [r for r in R if lo <= r["px"] < hi]
            if x:
                v = cell_stats(x); bk[f"{nm}|{lo}-{hi}"] = v
                print(f"  {nm} px {lo:.2f}-{hi:.2f} n {v['n']:3d} ev {v['events']:2d} NOwin {v['win']:.2f} px {v['px']:.3f} ret {v['ret']:+.3f} t {v['t']:.2f}")
    out["px_buckets"] = bk
    # 3. timing of first fill
    print("FIRST-FILL TIMING")
    tm = {}
    for nm, R in (("disc", D), ("val", V)):
        for lab, lo, hi in (("<=48h", 0, 48), (">48h", 48, 1e9)):
            x = [r for r in R if lo * 3600 < r["first_fill"] - r["post"] <= hi * 3600]
            if x:
                v = cell_stats(x); tm[f"{nm}|{lab}"] = v
                print(f"  {nm} {lab:6s} n {v['n']:3d} ev {v['events']:2d} NOwin {v['win']:.2f} px {v['px']:.3f} ret {v['ret']:+.3f} t {v['t']:.2f}")
    out["fill_timing"] = tm
    # 4. sensitivities (each a full re-simulation; reported, never chosen)
    print("SENSITIVITY (validation | discovery), any-print")
    sens = {}
    variants = [("base", {}), ("cancel_nocap", {"cancel_mode": "rule_nocap"}), ("cancel_call-1h", {"cancel_mode": "call_minus_1h"}),
                ("hold_to_first_close", {"cancel_mode": "hold"}), ("delay+2h", {"delay_h": 2}), ("delay+5h", {"delay_h": 5}),
                ("jump_min3", {"jump_min": 3}), ("spread>=10c", {"spread_min": 0.10})]
    for nm, kw in variants:
        R, _, _ = simulate(ev, hourly, daily, **kw)
        res = []
        for sel in (val, disc):
            x = [r for r in R if r["e"] in sel and r["fill_any"]]
            res.append(summarize(x) if x else {"n": 0})
        sens[nm] = {"validation": res[0], "discovery": res[1]}
        a, b = res
        print(f"  {nm:20s} val n {a['n']:3d} ret {a.get('ret', 0):+.3f} t {a.get('t', 0):.2f} wo3 {a.get('ret_wo3', 0):+.3f} | disc n {b['n']:3d} ret {b.get('ret', 0):+.3f} t {b.get('t', 0):.2f}")
    out["sensitivity"] = sens
    # 5. cancel-day sanity: fills whose first fill candle ends after the detected call start (should be none)
    late = [r for r in rows if r["fill_any"] and calls[r["e"]] and r["first_fill"] > calls[r["e"]] - 3600]
    print(f"fills after detected call start: {len(late)}")
    out["fills_after_call_start"] = len(late)
    (OUT / "detail.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    if sys.argv[1:2] == ["detail"]:
        detail(); sys.exit()
    main(*(sys.argv[1:] or ["run"]))
