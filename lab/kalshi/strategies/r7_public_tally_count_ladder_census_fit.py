"""Freeze the r7 forward-shadow count models from the PUBLIC TALLY history only (no Kalshi prices; no rule variants are
compared). Kalshi settled listings (already cached by the census, 0 calls) are used only to audit that each tally
reproduces the settlement values.

  KXEOWEEK      Federal Register EO signing dates, 52 complete Sun..Sat ET weeks 2025-09-28 .. 2026-09-26
                (raw/fr_eos_all.json); within-day timing from White House EO post times (raw/wh_hist).
  KXTRUMPNOMNUM White House 'Nominations Sent to the Senate' posts, nominations per ET publication date, same 52 weeks.
  KXTORNADO     SPC preliminary rough-log daily (convective-day) tornado counts 2015-2025 per calendar month; UTC hour
                profile 2015-2025 (raw/spc_ruf_NAT_<year>.json).
Writes frozen_model.json (+ sha1) and tally_audit.json.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_public_tally_count_ladder_census_fit"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_api import cached, K  # noqa: E402
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_data import (OUT, RAW, ET, wh_items, wh_eo,  # noqa: E402
                                                                            wh_nominations, spc_year)
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_model import fit_nb, strike_of, p_yes  # noqa: E402

W0 = dt.date(2025, 9, 28)          # first Sunday of the fit window
NW = 52                            # complete weeks (last: Sun 2026-09-20 .. Sat 2026-09-26)
TOR_YEARS = range(2015, 2026)


def week_index(d: dt.date) -> int | None:
    i = (d - W0).days // 7
    return i if 0 <= i < NW else None


def batch_pmf(sizes: list[int]) -> list[float]:
    """Empirical batch-size distribution over the fit window (index = size; no smoothing)."""
    out = [0.0] * (max(sizes) + 1)
    for x in sizes:
        out[x] += 1
    return [v / len(sizes) for v in out]


def fit_eo(items: list[dict]) -> tuple[dict, dict]:
    fr = json.loads((RAW / "fr_eos_all.json").read_text())["results"]
    sd = [dt.date.fromisoformat(r["signing_date"]) for r in fr if r.get("signing_date")]
    wk = [0] * NW; per_day = defaultdict(int)
    for d in sd:
        i = week_index(d)
        if i is not None:
            wk[i] += 1; per_day[d] += 1
    days_wk = [0] * NW; dow = [1.0] * 7
    for d in per_day:
        days_wk[week_index(d)] += 1; dow[(d.weekday() + 1) % 7] += 1
    hod = [1.0] * 24
    for x in wh_eo(items):
        hod[dt.datetime.fromtimestamp(x["ts"], ET).hour] += 1
    nb_days = fit_nb(days_wk); nb_cnt = fit_nb(wk)
    # audit: White House EO posts per week (ET date) vs Federal Register signing dates per week
    whw = [0] * NW
    for x in wh_eo(items):
        i = week_index(dt.date.fromisoformat(x["date_et"]))
        if i is not None:
            whw[i] += 1
    diff = [b - a for a, b in zip(whw, wk)]                 # FR minus WH
    kern = {str(k): diff.count(k) / NW for k in sorted(set(diff))}
    model = {"lam_batches": nb_days["mu"], "phi_batches": nb_days["phi"], "batch_pmf": batch_pmf(list(per_day.values())),
             "tally_error_kernel": kern, "weekly_counts": wk, "weekly_signing_days": days_wk,
             "nb_weekly_count_reference": {"mu": nb_cnt["mu"], "phi": nb_cnt["phi"], "var": nb_cnt["var"]},
             "profile": {"dow_share_from_Sunday": [x / sum(dow) for x in dow], "hour_share_ET": [x / sum(hod) for x in hod]},
             "fit_window": f"{W0} .. {W0 + dt.timedelta(days=7 * NW - 1)} (FR signing_date; batches = signing days; hour profile from WH EO post times)"}
    audit = {"wh_vs_fr_weekly_equal": f"{diff.count(0)}/{NW}", "wh_weekly": whw, "fr_weekly": wk, "fr_minus_wh": diff}
    return model, audit


def fit_nom(items: list[dict]) -> dict:
    wk = [0] * NW; posts_wk = [0] * NW; dow = [1.0] * 7; hod = [1.0] * 24; sizes = []
    for x in wh_nominations(items):
        d = dt.date.fromisoformat(x["date_et"]); i = week_index(d)
        if i is None:
            continue
        wk[i] += x["n_nominations"]; posts_wk[i] += 1; sizes.append(x["n_nominations"])
        dow[(d.weekday() + 1) % 7] += 1; hod[dt.datetime.fromtimestamp(x["ts"], ET).hour] += 1
    nb_posts = fit_nb(posts_wk); nb_cnt = fit_nb(wk)
    return {"lam_batches": nb_posts["mu"], "phi_batches": nb_posts["phi"], "batch_pmf": batch_pmf(sizes),
            "weekly_counts": wk, "weekly_posts": posts_wk,
            "nb_weekly_count_reference": {"mu": nb_cnt["mu"], "phi": nb_cnt["phi"], "var": nb_cnt["var"]},
            "profile": {"dow_share_from_Sunday": [x / sum(dow) for x in dow], "hour_share_ET": [x / sum(hod) for x in hod]},
            "fit_window": f"{W0} .. {W0 + dt.timedelta(days=7 * NW - 1)} (WH publication date ET; batches = nomination posts)"}


def fit_tornado() -> dict:
    per_m = defaultdict(list); hod = [1.0] * 24
    for y in TOR_YEARS:
        d = spc_year(y, cache=True)
        for k, v in d["daily"].items():
            per_m[int(k[:2])].append(int(v["torn"]))
        for h, v in enumerate(d["hour"]):
            hod[h] += v["torn"]
    mp = {}
    for m in range(1, 13):
        nb = fit_nb(per_m[m]); mp[str(m)] = {"mu": nb["mu"], "phi": nb["phi"], "n_days": nb["n"], "var": nb["var"]}
    return {"month_par": mp, "hour_share_utc": [x / sum(hod) for x in hod],
            "fit_window": f"SPC rough log {TOR_YEARS.start}-{TOR_YEARS.stop - 1}, convective-day counts per calendar month",
            "count_window": "UTC calendar month (SPC month totals assign reports by UTC date)"}


def audit_kalshi(items: list[dict]) -> dict:
    """Does each tally reproduce Kalshi's settled outcomes? (cached listings only)"""
    out = {}
    fr = json.loads((RAW / "fr_eos_all.json").read_text())["results"]
    sd = [dt.date.fromisoformat(r["signing_date"]) for r in fr if r.get("signing_date")]
    noms = wh_nominations(items)

    def listing(s, live_min):
        ms = {}
        for u in (f"/historical/markets?series_ticker={s}&limit=1000", f"/markets?series_ticker={s}&status=settled&min_close_ts={live_min}&limit=1000"):
            for m in (cached(K + u) or {}).get("markets", []):
                ms[m["ticker"]] = m
        return list(ms.values())

    def pred(n, m):
        stype, fl, cap = strike_of(m)
        if stype is None:
            return None
        return p_yes([1.0 if k == n else 0.0 for k in range(max(n, 1) + 1)], stype, fl, cap) > 0.5

    for s, live_min in (("KXEOWEEK", 1785888000), ("KXTRUMPNOMNUM", 1785542400)):
        ok = bad = skip = 0
        for m in listing(s, live_min):
            r = m.get("rules_primary") or ""
            mm = re.search(r"(?:during|from|between) (\w+ \d+, \d{4}) (?:to|through) (\w+ \d+, \d{4})", r)
            if not mm:
                skip += 1; continue
            a = dt.datetime.strptime(mm.group(1), "%b %d, %Y").date(); b = dt.datetime.strptime(mm.group(2), "%b %d, %Y").date()
            if s == "KXEOWEEK":
                n = sum(1 for d in sd if a <= d <= b)
            else:
                n = sum(x["n_nominations"] for x in noms if a.isoformat() <= x["date_et"] <= b.isoformat())
            p = pred(n, m)
            if p is None:
                skip += 1; continue
            ok += p == (m["result"] == "yes"); bad += p != (m["result"] == "yes")
        out[s] = {"markets_reproduced": ok, "mismatch": bad, "not_checkable": skip}
    rows = []
    spc = {y: spc_year(y, cache=True) for y in (2023, 2024, 2025, 2026)}
    MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
    ok = bad = 0
    for m in listing("KXTORNADO", 1785542400):
        mm = re.search(r"-(\d{2})([A-Z]{3})", m["event_ticker"])
        if not mm or 2000 + int(mm.group(1)) not in spc:
            continue
        n = int(spc[2000 + int(mm.group(1))]["month"].get(str(MON[mm.group(2)]), {}).get("torn", 0))
        p = pred(n, m)
        if p is None:
            continue
        good = p == (m["result"] == "yes"); ok += good; bad += not good
        if not good:
            rows.append({"t": m["ticker"], "spc_now": n, "result": m["result"], "expiration_value": m.get("expiration_value")})
    out["KXTORNADO"] = {"markets_reproduced_with_todays_spc": ok, "mismatch": bad, "mismatches": rows[:30],
                        "note": "SPC rough-log values are revised after settlement (Aug/Sep 2026: SPC now 100/57, Kalshi settled 98/55)"}
    return out


def main() -> None:
    items = wh_items(pages=10, cache_dir=RAW / "wh_hist")
    eo, eo_audit = fit_eo(items)
    model = {"frozen_at": dt.datetime.now(ET).isoformat(timespec="seconds"),
             "KXEOWEEK": eo, "KXTRUMPNOMNUM": fit_nom(items), "KXTORNADO": fit_tornado(),
             "sources": {"fr": "raw/fr_eos_all.json", "wh": "raw/wh_hist/wh_feed_p1..p10.xml", "spc": "raw/spc_ruf_NAT_2015..2025.json"}}
    txt = json.dumps(model, indent=1)
    (OUT / "frozen_model.json").write_text(txt)
    sha = hashlib.sha1(txt.encode()).hexdigest()
    audit = {"eo_wh_vs_fr": eo_audit, "kalshi_settlement_reproduction": audit_kalshi(items)}
    (OUT / "tally_audit.json").write_text(json.dumps(audit, indent=1))
    print("sha1", sha)
    for s in ("KXEOWEEK", "KXTRUMPNOMNUM"):
        print(s, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in model[s].items() if k in ("lam_batches", "phi_batches", "tally_error_kernel", "nb_weekly_count_reference")},
              "batch mean", round(sum(i * q for i, q in enumerate(model[s]["batch_pmf"])), 2))
    print("KXTORNADO", {m: (round(v["mu"], 2), round(v["phi"], 3)) for m, v in model["KXTORNADO"]["month_par"].items()})
    print(json.dumps(audit["kalshi_settlement_reproduction"], indent=1)[:1500])
    print("EO WH vs FR weekly equal:", eo_audit["wh_vs_fr_weekly_equal"], "FR-WH", eo_audit["fr_minus_wh"])


if __name__ == "__main__":
    main()
