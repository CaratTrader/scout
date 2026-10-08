"""ESPN data for the sports_books study: scoreboards (event id, scheduled start, teams, winner) and the DraftKings
moneyline (open / close) that ESPN's core odds API keeps for completed events. Cached under
data/kalshi_lab/strategies/sports_books/espn/ so a rerun makes no network calls.

Usage: python -m lab.kalshi.strategies.sports_books_espn [scoreboards|odds|all]"""
from __future__ import annotations
import datetime as dt, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/sports_books"); ESPN = OUT / "espn"
MK = Path("data/kalshi_lab/markets")
LEAGUE = {"KXMLBGAME": ("baseball", "mlb", ""), "KXNFLGAME": ("football", "nfl", ""), "KXNBAGAME": ("basketball", "nba", ""),
          "KXNHLGAME": ("hockey", "nhl", ""), "KXNCAAFGAME": ("football", "college-football", "&groups=80|&groups=81"),
          "KXUEFANLGAME": ("soccer", "uefa.nations", "")}
UA = {"User-Agent": "Mozilla/5.0 (research; polite)", "Accept": "application/json"}


def get(url: str, cache: Path, pace: float = 0.35) -> dict | None:
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                txt = r.read().decode()
            cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text(txt); time.sleep(pace)
            return json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                cache.parent.mkdir(parents=True, exist_ok=True); cache.write_text("null"); return None
            time.sleep(3 * (i + 1))
        except Exception as e:
            print("retry", url[-60:], str(e)[:60], flush=True); time.sleep(3 * (i + 1))
    return None


def dates_for(series: str) -> list[str]:
    exps = [json.loads(l)["exp"] for l in (MK / f"{series}.jsonl").open()]
    lo = dt.datetime.utcfromtimestamp(min(exps)).date() - dt.timedelta(days=2)
    hi = dt.datetime.utcfromtimestamp(max(exps)).date() + dt.timedelta(days=1)
    out = []
    d = lo
    while d <= hi:
        out.append(d.strftime("%Y%m%d")); d += dt.timedelta(days=1)
    return out


def scoreboards() -> dict:
    """series -> list of ESPN events {id, start (unix), teams: [{ha, abbr, names, winner, score}]}"""
    allev = {}
    for s, (sport, lg, extra) in LEAGUE.items():
        evs = {}
        for d in dates_for(s):
          for gi, ex in enumerate(extra.split("|")):
            url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{lg}/scoreboard?dates={d}&limit=300{ex}"
            j = get(url, ESPN / "scoreboard" / s / f"{d}{'_g%d' % gi if gi else ''}.json")
            for e in (j or {}).get("events") or []:
                c = e["competitions"][0]
                teams = []
                for x in c["competitors"]:
                    t = x.get("team") or {}
                    names = {t.get(k) for k in ("abbreviation", "displayName", "shortDisplayName", "location", "name", "nickname") if t.get(k)}
                    teams.append({"ha": x.get("homeAway"), "abbr": t.get("abbreviation"), "names": sorted(names), "winner": x.get("winner"),
                                  "score": x.get("score")})
                start = int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp())
                evs[e["id"]] = {"id": e["id"], "start": start, "teams": teams, "status": ((c.get("status") or {}).get("type") or {}).get("name"),
                                "comp": c["id"]}
        allev[s] = list(evs.values())
        print(s, len(allev[s]), "espn events", flush=True)
    (OUT / "espn_events.json").write_text(json.dumps(allev))
    return allev


def odds(series: str, ev: dict) -> dict | None:
    sport, lg, _ = LEAGUE[series]
    url = f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{lg}/events/{ev['id']}/competitions/{ev['comp']}/odds?limit=50"
    return get(url, ESPN / "odds" / series / f"{ev['id']}.json")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("scoreboards", "all"):
        scoreboards()
    if what in ("odds", "all"):
        from lab.kalshi.strategies.sports_books import match_events
        allev = json.loads((OUT / "espn_events.json").read_text())
        for s in LEAGUE:
            pairs = match_events(s, allev[s])
            n = 0
            for ke, ev in pairs.items():
                odds(s, ev); n += 1
            print(s, "odds fetched/cached", n, flush=True)
