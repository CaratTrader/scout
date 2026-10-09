"""Data for r4_nested_deadline_term_structure: nested 'by date' ladders (one event, rungs 'X happens before D_j').

Universe choice uses listing fields only (series title in the /series catalog, volume, rule text), never prices or
results:
  step list   live listing /markets?series_ticker=S (open + settled since the 2026-08-09 archive cutoff) and archive
              listing /historical/markets?series_ticker=S, for the by-date series below; markets already listed by
              earlier researchers are re-used from their caches (0 calls).
  step events nested-ladder detection (r4_nested_deadline_term_structure_parse.nested_events) -> events.json
  step candles hourly candles: live-tier events with one /series/{s}/events/{e}/candlesticks call (all rungs);
              archived rungs with one /historical/markets/{t}/candlesticks call each. Window per rung:
              [max(open, first deadline in the event - 62 d), min(close, own deadline)].
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_nested_deadline_term_structure_data [list|events|candles N|status]"""
from __future__ import annotations
import datetime as dt, json, re, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_nested_deadline_term_structure_api import kget, used, OUT
from lab.kalshi.strategies.r4_nested_deadline_term_structure_cache import index, load
from lab.kalshi.strategies.r4_nested_deadline_term_structure_parse import compact, nested_events, thin

CUTOFF = int(dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc).timestamp())
DAY, H = 86400, 3600
WIN = 62 * DAY
# By-date series picked from the cached /series catalogs by title ('When will ...', '... out as ...?', 'before ...') and
# lifetime volume, before any listing, price or result of them was read. Already-cached series are re-used for free.
LIVE_SERIES = ["KXBONDIOUT", "KXGABBARDOUT", "KXKASHOUT", "KXNOEMOUT", "KXHEGSETHOUT", "KXLEAVEWALZ", "KXGOVTFUND",
               "KXSHUTDOWNBYDATE", "KXDHSFUNDING", "KXSENATEREC", "KXFISAEXTEND", "KXTRUMPCHINA", "KXWARSHNOM",
               "KXLEAVELISACOOK", "KXSWALWELLOUT", "KXLEAVEPOWELLGOV", "KXPAHLAVIVISITA", "KXVOTEFUNDING",
               "KXTARIFFDECISIONRELEASE", "KXLUTNICKOUT", "KXAGCONF", "KXUSAIRANAGREEMENT", "KXHORMUZNORM", "KXDHSFUND",
               "KXGREENLAND", "KXALIENS", "KXTRUMPOUT27"]
HIST_SERIES: list[str] = []          # filled after the live listing (series whose events started before the cutoff)
MF = OUT / "markets.jsonl"; EF = OUT / "events.json"; CF = OUT / "candles.jsonl"


def _listings_on_disk() -> dict[str, dict]:
    M = {}
    for p, f in index().items():
        if p.startswith("/historical/markets?") or p.startswith("/markets?"):
            tier = "hist" if p.startswith("/historical") else "live"
            for m in load(f).get("markets", []) or []:
                M.setdefault(m["ticker"], compact(m, tier))
    return M


def list_markets(series: list[str], hist: bool) -> None:
    M = {json.loads(l)["t"]: json.loads(l) for l in MF.open()} if MF.exists() else _listings_on_disk()
    for s in series:
        cursor = None
        for _ in range(3):
            path = (f"/historical/markets?series_ticker={s}&limit=1000" if hist else f"/markets?series_ticker={s}&limit=1000")
            d = kget(path + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets", []) or []:
                M[m["ticker"]] = compact(m, "hist" if hist else "live")
            cursor = d.get("cursor")
            if not cursor or len(d.get("markets", []) or []) < 1000:
                break
        print(f"{s:26s} {'hist' if hist else 'live'} markets so far {len(M):6d} calls {used()}", flush=True)
    with MF.open("w") as f:
        for r in M.values():
            f.write(json.dumps(r) + "\n")


def events() -> dict:
    M = [json.loads(l) for l in MF.open()]
    ne = nested_events(M)
    out = {}
    for e, rs in ne.items():
        out[e] = {"series": rs[0]["series"], "rungs": [r["t"] for r in rs], "D": [r["D"] for r in rs],
                  "results": [r["result"] for r in rs], "tiers": [r["tier"] for r in rs], "vol": sum(r["vol"] for r in rs),
                  "first_D": rs[0]["D"], "last_D": rs[-1]["D"], "title": rs[0]["title"]}
    EF.write_text(json.dumps(out, indent=1))
    return out


def load_candles() -> dict[str, list]:
    out = {}
    if CF.exists():
        for l in CF.open():
            x = json.loads(l)
            out.setdefault(x["t"], [])
            out[x["t"]] = merge(out[x["t"]], x["c"])
    return out


def merge(a: list, b: list) -> list:
    d = {r[0]: r for r in a}
    for r in b:
        d[r[0]] = r
    return [d[k] for k in sorted(d)]


def _px(c: dict, side: str):
    x = c.get(side) or {}
    v = x.get("close", x.get("close_dollars"))
    return float(v) if v is not None else None


def rows_of(cs: list[dict]) -> list:
    """[end_ts, yes_ask, yes_bid, volume, trade_hi, trade_lo] per candle (Kalshi emits a candle per period)."""
    out = []
    for c in cs:
        pr = c.get("price") or {}
        hi = pr.get("high", pr.get("high_dollars")); lo = pr.get("low", pr.get("low_dollars"))
        out.append([c["end_period_ts"], _px(c, "yes_ask"), _px(c, "yes_bid"), float(c.get("volume") or c.get("volume_fp") or 0),
                    float(hi) if hi is not None else None, float(lo) if lo is not None else None])
    return sorted(out)


def cached_market_candles(M: dict) -> dict[str, list]:
    """Hourly candles of single markets already on disk from earlier researchers (historical per-market calls and
    /markets/candlesticks batches); windows from different calls are merged."""
    out: dict[str, list] = {}
    for p, f in index().items():
        if "period_interval=60" not in p:
            continue
        x = re.match(r"/historical/markets/([^/]+)/candlesticks", p)
        if x and x.group(1) in M:
            out[x.group(1)] = merge(out.get(x.group(1), []), rows_of(load(f).get("candlesticks", []) or []))
        elif p.startswith("/markets/candlesticks"):
            for mk in load(f).get("markets", []) or []:
                t = mk.get("market_ticker") or mk.get("ticker")
                if t in M:
                    out[t] = merge(out.get(t, []), rows_of(mk.get("candlesticks", []) or []))
        elif "/events/" in p:
            d = load(f)
            for t, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                if t in M:
                    out[t] = merge(out.get(t, []), rows_of(cs))
    return out


def covered(rows: list, lo: int, hi: int) -> bool:
    return bool(rows) and rows[0][0] <= lo + 2 * DAY and rows[-1][0] >= hi - 2 * DAY


NFAR = 3    # the hazard is fitted to at most the 3 nearest far rungs (fixed before any candle was read)


def windows(E: dict, M: dict) -> dict[str, tuple[int, int]]:
    """ticker -> (lo, hi) hourly window needed. Thinned ladder (>= 7 d gaps). Rung j is used as the near rung for
    t in (D_{j-1}, D_j] with tau <= 60 d, and as one of the NFAR nearest far rungs while R_{j-NFAR}..R_{j-1} are near;
    so lo_j = min over i in [j-NFAR, j] of max(D_{i-1}, D_i - 62 d), hi_j = min(close, D_j, now)."""
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    out = {}
    for e, v in E.items():
        rs = thin([M[t] for t in v["rungs"]])
        if len(rs) < 2:
            continue
        for j, r in enumerate(rs):
            starts = [max(rs[i - 1]["D"] if i > 0 else 0, rs[i]["D"] - WIN) for i in range(max(0, j - NFAR), j + 1)]
            lo = max(r["open"], min(starts)) - H; hi = min(r["close"], r["D"], now) + H
            if hi > lo:
                out[r["t"]] = (lo, hi)
    return out


def plan(E: dict, M: dict, have: dict, old: dict) -> list[tuple]:
    """('hist', ticker, lo, hi) per archived rung not already covered on disk; ('batch', [tickers], lo, hi) chunks of
    live rungs per event with n x hours <= 9,500 hourly candles per call."""
    W = windows(E, M); todo = []
    for e, v in E.items():
        rs = [M[t] for t in v["rungs"] if t in W]
        for r in rs:
            lo, hi = W[r["t"]]
            if r["tier"] == "hist" and r["t"] not in have and not covered(old.get(r["t"], []), lo, hi):
                todo.append((e, "hist", [r["t"]], lo, hi))
        live = [r for r in rs if r["tier"] == "live" and r["t"] not in have and not covered(old.get(r["t"], []), *W[r["t"]])]
        if live:
            lo = min(W[r["t"]][0] for r in live); hi = max(W[r["t"]][1] for r in live)
            for i in range(0, len(live), 25):
                grp = live[i:i + 25]
                span = 9500 // len(grp) * H
                t = lo
                while t < hi:
                    todo.append((e, "batch", [r["t"] for r in grp], t, min(t + span, hi))); t += span
    return todo


def fetch_candles(E: dict, M: dict, events_order: list[str], max_calls: int) -> None:
    have = load_candles(); start = used()
    old = cached_market_candles(M)
    todo = [x for x in plan(E, M, have, old)]
    rank = {e: i for i, e in enumerate(events_order)}
    todo = sorted([x for x in todo if x[0] in rank], key=lambda x: rank[x[0]])
    with CF.open("a") as g:
        for e, kind, tks, lo, hi in todo:
            if used() - start >= max_calls:
                print("call cap reached"); return
            if kind == "hist":
                d = kget(f"/historical/markets/{tks[0]}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
                g.write(json.dumps({"t": tks[0], "c": rows_of(d.get("candlesticks", []) or []), "src": "hist", "err": d.get("_error")}) + "\n")
            else:
                d = kget(f"/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={lo}&end_ts={hi}&period_interval=60")
                got = 0
                for mk in d.get("markets", []) or []:
                    t = mk.get("market_ticker") or mk.get("ticker")
                    g.write(json.dumps({"t": t, "c": rows_of(mk.get("candlesticks", []) or []), "src": "batch"}) + "\n"); got += 1
                if not got:
                    print("  batch returned nothing:", e, str(d)[:300])
            g.flush()
            print(f"{kind:5s} {e:30s} {tks[0]:40s} n={len(tks)} calls {used()}", flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "status"
    if what == "list":
        list_markets(LIVE_SERIES, hist=False)
    elif what == "listhist":
        list_markets(sys.argv[2].split(","), hist=True)
    elif what == "events":
        E = events()
        for e, v in sorted(E.items(), key=lambda kv: kv[1]["first_D"]):
            f = lambda x: dt.datetime.utcfromtimestamp(x).strftime("%y-%m-%d")
            print(f"{e:34s} n={len(v['rungs']):2d} vol={v['vol']:11.0f} {f(v['first_D'])}..{f(v['last_D'])} "
                  f"{''.join((r or '?')[0] for r in v['results'])} {''.join(t[0] for t in v['tiers'])} | {v['title'][:60]}")
    elif what == "plan":
        E = json.loads(EF.read_text()); M = {json.loads(l)["t"]: json.loads(l) for l in MF.open()}
        todo = plan(E, M, load_candles(), cached_market_candles(M))
        from collections import Counter
        c = Counter(x[0] for x in todo)
        for e, v in sorted(E.items(), key=lambda kv: kv[1]["first_D"]):
            rs = thin([M[t] for t in v["rungs"]])
            nset = sum(1 for r in rs if r["result"] in ("yes", "no"))
            print(f"{e:34s} thinned={len(rs):2d} settled={nset:2d} calls={c.get(e, 0):3d} vol={v['vol']:11.0f}")
        print("total calls", len(todo))
    elif what == "candles":
        E = json.loads(EF.read_text()); M = {json.loads(l)["t"]: json.loads(l) for l in MF.open()}
        order = sys.argv[3].split(",")
        fetch_candles(E, M, order, int(sys.argv[2]))
    elif what == "status":
        print("calls used", used())
