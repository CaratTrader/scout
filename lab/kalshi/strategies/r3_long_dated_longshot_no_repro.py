"""Adversarial reproduction of r3_long_dated_longshot_no (written from the claim text only, no code reused).

Claim: a taker buying NO at 0.80-0.92 on long-dated Kalshi retail markets (Politics, World, Companies, Science/Tech,
Economics) D days before the scheduled deadline earns >= +10% per trade after fees.

Own implementation:
- Universe: settled binary markets from Kalshi /historical/markets and /markets listings (raw API JSON; the other
  researcher's raw response cache is read as DATA only, plus my own fetches under repro/api_cache).
- Scheduled deadline S: parsed from rules_primary ("before <date>" -> that date 00:00 UTC, "by/on <date>" -> next day
  00:00 UTC, "before YYYY" -> Jan 1, "in YYYY" -> Jan 1 of YYYY+1); fall back to the ticker date; else to close_time
  only if the market cannot close early. All of this is known at listing time.
- Eligible: scheduled life S - open >= 45 days.
- Decision t = S - D days; signal = quote at t (last hourly candle with end <= t, age <= MAXAGE); fill one hour later
  at 1 - yes_bid (NO) / yes_ask (YES) from the last candle <= t+1h; band applied to the SIGNAL price; the market must
  be open at t and still open at the fill (actual close later than the fill; an early YES close means no trade).
- Fee 0.07 p (1-p) per contract, rounded up to the cent per 6-contract order. Return per $ = pnl / px.
- Split: events ordered by (latest) close among the sample; discovery = first 70% of events, validation = rest.
- t clustered by event (mean of event means / (pstdev / sqrt(n_events))), as lab/kalshi/calib.cell_stats.
Usage: .venv/bin/python -m lab.kalshi.strategies.r3_long_dated_longshot_no_repro [--fetch N] [--maxage H]"""
from __future__ import annotations
import calendar, hashlib, json, math, os, re, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
ROOT = Path("/Users/roomyhome/Tradeinc")
SRC = ROOT / "data/kalshi_lab/strategies/r3_long_dated_longshot_no"
OUT = SRC / "repro"
MYCACHE = OUT / "api_cache"
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
CATS = ("Politics", "World", "Companies", "Science and Technology", "Economics")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
DAY = 86400
N_ORDER = 6
CALLS = {"n": 0}


# ---------------------------------------------------------------- raw data access
def _paths(url: str) -> list[Path]:
    out = []
    for cand in (KBASE + url, url):
        for h in (hashlib.sha1(cand.encode()).hexdigest(), hashlib.md5(cand.encode()).hexdigest()):
            out += [SRC / "api_cache" / f"{h}.json", MYCACHE / f"{h}.json"]
    return out


def raw(url: str, allow_fetch: bool = False, budget: int = 0, prefer_mine: bool = False):
    paths = _paths(url)
    if prefer_mine:
        paths = [p for p in paths if MYCACHE in p.parents] + [p for p in paths if MYCACHE not in p.parents]
    for p in paths:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                pass
    if not allow_fetch or CALLS["n"] >= budget:
        return None
    from lab.us.data_refresh import fetch
    CALLS["n"] += 1
    txt = fetch(KBASE + url, pace=1.15, tries=2)
    MYCACHE.mkdir(parents=True, exist_ok=True)
    with (OUT / "calls.log").open("a") as f:
        f.write(f"{int(time.time())}\t{len(txt)}\t{url}\n")
    if not txt:
        CALLS["n"] += 1   # fetch(tries=2) made a second attempt
        return None
    (MYCACHE / f"{hashlib.sha1((KBASE + url).encode()).hexdigest()}.json").write_text(txt)
    return json.loads(txt)


def ts(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "")[:19]
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))


def day0(y: int, m: int, d: int) -> int:
    return calendar.timegm((y, m, d, 0, 0, 0))


# ---------------------------------------------------------------- scheduled deadline
DATE_RX = re.compile(r"\b(before|by|on|through|until)\s+(?:the end of\s+)?([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
YEAR_BEFORE = re.compile(r"\bbefore\s+(\d{4})\b", re.I)
YEAR_IN = re.compile(r"\b(?:in|during)\s+(\d{4})\b", re.I)
TICK_DATE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})$")


def deadline(m: dict) -> tuple[int | None, str]:
    rules = m.get("rules_primary") or ""
    x = DATE_RX.search(rules)
    if x and x.group(2).lower() in MONTHS:
        y, mo, d = int(x.group(4)), MONTHS[x.group(2).lower()], int(x.group(3))
        try:
            base = day0(y, mo, d)
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


# ---------------------------------------------------------------- universe
def listing_urls() -> list[str]:
    urls = []
    for f in (SRC / "calls.log", OUT / "calls.log"):
        if f.exists():
            for line in f.open():
                p = line.rstrip("\n").split("\t")
                if len(p) == 3 and int(p[1]) > 0 and "candlesticks" not in p[2] and "series_ticker=" in p[2] and "/events/" not in p[2]:
                    urls.append(p[2])
    return list(dict.fromkeys(urls))


def series_category() -> dict[str, str]:
    cat = {}
    for c in CATS:
        d = raw(f"/series?category={c.replace(' ', '%20')}&include_volume=true")
        for s in (d or {}).get("series", []):
            cat[s["ticker"]] = c
    return cat


def universe(extra_urls: list[str] = ()) -> dict[str, dict]:
    cat = series_category()
    out = {}
    for u in list(listing_urls()) + list(extra_urls):
        d = raw(u)
        if not d:
            continue
        ser = re.search(r"series_ticker=([A-Z0-9]+)", u).group(1)
        for m in d.get("markets", []):
            if m.get("market_type") != "binary" or m.get("result") not in ("yes", "no"):
                continue
            S, how = deadline(m)
            op, cl = ts(m.get("open_time")), ts(m.get("close_time"))
            if S is None or op is None or cl is None:
                continue
            out[m["ticker"]] = {"t": m["ticker"], "e": m["event_ticker"], "series": ser, "cat": cat.get(ser) or cat.get(ser[2:] if ser.startswith("KX") else "KX" + ser),
                                "open": op, "close": cl, "S": S, "S_how": how, "result": m["result"], "early_flag": bool(m.get("can_close_early")),
                                "vol": float(m.get("volume_fp") or m.get("volume") or 0), "hist": u.startswith("/historical"), "settled": ts(m.get("settlement_ts"))}
    return out


def eligible(U: dict) -> dict:
    return {k: m for k, m in U.items() if m["S"] - m["open"] >= 45 * DAY and m["cat"] in CATS}


# ---------------------------------------------------------------- candles
def _px(side: dict | None, key: str = "close"):
    if not side:
        return None
    v = side.get(key + "_dollars", side.get(key))
    if v is None:
        return None
    v = float(v)
    return v / 100 if v > 1.0001 else v


def parse_candles(lst: list[dict]) -> list[tuple[int, float, float, float]]:
    out = []
    for c in lst:
        a, b = _px(c.get("yes_ask")), _px(c.get("yes_bid"))
        if a is None or b is None:
            continue
        out.append((int(c["end_period_ts"]), a, b, float(c.get("volume_fp") or c.get("volume") or 0)))
    out.sort()
    return out


def candles_for(m: dict, allow_fetch=False, budget=0, prefer_mine=False) -> list | None:
    """Look up any cached hourly candle response for this market (their window or mine); fetch mine if allowed."""
    t = m["t"]
    found = []
    for f in (SRC / "calls.log", OUT / "calls.log"):
        if f.exists():
            for line in f.open():
                p = line.rstrip("\n").split("\t")
                if len(p) == 3 and "candlesticks" in p[2] and (f"/markets/{t}/candlesticks" in p[2] or f"market_tickers={t}&" in p[2]) and int(p[1]) > 0:
                    found.append(p[2])
    for u in (sorted(found, key=lambda u: "historical" not in u) if not prefer_mine else found[::-1]):
        d = raw(u, prefer_mine=prefer_mine)
        if d:
            lst = d.get("candlesticks") if "candlesticks" in d else next((x["candlesticks"] for x in d.get("markets", []) if x.get("market_ticker", t) == t), [])
            return parse_candles(lst)
    if not allow_fetch:
        return None
    start = max(m["open"], m["S"] - 62 * DAY) // 3600 * 3600
    end = min(m["close"], m["S"])
    if end <= start:
        return []
    if m["hist"]:
        u = f"/historical/markets/{t}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60"
        d = raw(u, True, budget)
        return parse_candles(d.get("candlesticks", [])) if d else None
    u = f"/markets/candlesticks?market_tickers={t}&start_ts={start}&end_ts={end}&period_interval=60"
    d = raw(u, True, budget)
    if not d:
        return None
    return parse_candles(next((x["candlesticks"] for x in d.get("markets", [])), []))


def quote(c: list, t: int, maxage_h: float):
    best = None
    for r in c:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > maxage_h * 3600:
        return None
    return best


# ---------------------------------------------------------------- trades and stats
def fee_pc(p: float, n: int = N_ORDER) -> float:
    return math.ceil(round(0.07 * n * p * (1 - p) * 100, 9)) / 100 / n


def trades(M: dict, C: dict, Ds=(60, 45, 30, 21, 14), delay_h=1, maxage_h=72.0) -> list[dict]:
    out = []
    for k, m in M.items():
        c = C.get(k)
        if not c:
            continue
        for D in Ds:
            t = m["S"] - D * DAY
            fill_t = t + delay_h * 3600
            if t < m["open"] or fill_t >= m["close"]:
                continue
            qs, qf = quote(c, t, maxage_h), quote(c, fill_t, maxage_h)
            if not qs or not qf:
                continue
            for side, ps, px, won in (("NO", 1 - qs[2], 1 - qf[2], m["result"] == "no"), ("YES", qs[1], qf[1], m["result"] == "yes")):
                if not (0.01 <= px <= 0.99):
                    continue
                pnl = (1.0 if won else 0.0) - px - fee_pc(px)
                out.append({"t": k, "e": m["e"], "series": m["series"], "D": D, "side": side, "ps": round(ps, 4), "px": round(px, 4), "won": won,
                            "ret": pnl / px, "t_close": m["close"], "fill_ts": fill_t, "months": (m["S"] - fill_t) / (30.44 * DAY)})
    return out


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    ne = len(em)
    sd = st.pstdev(em) if ne > 1 else 0.0
    tt = st.mean(em) / (sd / math.sqrt(ne)) if ne > 2 and sd > 0 else None
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["fill_ts"])
    h = len(srt) // 2
    return {"n": len(rows), "events": ne, "win": round(sum(r["won"] for r in rows) / len(rows), 4), "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(tt, 2) if tt is not None else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in srt[:h]), 4) if h else None, "half2": round(st.mean(r["ret"] for r in srt[h:]), 4)}


def sel(T, side, lo, hi, Ds):
    return [r for r in T if r["side"] == side and lo <= r["ps"] < hi and r["D"] in Ds]


def first_entry(rows):
    best = {}
    for r in sorted(rows, key=lambda r: r["fill_ts"]):
        best.setdefault(r["t"], r)
    return list(best.values())


CANDS = {"C1 NO 0.80-0.92 D60/30/14": ("NO", 0.80, 0.92, (60, 30, 14)),
         "C2 NO 0.70-0.80 D60/45/30/21/14": ("NO", 0.70, 0.80, (60, 45, 30, 21, 14)),
         "C3 NO 0.86-0.92 D60/45/30/21/14": ("NO", 0.86, 0.92, (60, 45, 30, 21, 14))}


def split(T, M, frac=0.7):
    evc = defaultdict(int)
    for m in M.values():
        evc[m["e"]] = max(evc[m["e"]], m["close"])
    closes = sorted(evc.values())
    cut = closes[int(len(closes) * frac)] if closes else 0
    disc = [r for r in T if evc[r["e"]] < cut]
    val = [r for r in T if evc[r["e"]] >= cut]
    return disc, val, cut


def fmt(name, s):
    if not s.get("n"):
        return f"  {name:42} n=0"
    f = lambda v, p=True: ("   nan" if v is None else (f"{v*100:+6.1f}%" if p else f"{v:6.2f}"))
    return (f"  {name:42} n={s['n']:4d} ev={s['events']:3d} win={s['win']*100:5.1f}% px={s['avg_px']:.3f} ret={f(s['ret_per_dollar'])} "
            f"t={f(s['t'], False)} wo3={f(s['ret_wo3'])} halves={f(s['half1'])}/{f(s['half2'])}")


def main():
    args = sys.argv[1:]
    maxage = float(args[args.index("--maxage") + 1]) if "--maxage" in args else 72.0
    OUT.mkdir(parents=True, exist_ok=True)
    U = universe()
    E = eligible(U)
    their = [json.loads(l)["t"] for l in (SRC / "sample.jsonl").open()]
    C = {}
    for k in their:
        if k in U:
            c = candles_for(U[k])
            if c is not None:
                C[k] = c
    lines = [f"universe {len(U)} settled binary markets, eligible (life>=45d, 5 categories) {len(E)}, their sample {len(their)}, with my parse {sum(k in E for k in their)}, candles found {len(C)}"]
    # deadline comparison with theirs
    theirS = {json.loads(l)["t"]: json.loads(l)["S"] for l in (SRC / "sample.jsonl").open()}
    diff = [(k, U[k]["S"], theirS[k], U[k]["S_how"]) for k in their if k in U and abs(U[k]["S"] - theirS[k]) > 3600]
    lines.append(f"deadline differences > 1h vs their S: {len(diff)}")
    for k, a, b, how in diff:
        lines.append(f"    {k:34} mine={time.strftime('%Y-%m-%d %H:%M', time.gmtime(a))} theirs={time.strftime('%Y-%m-%d %H:%M', time.gmtime(b))} via {how}")
    M = {k: U[k] for k in C}
    T = trades(M, C, maxage_h=maxage)
    disc, val, cut = split(T, M)
    lines.append(f"\nREPRO on their 70-market sample, maxage {maxage}h, split at event close {cut} ({time.strftime('%Y-%m-%d', time.gmtime(cut))}); trade rows {len(T)} (disc {len(disc)}, val {len(val)})")
    res = {"maxage_h": maxage, "cut": cut, "discovery": {}, "validation": {}}
    for name, (side, lo, hi, Ds) in CANDS.items():
        sd, sv = stats(sel(disc, side, lo, hi, Ds)), stats(sel(val, side, lo, hi, Ds))
        res["discovery"][name], res["validation"][name] = sd, sv
        lines.append(fmt("DISC " + name, sd))
        lines.append(fmt("VAL  " + name, sv))
        for r in sorted(sel(val, side, lo, hi, Ds), key=lambda r: r["fill_ts"]):
            lines.append(f"        {r['t']:34} D{r['D']} ps={r['ps']:.2f} px={r['px']:.2f} won={r['won']} ret={r['ret']:+.3f}")
    for name, rows in (("DISC NO 0.80-0.92 all D", sel(disc, "NO", .8, .92, (60, 45, 30, 21, 14))),
                       ("DISC NO 0.80-0.92 first-entry", first_entry(sel(disc, "NO", .8, .92, (60, 45, 30, 21, 14)))),
                       ("ALL  NO 0.80-0.92 D60/30/14", sel(T, "NO", .8, .92, (60, 30, 14))),
                       ("ALL  NO 0.80-0.92 first-entry", first_entry(sel(T, "NO", .8, .92, (60, 45, 30, 21, 14)))),
                       ("ALL  NO 0.70-0.97 first-entry", first_entry(sel(T, "NO", .7, .97, (60, 45, 30, 21, 14)))),
                       ("ALL  YES 0.03-0.20 D60/30/14", sel(T, "YES", .03, .20, (60, 30, 14)))):
        lines.append(fmt(name, stats(rows)))
    (OUT / f"repro_maxage{int(maxage)}.json").write_text(json.dumps(res, indent=1))
    (OUT / f"trades_maxage{int(maxage)}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in T))
    txt = "\n".join(lines)
    (OUT / f"analysis_maxage{int(maxage)}.txt").write_text(txt + "\n")
    print(txt)


USED16 = ("KXFEDCHAIRNOM", "KXGOVSHUT", "KXTRUMPPARDON", "KXCABOUT", "KXTRUMPADMINLEAVE", "KXUSAIRANAGREEMENT", "KXNOBELPEACE", "KXGREENLAND",
          "KXLLM1", "KXALIENS", "KXSPACEXSTARSHIP", "KXOAIAGI", "KXTIKTOKBAN", "KXOAIPROFIT", "KXRATECUTCOUNT", "KXLAYOFFSYINFO")
VERIFY = ("KXRATECUTCOUNT-25DEC31-T2", "KXUSAIRANAGREEMENT-27-26APR", "KXUSAIRANAGREEMENT-27-26JUN", "KXGOVSHUT-26JAN31")


def tradeable(m):
    return any(m["S"] - D * DAY >= m["open"] and m["S"] - D * DAY + 3600 < m["close"] for D in (60, 45, 30, 21, 14))


def pick(markets: list[dict], per_event=2) -> list[dict]:
    by = defaultdict(list)
    for m in markets:
        by[m["e"]].append(m)
    out = []
    for e, v in sorted(by.items()):
        out += sorted(v, key=lambda m: hashlib.sha1(m["t"].encode()).hexdigest())[:per_event]
    return out


def verify(budget):
    """Re-fetch the validation markets' candles myself (different window -> uncached URL) and compare quotes."""
    U = universe()
    rep = []
    for k in VERIFY:
        m = U[k]
        start = (max(m["open"], m["S"] - 61 * DAY) // 3600 + 1) * 3600
        end = min(m["close"], m["S"])
        u = f"/historical/markets/{k}/candlesticks?start_ts={start}&end_ts={end}&period_interval=60"
        d = raw(u, True, budget)
        mine = parse_candles(d.get("candlesticks", [])) if d else []
        theirs = candles_for(m)
        diffs = 0; checked = 0
        for D in (60, 45, 30, 21, 14):
            t = m["S"] - D * DAY
            for tt in (t, t + 3600):
                a, b = quote(mine, tt, 72), quote(theirs or [], tt, 72)
                if a and b:
                    checked += 1; diffs += (abs(a[2] - b[2]) > 1e-9 or abs(a[1] - b[1]) > 1e-9)
        rep.append({"t": k, "mine_candles": len(mine), "cached_candles": len(theirs or []), "quotes_checked": checked, "quote_mismatches": diffs})
    return rep


def expand(budget=196):
    OUT.mkdir(parents=True, exist_ok=True)
    log = {"verify": verify(budget)}
    print("verify", log["verify"], "calls", CALLS["n"], flush=True)
    rows = []
    for c in CATS:
        for s in (raw(f"/series?category={c.replace(' ', '%20')}&include_volume=true") or {}).get("series", []):
            if s.get("frequency") in ("one_off", "annual", "custom") and s["ticker"] not in USED16:
                rows.append((float(s.get("volume_fp") or 0), s["ticker"]))
    new = [t for _, t in sorted(rows, reverse=True)[:20]]
    log["new_series"] = new
    for s in new:
        raw(f"/historical/markets?series_ticker={s}&limit=1000", True, budget)
    print("listed", new, "calls", CALLS["n"], flush=True)
    U = universe()
    E = eligible(U)
    their = {json.loads(l)["t"] for l in (SRC / "sample.jsonl").open()}
    tier1 = pick([m for m in E.values() if m["series"] in new and tradeable(m)])
    tier2 = pick([m for m in E.values() if m["series"] in USED16 and m["t"] not in their and tradeable(m)])
    log["tier1_markets"] = len(tier1); log["tier2_markets"] = len(tier2)
    got = {}
    for m in tier1 + tier2:
        if CALLS["n"] >= budget:
            break
        c = candles_for(m, True, budget)
        if c is not None:
            got[m["t"]] = c
    log["fetched_markets"] = len(got); log["calls"] = CALLS["n"]
    (OUT / "expansion_log.json").write_text(json.dumps(log, indent=1))
    print(log, flush=True)


def evaluate_expansion(maxage=72.0):
    U = universe()
    E = eligible(U)
    their = {json.loads(l)["t"] for l in (SRC / "sample.jsonl").open()}
    C = {}
    for k, m in E.items():
        if k in their:
            continue
        c = candles_for(m, prefer_mine=True)
        if c:
            C[k] = c
    M = {k: U[k] for k in C}
    T = trades(M, C, maxage_h=maxage)
    cut = 1761919200
    lines = [f"EXPANSION (markets never sampled by the original researcher): {len(M)} markets with candles, {len({m['e'] for m in M.values()})} events, "
             f"series {sorted({m['series'] for m in M.values()})}; trade rows {len(T)}"]
    res = {}
    for name, (side, lo, hi, Ds) in CANDS.items():
        rows = sel(T, side, lo, hi, Ds)
        a = stats(rows); b = stats([r for r in rows if r["t_close"] < cut]); v = stats([r for r in rows if r["t_close"] >= cut])
        res[name] = {"all": a, "pre_cut": b, "post_cut": v}
        lines += [fmt("ALL  " + name, a), fmt("PRE  " + name, b), fmt("POST " + name, v)]
        for r in sorted(rows, key=lambda r: r["fill_ts"]):
            lines.append(f"        {r['t']:40} D{r['D']} ps={r['ps']:.2f} px={r['px']:.2f} won={r['won']} ret={r['ret']:+.3f}")
    ext = {"NO 0.80-0.92 all D first-entry": first_entry(sel(T, "NO", .8, .92, (60, 45, 30, 21, 14))),
           "NO 0.80-0.92 all D": sel(T, "NO", .8, .92, (60, 45, 30, 21, 14)),
           "NO 0.92-0.97 D60/30/14": sel(T, "NO", .92, .97, (60, 30, 14)),
           "NO 0.70-0.97 first-entry": first_entry(sel(T, "NO", .7, .97, (60, 45, 30, 21, 14))),
           "YES 0.80-0.92 D60/30/14": sel(T, "YES", .8, .92, (60, 30, 14)),
           "YES 0.03-0.20 D60/30/14": sel(T, "YES", .03, .20, (60, 30, 14))}
    for name, rows in ext.items():
        res[name] = stats(rows); lines.append(fmt(name, res[name]))
    (OUT / "expansion_result.json").write_text(json.dumps(res, indent=1))
    (OUT / "expansion_trades.jsonl").write_text("".join(json.dumps(r) + "\n" for r in T))
    txt = "\n".join(lines)
    (OUT / "expansion_analysis.txt").write_text(txt + "\n")
    print(txt)
    return M, C, T


if __name__ == "__main__":
    if "--expand" in sys.argv:
        expand(int(sys.argv[sys.argv.index("--expand") + 1]))
    elif "--eval-expansion" in sys.argv:
        evaluate_expansion()
    else:
        main()


def expand2(budget_total=196, ranks=(20, 30)):
    """Extension 2 (pre-registered addendum): series ranked 21-30, same sampling; separate from expansion 1."""
    prev = sum(1 for _ in (OUT / "calls.log").open()) if (OUT / "calls.log").exists() else 0
    CALLS["n"] = prev
    rows = []
    for c in CATS:
        for s in (raw(f"/series?category={c.replace(' ', '%20')}&include_volume=true") or {}).get("series", []):
            if s.get("frequency") in ("one_off", "annual", "custom") and s["ticker"] not in USED16:
                rows.append((float(s.get("volume_fp") or 0), s["ticker"]))
    new = [t for _, t in sorted(rows, reverse=True)[ranks[0]:ranks[1]]]
    for s in new:
        raw(f"/historical/markets?series_ticker={s}&limit=1000", True, budget_total)
    U = universe(); E = eligible(U)
    tier = pick([m for m in E.values() if m["series"] in new and tradeable(m)])
    got = 0
    for m in tier:
        if CALLS["n"] >= budget_total:
            break
        if candles_for(m, True, budget_total) is not None:
            got += 1
    log = {"series": new, "markets": len(tier), "fetched": got, "calls_total": CALLS["n"]}
    (OUT / "expansion2_log.json").write_text(json.dumps(log, indent=1))
    print(log)


if __name__ == "__main__" and "--expand2" in sys.argv:
    expand2()


EXT2 = ('KXBIDENPARDON', 'KXRATECUT', 'KXTRUMPATTEND', 'KXLEAVEPOWELL', 'KXTRUMPOUT', 'KXCRYPTOSTRUCTURE', 'KXRECSSNBER', 'KXFEDCHAIRCONFIRM',
        'KXTRUMPAPPROVE', 'KXPRESNOMFEDCHAIR')


def report():
    """Pooled view: their sample (reproduced), expansion 1, extension 2, and all Kalshi together."""
    M, C, T = evaluate_expansion()
    U = universe()
    their_T = [json.loads(l) for l in (OUT / "trades_maxage72.jsonl").open()]
    groups = {"their_sample": their_T, "expansion1": [r for r in T if r["series"] not in EXT2], "extension2": [r for r in T if r["series"] in EXT2]}
    groups["expansion_all"] = T
    groups["all_kalshi"] = their_T + T
    mac = lambda r: "GDP" in r["series"] or "FED" in r["series"] or "RATECUT" in r["series"] or "RECSS" in r["series"]
    out = {}
    lines = []
    for g, rows in groups.items():
        out[g] = {}
        for name, (side, lo, hi, Ds) in CANDS.items():
            s = sel(rows, side, lo, hi, Ds)
            out[g][name] = stats(s)
            lines.append(fmt(f"{g:13} {name}", out[g][name]))
        c1 = sel(rows, "NO", .8, .92, (60, 30, 14))
        pe = {}
        for r in sorted(c1, key=lambda r: r["fill_ts"]):
            pe.setdefault(r["e"], r)
        out[g]["C1 one trade per event"] = stats(list(pe.values()))
        out[g]["C1 macro (GDP/Fed/rates/recession)"] = stats([r for r in c1 if mac(r)])
        out[g]["C1 non-macro"] = stats([r for r in c1 if not mac(r)])
        for k in ("C1 one trade per event", "C1 macro (GDP/Fed/rates/recession)", "C1 non-macro"):
            lines.append(fmt(f"{g:13} {k}", out[g][k]))
    # exact binomial tail on unique events (all Kalshi, first C1 trade per event): P(<= observed losses | loss prob)
    c1 = sel(groups["all_kalshi"], "NO", .8, .92, (60, 30, 14))
    pe = {}
    for r in sorted(c1, key=lambda r: r["fill_ts"]):
        pe.setdefault(r["e"], r)
    ev = list(pe.values()); n = len(ev); L = sum(not r["won"] for r in ev); px = st.mean(r["px"] for r in ev)
    def tail(q):
        return sum(math.comb(n, k) * q ** k * (1 - q) ** (n - k) for k in range(L + 1))
    be10 = 1 - (1.10 * px + fee_pc(px))   # loss rate at which the mean return is exactly +10%
    fair = 1 - px
    out["binomial_events"] = {"n_events": n, "losses": L, "avg_px": round(px, 4), "loss_rate_fair": round(fair, 4), "p_le_obs_if_fair": round(tail(fair), 4),
                              "loss_rate_for_+3pct": round(1 - (1.03 * px + fee_pc(px)), 4), "p_le_obs_if_+3pct": round(tail(1 - (1.03 * px + fee_pc(px))), 4),
                              "loss_rate_for_+10pct": round(be10, 4), "p_le_obs_if_+10pct": round(tail(be10), 4)}
    lines.append("binomial on unique events (all Kalshi C1): " + json.dumps(out["binomial_events"]))
    (OUT / "pooled_result.json").write_text(json.dumps(out, indent=1))
    (OUT / "pooled_analysis.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__" and "--report" in sys.argv:
    report()
