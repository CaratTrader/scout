"""r5_kalshi_cross_series_follower: do thin Kalshi series that are logically tied to a liquid Kalshi market lag it
after a large move, by enough to buy the follower as a taker 1-2 h later?

Hypothesis. A liquid "lead" market (the next FOMC meeting's KXFEDDECISION outcomes; the "government shut down on
date X" market KXGOVSHUT) implies a deterministic bound on a thinner "follower" series (KXFED rate-after-meeting
strikes, the yearly cut count KXRATECUTCOUNT, the "cut by date" RATECUT markets, shutdown-in-year / shutdown-length
markets). After a lead move of >= 15c the follower may stay on the wrong side of the bound for an hour or more.

Pair map (frozen from listing metadata only, before any price was loaded; `freeze` writes frozen_pairs.json):
  A  FEDDECISION-M (next meeting, prior upper bound R known) -> FED-M-T{R-.50,R-.25,R}
       T(R-.25): YES >= L{H0,H25,H26}, NO >= L{C25,C26};  T(R-.50): YES >= L{H0,H25,H26,C25}, NO >= L{C26};
       T(R): YES >= L{H25,H26}, NO >= L{H0,C25,C26}      (L{S} = sum of the YES bids of the outcome markets S)
  B  FEDDECISION-M (next meeting in year Y, c cuts of 25bp so far in Y) -> RATECUTCOUNT-Y "exactly k":
       Exactly(c): NO >= L{C25,C26};  Exactly(c+1): NO >= L{C26}
  C  FEDDECISION-M -> RATECUT-D ("cut at least once between S and D", S <= M <= D): YES >= L{C25,C26}
  D  GOVSHUT-x ("shut down at 10 AM on day x") -> shutdown-in-year (SHUTDOWNBY) YES >= L; shutdown-length "> 0 days"
       YES >= L (and NO >= L_no for the Oct-2025 equivalence); "> N days" of the shutdown starting that day NO >= L_no.
  After the lead settles, its settlement value is the lead quote for 48 h (B, C, D followers outlive the lead).

Rule (test plan; parameters chosen on discovery only): lead move = |mid change| >= MOVE within <= 3 hourly steps
(valid two-sided quotes, spread <= 10c; lead settlement counts as a quote), one episode per lead event per 12 h;
decision at move hour + DEC_H; violation v = lead-side bid sum - follower-side ask - fee >= THR; taker fill on the
follower at the quote FILL_H hours after the decision; hold to settlement. Split 70/30 by move time.

Usage (in order): .venv/bin/python -m lab.kalshi.strategies.r5_kalshi_cross_series_follower
  freeze | fetch_leads | moves | fetch_followers | discover | uncond | fetch_lag | lag | freeze_candidates | validate
Result (2026-10-08): dead. 0 of 230 episode-pair combos (lead move >= 10c) violate a bound by >= 8c one hour after the
move; minute-level median follower lag 4 min (n=10); the rare violations are stale/wrong leads, and trading them loses."""
from __future__ import annotations
import datetime as dt, hashlib, json, math, re, statistics as st, sys, time
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies import r5_kalshi_cross_series_follower_data as D
from lab.kalshi.strategies.r5_kalshi_cross_series_follower_api import used, OUT

FROZEN = OUT / "frozen_pairs.json"
H = 3600
POST_H = 48          # hours a settled lead keeps acting as a quote for followers that outlive it
D_LOOKBACK_D = 30    # shutdown pairs: only the last 30 days before the lead's date (data budget; fixed before any price)


def outcome_code(sub: str, ticker: str = "") -> str | None:
    suf = ticker.rsplit("-", 1)[-1]
    if suf in ("H0", "C25", "H25", "C26", "H26"):
        return suf
    if suf in ("C>25", "H>25"):
        return {"C>25": "C26", "H>25": "H26"}[suf]
    s = sub.lower()
    if "maintain" in s or "hike 0" in s or "no change" in s or "no cut" in s:
        return "H0"
    if "cut >25" in s:
        return "C26"
    if "cut 25" in s:
        return "C25"
    if "hike >25" in s:
        return "H26"
    if "hike 25" in s:
        return "H25"
    return None


def freeze() -> None:
    M = D.meta()
    # ---------------- FOMC meetings (lead events) ----------------
    fd = defaultdict(dict)
    for t, m in M.items():
        if re.match(r"^(KX)?FEDDECISION-", t) and m["e"] != "FEDDECISION-24JAN":   # 24JAN: malformed duplicate closing 5 days early
            c = outcome_code(m["sub"], t)
            if c:
                fd[m["e"]][c] = t
    meetings = sorted(fd, key=lambda e: M[next(iter(fd[e].values()))]["close"])
    meetings = [e for e in meetings if M[next(iter(fd[e].values()))]["close"] >= D.ts("2023-09-01T00:00:00Z")]
    close_of = {e: M[next(iter(fd[e].values()))]["close"] for e in meetings}
    # KXFED event per meeting (same close date), and the upper bound after each meeting from the FED strike results
    fed_ev = defaultdict(list)
    for t, m in M.items():
        if re.match(r"^(KX)?FED-", t):
            fed_ev[m["e"]].append(m)
    fed_by_date = {}
    for e, ms in fed_ev.items():
        fed_by_date[dt.datetime.utcfromtimestamp(ms[0]["close"]).date()] = e
    upper_after = {}
    for e, ms in fed_ev.items():
        yes = [m["floor"] for m in ms if m["result"] == "yes" and m["floor"] is not None]
        no = [m["floor"] for m in ms if m["result"] == "no" and m["floor"] is not None]
        if yes and no and min(no) - max(yes) < 0.26:
            upper_after[e] = round(max(yes) + 0.25, 2)
    pairs = []; lead_events = {}
    prev_close = D.ts("2023-07-26T17:55:00Z")      # FEDDECISION-23JUL (hike to 5.50%)
    R = 5.50
    rate_path = []
    for e in meetings:
        cl = close_of[e]; date = dt.datetime.utcfromtimestamp(cl).date()
        fe = fed_by_date.get(date)
        lead_events[e] = {"kind": "fomc", "markets": fd[e], "window": [prev_close + H, cl], "close": cl}
        rate_path.append({"meeting": e, "R_before": R, "fed_event": fe})
        if fe:
            strikes = {round(m["floor"], 2): m["t"] for m in fed_ev[fe] if m["floor"] is not None}
            spec = {round(R - 0.25, 2): ({"H0", "H25", "H26"}, {"C25", "C26"}),
                    round(R - 0.50, 2): ({"H0", "H25", "H26", "C25"}, {"C26"}),
                    round(R, 2): ({"H25", "H26"}, {"H0", "C25", "C26"})}
            for x, (yes_set, no_set) in spec.items():
                if x in strikes:
                    for side, S in (("YES", yes_set), ("NO", no_set)):
                        pairs.append({"group": "A_fed_same_meeting", "lead_event": e, "lead": sorted(fd[e][c] for c in S if c in fd[e]),
                                      "follower": strikes[x], "f_side": side, "window": [prev_close + H, cl], "post": False,
                                      "why": f"{side} of 'above {x}%' after {e} is implied by {sorted(S)} (R={R})"})
            if fe in upper_after:
                R = upper_after[fe]
        prev_close = cl
    # ---------------- B: yearly cut count ----------------
    count_series = {2024: "RATECUTCOUNT-24DEC31", 2025: "KXRATECUTCOUNT-25DEC31"}
    units_so_far = defaultdict(int); prevR = None
    for rp in rate_path:
        e = rp["meeting"]; cl = close_of[e]; y = dt.datetime.utcfromtimestamp(cl).year
        nxt = [r for r in rate_path if r["meeting"] != e and close_of[r["meeting"]] > cl]
        R_after = nxt[0]["R_before"] if nxt else None
        if y in count_series:
            c = units_so_far[y]; ev = count_series[y]
            w = lead_events[e]["window"]
            for k, S in ((c, {"C25", "C26"}), (c + 1, {"C26"})):
                f = f"{ev}-T{k}"
                if f in M:
                    pairs.append({"group": "B_cut_count", "lead_event": e, "lead": sorted(fd[e][x] for x in S if x in fd[e]), "follower": f,
                                  "f_side": "NO", "window": [max(w[0], M[f]["open"]), cl + POST_H * H], "post": True,
                                  "why": f"a cut at {e} (outcomes {sorted(S)}) makes 'exactly {k}' impossible ({c} x25bp so far in {y})"})
        if R_after is not None and R_after < rp["R_before"]:
            units_so_far[y] += int(round((rp["R_before"] - R_after) / 0.25))
    # ---------------- C: cut at least once between S and D ----------------
    for t, m in M.items():
        if not re.match(r"^(KX)?RATECUT-", t):
            continue
        mm = re.search(r"between (\w+ \d+, \d{4}) and (\w+ \d+, \d{4})", m["rules"])
        if not mm:
            continue
        s0 = dt.datetime.strptime(mm.group(1), "%B %d, %Y").replace(tzinfo=dt.timezone.utc).timestamp()
        s1 = dt.datetime.strptime(mm.group(2), "%B %d, %Y").replace(tzinfo=dt.timezone.utc).timestamp() + 86400
        for e in meetings:
            cl = close_of[e]
            if s0 <= cl <= s1 and m["open"] < cl + POST_H * H and m["close"] > lead_events[e]["window"][0]:
                w = lead_events[e]["window"]
                pairs.append({"group": "C_cut_by_date", "lead_event": e, "lead": sorted(fd[e][x] for x in ("C25", "C26") if x in fd[e]),
                              "follower": t, "f_side": "YES", "window": [max(w[0], m["open"]), min(m["close"], cl + POST_H * H)], "post": True,
                              "why": f"a cut at {e} implies '{m['title']}'"})
    # ---------------- D: shutdown ----------------
    gs = {t: m for t, m in M.items() if re.match(r"^(KX)?GOVSHUT-", t) and m["open"] >= D.ts("2023-01-01T00:00:00Z")}
    for t, m in gs.items():
        lead_events[t] = {"kind": "shutdown", "markets": {"YES": t}, "window": [m["open"], m["close"]], "close": m["close"]}

    def addD(lead, fol, side, lead_side, why):
        if lead not in M or fol not in M:
            return
        L, F = M[lead], M[fol]
        w = [max(L["open"], F["open"], L["close"] - D_LOOKBACK_D * 86400), min(F["close"], L["close"] + POST_H * H)]
        if w[1] > w[0]:
            pairs.append({"group": "D_shutdown", "lead_event": lead, "lead": [lead], "lead_side": lead_side, "follower": fol, "f_side": side,
                          "window": w, "post": True, "why": why})
    addD("KXGOVSHUT-25OCT01", "KXSHUTDOWNBY-25", "YES", "YES", "shut down on Oct 1 2025 => shutdown before 2026")
    addD("KXGOVSHUT-25OCT01", "KXGOVSHUTLENGTH-26JAN01-0D", "YES", "YES", "shut down Oct 1 10 AM <=> shutdown starting Oct 1 lasts > 0 days")
    addD("KXGOVSHUT-25OCT01", "KXGOVSHUTLENGTH-26JAN01-0D", "NO", "NO", "no shutdown Oct 1 => the Oct-1 shutdown length market is NO")
    for t, m in M.items():
        if t.startswith("KXGOVSHUTLENGTH-26JAN01-") and t != "KXGOVSHUTLENGTH-26JAN01-0D" and m["open"] < M["KXGOVSHUT-25OCT01"]["close"]:
            addD("KXGOVSHUT-25OCT01", t, "NO", "NO", "no shutdown Oct 1 => 'shutdown starting Oct 1 lasts > N days' is NO")
    addD("KXGOVSHUT-26JAN31", "KXSHUTDOWNBY-26DEC31", "YES", "YES", "shut down on Jan 31 2026 => shutdown in 2026")
    for d_ in ("23OCT02", "23OCT03", "23OCT04", "23NOV17", "23NOV18", "23NOV20"):
        addD(f"GOVSHUT-{d_}", "GOVSHUTLENGTH-23DEC31-T0", "YES", "YES", f"shut down on {d_} => shut down > 0 days in 2023")
    for d_ in ("24JAN20", "24JAN22", "24FEB03", "24FEB05", "24MAR04", "24MAR11", "24MAR23"):
        addD(f"GOVSHUT-{d_}", "GOVSHUTLENGTH-24JUN30-T0", "YES", "YES", f"shut down on {d_} => shut down > 0 days by Jun 2024")
    addD("GOVSHUT-24MAR23", "SHUTDOWNBY-24", "YES", "YES", "shut down on Mar 23 2024 => shutdown in 2024")
    for p in pairs:
        p.setdefault("lead_side", "YES")
        p["id"] = hashlib.sha1(json.dumps([p["group"], p["lead_event"], p["lead"], p["follower"], p["f_side"]]).encode()).hexdigest()[:10]
    doc = {"frozen_at": dt.datetime.utcnow().isoformat() + "Z", "note": "built from listing metadata only (no prices loaded)",
           "n_pairs": len(pairs), "groups": {g: sum(p["group"] == g for p in pairs) for g in sorted({p["group"] for p in pairs})},
           "rate_path": rate_path, "lead_events": lead_events, "pairs": pairs}
    FROZEN.write_text(json.dumps(doc, indent=1))
    print(json.dumps(doc["groups"]), len(pairs), "pairs;", len(lead_events), "lead events")
    for rp in rate_path:
        print(rp)


def _load_frozen() -> dict:
    return json.loads(FROZEN.read_text())


def _lead_fetch_list(fz: dict) -> list[tuple[str, int, int]]:
    out = []
    for e, le in fz["lead_events"].items():
        yr = dt.datetime.utcfromtimestamp(le["close"]).year
        codes = (("H0", "C25", "H25") if yr in (2023, 2026) else ("H0", "C25")) if le["kind"] == "fomc" else ("YES",)
        w = le["window"]
        if le["kind"] == "shutdown":
            w = [max(w[0], w[1] - D_LOOKBACK_D * 86400), w[1]]
        for c in codes:
            t = le["markets"].get(c)
            if t:
                out.append((t, int(w[0]), int(w[1])))
    return out


def fetch_leads(dry: bool = False) -> None:
    fz = _load_frozen(); sp = D.requested_spans(60); store = D.harvest_cache([t for t, _, _ in _lead_fetch_list(fz)], 60)
    M = D.meta(); todo = []
    for t, a, b in _lead_fetch_list(fz):
        if M.get(t, {}).get("vol", 1) < 100 or t == "FEDDECISION-23SEP-C25":
            continue                                   # never traded: no quotes to learn from
        cov = D.span_cover(sp.get(t, []), a, b)
        if cov < 0.9:
            todo.append((t, a, b, cov))
    print(len(todo), "lead fetches needed; calls used so far", used())
    for t, a, b, cov in todo:
        print(" ", t, dt.datetime.utcfromtimestamp(a).date(), dt.datetime.utcfromtimestamp(b).date(), round(cov, 2))
    if dry or "--dry" in sys.argv:
        return
    for t, a, b, cov in todo:
        n = D.fetch_hist(store, t, a - 6 * H, b) if M[t]["tier"] == "hist" else D.fetch_live(store, [t], a - 6 * H, b)
        print(t, n, "candles; used", used(), flush=True)
    D.save_store(store, 60)


CF_H = 24            # archive candles are emitted only when the quote/trade changes: carry the last one forward this long
MAX_SPREAD = 0.10


def grid(rows: list[list], t0: int, t1: int, cf_h: int = CF_H) -> dict[int, tuple]:
    """hour -> (ask, bid) from the last candle at or before the hour (<= cf_h old)."""
    out = {}; i = 0; last = None
    h = (t0 // H) * H
    while h <= t1:
        while i < len(rows) and rows[i][0] <= h:
            last = rows[i]; i += 1
        if last is not None and h - last[0] <= cf_h * H:
            out[h] = (last[1], last[2])
        h += H
    return out


def mid(q):
    if not q or q[0] is None or q[1] is None or q[0] - q[1] > MAX_SPREAD or q[0] < q[1]:
        return None
    return (q[0] + q[1]) / 2


def lead_moves(fz: dict, store: dict, M: dict, move: float = 0.15, k_max: int = 3, sep_h: int = 12) -> list[dict]:
    eps = []
    for e, le in fz["lead_events"].items():
        w = le["window"]
        if le["kind"] == "shutdown":
            w = [max(w[0], w[1] - D_LOOKBACK_D * 86400), w[1]]
        trig = []
        for c, t in le["markets"].items():
            rows = D.series_of(store, t)
            if not rows:
                continue
            g = grid(rows, w[0], w[1])
            hs = sorted(g)
            for h in hs:
                m1 = mid(g[h])
                if m1 is None:
                    continue
                for k in range(1, k_max + 1):
                    m0 = mid(g.get(h - k * H))
                    if m0 is not None and abs(m1 - m0) >= move:
                        trig.append((h, t, round(m1 - m0, 3), "quote")); break
            # settlement of the lead as a move (followers that outlive the lead)
            res = M.get(t, {}).get("result")
            if res in ("yes", "no"):
                lastm = None
                for h in reversed(hs):
                    if h <= w[1] and mid(g[h]) is not None:
                        lastm = mid(g[h]); break
                val = 1.0 if res == "yes" else 0.0
                if lastm is not None and abs(val - lastm) >= move:
                    st_ = M[t].get("settled") or w[1]
                    trig.append((((max(st_, w[1]) // H) + 1) * H, t, round(val - lastm, 3), "settle"))
        trig.sort()
        last = None
        for h, t, dm, kind in trig:
            if last is None or h >= last + sep_h * H:
                eps.append({"lead_event": e, "t_move": h, "market": t, "dmid": dm, "kind": kind}); last = h
    eps.sort(key=lambda x: x["t_move"])
    return eps


def moves() -> None:
    fz = _load_frozen(); M = D.meta(); store = D.load_store(60)
    for mv in (0.10, 0.15, 0.20):
        eps = lead_moves(fz, store, M, move=mv)
        by = defaultdict(int)
        for x in eps:
            by[x["lead_event"]] += 1
        print(f"move >= {mv:.2f}: {len(eps)} episodes over {len(by)} lead events; settle-type {sum(x['kind']=='settle' for x in eps)}")
        if mv == 0.15:
            for x in eps:
                print("  ", dt.datetime.utcfromtimestamp(x["t_move"]).strftime("%Y-%m-%d %H:%M"), x["lead_event"], x["market"], x["dmid"], x["kind"])
            (OUT / "lead_moves_015.json").write_text(json.dumps(eps, indent=0))


LIVE_LO, LIVE_HI = 0.03, 0.97     # a bound is "live" at an episode when its lead-sum mid lies inside this band


def lead_sum(store: dict, M: dict, tickers: list[str], side: str, h: int, which: str = "bid", w_close: int | None = None) -> float | None:
    """Sum over the lead set of the YES bid (side YES) or NO bid = 1 - YES ask (side NO) at hour h, carried forward.
    A settled lead counts at its settlement value from its close on. Missing component -> 0 (conservative)."""
    tot = 0.0; any_ = False
    for t in tickers:
        m = M.get(t, {})
        if m.get("result") in ("yes", "no") and h >= m["close"]:
            v = 1.0 if m["result"] == "yes" else 0.0
            tot += v if side == "YES" else 1 - v; any_ = True; continue
        rows = D.series_of(store, t)
        q = grid(rows, h - CF_H * H, h).get(h) if rows else None
        if not q:
            continue
        ask, bid = q
        if which == "mid":
            mm = mid(q)
            if mm is None:
                continue
            tot += mm if side == "YES" else 1 - mm; any_ = True
        else:
            if side == "YES" and bid is not None:
                tot += bid; any_ = True
            elif side == "NO" and ask is not None:
                tot += 1 - ask; any_ = True
    return tot if any_ else None


def episode_pairs(fz, store, M, eps):
    """(episode, pair) combos where the pair window covers the decision and the bound is live (lead prices only)."""
    by_lead = defaultdict(list)
    for p in fz["pairs"]:
        by_lead[p["lead_event"]].append(p)
    out = []
    for x in eps:
        for p in by_lead[x["lead_event"]]:
            td = x["t_move"] + H
            if not (p["window"][0] <= td <= p["window"][1]):
                continue
            if not p["post"] and td >= M[p["follower"]]["close"]:
                continue
            ls = lead_sum(store, M, p["lead"], p["lead_side"], x["t_move"], "mid")
            if ls is None or not (LIVE_LO <= ls <= LIVE_HI):
                ls2 = lead_sum(store, M, p["lead"], p["lead_side"], x["t_move"] - 3 * H, "mid")
                if ls2 is None or not (LIVE_LO <= ls2 <= LIVE_HI):
                    continue
            out.append((x, p))
    return out


def fetch_followers(dry: bool = False) -> None:
    fz = _load_frozen(); M = D.meta(); store = D.load_store(60)
    eps = lead_moves(fz, store, M, move=0.10)            # fetch for the widest move grid examined on discovery
    combos = episode_pairs(fz, store, M, eps)
    store = D.harvest_cache(sorted({p["follower"] for _, p in combos}) + [t for t in store], 60)
    sp = D.requested_spans(60)
    need = {}
    for x, p in combos:
        f = p["follower"]; w = p["window"]
        if p["group"] == "D_shutdown":
            w = [max(w[0], x["t_move"] - 3 * 86400), w[1]]
        key = (f, p["lead_event"])
        a, b = need.get(key, (w[0], w[1]))
        need[key] = (min(a, w[0]), max(b, w[1]))
    todo = []
    for (f, e), (a, b) in sorted(need.items()):
        b = min(b, M[f]["close"] + H)
        cov = D.span_cover(sp.get(f, []), a, b)
        if cov < 0.9 and M[f]["vol"] > 0:
            todo.append((f, a - CF_H * H, b))
    print(len(combos), "episode-pair combos;", len(need), "follower windows;", len(todo), "to fetch; used", used())
    for t, a, b in todo:
        print("  ", t, dt.datetime.utcfromtimestamp(a).date(), dt.datetime.utcfromtimestamp(b).date())
    if dry or "--dry" in sys.argv:
        return
    for t, a, b in todo:
        n = D.fetch_hist(store, t, a, b) if M[t]["tier"] == "hist" else D.fetch_live(store, [t], a, b)
        print(t, n, "candles; used", used(), flush=True)
        D.save_store(store, 60)


def fee10(p: float) -> float:
    """Per-contract taker fee of a 10-contract order (Kalshi rounds the order fee up to the cent)."""
    return math.ceil(round(0.07 * 10 * p * (1 - p) * 100, 6)) / 100 / 10


def fquote(store, t, h):
    rows = D.series_of(store, t)
    return grid(rows, h - CF_H * H, h).get(h) if rows else None


def side_px(q, side):
    if not q:
        return None
    ask, bid = q
    if side == "YES":
        return ask
    return None if bid is None else round(1 - bid, 4)


def violation(store, M, p, h):
    """(v, follower side price) at hour h: lead-side bid sum - follower-side taker price - fee."""
    f = p["follower"]
    if h >= M[f]["close"]:
        return None, None
    px = side_px(fquote(store, f, h), p["f_side"])
    L = lead_sum(store, M, p["lead"], p["lead_side"], h, "bid")
    if px is None or L is None:
        return None, px
    return L - px - fee10(px), px


def simulate(fz, store, M, move=0.15, dec_h=1, thr=0.08, fill_h=1, groups=None, exit_close=False):
    eps = lead_moves(fz, store, M, move=move)
    combos = episode_pairs(fz, store, M, eps)
    trades = []; seen = set(); diag = []
    for x, p in combos:
        if groups and p["group"][0] not in groups:
            continue
        td = x["t_move"] + dec_h * H; tf = td + fill_h * H
        if not (p["window"][0] <= td <= p["window"][1]):
            continue
        v, pxd = violation(store, M, p, td)
        diag.append({"ep": x["t_move"], "lead_event": x["lead_event"], "pair": p["id"], "group": p["group"], "v": v})
        if v is None or v < thr:
            continue
        key = (p["follower"], x["t_move"])
        if key in seen:
            continue
        f = p["follower"]
        if tf >= M[f]["close"]:
            continue
        px = side_px(fquote(store, f, tf), p["f_side"])
        if px is None or not (0.01 <= px <= 0.99):
            continue
        seen.add(key)
        res = M[f]["result"]
        if res not in ("yes", "no"):
            continue
        win = (res == "yes") == (p["f_side"] == "YES")
        payoff = 1.0 if win else 0.0
        exit_at = None
        if exit_close:   # sell at the bid once the bound has closed (v <= 0), else hold to settlement
            h = tf + H
            while h < M[f]["close"] and h <= tf + 30 * 86400:
                v2, _ = violation(store, M, p, h)
                if v2 is not None and v2 <= 0:
                    q = fquote(store, f, h)
                    if q:
                        bidside = q[1] if p["f_side"] == "YES" else (None if q[0] is None else 1 - q[0])
                        if bidside is not None:
                            payoff = bidside - fee10(bidside); exit_at = h; break
                h += H
        pnl = payoff - px - fee10(px)
        trades.append({"e": x["lead_event"], "t": x["t_move"], "td": td, "tf": tf, "follower": f, "side": p["f_side"], "group": p["group"],
                       "v_dec": round(v, 4), "px_dec": pxd, "px": px, "won": win, "ret": pnl / px, "exit_at": exit_at, "pair": p["id"]})
    return trades, diag


def stats(rows):
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em); mean = st.mean(r["ret"] for r in rows)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    return {"n": len(rows), "events": n_e, "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret": round(mean, 4), "t": round(t, 2) if t == t else None, "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None}


def t_cut(fz, store, M):
    eps = [x for x in lead_moves(fz, store, M, move=0.15)]
    eps = [x for x in eps if any(True for _ in [0])]
    ts_ = sorted({x["t_move"] for x, _ in episode_pairs(fz, store, M, eps)})
    return ts_[int(len(ts_) * 0.7)], len(ts_)


GRID = [dict(move=mv, dec_h=dh, thr=th, groups=g) for mv in (0.10, 0.15, 0.20) for dh in (0, 1, 2) for th in (0.04, 0.08, 0.12)
        for g in (None, "A", "BC", "D")]


def discover() -> None:
    fz = _load_frozen(); M = D.meta(); store = D.load_store(60)
    cut, n_ep = t_cut(fz, store, M)
    print("split: 70% of", n_ep, "move>=0.15 episodes with a live pair; cut at", dt.datetime.utcfromtimestamp(cut))
    res = []
    for g in GRID:
        tr, diag = simulate(fz, store, M, **g)
        d = [r for r in tr if r["t"] < cut]
        sd = stats(d); res.append({"rule": g, **sd})
        print(json.dumps(g), json.dumps(sd))
    (OUT / "discovery.json").write_text(json.dumps({"cut": cut, "n_variants": len(GRID), "cells": res}, indent=1))


def simulate_uncond(fz, store, M, thr=0.08, fill_h=1, sep_h=24, t_lo=0, t_hi=10**10):
    """No move conditioning: every hour of every pair window where the follower and the lead set are quoted."""
    trades = []; hours = 0; viol_hours = 0
    have = set(store)
    for p in fz["pairs"]:
        f = p["follower"]
        if f not in have:
            continue
        w0, w1 = p["window"]
        if p["group"] == "D_shutdown":
            w0 = max(w0, w1 - (D_LOOKBACK_D + 2) * 86400)
        h = (max(w0, t_lo) // H + 1) * H; last = -10**10
        while h <= min(w1, t_hi, M[f]["close"] - H):
            v, _ = violation(store, M, p, h)
            if v is not None:
                hours += 1
                if v >= thr:
                    viol_hours += 1
                    if h >= last + sep_h * H:
                        tf = h + fill_h * H
                        px = side_px(fquote(store, f, tf), p["f_side"]) if tf < M[f]["close"] else None
                        res = M[f]["result"]
                        if px is not None and 0.01 <= px <= 0.99 and res in ("yes", "no"):
                            win = (res == "yes") == (p["f_side"] == "YES")
                            trades.append({"e": p["lead_event"], "t": h, "follower": f, "side": p["f_side"], "group": p["group"], "v_dec": round(v, 4),
                                           "px": px, "won": win, "ret": ((1.0 if win else 0.0) - px - fee10(px)) / px})
                            last = h
            h += H
    return trades, hours, viol_hours


def uncond() -> None:
    fz = _load_frozen(); M = D.meta(); store = D.load_store(60)
    cut, _ = t_cut(fz, store, M)
    out = {}
    for thr in (0.04, 0.08, 0.12):
        tr, hrs, vh = simulate_uncond(fz, store, M, thr=thr, t_hi=cut)
        out[thr] = {"pair_hours": hrs, "violation_hours": vh, **stats(tr)}
        print("discovery, unconditional, thr", thr, out[thr])
        for r in tr[:15]:
            print("   ", dt.datetime.utcfromtimestamp(r["t"]).strftime("%Y-%m-%d %H"), r["follower"], r["side"], r["v_dec"], r["px"], r["won"], round(r["ret"], 3))
    (OUT / "discovery_unconditional.json").write_text(json.dumps(out, indent=1))


# ---------------------------------------------------------------- 1-minute lag probe (descriptive; no parameter is chosen from it)
PROBE = [("2023-09-30 17:00", "GOVSHUT-23OCT02", ["GOVSHUTLENGTH-23DEC31-T0"]),
         ("2024-01-31 21:00", "FEDDECISION-24MAR20", ["FED-24MAR-T5.25", "RATECUT-24MAR20", "RATECUTCOUNT-24DEC31-T0"]),
         ("2024-09-12 18:00", "FEDDECISION-24SEP", ["FED-24SEP-T5.00", "RATECUTCOUNT-24DEC31-T1"]),
         ("2024-11-13 14:00", "KXFEDDECISION-24DEC", ["FED-24DEC-T4.50"]),
         ("2024-12-06 14:00", "KXFEDDECISION-24DEC", ["FED-24DEC-T4.50"]),
         ("2025-08-01 13:00", "KXFEDDECISION-25SEP", ["FED-25SEP-T4.25", "KXRATECUTCOUNT-25DEC31-T0"]),
         ("2025-09-29 22:00", "KXGOVSHUT-25OCT01", ["KXSHUTDOWNBY-25", "KXGOVSHUTLENGTH-26JAN01-0D"]),
         ("2025-11-19 18:00", "KXFEDDECISION-25DEC", ["FED-25DEC-T3.75", "KXRATECUTCOUNT-25DEC31-T2"]),
         ("2025-11-21 13:00", "KXFEDDECISION-25DEC", ["FED-25DEC-T3.75", "KXRATECUTCOUNT-25DEC31-T2"]),
         ("2026-01-29 05:00", "KXGOVSHUT-26JAN31", ["KXSHUTDOWNBY-26DEC31"]),
         ("2026-07-14 13:00", "KXFEDDECISION-26JUL", ["KXFED-26JUL-T3.75"]),
         ("2026-09-11 13:00", "KXFEDDECISION-26SEP", ["KXFED-26SEP-T3.75"])]
PRE_MIN, POST_MIN = 240, 120


def _probe_leads(fz, e):
    le = fz["lead_events"][e]
    return [le["markets"][c] for c in ("H0", "C25", "YES", "C26", "H25") if c in le["markets"]][:2]


def fetch_lag(dry: bool = False) -> None:
    fz = _load_frozen(); M = D.meta(); st1 = D.load_store(1); sp = D.requested_spans(1)
    todo = []
    for when, e, fols in PROBE:
        tm = int(dt.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp())
        a, b = tm - PRE_MIN * 60, tm + POST_MIN * 60
        for t in _probe_leads(fz, e) + fols:
            if D.span_cover(sp.get(t, []), a, b) < 0.9:
                todo.append((t, a, b))
    print(len(todo), "minute fetches; used", used())
    if dry or "--dry" in sys.argv:
        for x in todo:
            print("  ", x)
        return
    live = [x for x in todo if M[x[0]]["tier"] == "live"]
    for t, a, b in todo:
        if M[t]["tier"] == "hist":
            print(t, D.fetch_hist(st1, t, a, b, period=1), "used", used(), flush=True)
    if live:
        a, b = live[0][1], live[0][2]
        print("live batch", D.fetch_live(st1, [x[0] for x in live], a, b, period=1), "used", used())
    D.save_store(st1, 1)


def lag() -> None:
    """Minute-level: when does the lead cross 50% of its move, when does each follower's bound-implied price follow?"""
    fz = _load_frozen(); M = D.meta(); st1 = D.load_store(1)
    out = []
    pairs = fz["pairs"]
    for when, e, fols in PROBE:
        tm = int(dt.datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp())
        a, b = tm - PRE_MIN * 60, tm + POST_MIN * 60
        for f in fols:
            ps = [p for p in pairs if p["lead_event"] == e and p["follower"] == f]
            for p in ps:
                def series(tk):
                    rows = D.series_of(st1, tk); g = {}; i = 0; last = None
                    for m in range(a, b + 1, 60):
                        while i < len(rows) and rows[i][0] <= m:
                            last = rows[i]; i += 1
                        if last is not None and m - last[0] <= 240 * 60:   # minute candles are emitted on change
                            g[m] = (last[1], last[2])
                    return g
                Lg = {t: series(t) for t in p["lead"]}
                Fg = series(f)
                def lmid(m):
                    tot = 0; ok = False
                    for t, g in Lg.items():
                        mm = mid(g.get(m))
                        if mm is not None:
                            tot += mm if p["lead_side"] == "YES" else 1 - mm; ok = True
                    return tot if ok else None
                def fmid(m):
                    mm = mid(Fg.get(m)) if Fg.get(m) and Fg[m][0] is not None and Fg[m][1] is not None and Fg[m][0] - Fg[m][1] <= 0.25 else None
                    if mm is None:
                        return None
                    return mm if p["f_side"] == "YES" else 1 - mm
                mins = list(range(a, b + 1, 60))
                L = {m: lmid(m) for m in mins}; F = {m: fmid(m) for m in mins}
                l0 = [L[m] for m in mins[:60] if L[m] is not None]; l1 = [L[m] for m in mins[-30:] if L[m] is not None]
                f0 = [F[m] for m in mins[:60] if F[m] is not None]; f1 = [F[m] for m in mins[-30:] if F[m] is not None]
                rec = {"move": when, "lead_event": e, "follower": f, "side": p["f_side"], "lead": p["lead"],
                       "lead_minutes": sum(v is not None for v in L.values()), "fol_minutes": sum(v is not None for v in F.values())}
                if l0 and l1 and f0 and f1:
                    L0, L1, F0, F1 = st.median(l0), st.median(l1), st.median(f0), st.median(f1)
                    rec.update({"lead_pre": round(L0, 3), "lead_post": round(L1, 3), "fol_pre": round(F0, 3), "fol_post": round(F1, 3)})
                    def cross(S, x0, x1):
                        if abs(x1 - x0) < 0.05:
                            return None
                        for m in mins:
                            v = S[m]
                            if v is not None and (v - x0) / (x1 - x0) >= 0.5:
                                return m
                        return None
                    tl, tf_ = cross(L, L0, L1), cross(F, F0, F1)
                    rec["t_lead50"] = None if tl is None else dt.datetime.utcfromtimestamp(tl).strftime("%H:%M")
                    rec["t_fol50"] = None if tf_ is None else dt.datetime.utcfromtimestamp(tf_).strftime("%H:%M")
                    rec["lag_min"] = None if (tl is None or tf_ is None) else (tf_ - tl) // 60
                # bound violation at minute resolution (bids vs asks), after the lead's 50% crossing
                vs = []
                for m in mins:
                    ls = 0; ok = False
                    for t, g in Lg.items():
                        q = g.get(m)
                        if q:
                            if p["lead_side"] == "YES" and q[1] is not None:
                                ls += q[1]; ok = True
                            elif p["lead_side"] == "NO" and q[0] is not None:
                                ls += 1 - q[0]; ok = True
                    px = side_px(Fg.get(m), p["f_side"])
                    if ok and px is not None:
                        vs.append((m, ls - px - fee10(px)))
                rec["max_v"] = round(max((v for _, v in vs), default=float("nan")), 3)
                rec["min_v_ge_008"] = sum(1 for _, v in vs if v >= 0.08)
                rec["min_v_ge_004"] = sum(1 for _, v in vs if v >= 0.04)
                out.append(rec)
                print(json.dumps(rec))
    lags = [r["lag_min"] for r in out if r.get("lag_min") is not None]
    summ = {"n_pairs": len(out), "n_lag": len(lags), "median_lag_min": st.median(lags) if lags else None, "lags": sorted(lags),
            "pairs_with_any_minute_v_ge_008": sum(1 for r in out if r["min_v_ge_008"] > 0)}
    print(json.dumps(summ))
    (OUT / "lag_probe.json").write_text(json.dumps({"summary": summ, "rows": out}, indent=1))


CANDIDATES = [
    {"name": "C1 plan rule: move>=15c (<=3h), decide +1h, v>=8c, taker fill +1h, hold", "kind": "move", "params": dict(move=0.15, dec_h=1, thr=0.08, groups=None)},
    {"name": "C2 best discovery cell with a trade: move>=10c, decide +1h, v>=4c, fill +1h", "kind": "move", "params": dict(move=0.10, dec_h=1, thr=0.04, groups=None)},
    {"name": "C3 unconditional bound violation (any hour), v>=8c, fill +1h, 1 per pair per 24h", "kind": "uncond", "params": dict(thr=0.08)},
]


def freeze_candidates() -> None:
    f = OUT / "frozen_candidates.json"
    if f.exists():
        print("already frozen", f); return
    f.write_text(json.dumps({"frozen_at": dt.datetime.utcnow().isoformat() + "Z", "candidates": CANDIDATES,
                             "note": "chosen on discovery only (discovery.json, discovery_unconditional.json); validation is run once"}, indent=1))
    print("frozen", f)


def _report_rows(rows, cut_end=None):
    s_ = stats(rows)
    if rows:
        ts_ = sorted(r["t"] for r in rows); midt = ts_[len(ts_) // 2]
        h1 = [r["ret"] for r in rows if r["t"] < midt]; h2 = [r["ret"] for r in rows if r["t"] >= midt]
        s_["half1"] = round(st.mean(h1), 4) if h1 else None; s_["half2"] = round(st.mean(h2), 4) if h2 else None
    return s_


def validate() -> None:
    fz = _load_frozen(); M = D.meta(); store = D.load_store(60)
    cands = json.loads((OUT / "frozen_candidates.json").read_text())["candidates"]
    vf = OUT / "validation.json"
    if vf.exists():
        print("validation already run once:", vf.read_text()[:3000]); return
    cut, _ = t_cut(fz, store, M)
    span_days = (max(x["t_move"] for x in lead_moves(fz, store, M, 0.15)) - cut) / 86400
    out = []
    for c in cands:
        if c["kind"] == "move":
            tr, _ = simulate(fz, store, M, **c["params"])
        else:
            tr, _, _ = simulate_uncond(fz, store, M, thr=c["params"]["thr"], t_lo=cut)
        v = [r for r in tr if r["t"] >= cut]
        s_ = _report_rows(v); s_["rule"] = c["name"]; s_["trades_per_day"] = round(len(v) / span_days, 3) if span_days > 0 else None
        s_["trades"] = v
        out.append(s_)
        print(json.dumps({k: x for k, x in s_.items() if k != "trades"}))
        for r in v:
            print("    ", dt.datetime.utcfromtimestamp(r["t"]).strftime("%Y-%m-%d %H"), r["follower"], r["side"], r.get("v_dec"), r["px"], r["won"], round(r["ret"], 3))
    vf.write_text(json.dumps({"cut": cut, "validation_span_days": span_days, "results": out}, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    globals()[cmd]()
