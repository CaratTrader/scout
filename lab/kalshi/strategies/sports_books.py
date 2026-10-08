"""sports_books: is Kalshi's pre-game game-winner price wrong relative to the de-vigged sportsbook line?

Hypothesis. Kalshi sports game-winner markets (MLB, NFL, NBA, NHL, NCAAF, UEFA Nations League) are priced by Kalshi
market makers and retail flow. If the Kalshi price just before the scheduled start sits far from the de-vigged
sportsbook consensus (DraftKings, the one book ESPN's API keeps), the cheaper Kalshi side may be underpriced.

Data. Kalshi settled markets + 1-minute candles (data/kalshi_lab/{markets,candles}/<SERIES>.jsonl). ESPN scoreboards
(scheduled start, teams) and the ESPN core odds endpoint (DraftKings moneyline "open" and "close" for completed games),
cached by lab/kalshi/strategies/sports_books_espn.py under data/kalshi_lab/strategies/sports_books/espn/.

Decision. At t = ESPN scheduled start - L minutes, read the book line and the Kalshi quote at t+1 min (taker: YES at
yes_ask, NO at 1 - yes_bid; quote <= 30 min old). Edge = fair - price - fee. One trade per event: the largest edge
across that event's markets and sides, if it exceeds the threshold. Look-ahead caveat: ESPN only keeps the *closing*
DraftKings line (taken at the actual start), so L must be small (minutes) for the backtest to mimic a live poll of the
current line; the "open" line has no look-ahead and is tested as a control at any L.

Result (2026-10-08): DEAD. Kalshi's pre-game mid sits within ~0.5-1.5c of the de-vigged DraftKings close (spread 1c) and
is as well calibrated; signals fire almost only in NCAAF/UEFA and the three frozen candidates gave -9%, -15% and a
lottery +41% (t 0.78, -36% without its 3 best trades) on validation. See data/kalshi_lab/strategies/sports_books/result.json.

Usage: python -m lab.kalshi.strategies.sports_books [match|disc|val]"""
from __future__ import annotations
import json, math, re, statistics as stt, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote, cell_stats

OUT = Path("data/kalshi_lab/strategies/sports_books")
MK = Path("data/kalshi_lab/markets"); CD = Path("data/kalshi_lab/candles")
SERIES = ["KXMLBGAME", "KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXNCAAFGAME", "KXUEFANLGAME"]
STAKE = 5.0        # $ per trade, for the per-order fee rounding


def fee_c(p: float, mult: float = 1.0) -> float:
    """Per-contract taker fee with Kalshi's round-up to the cent per order, for a ~$5 order."""
    n = max(1, int(STAKE / p))
    return math.ceil(0.07 * mult * n * p * (1 - p) * 100 - 1e-9) / 100 / n


def norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ").replace(".", "").replace("'", "").replace("-", " ")
    s = re.sub(r"\bst\b", "state", s)
    return re.sub(r"\s+", " ", s).strip()


def load_kalshi(series: str) -> tuple[dict, dict]:
    ev = defaultdict(list)
    for l in (MK / f"{series}.jsonl").open():
        m = json.loads(l); ev[m["e"]].append(m)
    cd = {}
    for l in (CD / f"{series}.jsonl").open():
        x = json.loads(l); cd[x["t"]] = x["c"]
    return ev, cd


ALIAS = {"CWS": "CHW"}


def team_score(m: dict, t: dict) -> int:
    """3 = ticker code equals the ESPN abbreviation, 2 = subtitle equals a team name, 1 = subtitle is a name prefix."""
    code = m["t"].rsplit("-", 1)[1].upper()
    if t.get("abbr") and ALIAS.get(code, code) == t["abbr"].upper():
        return 3
    sub = norm(m.get("sub")).replace("university at ", "")
    names = {norm(n) for n in t["names"]}
    if sub in names:
        return 2
    return 1 if len(sub) >= 4 and any(n.startswith(sub) for n in names) else 0


def assign(teams_k: list[dict], ev: dict) -> dict | None:
    """Kalshi team market ticker -> ESPN home/away, best one-to-one assignment (None if any team is unmatched)."""
    if len(teams_k) != 2 or len(ev["teams"]) != 2:
        return None
    a, b = ev["teams"]
    s1 = team_score(teams_k[0], a) + team_score(teams_k[1], b) if team_score(teams_k[0], a) and team_score(teams_k[1], b) else 0
    s2 = team_score(teams_k[0], b) + team_score(teams_k[1], a) if team_score(teams_k[0], b) and team_score(teams_k[1], a) else 0
    if s1 == 0 and s2 == 0 or s1 == s2:
        return None
    x, y = (a, b) if s1 > s2 else (b, a)
    return {teams_k[0]["t"]: x["ha"], teams_k[1]["t"]: y["ha"]}


def is_tie(m: dict) -> bool:
    return m["t"].endswith("-TIE") or (m.get("sub") or "").lower() == "tie"


def match_events(series: str, espn: list[dict]) -> dict:
    """Kalshi event ticker -> ESPN event (with 'map': ticker -> home/away/draw). Kalshi's expected expiration is a placeholder
    for some games (TBD kick-offs), so the ESPN start may lie 10 h before to 14 h after it; the nearest full match wins."""
    kev, _ = load_kalshi(series)
    out = {}
    for e, ms in kev.items():
        teams = [m for m in ms if not is_tie(m)]
        exp = ms[0]["exp"]
        best = None
        for x in espn:
            if not (exp - 10 * 3600 <= x["start"] <= exp + 14 * 3600):
                continue
            mp = assign(teams, x)
            if mp:
                d = abs(exp - 3 * 3600 - x["start"])
                if best is None or d < best[0]:
                    best = (d, x, mp)
        if best:
            ev = dict(best[1]); ev["map"] = dict(best[2])
            for m in ms:
                if is_tie(m):
                    ev["map"][m["t"]] = "draw"
            out[e] = ev
    return out


def american(o) -> float | None:
    try:
        o = float(str(o).replace("+", ""))
    except Exception:
        return None
    if o == 0:
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def book_line(series: str, ev: dict, which: str = "close") -> dict | None:
    """{'home': p, 'away': p, 'draw': p} raw implied probabilities (with vig) from DraftKings via ESPN core odds."""
    f = OUT / "espn" / "odds" / series / f"{ev['id']}.json"
    if not f.exists():
        return None
    j = json.loads(f.read_text())
    if not j or not j.get("items"):
        return None
    it = j["items"][0]
    out = {}
    for side, key in (("home", "homeTeamOdds"), ("away", "awayTeamOdds")):
        o = it.get(key) or {}
        ml = ((o.get(which) or {}).get("moneyLine") or {}).get("american")
        if which == "close" and ml is None:
            ml = o.get("moneyLine")
        p = american(ml)
        if p is None:
            return None
        out[side] = p
    if series == "KXUEFANLGAME":
        d = it.get("drawOdds") or {}
        ml = ((d.get(which) or {}).get("moneyLine") or {}).get("american") if isinstance(d.get(which), dict) else None
        if ml is None and which == "close":
            ml = d.get("moneyLine")
        p = american(ml)
        if p is None:
            return None
        out["draw"] = p
    return out


def devig(raw: dict, method: str = "mult") -> dict:
    s = sum(raw.values())
    if method == "mult":
        return {k: v / s for k, v in raw.items()}
    if method == "power":   # p_i = raw_i^k with sum 1 (shades longshots more)
        lo, hi = 1.0, 3.0
        for _ in range(60):
            k = (lo + hi) / 2
            if sum(v ** k for v in raw.values()) > 1:
                lo = k
            else:
                hi = k
        return {k_: v ** ((lo + hi) / 2) for k_, v in raw.items()}
    raise ValueError(method)


def side_of(m: dict, ev: dict) -> str | None:
    return ev["map"].get(m["t"])


def build() -> list[dict]:
    """One row per Kalshi market with its ESPN start, book lines and Kalshi quotes at a grid of lead times."""
    allev = json.loads((OUT / "espn_events.json").read_text())
    rows = []
    for s in SERIES:
        kev, cd = load_kalshi(s)
        pairs = match_events(s, allev[s])
        for e, ms in kev.items():
            ev = pairs.get(e)
            if not ev:
                continue
            close = book_line(s, ev, "close"); opn = book_line(s, ev, "open")
            for m in ms:
                sd = side_of(m, ev)
                if sd is None:
                    continue
                c = cd.get(m["t"]) or []
                q = {}
                for L in (2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 150):
                    t = ev["start"] - L * 60
                    if t + 60 >= m["close"]:
                        continue    # the market had already closed (would leak the result)
                    qq = quote(c, t + 60)
                    if qq:
                        q[L] = qq
                rows.append({"s": s, "e": e, "t": m["t"], "side": sd, "start": ev["start"], "close": m["close"], "won": m["result"] == "yes",
                             "book_close": close, "book_open": opn, "q": q, "vol": m["vol"]})
    return rows


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "match"
    if what == "match":
        allev = json.loads((OUT / "espn_events.json").read_text())
        for s in SERIES:
            kev, _ = load_kalshi(s)
            pairs = match_events(s, allev[s])
            bad = 0
            for e, ev in pairs.items():
                for m in kev[e]:
                    sd = side_of(m, ev)
                    if sd in ("home", "away"):
                        w = [t for t in ev["teams"] if t["ha"] == sd][0]["winner"]
                        if w is not None and bool(w) != (m["result"] == "yes"):
                            bad += 1
            print(s, "kalshi events", len(kev), "matched", len(pairs), "winner mismatches", bad,
                  "start-exp offsets (h):", sorted({round((ev["start"] - kev[e][0]["exp"]) / 3600, 1) for e, ev in pairs.items()})[:12])
            un = [e for e in kev if e not in pairs][:8]
            print("   unmatched e.g.", un, [m["sub"] for e in un[:4] for m in kev[e]])


# ----------------------------------------------------------------------------------------------------------- analysis
def fair_of(r: dict, line: str, method: str) -> float | None:
    raw = r["book_close"] if line == "close" else r["book_open"]
    if not raw or r["side"] not in raw:
        return None
    return devig(raw, method)[r["side"]]


def event_trades(rows: list[dict], L: int, line: str = "close", method: str = "mult", X: float = 0.02, xmode: str = "prob",
                 sports: tuple | None = None, side: str = "any", fee_mult: dict | None = None, pmin: float = 0.03, pmax: float = 0.97) -> list[dict]:
    """At most one taker trade per event: the market/side with the largest edge (fair - price - fee) at lead time L,
    traded if edge >= X (xmode 'prob') or edge/price >= X (xmode 'ret')."""
    ev = defaultdict(list)
    for r in rows:
        if sports and r["s"] not in sports:
            continue
        ev[r["e"]].append(r)
    out = []
    for e, rs in ev.items():
        best = None
        for r in rs:
            p = fair_of(r, line, method)
            q = r["q"].get(L) or r["q"].get(str(L))
            if p is None or not q:
                continue
            ask, bid = q
            fm = (fee_mult or {}).get(r["s"], 1.0)
            for sd, px, fair, won in (("YES", ask, p, r["won"]), ("NO", 1 - bid, 1 - p, not r["won"])):
                if not (pmin <= px <= pmax):
                    continue
                if side == "dog" and fair >= 0.5 or side == "fav" and fair < 0.5:
                    continue
                f = fee_c(px, fm)
                edge = fair - px - f
                score = edge if xmode == "prob" else edge / px
                if best is None or score > best[0]:
                    best = (score, {"s": r["s"], "e": e, "t": r["t"], "t_close": r["close"], "start": r["start"], "side": sd, "px": px, "fair": fair,
                                    "edge": edge, "won": won, "ret": ((1.0 if won else 0.0) - px - f) / px, "vol": r["vol"]})
        if best and best[0] >= X:
            out.append(best[1])
    return out


def split_cut(rows: list[dict], frac: float = 0.7) -> int:
    closes = sorted({max(r["close"] for r in rows if r["e"] == e) for e in {r["e"] for r in rows}})
    return closes[int(len(closes) * frac)]


def stats(tr: list[dict]) -> dict:
    if len(tr) < 3:
        return {"n": len(tr)}
    s = cell_stats(tr)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}


SP = {"all": None, "mlb": ("KXMLBGAME",), "nfl": ("KXNFLGAME",), "nba": ("KXNBAGAME",), "nhl": ("KXNHLGAME",), "ncaaf": ("KXNCAAFGAME",),
      "uefa": ("KXUEFANLGAME",)}


def grid() -> list[dict]:
    """Every variant examined on discovery (counted in variants_examined)."""
    g = []
    for L in (2, 5, 10, 15):
        for method in ("mult", "power"):
            for X in (0.0, 0.01, 0.02, 0.03, 0.05):
                for sp in SP:
                    g.append({"line": "close", "L": L, "method": method, "X": X, "sp": sp, "side": "any"})
                for side in ("dog", "fav"):
                    g.append({"line": "close", "L": L, "method": method, "X": X, "sp": "all", "side": side})
    for L in (10, 60, 120):     # control: the opening line (no look-ahead, stale)
        for X in (0.0, 0.02, 0.05):
            g.append({"line": "open", "L": L, "method": "mult", "X": X, "sp": "all", "side": "any"})
    return g


def run_variant(rows: list[dict], v: dict) -> list[dict]:
    return event_trades(rows, v["L"], v["line"], v["method"], v["X"], "prob", SP[v["sp"]], v["side"])


def vname(v: dict) -> str:
    return f"{v['line']} L={v['L']} {v['method']} X>={v['X']:.2f} {v['sp']} {v['side']}"


def full_stats(tr: list[dict], t_lo: int, t_hi: int) -> dict:
    s = stats(tr)
    mid = (t_lo + t_hi) / 2
    h1 = [r for r in tr if r["t_close"] < mid]; h2 = [r for r in tr if r["t_close"] >= mid]
    s["half1"] = round(stt.mean(r["ret"] for r in h1), 4) if h1 else None
    s["half2"] = round(stt.mean(r["ret"] for r in h2), 4) if h2 else None
    s["n_half"] = [len(h1), len(h2)]
    days = max(1.0, (t_hi - t_lo) / 86400)
    s["trades_per_day"] = round(len(tr) / days, 2)
    return s


def load_rows() -> list[dict]:
    f = OUT / "rows.json"
    rows = json.loads(f.read_text()) if f.exists() else build()
    for r in rows:
        r["q"] = {int(k): v for k, v in r["q"].items()}
    return rows


def discovery(rows: list[dict], cut: int) -> list[tuple]:
    disc = [r for r in rows if r["close"] < cut]
    t_lo = min(r["close"] for r in disc)
    res = []
    for v in grid():
        tr = run_variant(disc, v)
        res.append((v, full_stats(tr, t_lo, cut)))
    return res


def brier(rows: list[dict], L: int) -> dict:
    """Calibration on the given rows: Brier and log loss of the Kalshi mid, the de-vigged close and open lines."""
    out = defaultdict(list)
    for r in rows:
        q = r["q"].get(L)
        if not q or not r["book_close"]:
            continue
        y = 1.0 if r["won"] else 0.0
        cands = {"kalshi_mid": (q[0] + q[1]) / 2, "dk_close_mult": fair_of(r, "close", "mult"), "dk_close_power": fair_of(r, "close", "power")}
        if r["book_open"] and r["side"] != "draw":
            cands["dk_open_mult"] = fair_of(r, "open", "mult")
        for k, p in cands.items():
            if p is None:
                continue
            p = min(max(p, 0.005), 0.995)
            out[k].append(((p - y) ** 2, -(y * math.log(p) + (1 - y) * math.log(1 - p))))
    return {k: {"n": len(v), "brier": round(stt.mean(a for a, _ in v), 5), "logloss": round(stt.mean(b for _, b in v), 5)} for k, v in out.items()}


def validate(rows: list[dict], cut: int) -> list[dict]:
    fz = json.loads((OUT / "frozen.json").read_text())
    val = [r for r in rows if r["close"] >= cut]
    t_hi = max(r["close"] for r in val)
    out = []
    for c in fz["candidates"]:
        tr = run_variant(val, c)
        s = full_stats(tr, cut, t_hi)
        s["rule"] = c["id"] + ": " + vname(c)
        s["by_series"] = {k: len([r for r in tr if r["s"] == k]) for k in SERIES}
        s["trades"] = [{k: r[k] for k in ("t", "side", "px", "fair", "edge", "won", "ret")} for r in tr]
        out.append(s)
    return out


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] in ("disc", "val"):
    rows = load_rows(); cut = split_cut(rows, 0.7)
    if sys.argv[1] == "disc":
        res = discovery(rows, cut)
        (OUT / "discovery.json").write_text(json.dumps([[vname(v), v, s] for v, s in res], indent=0))
        print(len(res), "variants")
    else:
        res = validate(rows, cut)
        (OUT / "validation.json").write_text(json.dumps(res, indent=1))
        for s in res:
            print(s["rule"], {k: v for k, v in s.items() if k not in ("trades", "rule")})
