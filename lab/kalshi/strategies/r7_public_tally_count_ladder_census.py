"""r7_public_tally_count_ladder_census - Step 0 census (and, only if it passes, Step 1) of Kalshi count ladders on a
running count that has a free, keyless, timestamped live tally.

Hypothesis (family): where Kalshi lists a bracket/threshold ladder on a running count (Truth Social posts, executive
orders, bills signed, YouTube views, Spotify streams, nominations, ...), retail prices the remaining increments
symmetrically around the mean and underweights the low-count mass of an over-dispersed process, so a negative-binomial
model of the remaining count (phi fitted on discovery) would beat the mid late in each window.

Step 0 (this file, `census`): <= 25 Kalshi listing calls, no candles. One settled listing per series for the live tier
(min_close_ts 2026-08-01) and one /historical listing (settled before 2026-08-08), served from earlier rounds' caches
where possible. A series QUALIFIES when
    settled events (live + archive, deduplicated by ticker) >= 20, median event volume >= 5,000 contracts, and the tally
    is free, keyless and timestamped (TALLY table below; classified from the settlement rules and a keyless probe),
    and it was not already killed by an equivalent remaining-increment test in an earlier round.
KILL the family when fewer than 3 series or fewer than 150 events qualify (pre-registered in the task, before the census).
Step 1 (NB model vs mid on discovery, then one frozen C1 rule on validation) runs only if the census passes.

Usage: .venv/bin/python -m lab.kalshi.strategies.r7_public_tally_count_ladder_census census
Output: data/kalshi_lab/strategies/r7_public_tally_count_ladder_census/census.json"""
from __future__ import annotations

import datetime as dt
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_api import OUT, calls_used, cached, get, K  # noqa: E402

LIVE_MIN_CLOSE = 1785542400          # 2026-08-01 00:00 UTC (the live tier holds ~68 days)
CENSUS_CAP = 25
MIN_EVENTS, MIN_MED_VOL = 20, 5000
KILL_MIN_SERIES, KILL_MIN_EVENTS = 3, 150

# Tally classification: (free, keyless, timestamped, live-within-window, source, note). Decided from the settlement
# rules and the sources' public interfaces BEFORE looking at any price data.
TALLY = {
    "KXTRUTHSOCIAL": (True, True, True, True, "Roll Call / Factba.se JSON feed (per-post timestamps, deletion flags)",
                      "the r6 template; all 34 events burned in r6 discovery/validation; forward logger already runs"),
    "KXEOWEEK": (True, True, True, True, "Federal Register API documents.json + public-inspection.json (signing_date, "
                 "filed_at) and whitehouse.gov/presidential-actions (post time)", "threshold ladder Above 0/1/2; settles on FR signing date; "
                 "closes when FR posts (days after the week)"),
    "KXBILLSCOUNTWEEKLY": (True, True, True, True, "whitehouse.gov briefings ('signed into law' posts); settlement on congress.gov",
                           "congress.gov API needs an api.data.gov key; HTML is bot-protected"),
    "KXBILLSCOUNT": (True, True, True, True, "same as KXBILLSCOUNTWEEKLY", "monthly"),
    "KXBILLSSIGNED": (True, True, True, True, "same as KXBILLSCOUNTWEEKLY", "monthly"),
    "KXBILLCOUNTM": (True, False, True, True, "congress.gov (Library of Congress) bill counts", "congress.gov API needs a key; "
                     "no keyless timestamped mirror of the settlement count"),
    "KXJUDGECOUNT": (True, True, True, True, "senate.gov roll-call vote XML (keyless, timestamped)", "voice-vote/en-bloc confirmations "
                     "are missing from the XML"),
    "KXPARDONSTRUMP": (True, True, False, False, "justice.gov/pardon clemency-grant lists", "posted with a lag of days, "
                       "not timestamped per grant at posting"),
    "KXTORNADO": (True, True, True, True, "NOAA SPC daily storm-report CSVs", "preliminary reports overcount; settlement on NOAA's "
                  "monthly count"),
    "KXYTVIEWSW": (True, False, True, False, "YouTube Charts (charts.youtube.com, JS app over an internal keyed API)",
                   "settles on a daily chart figure published after the day; no intraday public tally of 'Global daily views'"),
    "KXYTVIEWSD": (True, False, True, False, "same as KXYTVIEWSW", "daily snapshot, not a running count"),
    "KXSPOTSTREAMSUSA": (True, True, True, False, "kworb.net Spotify US daily chart", "published once per day after the chart day: "
                         "no intraday running tally"),
    "KXTRUMPNOMNUM": (True, True, True, True, "whitehouse.gov 'Nominations Sent to the Senate' posts", "one-off series"),
    "KXSPOTIFYSONGSFAMEISAGUN": (True, True, True, True, "kworb.net per-song daily totals (Spotify counter, daily)",
                                 "one-off song series (first-week streams)"),
    "KXSPOTIFYSONGSHALFTHEPLOT": (True, True, True, True, "kworb.net per-song daily totals", "one-off song series"),
    "KXSPOTIFYNUM1M": (True, True, True, True, "kworb.net Spotify daily chart", "monthly count of days at #1"),
    "KXYTVIEWSHIGH": (True, False, True, False, "YouTube Charts", "monthly max of daily views"),
    # already tested with an equivalent remaining-increment model and dead (EARLIER FAILURES): listed, not pooled
    "KXTSAW": (True, True, True, False, "tsa.gov throughput table (next-day lag)", "r3_tsa_weekly_accumulator: dead"),
    "KXBIGGESTQUAKE": (True, True, True, True, "USGS real-time feed", "running max, not a count; quake_entertain (r1): dead"),
}
BURNED = {"KXTRUTHSOCIAL": "r6 (all events used in discovery/validation)", "KXTSAW": "r3 (dead)", "KXBIGGESTQUAKE": "r1 (dead)"}

# live listing only where the archive is known to be empty or already cached; historical first page otherwise
PLAN = ["KXTRUTHSOCIAL", "KXEOWEEK", "KXBILLSCOUNTWEEKLY", "KXBILLSCOUNT", "KXBILLSSIGNED", "KXBILLCOUNTM", "KXJUDGECOUNT",
        "KXPARDONSTRUMP", "KXTORNADO", "KXYTVIEWSW", "KXYTVIEWSD", "KXSPOTSTREAMSUSA", "KXTRUMPNOMNUM",
        "KXSPOTIFYSONGSFAMEISAGUN", "KXSPOTIFYSONGSHALFTHEPLOT", "KXSPOTIFYNUM1M"]
CACHED_ONLY = {"KXTSAW", "KXBIGGESTQUAKE"}   # summarised from earlier rounds' result files, no calls


def fnum(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def live_urls(s: str) -> list[str]:
    """Prefer an earlier round's cached live listing (any min_close_ts at or before 2026-08-05) over a new call."""
    cands = [f"/markets?series_ticker={s}&status=settled&min_close_ts={m}&limit=1000" for m in (1785542400, 1785643200, 1785888000)]
    cands.append(f"/markets?series_ticker={s}&status=settled&limit=1000")
    return cands


def listing(s: str) -> dict:
    out = {"live": [], "archive": [], "live_truncated": False, "archive_truncated": False, "sources": []}
    for u in live_urls(s):
        d = cached(K + u)
        if d is not None:
            out["live"] = d.get("markets") or []; out["live_truncated"] = bool(d.get("cursor")) and len(out["live"]) >= 1000
            out["sources"].append("cache:" + u); break
    else:
        u = live_urls(s)[0]
        if calls_used("census") >= CENSUS_CAP:
            raise RuntimeError("census call cap")
        d = get(u, tag="census", cap=CENSUS_CAP)
        out["live"] = d.get("markets") or []; out["live_truncated"] = bool(d.get("cursor")) and len(out["live"]) >= 1000
        out["sources"].append("live:" + u)
    u = f"/historical/markets?series_ticker={s}&limit=1000"
    d = cached(K + u)
    if d is None:
        if calls_used("census") >= CENSUS_CAP:
            raise RuntimeError("census call cap")
        d = get(u, tag="census", cap=CENSUS_CAP); out["sources"].append("live:" + u)
    else:
        out["sources"].append("cache:" + u)
    out["archive"] = d.get("markets") or []; out["archive_truncated"] = bool(d.get("cursor")) and len(out["archive"]) >= 1000
    return out


def summarise(s: str, L: dict) -> dict:
    ms = {m["ticker"]: m for m in L["archive"] + L["live"] if m.get("result") in ("yes", "no")}
    ev = defaultdict(list)
    for m in ms.values():
        ev[m["event_ticker"]].append(m)
    evvol = sorted(sum(fnum(m.get("volume_fp") or m.get("volume")) for m in x) for x in ev.values())
    closes = sorted(ts(m["close_time"]) for m in ms.values() if m.get("close_time"))
    n_live = len({m["event_ticker"] for m in L["live"] if m.get("result") in ("yes", "no")})
    n_arch = len({m["event_ticker"] for m in L["archive"] if m.get("result") in ("yes", "no")})
    per_ev = [len(x) for x in ev.values()]
    stypes = sorted({str(m.get("strike_type")) for m in ms.values()})
    tl = TALLY.get(s)
    tally_ok = bool(tl and tl[0] and tl[1] and tl[2] and tl[3])
    r = {"series": s, "markets": len(ms), "events": len(ev), "events_live_tier": n_live, "events_archive": n_arch,
         "median_event_volume": round(st.median(evvol)) if evvol else 0, "q25_event_volume": round(evvol[len(evvol) // 4]) if evvol else 0,
         "markets_per_event_median": st.median(per_ev) if per_ev else 0, "strike_types": stypes,
         "first_close": dt.datetime.fromtimestamp(closes[0], dt.timezone.utc).date().isoformat() if closes else None,
         "last_close": dt.datetime.fromtimestamp(closes[-1], dt.timezone.utc).date().isoformat() if closes else None,
         "truncated": L["live_truncated"] or L["archive_truncated"],
         "events_last_30d": len({m["event_ticker"] for m in ms.values() if ts(m["close_time"]) and ts(m["close_time"]) >= 1791518400 - 30 * 86400}),
         "tally": {"free": tl[0], "keyless": tl[1], "timestamped": tl[2], "live_within_window": tl[3], "source": tl[4], "note": tl[5]} if tl else None,
         "tally_ok": tally_ok, "burned": BURNED.get(s), "rules_sample": ((next(iter(ms.values())).get("rules_primary") or "")[:240] if ms else ""),
         "listing_sources": L["sources"]}
    r["qualifies"] = (r["events"] >= MIN_EVENTS and r["median_event_volume"] >= MIN_MED_VOL and tally_ok and not BURNED.get(s))
    why = []
    if r["events"] < MIN_EVENTS:
        why.append(f"events {r['events']} < {MIN_EVENTS}")
    if r["median_event_volume"] < MIN_MED_VOL:
        why.append(f"median event volume {r['median_event_volume']} < {MIN_MED_VOL}")
    if not tally_ok:
        why.append("no free keyless timestamped live tally of the settlement count")
    if BURNED.get(s):
        why.append("burned: " + BURNED[s])
    r["fails"] = why
    return r


def burned_summary(s: str) -> dict:
    """Series already killed in earlier rounds: report from their own outputs (no calls)."""
    note = {"KXTSAW": "data/kalshi_lab/strategies/r3_tsa_weekly_accumulator/result.json",
            "KXBIGGESTQUAKE": "data/kalshi_lab/strategies/quake_entertain/result.json"}[s]
    tl = TALLY[s]
    return {"series": s, "events": None, "median_event_volume": None, "qualifies": False, "burned": BURNED[s],
            "tally": {"free": tl[0], "keyless": tl[1], "timestamped": tl[2], "live_within_window": tl[3], "source": tl[4], "note": tl[5]},
            "fails": ["burned: " + BURNED[s]], "see": note}


def census() -> dict:
    rows = []
    for s in PLAN:
        L = listing(s)
        rows.append(summarise(s, L))
        print(f"{s:28s} ev={rows[-1]['events']:4d} (live {rows[-1]['events_live_tier']:3d}, arch {rows[-1]['events_archive']:3d}) "
              f"med_vol={rows[-1]['median_event_volume']:8d} last={rows[-1]['last_close']} q={rows[-1]['qualifies']} {'; '.join(rows[-1]['fails'])}",
              flush=True)
    for s in sorted(CACHED_ONLY):
        rows.append(burned_summary(s))
    q = [r for r in rows if r["qualifies"]]
    n_ev = sum(r["events"] for r in q)
    # most generous pool: also count series that fail ONLY on being burned or on the tally (upper bound on reachable n)
    generous = [r for r in rows if r.get("events") and r["events"] >= MIN_EVENTS and (r.get("median_event_volume") or 0) >= MIN_MED_VOL]
    killed = len(q) < KILL_MIN_SERIES or n_ev < KILL_MIN_EVENTS
    out = {"criteria": {"min_events": MIN_EVENTS, "min_median_event_volume": MIN_MED_VOL, "tally": "free + keyless + timestamped + live within the window",
                        "kill": f"fewer than {KILL_MIN_SERIES} series or fewer than {KILL_MIN_EVENTS} events qualify"},
           "rows": rows, "qualifying_series": [r["series"] for r in q], "qualifying_events": n_ev,
           "generous_pool_ignoring_tally_and_burn": {"series": [r["series"] for r in generous], "events": sum(r["events"] for r in generous)},
           "killed": killed, "census_calls_used": calls_used("census"), "calls_used_total": calls_used()}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "census.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("qualifying_series", "qualifying_events", "generous_pool_ignoring_tally_and_burn", "killed",
                                          "census_calls_used", "calls_used_total")}, indent=1))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "census"
    if cmd == "census":
        census()
