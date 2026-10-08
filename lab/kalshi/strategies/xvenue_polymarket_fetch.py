"""Polymarket side of the cross-venue study (xvenue_polymarket): find the Polymarket market for each Kalshi market on
disk and cache its CLOB 1-minute price history (gamma-api + clob prices-history, public, no key, no Kalshi calls).

crypto: Kalshi KX{BTC,ETH,SOL,XRP}15M market opening at T  <->  Polymarket event slug {coin}-updown-15m-T ("Up" token).
sports: Kalshi KX{MLB,NFL,NHL,NBA}GAME event  <->  Polymarket event slug {league}-{away}-{home}-{date} (moneyline).
Output: data/kalshi_lab/strategies/xvenue_polymarket/pm_<family>.jsonl
Usage: python -m lab.kalshi.strategies.xvenue_polymarket_fetch crypto|sports [workers]"""
from __future__ import annotations
import datetime as dt, json, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/xvenue_polymarket"; MK = ROOT / "data/kalshi_lab/markets"
G = "https://gamma-api.polymarket.com"; C = "https://clob.polymarket.com"
UA = {"User-Agent": "scout-research"}
_lock = threading.Lock()


def get(url: str, tries: int = 5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            time.sleep(3 * (i + 1) if e.code == 429 else 1 + i)
        except Exception:
            time.sleep(1 + i)
    return None


def history(token: str, t0: int, t1: int) -> list:
    h = (get(f"{C}/prices-history?market={token}&startTs={t0}&endTs={t1}&fidelity=1") or {}).get("history", [])
    return [[int(p["t"]), float(p["p"])] for p in h]


def crypto_job(m: dict) -> dict | None:
    coin = m["series"][2:5].lower()
    slug = f"{coin}-updown-15m-{m['open']}"
    ev = get(f"{G}/events?slug={slug}")
    if not ev or not ev[0].get("markets"):
        return {"t": m["t"], "slug": slug, "miss": 1}
    pm = ev[0]["markets"][0]
    outs = json.loads(pm.get("outcomes") or "[]"); toks = json.loads(pm.get("clobTokenIds") or "[]")
    if len(outs) != 2 or outs[0].lower() != "up":
        return {"t": m["t"], "slug": slug, "miss": 2}
    res = json.loads(pm.get("outcomePrices") or "[]")
    h = history(toks[0], m["open"] - 120, m["close"] + 120)
    return {"t": m["t"], "slug": slug, "pm_up_won": (float(res[0]) > 0.5) if res else None, "vol": float(pm.get("volume") or 0), "h": h}


# ------------------------------------------------------------------ sports
LEAGUE = {"KXMLBGAME": "mlb", "KXNFLGAME": "nfl", "KXNHLGAME": "nhl", "KXNBAGAME": "nba"}
ALIAS = {("mlb", "AZ"): "ari", ("mlb", "ATH"): "oak", ("nfl", "LAR"): "la", ("nfl", "JAC"): "jax", ("nhl", "LA"): "lak", ("nhl", "VGK"): "las",
         ("nhl", "UTA"): "utah", ("nhl", "CGY"): "cal", ("nhl", "MTL"): "mon", ("nhl", "SJ"): "sj"}
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def parse_event(e: str):
    """KXMLBGAME-26SEP071305ATLPHI -> (date, 'ATLPHI');  KXNFLGAME-26SEP13BUFMIA -> (date, 'BUFMIA')."""
    s, code = e.split("-", 1)
    yy, mon, dd = int(code[:2]), MON[code[2:5]], int(code[5:7]); rest = code[7:]
    if rest[:4].isdigit():
        rest = rest[4:]
    return dt.date(2000 + yy, mon, dd), rest


def sports_job(ev_markets: list[dict]) -> dict:
    m0 = ev_markets[0]; lg = LEAGUE[m0["series"]]
    day, teams = parse_event(m0["e"])
    sides = {m["t"].rsplit("-", 1)[1]: m for m in ev_markets}
    # split the team string using the Kalshi market suffixes
    away = home = None
    for a in sides:
        if teams.startswith(a) and teams[len(a):] in sides:
            away, home = a, teams[len(a):]
    if not away:
        return {"e": m0["e"], "miss": "split"}
    tried = []
    al = lambda x: ALIAS.get((lg, x), x).lower()
    for d in (day, day - dt.timedelta(days=1), day + dt.timedelta(days=1)):
        for slug in (f"{lg}-{al(away)}-{al(home)}-{d.isoformat()}", f"{lg}-{al(home)}-{al(away)}-{d.isoformat()}"):
            tried.append(slug)
            ev = get(f"{G}/events?slug={slug}")
            if not ev:
                continue
            mk = [x for x in ev[0].get("markets") or [] if (x.get("sportsMarketType") in ("moneyline", None)) and x.get("slug") == slug] or \
                 [x for x in ev[0].get("markets") or [] if x.get("sportsMarketType") == "moneyline"]
            if not mk:
                continue
            pm = mk[0]
            outs = json.loads(pm.get("outcomes") or "[]"); toks = json.loads(pm.get("clobTokenIds") or "[]")
            t0 = min(x["exp"] for x in ev_markets) - 6 * 3600 - 600; t1 = max(min(x["close"], x["exp"] + 7200) for x in ev_markets) + 600
            h = history(toks[0], t0, t1)
            return {"e": m0["e"], "slug": slug, "away": away, "home": home, "outcomes": outs, "q": pm.get("question"),
                    "pm_res": pm.get("outcomePrices"), "vol": float(pm.get("volume") or 0), "h0": h, "gameStart": pm.get("gameStartTime")}
    return {"e": m0["e"], "miss": "slug", "tried": tried[:2]}


def run(fam: str, workers: int = 6) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"pm_{fam}.jsonl"
    done = set()
    if out.exists():
        for l in out.open():
            x = json.loads(l)
            if not x.get("miss"):
                done.add(x.get("t") or x.get("e"))
    if fam == "crypto":
        jobs = [m for s in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M") for m in map(json.loads, (MK / f"{s}.jsonl").open()) if m["t"] not in done]
        fn = crypto_job
    else:
        evs = {}
        for s in LEAGUE:
            for m in map(json.loads, (MK / f"{s}.jsonl").open()):
                evs.setdefault(m["e"], []).append(m)
        jobs = [v for k, v in evs.items() if k not in done and len(v) == 2]
        fn = sports_job
    print(fam, "jobs", len(jobs), flush=True)
    n = 0
    with ThreadPoolExecutor(workers) as ex, out.open("a") as f:
        for r in ex.map(fn, jobs):
            n += 1
            if r:
                with _lock:
                    f.write(json.dumps(r) + "\n"); f.flush()
            if n % 200 == 0:
                print(fam, n, flush=True)
    print(fam, "done", n, flush=True)


if __name__ == "__main__":
    run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 6)
