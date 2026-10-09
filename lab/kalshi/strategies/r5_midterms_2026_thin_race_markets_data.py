"""Data for r5_midterms_2026_thin_race_markets (kill test on the archive).

Step 1 (list24): 2024 general-election per-race markets, listed by guessed ticker (<SERIES>-24-R / -24-D) through
                 /historical/markets?tickers=... (130 tickers per call) for every House / Senate / Governor /
                 President-by-state series in the catalog. No price or result is used to choose the series.
Step 2 (list25): 2025 off-year races (Nov 4 2025: VA, NJ, NYC and other city races, TX-18 special) and AP-call timing
                 series, one /historical/markets?series_ticker=S call each.
Step 3 (candles): /historical/markets/{t}/candlesticks, 1-minute, election night [18:00 ET, +24 h], one call per
                 market. Markets are chosen BEFORE any candle is seen, by rules that use only listing fields that
                 were knowable before the night or that do not touch prices (see pick()).
Usage: python -m lab.kalshi.strategies.r5_midterms_2026_thin_race_markets_data list24|list25|pick|candles N"""
from __future__ import annotations
import datetime as dt, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r5_midterms_2026_thin_race_markets_api import kget, used, OUT

CAT = Path("data/kalshi_lab/series_all.json")
MK = OUT / "markets.jsonl"
CD = OUT / "candles.jsonl"
N24 = int(dt.datetime(2024, 11, 5, 23, 0, tzinfo=dt.timezone.utc).timestamp())   # 18:00 ET election night 2024
N25 = int(dt.datetime(2025, 11, 4, 23, 0, tzinfo=dt.timezone.utc).timestamp())   # 18:00 ET election night 2025
S25 = ["GOVPARTYVA", "GOVPARTYNJ", "KXLTGOVVA", "KXATTYGENVA", "KXAPCALLVAGOV", "KXAPCALLNJGOV", "KXAPCALLVAAG", "KXAPCALLSNYC",
       "KXMAYORSEATTLE", "KXMAYORSEA", "KXMAYORMIN", "KXMAYORDETROIT", "KXMAYORCHAR", "HOUSETX18S", "KXVAHOUSE", "KXNJGA"]


def ts(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def row(m: dict, cls: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0], "cls": cls,
            "open": ts(m.get("open_time")), "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")),
            "settled": ts(m.get("settlement_ts")), "result": m.get("result"), "vol": float(m.get("volume_fp") or m.get("volume") or 0),
            "oi": float(m.get("open_interest_fp") or 0), "title": (m.get("title") or "")[:160], "sub": (m.get("yes_sub_title") or "")[:80],
            "rules": (m.get("rules_primary") or "")[:300], "created": ts(m.get("created_time"))}


def load() -> dict:
    return {json.loads(l)["t"]: json.loads(l) for l in MK.open()} if MK.exists() else {}


def save(rows: dict) -> None:
    with MK.open("w") as g:
        for r in rows.values():
            g.write(json.dumps(r) + "\n")


def series24() -> list[tuple[str, str]]:
    s = json.loads(CAT.read_text())["series"]
    pats = {"house": r"^HOUSE(PARTY)?-?[A-Z]{2}-?\d+$", "senate": r"^SENATE(PARTY)?-?[A-Z]{2}[S]?$",
            "pres": r"^PRESPARTY[A-Z]{2}$", "gov": r"^GOVPARTY-?[A-Z]{2}$"}
    out = []
    for x in s:
        for cls, p in pats.items():
            if re.match(p, x["ticker"]):
                out.append((x["ticker"], cls))
    return sorted(out)


def list24() -> None:
    rows = load(); ser = series24()
    tick = [(f"{s}-24-{p}", cls) for s, cls in ser for p in ("R", "D")]
    cls_of = {t: c for t, c in tick}
    for i in range(0, len(tick), 130):
        chunk = [t for t, _ in tick[i:i + 130]]
        d = kget(f"/historical/markets?tickers={','.join(chunk)}&limit=1000")
        ms = d.get("markets") or []
        for m in ms:
            rows[m["ticker"]] = row(m, cls_of.get(m["ticker"], "?"))
        print(f"chunk {i}: asked {len(chunk)} got {len(ms)} total {len(rows)} calls {used()}", str(d)[:120] if not ms else "", flush=True)
    save(rows)


def list25() -> None:
    rows = load()
    for s in S25:
        d = kget(f"/historical/markets?series_ticker={s}&limit=1000")
        ms = d.get("markets") or []
        k = 0
        for m in ms:
            c = ts(m.get("close_time")) or 0; o = ts(m.get("open_time")) or 0
            if o < N25 + 86400 and c > N25 - 3600:          # alive on election night 2025
                rows[m["ticker"]] = row(m, "y25"); k += 1
        print(f"{s:22s} got {len(ms):4d} alive-on-night {k:4d} calls {used()}", flush=True)
    save(rows)


if __name__ == "__main__" and sys.argv[1] in ("list24", "list25"):
    {"list24": list24, "list25": list25}[sys.argv[1]]()


# ---------------------------------------------------------------- step 3: pick markets (no prices, no candles seen)
PRES_LEAN = ["PRESPARTYVA", "PRESPARTYNH", "PRESPARTYMN", "PRESPARTYNM", "PRESPARTYME", "PRESPARTYIA"]
RACES25 = ["GOVPARTYVA", "GOVPARTYNJ", "KXLTGOVVA", "KXATTYGENVA", "KXVAHOUSE", "KXNJGA", "KXMAYORCHAR"]
EXTRA25 = ["KXMAYORSEATTLE-25-KWIL", "KXMAYORSEATTLE-25-BH", "KXMAYORDETROIT-25-MS"]
# AP-call ladders: the two earliest strikes that resolved YES (they jump at the call; chosen for the latency study)
APCALL = ["KXAPCALLVAGOV-25NOV04-800", "KXAPCALLVAGOV-25NOV04-830", "KXAPCALLNJGOV-25NOV04-930", "KXAPCALLNJGOV-25NOV04-1000",
          "KXAPCALLVAAG-25NOV04-1030", "KXAPCALLVAAG-25NOV04-1100", "KXAPCALLSNYC-25NOV04-1000", "KXAPCALLSNYC-25NOV04-1030"]


def pick() -> list[dict]:
    rows = load(); ev = {}
    for r in rows.values():
        ev.setdefault(r["e"], []).append(r)
    out = []
    for e, ms in ev.items():
        s = ms[0]["series"]; cls = ms[0]["cls"]
        top = max(ms, key=lambda m: m["vol"])
        if cls in ("house", "senate", "gov") or (cls == "pres" and s in PRES_LEAN) or s in RACES25:
            if top["vol"] >= 500:
                out.append({**top, "night": N25 if cls == "y25" else N24, "role": "race"})
    for t in EXTRA25:
        out.append({**rows[t], "night": N25, "role": "race"})
    for t in APCALL:
        out.append({**rows[t], "night": N25, "role": "apcall"})
    return out


def _compact(cs: list[dict]) -> list[list]:
    def g(c, k, sub):
        d = c.get(k) or {}
        v = d.get(sub + "_dollars", d.get(sub))
        return float(v) if v not in (None, "") else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask", "close"), g(c, "yes_bid", "close"), g(c, "yes_ask", "low"),
             g(c, "yes_bid", "high"), float(c.get("volume_fp") or c.get("volume") or 0), g(c, "price", "close"),
             g(c, "price", "high"), g(c, "price", "low"), g(c, "yes_ask", "high"), g(c, "yes_bid", "low")] for c in cs]


PRE_H, POST_H = 1, 25     # window [night - 1 h, night + 25 h] = 17:00 ET .. 19:00 ET next day


def candles(max_calls: int) -> None:
    sel = pick()
    (OUT / "picked.json").write_text(json.dumps([{k: m[k] for k in ("t", "e", "series", "cls", "role", "night", "vol", "result")} for m in sel], indent=0))
    have = {json.loads(l)["t"] for l in CD.open()} if CD.exists() else set()
    start = used()
    order = sorted(sel, key=lambda m: ({"house": 0, "gov": 1, "senate": 2, "y25": 3, "pres": 4}.get(m["cls"], 5), m["role"] != "race", m["t"]))
    with CD.open("a") as fh:
        for m in order:
            if m["t"] in have:
                continue
            if used() - start >= max_calls:
                print("budget stop"); return
            lo = m["night"] - PRE_H * 3600; hi = min(m["night"] + POST_H * 3600, (m["close"] or 2e9) + 600)
            d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={int(hi)}&period_interval=1")
            cs = d.get("candlesticks")
            if cs is None:
                print("no candles", m["t"], str(d)[:200]); continue
            fh.write(json.dumps({"t": m["t"], "c": _compact(cs)}) + "\n"); fh.flush()
            have.add(m["t"]); print(m["cls"], m["t"], len(cs), "calls", used(), flush=True)


if __name__ == "__main__" and sys.argv[1] in ("pick", "candles"):
    if sys.argv[1] == "pick":
        import collections
        p = pick(); print(len(p), collections.Counter(m["cls"] for m in p))
    else:
        candles(int(sys.argv[2]))


# ---------------------------------------------------------------- step 4: sibling books (cross-book staleness test)
def siblings(max_calls: int) -> None:
    """For every race market whose own path triggered (Q or M, see the study module), fetch the other book of the
    same event (2-candidate races: the -D book for an -R pick and vice versa). Chosen by the market's own path only."""
    rows = load(); per = [json.loads(l) for l in (OUT / "per_market.jsonl").open()]
    have = {json.loads(l)["t"] for l in CD.open()} if CD.exists() else set()
    picked = json.loads((OUT / "picked.json").read_text()); pk = {m["t"] for m in picked}
    todo = []
    for r in per:
        if r["t"].startswith("KXMAYORSEATTLE") or r["role"] != "race" or not ((r.get("Q") or {}).get("t0") or (r.get("M") or {}).get("t0")):
            continue
        e = rows[r["t"]]["e"]
        sib = [m for m in rows.values() if m["e"] == e and m["t"] != r["t"] and m["vol"] >= 200]
        sib.sort(key=lambda m: -m["vol"])
        for m in sib[:1]:
            if m["t"] not in have and m["t"] not in pk:
                todo.append({**m, "night": r["night"], "role": "sibling"})
    start = used()
    with CD.open("a") as fh:
        for m in todo:
            if used() - start >= max_calls:
                print("budget stop"); break
            lo = m["night"] - PRE_H * 3600; hi = min(m["night"] + POST_H * 3600, (m["close"] or 2e9) + 600)
            d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={int(hi)}&period_interval=1")
            cs = d.get("candlesticks")
            if cs is None:
                print("no candles", m["t"], str(d)[:200]); continue
            fh.write(json.dumps({"t": m["t"], "c": _compact(cs)}) + "\n"); fh.flush()
            picked.append({k: m[k] for k in ("t", "e", "series", "cls", "role", "night", "vol", "result")})
            print("sibling", m["t"], len(cs), "calls", used(), flush=True)
    (OUT / "picked.json").write_text(json.dumps(picked, indent=0))


if __name__ == "__main__" and sys.argv[1] == "siblings":
    siblings(int(sys.argv[2]))
