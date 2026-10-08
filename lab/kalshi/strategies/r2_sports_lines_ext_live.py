"""Live (no look-ahead) snapshots for r2_sports_lines_ext: the CURRENT DraftKings spread / total from ESPN scoreboards
next to the Kalshi open spread / total ladders, at whatever horizon before the start the games are. Each snapshot is
timestamped and stored under data/kalshi_lab/strategies/r2_sports_lines_ext/live/, so repeated snapshots also show
whether Kalshi moves after ESPN/DraftKings moves (lag test) - and the forward collector can be pointed at it.

Kalshi calls per snapshot: one open-markets list per series (+ cursor pages), counted against the study budget.
Usage: python -m lab.kalshi.strategies.r2_sports_lines_ext_live snap [SERIES,...] | report | book TICKER,..."""
from __future__ import annotations
import datetime as dt, json, re, statistics as stt, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import K
from lab.kalshi.strategies import r2_sports_lines_ext_fetch as F
from lab.kalshi.strategies.sports_books import assign, american, devig

LIVE = F.OUT / "live"
UA = {"User-Agent": "Mozilla/5.0 (research; polite)", "Accept": "application/json"}
LEAGUES = {"nfl": ("football", "nfl", [""]), "ncaaf": ("football", "college-football", ["&groups=80", "&groups=81"]),
           "mlb": ("baseball", "mlb", [""]), "nhl": ("hockey", "nhl", [""])}
SERIES = {"KXNFLTOTAL": "nfl", "KXNFLSPREAD": "nfl", "KXNCAAFTOTAL": "ncaaf", "KXNCAAFSPREAD": "ncaaf",
          "KXMLBTOTAL": "mlb", "KXMLBSPREAD": "mlb", "KXNHLTOTAL": "nhl", "KXNHLSPREAD": "nhl"}


def espn_get(url: str) -> dict:
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("espn retry", str(e)[:60], flush=True); time.sleep(3 * (i + 1))
    return {}


def num(x) -> float | None:
    try:
        return float(str(x).lstrip("ou").replace("+", ""))
    except Exception:
        return None


def espn_games(now: float) -> list[dict]:
    """Scheduled games in the next ~60 h with the current DraftKings spread / total and their prices."""
    days = sorted({(dt.datetime.fromtimestamp(now, dt.timezone.utc) + dt.timedelta(hours=h)).strftime("%Y%m%d") for h in (-6, 0, 24, 48)})
    out = {}
    for lg, (sport, path, groups) in LEAGUES.items():
        for d in days:
            for g in groups:
                j = espn_get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{path}/scoreboard?dates={d}&limit=300{g}")
                for e in j.get("events") or []:
                    c = e["competitions"][0]
                    if ((c.get("status") or {}).get("type") or {}).get("name") != "STATUS_SCHEDULED":
                        continue
                    start = int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp())
                    if not (now < start < now + 60 * 3600):
                        continue
                    teams = []
                    for x in c["competitors"]:
                        t = x.get("team") or {}
                        names = {t.get(k) for k in ("abbreviation", "displayName", "shortDisplayName", "location", "name", "nickname") if t.get(k)}
                        teams.append({"ha": x.get("homeAway"), "abbr": t.get("abbreviation"), "names": sorted(names)})
                    o = next((o for o in c.get("odds") or [] if (o.get("provider") or {}).get("name") == "DraftKings"), None)
                    if not o:
                        continue
                    ln = {}
                    ps = o.get("pointSpread") or {}
                    for ha in ("home", "away"):
                        cl = (ps.get(ha) or {}).get("close") or {}
                        ln[f"{ha}_pts"] = num(cl.get("line")); ln[f"{ha}_px"] = american(cl.get("odds"))
                    to = o.get("total") or {}
                    ov = (to.get("over") or {}).get("close") or {}; un = (to.get("under") or {}).get("close") or {}
                    ln["total"] = num(ov.get("line")); ln["over"] = american(ov.get("odds")); ln["under"] = american(un.get("odds"))
                    out[(lg, e["id"])] = {"lg": lg, "id": e["id"], "start": start, "teams": teams, "dk": ln}
    return list(out.values())


def kalshi_open(series: str) -> list[dict]:
    out = []; cursor = ""
    while True:
        d = F.kget(f"{K}/markets?series_ticker={series}&status=open&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        for m in d.get("markets") or []:
            out.append({"t": m["ticker"], "e": m["event_ticker"], "floor": m.get("floor_strike"), "type": m.get("strike_type"), "sub": m.get("yes_sub_title"),
                        "bid": float(m.get("yes_bid_dollars") or 0), "ask": float(m.get("yes_ask_dollars") or 0),
                        "bid_sz": float(m.get("yes_bid_size_fp") or 0), "ask_sz": float(m.get("yes_ask_size_fp") or 0),
                        "vol": float(m.get("volume_fp") or 0), "upd": m.get("updated_time"), "occ": m.get("occurrence_datetime")})
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets"):
            return out


def snap(series: list[str]) -> Path:
    now = time.time()
    games = espn_games(now)
    kal = {s: kalshi_open(s) for s in series}
    f = LIVE / f"snap_{int(now)}.json"
    LIVE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"ts": now, "games": games, "kalshi": kal}))
    print("snapshot", f, "games", len(games), {s: len(v) for s, v in kal.items()}, "calls so far", F.calls(), flush=True)
    return f


def match(snapd: dict) -> list[dict]:
    """Rows for every Kalshi open strike equal to the current DK main line (spread FAV -X.5 / total X.5)."""
    games = defaultdict(list)
    for g in snapd["games"]:
        games[g["lg"]].append(g)
    # Kalshi event suffix (shared by the spread and total events of one game) -> ESPN game, via the spread ladder's team names
    suffix_game = {}; suffix_codes = {}
    for s, ms in snapd["kalshi"].items():
        if not s.endswith("SPREAD"):
            continue
        ev = defaultdict(dict)
        for m in ms:
            code = re.sub(r"\d+$", "", m["t"].rsplit("-", 1)[1].upper())
            name = re.sub(r"\s+wins by.*$", "", m.get("sub") or "")
            ev[m["e"]][code] = {"t": f"X-{code}", "sub": name}
        for e, teams in ev.items():
            tk = list(teams.values())
            best = None
            for g in games[SERIES[s]]:
                mp = assign(tk, g)
                if mp:
                    best = g; codes = {k.split("-", 1)[1]: v for k, v in mp.items()}; break
            if best:
                suffix_game[(SERIES[s], e.split("-", 1)[1])] = best; suffix_codes[(SERIES[s], e.split("-", 1)[1])] = codes
    rows = []
    for s, ms in snapd["kalshi"].items():
        lg = SERIES[s]
        for m in ms:
            key = (lg, m["e"].split("-", 1)[1])
            g = suffix_game.get(key)
            if not g or m.get("type") != "greater" or m.get("floor") is None or not (0 < m["bid"] < m["ask"] < 1):
                continue
            dk = g["dk"]; x = float(m["floor"])
            if s.endswith("TOTAL"):
                if dk.get("total") != x or not dk.get("over") or not dk.get("under"):
                    continue
                raw = {"a": dk["over"], "b": dk["under"]}
            else:
                code = re.sub(r"\d+$", "", m["t"].rsplit("-", 1)[1].upper()); ha = suffix_codes[key].get(code)
                if not ha:
                    continue
                oth = "away" if ha == "home" else "home"
                if dk.get(f"{ha}_pts") != -x or not dk.get(f"{ha}_px") or not dk.get(f"{oth}_px"):
                    continue
                raw = {"a": dk[f"{ha}_px"], "b": dk[f"{oth}_px"]}
            mid = (m["bid"] + m["ask"]) / 2
            rows.append({"s": s, "lg": lg, "t": m["t"], "h_to_start": round((g["start"] - snapd["ts"]) / 3600, 2), "mid": mid, "spr": round(m["ask"] - m["bid"], 3),
                         "fair_mult": devig(raw, "mult")["a"], "fair_power": devig(raw, "power")["a"], "bid_sz": m["bid_sz"], "ask_sz": m["ask_sz"],
                         "vol": m["vol"], "upd": m["upd"]})
    return rows


def report() -> dict:
    out = {}
    for f in sorted(LIVE.glob("snap_*.json")):
        rows = match(json.loads(f.read_text()))
        if not rows:
            continue
        by = defaultdict(list)
        for r in rows:
            hb = "<3h" if r["h_to_start"] < 3 else "3-24h" if r["h_to_start"] < 24 else ">24h"
            for k in ("all", r["s"], hb):
                by[k].append(r)
        rep = {}
        for k, v in sorted(by.items()):
            g = sorted(abs(r["mid"] - r["fair_mult"]) for r in v); gp = sorted(abs(r["mid"] - r["fair_power"]) for r in v)
            rep[k] = {"n": len(v), "median_gap_mult_c": round(100 * stt.median(g), 2), "median_gap_power_c": round(100 * stt.median(gp), 2),
                      "p90_gap_mult_c": round(100 * g[int(0.9 * (len(g) - 1))], 2), "share_gap_ge_3c": round(sum(x >= 0.03 for x in g) / len(g), 3),
                      "median_spread_c": round(100 * stt.median(r["spr"] for r in v), 2),
                      "median_best_ask_size": stt.median(r["ask_sz"] for r in v), "median_best_bid_size": stt.median(r["bid_sz"] for r in v)}
        out[f.name] = {"rows": len(rows), "summary": rep, "largest_gaps": sorted(rows, key=lambda r: -abs(r["mid"] - r["fair_mult"]))[:8]}
    (F.OUT / "live_report.json").write_text(json.dumps(out, indent=1))
    return out


def lag() -> dict:
    """Pairs of consecutive snapshots: when the DK fair at a fixed strike moved by >= 1.5c, how much of that move had
    the Kalshi mid made by the second snapshot (slope of Kalshi change on DK change)."""
    snaps = [json.loads(f.read_text()) for f in sorted(LIVE.glob("snap_*.json"))]
    res = []
    for a, b in zip(snaps, snaps[1:]):
        ra = {r["t"]: r for r in match(a)}; rb = {r["t"]: r for r in match(b)}
        for t in set(ra) & set(rb):
            res.append({"t": t, "dt_min": round((b["ts"] - a["ts"]) / 60, 1), "d_dk": rb[t]["fair_mult"] - ra[t]["fair_mult"], "d_k": rb[t]["mid"] - ra[t]["mid"],
                        "gap_a": ra[t]["mid"] - ra[t]["fair_mult"], "gap_b": rb[t]["mid"] - rb[t]["fair_mult"]})
    mv = [r for r in res if abs(r["d_dk"]) >= 0.015]
    out = {"pairs": len(res), "dk_moves_ge_1.5c": len(mv), "moves": mv[:30]}
    if res:
        out["median_abs_gap_first_c"] = round(100 * stt.median(abs(r["gap_a"]) for r in res), 2)
        out["median_abs_gap_second_c"] = round(100 * stt.median(abs(r["gap_b"]) for r in res), 2)
    (F.OUT / "live_lag.json").write_text(json.dumps(out, indent=1))
    return out


def books(tickers: list[str]) -> list[dict]:
    out = []
    for t in tickers:
        d = F.kget(f"{K}/markets/{t}/orderbook")
        ob = d.get("orderbook_fp") or d.get("orderbook") or {}
        yes = ob.get("yes_dollars") or ob.get("yes") or []; no = ob.get("no_dollars") or ob.get("no") or []
        best_yes_bid = max(yes, key=lambda r: float(r[0])) if yes else None
        best_no_bid = max(no, key=lambda r: float(r[0])) if no else None
        out.append({"t": t, "ts": time.time(), "best_yes_bid": best_yes_bid, "best_no_bid": best_no_bid,
                    "yes_levels": len(yes), "no_levels": len(no)})
    f = LIVE / f"books_{int(time.time())}.json"
    f.write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "report"
    if what == "snap":
        snap(sys.argv[2].split(",") if len(sys.argv) > 2 else list(SERIES))
    elif what == "report":
        r = report()
        for k, v in r.items():
            print(k, v["rows"]); [print("  ", kk, vv) for kk, vv in v["summary"].items()]
        print(json.dumps(lag(), indent=0)[:1500])
    elif what == "book":
        print(json.dumps(books(sys.argv[2].split(",")), indent=0))
