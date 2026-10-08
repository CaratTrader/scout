"""tennis_model: does a results-based Elo (built only from Kalshi's own settled tennis matches) know something the Kalshi
pre-match price does not?  And do in-play prices over-react after the pre-match favourite falls behind?

Data: data/kalshi_lab/strategies/tennis_model/results/*.jsonl (Kalshi settled tennis markets 2026-06-01..09-27, for the
Elo burn-in) + data/kalshi_lab/{markets,candles} (2026-09-27..10-08, 2601 events with 1-minute candles: the test set).
Split: test events ordered by close time, DISCOVERY = first 70%, VALIDATION = last 30%.
Execution: decide at t with data <= t (Elo uses only matches that closed before t); fill as taker one minute later.
Usage: python -m lab.kalshi.strategies.tennis_model [discovery|validate]"""
from __future__ import annotations
import json, math, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.tennis_model_data import (SERIES, OUT, load, split, quote, fill, trade_row, stats, fmt)

MEN = {"KXATPMATCH", "KXATPCHALLENGERMATCH", "KXITFMATCH"}
RES = OUT / "results"


def tier(series: str, tour: str) -> str:
    if series in ("KXATPMATCH", "KXWTAMATCH"):
        return "tour"
    if series in ("KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"):
        return "chall"
    m = re.search(r"\b[MW](\d+)\b", tour or "")
    return f"itf{m.group(1)}" if m else "itf"


def all_matches() -> list[dict]:
    """One row per settled event (archive + lab): close, pool, tier, winner, loser."""
    ev = defaultdict(dict)
    for s in SERIES:
        for src in (RES / f"{s}.jsonl", Path(f"data/kalshi_lab/markets/{s}.jsonl")):
            if not src.exists():
                continue
            for line in src.open():
                m = json.loads(line)
                if m["result"] not in ("yes", "no"):
                    continue
                d = ev[m["e"]]
                d.setdefault("mk", {})[m["t"]] = (m["sub"], m["result"] == "yes")
                d["close"] = m["close"]; d["series"] = s; d["e"] = m["e"]
                if m.get("tour"):
                    d["tour"] = m["tour"]
    out = []
    for d in ev.values():
        v = list(d["mk"].values())
        if len(v) != 2 or v[0][1] == v[1][1]:
            continue
        w = v[0][0] if v[0][1] else v[1][0]; l = v[1][0] if v[0][1] else v[0][0]
        out.append({"e": d["e"], "close": d["close"], "pool": "M" if d["series"] in MEN else "W", "tier": tier(d["series"], d.get("tour", "")),
                    "w": w, "l": l})
    out.sort(key=lambda r: r["close"])
    return out


INIT = {"tour": 1700, "chall": 1600, "itf100": 1580, "itf75": 1560, "itf50": 1540, "itf35": 1500, "itf25": 1480, "itf15": 1440, "itf": 1460}


class Elo:
    def __init__(self, kscale: float = 250, tier_init: bool = True):
        self.r = {}; self.n = defaultdict(int); self.kscale = kscale; self.tier_init = tier_init

    def get(self, pool: str, name: str, tr: str) -> float:
        k = (pool, name)
        if k not in self.r:
            self.r[k] = INIT.get(tr, 1500) if self.tier_init else 1500
        return self.r[k]

    def p(self, pool, a, b, tr) -> float:
        return 1 / (1 + 10 ** ((self.get(pool, b, tr) - self.get(pool, a, tr)) / 400))

    def update(self, pool, w, l, tr):
        pw = self.p(pool, w, l, tr)
        kw = self.kscale / (self.n[(pool, w)] + 5) ** 0.4; kl = self.kscale / (self.n[(pool, l)] + 5) ** 0.4
        self.r[(pool, w)] += kw * (1 - pw); self.r[(pool, l)] -= kl * (1 - pw)
        self.n[(pool, w)] += 1; self.n[(pool, l)] += 1


def mid_at(mk: dict, t: int):
    q = quote(mk["c"], t)
    return None if not q or q[1] <= 0 else ((q[0] + q[1]) / 2, q[0], q[1])


def prematch_rows(events: list[dict], dec_off_min: int = -360, kscale: float = 250, tier_init: bool = True) -> list[dict]:
    """For each test event: decision t = exp + dec_off_min; Elo as of t (matches closed before t); market mid at t."""
    M = all_matches()
    E = Elo(kscale, tier_init)
    pool_of = {e["e"]: ("M" if e["series"] in MEN else "W") for e in events}
    tier_of = {}
    for m in M:
        tier_of[m["e"]] = m["tier"]
    queries = sorted(((e["exp"] + dec_off_min * 60, e) for e in events), key=lambda x: x[0])
    rows = []; i = 0
    for t, e in queries:
        while i < len(M) and M[i]["close"] < t:
            m = M[i]; E.update(m["pool"], m["w"], m["l"], m["tier"]); i += 1
        if t + 60 >= e["close"]:
            continue
        mks = sorted(e["mk"].values(), key=lambda x: x["t"])
        A, B = mks
        qa, qb = mid_at(A, t), mid_at(B, t)
        if not qa or not qb:
            continue
        pool = pool_of[e["e"]]; tr = tier_of.get(e["e"], "itf")
        na, nb = E.n[(pool, A["name"])], E.n[(pool, B["name"])]
        pe = E.p(pool, A["name"], B["name"], tr)
        pm = (qa[0] + 1 - qb[0]) / 2
        rows.append({"e": e["e"], "ev": e, "t": t, "A": A, "B": B, "pm": pm, "pe": pe, "na": na, "nb": nb, "y": 1 if A["won"] else 0,
                     "spread_a": qa[1] - qa[2], "series": e["series"], "close": e["close"]})
    return rows


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def fit_logistic(X: list[list[float]], y: list[int], iters: int = 50, ridge: float = 1e-6):
    """Newton-Raphson logistic regression (no intercept unless a column of ones is given). Returns (beta, se)."""
    k = len(X[0]); b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = sum(bj * xj for bj, xj in zip(b, xi)); p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            for j in range(k):
                g[j] += (yi - p) * xi[j]
                for l in range(k):
                    H[j][l] += p * (1 - p) * xi[j] * xi[l]
        for j in range(k):
            H[j][j] += ridge
        step = solve(H, g)
        b = [bj + sj for bj, sj in zip(b, step)]
        if max(abs(s) for s in step) < 1e-9:
            break
    inv = invert(H)
    return b, [math.sqrt(max(inv[j][j], 0)) for j in range(k)]


def solve(A, v):
    n = len(v); M = [row[:] + [v[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c])); M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[c][c] != 0:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * b for a, b in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if M[i][i] else 0 for i in range(n)]


def invert(A):
    n = len(A)
    cols = [solve(A, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def logloss(rows, key):
    return st.mean(-(r["y"] * math.log(max(min(r[key], 1 - 1e-6), 1e-6)) + (1 - r["y"]) * math.log(max(min(1 - r[key], 1 - 1e-6), 1e-6))) for r in rows)


def model_trades(rows: list[dict], coef: list[float], theta: float, min_n: int = 0, side_mode: str = "both", max_spread: float = 0.05) -> list[dict]:
    """p_model = sigmoid(c0*logit(pm) + c1*logit(pe)); buy YES of the player whose model prob exceeds the taker price by theta."""
    out = []
    for r in rows:
        if min(r["na"], r["nb"]) < min_n:
            continue
        z = coef[0] * logit(r["pm"]) + coef[1] * logit(r["pe"])
        pA = 1 / (1 + math.exp(-z))
        e = r["ev"]; best = None
        for mk, pw in ((r["A"], pA), (r["B"], 1 - pA)):
            f = fill(mk, "YES", r["t"], e["close"])
            if not f:
                continue
            px, won = f
            q = quote(mk["c"], r["t"] + 60)
            if q[0] - q[1] > max_spread:
                continue
            edge = pw - px
            if edge >= theta and (best is None or edge > best[0]):
                best = (edge, mk, px, won, pw)
        if best:
            edge, mk, px, won, pw = best
            if side_mode == "fav" and px < 0.5 or side_mode == "dog" and px >= 0.5:
                continue
            out.append(trade_row(e, mk, "YES", px, won, r["t"], edge=edge, p_model=pw))
    return out


def load_early() -> dict:
    p = OUT / "early_candles.jsonl"
    out = {}
    for line in p.open():
        x = json.loads(line)
        out[x["t"]] = [r for r in x["c"] if r[1] is not None and r[2] is not None and r[2] > 0 and r[1] < 1]
    return out


def early_rows(events: list[dict], hrs_before_exp: float, kscale: float = 250, tier_init: bool = True, early: dict | None = None) -> list[dict]:
    """Decision at the last hourly candle ending at or before exp - hrs (quote age <= 60 min), Elo as of that time.
    Fill: taker at the NEXT hourly candle's quote (one hour later, strictly after the decision), or the lab 1-minute
    candles if that time falls inside the lab window. Market A = alphabetically first ticker (its book gives both sides)."""
    early = early if early is not None else load_early()
    M = all_matches(); E = Elo(kscale, tier_init)
    tier_of = {m["e"]: m["tier"] for m in M}
    Q = []
    for e in events:
        A = e["mk"][sorted(e["mk"])[0]]
        c = early.get(A["t"]) or []
        t_target = e["exp"] - int(hrs_before_exp * 3600)
        prior = [r for r in c if r[0] <= t_target]
        if not prior or t_target - prior[-1][0] > 3600:
            continue
        Q.append((prior[-1][0], e, A, prior[-1], c))
    Q.sort(key=lambda x: x[0])
    rows = []; i = 0
    for t, e, A, r0, c in Q:
        while i < len(M) and M[i]["close"] < t:
            m = M[i]; E.update(m["pool"], m["w"], m["l"], m["tier"]); i += 1
        B = [m for k, m in e["mk"].items() if k != A["t"]][0]
        pool = "M" if e["series"] in MEN else "W"; tr = tier_of.get(e["e"], "itf")
        pm = (r0[1] + r0[2]) / 2
        nxt = [r for r in c if t < r[0] <= t + 3600]
        fq = (nxt[0][1], nxt[0][2]) if nxt else None
        if fq is None:
            q = quote(A["c"], t + 3600)
            fq = (q[0], q[1]) if q else None
        rows.append({"e": e["e"], "ev": e, "t": t, "A": A, "B": B, "pm": pm, "spread": r0[1] - r0[2], "pe": E.p(pool, A["name"], B["name"], tr),
                     "na": E.n[(pool, A["name"])], "nb": E.n[(pool, B["name"])], "y": 1 if A["won"] else 0, "fq": fq,
                     "series": e["series"], "close": e["close"]})
    return rows


def early_trades(rows: list[dict], coef: list[float], theta: float, min_n: int = 0, max_spread: float = 0.06, px_lo: float = 0.0, px_hi: float = 1.0) -> list[dict]:
    """Model p_A = sigmoid(c0*logit(pm) + c1*logit(pe)); buy A YES (at ask) or A NO (at 1 - bid) one hour later when the
    model probability beats that taker price by theta. pm is the mid at decision time; the fill quote is not seen."""
    out = []
    for r in rows:
        if r["fq"] is None or min(r["na"], r["nb"]) < min_n or r["spread"] > max_spread:
            continue
        z = coef[0] * logit(r["pm"]) + coef[1] * logit(r["pe"]); pA = 1 / (1 + math.exp(-z))
        # decide the side on decision-time information only: the mid at t
        if pA - r["pm"] >= theta:
            side, pw = "YES", pA
        elif (1 - pA) - (1 - r["pm"]) >= theta:
            side, pw = "NO", 1 - pA
        else:
            continue
        ask, bid = r["fq"]
        px = ask if side == "YES" else 1 - bid
        if not (0.01 <= px <= 0.99) or not (px_lo <= px < px_hi):
            continue
        won = r["A"]["won"] if side == "YES" else not r["A"]["won"]
        out.append(trade_row(r["ev"], r["A"], side, px, won, r["t"], edge=pw - px, p_model=pw))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Frozen candidates (chosen on DISCOVERY only, 2026-10-08; see data/kalshi_lab/strategies/tennis_model/preregistration.json)
UPPER = ("KXATPCHALLENGERMATCH", "KXATPMATCH", "KXWTAMATCH", "KXWTACHALLENGERMATCH")
FROZEN = {
    "C1_fav_comeback_all": {"series": SERIES, "p0_min": 0.80, "level": 0.40},
    "C2_fav_comeback_upper": {"series": UPPER, "p0_min": 0.75, "level": 0.40},
    "C3_elo_early": {"hrs": 14, "theta": 0.20, "min_n": 10, "kscale": 250, "tier_init": True},
}


def prematch_fav(e: dict, early: dict, hrs: float = 8.0, max_spread: float = 0.06):
    """Pre-match favourite and its mid from the last hourly candle ending at or before exp - hrs (market A's book)."""
    A = e["mk"][sorted(e["mk"])[0]]; B = [m for k, m in e["mk"].items() if k != A["t"]][0]
    c = [r for r in early.get(A["t"], []) if r[0] <= e["exp"] - hrs * 3600]
    if not c or c[-1][1] - c[-1][2] > max_spread or e["exp"] - hrs * 3600 - c[-1][0] > 3600:
        return None
    pA = (c[-1][1] + c[-1][2]) / 2
    return (A, pA) if pA >= 0.5 else (B, 1 - pA)


def comeback_trades(events: list[dict], early: dict, series, p0_min: float, level: float) -> list[dict]:
    """Strong pre-match favourite (mid >= p0_min at exp-8h) whose in-play 1-minute mid first falls to <= level
    (bid > 0, spread <= 0.05): buy the favourite YES at the ask one minute later, hold to settlement."""
    out = []
    for e in events:
        if e["series"] not in series:
            continue
        pf = prematch_fav(e, early)
        if not pf or pf[1] < p0_min:
            continue
        F, p0 = pf
        tc = next((r[0] for r in F["c"] if r[2] > 0 and r[1] - r[2] <= 0.05 and (r[1] + r[2]) / 2 <= level), None)
        if tc is None:
            continue
        f = fill(F, "YES", tc, e["close"])
        if f:
            out.append(trade_row(e, F, "YES", f[0], f[1], tc, p0=p0))
    return out


def run_frozen(events: list[dict], early: dict | None = None) -> dict:
    early = early if early is not None else load_early()
    out = {}
    for name, cfg in FROZEN.items():
        if name.startswith("C3"):
            R = early_rows(events, cfg["hrs"], cfg["kscale"], cfg["tier_init"], early=early)
            rows = early_trades(R, [0, 1], cfg["theta"], cfg["min_n"])
        else:
            rows = comeback_trades(events, early, cfg["series"], cfg["p0_min"], cfg["level"])
        out[name] = rows
    return out


def main(mode: str = "discovery") -> None:
    E = load(); D, V, cut = split(E)
    early = load_early()
    sets = {"discovery": D, "validation": V}[mode]
    res = run_frozen(sets, early)
    for name, rows in res.items():
        print(fmt(f"{mode} {name}", stats(rows)))
    (OUT / f"frozen_{mode}.json").write_text(json.dumps({k: {"stats": stats(v), "trades": [{x: r[x] for x in ("e", "ticker", "side", "px", "won", "ret", "t", "close")} for r in v]}
                                                         for k, v in res.items()}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "discovery")
