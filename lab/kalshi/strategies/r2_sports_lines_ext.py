"""r2_sports_lines_ext: are Kalshi's game SPREAD and TOTAL markets priced off the sportsbook line as tightly as the
game-winner markets are?

Hypothesis. Kalshi winner markets sit 0.55-1.5c from de-vigged DraftKings because market makers copy the books
(sports_books, round 1). Spread / total markets (KX{NFL,NCAAF,MLB,NHL,UEFANL}{SPREAD,TOTAL}) are less liquid ladders
of 10-30 strikes per game and may get less copying, so the strike that equals the DraftKings main line may sit away
from the de-vigged DraftKings price (spread or over/under price from ESPN's core odds, the only book ESPN keeps).

Kill test (step 1). For every settled market whose strike equals the DraftKings CLOSING main line exactly (half-point
lines only: total X.5 -> "Over X.5"; spread FAV -X.5 -> "FAV wins by over X.5"), compare the Kalshi mid L minutes
before the ESPN scheduled start with the de-vigged DraftKings price (multiplicative and power methods). Dead if the
median absolute gap is < 2c with 1-2c Kalshi spreads.
Protocol (step 3, run regardless so the verdict rests on returns too): one trade per Kalshi event (game x product), the
side with the largest edge = fair - taker price - fee, if edge >= X; taker at the quote 1 minute after the decision
(YES at yes_ask, NO at 1 - yes_bid, quote <= 30 min old); fee 0.07 * mult * p(1-p) rounded up to the cent per ~$5
order (mult 0.5 for MLB spread/total). Events ordered by start; DISCOVERY = first 70%, VALIDATION = last 30%.
Clusters for the t-stat: the game (spread and total of one game share a cluster).
Look-ahead caveat: ESPN keeps only the DraftKings closing line (taken at the actual start), so L is kept at <= 15 min.

Result (2026-10-08): DEAD at the kill test. 1,304 exact-line markets (10 series, 865 games, 2026-09-06..10-07): median
|Kalshi mid - de-vigged DK| 0.67c (mult) / 0.66c (power) at L=15, p90 2.1c, 3.3% of markets >= 3c, Kalshi spread 1c.
Largest in UEFA totals/spreads and NFL (~1.0c), smallest MLB (0.55c). The Kalshi mid beats DK on Brier and log loss;
regressing (won - mid) on (DK - mid) gives slope -0.6 +/- 1.0. A live no-look-ahead snapshot (105 strikes, 0-50 h before
start) gives a 0.58c median gap and no gap >= 3c. Discovery grid (640 variants): signals have a modelled edge of only
0.5-0.9c per trade (+1-2% per $); the three frozen candidates on validation: +45% (n=8), +9% (n=42, t 0.69, first half
-22%), +26% (n=26, t 1.29) - all binary-outcome noise around a ~+2% expectation. See result.json.

Usage: python -m lab.kalshi.strategies.r2_sports_lines_ext targets|fetch|kill|disc|val|all"""
from __future__ import annotations
import json, math, re, statistics as stt, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote
from lab.kalshi.strategies.sports_books import match_events, american, devig, OUT as SB_OUT
from lab.kalshi.strategies import r2_sports_lines_ext_fetch as F

OUT = F.OUT
PROD = {  # series -> (game series, sport key, product, fee multiplier)
    "KXNFLTOTAL": ("KXNFLGAME", "nfl", "total", 1.0), "KXNFLSPREAD": ("KXNFLGAME", "nfl", "spread", 1.0),
    "KXNCAAFTOTAL": ("KXNCAAFGAME", "ncaaf", "total", 1.0), "KXNCAAFSPREAD": ("KXNCAAFGAME", "ncaaf", "spread", 1.0),
    "KXMLBTOTAL": ("KXMLBGAME", "mlb", "total", 0.5), "KXMLBSPREAD": ("KXMLBGAME", "mlb", "spread", 0.5),
    "KXNHLTOTAL": ("KXNHLGAME", "nhl", "total", 1.0), "KXNHLSPREAD": ("KXNHLGAME", "nhl", "spread", 1.0),
    "KXUEFANLTOTAL": ("KXUEFANLGAME", "uefa", "total", 1.0), "KXUEFANLSPREAD": ("KXUEFANLGAME", "uefa", "spread", 1.0),
}
LEADS = (2, 5, 10, 15, 30, 45, 60)   # trading uses L <= 15 only (closing-line look-ahead); 30-60 are lag diagnostics
WIN_BEFORE = 75    # minutes of candles fetched before the scheduled start (quote carry-forward needs <= 30 min)
WIN_AFTER = 3
STAKE = 5.0


def fee_c(p: float, mult: float = 1.0) -> float:
    """Per-contract taker fee with Kalshi's round-up to the cent per order, for a ~$5 order."""
    n = max(1, int(STAKE / p))
    return math.ceil(0.07 * mult * n * p * (1 - p) * 100 - 1e-9) / 100 / n


def load(series: str) -> dict:
    ev = defaultdict(list)
    for l in (F.MKD / f"{series}.jsonl").open():
        m = json.loads(l); ev[m["e"]].append(m)
    return ev


_espn = None
_pairs: dict = {}


def game_pairs(gs: str) -> dict:
    global _espn
    if _espn is None:
        _espn = json.loads((SB_OUT / "espn_events.json").read_text())
    if gs not in _pairs:
        _pairs[gs] = match_events(gs, _espn[gs])
    return _pairs[gs]


def dk(gs: str, ev: dict) -> dict | None:
    f = SB_OUT / "espn" / "odds" / gs / f"{ev['id']}.json"
    if not f.exists():
        return None
    j = json.loads(f.read_text())
    if not j or not j.get("items"):
        return None
    it = j["items"][0]
    if (it.get("provider") or {}).get("name") != "DraftKings":
        return None
    out = {}
    for which in ("close", "open"):
        d = {}
        tot = it.get(which) or {}
        try:
            d["total"] = float((tot.get("total") or {}).get("american"))
            d["over"], d["under"] = american((tot.get("over") or {}).get("american")), american((tot.get("under") or {}).get("american"))
        except Exception:
            pass
        for ha, key in (("home", "homeTeamOdds"), ("away", "awayTeamOdds")):
            o = (it.get(key) or {}).get(which) or {}
            try:
                d[f"{ha}_pts"] = float((o.get("pointSpread") or {}).get("american"))
                d[f"{ha}_px"] = american((o.get("spread") or {}).get("american"))
            except Exception:
                pass
        out[which] = d
    return out


def code_map(gs: str, game_event: str, ev: dict) -> dict:
    """team code (as in the ticker suffix) -> 'home' / 'away', from the matched game-winner market tickers."""
    out = {}
    for tk, ha in ev["map"].items():
        if ha in ("home", "away"):
            out[tk.rsplit("-", 1)[1].upper()] = ha
    return out


def market_line(series: str, m: dict, cmap: dict) -> tuple[str, float] | None:
    """('over', X) for totals; (home/away, X) for 'TEAM wins by over X'."""
    if m.get("type") != "greater" or m.get("floor") is None:
        return None
    if PROD[series][2] == "total":
        return "over", float(m["floor"])
    code = re.sub(r"\d+$", "", m["t"].rsplit("-", 1)[1].upper())
    ha = cmap.get(code)
    return (ha, float(m["floor"])) if ha else None


def fair(series: str, line: tuple, book: dict, method: str) -> float | None:
    """De-vigged DraftKings probability for the Kalshi YES proposition, only when the strike equals the DK main line."""
    if not book:
        return None
    side, x = line
    if side == "over":
        if book.get("total") != x or not book.get("over") or not book.get("under"):
            return None
        return devig({"o": book["over"], "u": book["under"]}, method)["o"]
    other = "away" if side == "home" else "home"
    if book.get(f"{side}_pts") != -x or not book.get(f"{side}_px") or not book.get(f"{other}_px"):
        return None
    return devig({"a": book[f"{side}_px"], "b": book[f"{other}_px"]}, method)["a"]


def build_rows() -> list[dict]:
    """One row per Kalshi spread/total market whose strike equals the DraftKings closing main line."""
    rows = []
    cd = {}
    for f in F.CDD.glob("*.jsonl") if F.CDD.exists() else []:
        for l in f.open():
            x = json.loads(l); cd[x["t"]] = x["c"]
    for s, (gs, sport, prod, fm) in PROD.items():
        if not (F.MKD / f"{s}.jsonl").exists():
            continue
        pairs = game_pairs(gs)
        for e, ms in load(s).items():
            ge = gs + "-" + e.split("-", 1)[1]
            ev = pairs.get(ge)
            if not ev:
                continue
            book = dk(gs, ev)
            if not book:
                continue
            cmap = code_map(gs, ge, ev)
            for m in ms:
                ln = market_line(s, m, cmap)
                if not ln:
                    continue
                fc = {meth: fair(s, ln, book["close"], meth) for meth in ("mult", "power")}
                if fc["mult"] is None:
                    continue
                fo = fair(s, ln, book["open"], "mult")
                c = cd.get(m["t"])
                q = {}
                if c is not None:
                    for L in LEADS:
                        t = ev["start"] - L * 60
                        if t + 60 >= m["close"]:
                            continue
                        qq = quote(c, t + 60); q0 = quote(c, t)
                        if qq:
                            q[L] = {"fill": qq, "dec": q0}
                rows.append({"s": s, "sport": sport, "prod": prod, "fm": fm, "e": e, "game": ge, "t": m["t"], "line": ln, "start": ev["start"],
                             "close": m["close"], "open": m["open"], "won": m["result"] == "yes", "fair_mult": fc["mult"], "fair_power": fc["power"],
                             "fair_open": fo, "vol": m["vol"], "have_c": c is not None, "q": q})
    return rows


def targets() -> list[dict]:
    rows = build_rows()
    tg = [{"s": r["s"], "t": r["t"], "lo": r["start"] - WIN_BEFORE * 60, "hi": r["start"] + WIN_AFTER * 60}
          for r in rows if r["open"] is not None and r["open"] < r["start"] - 20 * 60]
    return tg


# ----------------------------------------------------------------------------------------------------------- analysis
def kill(rows: list[dict], L: int = 15) -> dict:
    out = {}
    by = defaultdict(list)
    for r in rows:
        x = r["q"].get(L) or r["q"].get(str(L))
        if not x or not x.get("dec"):
            continue
        ask, bid = x["dec"]
        if not (0 < bid < ask < 1):
            continue
        mid = (ask + bid) / 2
        for k in ("all", r["s"], r["sport"], r["prod"]):
            by[k].append((abs(mid - r["fair_mult"]), abs(mid - r["fair_power"]), ask - bid, mid - r["fair_mult"], r["vol"]))
    for k, v in sorted(by.items()):
        g1 = sorted(a for a, *_ in v); g2 = sorted(b for _, b, *_ in v); sp = sorted(c for _, _, c, *_ in v)
        out[k] = {"n": len(v), "median_gap_mult_c": round(100 * stt.median(g1), 2), "median_gap_power_c": round(100 * stt.median(g2), 2),
                  "mean_gap_mult_c": round(100 * stt.mean(g1), 2), "p90_gap_mult_c": round(100 * g1[int(0.9 * (len(g1) - 1))], 2),
                  "share_gap_ge_3c": round(sum(a >= 0.03 for a in g1) / len(g1), 3), "median_spread_c": round(100 * stt.median(sp), 2),
                  "mean_signed_mid_minus_fair_c": round(100 * stt.mean(d for *_, d, _ in v), 2)}
    return out


def calibration(rows: list[dict], L: int = 15) -> dict:
    out = defaultdict(list)
    for r in rows:
        x = r["q"].get(L) or r["q"].get(str(L))
        if not x or not x.get("dec"):
            continue
        ask, bid = x["dec"]; y = 1.0 if r["won"] else 0.0
        for k, p in (("kalshi_mid", (ask + bid) / 2), ("dk_mult", r["fair_mult"]), ("dk_power", r["fair_power"])):
            p = min(max(p, 0.005), 0.995)
            out[k].append(((p - y) ** 2, -(y * math.log(p) + (1 - y) * math.log(1 - p))))
    return {k: {"n": len(v), "brier": round(stt.mean(a for a, _ in v), 5), "logloss": round(stt.mean(b for _, b in v), 5)} for k, v in out.items()}


def event_trades(rows: list[dict], L: int, method: str, X: float, sports=None, prods=None, side="any", pmin=0.05, pmax=0.95) -> list[dict]:
    ev = defaultdict(list)
    for r in rows:
        if sports and r["sport"] not in sports or prods and r["prod"] not in prods:
            continue
        ev[r["e"]].append(r)
    out = []
    for e, rs in ev.items():
        best = None
        for r in rs:
            x = r["q"].get(L) or r["q"].get(str(L))
            if not x:
                continue
            ask, bid = x["fill"]
            p = r["fair_mult"] if method == "mult" else r["fair_power"]
            for sd, px, fr, won in (("YES", ask, p, r["won"]), ("NO", 1 - bid, 1 - p, not r["won"])):
                if not (pmin <= px <= pmax):
                    continue
                if side == "fav" and fr < 0.5 or side == "dog" and fr >= 0.5:
                    continue
                f = fee_c(px, r["fm"])
                edge = fr - px - f
                if best is None or edge > best[0]:
                    best = (edge, {"s": r["s"], "e": e, "game": r["game"], "t": r["t"], "start": r["start"], "t_close": r["close"], "side": sd, "px": px,
                                   "fair": fr, "edge": edge, "won": won, "ret": ((1.0 if won else 0.0) - px - f) / px, "vol": r["vol"]})
        if best and best[0] >= X:
            out.append(best[1])
    return out


def stats(tr: list[dict], t_lo: int | None = None, t_hi: int | None = None) -> dict:
    if not tr:
        return {"n": 0}
    cl = defaultdict(list)
    for r in tr:
        cl[r["game"]].append(r["ret"])
    em = [stt.mean(v) for v in cl.values()]
    n_e = len(em)
    t = stt.mean(em) / (stt.pstdev(em) / math.sqrt(n_e)) if n_e > 2 and stt.pstdev(em) > 0 else float("nan")
    rs = sorted((r["ret"] for r in tr), reverse=True)
    s = {"n": len(tr), "events": n_e, "win": round(sum(r["won"] for r in tr) / len(tr), 4), "avg_px": round(stt.mean(r["px"] for r in tr), 4),
         "ret_per_dollar": round(stt.mean(r["ret"] for r in tr), 4), "t": round(t, 3) if t == t else None,
         "ret_wo3": round(stt.mean(rs[3:]), 4) if len(rs) > 3 else None}
    if t_lo is not None:
        mid = (t_lo + t_hi) / 2
        h1 = [r["ret"] for r in tr if r["start"] < mid]; h2 = [r["ret"] for r in tr if r["start"] >= mid]
        s["half1"] = round(stt.mean(h1), 4) if h1 else None; s["half2"] = round(stt.mean(h2), 4) if h2 else None
        s["n_half"] = [len(h1), len(h2)]
        s["trades_per_day"] = round(len(tr) / max(1.0, (t_hi - t_lo) / 86400), 2)
    return s


SPORTS = {"all": None, "nfl": ("nfl",), "ncaaf": ("ncaaf",), "mlb": ("mlb",), "nhl": ("nhl",), "uefa": ("uefa",)}
PRODS = {"both": None, "total": ("total",), "spread": ("spread",)}


def grid() -> list[dict]:
    g = []
    for L in (2, 5, 10, 15):
        for method in ("mult", "power"):
            for X in (0.0, 0.01, 0.02, 0.03):
                for sp in SPORTS:
                    for pr in PRODS:
                        g.append({"L": L, "method": method, "X": X, "sp": sp, "pr": pr, "side": "any"})
                for side in ("fav", "dog"):
                    g.append({"L": L, "method": method, "X": X, "sp": "all", "pr": "both", "side": side})
    return g


def vname(v: dict) -> str:
    return f"L={v['L']} {v['method']} X>={v['X']:.2f} {v['sp']} {v['pr']} {v['side']}"


def run_variant(rows, v):
    return event_trades(rows, v["L"], v["method"], v["X"], SPORTS[v["sp"]], PRODS[v["pr"]], v["side"])


def split(rows: list[dict], frac: float = 0.7) -> int:
    starts = sorted({r["start"] for r in rows})
    return starts[int(len(starts) * frac)]


def load_rows() -> list[dict]:
    rows = json.loads((OUT / "rows.json").read_text())
    for r in rows:
        r["q"] = {int(k): v for k, v in r["q"].items()}
        r["line"] = tuple(r["line"])
    return rows


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "targets"
    if what == "targets":
        rows = build_rows()
        by = defaultdict(int)
        for r in rows:
            by[r["s"]] += 1
        tg = targets()
        print("exact-line markets", len(rows), dict(by), "| candle targets (opened >= 20 min pre-start)", len(tg))
    if what == "fetch":
        mx = int(sys.argv[2]) if len(sys.argv) > 2 else None
        F.candles(targets(), mx)
    if what in ("rows", "all"):
        rows = build_rows()
        (OUT / "rows.json").write_text(json.dumps(rows))
        print("rows", len(rows), "with candles", sum(r["have_c"] for r in rows), "with L=15 quote", sum(15 in r["q"] for r in rows))


def lead_drift(rows: list[dict]) -> dict:
    """Does Kalshi lag the book? Median |Kalshi mid - DK closing fair| by lead time on the same markets: if Kalshi already
    sits on the closing line 60 min out, it is not waiting for the book to move."""
    out = {}
    have = [r for r in rows if all(L in r["q"] and r["q"][L].get("dec") for L in (2, 15, 60))]
    for L in LEADS:
        g = [abs((r["q"][L]["dec"][0] + r["q"][L]["dec"][1]) / 2 - r["fair_mult"]) for r in have if L in r["q"] and r["q"][L].get("dec")]
        if g:
            out[L] = {"n": len(g), "median_gap_c": round(100 * stt.median(g), 2), "mean_gap_c": round(100 * stt.mean(g), 2)}
    # how much of the Kalshi move from L=60 to L=2 points toward the closing line
    tow = [((r["q"][2]["dec"][0] + r["q"][2]["dec"][1]) / 2 - (r["q"][60]["dec"][0] + r["q"][60]["dec"][1]) / 2,
            r["fair_mult"] - (r["q"][60]["dec"][0] + r["q"][60]["dec"][1]) / 2) for r in have]
    big = [(dk_, dm) for dk_, dm in tow if abs(dm) >= 0.02]
    out["gap60_ge_2c"] = {"n": len(big), "share_closed_by_L2": round(stt.mean(min(1.5, max(-0.5, a / b)) for a, b in big), 3) if big else None}
    return out


def recon(rows: list[dict]) -> dict:
    """Regression of (won - mid) on (DK fair - mid) at L=15: slope ~0 means the book adds nothing beyond Kalshi."""
    xs, ys, gs = [], [], []
    for r in rows:
        x = r["q"].get(15)
        if not x or not x.get("dec"):
            continue
        mid = (x["dec"][0] + x["dec"][1]) / 2
        xs.append(r["fair_power"] - mid); ys.append((1.0 if r["won"] else 0.0) - mid); gs.append(r["game"])
    n = len(xs); mx = stt.mean(xs); my = stt.mean(ys)
    sxx = sum((a - mx) ** 2 for a in xs)
    b = sum((a - mx) * (c - my) for a, c in zip(xs, ys)) / sxx if sxx else float("nan")
    # cluster-robust SE by game
    cl = defaultdict(float)
    for a, c, g in zip(xs, ys, gs):
        cl[g] += (a - mx) * ((c - my) - b * (a - mx))
    se = math.sqrt(sum(v * v for v in cl.values())) / sxx if sxx else float("nan")
    return {"n": n, "slope": round(b, 3), "se_clustered": round(se, 3)}


def main(what: str) -> None:
    rows = load_rows()
    cut = split(rows)
    disc = [r for r in rows if r["start"] < cut]; val = [r for r in rows if r["start"] >= cut]
    if what == "kill":
        res = {"kill_L15": kill(rows, 15), "kill_L5": kill(rows, 5), "calibration_L15_all": calibration(rows, 15),
               "calibration_L15_disc": calibration(disc, 15), "lead_drift": lead_drift(rows), "recon_disc": recon(disc), "recon_all": recon(rows)}
        (OUT / "kill.json").write_text(json.dumps(res, indent=1, default=str))
        for k, v in res.items():
            print(k)
            if isinstance(v, dict):
                for kk, vv in v.items():
                    print("   ", kk, vv)
    if what == "disc":
        t_lo = min(r["start"] for r in disc)
        out = []
        for v in grid():
            tr = run_variant(disc, v)
            out.append([vname(v), v, stats(tr, t_lo, cut)])
        (OUT / "discovery.json").write_text(json.dumps(out))
        ok = [x for x in out if x[2]["n"] >= 30]
        print(len(out), "variants;", len(ok), "with n>=30")
        for x in sorted(ok, key=lambda x: -x[2]["ret_per_dollar"])[:15]:
            print("  ", x[0], {k: x[2][k] for k in ("n", "events", "win", "avg_px", "ret_per_dollar", "t", "ret_wo3", "half1", "half2")})
        base = run_variant(disc, {"L": 15, "method": "mult", "X": -1.0, "sp": "all", "pr": "both", "side": "any"})
        print("  baseline (always take the best side, no threshold):", stats(base))
    if what == "val":
        fz = json.loads((OUT / "frozen.json").read_text())
        t_hi = max(r["start"] for r in val)
        res = []
        for c in fz["candidates"]:
            tr = run_variant(val, c)
            s = stats(tr, cut, t_hi); s["rule"] = c["id"] + ": " + vname(c)
            s["trades"] = [{k: r[k] for k in ("t", "side", "px", "fair", "edge", "won", "ret")} for r in tr]
            res.append(s)
            print(s["rule"], {k: v for k, v in s.items() if k not in ("trades", "rule")})
        (OUT / "validation.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] in ("kill", "disc", "val"):
    main(sys.argv[1])
