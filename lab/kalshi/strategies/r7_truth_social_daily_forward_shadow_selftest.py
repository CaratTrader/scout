"""Offline selftest of the r7_truth_social_daily_forward_shadow logger and summary (no network: fake Kalshi and Roll Call).

Checks: fee rounding; bracket parsing on a cached live KXTRUTHSOCIALD list; ladder probabilities sum to 1 and equal the
weekly code for integer counts (and document the weekly code's gap for fractional counts); the wall-clock profile mass
equals the weekly code's minute indexing outside DST weeks; on a Saturday the daily model with the weekly level window
reproduces the frozen weekly model's remaining-count mean exactly; order-book depth on the cached live book; a full
pass (signals, book reads, fills, take-once, slot de-duplication, call cap); a Roll Call failure gives no signals; the
Saturday arm; settlement; the summary and gate.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_truth_social_daily_forward_shadow_selftest"""
from __future__ import annotations
import datetime as dt, json, math, random, shutil, sys, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies import r7_truth_social_daily_forward_shadow_logger as L
from lab.kalshi.strategies import r7_truth_social_daily_forward_shadow_summary as SUM
from lab.kalshi.strategies.r6_truth_social_count_endgame import Model

ET = ZoneInfo("America/New_York")
CACHE = ROOT / "data/kalshi_lab/strategies/lead_round6"
OK = []


def check(name: str, cond: bool, info="") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info != "" else ""))
    OK.append(cond)


def ts(y, m, d, h=0, mi=0) -> int:
    return int(dt.datetime(y, m, d, h, mi, tzinfo=ET).timestamp())


class FakeNet:
    def __init__(self, posts, km, now, cap=L.MAX_CALLS):
        self.posts = sorted(posts, key=lambda p: -p["ts"]); self.km = km; self.now = now; self.cap = cap
        self.calls = 0; self.log = []; self.fail_rc = False

    def kalshi(self, path):
        if self.calls >= self.cap:
            raise L.CallCap(path)
        self.calls += 1; self.log.append(path)
        for k, v in self.km.items():
            if path.startswith(k):
                return v(path) if callable(v) else v
        return None

    def rollcall_page(self, p):
        if self.fail_rc:
            return ""
        vis = [x for x in self.posts if x["ts"] <= self.now]
        chunk = vis[(p - 1) * 50:p * 50]
        return json.dumps({"data": [{"id": x["id"], "date": dt.datetime.fromtimestamp(x["ts"], ET).isoformat(),
                                     "deleted_flag": x["deleted"] and (x["flag"] or 0) <= self.now,
                                     "social": {"deleted_date": dt.datetime.fromtimestamp(x["flag"], ET).isoformat()
                                                if x["deleted"] and x["flag"] and x["flag"] <= self.now else None}} for x in chunk]})

    def sleep(self, s):
        pass

    def clock(self):
        return self.now


def synth_posts(t0: int, t1: int, per_day: float, seed: int) -> list[dict]:
    rnd = random.Random(seed); out = []; t = t0
    while True:
        t += int(rnd.expovariate(per_day / 86400))
        if t >= t1:
            break
        dele = rnd.random() < 0.02
        out.append({"id": f"{seed}-{t}", "ts": t, "deleted": dele, "flag": t + 6 * 3600 if dele else None})
    return out


def daily_markets(day: dt.date, quotes: dict) -> list[dict]:
    lbl = day.strftime("%b %-d, %Y"); close = dt.datetime(day.year, day.month, day.day, 23, 59, tzinfo=ET).astimezone(dt.timezone.utc)
    rows = []
    spec = [("T5", "less", None, 5, "<5"), ("B7", "between", 5, 9, "5-9"), ("B12", "between", 10, 14, "10-14"), ("B17", "between", 15, 19, "15-19"),
            ("B22", "between", 20, 24, "20-24"), ("B27", "between", 25, 29, "25-29"), ("B34", "between", 30, 39, "30-39"), ("T40", "greater_or_equal", 40, None, "40+")]
    ev = f"KXTRUTHSOCIALD-{day.strftime('%y%b%d').upper()}"
    for suf, st_, lo, hi, sub in spec:
        tk = f"{ev}-{suf}"; bid, ask = quotes.get(tk, (0.0, 1.0))
        rows.append({"ticker": tk, "event_ticker": ev, "strike_type": st_, "floor_strike": lo, "cap_strike": hi, "yes_sub_title": sub,
                     "close_time": close.strftime("%Y-%m-%dT%H:%M:%SZ"), "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
                     "yes_bid_size_fp": "50.00" if bid > 0 else "0.00", "yes_ask_size_fp": "50.00" if ask < 1 else "0.00", "volume_fp": "100.00",
                     "rules_secondary": f"The observation period begins at 12:00 AM ET on {lbl} and ends at 11:59 PM ET on {lbl}."})
    return rows


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="r7selftest_"))
    L.OUT = tmp; SUM.OUT = tmp
    M = L.load_model()
    # 1 fees
    check("fee 10 @ 0.50 = $0.18", abs(L.fee_order(0.5, 10) - 0.18) < 1e-9, L.fee_order(0.5, 10))
    check("fee 5 @ 0.43 = $0.09", abs(L.fee_order(0.43, 5) - 0.09) < 1e-9, L.fee_order(0.43, 5))
    # 2 brackets on the cached live list (2026-10-08)
    live = json.loads((CACHE / "api_cache/4347e3853f4f858fa87a3eba.json").read_text())["markets"]
    brs = sorted((L.bracket(m) for m in live), key=lambda b: b["lo"])
    check("8 brackets, complete ladder", len(brs) == 8 and L.ladder_ok(brs), [(b["lo"], b["hi"] if b["hi"] < L.BIG else "inf") for b in brs])
    check("40+ is greater_or_equal -> lo 40; <5 -> hi 4", brs[-1]["lo"] == 40 and brs[0]["hi"] == 4)
    w = L.market_window(live[0])
    check("window from rules = Oct 8 00:00 ET .. Oct 9 00:00 ET", w == (ts(2026, 10, 8), ts(2026, 10, 9)), w)
    # 3 ladder probabilities
    worst = 0.0
    for seen, recent in ((0, 0), (4, 4), (12, 9), (15, 27), (24, 3), (41, 60)):
        for mu in (0.3, 2, 7.5, 16, 40):
            s = sum(L.ladder_probs(seen, recent, M.delta, mu, M.phi, brs).values()); worst = max(worst, abs(s - 1))
    check("ladder probabilities sum to 1 (with and without pending deletions)", worst < 1e-6, f"max |sum-1| {worst:.2e}")
    pr = L.ladder_probs(15, 27, M.delta, 0.5, M.phi, brs)
    b15 = next(b["t"] for b in brs if b["lo"] == 15); b10 = next(b["t"] for b in brs if b["lo"] == 10)
    check("count just inside 15-19 at the close stays mostly there (no rounding artefact)", pr[b15] > 0.55 and pr[b10] < 0.45,
          f"P(15-19) {pr[b15]:.3f}, P(10-14) {pr[b10]:.3f}")
    x = {"A": ts(2026, 10, 8), "B": ts(2026, 10, 9), "brackets": brs}

    class FP:   # count fixed at 12 visible, no deletions
        all = [ts(2026, 10, 8, 5) + 60 * i for i in range(12)]; dele = []; lag = 0   # after the 03:00 ET flag run

        def seen(self, A, t):
            return sum(1 for a in self.all if A <= a <= t)
    M0 = Model.from_frozen({**json.loads(L.FROZEN_MODEL.read_text()), "delta": 0.0})
    t = ts(2026, 10, 8, 20)
    wk = M0.bracket_probs(x, FP(), t); c, seen, mu = M0.dist(x, FP(), t)
    mine = L.ladder_probs(int(c), 0, M.delta, mu, M0.phi, brs)
    check("no pending deletions: ladder_probs == weekly bracket_probs", max(abs(wk[k] - mine[k]) for k in wk) < 1e-12, f"c={c} mu={mu:.2f}")
    wk_frac = M.bracket_probs(x, FP(), t)
    check("documented: weekly code with the deletion haircut leaves mass out (sum < 1)", sum(wk_frac.values()) < 0.999,
          f"weekly sum {sum(wk_frac.values()):.4f}")
    # 4 profile mass
    A = ts(2026, 10, 11)
    pm = L.profile_mass(M, A, A + 7 * 86400)
    check("weekly profile mass = discovery weekly mean", abs(pm - M.weekly_mean) < 0.01, f"{pm:.3f} vs {M.weekly_mean:.3f}")
    diffs = [abs(L.profile_mass(M, a, b) - M.mass(A, a, b)) for a, b in ((A + 3600 * 5, A + 3600 * 29), (A + 86400 * 5 + 600, A + 86400 * 7))]
    check("wall-clock mass == weekly minute indexing outside DST weeks", max(diffs) < 1e-6, max(diffs))
    # 5 Saturday consistency with the frozen weekly model (level window = week start; daily end = weekly end)
    sat_t = ts(2026, 10, 17, 15, 0)
    posts = synth_posts(ts(2026, 10, 3), ts(2026, 10, 18), 21, seed=1)
    rows = [{"ts": p["ts"], "deleted": p["deleted"] and p["flag"] <= sat_t, "flag_ts": p["flag"] if p["deleted"] and p["flag"] <= sat_t else None}
            for p in posts if p["ts"] <= sat_t]
    P = L.Posts(rows, sat_t)
    Aw, Bw = L.week_bounds(sat_t)
    xw = {"A": Aw, "B": Bw, "brackets": []}
    _, _, mu_w = M.dist(xw, P, sat_t)
    mu_d = L.level(M, P, sat_t, Aw) * L.profile_mass(M, sat_t, ts(2026, 10, 18))
    check("Saturday: daily mu (weekly level window) == frozen weekly model mu", abs(mu_w - mu_d) < 1e-9, f"{mu_w:.4f} vs {mu_d:.4f}")
    # 6 order book depth on the cached live book (B12 2026-10-08 23:53 ET)
    book = json.loads((CACHE / "lead_checks.json").read_text())["truth_social_daily"]["live"]["orderbook_sample"]["book"]
    d_no = L.book_depth(book, "NO", 0.43); d_yes = L.book_depth(book, "YES", 0.97); d_y99 = L.book_depth(book, "YES", 0.99)
    check("book: NO @0.43 -> 16 contracts (YES bid 0.57 x16)", d_no["disp_at_px"] == 16 and d_no["best_exec"] == 0.43, d_no["disp_at_px"])
    check("book: YES @0.97 -> 0 (NO bids only at 0.01)", d_yes["disp_at_px"] == 0 and d_y99["disp_at_px"] == 285.45)
    # 7 full pass (Friday 2026-10-16 20:00 ET), with crafted quotes
    day = dt.date(2026, 10, 16); now = ts(2026, 10, 16, 20, 0)
    feed = synth_posts(ts(2026, 10, 1), ts(2026, 10, 17), 21, seed=2)
    rows = [{"ts": p["ts"], "deleted": p["deleted"] and p["flag"] <= now, "flag_ts": p["flag"] if p["deleted"] and p["flag"] <= now else None}
            for p in feed if p["ts"] <= now]
    P = L.Posts(rows, now)
    mk0 = daily_markets(day, {})
    brs = sorted((L.bracket(m) for m in mk0), key=lambda b: b["lo"])
    mod = L.daily_model(M, P, now, ts(2026, 10, 16), ts(2026, 10, 17), brs)
    p1 = mod["p"]["d1"]
    hi_b = max(brs, key=lambda b: p1[b["t"]]); lo_b = min((b for b in brs if b is not hi_b), key=lambda b: abs(p1[b["t"]] - 0.15))
    yes_ask = round(max(0.10, p1[hi_b["t"]] - 0.15), 2)
    no_px = round(min(0.90, (1 - p1[lo_b["t"]]) - 0.12), 2)
    q = {hi_b["t"]: (0.02, yes_ask), lo_b["t"]: (round(1 - no_px, 2), 0.99)}
    km = {"/markets?series_ticker=KXTRUTHSOCIALD": {"markets": daily_markets(day, q)},
          f"/markets/{hi_b['t']}/orderbook": {"orderbook_fp": {"no_dollars": [[f"{1 - yes_ask:.4f}", "3.00"]], "yes_dollars": [["0.0200", "50.00"]]}},
          f"/markets/{lo_b['t']}/orderbook": {"orderbook_fp": {"yes_dollars": [[f"{1 - no_px:.4f}", "0.50"]], "no_dollars": [["0.0100", "50.00"]]}}}
    net = FakeNet(feed, km, now)
    rec = L.one_pass(now=now, net=net, M=M)
    fw = L._read(tmp / "forward.jsonl")
    sg = [r for r in fw if r["row"] == "signal"]
    check("pass ran in slot 40 with 1 list + 2 book calls", rec and rec["slot"] == 40 and net.calls == 3, (rec or {}).get("slot"))
    check("model count equals the visible count", rec["seen_today"] == P.seen(ts(2026, 10, 16), now), rec["seen_today"])
    yes_sig = [s for s in sg if s["t"] == hi_b["t"] and s["side"] == "YES"]
    no_sig = [s for s in sg if s["t"] == lo_b["t"] and s["side"] == "NO"]
    check("YES signal filled 3 contracts at the signal price", yes_sig and yes_sig[0]["filled"] and yes_sig[0]["n_fill"] == 3 and yes_sig[0]["px"] == yes_ask,
          yes_sig and {k: yes_sig[0][k] for k in ("px", "model", "n_fill")})
    check("NO signal with 0.5 displayed contracts is unfilled", no_sig and not no_sig[0]["filled"], no_sig and no_sig[0]["book"])
    check("no other signals", len(sg) == 2, [(s["t"][-4:], s["side"]) for s in sg])
    check("same slot again is a no-op", L.one_pass(now=now + 120, net=FakeNet(feed, km, now + 120), M=M) is None)
    net2 = FakeNet(feed, km, now + 900)
    rec2 = L.one_pass(now=now + 900, net=net2, M=M)
    new = [r for r in L._read(tmp / "forward.jsonl") if r["row"] == "signal" and r["ts"] == rec2["ts"]]
    check("next slot: filled side not re-taken, unfilled side re-tried", [(s["t"], s["side"]) for s in new] == [(lo_b["t"], "NO")] and net2.calls == 2,
          [(s["t"][-4:], s["side"]) for s in new])
    # 8 call cap with many signals (crossed synthetic books)
    qq = {}
    for b in brs:
        p = p1[b["t"]]; qq[b["t"]] = (round(min(0.90, p + 0.13), 2), round(max(0.10, p - 0.13), 2) if p >= 0.23 else 0.99)
    km3 = {"/markets?series_ticker=KXTRUTHSOCIALD": {"markets": daily_markets(day, qq)}, "/markets/": {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}}
    keep = L.MAX_BOOKS
    L.MAX_BOOKS = 3
    net3 = FakeNet(feed, km3, now + 1800)
    rec3 = L.one_pass(now=now + 1800, net=net3, M=M)
    check("many signals: book reads stop at MAX_BOOKS (set to 3 here)", rec3["n_new_signals"] > 3 and rec3["books"] == 3 and net3.calls == 4,
          f"signals {rec3['n_new_signals']}, books {rec3['books']}, calls {net3.calls}")
    L.MAX_BOOKS = 20
    net3b = FakeNet(feed, km3, now + 2000)
    rec3b = L.one_pass(now=now + 2000, net=net3b, M=M, force=True)
    check("many signals: the 10-call cap holds even if MAX_BOOKS were larger", net3b.calls <= 10 and rec3b["books"] == min(rec3b["n_new_signals"], 9),
          f"signals {rec3b['n_new_signals']}, books {rec3b['books']}, calls {net3b.calls}")
    L.MAX_BOOKS = keep
    # 9 Roll Call failure -> no signals
    net4 = FakeNet(feed, km, now + 2700); net4.fail_rc = True
    rec4 = L.one_pass(now=now + 2700, net=net4, M=M)
    check("Roll Call failure: pass logged, not eligible, no signals", rec4 and not rec4["eligible"] and rec4["n_signals"] == 0)
    # 10 outside the window: no pass, no calls
    net5 = FakeNet(feed, km, ts(2026, 10, 16, 9, 30))
    check("09:30 ET: no pass and no calls", L.one_pass(now=ts(2026, 10, 16, 9, 30), net=net5, M=M) is None and net5.calls == 0)
    # 11 Saturday arm (2026-10-17 15:00 ET)
    sday = dt.date(2026, 10, 17); snow = ts(2026, 10, 17, 15, 0)
    rows = [{"ts": p["ts"], "deleted": p["deleted"] and p["flag"] <= snow, "flag_ts": p["flag"] if p["deleted"] and p["flag"] <= snow else None}
            for p in feed if p["ts"] <= snow]
    Ps = L.Posts(rows, snow); F = Ps.kept_between(ts(2026, 10, 11), ts(2026, 10, 17))
    wspec = [("T80", "less", None, 80)] + [(f"B{lo + 9}", "between", lo, lo + 19) for lo in range(80, 220, 20)] + [("B230", "between", 220, 240), ("T240", "greater", 240, None)]
    W = F + 22
    wmk = []
    for suf, st_, lo, hi in wspec:
        lo_, hi_ = (0, hi - 1) if st_ == "less" else ((lo + 1, 10 ** 6) if st_ == "greater" else (lo, hi))
        mid = 0.6 if lo_ <= W <= hi_ else 0.05
        wmk.append({"ticker": f"KXTRUTHSOCIAL-26OCT17-{suf}", "event_ticker": "KXTRUTHSOCIAL-26OCT17", "strike_type": st_, "floor_strike": lo,
                    "cap_strike": hi, "yes_bid_dollars": f"{mid - 0.01:.4f}", "yes_ask_dollars": f"{mid + 0.01:.4f}",
                    "close_time": "2026-10-18T13:59:00Z",
                    "rules_secondary": "The observation period begins at 12:00 AM ET on Oct 11, 2026 and ends at 11:59 PM ET on Oct 17, 2026."})
    km6 = {"/markets?series_ticker=KXTRUTHSOCIALD": {"markets": daily_markets(sday, {})}, "/markets?series_ticker=KXTRUTHSOCIAL&": {"markets": wmk}}
    net6 = FakeNet(feed, km6, snow)
    L.one_pass(now=snow, net=net6, M=M)
    sa = [r for r in L._read(tmp / "forward.jsonl") if r["row"] == "sat_arm"]
    ok6 = bool(sa) and not sa[-1].get("skip")
    s_imp = sum(sa[-1]["implied_daily"].values()) if ok6 else None
    check("Saturday arm: weekly ladder parsed, F computed, implied daily sums to <= 1", ok6 and 0.5 < s_imp <= 1.0 + 1e-6 and sa[-1]["F_kept_sun_fri"] == F,
          sa[-1] if not ok6 else f"F={F} implied sum={s_imp:.3f} edges={len(sa[-1]['edges'])}")
    check("Saturday arm uses one extra call", net6.calls == 2, net6.calls)
    # 12 settlement of the Friday event (Saturday 10:00 ET)
    fin = sum(1 for p in feed if ts(2026, 10, 16) <= p["ts"] < ts(2026, 10, 17) and not p["deleted"])
    res_mk = []
    for m in daily_markets(day, {}):
        b = L.bracket(m); res_mk.append({**m, "result": "yes" if b["lo"] <= fin <= b["hi"] else "no", "expiration_value": str(fin)})
    netS = FakeNet(feed, {"/markets?event_ticker=KXTRUTHSOCIALD-26OCT16": {"markets": res_mk}}, ts(2026, 10, 17, 10))
    st_rows = L.settle(now=ts(2026, 10, 17, 10), net=netS)
    evr = [r for r in st_rows if r["row"] == "event"]
    ys = [r for r in st_rows if r["row"] == "settled" and r["t"] == hi_b["t"] and r["side"] == "YES" and r["filled"]]
    won = hi_b["lo"] <= fin <= hi_b["hi"]
    exp_ret = ((1.0 if won else 0.0) - yes_ask - L.fee_order(yes_ask, 3) / 3) / yes_ask
    check("settle: event row matches Roll Call, filled YES P&L correct", evr and evr[0]["rc_bracket_match"] is True and ys and ys[0]["won"] == won
          and abs(ys[0]["ret_per_dollar"] - exp_ret) < 1e-4, f"final {fin}, won {won}, ret {ys and ys[0]['ret_per_dollar']}")
    check("settle again within 50 min: no calls", L.settle(now=ts(2026, 10, 17, 10, 20), net=FakeNet(feed, {}, 0)) == [])
    # 13 summary
    out = SUM.main([])
    check("summary: 1 filled trade, gate not passed, kill rule too early", out["primary_filled_trades"]["n"] == 1 and not out["gate"]["PASS"]
          and out["kill_rule"]["verdict"] == "too early", out["primary_filled_trades"])
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nselftest: {sum(OK)}/{len(OK)} passed")
    return 0 if all(OK) else 1


if __name__ == "__main__":
    sys.exit(main())
