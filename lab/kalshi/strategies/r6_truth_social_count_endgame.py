"""r6_truth_social_count_endgame - does KXTRUTHSOCIAL (weekly count of Trump's Truth Social posts, settled on Roll Call /
Factba.se) lag the publicly countable post tally, or price a naive count, in the last ~40 h before close?

Pipeline (stdlib only):
  events()      34 settled events (25 archive Feb-Aug 2026, 9 live tier Aug-Oct 2026) from the round-5 lead cache;
                observation window Sun 00:00 ET .. Sat 23:59 ET, close Sun 09:59 ET.
  posts         Roll Call feed (r6_truth_social_count_endgame_data.rollcall), deleted posts excluded at settlement.
                Reproduces 34/34 settled brackets (exact values: 3 of 4 within 4 posts, one off by 12 within its bracket).
  count_at(t)   what a poller sees at t: posts with ts <= t in the window, minus posts whose deletion Roll Call had
                already flagged by t (deleted_date <= t).
  model         remaining posts (t .. window end) ~ negative binomial: mean = level * sum of an hour-of-week rate
                profile over the remaining minutes; level = shrunk ratio of this week's observed count to the
                profile's expectation so far; dispersion fitted on discovery weeks. Plus a deletion haircut.
  backtest      decision grid every 5 min from close-40h; fill at the quote 1 min later (taker: YES at ask, NO at
                1 - bid), skip quotes older than 30 min; fee ceil-to-cent on a 10-lot; one trade per market (first
                signal); returns per $; t clustered by event.
Usage: python -m lab.kalshi.strategies.r6_truth_social_count_endgame [plan|fetch_disc|fetch_val|search|disc|val]"""
from __future__ import annotations
import bisect, datetime as dt, json, math, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_truth_social_count_endgame_data import (OUT, lead_markets, load_candles, CANDLES,
                                                                      fetch_archive_market, fetch_live_markets, calls_used)

ET = ZoneInfo("America/New_York")
ARCHIVE_CUTOFF = dt.datetime(2026, 8, 8, tzinfo=dt.timezone.utc).timestamp()
WIN_H = 40            # decision window starts at close - 40 h (Fri ~18:00 ET)
SPLIT = 0.7


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def events() -> list[dict]:
    ev = defaultdict(list)
    for m in lead_markets():
        ev[m["event_ticker"]].append(m)
    out = []
    for e, ms in ev.items():
        r = ms[0]["rules_secondary"]
        mm = re.search(r"begins at 12:00 AM ET on (\w+ \d+, \d+) and ends at 11:59 PM ET on (\w+ \d+, \d+)", r)
        a = dt.datetime.strptime(mm.group(1), "%b %d, %Y").replace(tzinfo=ET)
        b = (dt.datetime.strptime(mm.group(2), "%b %d, %Y") + dt.timedelta(days=1)).replace(tzinfo=ET)
        brs = []
        for m in ms:
            stype = m["strike_type"]; lo = m.get("floor_strike"); hi = m.get("cap_strike")
            if stype == "between":
                L, H = int(lo), int(hi)
            elif stype == "less":
                L, H = 0, int(hi) - 1
            elif stype == "greater":
                L, H = int(lo) + 1, 10 ** 6
            else:
                raise ValueError(stype)
            brs.append({"t": m["ticker"], "lo": L, "hi": H, "won": m["result"] == "yes", "vol": float(m.get("volume_fp") or 0),
                        "sub": m["yes_sub_title"]})
        brs.sort(key=lambda x: x["lo"])
        close = _ts(ms[0]["close_time"])
        out.append({"e": e, "A": int(a.timestamp()), "B": int(b.timestamp()), "open": _ts(ms[0]["open_time"]), "close": close,
                    "archive": close < ARCHIVE_CUTOFF, "brackets": brs, "exp_value": ms[0].get("expiration_value")})
    out.sort(key=lambda x: x["close"])
    k = int(len(out) * SPLIT)
    for i, x in enumerate(out):
        x["split"] = "disc" if i < k else "val"
    return out


class Posts:
    def __init__(self):
        P = json.loads((OUT / "posts.json").read_text())
        self.keep = sorted(p["ts"] for p in P.values() if not p["deleted"])
        self.all = sorted(p["ts"] for p in P.values())
        # deleted posts: (post ts, flag ts)
        self.dele = sorted((p["ts"], int(dt.datetime.fromisoformat(p["deleted_date"]).timestamp()) if p.get("deleted_date") else 10 ** 10)
                           for p in P.values() if p["deleted"])

    def final(self, A: int, B: int) -> int:
        return bisect.bisect_left(self.keep, B) - bisect.bisect_left(self.keep, A)

    def seen(self, A: int, t: int) -> int:
        """Count a poller sees at t: all posts in [A, t] minus deletions already flagged by t."""
        n = bisect.bisect_right(self.all, t) - bisect.bisect_left(self.all, A)
        n -= sum(1 for ts, fl in self.dele if A <= ts <= t and fl <= t)
        return n

    def unflagged_deleted(self, A: int, B: int, t: int) -> int:
        return sum(1 for ts, fl in self.dele if A <= ts < B and ts <= t < fl)


def plan(evs: list[dict], P: Posts, span: int = 90) -> dict:
    """Markets to fetch. Validation events: all brackets. Discovery archive events (one call per market): brackets that
    intersect [c0, c0 + span], c0 = count seen at close - 40 h (decision-time information only; discovery max of the
    remaining count was 74)."""
    out = {}
    for x in evs:
        t0 = x["close"] - WIN_H * 3600
        c0 = P.seen(x["A"], t0)
        if x["split"] == "val":
            sel = [b["t"] for b in x["brackets"]]
        else:
            sel = [b["t"] for b in x["brackets"] if b["hi"] >= c0 and b["lo"] <= c0 + span]
        out[x["e"]] = {"c0": c0, "tickers": sel, "start": t0 - 3600, "end": x["close"], "archive": x["archive"], "split": x["split"]}
    return out


def fetch(split: str) -> None:
    evs = events(); P = Posts(); pl = plan(evs, P)
    store = load_candles()
    for e, p in pl.items():
        if p["split"] != split:
            continue
        need = [t for t in p["tickers"] if t not in store]
        if not need:
            continue
        if p["archive"]:
            for t in need:
                fetch_archive_market(t, p["start"], p["end"], store)
        else:
            fetch_live_markets(need, p["start"], p["end"], store)
        CANDLES.write_text(json.dumps(store))
        print(e, len(need), "calls used", calls_used(), flush=True)
    CANDLES.write_text(json.dumps(store))



# ---------------------------------------------------------------- posting-rate model (fitted on discovery weeks only)
WEEK_MIN = 7 * 24 * 60


def nb_logpmf(k: int, mu: float, phi: float) -> float:
    mu = max(mu, 1e-6)
    return (math.lgamma(k + phi) - math.lgamma(phi) - math.lgamma(k + 1) + phi * math.log(phi / (phi + mu)) + k * math.log(mu / (phi + mu)))


def nb_cdf_table(mu: float, phi: float, kmax: int) -> list[float]:
    """cumulative P(R <= k) for k = 0..kmax"""
    out = []; s = 0.0
    for k in range(kmax + 1):
        s += math.exp(nb_logpmf(k, mu, phi)); out.append(min(s, 1.0))
    return out


class Model:
    """Remaining kept posts in (t, B] ~ NB(mean = L * profile_mass(t..B), size phi).
    profile: hour-of-day (ET) x day-of-week multiplicative rates in posts per minute, from discovery weeks.
    L = (c_kept_est + a) / (E_so_far + a) blended with the last-24h ratio by weight w."""

    def __init__(self, evs: list[dict], P: Posts, a: float = 30.0, w: float = 0.0, phi: float = 8.0):
        self.a, self.w, self.phi = a, w, phi
        disc = [x for x in evs if x["split"] == "disc"]
        hod = [0.0] * 24; dow = [0.0] * 7; n_all = 0; n_del = 0
        for x in disc:
            for ts in P.keep[bisect.bisect_left(P.keep, x["A"]):bisect.bisect_left(P.keep, x["B"])]:
                m = (ts - x["A"]) // 60; hod[(m // 60) % 24] += 1; dow[min(m // 1440, 6)] += 1
            n_all += bisect.bisect_left(P.all, x["B"]) - bisect.bisect_left(P.all, x["A"])
        n_w = len(disc); tot = sum(hod)
        self.delta = 1 - tot / n_all if n_all else 0.0
        hod = [h / tot * 24 for h in hod]; dow = [d / tot * 7 for d in dow]   # relative factors, mean 1
        base = tot / n_w / WEEK_MIN                                            # posts per minute, average week
        self.hod, self.dow, self.base = hod, dow, base
        self._build()
        self.weekly_mean = tot / n_w

    def _build(self) -> None:
        rate = [self.base * self.hod[(m // 60) % 24] * self.dow[m // 1440] for m in range(WEEK_MIN)]
        self.cum = [0.0]
        for r in rate:
            self.cum.append(self.cum[-1] + r)

    def export(self) -> dict:
        return {"a": self.a, "w": self.w, "phi": self.phi, "delta": self.delta, "base_per_min": self.base,
                "hod_factor_ET": self.hod, "dow_factor_from_window_start_Sun0": self.dow, "weekly_mean_disc": self.weekly_mean}

    @classmethod
    def from_frozen(cls, d: dict) -> "Model":
        m = cls.__new__(cls)
        m.a, m.w, m.phi, m.delta, m.base = d["a"], d["w"], d["phi"], d["delta"], d["base_per_min"]
        m.hod, m.dow, m.weekly_mean = d["hod_factor_ET"], d["dow_factor_from_window_start_Sun0"], d["weekly_mean_disc"]
        m._build()
        return m

    def mass(self, A: int, t0: int, t1: int) -> float:
        m0 = min(max((t0 - A) // 60, 0), WEEK_MIN); m1 = min(max((t1 - A) // 60, 0), WEEK_MIN)
        return self.cum[m1] - self.cum[m0]

    def kept_est(self, x: dict, P: Posts, t: int) -> float:
        seen = P.seen(x["A"], t); tv = t - getattr(P, "lag", 0)
        # posts since the last deletion-flag run (03:00 ET) may still be deleted: expected haircut delta per post
        lt = dt.datetime.fromtimestamp(t, ET); last = lt.replace(hour=3, minute=0, second=0, microsecond=0)
        if last > lt:
            last -= dt.timedelta(days=1)
        recent = max(0, bisect.bisect_right(P.all, tv) - bisect.bisect_left(P.all, max(int(last.timestamp()), x["A"])))
        return seen - self.delta * recent, seen

    def dist(self, x: dict, P: Posts, t: int) -> tuple[float, float, float]:
        """-> (kept count estimate, seen count, mean of remaining kept posts)"""
        c, seen = self.kept_est(x, P, t)
        E = self.mass(x["A"], x["A"], t)
        L = (c + self.a) / (E + self.a)
        if self.w > 0:
            t24 = max(x["A"], t - 86400)
            c24 = P.seen(x["A"], t) - P.seen(x["A"], t24)
            E24 = self.mass(x["A"], t24, t)
            L = (1 - self.w) * L + self.w * (c24 + self.a / 3) / (E24 + self.a / 3)
        mu = L * self.mass(x["A"], t, x["B"])
        return c, seen, mu

    def bracket_probs(self, x: dict, P: Posts, t: int) -> dict:
        c, seen, mu = self.dist(x, P, t)
        if t >= x["B"]:
            # window over: final = kept count; remaining uncertainty only from unflagged deletions
            base = int(round(c)); return {b["t"]: (1.0 if b["lo"] <= base <= b["hi"] else 0.0) for b in x["brackets"]}
        kmax = 400; cdf = nb_cdf_table(mu, self.phi, kmax); out = {}
        base = c
        for b in x["brackets"]:
            lo_r = math.ceil(b["lo"] - base - 1e-9); hi_r = math.floor(b["hi"] - base + 1e-9)
            if hi_r < 0:
                out[b["t"]] = 0.0; continue
            lo_r = max(lo_r, 0); hi_r = min(hi_r, kmax)
            p = cdf[hi_r] - (cdf[lo_r - 1] if lo_r > 0 else 0.0)
            out[b["t"]] = max(0.0, min(1.0, p if b["hi"] < 10 ** 6 else 1.0 - (cdf[lo_r - 1] if lo_r > 0 else 0.0)))
        return out


# ---------------------------------------------------------------- evaluation
from lab.kalshi.calib import quote  # noqa: E402  (yes_ask, yes_bid) from the last candle <= t if <= 30 min old

FROZEN = {"a": 15.0, "w": 0.3, "phi": 1.0}      # chosen on discovery (best NB log-likelihood region, 144 variants)


def fee10(p: float) -> float:
    """per-contract fee of a 10-contract taker order, Kalshi rounds the order fee up to the cent"""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def grid(x: dict, step: int = 300, start_h: float = WIN_H, end_before: int = 600) -> list[int]:
    t0 = x["close"] - int(start_h * 3600); t1 = x["close"] - end_before
    return list(range(t0, t1, step))


class LaggedPosts:
    """Posts as seen through a source latency of `lag` seconds (count at t uses posts with ts <= t - lag)."""
    def __init__(self, P: Posts, lag: int):
        self.P, self.lag = P, lag
        self.keep, self.all, self.dele = P.keep, P.all, P.dele

    def seen(self, A, t):
        return self.P.seen(A, t - self.lag)

    def final(self, A, B):
        return self.P.final(A, B)


def compare_ll(evs, P, M, C, split="disc", lag=120, only_pre_B=True):
    """Per-market binary log loss of the model vs the quote mid at a 5-min grid; and |mid - model| <= 5c share."""
    LP = LaggedPosts(P, lag); ll_m = ll_q = 0.0; n = 0; close5 = 0; per_ev = defaultdict(lambda: [0.0, 0.0, 0])
    for x in evs:
        if x["split"] != split:
            continue
        have = [b for b in x["brackets"] if b["t"] in C]
        if not have:
            continue
        for t in grid(x):
            if only_pre_B and t >= x["B"]:
                continue
            pr = M.bracket_probs(x, LP, t)
            for b in have:
                q = quote(C[b["t"]], t)
                if not q:
                    continue
                ask, bid = q; mid = (ask + bid) / 2
                pm = min(max(pr[b["t"]], 0.005), 0.995); pq = min(max(mid, 0.005), 0.995); y = b["won"]
                lm = -math.log(pm if y else 1 - pm); lq = -math.log(pq if y else 1 - pq)
                ll_m += lm; ll_q += lq; n += 1; close5 += abs(mid - pr[b["t"]]) <= 0.05
                e = per_ev[x["e"]]; e[0] += lm; e[1] += lq; e[2] += 1
    d = [(v[0] - v[1]) / v[2] for v in per_ev.values() if v[2]]
    t_ev = st.mean(d) / (st.pstdev(d) / math.sqrt(len(d))) if len(d) > 2 and st.pstdev(d) > 0 else float("nan")
    return {"n": n, "events": len(per_ev), "ll_model": ll_m / max(n, 1), "ll_mid": ll_q / max(n, 1),
            "share_within_5c": close5 / max(n, 1), "t_model_minus_mid_by_event": t_ev}


def signals(evs, P, M, C, split, theta=0.10, lag=120, start_h=WIN_H, phase="all", side_filter=None, step=300,
            px_lo=0.03, px_hi=0.97, delay=60):
    """First signal per market: buy the side whose model probability beats its taker price by >= theta.
    phase: 'pre' = before the window end (count still accruing), 'post' = after it, 'all'."""
    LP = LaggedPosts(P, lag); out = []
    for x in evs:
        if x["split"] != split:
            continue
        have = [b for b in x["brackets"] if b["t"] in C]; done = set()
        for t in grid(x, step=step, start_h=start_h):
            if phase == "pre" and t >= x["B"]:
                break
            if phase == "post" and t < x["B"]:
                continue
            pr = None
            for b in have:
                if b["t"] in done:
                    continue
                q = quote(C[b["t"]], t + delay)
                if not q:
                    continue
                ask, bid = q
                if pr is None:
                    pr = M.bracket_probs(x, LP, t)
                p = pr[b["t"]]
                for side, px, pw, won in (("YES", ask, p, b["won"]), ("NO", 1 - bid, 1 - p, not b["won"])):
                    if side_filter and side != side_filter:
                        continue
                    if not (px_lo <= px <= px_hi) or pw - px < theta:
                        continue
                    f = fee10(px); pnl = (1.0 if won else 0.0) - px - f
                    out.append({"e": x["e"], "t": b["t"], "side": side, "px": px, "model": round(pw, 4), "won": won,
                                "ret": pnl / px, "t_dec": t, "h_before_close": round((x["close"] - t) / 3600, 2),
                                "post_window": t >= x["B"], "t_close": x["close"]})
                    done.add(b["t"]); break
    return out


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; n_e = len(em)
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    srt = sorted(rows, key=lambda r: r["t_close"]); mid = srt[len(srt) // 2]["t_close"]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "avg_px": st.mean(r["px"] for r in rows),
            "ret_per_dollar": st.mean(r["ret"] for r in rows), "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan"),
            "half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan")}


def structural(evs, P, C, split, h_lo=2.0, h_hi=12.0, px_max=0.80, px_min=0.05, lag=120, step=300, delay=60, k_up=0):
    """Model-free rule: once per event, buy YES on the bracket k_up steps above the one containing the visible count
    (k_up=0: the current bracket) at the first decision time with h_lo <= hours to window end <= h_hi and
    px_min <= ask <= px_max."""
    LP = LaggedPosts(P, lag); out = []
    for x in evs:
        if x["split"] != split:
            continue
        brs = x["brackets"]
        for t in grid(x, step=step):
            hb = (x["B"] - t) / 3600
            if hb > h_hi:
                continue
            if hb < h_lo:
                break
            seen = LP.seen(x["A"], t)
            idx = [i for i, b in enumerate(brs) if b["lo"] <= seen <= b["hi"]]
            if not idx or idx[0] + k_up >= len(brs):
                break
            b = brs[idx[0] + k_up]
            if b["t"] not in C:
                continue
            q = quote(C[b["t"]], t + delay)
            if not q:
                continue
            ask = q[0]
            if px_min <= ask <= px_max:
                f = fee10(ask); pnl = (1.0 if b["won"] else 0.0) - ask - f
                out.append({"e": x["e"], "t": b["t"], "side": "YES", "px": ask, "won": b["won"], "ret": pnl / ask,
                            "t_dec": t, "h_before_close": round((x["close"] - t) / 3600, 2), "post_window": False, "t_close": x["close"]})
                break
    return out


# ---------------------------------------------------------------- frozen candidates (chosen on discovery, 2026-10-09 ~00:30 ET)
CANDIDATES = {
    "C1_model_NO_theta10_sat": {"theta": 0.10, "start_h": 24, "phase": "pre", "side_filter": "NO"},
    "C2_model_both_theta15_sat": {"theta": 0.15, "start_h": 24, "phase": "pre", "side_filter": None},
    "C3_model_YES_theta20_sat": {"theta": 0.20, "start_h": 24, "phase": "pre", "side_filter": "YES"},
}


def run(split: str) -> dict:
    evs = events(); P = Posts(); C = load_candles(); M = Model(evs, P, **FROZEN)
    out = {"split": split, "events": [x["e"] for x in evs if x["split"] == split],
           "loglik": compare_ll(evs, P, M, C, split, lag=120), "candidates": {}}
    for name, kw in CANDIDATES.items():
        rows = signals(evs, P, M, C, split, lag=120, **kw)
        out["candidates"][name] = {"rule": kw, "stats": stats(rows), "trades": rows}
    (OUT / f"{split}_result.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def discovery_search() -> dict:
    """Audit trail of the discovery search (all on discovery weeks only): 144 model fits (a x w x phi, NB log-likelihood of
    the remaining count at hourly points from close-40h), 72 signal-rule cells (theta x start x phase x side), 12
    model-free 'current bracket YES' cells, 1 post-window cell. Validation was run once on the 3 frozen CANDIDATES."""
    evs = events(); P0 = Posts(); P = LaggedPosts(P0, 120); C = load_candles(); out = {"model_fit": [], "signal_grid": [], "structural": []}
    disc = [x for x in evs if x["split"] == "disc"]
    for a in (5, 15, 30, 60, 120, 250):
        for w in (0, 0.3, 0.6):
            M = Model(evs, P0, a=a, w=w); pts = []
            for x in disc:
                fin = P0.final(x["A"], x["B"])
                for h in range(40, 0, -1):
                    t = x["close"] - h * 3600
                    if t >= x["B"]:
                        break
                    c, seen, mu = M.dist(x, P, t); pts.append((max(0, fin - round(c)), mu))
            for phi in (0.5, 0.75, 1, 1.5, 2, 3, 5, 8):
                out["model_fit"].append({"a": a, "w": w, "phi": phi, "mean_loglik": sum(nb_logpmf(R, mu, phi) for R, mu in pts) / len(pts)})
    M = Model(evs, P0, **FROZEN)
    for theta in (0.05, 0.10, 0.15, 0.20):
        for start_h in (40, 24, 14):
            for phase in ("pre", "all"):
                for side in (None, "YES", "NO"):
                    out["signal_grid"].append({"theta": theta, "start_h": start_h, "phase": phase, "side": side,
                                               **stats(signals(evs, P0, M, C, "disc", theta=theta, start_h=start_h, phase=phase, side_filter=side))})
    out["post_window"] = stats(signals(evs, P0, M, C, "disc", theta=0.05, start_h=40, phase="post"))
    for h_lo, h_hi in ((2, 12), (1, 6), (6, 18), (2, 24)):
        for pmax in (0.6, 0.8, 0.9):
            out["structural"].append({"h_lo": h_lo, "h_hi": h_hi, "px_max": pmax, **stats(structural(evs, P0, C, "disc", h_lo=h_lo, h_hi=h_hi, px_max=pmax))})
    (OUT / "discovery_search.json").write_text(json.dumps(out, indent=1, default=str))
    return out

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "plan":
        evs = events(); P = Posts(); pl = plan(evs, P)
        n = {"disc": 0, "val": 0}
        for e, p in pl.items():
            n[p["split"]] += len(p["tickers"]) if p["archive"] else 0
            print(e, p["split"], "archive" if p["archive"] else "live", "c0", p["c0"], len(p["tickers"]))
        print("archive per-market calls:", n, "calls used so far", calls_used())
    elif cmd == "fetch_disc":
        fetch("disc")
    elif cmd == "fetch_val":
        fetch("val")
    elif cmd == "search":
        r = discovery_search(); print(len(r["model_fit"]), len(r["signal_grid"]), len(r["structural"]))
    elif cmd in ("disc", "val"):
        r = run(cmd)
        print(json.dumps({"loglik": r["loglik"], **{k: v["stats"] for k, v in r["candidates"].items()}}, indent=1, default=str))
