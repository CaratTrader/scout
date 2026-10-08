"""Data for archive_calibration: game-winner markets (MLB; NFL + NCAAF) with hourly candles around the scheduled start.

Live window (settled after the archive cutoff 2026-08-09): /markets listing + batch /markets/candlesticks at
period_interval=60 (the 10,000-candle cap counts hourly candles, so ~100 markets x ~95 h per call).
Archive (settled before 2026-08-09): /historical/markets listing + one /historical/markets/{t}/candlesticks call per game,
on a seeded random sample of games (never chosen by price or outcome).
One market per game: the alphabetically first ticker of the event (both sides tradable through YES ask / 1 - YES bid).
Usage: python -m lab.kalshi.strategies.archive_calibration_fetch [live|arch|all]"""
from __future__ import annotations
import datetime as dt, json, random, re, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.archive_calibration_api import kget, used, OUT

CUTOFF = "2026-08-09T00:00:00Z"
LIVE_SERIES = ("KXMLBGAME", "KXNFLGAME", "KXNCAAFGAME")
ARCH_PLAN = {"KXNFLGAME": (45, 1), "KXNCAAFGAME": (35, 2), "KXMLBGAME": (30, 4)}   # series -> (games to sample, listing pages)
MF = OUT / "markets.jsonl"; CF = OUT / "candles.jsonl"
TZ = {"EDT": -4, "EST": -5, "CDT": -5, "CST": -6, "MDT": -6, "MST": -7, "PDT": -7, "PST": -8, "AKDT": -8, "HST": -10}
MON = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}
RX = re.compile(r"scheduled for (\w{3}) (\d{1,2}), (\d{4}) at (\d{1,2}):(\d{2}) ?(AM|PM) ([A-Z]{2,4})")


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def start_time(m: dict) -> tuple[int | None, str]:
    """Scheduled start: from the rules text when it carries a time, else expected_expiration_time - 3 h
    (checked on NFL 2025 and MLB: Kalshi sets the expected expiration 3 h after the scheduled start)."""
    x = RX.search(m.get("rules_primary") or "")
    if x and x.group(7) in TZ:
        mo, d, y, h, mi, ap, tz = x.groups(); h = int(h) % 12 + (12 if ap == "PM" else 0)
        loc = dt.datetime(int(y), MON[mo], int(d), h, int(mi))
        return int((loc - dt.timedelta(hours=TZ[tz])).replace(tzinfo=dt.timezone.utc).timestamp()), "rules"
    e = ts(m.get("expected_expiration_time"))
    return (e - 3 * 3600 if e else None), "exp-3h"


def slim(m: dict, src: str) -> dict:
    S, how = start_time(m)
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["ticker"].split("-")[0], "src": src, "S": S, "S_how": how,
            "exp": ts(m.get("expected_expiration_time")), "close": ts(m.get("close_time")), "result": m.get("result"),
            "vol": float(m.get("volume_fp") or m.get("volume") or 0), "sub": m.get("yes_sub_title")}


def one_per_game(ms: list[dict]) -> list[dict]:
    ev = defaultdict(list)
    for m in ms:
        if m["result"] in ("yes", "no") and m["S"]:
            ev[m["e"]].append(m)
    return [sorted(v, key=lambda m: m["t"])[0] for v in ev.values()]


def compact(cs: list[dict]) -> list[list]:
    def g(c, k):
        v = c.get(k) or {}
        x = v.get("close_dollars", v.get("close"))
        return float(x) if x not in (None, "") else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask"), g(c, "yes_bid"), float(c.get("volume_fp") or c.get("volume") or 0),
             float(c.get("open_interest_fp") or c.get("open_interest") or 0)] for c in cs]


def load_markets() -> dict:
    return {json.loads(l)["t"]: json.loads(l) for l in MF.open()} if MF.exists() else {}


def save_markets(mk: dict) -> None:
    MF.write_text("".join(json.dumps(m) + "\n" for m in sorted(mk.values(), key=lambda m: (m["S"] or 0, m["t"]))))


def have_candles() -> set:
    return {json.loads(l)["t"] for l in CF.open()} if CF.exists() else set()


def live() -> None:
    mk = load_markets(); lo = ts(CUTOFF)
    for s in LIVE_SERIES:
        if any(m["series"] == s and m["src"] == "live" for m in mk.values()):
            continue
        cursor = ""; n = 0
        probe = OUT / "probe_mlb_live_page1.json"
        while True:
            if s == "KXMLBGAME" and not cursor and probe.exists():
                d = json.loads(probe.read_text())
            else:
                d = kget(f"/markets?series_ticker={s}&status=settled&min_close_ts={lo}&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
            for m in d.get("markets") or []:
                mk[m["ticker"]] = slim(m, "live"); n += 1
            cursor = d.get("cursor") or ""
            if not cursor or not d.get("markets"):
                break
        print(s, "live markets", n, "calls used", used(), flush=True)
        save_markets(mk)
    have = have_candles()
    todo = sorted((m for m in one_per_game([m for m in mk.values() if m["src"] == "live"]) if m["t"] not in have), key=lambda m: m["S"])
    print("live games to fetch", len(todo), flush=True)
    i = 0
    with CF.open("a") as fh:
        while i < len(todo):
            b = [todo[i]]; a, z = todo[i]["S"] - 8 * 3600, todo[i]["S"] + 3600
            while i + len(b) < len(todo) and len(b) < 100:
                m = todo[i + len(b)]; a2, z2 = min(a, m["S"] - 8 * 3600), max(z, m["S"] + 3600)
                if (len(b) + 1) * ((z2 - a2) // 3600 + 1) > 9500:
                    break
                b.append(m); a, z = a2, z2
            i += len(b)
            d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in b)}&start_ts={a}&end_ts={z}&period_interval=60")
            got = {x.get("market_ticker") or x.get("ticker"): x.get("candlesticks") or [] for x in d.get("markets") or []}
            for m in b:
                if m["t"] in got:
                    fh.write(json.dumps({"t": m["t"], "c": compact(got[m["t"]])}) + "\n")
            fh.flush()
            print("batch", len(b), "got", len(got), "calls used", used(), flush=True)


def arch() -> None:
    mk = load_markets(); rng = random.Random(20261008)
    for s, (k, pages) in ARCH_PLAN.items():
        pool = [m for m in mk.values() if m["series"] == s and m["src"] == "arch"]
        if not pool:
            cursor = ""
            for p in range(pages):
                cache = OUT / ("arch_nfl_p1.json" if s == "KXNFLGAME" and p == 0 else ("probe_mlb_page1.json" if s == "KXMLBGAME" and p == 0 else f"arch_{s}_p{p + 1}.json"))
                if cache.exists():
                    d = json.loads(cache.read_text())
                else:
                    d = kget(f"/historical/markets?series_ticker={s}&limit=1000" + (f"&cursor={cursor}" if cursor else "")); cache.write_text(json.dumps(d))
                for m in d.get("markets") or []:
                    mk[m["ticker"]] = slim(m, "arch")
                cursor = d.get("cursor") or ""
                if not cursor:
                    break
            save_markets(mk)
        games = sorted(one_per_game([m for m in mk.values() if m["series"] == s and m["src"] == "arch"]), key=lambda m: m["t"])
        pick = rng.sample(games, min(k, len(games)))
        have = have_candles(); n = 0
        with CF.open("a") as fh:
            for m in sorted(pick, key=lambda m: m["S"]):
                if m["t"] in have:
                    continue
                d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={m['S'] - 8 * 3600}&end_ts={m['S'] + 3600}&period_interval=60")
                if "candlesticks" in d:
                    fh.write(json.dumps({"t": m["t"], "c": compact(d["candlesticks"])}) + "\n"); fh.flush(); n += 1
        print(s, "archive games", len(games), "sampled", len(pick), "fetched", n, "calls used", used(), flush=True)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("live", "all"):
        live()
    if what in ("arch", "all"):
        arch()
    print("DONE calls used", used(), flush=True)
