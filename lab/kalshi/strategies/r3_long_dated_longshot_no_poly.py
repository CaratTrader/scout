"""External check for r3_long_dated_longshot_no on Polymarket (free gamma + CLOB APIs, no key; no Kalshi calls).

Kalshi's own sample is small (the Kalshi call budget allowed ~60 usable markets), so the pre-specified rule is
replayed, unchanged, on a much larger set of long-dated Polymarket markets in the same kinds of categories. This can
not validate a Kalshi strategy (different venue, fees and crowd); it measures whether the premise (long-dated longshot
YES is overpriced by enough to pay >= +10% on NO at 0.80-0.92) holds anywhere at scale.

Universe: closed events (tags politics, geopolitics, world, business, tech, science, economy; event volume >= $100k,
end 2023-01..2026-09), binary Yes/No markets resolved exactly 1/0, market volume >= $25k, scheduled life
(market endDate - startDate) >= 45 d; sports/crypto-tagged events dropped. Seeded random sample, <= 3 markets/event.
Prices: CLOB /prices-history?market=<YES token>&interval=max&fidelity=720 (12-hourly points; finer fidelity is refused
for closed markets). Signal = last point <= S - D days; fill = first point >= signal time + 1 h (<= 12 h later), taker
cost = point price + 1c (half-spread proxy), plus the Kalshi taker fee for comparability (Polymarket itself charges
none on most of these markets). S = market endDate (scheduled; unchanged when a market resolves early).
Usage: python -m lab.kalshi.strategies.r3_long_dated_longshot_no_poly [events|sample|prices|all]"""
from __future__ import annotations
import datetime as dt, hashlib, json, random, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_long_dated_longshot_no_api import OUT

P = OUT / "poly"; PC = P / "cache"
G, C = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
TAGS = ["politics", "geopolitics", "world", "business", "tech", "science", "economy"]
EXCL = {"sports", "crypto", "nba", "nfl", "soccer", "mlb", "nhl", "tennis", "golf", "f1", "ufc", "boxing", "esports",
        "bitcoin", "ethereum", "crypto-prices", "chess", "cricket", "basketball", "football", "hockey", "baseball"}
DAY = 86400


def get(url: str, pace: float = 1.2):
    PC.mkdir(parents=True, exist_ok=True)
    f = PC / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research", "Accept": "application/json"}), timeout=90) as r:
                txt = r.read().decode()
            time.sleep(pace); f.write_text(txt); return json.loads(txt)
        except Exception as e:
            print("retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return None


def ts(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00").replace(" ", "T")
    if len(s) >= 3 and s[-3] in "+-" and s[-6] != ":" and ":" not in s[-3:]:
        s += ":00"
    try:
        return int(dt.datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def events() -> None:
    rows = {}
    for tag in TAGS:
        for page in range(40):
            d = get(f"{G}/events?closed=true&tag_slug={tag}&limit=100&offset={page * 100}&end_date_min=2023-01-01T00:00:00Z"
                    f"&end_date_max=2026-09-30T00:00:00Z&volume_min=100000&order=volume&ascending=false")
            if not d:
                break
            for e in d:
                tags = {t.get("slug") for t in e.get("tags") or []}
                if tags & EXCL:
                    continue
                for m in e.get("markets") or []:
                    try:
                        outs, prices, toks = json.loads(m.get("outcomes") or "[]"), json.loads(m.get("outcomePrices") or "[]"), json.loads(m.get("clobTokenIds") or "[]")
                    except Exception:
                        continue
                    if outs != ["Yes", "No"] or prices not in (["1", "0"], ["0", "1"]) or len(toks) != 2:
                        continue
                    S, start = ts(m.get("endDate")), ts(m.get("startDate") or m.get("createdAt"))
                    if not S or not start or S - start < 45 * DAY or float(m.get("volumeNum") or 0) < 25000:
                        continue
                    rows[m["id"]] = {"id": m["id"], "e": e["slug"], "tag": tag, "q": m.get("question", "")[:140], "S": S, "start": start,
                                     "closed": ts(m.get("closedTime")), "yes_won": prices == ["1", "0"], "tok": toks[0],
                                     "vol": float(m.get("volumeNum") or 0), "neg_risk": bool(e.get("negRisk"))}
            print(tag, "page", page, "events", len(d), "markets kept", len(rows), flush=True)
            if len(d) < 100:
                break
    (P / "markets.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows.values()))


def sample(n: int = 900, per_event: int = 3, seed: int = 20261008) -> list[dict]:
    R = [json.loads(l) for l in (P / "markets.jsonl").open()]
    by = defaultdict(list)
    for r in R:
        by[r["e"]].append(r)
    rng = random.Random(seed); pool = []
    for e in sorted(by):
        ms = sorted(by[e], key=lambda r: r["id"]); rng.shuffle(ms); pool += ms[:per_event]
    rng.shuffle(pool); out = pool[:n]
    (P / "sample.jsonl").write_text("".join(json.dumps(r) + "\n" for r in out))
    print("universe", len(R), "events", len(by), "-> sample", len(out), "events", len({r["e"] for r in out}))
    return out


def prices() -> None:
    S = [json.loads(l) for l in (P / "sample.jsonl").open()]
    with (P / "prices.jsonl").open("w") as g:
        for i, r in enumerate(S):
            d = get(f"{C}/prices-history?market={r['tok']}&interval=max&fidelity=720")
            h = (d or {}).get("history", [])
            g.write(json.dumps({"id": r["id"], "h": [[x["t"], x["p"]] for x in h]}) + "\n")
            if i % 50 == 0:
                print(i, r["q"][:60], len(h), flush=True)


def analyze() -> dict:
    """Replay the Kalshi rule grid on the Polymarket sample. Nothing is tuned here: the headline is the pre-specified
    NO 0.80-0.92 at D in {60, 30, 14}; the grid is reported for context and counted as variants."""
    from lab.kalshi.strategies.r3_long_dated_longshot_no import stats, fmt, fee, DGRID, BANDS
    M = {json.loads(l)["id"]: json.loads(l) for l in (P / "sample.jsonl").open()}
    H = {json.loads(l)["id"]: json.loads(l)["h"] for l in (P / "prices.jsonl").open()}
    T = []
    evc = defaultdict(int)
    for r in M.values():
        evc[r["e"]] = max(evc[r["e"]], r["closed"] or r["S"])
    for i, r in M.items():
        h = sorted(H.get(i) or [])
        if len(h) < 3:
            continue
        end = r["closed"] or r["S"]
        for D in DGRID:
            t = r["S"] - D * DAY
            if t < r["start"] or t + 3600 >= end:
                continue
            sig = [x for x in h if x[0] <= t]; fil = [x for x in h if t + 3600 <= x[0] <= t + 24 * 3600]
            if not sig or t - sig[-1][0] > 3 * DAY or not fil:
                continue
            p_s, p_f = sig[-1][1], fil[0][1]
            if fil[0][0] >= end:
                continue
            for side, ps, pf, won in (("NO", 1 - p_s, 1 - p_f, not r["yes_won"]), ("YES", p_s, p_f, r["yes_won"])):
                px = min(pf + 0.01, 0.995)
                if not (0.02 <= px <= 0.99):
                    continue
                for feeopt in ("fee", "nofee"):
                    pnl = (1.0 if won else 0.0) - px - (fee(px) if feeopt == "fee" else 0.0)
                    T.append({"t": i, "e": r["e"], "series": r["tag"], "D": D, "side": side, "ps": ps, "px": px, "won": won,
                              "pnl": pnl, "ret": pnl / px, "months": max((end - fil[0][0]) / (30 * DAY), 1 / 30),
                              "t_close": evc[r["e"]], "fill_ts": fil[0][0], "feeopt": feeopt, "year": dt.datetime.utcfromtimestamp(end).year,
                              "neg_risk": r["neg_risk"]})
    events_sorted = sorted(evc, key=lambda e: evc[e]); cut = evc[events_sorted[int(len(events_sorted) * 0.7)]]
    lines = [f"Polymarket sample: {len(M)} markets, {len(evc)} events, priced {sum(1 for i in M if len(H.get(i) or []) >= 3)}; "
             f"split at {dt.datetime.utcfromtimestamp(cut):%Y-%m-%d}"]
    def cell(rows, side, band, D=None, feeopt="fee"):
        return [x for x in rows if x["feeopt"] == feeopt and x["side"] == side and band[0] <= x["ps"] < band[1] and (D is None or x["D"] in D)]
    out = {}
    for part, rows in (("all", T), ("discovery", [x for x in T if x["t_close"] < cut]), ("validation", [x for x in T if x["t_close"] >= cut])):
        lines.append(f"\n== {part}")
        for side in ("NO", "YES"):
            for band in BANDS:
                for D in [(d,) for d in DGRID] + [(60, 30, 14)]:
                    k = f"{side} {band[0]:.2f}-{band[1]:.2f} D{'/'.join(map(str, D))}"
                    v = stats(cell(rows, side, band, D)); out[f"{part}|{k}"] = v
                    if v.get("n", 0) >= 10 and (len(D) > 1 or part == "all"):
                        lines.append(fmt(k, v))
        k = "NO 0.80-0.92 D60/30/14 (no fee)"; v = stats(cell(rows, "NO", (0.80, 0.92), (60, 30, 14), "nofee")); out[f"{part}|{k}"] = v; lines.append(fmt(k, v))
    head = [x for x in T if x["feeopt"] == "fee" and x["side"] == "NO" and 0.80 <= x["ps"] < 0.92 and x["D"] in (60, 30, 14)]
    lines.append("\n== headline NO 0.80-0.92 D60/30/14 by tag / year / neg-risk (all periods)")
    for key in ("series", "year", "neg_risk"):
        g = defaultdict(list)
        for x in head:
            g[x[key]].append(x)
        for k2, xs in sorted(g.items(), key=lambda kv: str(kv[0])):
            v = stats(xs); out[f"by_{key}|{k2}"] = v; lines.append(fmt(f"{key}={k2}", v))
    txt = "\n".join(lines); print(txt); (P / "analysis.txt").write_text(txt)
    (P / "cells.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "all"
    if w == "analyze":
        analyze()
    if w in ("events", "all"):
        events()
    if w in ("sample", "all"):
        sample()
    if w in ("prices", "all"):
        prices()
