"""Data for r4_official_posting_lag_study: Kalshi markets that resolve on an official, timestamped posting
(Senate / House roll-call XML, Supreme Court opinions), the official timestamps, and 1-minute candles around them.

Step 1 (list): series chosen from the catalog's settlement_sources only (rules.senate.gov / congress.gov /
               clerk.house.gov / supremecourt.gov) and titles that name one roll call or one opinion, before any price
               or result was seen. /historical/markets?series_ticker=S (archive, settled before 2026-08-08); the live
               tier /markets?series_ticker=S&status=settled only when the archive is empty.
Step 2 (official): Senate XML vote_date + modify_date, House XML action-time, supremecourt.gov opinion PDF headers.
Step 3 (candles): one /historical/markets/{t}/candlesticks (or batch /markets/candlesticks) call per market,
               period 1 min, [t_evt - 3 h, t_evt + 3 h].
Usage: python -m lab.kalshi.strategies.r4_official_posting_lag_study_data list"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_official_posting_lag_study_api import kget, used, OUT

# Priority order (budget): Senate vote-count / per-senator markets on single roll calls, House roll calls, SCOTUS.
SENATE = ["KXHEGSETHCOUNT", "KXVOTEHEGSETH", "KXVOTEPATEL", "KXVOTEDPATEL", "KXVOTERFK", "KXVOTEDRFK", "KXVOTETULSI",
          "KXVOTEDTULSI", "KXMCMAHONCOUNT", "KXVOTEMCMAHON", "KXZELDINCOUNT", "KXBURGUMCOUNT", "KXLUTNICKCOUNT",
          "KXROLLINSCOUNT", "KXWRIGHTCOUNT", "KXVOUGHTCOUNT", "KXWALTZCOUNT", "KXVOTEMIRAN", "KXVOTEGREER",
          "KXVOTECHAVEZDEREMER", "KXVOTERECNCS", "KXVOTETARIFFS", "KXCONGRESSTARIFFCOUNT", "KXVOTEBLANCHE",
          "KXVOTEMEANS", "KXVOTELAKE", "KXVOTEMULLIN", "KXVOTEOVERTON", "KXVOTEANTONI", "KXVOTEFEDCHAIR",
          "KXVOTECLARITY", "KXVOTESHUTDOWNS", "KXVOTERECISSION", "KXVOTEBUDGETRESS", "KXTRUMPNOMMAX"]
HOUSE = ["KXVOTERECNCH", "KXHOUSEEPSTEIN", "KXVOTESHUTDOWNH", "KXEXPELSWALWELLVOTES"]
SCOTUS = ["KXTRUMPSCOTUSVOTE", "KXVRASCOTUSVOTE", "KXTRUMPSLAUGHTERVOTE", "KXSKRMETTI", "KXWATSONRNC", "KXTARIFFS"]
DAY = 86400


def ts(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def row(m: dict, tier: str, kind: str) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0], "kind": kind, "tier": tier,
            "open": ts(m.get("open_time")), "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")),
            "settled": ts(m.get("settlement_ts")), "result": m.get("result"), "type": m.get("market_type"),
            "strike": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
            "vol": float(m.get("volume_fp") or m.get("volume") or 0), "last": m.get("last_price_dollars"),
            "title": (m.get("title") or "")[:140], "sub": (m.get("yes_sub_title") or "")[:80],
            "rules": (m.get("rules_primary") or "")[:400]}


def list_markets() -> None:
    f = OUT / "markets.jsonl"
    rows = {json.loads(l)["t"]: json.loads(l) for l in f.open()} if f.exists() else {}
    done = {r["series"] for r in rows.values()}
    for kind, ss in (("senate", SENATE), ("house", HOUSE), ("scotus", SCOTUS)):
        for s in ss:
            d = kget(f"/historical/markets?series_ticker={s}&limit=1000")
            ms = d.get("markets", []) or []
            for m in ms:
                rows[m["ticker"]] = row(m, "hist", kind)
            if not ms:
                d = kget(f"/markets?series_ticker={s}&status=settled&limit=1000")
                for m in d.get("markets", []) or []:
                    rows[m["ticker"]] = row(m, "live", kind)
            print(f"{s:24s} hist {len(ms):4d}  total {len(rows):5d}  calls {used()}", flush=True)
            with f.open("w") as g:
                for r in rows.values():
                    g.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "list":
        list_markets()


# ---------------------------------------------------------------- step 2/3: events and candles
import datetime as _dt
from zoneinfo import ZoneInfo
ETZ = ZoneInfo("America/New_York")


def _et(s: str) -> int:
    return int(_dt.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ETZ).timestamp())


# Each event = one official resolving record. senate: roll-call id in senate_votes.json (vote_date = official vote
# time, modify_date = latest XML edit, an upper bound on the XML's first posting); house: clerk XML action-time;
# scotus: opinion day, 10:00 ET (opinions are posted on supremecourt.gov from 10:00 in an order not announced).
EVENTS = [
    # id, kind, official record, official time (ET) or None (=from senate_votes.json), event tickers
    ("S-HEGSETH", "senate", "119-1-15", None, ["KXHEGSETHCOUNT-25", "KXVOTEHEGSETH-26"]),
    ("S-ZELDIN", "senate", "119-1-24", None, ["KXZELDINCOUNT-25"]),
    ("S-BURGUM", "senate", "119-1-26", None, ["KXBURGUMCOUNT-25"]),
    ("S-WRIGHT", "senate", "119-1-30", None, ["KXWRIGHTCOUNT-25"]),
    ("S-VOUGHT", "senate", "119-1-37", None, ["KXVOUGHTCOUNT-25"]),
    ("S-GABBARD", "senate", "119-1-50", None, ["KXVOTETULSI-26", "KXVOTEDTULSI"]),
    ("S-RFK", "senate", "119-1-52", None, ["KXVOTERFK-26", "KXVOTEDRFK"]),
    ("S-ROLLINS", "senate", "119-1-53", None, ["KXROLLINSCOUNT-25"]),
    ("S-LUTNICK", "senate", "119-1-57", None, ["KXLUTNICKCOUNT-25"]),
    ("S-PATEL", "senate", "119-1-61", None, ["KXVOTEPATEL-26", "KXVOTEDPATEL"]),
    ("S-GREER", "senate", "119-1-94", None, ["KXVOTEGREER-26"]),
    ("S-MCMAHON", "senate", "119-1-99", None, ["KXMCMAHONCOUNT-25", "KXVOTEMCMAHON-26"]),
    ("S-CHAVEZ", "senate", "119-1-111", None, ["KXVOTECHAVEZDEREMER-26"]),
    ("S-MIRAN", "senate", "119-1-117", None, ["KXVOTEMIRAN-26"]),
    ("S-WALTZ", "senate", "119-1-530", None, ["KXWALTZCOUNT-25"]),
    ("S-OBBBA", "senate", "119-1-372", None, ["KXVOTERECNCS-26"]),
    ("S-CR-NOV25", "senate", "119-1-618", None, ["KXVOTESHUTDOWNS-263"]),
    ("S-APPROP-JAN15", "senate", "119-2-11", None, ["KXVOTESHUTDOWNS-27"]),
    ("S-APPROP-JAN30", "senate", "119-2-20", None, ["KXVOTESHUTDOWNS-MAR26"]),
    ("S-MULLIN", "senate", "119-2-63", None, ["KXVOTEMULLIN-27"]),
    ("S-BUDGETRES", "senate", "119-2-105", None, ["KXVOTEBUDGETRESS-JAN27"]),
    ("S-WARSH", "senate", "119-2-120", None, ["KXVOTEFEDCHAIR-27"]),
    ("S-RECON26", "senate", "119-2-163", None, ["KXVOTERECNCS-MAY26"]),
    ("S-BLANCHE-AG", "senate", "119-2-230", None, ["KXVOTEBLANCHE-27"]),
    ("S-CLARITY", "senate", "119-2-234", None, ["KXVOTECLARITY-26SEP15"]),
    ("H-OBBBA-MAY", "house", "2025 roll 145", "2025-05-22 06:54", ["KXVOTERECNCH-26"]),
    ("H-CR-SEP19", "house", "2025 roll 281", "2025-09-19 10:44", ["KXVOTESHUTDOWNH-25"]),
    ("H-CR-NOV12", "house", "2025 roll 285", "2025-11-12 20:21", ["KXVOTESHUTDOWNH-26"]),
    ("H-EPSTEIN", "house", "2025 roll 289", "2025-11-18 14:43", ["KXHOUSEEPSTEIN-26JAN01", "KXHOUSEEPSTEIN-26JAN01V"]),
    ("H-APPROP-JAN8", "house", "2026 roll 7", "2026-01-08 15:10", ["KXVOTESHUTDOWNH-27"]),
    ("H-APPROP-FEB3", "house", "2026 roll 53", "2026-02-03 14:09", ["KXVOTESHUTDOWNH-APR26"]),
    ("C-SKRMETTI", "scotus", "23-477 (6/18/25)", "2025-06-18 10:00", ["KXSKRMETTI"]),
    ("C-TARIFFS", "scotus", "24-1287 (2/20/26)", "2026-02-20 10:00", ["KXTRUMPSCOTUSVOTE-26"]),
    ("C-CALLAIS", "scotus", "24-109 (4/29/26)", "2026-04-29 10:00", ["KXVRASCOTUSVOTE-26"]),
    ("C-WATSON", "scotus", "24-1260 (6/29/26)", "2026-06-29 10:00", ["KXWATSONRNC"]),
    ("C-SLAUGHTER", "scotus", "25-332 (6/29/26)", "2026-06-29 10:00", ["KXTRUMPSLAUGHTERVOTE-26"]),
]
PRE_MIN, POST_MAX_MIN = 180, 480


def event_rows() -> list[dict]:
    sv = json.loads((OUT / "senate_votes.json").read_text())
    ms = [json.loads(l) for l in (OUT / "markets.jsonl").open()]
    out = []
    for eid, kind, rec, when, evs in EVENTS:
        if kind == "senate":
            t_evt, t_post = sv[rec]["vote_ts"], sv[rec]["modify_ts"]
        else:
            t_evt, t_post = _et(when), None
        sel = []
        for e in evs:
            mm = [m for m in ms if m["e"] == e and m["vol"] >= 200]
            mm.sort(key=lambda m: -m["vol"])
            k = 3 if len(mm) >= 10 and not any(ch.isdigit() for ch in mm[0]["sub"][:3]) else 2
            pick = mm[:k] if len(evs) == 1 else mm[:(2 if len(mm) > 1 else 1)]
            win = [m for m in mm if m["result"] == "yes"]
            if win and any(ch.isdigit() for ch in win[0]["sub"]) and win[0] not in pick:
                pick.append(win[0])        # count buckets: the winning bucket carries the full repricing
            sel += pick
        out.append({"id": eid, "kind": kind, "record": rec, "t_evt": t_evt, "t_post": t_post,
                    "markets": [{"t": m["t"], "tier": m["tier"], "close": m["close"], "result": m["result"],
                                 "vol": m["vol"], "sub": m["sub"]} for m in sel]})
    return out


def _compact(cs: list[dict]) -> list[list]:
    def g(c, k, sub):
        d = c.get(k) or {}
        v = d.get(sub + "_dollars", d.get(sub))
        return float(v) if v not in (None, "") else None
    return [[int(c["end_period_ts"]), g(c, "yes_ask", "close"), g(c, "yes_bid", "close"), g(c, "yes_ask", "low"),
             g(c, "yes_bid", "high"), float(c.get("volume_fp") or c.get("volume") or 0), g(c, "price", "close"),
             g(c, "price", "high"), g(c, "price", "low")] for c in cs]


def candles(max_calls: int = 100) -> None:
    ev = event_rows()
    (OUT / "events.json").write_text(json.dumps(ev, indent=1))
    cf = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in cf.open()} if cf.exists() else set()
    start = used()
    with cf.open("a") as fh:
        for e in ev:
            live = [m for m in e["markets"] if m["tier"] == "live" and m["t"] not in have]
            for m in e["markets"]:
                if m["t"] in have or m["tier"] == "live":
                    continue
                if used() - start >= max_calls:
                    print("budget stop"); return
                lo = e["t_evt"] - PRE_MIN * 60
                hi = min(m["close"] + 600, e["t_evt"] + POST_MAX_MIN * 60)
                d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={lo}&end_ts={hi}&period_interval=1")
                cs = d.get("candlesticks")
                if cs is None:
                    print("no candles", m["t"], str(d)[:200]); continue
                fh.write(json.dumps({"t": m["t"], "ev": e["id"], "c": _compact(cs)}) + "\n"); fh.flush()
                have.add(m["t"]); print(e["id"], m["t"], len(cs), "calls", used(), flush=True)
            if live:
                lo = e["t_evt"] - PRE_MIN * 60
                hi = min(max(m["close"] for m in live) + 600, e["t_evt"] + POST_MAX_MIN * 60)
                d = kget(f"/markets/candlesticks?market_tickers={','.join(m['t'] for m in live)}&start_ts={lo}&end_ts={hi}&period_interval=1")
                for mk in d.get("markets", []) or []:
                    t = mk.get("market_ticker") or mk.get("ticker")
                    fh.write(json.dumps({"t": t, "ev": e["id"], "c": _compact(mk.get("candlesticks", []))}) + "\n")
                    have.add(t); print(e["id"], t, len(mk.get("candlesticks", [])), "calls", used(), flush=True)
                if not d.get("markets"):
                    print("live batch empty", e["id"], str(d)[:200])


if __name__ == "__main__" and sys.argv[1] in ("events", "candles"):
    if sys.argv[1] == "events":
        for e in event_rows():
            print(e["id"], e["t_evt"], e["t_post"], [(m["t"].split("-")[-1], m["result"], int(m["vol"])) for m in e["markets"]])
    else:
        candles(int(sys.argv[2]) if len(sys.argv) > 2 else 100)
