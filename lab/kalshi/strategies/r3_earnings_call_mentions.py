"""r3_earnings_call_mentions: are Kalshi earnings-call mention markets ("will <company> say <word> during its next
earnings call") mispriced against the company's own word base rates, and does a listing-time maker NO pay?

Data (r3_earnings_call_mentions_data.py, Kalshi calls logged in calls.log):
  markets_hist.jsonl   /historical results (settled before ~2026-08-08) for 93 series: base rates only, never prices
  markets_live.jsonl   live-tier lists for 61 series; 46 settled events 2026-08-05..10-08 have prices
  candles_W1.jsonl     hourly candles [first open - 1 h, first open + 49 h]   (listing / first-quote decision)
  candles_W2.jsonl     hourly candles [first close - 52 h, last close + 1 h]   (call detection, pre-call decision)
  candles_D.jsonl      daily candles over each event's life                    (maker fill path between W1 and W2)
  candle row: [end_ts, ask_close, bid_close, ask_low, bid_high, price_close, price_high, price_low, volume]

No look-ahead:
  * Earnings-mention markets do NOT close when the word is said: every market of an event closes in bulk 0.5-27 h after
    the call. The scheduled call start is public (company IR; Kalshi's occurrence_datetime while the event is open) but
    the settled API record overwrites it. The backtest therefore locates the call as the first hour in which >= 2 of the
    event's markets jump to a 0.95 bid / 0.97 print from an ask <= 0.85 three hours earlier (words being said), and
    treats call start = start of that hour. The pre-call decision is 2 h before that (H_pre = jump_end - 3 h); its
    taker fill uses the next hourly quote (H_pre + 1 h, still before the call). The detected hour only fixes WHEN a
    pre-call snapshot is taken; prices before it carry no in-call information.
  * first-quote decision: first top-of-hour >= first open + 1 h; taker fill at the next hourly quote.
  * base rates: same series and word key, other events, close_time <= H - 1 h; shrunk p = (k + 3 p0)/(n + 3) with p0 the
    series' YES rate so far ((K+1)/(N+2)); a = 3 is the round-2 value, not tuned here.
  * taker fee 0.07 p (1-p) per contract, rounded up to the cent per order of floor($5/p) contracts. Makers pay nothing
    (all series fee_type 'quadratic').
Split: events ordered by first close; discovery = first 70%, validation = last 30%.
Usage: python -m lab.kalshi.strategies.r3_earnings_call_mentions [overview|killtest|taker|maker|validate|all]"""
from __future__ import annotations
import datetime as dt, json, math, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/r3_earnings_call_mentions")
ET = ZoneInfo("America/New_York")
STAKE = 5.0
SPLIT = 0.7
A_SHRINK = 3.0
VARIANTS = []          # every variant examined is appended here (name, part)


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def et(t):
    return dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")


def wkey(m: dict) -> str:
    w = m.get("word") or (m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or ""
    parts = sorted(p.strip() for p in re.sub(r"[^a-z0-9/+() ]", "", w.lower()).split("/") if p.strip())
    return "/".join(parts)


def fee_pc(p: float) -> float:
    n = max(1, int(STAKE / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def logit(p):
    p = min(max(p, 0.005), 0.995)
    return math.log(p / (1 - p))


def sig(z):
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


# ------------------------------------------------------------------------------------------------------------ loading
def load():
    hist = [json.loads(l) for l in (OUT / "markets_hist.jsonl").open()]
    live = [json.loads(l) for l in (OUT / "markets_live.jsonl").open()]
    allm = {}
    for m in hist + live:                       # live record wins on overlap
        allm[m["ticker"]] = m
    pool = []
    for m in allm.values():
        if m["result"] in ("yes", "no"):
            pool.append({"s": m["series"], "w": wkey(m), "close": ts(m["close_time"]), "y": m["result"] == "yes", "e": m["event_ticker"]})
    H = defaultdict(dict); D = defaultdict(dict)
    for w in ("W1", "W2"):
        for l in (OUT / f"candles_{w}.jsonl").open():
            x = json.loads(l)
            for t, cs in x["c"].items():
                for r in cs:
                    H[t][r[0]] = r
    for l in (OUT / "candles_D.jsonl").open():
        x = json.loads(l)
        for t, cs in x["c"].items():
            for r in cs:
                D[t][r[0]] = r
    W = {"W1": {}, "W2": {}}
    for w in ("W1", "W2"):
        for l in (OUT / f"candles_{w}.jsonl").open():
            x = json.loads(l)
            W[w][x["e"]] = (x["lo"], x["hi"])
    mk = [m for m in live if m["result"] in ("yes", "no") and m["ticker"] in H]
    ev = defaultdict(list)
    for m in mk:
        m["open_ts"], m["close_ts"] = ts(m["open_time"]), ts(m["close_time"]); m["y"] = m["result"] == "yes"; m["w"] = wkey(m)
        m["vol"] = float(m.get("volume_fp") or 0)
        ev[m["event_ticker"]].append(m)
    events = {}
    for e, x in ev.items():
        events[e] = {"e": e, "series": x[0]["series"], "markets": x, "open": min(m["open_ts"] for m in x),
                     "first_close": min(m["close_ts"] for m in x), "last_close": max(m["close_ts"] for m in x),
                     "w1": W["W1"].get(e), "w2": W["W2"].get(e)}
    order = sorted(events, key=lambda e: (events[e]["first_close"], e))
    cut = int(round(len(order) * SPLIT))
    for i, e in enumerate(order):
        events[e]["part"] = "disc" if i < cut else "val"
        events[e]["idx"] = i
    Hs = {t: sorted(v.values()) for t, v in H.items()}
    Ds = {t: sorted(v.values()) for t, v in D.items()}
    return events, Hs, Ds, pool


# ------------------------------------------------------------------------------------------------------ call detection
def detect_call(evd, Hs):
    """End ts of the first hourly candle (within 52 h of the event's first close) in which any market jumps
    (yes_bid high >= 0.90 or a print >= 0.95) from an ask <= 0.85 three hours earlier: words being said. Deliberately
    sensitive: a spurious early hit only moves the pre-call snapshot earlier, never into the call."""
    lo = evd["first_close"] - 52 * 3600; best = None
    for m in evd["markets"]:
        cs = Hs.get(m["ticker"], []); idx = {r[0]: r for r in cs}
        for r in cs:
            T = r[0]
            if T < lo or (best is not None and T >= best):
                continue
            prev = idx.get(T - 3 * 3600)
            if prev is None or prev[1] is None or prev[1] > 0.85:
                continue
            if (r[4] is not None and r[4] >= 0.90) or (r[6] is not None and r[6] >= 0.95):
                best = T; break
    return best


def precall_hour(evd, Hs):
    """Pre-call decision hour: 14:00 ET on the call day for an afternoon/evening call (releases come at 16:00 or later),
    06:00 ET for a morning call, and never later than 3 h before the detected jump hour's end. None if no jump found."""
    T = detect_call(evd, Hs)
    if T is None:
        return None
    d = dt.datetime.fromtimestamp(T - 3600, ET)
    S = dt.datetime(d.year, d.month, d.day, 14 if d.hour >= 12 else 6, tzinfo=ET).timestamp()
    return int(min(S, T - 3 * 3600)) // 3600 * 3600


def quote(Hs, t, H):
    """(ask, bid) of the hourly candle ending exactly at H (book at H); None if missing/one-sided."""
    for r in Hs.get(t, []):
        if r[0] == H:
            if r[1] is None or r[2] is None or not (0 < r[2] < r[1] < 1):
                return None
            return r[1], r[2]
    return None


# ----------------------------------------------------------------------------------------------------------- base rates
class BaseRates:
    def __init__(self, pool):
        self.by_w = defaultdict(list); self.by_s = defaultdict(list); self.all = []
        for p in pool:
            self.by_w[(p["s"], p["w"])].append((p["close"], p["y"], p["e"]))
            self.by_s[p["s"]].append((p["close"], p["y"], p["e"]))
            self.all.append((p["close"], p["y"], p["e"]))

    def get(self, s, w, T, e, a=A_SHRINK):
        x = [y for c, y, ee in self.by_w.get((s, w), []) if c <= T - 3600 and ee != e]
        g = [y for c, y, ee in self.by_s.get(s, []) if c <= T - 3600 and ee != e]
        if len(g) < 10:
            g = [y for c, y, ee in self.all if c <= T - 3600 and ee != e]
        p0 = (sum(g) + 1) / (len(g) + 2)
        k, n = sum(x), len(x)
        return {"k": k, "n": n, "p0": p0, "pbr": (k + a * p0) / (n + a)}


# ------------------------------------------------------------------------------------------------------------ snapshots
def snapshots(events, Hs, BR, which):
    """One decision per market. which = 'first' (first top-of-hour >= open + 1 h) or 'pre' (2 h before the detected call
    hour). Returns dicts with decision quote (ask, bid at H) and fill quote (next hour)."""
    out = []
    for e, ev in events.items():
        if which == "pre":
            H0 = precall_hour(ev, Hs)
            if H0 is None:
                continue
        for m in ev["markets"]:
            if which == "first":
                H = ((m["open_ts"] + 3600 + 3599) // 3600) * 3600
            else:
                H = H0
                if H < m["open_ts"] + 3600:
                    continue
            q = quote(Hs, m["ticker"], H)
            if q is None:
                continue
            qf = quote(Hs, m["ticker"], H + 3600)
            b = BR.get(m["series"], m["w"], H, e)
            out.append({"m": m, "e": e, "part": ev["part"], "H": H, "ask": q[0], "bid": q[1], "mid": (q[0] + q[1]) / 2,
                        "fask": qf[0] if qf else None, "fbid": qf[1] if qf else None, "y": m["y"], "tclose": ev["first_close"], **b})
    return out


# ----------------------------------------------------------------------------------------------------------- statistics
def cstats(rows):
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; ne = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(ne - 1)) if ne > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    mid = sorted(r["tclose"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["tclose"] < mid]; h2 = [r["ret"] for r in rows if r["tclose"] >= mid]
    return {"n": len(rows), "events": ne, "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2), "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None}


def solve(A, v):
    n = len(v); M = [row[:] + [v[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c])); M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[c][c] != 0:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def inv(A):
    n = len(A); cols = [solve(A, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def fit_logit(X, y, groups, iters=60):
    """Newton-Raphson logistic regression; returns (b, cluster-robust SE by group)."""
    k = len(X[0]); b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; Hm = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            p = sig(sum(a * c for a, c in zip(b, xi)))
            for i in range(k):
                g[i] += (yi - p) * xi[i]
                for j in range(k):
                    Hm[i][j] += p * (1 - p) * xi[i] * xi[j]
        step = solve(Hm, g); b = [a + s for a, s in zip(b, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    Hm = [[0.0] * k for _ in range(k)]; sg = defaultdict(lambda: [0.0] * k)
    for xi, yi, gi in zip(X, y, groups):
        p = sig(sum(a * c for a, c in zip(b, xi)))
        for i in range(k):
            sg[gi][i] += (yi - p) * xi[i]
            for j in range(k):
                Hm[i][j] += p * (1 - p) * xi[i] * xi[j]
    Hi = inv(Hm); Mm = [[sum(s[i] * s[j] for s in sg.values()) for j in range(k)] for i in range(k)]
    G = len(sg); cfac = G / (G - 1) if G > 1 else 1.0
    V = [[cfac * sum(Hi[i][a] * Mm[a][c] * Hi[c][j] for a in range(k) for c in range(k)) for j in range(k)] for i in range(k)]
    return b, [math.sqrt(max(V[i][i], 0)) for i in range(k)]


def ll(p, y):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return -(math.log(p) if y else math.log(1 - p))


# ------------------------------------------------------------------------------------------------------------- overview
def overview(events, Hs, Ds, pool):
    print(f"events with prices: {len(events)}; markets {sum(len(e['markets']) for e in events.values())}; base-rate pool {len(pool)} settled markets")
    nd = sum(1 for e in events.values() if e["part"] == "disc")
    print(f"discovery events {nd}, validation {len(events) - nd}")
    BR = BaseRates(pool)
    rows = []
    for e, ev in sorted(events.items(), key=lambda kv: kv[1]["first_close"]):
        T = detect_call(ev, Hs); Hp = precall_hour(ev, Hs)
        nprior = 0
        for m in ev["markets"]:
            nprior += BR.get(m["series"], m["w"], ev["open"] + 3600, e)["n"] > 0
        occ = sorted({m.get("occurrence_datetime") or "" for m in ev["markets"]})
        rows.append({"e": e, "part": ev["part"], "open": et(ev["open"]), "call_jump_end": et(T) if T else None, "pre": et(Hp) if Hp else None, "first_close": et(ev["first_close"]),
                     "occ": occ[0], "n": len(ev["markets"]), "yes": sum(m["y"] for m in ev["markets"]), "with_prior": nprior,
                     "vol": int(sum(m["vol"] for m in ev["markets"]))})
        r = rows[-1]
        print(f"  {r['part']} {e:38s} open {r['open']} jump {r['call_jump_end']} pre {r['pre']} close {r['first_close']} occ {r['occ'][:16]} n={r['n']:2d} yes={r['yes']:2d} prior={r['with_prior']:2d} vol={r['vol']}")
    return rows


if __name__ == "__main__":
    E, Hs, Ds, pool = load()
    what = sys.argv[1] if len(sys.argv) > 1 else "overview"
    if what == "overview":
        overview(E, Hs, Ds, pool)


# ----------------------------------------------------------------------------------------------------- step 1: calibration
def calibration(S):
    """y ~ logit(mid): intercept, slope (cluster SE by event); YES premium = mean(mid - y)."""
    X = [[1.0, logit(s["mid"])] for s in S]; y = [1 if s["y"] else 0 for s in S]; g = [s["e"] for s in S]
    b, se = fit_logit(X, y, g)
    return {"n": len(S), "events": len(set(g)), "intercept": round(b[0], 3), "intercept_se": round(se[0], 3), "slope": round(b[1], 3), "slope_se": round(se[1], 3),
            "yes_premium": round(st.mean(s["mid"] - s["y"] for s in S), 4), "yes_rate": round(st.mean(y), 3), "mean_mid": round(st.mean(s["mid"] for s in S), 3),
            "median_spread": round(st.median(s["ask"] - s["bid"] for s in S), 3),
            "brier_mid": round(st.mean((s["mid"] - s["y"]) ** 2 for s in S), 4), "logloss_mid": round(st.mean(ll(s["mid"], s["y"]) for s in S), 4)}


# ------------------------------------------------------------------------------------------------------ step 2: kill test
def killtest_one(S, nmin):
    T = [s for s in S if s["n"] >= nmin]
    if len(T) < 30:
        return {"n": len(T)}
    X = [[1.0, logit(s["mid"]), logit(s["pbr"])] for s in T]; y = [1 if s["y"] else 0 for s in T]; g = [s["e"] for s in T]
    b, se = fit_logit(X, y, g)
    X0 = [[1.0, logit(s["pbr"])] for s in T]; b0, se0 = fit_logit(X0, y, g)
    return {"n": len(T), "events": len(set(g)), "b_int": round(b[0], 3), "b_mid": round(b[1], 3), "b_br": round(b[2], 3), "se_br": round(se[2], 3),
            "z_br": round(b[2] / se[2], 2) if se[2] > 0 else None, "b_br_alone": round(b0[1], 3), "z_br_alone": round(b0[1] / se0[1], 2) if se0[1] > 0 else None,
            "brier_mid": round(st.mean((s["mid"] - s["y"]) ** 2 for s in T), 4), "brier_br": round(st.mean((s["pbr"] - s["y"]) ** 2 for s in T), 4),
            "brier_raw_k_over_n": round(st.mean((s["k"] / s["n"] - s["y"]) ** 2 for s in T), 4) if nmin >= 1 else None,
            "mean_n_prior": round(st.mean(s["n"] for s in T), 2)}


# --------------------------------------------------------------------------------------------------------- taker trades
def taker_trades(S, prob, edge, side_filter=None, fill="next"):
    """Buy YES if prob - ask - fee >= edge, NO if (1 - prob) - (1 - bid) - fee >= edge (decision quote at H).
    fill='next': at the next hourly quote (skip if missing); fill='same': at the decision quote."""
    out = []
    for s in S:
        p = prob(s)
        if p is None:
            continue
        a, b = s["ask"], s["bid"]
        fa, fb = (s["fask"], s["fbid"]) if fill == "next" else (a, b)
        for side in ("YES", "NO"):
            if side_filter and side != side_filter:
                continue
            px = a if side == "YES" else 1 - b
            pw = p if side == "YES" else 1 - p
            if pw - px - fee_pc(px) < edge:
                continue
            fpx = fa if side == "YES" else (1 - fb if fb is not None else None)
            if fpx is None or not (0.01 <= fpx <= 0.99):
                continue
            won = s["y"] if side == "YES" else not s["y"]
            out.append({"e": s["e"], "ret": ((1.0 if won else 0.0) - fpx - fee_pc(fpx)) / fpx, "won": won, "px": fpx, "tclose": s["tclose"],
                        "t": s["m"]["ticker"], "side": side, "p": p})
    return out


# ---------------------------------------------------------------------------------------------------------- maker trades
def maker_orders(events, Hs, Ds, BR, cancel="precall", side="sell_yes", post="first"):
    """Round-2 C1 (side='sell_yes', post='first', cancel='precall'): at the first top-of-hour >= open + 1 h, if
    0 < bid < ask < 1 and ask - bid >= 0.02, rest a SELL-YES (= buy NO at 1 - s) at s = ask - 0.01, one order per market.
    side='buy_yes' mirrors it (YES bid at bid + 0.01). post='tight': at the first hour (<= 49 h after open) whose spread
    is <= 0.10 (and >= 0.02). Cancel: 'precall' = one hour after the pre-call decision hour (15:00 ET for afternoon calls,
    07:00 ET for morning calls; the event start in round 2's sense), or 'firstclose' = when the first market of the event
    closes (round 2's literal trigger; for earnings mentions that comes AFTER the call).
    Fill: a print through or at the level after posting and before the cancel - hourly candles ending in (P, C], plus
    daily candles lying wholly inside (P, C]. 'through' = a print at least 1c beyond the level."""
    out = []
    for e, ev in events.items():
        Hp = precall_hour(ev, Hs)
        for m in ev["markets"]:
            P0 = ((m["open_ts"] + 3600 + 3599) // 3600) * 3600
            P = None; q = None
            if post == "first":
                P = P0; q = quote(Hs, m["ticker"], P)
                if q is None or q[0] - q[1] < 0.02:
                    continue
            else:
                for r in Hs.get(m["ticker"], []):
                    if P0 <= r[0] <= m["open_ts"] + 49 * 3600:
                        qq = quote(Hs, m["ticker"], r[0])
                        if qq and 0.02 <= qq[0] - qq[1] <= 0.10:
                            P, q = r[0], qq; break
                if P is None:
                    continue
            lvl = round(q[0] - 0.01, 2) if side == "sell_yes" else round(q[1] + 0.01, 2)
            if cancel == "precall":
                if Hp is None or Hp + 3600 <= P:
                    continue
                C = Hp + 3600
            else:
                C = ev["first_close"]
            first = None; through = False
            for src, ok in ((Hs.get(m["ticker"], []), lambda r: P < r[0] <= C), (Ds.get(m["ticker"], []), lambda r: r[0] - 86400 >= P and r[0] <= C)):
                for r in src:
                    if not ok(r):
                        continue
                    if side == "sell_yes":
                        hit = r[6] is not None and r[6] >= lvl - 1e-9; thr = hit and r[6] >= lvl + 0.01 - 1e-9
                    else:
                        hit = r[7] is not None and r[7] <= lvl + 1e-9; thr = hit and r[7] <= lvl - 0.01 + 1e-9
                    if hit:
                        first = r[0] if first is None else min(first, r[0]); through |= thr
            b = BR.get(m["series"], m["w"], P, e)
            if side == "sell_yes":
                won = not m["y"]; px = 1 - lvl
            else:
                won = m["y"]; px = lvl
            out.append({"e": e, "part": ev["part"], "t": m["ticker"], "P": P, "C": C, "s": lvl, "ask": q[0], "bid": q[1], "mid": (q[0] + q[1]) / 2,
                        "filled": first is not None, "through": through, "fill_ts": first, "y": m["y"], "tclose": ev["first_close"], "vol": m["vol"], **b,
                        "ret": ((1 - px) / px) if won else -1.0, "won": won, "px": px})
    return out


def maker_summary(O):
    F = [o for o in O if o["filled"]]
    U = [o for o in O if not o["filled"]]
    th = [o for o in F if o["through"]]
    return {"posted": len(O), "filled": len(F), "fill_rate": round(len(F) / len(O), 3) if O else None,
            "all_fills": cstats(F), "through_fills": cstats(th), "touch_only_fills": cstats([o for o in F if not o["through"]]),
            "unfilled_win_rate": round(st.mean(o["won"] for o in U), 3) if U else None,
            "filled_win_rate": round(st.mean(o["won"] for o in F), 3) if F else None,
            "all_posted_if_filled": cstats(O)}


# ------------------------------------------------------------------------------------------- frozen candidates / validation
FROZEN = {
    "C1_maker_no_listing": {"side": "sell_yes", "post": "first", "cancel": "precall", "max_level": None,
                            "rule": "round-2 C1 unchanged except the cancel trigger: at the first top-of-hour >= open + 1 h, if 0 < bid < ask < 1 and "
                                    "ask - bid >= 0.02, rest SELL-YES (= buy NO) at s = ask - 0.01, one order per market; cancel at the event start "
                                    "(15:00 ET on the call day for afternoon calls, 07:00 ET for morning calls). No fee (quadratic series)."},
    "MY1_maker_yes_low_precall": {"side": "buy_yes", "post": "first", "cancel": "precall", "max_level": 0.10,
                                  "rule": "mirror of C1 restricted to the listing placeholder book: at the first top-of-hour >= open + 1 h, if bid + 0.01 <= 0.10 and "
                                          "ask - bid >= 0.02, rest a YES bid at bid + 0.01; cancel at the event start as C1."},
    "MY2_maker_yes_low_hold": {"side": "buy_yes", "post": "first", "cancel": "firstclose", "max_level": 0.10,
                               "rule": "as MY1 but never cancelled (the order rests through the call until the event's markets close)."},
}


def frozen_orders(name, E, Hs, Ds, BR):
    f = FROZEN[name]
    O = maker_orders(E, Hs, Ds, BR, f["cancel"], f["side"], f["post"])
    if f["max_level"] is not None:
        O = [o for o in O if o["s"] <= f["max_level"] + 1e-9]
    return O


def run_frozen(part):
    E, Hs, Ds, pool = load(); BR = BaseRates(pool)
    res = {}
    for name in FROZEN:
        O = [o for o in frozen_orders(name, E, Hs, Ds, BR) if o["part"] == part]
        res[name] = maker_summary(O)
        days = (max(o["tclose"] for o in O) - min(o["tclose"] for o in O)) / 86400 if O else None
        res[name]["fills_per_day"] = round(res[name]["filled"] / days, 2) if days else None
        res[name]["fills"] = [{k: o[k] for k in ("t", "P", "C", "s", "fill_ts", "through", "won", "px", "ret")} for o in O if o["filled"]]
    return res


def write_disc_freeze():
    d = run_frozen("disc")
    out = {"written": "2026-10-08, rules frozen on discovery events (first 32 of 46 by first close: 2026-08-05..09-10)",
           "candidates": {k: {"spec": FROZEN[k], "discovery": {kk: vv for kk, vv in v.items() if kk != "fills"}} for k, v in d.items()}}
    (OUT / "frozen_on_discovery.json").write_text(json.dumps(out, indent=1, default=str))
    for k, v in d.items():
        print(k, json.dumps(v["all_fills"]), "fill_rate", v["fill_rate"])


def validate():
    v = run_frozen("val")
    (OUT / "validation.json").write_text(json.dumps(v, indent=1, default=str))
    for k, x in v.items():
        print(k, "posted", x["posted"], "fill_rate", x["fill_rate"], "\n   all", json.dumps(x["all_fills"]), "\n   through", json.dumps(x["through_fills"]),
              "\n   win filled", x["filled_win_rate"], "unfilled", x["unfilled_win_rate"], "fills/day", x["fills_per_day"])


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] in ("freeze", "validate"):
    write_disc_freeze() if sys.argv[1] == "freeze" else validate()


# ------------------------------------------------------------------------------------------------ discovery report
def tight_snapshots(E, Hs, BR, maxspr=0.10):
    """First hour (<= 49 h after open, >= open + 1 h) whose spread is <= maxspr: the first 'real' book."""
    out = []
    for e, ev in E.items():
        for m in ev["markets"]:
            H0 = ((m["open_ts"] + 3600 + 3599) // 3600) * 3600
            for r in Hs.get(m["ticker"], []):
                if r[0] < H0 or r[0] > m["open_ts"] + 49 * 3600:
                    continue
                q = quote(Hs, m["ticker"], r[0])
                if q and q[0] - q[1] <= maxspr:
                    qf = quote(Hs, m["ticker"], r[0] + 3600); b = BR.get(m["series"], m["w"], r[0], e)
                    out.append({"m": m, "e": e, "part": ev["part"], "H": r[0], "ask": q[0], "bid": q[1], "mid": (q[0] + q[1]) / 2,
                                "fask": qf[0] if qf else None, "fbid": qf[1] if qf else None, "y": m["y"], "tclose": ev["first_close"], **b})
                    break
    return out


def daily_bucket_snapshots(E, Hs, Ds, BR):
    rows = defaultdict(list)
    for e, ev in E.items():
        Hp = precall_hour(ev, Hs)
        if Hp is None:
            continue
        for m in ev["markets"]:
            for r in Ds.get(m["ticker"], []):
                T = r[0]
                if T < m["open_ts"] + 3600 or T >= Hp:
                    continue
                a, b = r[1], r[2]
                if a is None or b is None or not (0 < b < a < 1):
                    continue
                d = (Hp - T) / 86400
                bk = "0-1d" if d < 1 else "1-3d" if d < 3 else "3-7d" if d < 7 else "7-14d" if d < 14 else "14d+"
                br = BR.get(m["series"], m["w"], T, e)
                rows[(ev["part"], bk)].append({"e": e, "mid": (a + b) / 2, "ask": a, "bid": b, "y": m["y"], "H": T, "m": m, "tclose": ev["first_close"], **br})
    return rows


def discovery_report():
    E, Hs, Ds, pool = load(); BR = BaseRates(pool)
    rep = {"events": len(E), "disc_events": sum(1 for e in E.values() if e["part"] == "disc"),
           "disc_window": [et(min(e["first_close"] for e in E.values() if e["part"] == "disc")), et(max(e["first_close"] for e in E.values() if e["part"] == "disc"))],
           "val_window": [et(min(e["first_close"] for e in E.values() if e["part"] == "val")), et(max(e["first_close"] for e in E.values() if e["part"] == "val"))]}
    snaps = {"first_quote": snapshots(E, Hs, BR, "first"), "first_tight_quote": tight_snapshots(E, Hs, BR), "pre_call": snapshots(E, Hs, BR, "pre")}
    rep["calibration"] = {}; rep["killtest"] = {}
    for k, S in snaps.items():
        Sd = [s for s in S if s["part"] == "disc"]
        rep["calibration"][k] = calibration(Sd)
        rep["killtest"][k] = {f"nmin{n}": killtest_one(Sd, n) for n in (0, 1, 2, 3)}
    rows = daily_bucket_snapshots(E, Hs, Ds, BR)
    rep["by_horizon_daily_disc"] = {bk: {"calibration": calibration(rows[("disc", bk)]), "killtest_nmin1": killtest_one(rows[("disc", bk)], 1)}
                                    for bk in ("14d+", "7-14d", "3-7d", "1-3d", "0-1d") if rows[("disc", bk)]}
    Sd = [s for s in snaps["first_quote"] if s["part"] == "disc"]
    rep["taker_first_quote_disc"] = {f"nmin{n}_edge{e}": cstats(taker_trades([s for s in Sd if s["n"] >= n], lambda s: s["pbr"], e))
                                     for n in (1, 2, 3) for e in (0.10, 0.15, 0.20)}
    Sp = [s for s in snaps["pre_call"] if s["part"] == "disc"]
    bands = (0, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 1.0); bt = {}
    for lo, hi in zip(bands, bands[1:]):
        x = [s for s in Sp if lo <= s["mid"] < hi]
        if len(x) >= 5:
            bt[f"{lo:.2f}-{hi:.2f}"] = {"YES": cstats(taker_trades(x, lambda s: 1.0, -9, "YES")), "NO": cstats(taker_trades(x, lambda s: 0.0, -9, "NO"))}
    rep["taker_precall_bands_disc"] = bt
    mg = {}
    for side in ("sell_yes", "buy_yes"):
        for post in ("first", "tight"):
            for cancel in ("precall", "firstclose"):
                O = [o for o in maker_orders(E, Hs, Ds, BR, cancel, side, post) if o["part"] == "disc"]
                mg[f"{side}|{post}|{cancel}"] = {k: v for k, v in maker_summary(O).items()}
    rep["maker_grid_disc"] = mg
    (OUT / "discovery.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({"killtest": {k: {n: {kk: vv for kk, vv in v.items() if kk in ("n", "z_br", "b_br")} for n, v in d.items()} for k, d in rep["killtest"].items()}}, indent=0))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "discovery":
    discovery_report()
