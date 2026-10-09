"""r3_rt_score_drift data: films, RT slugs, Wayback score paths, Kalshi candles.

Outputs under data/kalshi_lab/strategies/r3_rt_score_drift/:
  markets.jsonl   one settled KXRT market per line (archive /historical + the live list cached by round-1 quake_entertain)
  films.json      event -> {title, open, close, final, slug}
  captures.jsonl  one Wayback capture of the film's RT page per line {e, ts, liked, notliked, n, score}
  candles/<event>.json  {ticker: [[ts, yes_ask_close, yes_bid_close, volume], ...]} hourly
Usage: python lab/kalshi/strategies/r3_rt_score_drift_data.py [films|slugs|cdx|caps|kcandles_live|kcandles_arch]"""
from __future__ import annotations
import calendar, html, json, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_rt_score_drift_api import OUT, kalshi, iso, wb_get, calls_used, _key

QE_RAW = Path("data/kalshi_lab/strategies/quake_entertain/raw/34a78e185dce12306212.json")   # round-1 cache of the live settled KXRT list
RT = OUT / "rt"; RT.mkdir(exist_ok=True)
CAND = OUT / "candles"; CAND.mkdir(exist_ok=True)
WINDOW_D = 8    # candles cover [close - 8 days, close]: the embargo-to-settlement window (event candles cap ~5,000 per call)


def norm(x: dict) -> dict:
    return {"t": x["ticker"], "e": x["event_ticker"], "title": x.get("title"), "open": iso(x["open_time"]), "close": iso(x["close_time"]),
            "result": x.get("result"), "type": x.get("strike_type"), "floor": x.get("floor_strike"), "cap": x.get("cap_strike"),
            "value": x.get("expiration_value"), "vol": float(x.get("volume_fp") or x.get("volume") or 0)}


def films() -> dict:
    ms = {}
    for x in kalshi("/historical/markets?series_ticker=KXRT&limit=1000").get("markets", []):
        ms[x["ticker"]] = norm(x)
    for x in json.loads(QE_RAW.read_text()).get("markets", []):
        ms[x["ticker"]] = norm(x)
    (OUT / "markets.jsonl").write_text("".join(json.dumps(m) + "\n" for m in sorted(ms.values(), key=lambda m: (m["close"], m["t"]))))
    ev = defaultdict(list)
    for m in ms.values():
        ev[m["e"]].append(m)
    fl = json.loads((OUT / "films.json").read_text()) if (OUT / "films.json").exists() else {}
    for e, xs in ev.items():
        t = re.sub(r"\s*Rotten Tomatoes score\??\s*$", "", xs[0]["title"] or "", flags=re.I).strip()
        vals = {re.sub(r"[^0-9]", "", str(m["value"])) for m in xs if m["value"] is not None}
        # the settled value: derive from results (largest 'greater' strike resolved yes, smallest resolved no)
        yes = [m["floor"] for m in xs if m["result"] == "yes" and m["type"] == "greater"]
        no = [m["floor"] for m in xs if m["result"] == "no" and m["type"] == "greater"]
        lo = max(yes) + 1 if yes else None; hi = min(no) if no else None
        rec = fl.get(e, {})
        rec.update({"title": t, "open": min(m["open"] for m in xs), "close": max(m["close"] for m in xs), "n_markets": len(xs),
                    "value_raw": sorted(vals), "final_bounds": [lo, hi], "vol": sum(m["vol"] for m in xs)})
        fl[e] = rec
    (OUT / "films.json").write_text(json.dumps(fl, indent=1))
    print(len(ms), "markets", len(fl), "films")
    return fl


def rt_get(url: str, pace: float = 2.0) -> str:
    f = RT / (_key(url) + ".html")
    if f.exists():
        return f.read_text()
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research script)"}), timeout=60) as r:
                txt = r.read().decode("utf-8", "replace")
            f.write_text(txt); time.sleep(pace); return txt
        except Exception as e:
            print("rt retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return ""


def slugs() -> None:
    fl = json.loads((OUT / "films.json").read_text())
    for e, r in sorted(fl.items(), key=lambda kv: kv[1]["close"]):
        if r.get("slug"):
            continue
        q = r["title"].replace("–", "-")
        s = rt_get("https://www.rottentomatoes.com/search?search=" + urllib.parse.quote(q))
        rows = []
        for m in re.finditer(r"<search-page-media-row(.*?)</search-page-media-row>", s, re.S):
            t = m.group(1)
            a = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', t[:1500]))
            h = re.search(r'href="https://www.rottentomatoes.com/m/([^"]+)"', t)
            name = re.search(r'slot="title"[^>]*>\s*([^<]+?)\s*<', t)
            if h:
                rows.append({"slug": h.group(1), "year": a.get("release-year"), "score": a.get("tomatometer-score"), "name": html.unescape(name.group(1)) if name else ""})
        cand = [x for x in rows if x["year"] in ("2026",)]
        r["search"] = rows[:8]
        r["slug"] = cand[0]["slug"] if cand else None
        print(e, r["title"], "->", r["slug"], [(x["slug"], x["year"], x["score"]) for x in cand[:3]], flush=True)
        (OUT / "films.json").write_text(json.dumps(fl, indent=1))


def cdx() -> None:
    fl = json.loads((OUT / "films.json").read_text())
    for e, r in sorted(fl.items(), key=lambda kv: kv[1]["close"]):
        if not r.get("slug") or r.get("cdx") is not None:
            continue
        a = time.strftime("%Y%m%d", time.gmtime(r["open"] - 10 * 86400)); b = time.strftime("%Y%m%d", time.gmtime(r["close"] + 3 * 86400))
        url = f"https://web.archive.org/cdx/search/cdx?url=rottentomatoes.com/m/{r['slug']}&output=json&fl=timestamp,statuscode,digest,original&from={a}&to={b}"
        s, body, _ = wb_get(url, pace=3.0, tries=8)
        if s != 200:
            print(e, "cdx failed", s, flush=True); continue
        rows = json.loads(body or "[]")[1:]
        r["cdx"] = [x for x in rows if x[1] == "200"]
        print(e, r["slug"], len(rows), "captures,", len(r["cdx"]), "ok", flush=True)
        (OUT / "films.json").write_text(json.dumps(fl, indent=1))


def parse_rt(b: str) -> dict | None:
    m = re.search(r'"criticsScore":\{([^{}]*)\}', b)
    if m:
        d = {}
        for k in ("likedCount", "notLikedCount", "reviewCount", "score", "certified"):
            mm = re.search(rf'"{k}":"?([^",}}]*)"?', m.group(1))
            d[k] = mm.group(1) if mm else None
        try:
            liked = int(d["likedCount"]); nl = int(d["notLikedCount"])
            sc = int(d["score"]) if d["score"] not in (None, "", "null") else None
            return {"liked": liked, "notliked": nl, "n": liked + nl, "score": sc, "rc": int(d["reviewCount"] or 0)}
        except Exception:
            pass
    m = re.search(r'"aggregateRating":\{[^{}]*"ratingValue":"?(\d+)"?[^{}]*"reviewCount":(\d+)', b)
    if m:
        sc, n = int(m.group(1)), int(m.group(2))
        return {"liked": round(sc * n / 100), "notliked": n - round(sc * n / 100), "n": n, "score": sc, "rc": n, "approx": True}
    return None


def caps(loop: bool = True) -> None:
    import subprocess
    while True:
        busy = bool(subprocess.run(["pgrep", "-f", "r3_rt_score_drift_data.py cdx"], capture_output=True).stdout.strip())
        _caps_once()
        if not (loop and busy):
            break
        time.sleep(30)


def _caps_once() -> None:
    fl = json.loads((OUT / "films.json").read_text())
    cf = OUT / "captures.jsonl"
    have = {(x["e"], x["ts"]) for x in map(json.loads, cf.open())} if cf.exists() else set()
    with cf.open("a") as f:
        for e, r in sorted(fl.items(), key=lambda kv: kv[1]["close"]):
            seen = set()
            for ts, st_, dig, orig in r.get("cdx") or []:
                if (e, ts) in have or dig in seen:
                    seen.add(dig); continue
                seen.add(dig)
                s, b, _ = wb_get(f"https://web.archive.org/web/{ts}id_/{orig}", pace=1.5)
                p = parse_rt(b) if s == 200 else None
                rec = {"e": e, "ts": ts, "t": int(time.mktime(time.strptime(ts, "%Y%m%d%H%M%S"))) - time.timezone, "http": s, **(p or {"parse": False})}
                f.write(json.dumps(rec) + "\n"); f.flush()
                print(e, ts, s, p, flush=True)


def kcandles_live() -> None:
    """Hourly event candles over the whole life of each live-listed event (one call per event)."""
    fl = json.loads((OUT / "films.json").read_text())
    live = {json.loads(l)["e"] for l in open(OUT / "markets.jsonl") if json.loads(l)["close"] >= iso("2026-08-03T00:00:00Z")}
    for e in sorted(live, key=lambda e: fl[e]["close"]):
        f = CAND / f"{e}.json"
        if f.exists():
            continue
        r = fl[e]; lo = max(r["open"], r["close"] - WINDOW_D * 86400) - 3600; hi = r["close"] + 3600
        got = defaultdict(list)
        for _ in range(3):
            d = kalshi(f"/series/KXRT/events/{e}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
            for tk, cs in zip(d.get("market_tickers") or [], d.get("market_candlesticks") or []):
                got[tk].extend(cs)
            adj = int(d.get("adjusted_end_ts") or hi)
            if not d or adj >= hi - 3600:
                break
            lo = adj
        out = {tk: compact(cs) for tk, cs in got.items()}
        f.write_text(json.dumps(out))
        print(e, len(out), "markets", sum(len(v) for v in out.values()), "candles; calls", calls_used(), flush=True)


def _f(c: dict, k: str) -> float | None:
    v = c.get(k)
    if isinstance(v, dict):
        v = v.get("close_dollars", v.get("close"))
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def compact(cs: list[dict]) -> list[list]:
    out = []
    for c in cs:
        a, b = _f(c, "yes_ask"), _f(c, "yes_bid")
        if a is not None and a > 1.5:
            a /= 100.0
        if b is not None and b > 1.5:
            b /= 100.0
        out.append([int(c["end_period_ts"]), a, b, float(c.get("volume_fp") or c.get("volume") or 0)])
    return sorted(out)


def wayback_all(pace: float = 4.0) -> None:
    """One sequential process (archive.org refuses connections when hit in parallel): for each film, CDX listing, then
    its captures in [open - 3 d, close + 2 d] plus the last capture before that window."""
    fl = json.loads((OUT / "films.json").read_text())
    cf = OUT / "captures.jsonl"
    have = {(x["e"], x["ts"]): x for x in map(json.loads, cf.open())} if cf.exists() else {}
    for e in sorted(fl, key=lambda e: fl[e]["close"]):
        r = fl[e]
        if not r.get("slug"):
            continue
        if r.get("cdx") is None and "--no-cdx" in sys.argv:
            continue
        if r.get("cdx") is None:
            a = time.strftime("%Y%m%d", time.gmtime(r["open"] - 10 * 86400)); b = time.strftime("%Y%m%d", time.gmtime(r["close"] + 3 * 86400))
            url = f"https://web.archive.org/cdx/search/cdx?url=rottentomatoes.com/m/{r['slug']}&output=json&fl=timestamp,statuscode,digest,original&from={a}&to={b}"
            s_, body, _ = wb_get(url, pace=pace, tries=8)
            if s_ != 200:
                print(e, "cdx failed", s_, flush=True); continue
            r["cdx"] = [x for x in json.loads(body or "[]")[1:] if x[1] == "200"]
            fl2 = json.loads((OUT / "films.json").read_text()); fl2[e]["cdx"] = r["cdx"]; (OUT / "films.json").write_text(json.dumps(fl2, indent=1))
            print(e, r["slug"], len(r["cdx"]), "ok captures", flush=True)
        lo = r["open"] - 3 * 86400; hi = r["close"] + 2 * 86400
        tsec = lambda ts: calendar.timegm(time.strptime(ts, "%Y%m%d%H%M%S"))
        done = {ts: x for (ee, ts), x in have.items() if ee == e}
        # newest first, from close + 2 d back to the embargo: stop at the first capture with no score (pre-embargo)
        # or at open - 3 d; keep captures >= 2 h apart and skip identical digests (identical page)
        rows = sorted((x for x in r["cdx"] if tsec(x[0]) <= hi), key=lambda x: x[0], reverse=True)
        seen = set(); last_t = None
        with cf.open("a") as f:
            for ts, st_, dig, orig in rows:
                t = tsec(ts)
                if dig in seen:
                    continue
                if last_t is not None and last_t - t < 2 * 3600:
                    continue
                if ts in done:
                    pr = done[ts]
                else:
                    s_, b, _ = wb_get(f"https://web.archive.org/web/{ts}id_/{orig}", pace=pace, tries=6, strip=True)
                    pr0 = parse_rt(b) if s_ == 200 else None
                    pr = {"e": e, "ts": ts, "t": t, "http": s_, **(pr0 or {"parse": False})}
                    f.write(json.dumps(pr) + "\n"); f.flush(); have[(e, ts)] = pr
                    print(e, ts, s_, pr0, flush=True)
                seen.add(dig); last_t = t
                if pr.get("http") == 200 and (pr.get("n") == 0 or t < lo):
                    break


def kcandles_arch(per_event: int = 3, reserve: int = 6) -> None:
    """/historical per-market hourly candles over [close - 8 d, close + 1 h] for up to `per_event` strikes of each archived
    event, chosen with information known at the decision captures only: the strikes nearest to the RT score at
    anchor+24 h and anchor+72 h (anchor = max(first score capture, market open)). Never uses the result or final score."""
    from lab.kalshi.strategies.r3_rt_score_drift import load, build, capture_at, H
    fl, mk, paths, cand = load()
    evs, cut, info = build(fl, mk, paths, cand)
    plan = {}
    plog = (OUT / "probe.log").read_text() if (OUT / "probe.log").exists() else ""
    complete = {e for e in evs if fl[e].get("cdx")} | {e for e in evs if f"{e} probed" in plog}
    for e in evs:
        if (CAND / f"{e}.json").exists() or e not in complete:
            continue
        x = info[e]
        if x["E"] is None:
            continue
        A = max(x["E"], x["open"])
        ks = sorted({m["floor"] for m in mk[e] if m["type"] == "greater"})
        picks = []
        for dt_h in (24, 72, 48):
            c = capture_at(x["path"], A + dt_h * H, max_age=1e12)
            if not c or c["t"] >= x["close"] - H:
                continue
            for k in sorted(ks, key=lambda k: (abs(k + 0.5 - 100 * c["liked"] / c["n"]), k)):
                if k not in picks:
                    picks.append(k); break
        # fill up to per_event with the next nearest strikes to the anchor+24 h score
        c = capture_at(x["path"], A + 24 * H, max_age=1e12)
        if c:
            for k in sorted(ks, key=lambda k: (abs(k + 0.5 - 100 * c["liked"] / c["n"]), k)):
                if len(picks) >= per_event:
                    break
                if k not in picks:
                    picks.append(k)
        if picks:
            plan[e] = picks[:per_event]
    need = sum(len(v) for v in plan.values())
    pending = [e for e in evs if not (CAND / f"{e}.json").exists()]            # archived events still without candles
    allow = max(1, min(per_event, (200 - reserve - calls_used()) // max(1, len(pending))))
    print("plan:", len(plan), "events ready,", len(pending), "pending in all; per-event allowance", allow, "; calls used", calls_used(), flush=True)
    plan = {e: v[:allow] for e, v in plan.items()}
    old = json.loads((OUT / "arch_plan.json").read_text()) if (OUT / "arch_plan.json").exists() else {}
    old.update(plan); (OUT / "arch_plan.json").write_text(json.dumps(old, indent=1))
    for e, ks in plan.items():
        r = fl[e]; lo = max(min(m["open"] for m in mk[e]), r["close"] - WINDOW_D * 86400) - 3600; hi = r["close"] + 3600
        out = {}
        for k in ks:
            t = next(m["t"] for m in mk[e] if m["floor"] == k and m["type"] == "greater")
            if calls_used() >= 200 - reserve:
                break
            d = kalshi(f"/historical/markets/{t}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=60")
            out[t] = compact(d.get("candlesticks") or [])
        (CAND / f"{e}.json").write_text(json.dumps(out))
        print(e, ks, {t: len(v) for t, v in out.items()}, "calls", calls_used(), flush=True)



def probe(slug: str, t: int, pace: float = 7.0) -> dict | None:
    """Fetch the Wayback capture nearest to unix time t (the playback redirect picks it; CDX is mostly 503)."""
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(t))
    s_, b, h = wb_get(f"https://web.archive.org/web/{ts}id_/https://www.rottentomatoes.com/m/{slug}", pace=pace, tries=6, strip=True)
    fu = h.get("final_url") or ""
    m = re.search(r"/web/(\d{14})id_/(.*)$", fu)
    if s_ != 200 or not m:
        return {"probe": ts, "http": s_, "parse": False}
    if not re.search(rf"/m/{re.escape(slug)}/?$", m.group(2)):
        return {"probe": ts, "http": s_, "parse": False, "other": m.group(2)}
    pr = parse_rt(b)
    return {"probe": ts, "ts": m.group(1), "t": calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S")), "http": s_, **(pr or {"parse": False})}


def wayback_probe(step_h: int = 24) -> None:
    """Films without a CDX list: probe a 24 h grid back from close + 3 h to the first unscored (pre-embargo) capture or
    max(open - 1 d, close - 8 d), then the instants anchor - 12 h, anchor + 26 h, + 50 h, + 74 h (anchor = first scored
    capture, or market open if later). archive.org allows ~8 requests/min from here, so the grid is coarse."""
    fl = json.loads((OUT / "films.json").read_text())
    cf = OUT / "captures.jsonl"
    have = {(x["e"], x["ts"]) for x in map(json.loads, cf.open()) if x.get("ts")} if cf.exists() else set()
    def put(e, x, f):
        if x and x.get("ts") and (e, x["ts"]) not in have:
            have.add((e, x["ts"])); f.write(json.dumps({"e": e, **x}) + "\n"); f.flush()
            print(e, x.get("ts"), x.get("n"), x.get("score"), flush=True)
    order = sorted(fl, key=lambda e: fl[e]["close"])
    for e in order:
        r = fl[e]
        if not r.get("slug") or r.get("cdx"):
            continue
        lo = max(r["open"] - 86400, r["close"] - 8 * 86400); hi = r["close"] + 3 * 3600
        got = []
        with cf.open("a") as f:
            t = hi
            while t >= lo:
                x = probe(r["slug"], t)
                put(e, x, f)
                if x and x.get("ts"):
                    got.append(x)
                    if x.get("n") == 0 and x["t"] < r["close"] - 24 * 3600:
                        break          # pre-embargo: nothing earlier is needed
                t -= step_h * 3600
            sc = sorted((x for x in got if x.get("n", 0) >= 5 and x.get("score") is not None), key=lambda x: x["t"])
            if sc:
                A = max(sc[0]["t"], r["open"])
                for tt in (A - 12 * 3600, A + 26 * 3600, A + 50 * 3600, A + 74 * 3600):
                    if lo - 86400 <= tt <= r["close"]:
                        put(e, probe(r["slug"], tt), f)
        print(e, "probed", len(got), flush=True)


if __name__ == "__main__":
    {"films": films, "slugs": slugs, "cdx": cdx, "caps": caps, "kcandles_live": kcandles_live, "wayback": wayback_all, "kcandles_arch": kcandles_arch, "probe": wayback_probe}[sys.argv[1]]()
