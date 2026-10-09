"""Data for the r5_scheduled_release_poller backtest: Billboard 200 Sunday reveal vs Kalshi KXTOPALBUM 1-minute books.

Why only the Billboard 200: of the scheduled web releases in the family, it is the only one with (a) a public,
second-stamped publication time we can recover after the fact (billboard.com WordPress REST API date_gmt) and (b) a Kalshi
market still open at the release (KXTOPALBUM closes Sunday 23:59 ET, the reveal is Sunday ~15:00 ET). The Hot 100
(KXTOPSONG) and Netflix weekly series close BEFORE their publication (Hot 100 top-10 Monday, Netflix Top 10 Tuesday),
House roll-call XML has no Last-Modified header, and SCOTUS PDFs are staged on the server hours before release
(Last-Modified 08:40 ET), so those are forward-only.

Step 1 (no prices): Kalshi KXTOPALBUM events and markets from the r2_chart_markets listing (live + /historical);
  Billboard posts per reveal Sunday from the WordPress REST API (search "Billboard 200", Sunday 00:00 - Monday 06:00 ET),
  first qualifying title (frozen parse rule, r5_scheduled_release_poller_parse.py) before the market close.
Step 2 (prices): 1-minute candles over [t_art - 60 min, t_art + 30 min]:
  * events whose markets are still on the live endpoint: one batch /markets/candlesticks call for every market;
  * archive events: /historical/markets/{t}/candlesticks, one call per market, phase 1 = the market the article
    decides YES; phase 2 = only for events where that market's ask 2 min before the article was < 0.92 (first candle's
    open when nothing printed in the hour before) or the #1 was unlisted: the two highest-lifetime-volume other markets (the plausible contenders; volume is used only to choose
    which books to download, never as a trade filter).
Outputs (data/kalshi_lab/strategies/r5_scheduled_release_poller/): b200_articles.json, b200_events.json, candles.jsonl
  candles: {"t": ticker, "c": [[end_ts, ask_c, bid_c, ask_hi, bid_lo, ask_lo, bid_hi, vol, ask_open, bid_open], ...]}
Usage: python -m lab.kalshi.strategies.r5_scheduled_release_poller_data [articles|candles1|candles2|all]"""
from __future__ import annotations
import datetime as dt, json, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.r5_scheduled_release_poller_api import OUT, kget, used, wget  # noqa: E402
from lab.kalshi.strategies.r5_scheduled_release_poller_parse import (album_from_title, clean_title,  # noqa: E402
                                                                      is_b200_no1_title, match_markets)

ET = ZoneInfo("America/New_York")
R2 = ROOT / "data/kalshi_lab/strategies/r2_chart_markets"
AF = OUT / "b200_articles.json"
EF = OUT / "b200_events.json"
CF = OUT / "candles.jsonl"
PRE, POST = 3600, 1800
LIVE_FROM = dt.datetime(2026, 7, 26, tzinfo=ET).timestamp()   # try the live batch endpoint from here on


def load_markets() -> dict[str, list[dict]]:
    ev = defaultdict(dict)
    for fn in ("markets_hist.jsonl", "markets.jsonl"):
        for l in (R2 / fn).open():
            m = json.loads(l)
            if m["series"] == "KXTOPALBUM":
                ev[m["e"]][m["t"]] = m
    return {e: sorted(d.values(), key=lambda m: m["t"]) for e, d in ev.items()}


def wp_posts(day: dt.date) -> list[dict]:
    a = day.isoformat() + "T00:00:00"; b = (day + dt.timedelta(days=1)).isoformat() + "T06:00:00"
    u = (f"https://www.billboard.com/wp-json/wp/v2/posts?search=Billboard%20200&after={a}&before={b}&per_page=100"
         f"&orderby=date&order=asc&_fields=id,date,date_gmt,modified_gmt,title,link,categories")
    s, t, _ = wget(u, pace=1.0)
    return json.loads(t) if s == 200 and t.strip().startswith("[") else []


def articles() -> None:
    evs = load_markets()
    out = {}
    for e, ms in sorted(evs.items(), key=lambda x: x[1][0]["close"]):
        close = max(m["close"] for m in ms)
        day = dt.datetime.fromtimestamp(close, ET).date()
        if dt.datetime.fromtimestamp(close, ET).hour < 6:
            day -= dt.timedelta(days=1)
        ps = wp_posts(day)
        q = []
        for p in ps:
            ts = int(dt.datetime.fromisoformat(p["date_gmt"]).replace(tzinfo=dt.timezone.utc).timestamp())
            ti = clean_title(p["title"]["rendered"])
            if ts < close and is_b200_no1_title(ti):
                q.append({"ts": ts, "title": ti, "album": album_from_title(ti), "id": p["id"], "link": p.get("link")})
        q.sort(key=lambda x: x["ts"])
        out[e] = {"close": close, "day": day.isoformat(), "article": q[0] if q else None, "n_candidates": len(q)}
    AF.write_text(json.dumps(out, indent=1))
    # decided sides (frozen parse rule), joined to listing; markets opened after the fill cannot be traded
    rows = []
    for e, a in out.items():
        ms = evs[e]
        art = a["article"]
        r = {"e": e, "close": a["close"], "day": a["day"], "t_art": art["ts"] if art else None,
             "title": art["title"] if art else None, "album": art["album"] if art else None,
             "markets": [{"t": m["t"], "sub": m["sub"], "open": m["open"], "result": m["result"], "vol": m.get("vol"),
                          "src": m.get("src")} for m in ms]}
        if art and art["album"]:
            subs = {m["t"]: m["sub"] for m in ms if m["open"] <= art["ts"]}
            mm = match_markets(art["album"], subs)
            r.update({"match": mm["status"], "yes": mm["yes"], "no": mm["no"]})
            res = {m["t"]: m["result"] for m in ms}
            wrong = ([mm["yes"]] if mm["yes"] and res.get(mm["yes"]) != "yes" else []) + [t for t in mm["no"] if res.get(t) != "no"]
            r["wrong_side"] = wrong
        else:
            r.update({"match": "no_article" if not art else "unparsed", "yes": None, "no": [], "wrong_side": []})
        rows.append(r)
    EF.write_text(json.dumps(rows, indent=1))
    n_art = sum(1 for r in rows if r["t_art"])
    print("events", len(rows), "with article", n_art, "match", {k: sum(1 for r in rows if r["match"] == k) for k in
          ("exact", "contain", "unlisted", "ambiguous", "unparsed", "no_article")},
          "wrong-side", sum(len(r["wrong_side"]) for r in rows))


def compact(cs: list[dict]) -> list[list]:
    def g(c, k, f="close_dollars"):
        v = c.get(k) or {}
        x = v.get(f, v.get(f.replace("_dollars", "")))     # live: close_dollars; /historical: close
        return float(x) if x not in (None, "") else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask"), g(c, "yes_bid"), g(c, "yes_ask", "high_dollars"),
             g(c, "yes_bid", "low_dollars"), g(c, "yes_ask", "low_dollars"), g(c, "yes_bid", "high_dollars"),
             float(c.get("volume_fp") or c.get("volume") or 0), g(c, "yes_ask", "open_dollars"),
             g(c, "yes_bid", "open_dollars")] for c in cs]


def rebuild() -> None:
    """Recompute candles.jsonl from the on-disk API cache (no Kalshi calls)."""
    import hashlib
    from lab.kalshi.strategies.r5_scheduled_release_poller_api import CACHE
    rows = json.loads(EF.read_text())
    out = {}
    for r in rows:
        if not r["t_art"]:
            continue
        lo, hi = r["t_art"] - PRE, r["t_art"] + POST
        tradable = [m for m in r["markets"] if m["open"] <= r["t_art"] + 120]
        tks = [m["t"] for m in tradable]
        p = f"/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={lo}&end_ts={hi}&period_interval=1"
        f = CACHE / (hashlib.sha1(p.encode()).hexdigest() + ".json")
        if f.exists():
            d = json.loads(f.read_text())
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            if got and any(got.values()):
                for t in tks:
                    out[t] = compact(got.get(t, []))
        for m in r["markets"]:
            p = f"/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=1"
            f = CACHE / (hashlib.sha1(p.encode()).hexdigest() + ".json")
            if f.exists() and m["t"] not in out:
                d = json.loads(f.read_text())
                if "candlesticks" in d:
                    out[m["t"]] = compact(d["candlesticks"])
    CF.write_text("".join(json.dumps({"t": t, "c": c}) + "\n" for t, c in out.items()))
    print("rebuilt", len(out), "markets")


def have() -> dict[str, list]:
    return {json.loads(l)["t"]: json.loads(l)["c"] for l in CF.open()} if CF.exists() else {}


def _save(t: str, c: list) -> None:
    with CF.open("a") as fh:
        fh.write(json.dumps({"t": t, "c": c}) + "\n")


def _hist(t: str, lo: int, hi: int) -> list | None:
    d = kget(f"/historical/markets/{t}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=1")
    if "candlesticks" not in d:
        print("FAIL hist", t, str(d)[:160]); return None
    return compact(d["candlesticks"])


def candles(phase: int) -> None:
    rows = json.loads(EF.read_text())
    hv = have()
    for r in rows:
        if not r["t_art"]:
            continue
        lo, hi = r["t_art"] - PRE, r["t_art"] + POST
        tradable = [m for m in r["markets"] if m["open"] <= r["t_art"] + 120]
        if all(m["t"] in hv for m in tradable):
            continue
        if r["close"] >= LIVE_FROM and phase == 1:
            tks = [m["t"] for m in tradable]
            d = kget(f"/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={lo}&end_ts={hi}&period_interval=1")
            got = {(x.get("market_ticker") or x.get("ticker")): x.get("candlesticks") or [] for x in d.get("markets") or []}
            if got and any(got.values()):
                for t in tks:
                    c = compact(got.get(t, [])); _save(t, c); hv[t] = c
                print(r["e"], "live batch", len(tks), "used", used(), flush=True)
                continue
            print(r["e"], "live batch empty -> archive", str(d)[:120])
        if phase == 1:
            t = r["yes"]
            if t and t not in hv:
                c = _hist(t, lo, hi)
                if c is not None:
                    _save(t, c); hv[t] = c
            print(r["e"], "phase1", t, "used", used(), flush=True)
        else:
            need = r["yes"] is None and r["match"] == "unlisted"
            if r["yes"] and r["yes"] in hv:
                c = hv[r["yes"]]
                pre = [x for x in c if x[0] <= r["t_art"] - 120 and x[1] is not None]
                # sparse candles: with no print in the hour before, the first candle's open is the pre-article state
                pa = pre[-1][1] if pre else (c[0][8] if c and len(c[0]) > 8 else None)
                need = pa is not None and pa < 0.92
            if not need:
                continue
            others = sorted([m for m in tradable if m["t"] in r["no"]], key=lambda m: -(m["vol"] or 0))[:2]
            for m in others:
                if m["t"] in hv:
                    continue
                c = _hist(m["t"], lo, hi)
                if c is not None:
                    _save(m["t"], c); hv[m["t"]] = c
            print(r["e"], "phase2", [m["t"] for m in others], "used", used(), flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    OUT.mkdir(parents=True, exist_ok=True)
    if what == "rebuild":
        rebuild()
    if what in ("articles", "all"):
        articles()
    if what in ("candles1", "all"):
        candles(1)
    if what in ("candles2", "all"):
        candles(2)
