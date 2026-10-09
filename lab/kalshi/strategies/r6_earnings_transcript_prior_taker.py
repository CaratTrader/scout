"""r6_earnings_transcript_prior_taker: does a company's own transcript history (word hits over its last 4-12 earnings
calls, counted by Kalshi's word rules from free public transcripts) predict Kalshi earnings-call mention markets better
than the T-24h mid, and does a taker on the side where prior - price >= 15c after fee earn >= +10%/$?

Data
  Kalshi (lab/kalshi/strategies/r6_earnings_transcript_prior_taker_data.py): settled markets and results from the r3
  caches; hourly book snapshots at T-24h from r3 W2 (all words, 46 live-tier events Aug-Oct 2026), r4 (one random word
  in each of 95 archive events) and this task's own archive sample (one random word in each of 162 more archive events,
  /historical/markets/{t}/candlesticks, chosen from listing fields only before any result was seen).
  Transcripts (r6_earnings_transcript_prior_taker_tx.py): Motley Fool free transcript pages, found through its public
  monthly sitemaps 2022-06..2026-10; only per-word counts are stored, never text.

No look-ahead
  * Call start = the start time printed in the Fool transcript header of that call (companies announce it weeks
    ahead; Kalshi's own occurrence_datetime carries it for live-tier markets and is used when Fool lacks the call).
    The event's close time is used only to tell WHICH call an event refers to (the first call on/after listing, before
    the bulk close), never for timing.
  * Decision D = top of the hour at or before call start - 24 h. Decision quote = hourly book snapshot at D (age <= 1 h).
    Fill = the next hourly snapshot (D + 1 h): YES at yes_ask, NO at 1 - yes_bid, only if it is no worse than the
    decision price + 3c (a limit order). Fee 0.07 p (1 - p) per contract, rounded up to the cent on a $5 order.
  * Prior = transcripts of calls dated >= 20 days before the target call (the previous calls), most recent N. Kalshi
    history = settled markets of the same series and word key in other events closed before D - 1 h.
Split: events ordered by call start; discovery = first 70%, validation = last 30%.

Result (2026-10-09, data/kalshi_lab/strategies/r6_earnings_transcript_prior_taker/result.json): DEAD. Kill test fired on
discovery: prior log loss 0.681 vs mid 0.559 (135 markets), every one of 14 prior variants loses to the mid, blend
weight on the prior's residual 0.11 +- 0.18. Counting reproduces Kalshi's settlement on the target calls 98.0% of the
time, so the counts are right; the T-24h book already prices word history. Validation (61 events, 572 markets):
test-plan rule +0.1%/$ (t -0.27), best discovery cell -4.7%/$.
Usage: python -m lab.kalshi.strategies.r6_earnings_transcript_prior_taker [fetch_tx|fetch_extra|build|killtest|grid|validate|all]"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r6_earnings_transcript_prior_taker_data as D  # noqa: E402
from lab.kalshi.strategies import r6_earnings_transcript_prior_taker_tx as T  # noqa: E402

OUT = D.OUT
ET = ZoneInfo("America/New_York")
STAKE = 5.0
SPLIT = 0.7
LIMIT_SLIP = 0.03
VARIANTS: list[str] = []


# ------------------------------------------------------------------------------------------------------------ fetches
def fetch_tx(only: list[str] | None = None) -> None:
    ms = D.load_markets()
    ev = D.events(ms)
    urls = T.all_urls()
    words, names = {}, {}
    for m in ms.values():
        words.setdefault(m["s"], set()).add(m["w"])
    for e in ev.values():
        if e["name"]:
            names.setdefault(e["s"], e["name"])
    for s in sorted(names):
        if only and s not in only:
            continue
        T.fetch_company(s, names[s], sorted(w for w in words[s] if w), urls, max_fetch=40)


def fetch_extra(max_calls: int = 170) -> None:
    """Archive sample: one random market per uncached archive event, hourly candles over [open - 1 h, min(close, open + 75 d) + 1 h]."""
    ms = D.load_markets()
    ev = D.events(ms)
    jobs = [(t, lo, min(hi, lo + 75 * 86400)) for t, lo, hi in D.plan_extra(ms, ev)]
    print(len(jobs), "planned", flush=True)
    D.fetch_extra(jobs, max_calls=max_calls)


# ------------------------------------------------------------------------------------------------------------ helpers
def fee_pc(p: float) -> float:
    n = max(1, int(STAKE / p))
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def clip(p: float, lo: float = 0.01) -> float:
    return min(max(p, lo), 1 - lo)


def ll(p: float, y: bool) -> float:
    p = clip(p)
    return -math.log(p if y else 1 - p)


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1 - p))


def sig(z: float) -> float:
    return 1 / (1 + math.exp(-max(min(z, 30), -30)))


def snap(rows: list, t: int, max_age: int = 3600):
    best = None
    for r in rows:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > max_age:
        return None
    return best


def call_start(date_s: str, mins: int | None) -> int | None:
    if not date_s or mins is None:
        return None
    d = dt.date.fromisoformat(date_s)
    return int(dt.datetime(d.year, d.month, d.day, mins // 60, mins % 60, tzinfo=ET).timestamp())


# ------------------------------------------------------------------------------------------------------------ build
def build(max_age_h: int = 1, tag: str = "") -> list[dict]:
    """One row per (event, word market) with a T-24h quote. Writes rows{tag}.jsonl and build_summary{tag}.json.
    max_age_h: how old the hourly book snapshot may be at D and at D + 1 h (1 = primary; hours without a candle had no
    book change, so 6 is reported as a sensitivity)."""
    ms = D.load_markets()
    ev = D.events(ms)
    q = D.load_quotes()
    txs = {}
    for s in {e["s"] for e in ev.values()}:
        by_date = {}
        for r in T.load_tx(s):
            if r.get("ok") and r.get("date") and r.get("n_words_company", 0) > 500:
                by_date.setdefault(r["date"], r)       # one transcript per call date
        txs[s] = sorted(by_date.values(), key=lambda r: r["date"])
    hist = defaultdict(list)                            # (series, word key) -> [(close, yes, event)]
    for m in ms.values():
        if m["y"] is not None:
            hist[(m["s"], m["wk"])].append((m["close"], m["y"], m["e"]))
    rows, summ = [], defaultdict(int)
    xcheck = []
    for e in sorted(ev.values(), key=lambda e: e["open"]):
        if not e["settled"]:
            continue
        tq = [t for t in e["tickers"] if t in q]
        if not tq:
            continue
        summ["events_with_quotes"] += 1
        od = dt.datetime.fromtimestamp(e["open"], ET).date()
        cd = dt.datetime.fromtimestamp(e["close1"], ET).date()
        tgt = [r for r in txs.get(e["s"], []) if max(od, cd - dt.timedelta(days=7)) <= dt.date.fromisoformat(r["date"]) <= cd]
        occ = min(e["occs"]) if e.get("occs") else None
        cs, src = None, None
        if tgt and call_start(tgt[-1]["date"], tgt[-1]["mins"]):
            cs, src = call_start(tgt[-1]["date"], tgt[-1]["mins"]), "fool"
            Dt = (cs - 24 * 3600) // 3600 * 3600
            if occ:
                xcheck.append(round((occ - cs) / 3600, 2))
        elif occ and e["close1"] - 30 * 3600 <= occ <= e["close1"] + 3600:
            # settled records carry occurrence_datetime ~ the END of the call (it tracks the bulk close), so the
            # decision is put 27 h before it: >= 24 h before the scheduled start for any call of <= 3 h.
            cs, src = int(occ) - 2 * 3600, "occ"
            Dt = (int(occ) - 27 * 3600) // 3600 * 3600
        if cs is None:
            summ["no_call_time"] += 1
            continue
        summ[f"call_src_{src}"] += 1
        prior = [r for r in txs.get(e["s"], []) if dt.date.fromisoformat(r["date"]) <= dt.datetime.fromtimestamp(cs, ET).date() - dt.timedelta(days=20)]
        prior = prior[::-1]                              # most recent first
        for t in tq:
            m = ms[t]
            if m["open"] and m["open"] > Dt - 3600:
                summ["listed_after_D"] += 1
                continue
            if m["close"] and m["close"] <= Dt + 3600:
                summ["closed_before_fill"] += 1
                continue
            qd = snap(q[t], Dt, max_age=max_age_h * 3600)
            qf = snap(q[t], Dt + 3600, max_age=max_age_h * 3600)
            if not qd:
                summ["no_decision_quote"] += 1
                continue
            alts, thr = T.word_spec(m["w"])
            cc = [r["counts"].get(m["w"]) for r in prior]
            cc = [c for c in cc if c is not None]
            kh = [(c, y) for c, y, e2 in hist[(m["s"], m["wk"])] if e2 != m["e"] and c <= Dt - 3600]
            vol_win = sum(r[3] or 0 for r in q[t] if Dt - 6 * 3600 < r[0] <= Dt + 6 * 3600)
            rows.append({"e": m["e"], "s": m["s"], "t": t, "w": m["w"], "thr": thr, "y": m["y"], "call": cs, "src": src, "D": Dt,
                         "day": dt.datetime.fromtimestamp(cs, ET).date().isoformat(),
                         "ask_d": qd[1], "bid_d": qd[2], "ask_f": qf[1] if qf else None, "bid_f": qf[2] if qf else None,
                         "fill_age_h": (Dt + 3600 - qf[0]) / 3600 if qf else None,
                         "n_tx": len(cc), "c": [c[0] for c in cc[:12]], "a": [c[1] for c in cc[:12]],
                         "tx_dates": [r["date"] for r in prior[:12]],
                         "k_hist": sum(y for _, y in kh), "n_hist": len(kh), "vol12h": vol_win, "vol_life": m["vol"],
                         "sample": "W2" if m["e"] in W2_EVENTS() else ("r4" if t in R4_TICKERS() else "extra")})
            summ["rows"] += 1
    summ["call_time_fool_minus_occ_h"] = sorted(xcheck)
    (OUT / f"rows{tag}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (OUT / f"build_summary{tag}.json").write_text(json.dumps(summ, indent=1, default=str))
    return rows


_W2, _R4 = [], []


def W2_EVENTS() -> set:
    if not _W2:
        _W2.append({json.loads(l)["e"] for l in (D.R3 / "candles_W2.jsonl").open()})
    return _W2[0]


def R4_TICKERS() -> set:
    if not _R4:
        _R4.append({json.loads(l)["t"] for l in (D.R4 / "candles.jsonl").open()})
    return _R4[0]


def load_rows(tag: str = "") -> list[dict]:
    return [json.loads(l) for l in (OUT / f"rows{tag}.jsonl").open()]


def split(rows: list[dict]) -> tuple[list[dict], list[dict], int]:
    ev = sorted({(r["call"], r["e"]) for r in rows})
    cut = ev[int(len(ev) * SPLIT)][0]
    return [r for r in rows if r["call"] < cut], [r for r in rows if r["call"] >= cut], cut


# ------------------------------------------------------------------------------------------------------------ priors
def prior(r: dict, N: int = 8, a: float = 2.0, p0: float = 0.5, mode: str = "hit", speech: str = "c", decay: float = 1.0) -> float | None:
    """Beta-binomial shrinkage of the word's hit rate over the last N transcribed calls toward p0.
    mode 'hit': k = calls with count >= thr; mode 'pois': P(X >= thr) with X ~ Poisson(mean count), shrunk the same way.
    mode 'hist': Kalshi's own settled history only; mode 'both': transcripts plus Kalshi history (history added as
    extra calls). decay < 1 weights the i-th most recent call by decay**i."""
    cs = r[speech][:N]
    thr = r["thr"]
    if mode == "hist":
        return (r["k_hist"] + a * p0) / (r["n_hist"] + a)
    if not cs and mode != "both":
        return None
    w = [decay ** i for i in range(len(cs))]
    if mode == "pois":
        if not cs:
            return None
        lam = sum(wi * c for wi, c in zip(w, cs)) / sum(w)
        pk = 1 - sum(math.exp(-lam) * lam ** j / math.factorial(j) for j in range(thr))
        n = sum(w)
        return (pk * n + a * p0) / (n + a)
    k = sum(wi for wi, c in zip(w, cs) if c >= thr)
    n = sum(w)
    if mode == "both":
        k += r["k_hist"]; n += r["n_hist"]
        if n == 0:
            return None
    return (k + a * p0) / (n + a)


def mid(r: dict) -> float | None:
    if r["ask_d"] is None or r["bid_d"] is None:
        return None
    return (r["ask_d"] + r["bid_d"]) / 2


# ------------------------------------------------------------------------------------------------------------ stats
def tstat(vals_by_cluster: dict) -> float:
    em = [st.mean(v) for v in vals_by_cluster.values()]
    if len(em) < 3 or st.pstdev(em) == 0:
        return float("nan")
    return st.mean(em) / (st.pstdev(em) / math.sqrt(len(em)))


def beta_lb(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact binomial (Clopper-Pearson) lower bound on the win rate."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, k / n
    for _ in range(60):
        p = (lo + hi) / 2
        tail = sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
        if tail > alpha:
            hi = p
        else:
            lo = p
    return lo


def trade_stats(tr: list[dict]) -> dict:
    if not tr:
        return {"n": 0}
    by_e, by_d = defaultdict(list), defaultdict(list)
    for x in tr:
        by_e[x["e"]].append(x["ret"]); by_d[x["day"]].append(x["ret"])
    ev_sum = sorted(((sum(v), k) for k, v in by_e.items()), reverse=True)
    top3 = {k for _, k in ev_sum[:3]}
    rest = [x["ret"] for x in tr if x["e"] not in top3]
    rs = sorted((x["ret"] for x in tr), reverse=True)
    tt = sorted(tr, key=lambda x: x["call"])
    h = len(tt) // 2
    wins = sum(x["won"] for x in tr)
    apx = st.mean(x["px"] for x in tr)
    afee = st.mean(fee_pc(x["px"]) for x in tr)
    lb = beta_lb(wins, len(tr))
    return {"n": len(tr), "events": len(by_e), "days": len(by_d), "win": round(wins / len(tr), 4), "avg_px": round(apx, 4),
            "ret_per_dollar": round(st.mean(x["ret"] for x in tr), 4), "t": round(tstat(by_e), 3), "t_day": round(tstat(by_d), 3),
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "ret_wo3_events": round(st.mean(rest), 4) if rest else None,
            "half1": round(st.mean(x["ret"] for x in tt[:h]), 4) if h else None,
            "half2": round(st.mean(x["ret"] for x in tt[h:]), 4) if len(tt) - h else None,
            "binomial_lb_ret": round((lb - apx - afee) / apx, 4),
            "yes_share": round(sum(x["side"] == "YES" for x in tr) / len(tr), 3),
            "median_vol12h": st.median(x["vol12h"] for x in tr), "median_vol_life": st.median(x["vol_life"] for x in tr)}


# ------------------------------------------------------------------------------------------------------------ kill test
def irls(X: list[list[float]], y: list[int], iters: int = 50, ridge: float = 1e-6) -> tuple[list[float], list[float]]:
    """Logistic regression by Newton-Raphson; returns (beta, standard errors)."""
    k = len(X[0])
    b = [0.0] * k
    H = None
    for _ in range(iters):
        g = [0.0] * k
        H = [[ridge if i == j else 0.0 for j in range(k)] for i in range(k)]
        for xi, yi in zip(X, y):
            p = sig(sum(bj * xj for bj, xj in zip(b, xi)))
            for i in range(k):
                g[i] += (yi - p) * xi[i]
                for j in range(k):
                    H[i][j] += p * (1 - p) * xi[i] * xi[j]
        step = solve(H, g)
        b = [bi + si for bi, si in zip(b, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    inv = invert(H)
    return b, [math.sqrt(max(inv[i][i], 0)) for i in range(k)]


def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[c][c] != 0:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def invert(A):
    n = len(A)
    return [list(col) for col in zip(*[solve(A, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)])]


def ll_compare(rows: list[dict], pf, label: str) -> dict:
    """Log loss of a prior vs the T-24h mid on rows where both exist; paired difference clustered by event."""
    xs = []
    for r in rows:
        p, m = pf(r), mid(r)
        if p is None or m is None:
            continue
        xs.append((r, p, m))
    if not xs:
        return {"label": label, "n": 0}
    d_by_e = defaultdict(list)
    for r, p, m in xs:
        d_by_e[r["e"]].append(ll(p, r["y"]) - ll(m, r["y"]))
    return {"label": label, "n": len(xs), "events": len(d_by_e), "yes_rate": round(st.mean(r["y"] for r, _, _ in xs), 3),
            "ll_prior": round(st.mean(ll(p, r["y"]) for r, p, _ in xs), 4), "ll_mid": round(st.mean(ll(m, r["y"]) for r, _, m in xs), 4),
            "brier_prior": round(st.mean((p - r["y"]) ** 2 for r, p, _ in xs), 4), "brier_mid": round(st.mean((m - r["y"]) ** 2 for r, _, m in xs), 4),
            "ll_diff_prior_minus_mid": round(st.mean(ll(p, r["y"]) - ll(m, r["y"]) for r, p, m in xs), 4),
            "t_diff_by_event": round(tstat(d_by_e), 3)}


def killtest(rows: list[dict] | None = None) -> dict:
    rows = rows or load_rows()
    disc, val, cut = split(rows)
    p0 = st.mean(r["y"] for r in disc)
    res = {"cut_call_ts": cut, "cut_et": dt.datetime.fromtimestamp(cut, ET).isoformat(), "p0_discovery": round(p0, 4),
           "discovery_rows": len(disc), "discovery_events": len({r["e"] for r in disc}),
           "validation_rows": len(val), "validation_events": len({r["e"] for r in val})}
    prim = lambda r: prior(r, 8, 2.0, p0)  # noqa: E731   pre-committed primary prior (N=8, a=2, company speech, hit share)
    res["PRIMARY"] = ll_compare(disc, prim, "PRIMARY N8 a2 hit company-speech")
    VARIANTS.append("kill:PRIMARY")
    desc = {}
    for N in (4, 8, 12):
        for a in (1.0, 2.0, 4.0):
            lab = f"N{N} a{a:g} hit"
            desc[lab] = ll_compare(disc, lambda r, N=N, a=a: prior(r, N, a, p0), lab); VARIANTS.append("kill:" + lab)
    for lab, f in [("N8 a2 pois", lambda r: prior(r, 8, 2.0, p0, "pois")), ("N8 a2 all-speech", lambda r: prior(r, 8, 2.0, p0, speech="a")),
                   ("N12 a2 decay0.8", lambda r: prior(r, 12, 2.0, p0, decay=0.8)), ("kalshi-history a2", lambda r: prior(r, 8, 2.0, p0, "hist")),
                   ("transcripts+history N8 a2", lambda r: prior(r, 8, 2.0, p0, "both"))]:
        desc[lab] = ll_compare(disc, f, lab); VARIANTS.append("kill:" + lab)
    res["descriptive_variants"] = desc
    # incremental information: y ~ 1 + logit(mid) + (logit(prior) - logit(mid))
    X, y = [], []
    for r in disc:
        p, m = prim(r), mid(r)
        if p is None or m is None:
            continue
        X.append([1.0, logit(m), logit(p) - logit(m)]); y.append(int(r["y"]))
    if len(X) > 20:
        b, se = irls(X, y)
        res["logit_blend_discovery"] = {"n": len(X), "b_const": round(b[0], 3), "b_logit_mid": round(b[1], 3), "se_mid": round(se[1], 3),
                                        "b_prior_minus_mid": round(b[2], 3), "se_prior_minus_mid": round(se[2], 3),
                                        "z_prior_minus_mid": round(b[2] / se[2], 2) if se[2] else None}
    VARIANTS.append("kill:logit_blend")
    # coverage of the priors
    res["n_tx_distribution_discovery"] = dict(sorted(defaultdict(int, {k: sum(1 for r in disc if min(r["n_tx"], 12) == k) for k in range(13)}).items()))
    pr = res["PRIMARY"]
    res["kill"] = bool(pr.get("n", 0) == 0 or pr["ll_prior"] >= pr["ll_mid"])
    (OUT / "killtest.json").write_text(json.dumps(res, indent=1))
    return res


# ------------------------------------------------------------------------------------------------------------ trading
def trades(rows: list[dict], pf, thr: float = 0.15, side: str = "both", pmin: float = 0.03, pmax: float = 0.97) -> list[dict]:
    out = []
    for r in rows:
        p = pf(r)
        if p is None:
            continue
        cand = []
        if side in ("both", "YES") and r["ask_d"] is not None and pmin <= r["ask_d"] <= pmax:
            e = p - r["ask_d"] - fee_pc(r["ask_d"])
            cand.append((e, "YES", r["ask_d"]))
        if side in ("both", "NO") and r["bid_d"] is not None and pmin <= 1 - r["bid_d"] <= pmax:
            px = 1 - r["bid_d"]
            e = (1 - p) - px - fee_pc(px)
            cand.append((e, "NO", px))
        if not cand:
            continue
        e, sd, px_d = max(cand)
        if e < thr:
            continue
        fx = (r["ask_f"] if sd == "YES" else (1 - r["bid_f"] if r["bid_f"] is not None else None))
        if fx is None or fx > px_d + LIMIT_SLIP or not (0.01 <= fx <= 0.99):
            continue
        won = r["y"] if sd == "YES" else not r["y"]
        out.append({"e": r["e"], "t": r["t"], "day": r["day"], "call": r["call"], "side": sd, "px": fx, "edge": round(e, 4), "prior": round(p, 4),
                    "won": won, "ret": ((1.0 if won else 0.0) - fx - fee_pc(fx)) / fx, "vol12h": r["vol12h"], "vol_life": r["vol_life"]})
    return out


def grid(rows: list[dict] | None = None) -> dict:
    rows = rows or load_rows()
    disc, _, _ = split(rows)
    p0 = st.mean(r["y"] for r in disc)
    priors = {"N8a2": lambda r: prior(r, 8, 2.0, p0), "N4a2": lambda r: prior(r, 4, 2.0, p0), "N12a2": lambda r: prior(r, 12, 2.0, p0),
              "both_N8a2": lambda r: prior(r, 8, 2.0, p0, "both")}
    cells = {}
    for pn, pf in priors.items():
        for thr in (0.10, 0.15, 0.20, 0.25):
            for side in ("both", "YES", "NO"):
                k = f"{pn}|thr{thr:.2f}|{side}"
                cells[k] = trade_stats(trades(disc, pf, thr, side)); VARIANTS.append("grid:" + k)
    (OUT / "grid_discovery.json").write_text(json.dumps(cells, indent=1))
    return cells


def summary_line(k: str, v: dict) -> str:
    if not v.get("n"):
        return f"{k:28s} n=0"
    return (f"{k:28s} n={v['n']:4d} ev={v['events']:4d} win={v['win']:.0%} px={v['avg_px']:.2f} ret={v['ret_per_dollar']:+.1%} "
            f"t={v['t']:5.2f} tday={v['t_day']:5.2f} wo3ev={v['ret_wo3_events'] if v['ret_wo3_events'] is None else round(v['ret_wo3_events'], 3)} "
            f"lb={v['binomial_lb_ret']:+.1%} yes={v['yes_share']:.0%}")


# ------------------------------------------------------------------------------------------------------------ checks
def count_check() -> dict:
    """How well do my transcript counts reproduce Kalshi's settlement on the TARGET call (diagnostic only; the target
    call is never used for a decision)? Agreement of (company-speech count >= thr) with the Kalshi result."""
    ms = D.load_markets()
    ev = D.events(ms)
    res = defaultdict(lambda: [0, 0])
    bad = []
    for e in ev.values():
        if not e["settled"]:
            continue
        txs = [r for r in T.load_tx(e["s"]) if r.get("ok") and r.get("date") and r.get("n_words_company", 0) > 500]
        od = dt.datetime.fromtimestamp(e["open"], ET).date(); cd = dt.datetime.fromtimestamp(e["close1"], ET).date()
        tgt = sorted((r for r in txs if max(od, cd - dt.timedelta(days=7)) <= dt.date.fromisoformat(r["date"]) <= cd), key=lambda r: r["date"])
        if not tgt:
            continue
        r = tgt[-1]
        for t in e["tickers"]:
            m = ms[t]
            c = r["counts"].get(m["w"])
            if c is None:
                continue
            _, thr = T.word_spec(m["w"])
            for key in ("all", r["fmt"]):
                res[key][1] += 1
                res[key][0] += (c[0] >= thr) == m["y"]
            res["allspeech"][1] += 1; res["allspeech"][0] += (c[1] >= thr) == m["y"]
            if (c[0] >= thr) != m["y"]:
                bad.append((t, m["w"], c, m["y"]))
    out = {k: {"agree": v[0], "n": v[1], "rate": round(v[0] / v[1], 4) if v[1] else None} for k, v in res.items()}
    out["kalshi_yes_but_count0"] = sum(1 for b in bad if b[3]); out["kalshi_no_but_counted"] = sum(1 for b in bad if not b[3])
    out["sample_mismatches"] = bad[:25]
    (OUT / "count_check.json").write_text(json.dumps(out, indent=1))
    return out


def validate(frozen: list[dict], rows: list[dict] | None = None) -> list[dict]:
    """Evaluate the frozen candidates ONCE on the validation events."""
    rows = rows or load_rows()
    disc, val, _ = split(rows)
    p0 = st.mean(r["y"] for r in disc)
    out = []
    for c in frozen:
        pf = lambda r, c=c: prior(r, c["N"], c["a"], p0, c.get("mode", "hit"))  # noqa: E731
        tr = trades(val, pf, c["thr"], c["side"])
        v = trade_stats(tr)
        days = (max(x["call"] for x in tr) - min(x["call"] for x in tr)) / 86400 if len(tr) > 1 else None
        v["rule"] = c["rule"]
        v["trades_per_day"] = round(len(tr) / days, 3) if days else None
        out.append(v)
        (OUT / f"trades_validation_{c['id']}.jsonl").write_text("\n".join(json.dumps(x) for x in tr) + "\n")
    (OUT / "validation.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "fetch_tx":
        fetch_tx(sys.argv[2:] or None)
    elif cmd == "fetch_extra":
        fetch_extra()
    elif cmd == "build":
        rs = build()
        print(len(rs), "rows;", json.loads((OUT / "build_summary.json").read_text()))
    elif cmd == "countcheck":
        print(json.dumps(count_check(), indent=1)[:3000])
    elif cmd == "killtest":
        print(json.dumps(killtest(), indent=1))
    elif cmd == "validate":
        fz = json.loads((OUT / "frozen_on_discovery.json").read_text())["frozen"]
        for v in validate(fz):
            print(summary_line(v["rule"][:3], v))
    elif cmd == "grid":
        for k, v in sorted(grid().items(), key=lambda kv: -(kv[1].get("t") or -9) if kv[1].get("n", 0) >= 20 else 9):
            print(summary_line(k, v))
