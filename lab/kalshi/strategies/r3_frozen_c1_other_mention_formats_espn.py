"""Scheduled start times of the sampled sports-mention games (ESPN public scoreboard; no key, cached to disk).

Kalshi's sports announcer markets do not close word by word: every word settles in a batch after the game, so the
frozen C1 cancel trigger ("any market of the event closed") only fires post-game and a resting order sits through the
broadcast. The variant V_sched cancels at the game's scheduled start instead (a schedule is public days ahead, so this
uses no information from after the decision). Event ticker: <SERIES>-<YY><MON><DD><TEAM1><TEAM2>.
Usage: .venv/bin/python -m lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_espn"""
from __future__ import annotations
import datetime as dt, json, re, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_frozen_c1_other_mention_formats_data import OUT

LEAGUE = {"KXMLBMENTION": "baseball/mlb", "KXNBAMENTION": "basketball/nba", "KXNHLMENTION": "hockey/nhl", "KXWNBAMENTION": "basketball/wnba"}
ALIAS = {"ARI": ["AZ"], "CHW": ["CWS"], "NY": ["NYK", "NY", "NYL"], "SA": ["SAS"], "CON": ["CONN", "CON"], "GS": ["GSW", "GS"], "WSH": ["WSH", "WAS"],
         "LV": ["LV", "LVA"], "LA": ["LA", "LAS"], "POR": ["PDX", "POR"], "PHX": ["PHX", "PHO"], "TB": ["TB", "TBR"], "VGK": ["VGK", "VEG"],
         "NO": ["NOP", "NO"], "UTAH": ["UTA", "UTAH"]}
MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
CACHE = OUT / "espn"


def get(url: str) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (re.sub(r"[^A-Za-z0-9]+", "_", url.split("/sports/")[1]) + ".json")
    if f.exists():
        return json.loads(f.read_text())
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=30) as r:
                txt = r.read().decode()
            f.write_text(txt); time.sleep(0.5)
            return json.loads(txt)
        except Exception as e:
            print("retry", i, str(e)[:80]); time.sleep(3)
    return {}


def aliases(abbr: str) -> list[str]:
    return list({abbr, *ALIAS.get(abbr, [])})


def start_of(event: str) -> dict:
    s, rest = event.split("-", 1)
    m = re.match(r"(\d\d)([A-Z]{3})(\d\d)([A-Z]+)$", rest)
    if not m or s not in LEAGUE:
        return {"event": event, "start": None, "why": "special event (no date/teams in the ticker)"}
    day = dt.date(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3))); teams = m.group(4)
    for d in (day, day + dt.timedelta(days=1), day - dt.timedelta(days=1)):
        j = get(f"https://site.api.espn.com/apis/site/v2/sports/{LEAGUE[s]}/scoreboard?dates={d:%Y%m%d}")
        for g in j.get("events") or []:
            comp = (g.get("competitions") or [{}])[0]
            ab = [c.get("team", {}).get("abbreviation", "") for c in comp.get("competitors") or []]
            if len(ab) != 2:
                continue
            ok = any(a + b == teams for x, y in (ab, ab[::-1]) for a in aliases(x) for b in aliases(y))
            if ok:
                st = int(dt.datetime.fromisoformat(g["date"].replace("Z", "+00:00")).timestamp())
                return {"event": event, "start": st, "espn_id": g.get("id"), "name": g.get("shortName"), "date_utc": g["date"], "lookup_day": str(d)}
    return {"event": event, "start": None, "why": "no ESPN match"}


if __name__ == "__main__":
    O = [json.loads(l) for l in (OUT / "orders.jsonl").open()]
    evs = sorted({o["e"] for o in O if o["group"] == "sports"})
    res = {e: start_of(e) for e in evs}
    (OUT / "sports_starts.json").write_text(json.dumps(res, indent=1))
    miss = [e for e, v in res.items() if v["start"] is None]
    print(len(evs), "events,", len(evs) - len(miss), "matched; unmatched:", miss)
