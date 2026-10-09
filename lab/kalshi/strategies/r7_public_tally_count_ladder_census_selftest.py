"""Offline selftest for the r7 forward shadow (no network, no Kalshi calls). Exit code 0 = all checks pass.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_public_tally_count_ladder_census_selftest"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_api import cached, K  # noqa: E402
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_data import OUT, RAW, wh_items, wh_nominations, parse_feed  # noqa: E402
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_model import (Frozen, nb_pmf, compound, p_yes, strike_of,  # noqa: E402
                                                                             apply_kernel, tornado_remaining, KMAX)
from lab.kalshi.strategies import r7_public_tally_count_ladder_census_logger as L  # noqa: E402
from lab.kalshi.strategies.r7_public_tally_count_ladder_census_summary import main as summary_main, cp_lower  # noqa: E402

ET = L.ET
FAILS: list[str] = []


def check(name: str, cond: bool, info="") -> None:
    print(("ok   " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def mean(p):
    return sum(k * q for k, q in enumerate(p))


def settled(series: str, live_min: int) -> list[dict]:
    ms = {}
    for u in (f"/historical/markets?series_ticker={series}&limit=1000", f"/markets?series_ticker={series}&status=settled&min_close_ts={live_min}&limit=1000"):
        for m in (cached(K + u) or {}).get("markets", []):
            ms[m["ticker"]] = m
    return list(ms.values())


def main() -> int:
    # 1. NB / compound arithmetic
    p = nb_pmf(3.0, 0.7); check("nb pmf sums to 1", abs(sum(p) - 1) < 1e-9, f"{sum(p):.12f}"); check("nb mean", abs(mean(p) - 3.0) < 1e-3, f"{mean(p):.5f}")
    pb = nb_pmf(1.2, 1000.0); bp = [0, 0.6, 0.3, 0.1]
    c = compound(pb, bp); check("compound sums to 1", abs(sum(c) - 1) < 1e-9); check("compound mean = E[P] E[S]", abs(mean(c) - 1.2 * 1.5) < 1e-3, f"{mean(c):.4f}")
    k = apply_kernel([0, 0, 1.0] + [0.0] * (KMAX - 2), {-1: 0.1, 0: 0.8, 1: 0.1}, floor=2)
    check("kernel floored at FR count", abs(k[2] - 0.9) < 1e-12 and abs(k[3] - 0.1) < 1e-12)

    # 2. strikes
    fin = [0.2, 0.3, 0.5] + [0.0] * (KMAX - 2)
    check("greater", abs(p_yes(fin, "greater", 0, None) - 0.8) < 1e-12)
    check("greater_or_equal", abs(p_yes(fin, "greater_or_equal", 2, None) - 0.5) < 1e-12)
    check("between", abs(p_yes(fin, "between", 1, 1) - 0.3) < 1e-12)
    check("unparseable -> None", p_yes(fin, None, None, None) is None)
    check("fee10(0.50) = 1.8c", abs(L.fee10(0.5) - 0.018) < 1e-12, L.fee10(0.5)); check("fee10(0.90) = 0.7c", abs(L.fee10(0.9) - 0.007) < 1e-12, L.fee10(0.9))

    # 3. frozen model + every cached settled market parses to a strike and a 7-day / monthly window
    FZ = Frozen.load(OUT / "frozen_model.json")
    for s, lm in (("KXEOWEEK", 1785888000), ("KXTRUMPNOMNUM", 1785542400), ("KXTORNADO", 1785542400)):
        ms = settled(s, lm)
        st_ok = sum(strike_of(m)[0] is not None for m in ms)
        wins = [L.market_window(s, m) for m in ms]
        if s == "KXTORNADO":
            w_ok = sum(1 for w in wins if w and 28 * 86400 <= w[1] - w[0] <= 31 * 86400)
        else:
            w_ok = sum(1 for w in wins if w and abs((w[1] - w[0]) - 7 * 86400) <= 3600 and dt.datetime.fromtimestamp(w[0], ET).weekday() == 6)
        older = sum(1 for m in ms if "Sep 8, 2025 to Sep 15" in (m.get("rules_primary") or "") or "Sep 21, 2025 to Sep 27" in (m.get("rules_primary") or "")
                    or m.get("event_ticker") == "TORNADO-2330")   # legacy rule texts / one malformed 2023 event (skipped by the logger)
        check(f"{s}: strikes parse", st_ok == len(ms), f"{st_ok}/{len(ms)}")
        check(f"{s}: windows parse (Sun..Sun ET / UTC month)", w_ok >= len(ms) - older, f"{w_ok}/{len(ms)} (2025 legacy rule texts: {older})")

    # 4. degenerate end states
    A = int(dt.datetime(2026, 9, 27, tzinfo=ET).timestamp()); B = A + 7 * 86400
    pmf, info = FZ.weekly_pmf("KXTRUMPNOMNUM", A, B, B + 10, 17, 1)
    check("weekly pmf after window end = point mass at seen", abs(pmf[17] - 1) < 1e-9, info)
    pmf, info = FZ.weekly_pmf("KXEOWEEK", A, B, B + 10, 3, 1, fr_floor=3)
    check("EO pmf after window end: kernel floored at FR", abs(sum(pmf[3:5]) - 1) < 1e-9 and pmf[2] == 0, f"P3={pmf[3]:.3f} P4={pmf[4]:.3f}")
    pmf, info = FZ.weekly_pmf("KXEOWEEK", A, B, A + 3600, 0, 0)
    check("EO prior weekly mean ~ fitted mean", abs(mean(pmf) - FZ.eo["lam_batches"] * mean(FZ.eo["batch_pmf"])) < 0.25, f"{mean(pmf):.3f}")
    W_end = int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp())
    r = tornado_remaining(W_end + 5, W_end, 0, FZ.tor["month_par"], FZ.tor["hour_share_utc"]); check("tornado remaining after end = 0", abs(r[0] - 1) < 1e-12)
    r1 = tornado_remaining(W_end - 14 * 3600, W_end, 0, FZ.tor["month_par"], FZ.tor["hour_share_utc"])
    r2 = tornado_remaining(W_end - 14 * 3600, W_end, 30, FZ.tor["month_par"], FZ.tor["hour_share_utc"])
    check("tornado: an active day raises the remaining mean", mean(r2) > mean(r1), f"{mean(r1):.2f} -> {mean(r2):.2f}")

    # 5. nomination parser reproduces settled KXTRUMPNOMNUM thresholds (cached WH history)
    items = wh_items(pages=10, cache_dir=RAW / "wh_hist"); noms = wh_nominations(items)
    check("nomination posts parsed > 0 nominations", all(x["n_nominations"] > 0 for x in noms) and len(noms) >= 40, f"{len(noms)} posts")
    ok = bad = 0
    for m in settled("KXTRUMPNOMNUM", 1785542400):
        A_, B_ = L.market_window("KXTRUMPNOMNUM", m)
        S = L.tally_state("KXTRUMPNOMNUM", A_, B_, B_ + 36000, {"wh": items})
        stype, fl, cap = strike_of(m)
        pr = p_yes([1.0 if j == S["c_seen"] else 0.0 for j in range(KMAX + 1)], stype, fl, cap) > 0.5
        ok += pr == (m["result"] == "yes"); bad += pr != (m["result"] == "yes")
    check("logger tally_state reproduces KXTRUMPNOMNUM settlements", bad == 0 and ok > 0, f"{ok} ok, {bad} bad")

    # 6. synthetic pass: phase c1, one deliberately mispriced NO -> C1 signal, fill at 1 - yes_bid, forced flag kept
    t = B - 6 * 3600
    mk = lambda tk, fl, bid, ask: {"ticker": tk, "event_ticker": "KXEOWEEK-26OCT03", "strike_type": "greater", "floor_strike": fl,  # noqa: E731
                                   "rules_primary": "If the President signs above %d executive orders during Sep 27, 2026 to Oct 3, 2026, then the market resolves to Yes." % fl,
                                   "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask), "yes_bid_size_fp": "50.00", "yes_ask_size_fp": "40.00",
                                   "yes_sub_title": f"Above {fl}", "close_time": "2026-10-10T14:00:00Z"}
    T = {"wh": [], "fr": [], "pi": []}
    rows = L.evaluate("KXEOWEEK", [mk("X-2", 2, 0.45, 0.50), mk("X-0", 0, 0.02, 0.05)], t, T, FZ)
    check("synthetic: one row in phase c1", len(rows) == 1 and rows[0]["phase"] == "c1", rows[0]["phase"] if rows else None)
    sig = [s for s in rows[0]["signals"] if s["rule"] == "C1"]
    p_no = 1 - next(m["model"] for m in rows[0]["markets"] if m["t"] == "X-2")
    check("synthetic: C1 NO on 'Above 2' at 1 - bid", any(s["t"] == "X-2" and s["side"] == "NO" and abs(s["px"] - 0.55) < 1e-9 for s in sig), f"P(NO)={p_no:.3f}")
    check("synthetic: no C1 signal outside the price band", not any(s["t"] == "X-0" for s in sig))
    check("synthetic: idle window skipped unless forced", L.evaluate("KXEOWEEK", [mk("X-2", 2, 0.45, 0.50)], A + 3600, T, FZ) == [])
    fr = L.evaluate("KXEOWEEK", [mk("X-2", 2, 0.45, 0.50)], A + 3600, T, FZ, force=True)
    check("synthetic: forced pass logs but emits no signals", len(fr) == 1 and fr[0]["forced"] and fr[0]["signals"] == [])

    # 7. summary / gate arithmetic on synthetic settled rows
    check("Clopper-Pearson 10/10 lower bound", abs(cp_lower(10, 10) - 0.05 ** 0.1) < 1e-6, f"{cp_lower(10, 10):.4f}")
    fake = [{"rule": "C1", "t": f"T{i}", "event": f"E{i}", "series": "KXEOWEEK", "side": "NO", "px": 0.80, "fee": L.fee10(0.80), "ts": i,
             "won": i % 10 != 0, "ret_per_dollar": ((1.0 if i % 10 != 0 else 0.0) - 0.80 - L.fee10(0.80)) / 0.80, "size_at_px": 20} for i in range(50)]
    sm = summary_main(K=20175, rows=fake, write=False)["rules"]["C1"]
    check("summary: 50 events, 90% win at 0.80", sm["n_events"] == 50 and abs(sm["win"] - 0.9) < 1e-12, f"mean {sm['mean_ret_per_dollar']:+.3f} t {sm['t']:.2f}")
    check("summary: gate fails on t at K=20175", sm["gate"]["t"] is False and sm["gate_pass"] is False)
    k2 = summary_main(K=20175, rows=[dict(r, won=False, ret_per_dollar=-1.0) for r in fake[:25]], write=False)
    check("kill rule fires after 20 losing events", (k2["kill"] or "").startswith("mean per $"), k2["kill"])

    # 8. feed parser on a minimal synthetic RSS item
    xml = ("<rss><channel><item><title>Nominations and Withdrawal Sent to the Senate</title><link>https://x/1</link>"
           "<pubDate>Mon, 28 Sep 2026 19:59:28 +0000</pubDate><category><![CDATA[Nominations &amp; Appointments]]></category>"
           "<content:encoded><![CDATA[<p>NOMINATIONS SENT TO THE SENATE:</p><p>A B, of Ohio, to be Judge.</p><p>C D, of Utah, a Career Member"
           " of the Senior Foreign Service, Class of Counselor, to be Ambassador.</p><p>WITHDRAWAL SENT TO THE SENATE:</p>"
           "<p>E F, of Iowa, to be Marshal.</p>]]></content:encoded></item></channel></rss>")
    it = parse_feed(xml)
    check("feed parser: 2 nominations, withdrawal excluded, ET date", it and it[0]["n_nominations"] == 2 and it[0]["date_et"] == "2026-09-28",
          it[0] if it else None)
    print(f"\n{'ALL OK' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    (OUT / "selftest.json").write_text(json.dumps({"ran": dt.datetime.now(ET).isoformat(timespec="seconds"), "failures": FAILS}, indent=1))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
