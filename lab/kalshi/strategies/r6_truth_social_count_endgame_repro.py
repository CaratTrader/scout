"""Adversarial reproduction of r6_truth_social_count_endgame (KXTRUTHSOCIAL weekly post-count brackets).

Written from the claim's text only (no code of the original family is imported or read).
Data: own fetches of the Roll Call factbase feed, the Kalshi KXTRUTHSOCIAL market list, and 1-minute candles of the
11 validation events (close-24h-31min .. window end+3min). Model: hour-of-day x day-of-week rate profile fitted on the
23 discovery weeks; count c = posts visible at t (deletions flagged at deleted_date) minus delta x posts since the last
03:00 ET flag run; remaining ~ NegBin(mean L*R*(1-delta), size phi); L = (1-w)(c+a)/(E+a) + w(c24+a)/(E24+a).
Rules (frozen by the claimant): C1 NO theta .10, C2 both theta .15, C3 YES theta .20; NO/YES price .03-.97; decisions
every 5 min from close-24h to the end of the count window (Sat 23:59 ET); first filled signal per bracket and side;
fill at the taker quote 1 minute later (calib.quote: <=30 min old); fee ceil-to-cent on a 10-contract order.

Usage: python -m lab.kalshi.strategies.r6_truth_social_count_endgame_repro meta|posts|candles|run
Outputs: data/kalshi_lab/strategies/r6_truth_social_count_endgame/repro/"""
from __future__ import annotations
import datetime as dt, hashlib, json, math, statistics as st, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote, cell_stats

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r6_truth_social_count_endgame/repro"
RAW = OUT / "raw"
ORIG = ROOT / "data/kalshi_lab/strategies/r6_truth_social_count_endgame"   # claimant's DATA caches (only for cross-checks)
RC = "https://rollcall.com/wp-json/factbase/v1/twitter?platform=truth+social&sort=date&sort_order=desc&page={}&format=json"
RULES = {"C1": ("NO", 0.10), "C2": (None, 0.15), "C3": ("YES", 0.20)}


# ----------------------------------------------------------------------------------------------------------- fetch
def kget(url: str) -> dict:
    from lab.us.data_refresh import fetch
    f = RAW / ("k_" + hashlib.sha1(url.encode()).hexdigest()[:20] + ".json")
    if f.exists():
        return json.loads(f.read_text())
    t = fetch(url, pace=1.15)
    with open(RAW / "calls.log", "a") as lg:
        lg.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {len(t)} {url}\n")
    f.write_text(t)
    return json.loads(t) if t else {}


def fetch_posts(min_date: str = "2026-02-01") -> None:
    d = RAW / "rollcall"; d.mkdir(parents=True, exist_ok=True)
    page = 1
    while True:
        f = d / f"p{page:04d}.json"
        if not f.exists():
            req = urllib.request.Request(RC.format(page), headers={"User-Agent": "Mozilla/5.0 (research; low-rate)", "Accept": "application/json"})
            for i in range(5):
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        f.write_text(r.read().decode()); break
                except Exception as e:
                    print("retry", page, e); time.sleep(5 * (i + 1))
            time.sleep(1.5)
        rows = json.loads(f.read_text())["data"]
        if not rows or rows[-1]["date"][:10] < min_date:
            break
        page += 1
    posts = {}
    for f in sorted(d.glob("p*.json")):
        for x in json.loads(f.read_text())["data"]:
            ts = dt.datetime.fromisoformat(x["date"]).timestamp()
            dd = x["social"].get("deleted_date")
            posts[x["id"]] = {"ts": int(ts), "del": bool(x.get("deleted_flag")),
                              "del_ts": int(dt.datetime.fromisoformat(dd).timestamp()) if dd else None}
    (OUT / "posts.json").write_text(json.dumps(posts))
    print("pages", page, "posts", len(posts))


def fetch_meta() -> None:
    """settled KXTRUTHSOCIAL markets: archive (before 2026-08-08) + live settled list (2 calls)"""
    from lab.us.data_refresh import K
    allm = []
    for base in (f"{K}/historical/markets?series_ticker=KXTRUTHSOCIAL&limit=1000", f"{K}/markets?series_ticker=KXTRUTHSOCIAL&status=settled&limit=1000"):
        cur = ""
        while True:
            d = kget(base + (f"&cursor={cur}" if cur else ""))
            allm += d.get("markets", [])
            cur = d.get("cursor") or ""
            if not cur or not d.get("markets"):
                break
    (OUT / "markets_raw.json").write_text(json.dumps(allm))
    print("markets", len(allm))


def parse_candles(cs: list) -> list[list]:
    def px(o):
        if not isinstance(o, dict):
            return None
        v = o.get("close_dollars", o.get("close"))
        if v is None:
            return None
        v = float(v)
        return v / 100 if v > 1.0 else v
    out = []
    for c in cs:
        vol = c.get("volume_fp", c.get("volume", 0)) or 0
        out.append([int(c["end_period_ts"]), px(c.get("yes_ask")), px(c.get("yes_bid")), None, None, float(vol)])
    return sorted(out)


def fetch_candles() -> None:
    from lab.us.data_refresh import K
    ev = events()
    val = [e for e in ev if e["split"] == "val"]
    cand = {}
    for e in val:
        a, b = e["close"] - 24 * 3600 - 31 * 60, e["wend"] + 180
        tick = [m["ticker"] for m in e["mk"]]
        if e["close"] < dt.datetime(2026, 8, 8, tzinfo=ET).timestamp():      # archive (settled before 2026-08-08)
            for t in tick:
                d = kget(f"{K}/historical/markets/{t}/candlesticks?start_ts={a}&end_ts={b}&period_interval=1")
                cand[t] = parse_candles(d.get("candlesticks", []))
        else:
            per = max(1, 9990 // ((b - a) // 60 + 1))
            for i in range(0, len(tick), per):
                d = kget(f"{K}/markets/candlesticks?market_tickers={','.join(tick[i:i + per])}&start_ts={a}&end_ts={b}&period_interval=1")
                for m in d.get("markets", []):
                    cand[m.get("market_ticker") or m.get("ticker")] = parse_candles(m.get("candlesticks", []))
    (OUT / "candles_val.json").write_text(json.dumps(cand))
    print({t: len(v) for t, v in cand.items()})


# ----------------------------------------------------------------------------------------------------------- data
def events() -> list[dict]:
    ms = json.loads((OUT / "markets_raw.json").read_text())
    g = defaultdict(list)
    for m in ms:
        g[m["event_ticker"]].append(m)
    ev = []
    for e, mk in g.items():
        sat = dt.datetime.strptime(e.split("-")[1], "%y%b%d").date()
        ws = dt.datetime.combine(sat - dt.timedelta(days=6), dt.time(0), ET)
        we = dt.datetime.combine(sat + dt.timedelta(days=1), dt.time(0), ET)
        close = int(dt.datetime.fromisoformat(mk[0]["close_time"].replace("Z", "+00:00")).timestamp())
        rec = int(dt.datetime.combine(sat + dt.timedelta(days=1), dt.time(10), ET).timestamp())
        ev.append({"e": e, "ws": int(ws.timestamp()), "wend": int(we.timestamp()), "close": close, "rec": rec, "mk": mk,
                   "exp_value": mk[0].get("expiration_value")})
    ev.sort(key=lambda x: x["close"])
    cut = int(len(ev) * 0.7)
    for i, x in enumerate(ev):
        x["split"] = "disc" if i < cut else "val"
    return ev


def in_bracket(m: dict, n: int) -> bool:
    s = m["strike_type"]
    if s == "between":
        return m["floor_strike"] <= n <= m["cap_strike"]
    if s == "less":
        return n < m["cap_strike"]
    if s == "greater":
        return n > m["floor_strike"]
    raise ValueError(s)


def visible(P: list[tuple], lo: int, t: int) -> int:
    """posts with lo <= ts <= t whose deletion had not been flagged by t"""
    return sum(1 for ts, dl, dts in P if lo <= ts <= t and not (dl and dts is not None and dts <= t))


# ----------------------------------------------------------------------------------------------------------- model
def fit(P: list[tuple], ev: list[dict]) -> dict:
    hod = [0.0] * 24; dow = [0.0] * 7; n = 0; nd = 0; minutes = 0
    for e in ev:
        if e["split"] != "disc":
            continue
        d0 = dt.datetime.fromtimestamp(e["ws"], ET).date()
        minutes += (e["wend"] - e["ws"]) // 60
        for ts, dl, dts in P:
            if e["ws"] <= ts < e["wend"]:
                lt = dt.datetime.fromtimestamp(ts, ET)
                hod[lt.hour] += 1; dow[(lt.date() - d0).days] += 1; n += 1
                nd += dl and dts is not None and dts <= e["rec"]
    return {"base_per_min": n / minutes, "hod": [h / (n / 24) for h in hod], "dow": [d / (n / 7) for d in dow],
            "delta": nd / n, "weekly_mean": n / sum(e["split"] == "disc" for e in ev)}


class Model:
    def __init__(self, prm: dict, a: float = 15.0, w: float = 0.3, phi: float = 1.0):
        self.p, self.a, self.w, self.phi = prm, a, w, phi
        self._cache = {}

    def weights(self, ws: int, we: int) -> list[float]:
        if (ws, we) not in self._cache:
            d0 = dt.datetime.fromtimestamp(ws, ET).date(); out = []
            for s in range(ws, we, 60):
                lt = dt.datetime.fromtimestamp(s, ET)
                out.append(self.p["base_per_min"] * self.p["hod"][lt.hour] * self.p["dow"][min(6, (lt.date() - d0).days)])
            cum = [0.0]
            for x in out:
                cum.append(cum[-1] + x)
            self._cache[(ws, we)] = cum
        return self._cache[(ws, we)]

    def expected(self, ws: int, we: int, a: int, b: int) -> float:
        """expected posts in [a, b) under the profile; outside the window use the matching weekday-agnostic rate"""
        cum = self.weights(ws, we)
        ia = max(0, min(len(cum) - 1, (a - ws) // 60)); ib = max(0, min(len(cum) - 1, (b - ws) // 60))
        extra = max(0, ws - a) / 60 * self.p["base_per_min"]
        return cum[ib] - cum[ia] + extra

    def probs(self, e: dict, P: list[tuple], t: int, lag: int = 0, rounding: str = "round") -> tuple[float, dict]:
        tk = t - lag
        vis = visible(P, e["ws"], tk)
        last3 = dt.datetime.combine(dt.datetime.fromtimestamp(tk, ET).date(), dt.time(3), ET).timestamp()
        if last3 > tk:
            last3 -= 86400
        recent = sum(1 for ts, _, _ in P if max(last3, e["ws"]) < ts <= tk)
        c = vis - self.p["delta"] * recent
        E = self.expected(e["ws"], e["wend"], e["ws"], tk)
        c24 = visible(P, tk - 86400, tk); E24 = self.expected(e["ws"], e["wend"], tk - 86400, tk)
        L = (1 - self.w) * (c + self.a) / (E + self.a) + self.w * (c24 + self.a) / (E24 + self.a)
        mu = L * self.expected(e["ws"], e["wend"], tk, e["wend"]) * (1 - self.p["delta"])
        ci = round(c) if rounding == "round" else math.floor(c)
        q = mu / (self.phi + mu); pp = self.phi / (self.phi + mu)

        def F(k):   # NB(size phi, mean mu) CDF at integer k; phi=1 -> geometric
            if k < 0:
                return 0.0
            if self.phi == 1.0:
                return 1 - q ** (k + 1)
            s, pk = 0.0, pp ** self.phi
            for j in range(k + 1):
                s += pk; pk *= q * (j + self.phi) / (j + 1)
            return min(1.0, s)
        out = {}
        for m in e["mk"]:
            s = m["strike_type"]
            if s == "between":
                pr = F(m["cap_strike"] - ci) - F(m["floor_strike"] - 1 - ci)
            elif s == "less":
                pr = F(m["cap_strike"] - 1 - ci)
            else:
                pr = 1 - F(m["floor_strike"] - ci)
            out[m["ticker"]] = max(0.0, min(1.0, pr))
        return c, {"mu": mu, "L": L, "probs": out}


# ----------------------------------------------------------------------------------------------------------- backtest
def fee10(p: float) -> float:
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def backtest(ev, P, model, cand, split="val", step=300, offset=0, lag=0, delay=60, start_h=24, rounding="round",
             recheck=False) -> dict[str, list[dict]]:
    rows = {k: [] for k in RULES}
    for e in ev:
        if e["split"] != split:
            continue
        won = {m["ticker"]: m["result"] == "yes" for m in e["mk"]}
        done = set()
        t = e["close"] - start_h * 3600 + offset
        while t < e["wend"]:
            _, info = model.probs(e, P, t, lag, rounding)
            for tk, pY in info["probs"].items():
                c = cand.get(tk)
                if not c:
                    continue
                q = quote(c, t)
                if not q:
                    continue
                ask, bid = q
                for side, px_dec, pm in (("YES", ask, pY), ("NO", 1 - bid, 1 - pY)):
                    for r, (sf, th) in RULES.items():
                        if (r, tk, side) in done or (sf and sf != side):
                            continue
                        if not (0.03 <= px_dec <= 0.97) or pm - px_dec < th:
                            continue
                        qf = quote(c, t + delay)
                        if not qf:
                            continue
                        px = qf[0] if side == "YES" else 1 - qf[1]
                        if not (0.01 <= px <= 0.99):
                            continue
                        if recheck and pm - px < th:
                            continue
                        w = won[tk] if side == "YES" else not won[tk]
                        vol30 = sum(x[5] for x in c if t < x[0] <= t + 1800)
                        rows[r].append({"e": e["e"], "t_close": e["close"], "tk": tk, "side": side, "t": t, "px": px,
                                        "px_dec": px_dec, "model": pm, "won": w, "ret": ((1.0 if w else 0.0) - px - fee10(px)) / px,
                                        "vol30": vol30, "mu": info["mu"]})
                        done.add((r, tk, side))
            t += step
    return rows


def beta_lb(k: int, n: int, alpha: float = 0.05) -> float:
    """one-sided exact (Clopper-Pearson) lower bound for a binomial proportion"""
    if k == 0:
        return 0.0
    def tail(p):   # P(X >= k | p)
        return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
    lo, hi = 0.0, k / n
    for _ in range(60):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    s = cell_stats(rows)
    mid = sorted(r["t_close"] for r in rows)[len(rows) // 2]
    h1 = [r["ret"] for r in rows if r["t_close"] < mid]; h2 = [r["ret"] for r in rows if r["t_close"] >= mid]
    k = sum(r["won"] for r in rows); n = len(rows); lb = beta_lb(k, n)
    apx = st.mean(r["px"] for r in rows)
    vs = sorted(r["vol30"] for r in rows)
    return {"n": n, "events": s["events"], "win": round(s["win"], 4), "avg_px": round(apx, 4), "ret_per_dollar": round(s["ret"], 4),
            "t": round(s["t"], 3), "ret_wo3": round(s["ret_wo3"], 4),
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None,
            "cp95_win_lb": round(lb, 4), "cp95_ret_lb": round((lb - apx - fee10(apx)) / apx, 4),
            "median_vol30_contracts": vs[len(vs) // 2], "median_ret": round(st.median(r["ret"] for r in rows), 4)}


def load_posts() -> list[tuple]:
    d = json.loads((OUT / "posts.json").read_text())
    return sorted((v["ts"], v["del"], v["del_ts"]) for v in d.values())


def run() -> None:
    ev = events(); P = load_posts()
    # 1. settlement check: final count (posts in window not deleted-flagged by the 10:00 ET recording) vs Kalshi result
    chk = []
    for e in ev:
        n = visible(P, e["ws"], e["rec"]) - visible(P, e["wend"], e["rec"])
        win = [m["ticker"] for m in e["mk"] if m["result"] == "yes"]
        ours = [m["ticker"] for m in e["mk"] if in_bracket(m, n)]
        allposts = sum(1 for ts, _, _ in P if e["ws"] <= ts < e["wend"])
        chk.append({"e": e["e"], "count": n, "all_incl_deleted": allposts, "kalshi_win": win, "ours": ours, "match": win == ours,
                    "expiration_value": e["exp_value"]})
    prm = fit(P, ev)
    frozen = json.loads((ORIG / "frozen_model.json").read_text())
    prm_frozen = {"base_per_min": frozen["base_per_min"], "hod": frozen["hod_factor_ET"],
                  "dow": frozen["dow_factor_from_window_start_Sun0"], "delta": frozen["delta"]}
    cand = json.loads((OUT / "candles_val.json").read_text())
    res = {"settlement_check": {"matched": sum(c["match"] for c in chk), "of": len(chk), "rows": chk},
           "split": {"disc": [e["e"] for e in ev if e["split"] == "disc"], "val": [e["e"] for e in ev if e["split"] == "val"]},
           "own_fit": {k: (v if not isinstance(v, list) else [round(x, 3) for x in v]) for k, v in prm.items()},
           "claimant_frozen_profile": {"delta": frozen["delta"], "base_per_min": frozen["base_per_min"], "weekly_mean": frozen["weekly_mean_disc"]},
           "validation": {}, "sensitivity": {}}
    M_own = Model(prm); M_fr = Model(prm_frozen)
    main = backtest(ev, P, M_own, cand)
    for r, rows in main.items():
        res["validation"][r] = summarize(rows)
    res["validation_trades_C1"] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()} for x in main["C1"]]
    res["validation_trades_C2"] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()} for x in main["C2"]]
    res["validation_trades_C3"] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()} for x in main["C3"]]
    sens = {"frozen_profile": dict(model=M_fr), "grid_offset_+1min": dict(model=M_own, offset=60),
            "lag_5min": dict(model=M_own, lag=300), "lag_15min": dict(model=M_own, lag=900),
            "fill_delay_2min": dict(model=M_own, delay=120), "recheck_edge_at_fill": dict(model=M_own, recheck=True),
            "floor_count": dict(model=M_own, rounding="floor"), "step_1min": dict(model=M_own, step=60),
            "step_15min": dict(model=M_own, step=900), "start_close-18h": dict(model=M_own, start_h=18),
            "phi_3": dict(model=Model(prm, phi=3.0)), "w_0_a_15": dict(model=Model(prm, w=0.0))}
    for name, kw in sens.items():
        m = kw.pop("model")
        rr = backtest(ev, P, m, cand, **kw)
        res["sensitivity"][name] = {r: summarize(v) for r, v in rr.items()}
    # 2. descriptive: model vs mid log loss on validation decision points (in-play brackets)
    ll = defaultdict(lambda: [0.0, 0.0, 0]); near = [0, 0]
    for e in ev:
        if e["split"] != "val":
            continue
        won = {m["ticker"]: m["result"] == "yes" for m in e["mk"]}
        t = e["close"] - 24 * 3600
        while t < e["wend"]:
            _, info = M_own.probs(e, P, t)
            for tk, pY in info["probs"].items():
                q = quote(cand.get(tk) or [], t)
                if not q:
                    continue
                mid = (q[0] + q[1]) / 2
                if not 0.02 <= mid <= 0.98:
                    continue
                y = won[tk]; cl = lambda p: min(max(p, 1e-4), 1 - 1e-4)
                ll[e["e"]][0] += -math.log(cl(pY) if y else 1 - cl(pY)); ll[e["e"]][1] += -math.log(cl(mid) if y else 1 - cl(mid)); ll[e["e"]][2] += 1
                near[0] += abs(mid - pY) <= 0.05; near[1] += 1
            t += 300
    diffs = [(a - b) / n for a, b, n in ll.values() if n]
    res["logloss_val"] = {"model": sum(v[0] for v in ll.values()) / sum(v[2] for v in ll.values()),
                          "mid": sum(v[1] for v in ll.values()) / sum(v[2] for v in ll.values()),
                          "t_by_event": st.mean(diffs) / (st.pstdev(diffs) / math.sqrt(len(diffs))), "share_within_5c": near[0] / near[1],
                          "n_points": near[1]}
    # 3. cross-check: discovery C1 on the claimant's cached discovery candles (their data, our code)
    try:
        cd = json.loads((ORIG / "candles_1m.json").read_text())
        dr = backtest(ev, P, M_own, cd, split="disc")
        res["discovery_on_claimant_candles"] = {r: summarize(v) for r, v in dr.items()}
        vr = backtest(ev, P, M_own, cd, split="val")
        res["validation_on_claimant_candles"] = {r: summarize(v) for r, v in vr.items()}
    except FileNotFoundError:
        pass
    (OUT / "repro_result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("validation", "logloss_val")}, indent=1))
    print("settle", res["settlement_check"]["matched"], "/", res["settlement_check"]["of"])
    for name, v in res["sensitivity"].items():
        print(f"{name:24s}", " | ".join(f"{r} n={x.get('n')} ret={x.get('ret_per_dollar')} t={x.get('t')} wo3={x.get('ret_wo3')}" for r, x in v.items()))
    for k in ("discovery_on_claimant_candles", "validation_on_claimant_candles"):
        if k in res:
            print(k, " | ".join(f"{r} n={x.get('n')} ret={x.get('ret_per_dollar')} t={x.get('t')}" for r, x in res[k].items()))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True); RAW.mkdir(parents=True, exist_ok=True)
    {"meta": fetch_meta, "posts": fetch_posts, "candles": fetch_candles, "run": run}[sys.argv[1]]()
