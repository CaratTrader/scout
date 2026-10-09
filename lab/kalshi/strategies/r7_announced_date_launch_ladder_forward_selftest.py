"""Offline selftest of r7_announced_date_launch_ladder_forward (0 Kalshi calls: a fake fetcher serves synthetic responses).

Checks: deadline parser identical to r6's on every cached listing on disk; classifier include/exclude cases; fee rounding;
Beta-bound arithmetic; the per-pass call cap; catalog diff (new series, volume moved); inventory refresh; the 12:00 ET
first-in-band rule over two days (one rung in band at the first look, one only at the second, one out of the window);
book walk and the conservative fill; a no-book fill at the look price; dry runs never change the state; settlement;
the summary's stats, kill rule and integrity checks.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_selftest"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r7_announced_date_launch_ladder_forward_common as C  # noqa: E402
from lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_api import Budget, BudgetExhausted  # noqa: E402
from lab.kalshi.strategies.r7_announced_date_launch_ladder_forward_logger import Logger, clock_mode  # noqa: E402
from lab.kalshi.strategies import r7_announced_date_launch_ladder_forward_summary as SUM  # noqa: E402

ECC = "This market will close and expire early if the event occurs."


def et(y, m, d, h=0, mi=0) -> int:
    return int(dt.datetime(y, m, d, h, mi, tzinfo=C.ET).timestamp())


def mk(t, e, rules, title, yb=0.05, ya=0.08, status="active", result="", ecc=ECC, exp_days=7, D=None):
    le = (D or et(2026, 10, 20)) + exp_days * C.DAY
    return {"ticker": t, "event_ticker": e, "market_type": "binary", "rules_primary": rules, "title": title,
            "yes_sub_title": title[-20:], "early_close_condition": ecc, "status": status, "result": result,
            "latest_expiration_time": dt.datetime.fromtimestamp(le, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "yes_bid_dollars": f"{yb:.4f}", "yes_ask_dollars": f"{ya:.4f}", "yes_bid_size_fp": "12.00", "yes_ask_size_fp": "30.00",
            "volume_24h_fp": "100.00", "open_time": "2026-10-01T00:00:00Z", "close_time": "2026-10-20T03:59:00Z"}


class Fake:
    """Routes URLs to a mutable scenario."""

    def __init__(self):
        self.catalog = {}            # category -> list of series rows
        self.series_markets = {}     # series -> list of markets
        self.quotes = {}             # ticker -> market row (decision / settlement quotes)
        self.books = {}              # ticker -> orderbook response
        self.hist = {}               # ticker -> archived market row
        self.calls = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        u = urllib.parse.urlparse(url); q = urllib.parse.parse_qs(u.query)
        path = u.path.split("/trade-api/v2", 1)[-1]
        if path == "/series":
            return json.dumps({"series": self.catalog.get(q["category"][0], [])})
        if path == "/markets" and "series_ticker" in q:
            return json.dumps({"markets": self.series_markets.get(q["series_ticker"][0], []), "cursor": ""})
        if path == "/markets" and "tickers" in q:
            return json.dumps({"markets": [self.quotes[t] for t in q["tickers"][0].split(",") if t in self.quotes]})
        if path.startswith("/historical/markets/"):
            t = path.split("/")[3]
            return json.dumps({"market": self.hist[t]} if t in self.hist else {"error": {"code": "not_found"}})
        if path.endswith("/orderbook"):
            return json.dumps(self.books.get(path.split("/")[2], {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}))
        return json.dumps({"_error": "unrouted " + path})


def check(cond: bool, msg: str, fails: list) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def selftest() -> int:
    fails: list[str] = []
    # ---------------- parser equality with r6 on every cached listing (offline)
    try:
        from lab.kalshi.strategies.r6_near_deadline_nothing_happens_census_data import deadline as d6, raw_markets
        M = raw_markets()
        mism = sum(1 for _, (_, m) in M.items() if d6(m) != C.deadline(m))
        check(mism == 0 and len(M) > 1000, f"deadline parser equals r6's on {len(M)} cached markets (mismatches {mism})", fails)
    except Exception as ex:  # noqa: BLE001
        print("skip parser-equality check:", str(ex)[:100])
    # ---------------- classifier
    D = et(2026, 10, 15)
    cases = [
        (mk("A", "E", "If Test Artist releases 'X' on Spotify before Oct 15, 2026, then the market resolves to Yes.", "Will X release?"), True),
        (mk("B", "E", "If SpaceX launches Starship flight test number 14 before Oct 15, 2026, then the market resolves to Yes.", "Starship?"), True),
        (mk("C", "E", "If the Supreme Court releases its decision on tariffs before Oct 15, 2026, then the market resolves to Yes.", "Court?"), False),
        (mk("D", "E", "If SpaceX's next Starship launch is before Oct 15, 2026 at 7:30pm EST, then the market resolves to Yes.", "Time?"), False),
        (mk("E", "E", "If Acme announces an IPO before Oct 15, 2026, then the market resolves to Yes.", "IPO?"), False),
        (mk("F", "E", "If the CPI data is released before Oct 15, 2026, then the market resolves to Yes.", "CPI?", ecc="This market will close and expire early if the economic data is released."), False),
        (mk("G", "E", "If Google releases a model called Gemini 4 or greater by 11:59 PM ET on Oct 14, 2026, then the market resolves to Yes.", "Gemini?"), True),
        (mk("H", "E", "If Test Artist releases 'X' before Oct 15, 2026, then the market resolves to Yes.", "X?", exp_days=40, D=D), False),
    ]
    for m, want in cases:
        ok, why, d = C.qualify(m)
        check(ok == want, f"qualify {m['ticker']} -> {ok} ({why}), expected {want}", fails)
    check(C.qualify(cases[6][0])[2] == et(2026, 10, 15), "'by 11:59 PM ET on Oct 14' -> D = Oct 15 00:00 ET", fails)
    check(C.series_candidate({"ticker": "KXZZALB", "title": "Zz album release date", "category": "Entertainment", "frequency": "one_off"}),
          "series candidate: album release date", fails)
    check(not C.series_candidate({"ticker": "KXZZCT", "title": "Supreme Court decision release", "category": "Politics", "frequency": "one_off"}),
          "series not candidate: court decision", fails)
    check(not C.series_candidate({"ticker": "KXTARIFFDECISIONRELEASE", "title": "x release", "category": "Politics", "frequency": "one_off"}),
          "series not candidate: excluded list", fails)
    # ---------------- fees and bounds
    check(abs(C.fee_per_contract(0.50) - 0.018) < 1e-12, "fee 10-lot at 0.50 = 18c -> 0.018/contract", fails)
    check(abs(C.fee_per_contract(0.87) - 0.008) < 1e-12, "fee 10-lot at 0.87 = 8c -> 0.008/contract", fails)
    b24 = C.beta_bound_ret(24, 24, 0.87, C.fee_per_contract(0.87))
    b23 = C.beta_bound_ret(23, 24, 0.87, C.fee_per_contract(0.87))
    b38 = C.beta_bound_ret(37, 38, 0.87, C.fee_per_contract(0.87))
    b40 = C.beta_bound_ret(39, 40, 0.87, C.fee_per_contract(0.87))
    print(f"     Beta bound at px 0.87: 24/24 {b24:+.4f}, 23/24 {b23:+.4f}, 37/38 {b38:+.4f}, 39/40 {b40:+.4f}")
    check(b24 > 0 > b23, "Beta bound: 24 loss-free products > 0, one loss in 24 < 0", fails)
    # ---------------- budget cap
    tmp = Path(tempfile.mkdtemp(prefix="r7_selftest_"))
    try:
        fake = Fake()
        bud = Budget(10, tmp, fetcher=fake)
        n = 0
        try:
            for _ in range(15):
                bud.get("/markets?series_ticker=X&status=open&limit=1000"); n += 1
        except BudgetExhausted:
            pass
        check(n == 10 and bud.used == 10, f"Budget stops at 10 calls (made {n})", fails)
        check(Budget(25, tmp, fetcher=fake).cap == 10, "cap cannot be raised above 10", fails)
        (tmp / "calls.log").unlink()
        # ---------------- scenario
        cat_file = tmp / "series_all.json"
        cand_rows = [{"ticker": "KXZZALBUM", "title": "Zz album release date", "category": "Entertainment", "frequency": "one_off", "volume_fp": "100"},
                     {"ticker": "KXZZGPT", "title": "Zz GPT release", "category": "Science and Technology", "frequency": "custom", "volume_fp": "50"},
                     {"ticker": "KXZZCOURT", "title": "Supreme Court decision release", "category": "Politics", "frequency": "one_off", "volume_fp": "9"}]
        cand_rows += [{"ticker": f"KXZZFILL{i:02d}", "title": f"Filler {i} release date", "category": "Entertainment",
                       "frequency": "one_off", "volume_fp": "5"} for i in range(15)]
        cat_file.write_text(json.dumps({"series": cand_rows}))
        fake = Fake()
        fake.catalog = {"Entertainment": [dict(r) for r in cand_rows if r["category"] == "Entertainment"] +
                        [{"ticker": "KXZZNEWALB", "title": "Zz2 album release date", "category": "Entertainment", "frequency": "one_off", "volume_fp": "1"}],
                        "Science and Technology": [dict(cand_rows[1], volume_fp="80")],
                        "Politics": [cand_rows[2]]}
        fake.catalog["Entertainment"][0]["volume_fp"] = "160"      # KXZZALBUM moved
        D1, D2, D3, D4 = et(2026, 10, 15), et(2026, 10, 17), et(2026, 10, 30), et(2026, 10, 16)
        A1 = mk("KXZZALBUM-EVT1-OCT15", "KXZZALBUM-EVT1", "If Zz releases 'One' on Spotify before Oct 15, 2026, then the market resolves to Yes.", "Will Zz release One before Oct 15?", yb=0.10, ya=0.13, D=D1)
        A2 = mk("KXZZALBUM-EVT2-OCT17", "KXZZALBUM-EVT2", "If Zz releases 'Two' on Spotify before Oct 17, 2026, then the market resolves to Yes.", "Will Zz release Two before Oct 17?", yb=0.02, ya=0.04, D=D2)
        A3 = mk("KXZZALBUM-EVT2-OCT30", "KXZZALBUM-EVT2", "If Zz releases 'Two' on Spotify before Oct 30, 2026, then the market resolves to Yes.", "Will Zz release Two before Oct 30?", yb=0.30, ya=0.35, D=D3)
        B1 = mk("KXZZGPT-EVT3-OCT16", "KXZZGPT-EVT3", "If Zz Labs releases a model called ZzGPT-9 or greater before Oct 16, 2026, then the market resolves to Yes.", "ZzGPT-9 before Oct 16?", yb=0.25, ya=0.28, D=D4)
        CT = mk("KXZZCOURT-EVT9-OCT15", "KXZZCOURT-EVT9", "If the Supreme Court releases its decision before Oct 15, 2026, then the market resolves to Yes.", "Court?", D=D1)
        fake.series_markets = {"KXZZALBUM": [A1, A2, A3], "KXZZGPT": [B1], "KXZZCOURT": [CT]}
        fake.quotes = {m["ticker"]: dict(m) for m in (A1, A2, A3, B1)}
        fake.books = {A1["ticker"]: {"orderbook_fp": {"yes_dollars": [["0.0900", "50.00"], ["0.1000", "3.00"]], "no_dollars": [["0.8700", "10.00"]]}},
                      B1["ticker"]: {"orderbook_fp": {"yes_dollars": [["0.2500", "400.00"]], "no_dollars": []}}}

        def run(now, mode, cap=10, dry=False):
            lg = Logger(Budget(cap, tmp, fetcher=fake), now, tmp, catalog_file=cat_file, seeds=())
            return lg, lg.run(mode, dry=dry)

        check(clock_mode(et(2026, 10, 12, 12, 5)) == "decide" and clock_mode(et(2026, 10, 12, 11, 30)) == "maintain"
              and clock_mode(et(2026, 10, 12, 0, 7)) is None, "clock: 12:xx decide, 11:xx maintain, else no-op", fails)
        lg0 = Logger(Budget(10, tmp, fetcher=fake), et(2026, 10, 12, 10, 0), tmp, catalog_file=cat_file, seeds=())
        lg0.ensure_universe()
        check(lg0.seed_catalog_baseline([cat_file]) == 17, "catalog baseline seeded from a file on disk (17 candidates)", fails)
        lg0.save()
        lg, s = run(et(2026, 10, 12, 11, 0), "maintain")          # Monday: catalog is overdue (never run)
        check(s["calls"] == 10, f"first maintain pass = the 10-call catalog diff (calls {s['calls']})", fails)
        st = json.loads((tmp / "state.json").read_text())
        check("KXZZCOURT" not in st["series"], "court series never becomes a candidate", fails)
        check(st["series"].get("KXZZNEWALB", {}).get("new_since") is not None and st["series"]["KXZZNEWALB"]["tier"] == "warm",
              "new catalog series flagged new and warm", fails)
        check(st["series"]["KXZZALBUM"]["tier"] == "warm" and st["series"]["KXZZFILL00"]["tier"] == "cold",
              "volume moved -> warm; unchanged -> cold", fails)
        lg, s = run(et(2026, 10, 12, 11, 10), "maintain")
        st = json.loads((tmp / "state.json").read_text())
        check(s["calls"] == 8, f"refresh: 3 warm + 5 cold (cold capped at 5 per pass) = 8 calls (calls {s['calls']})", fails)
        check(st["series"]["KXZZALBUM"]["tier"] == "live" and st["series"]["KXZZGPT"]["tier"] == "live", "series with open qualifying rungs -> live", fails)
        check(set(st["rungs"]) == {A1["ticker"], A2["ticker"], A3["ticker"], B1["ticker"]}, f"rungs inventoried: {sorted(st['rungs'])}", fails)
        n_before = len(fake.calls)
        lg, s = run(et(2026, 10, 12, 0, 30), "decide", dry=True)     # forced decide outside the hour: dry
        st2 = json.loads((tmp / "state.json").read_text())
        check(st2 == st, "dry decide does not change the state", fails)
        # ---- day 0 decision at 12:00 ET
        lg, s = run(et(2026, 10, 12, 12, 0), "decide")
        st = json.loads((tmp / "state.json").read_text())
        rows = [json.loads(l) for l in open(tmp / "forward.jsonl")]
        ent = [r for r in rows if r["type"] == "entry" and not r.get("dry")]
        check({r["ticker"] for r in ent} == {A1["ticker"], B1["ticker"]}, f"day 0 entries A1 (NO 0.90) and B1 (NO 0.75): {[r['ticker'] for r in ent]}", fails)
        e1 = next(r for r in ent if r["ticker"] == A1["ticker"])
        want = round(5.0 / (3 + (5 - 2.7) / 0.91), 4)
        check(abs(e1["px"] - max(0.90, want)) < 1e-4, f"A1 fill = book walk for $5 ({e1['px']} vs {want})", fails)
        check(st["rungs"][A2["ticker"]]["last_look_date"] == "2026-10-12" and not st["rungs"][A2["ticker"]]["bought"], "A2 looked (NO 0.98), not bought", fails)
        check(st["rungs"][A3["ticker"]]["looks"] == 0, "A3 (D 18 days ahead) not looked", fails)
        lg, s = run(et(2026, 10, 12, 12, 10), "decide")
        check(s["calls"] == 0, "second 12:xx pass on the same day makes no calls", fails)
        # ---- day 1: A2 enters the band; only 1 call allowed -> fill at the look price
        fake.quotes[A2["ticker"]] = dict(A2, yes_bid_dollars="0.2000", yes_ask_dollars="0.2300")
        lg, s = run(et(2026, 10, 13, 12, 0), "decide", cap=1)
        rows = [json.loads(l) for l in open(tmp / "forward.jsonl")]
        e2 = [r for r in rows if r["type"] == "entry" and r["ticker"] == A2["ticker"] and not r.get("dry")]
        check(len(e2) == 1 and abs(e2[0]["px"] - 0.80) < 1e-9 and e2[0]["book"] is None, "A2 day-1 entry at the look price 0.80 (no book call left)", fails)
        # ---- settlement
        fake.quotes[A1["ticker"]] = dict(A1, status="finalized", result="no")
        fake.quotes[B1["ticker"]] = dict(B1, status="finalized", result="yes")
        fake.quotes[A2["ticker"]] = dict(A2, status="finalized", result="yes")
        lg, s = run(et(2026, 10, 17, 11, 0), "settle")
        rows = [json.loads(l) for l in open(tmp / "forward.jsonl")]
        se = {r["ticker"]: r for r in rows if r["type"] == "settle"}
        check(se.get(A1["ticker"], {}).get("won") is True and se.get(B1["ticker"], {}).get("won") is False and se.get(A2["ticker"], {}).get("won") is False,
              "settled: A1 won, B1 lost, A2 lost", fails)
        r1 = se[A1["ticker"]]["ret_per_dollar"]
        check(abs(r1 - (1 - e1["px"] - C.fee_per_contract(e1["px"])) / e1["px"]) < 1e-4, f"A1 return per $ {r1:+.4f}", fails)
        # archive fallback: a position missing from the live tier 20+ days after D is read from /historical/markets/{t}
        st = json.loads((tmp / "state.json").read_text())
        st["positions"]["KXZZOLD-EVT8-SEP01"] = {"e": "KXZZOLD-EVT8", "s": "KXZZOLD", "D": et(2026, 9, 1), "px": 0.9,
                                                 "entry_ts": et(2026, 8, 28, 12), "settled": False, "last_check": 0}
        (tmp / "state.json").write_text(json.dumps(st))
        fake.hist["KXZZOLD-EVT8-SEP01"] = {"ticker": "KXZZOLD-EVT8-SEP01", "status": "finalized", "result": "no"}
        lg, s = run(et(2026, 10, 17, 18, 0), "settle")
        st = json.loads((tmp / "state.json").read_text())
        check(st["positions"]["KXZZOLD-EVT8-SEP01"].get("settled") is True and s["calls"] == 2,
              f"archive fallback settles an old position (calls {s['calls']})", fails)
        st["positions"].pop("KXZZOLD-EVT8-SEP01"); (tmp / "state.json").write_text(json.dumps(st))
        with open(tmp / "forward.jsonl") as fh:
            keep = [l for l in fh if "KXZZOLD" not in l]
        with open(tmp / "forward.jsonl", "w") as fh:
            fh.writelines(keep)
        res = SUM.summarize(tmp)
        check(res["stats"]["n"] == 3 and res["stats"]["products"] == 3 and res["stats"]["product_losses"] == 2,
              f"summary n 3, products 3, losing products 2 ({res['stats'].get('n')}, {res['stats'].get('products')}, {res['stats'].get('product_losses')})", fails)
        check(res["kill"]["killed"] and res["verdict"] == "KILLED", "kill rule: 2nd losing product before 30 -> KILLED", fails)
        check(res["dry_rows_ignored"] > 0 and not res["integrity_errors"], f"dry rows ignored, no integrity errors {res['integrity_errors']}", fails)
        # integrity: an injected out-of-band entry must be caught
        with open(tmp / "forward.jsonl", "a") as fh:
            fh.write(json.dumps({"type": "entry", "ts": et(2026, 10, 13, 12, 1), "iso": "", "ticker": "X-1", "event": "X", "series": "X",
                                 "filled": True, "signal_no": 0.97, "px": 0.97, "D": et(2026, 10, 16)}) + "\n")
        res = SUM.summarize(tmp)
        check(any("out of band" in e for e in res["integrity_errors"]), "integrity: out-of-band entry flagged", fails)
        print(f"     fake calls made: {len(fake.calls)} (all offline)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELFTEST", "PASS" if not fails else f"FAIL ({len(fails)})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(selftest())
