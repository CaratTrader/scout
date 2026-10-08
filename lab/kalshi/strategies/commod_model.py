"""commod_model: fair value of Kalshi commodity markets from free futures prices, traded against the Kalshi quote.

Family members
  15-minute (KXGOLD15M, KXSILVER15M, KXWTI15M): YES iff Pyth index 1-min close at close >= Pyth close at open (the
    strike). Verified on 2,890 markets: result == (next market's floor >= this floor), ties resolve YES.
    Proxy for the Pyth path: Yahoo 1-minute futures bars (GC=F, SI=F, CL=F); Pyth(T) ~ close of the Yahoo bar that
    starts at T-60 (15-min change correlation 0.995-0.997, residual 9% of the move sd).
  daily (KXGOLDD, KXNATGASD: Pyth 1-min close at 17:00 ET; KXWTI: ICE front-month daily settlement, ~14:30 ET).

Decision rule at minute t (data <= t only): displacement d = F(t) - F(open) [15-min] or the basis-adjusted futures
price minus the strike [daily]; per-minute sigma from the last 60 one-minute futures changes; fair p = Phi(d / (k sigma
sqrt(tau))) with tau = minutes to close. Fill one minute later as taker at the quote (YES at ask, NO at 1 - bid),
fee 0.07 p (1-p) per contract rounded UP to the cent per 10-contract order (a ~$5 stake). Resting (maker) variant:
fill only if the opposite side traded through the resting price (candle ask_low < bid / bid_high > ask); these series
are fee_type 'quadratic', so makers pay no fee.
Time split by event close per sub-family: discovery = first 70%, validation = last 30%. Every variant examined is
counted in VARIANTS; at most 3 frozen candidates (FROZEN) are evaluated on validation, once.
Results (2026-10-08): 207 variants examined; best discovery t = 1.42 (bar ~4.0 at K ~1,390). Validation of the three
frozen candidates: C1 15-min taker -23%/$ (n 30), C2 daily taker -24%/$ (n 172, 16 events), C3 15-min maker +1%/$
(n 21, t 0.03). At equal information the Kalshi 15-min quote is as good as or better than the futures model (Brier),
and one minute of reaction delay makes the model strictly worse: verdict dead. See data/kalshi_lab/strategies/commod_model/.
Usage: python -m lab.kalshi.strategies.commod_model [disc|val]"""
from __future__ import annotations
import bisect, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote, fee, cell_stats
from lab.kalshi.strategies.commod_model_data import load

ROOT = Path("data/kalshi_lab"); OUT = ROOT / "strategies" / "commod_model"
FUT15 = {"KXGOLD15M": "GC=F", "KXSILVER15M": "SI=F", "KXWTI15M": "CL=F"}
N = st.NormalDist()


class Fut:
    """Yahoo 1-minute bars; px(T) = close of the last bar starting at or before T-60 (the price at T)."""
    def __init__(self, sym: str):
        b = load(sym, "1m"); self.ts = [x[0] for x in b]; self.c = [x[4] for x in b]

    def idx(self, T: int, max_age: int = 600) -> int | None:
        i = bisect.bisect_right(self.ts, T - 60) - 1
        return i if i >= 0 and T - 60 - self.ts[i] <= max_age else None

    def px(self, T: int, max_age: int = 600) -> float | None:
        i = self.idx(T, max_age); return self.c[i] if i is not None else None

    def sigma(self, T: int, n: int = 60) -> float | None:
        """Per-minute sd of close-to-close changes over bars known at T (gaps scaled by sqrt of the gap)."""
        i = self.idx(T)
        if i is None or i < n + 1:
            return None
        r = []
        for j in range(i - n + 1, i + 1):
            gap = max(1, (self.ts[j] - self.ts[j - 1]) // 60)
            if gap > 30:
                continue
            r.append((self.c[j] - self.c[j - 1]) / math.sqrt(gap))
        return math.sqrt(sum(x * x for x in r) / len(r)) if len(r) >= n // 2 else None


def markets(series: str) -> tuple[dict, dict]:
    meta = {m["t"]: m for m in map(json.loads, (ROOT / "markets" / f"{series}.jsonl").open())}
    cand = {x["t"]: x["c"] for x in map(json.loads, (ROOT / "candles" / f"{series}.jsonl").open())}
    return meta, cand


def rows15(delay: int = 60, taus=range(1, 15), sig_n: int = 60) -> list[dict]:
    """One row per (market, decision minute): features known at t, fill quote at t + delay."""
    out = []
    for s, y in FUT15.items():
        F = Fut(y); meta, cand = markets(s)
        for tk, m in meta.items():
            c = cand.get(tk)
            if not c or m["floor"] is None:
                continue
            f0 = F.px(m["open"], max_age=180)
            if f0 is None:
                continue
            won = m["result"] == "yes"
            for tau in taus:
                t = m["close"] - tau * 60
                ft = F.px(t, max_age=180); sg = F.sigma(t, sig_n)
                q = quote(c, t + delay)
                if ft is None or not sg or not q:
                    continue
                q0 = quote(c, t)   # the quote visible at decision time (for the 'market moved' feature)
                out.append({"s": s, "e": m["e"], "t_close": m["close"], "tau": tau, "d": ft - f0, "sig": sg, "K": m["floor"],
                            "ask": q[0], "bid": q[1], "ask0": q0[0] if q0 else None, "bid0": q0[1] if q0 else None, "won": won,
                            "hour": (m["close"] // 3600) % 24})
    return out


def split(rows: list[dict], frac: float = 0.7) -> float:
    closes = sorted({r["t_close"] for r in rows}); return closes[int(len(closes) * frac)]


ORDER = 10   # contracts per order for the per-order fee rounding (~$5 stake)


def fee_order(px: float, n: int = ORDER) -> float:
    """Per-contract taker fee with Kalshi's round-up to the cent per order."""
    return math.ceil(round(0.07 * px * (1 - px) * n * 100, 6)) / 100 / n


def trade(r: dict, p: float, side: str) -> dict:
    px = r["ask"] if side == "YES" else 1 - r["bid"]; w = r["won"] if side == "YES" else not r["won"]
    pnl = (1.0 if w else 0.0) - px - fee_order(px)
    return {"e": r["e"], "s": r["s"], "t_close": r["t_close"], "tau": r["tau"], "side": side, "px": px, "won": w, "ret": pnl / px, "p": p}



DAILY = {"KXGOLDD": "GC=F", "KXNATGASD": "NG=F", "KXWTI": "CL=F"}


def settle_brackets(series: str) -> list[tuple[int, float, float]]:
    """(close_ts, lo, hi) per settled daily event: the settlement value lies in (highest YES strike, lowest NO strike]."""
    ev = defaultdict(list)
    for m in map(json.loads, (ROOT / "markets" / f"{series}.jsonl").open()):
        ev[m["e"]].append(m)
    out = []
    for ms in ev.values():
        ys = [m["floor"] for m in ms if m["result"] == "yes"]; ns = [m["floor"] for m in ms if m["result"] == "no"]
        if ys and ns:
            out.append((ms[0]["close"], max(ys), min(ns)))
    return sorted(out)


def rows_daily(step: int = 5, horizon_min: int = 360, sig_n: int = 120, delay: int = 60) -> list[dict]:
    """One row per (daily market, decision minute). Basis = futures minus the settlement-bracket midpoint of the
    previous event of the same series (known at t: that event closed a day or more earlier)."""
    out = []
    for s, y in DAILY.items():
        F = Fut(y); meta, cand = markets(s); br = settle_brackets(s)
        basis_at = {}
        for i, (T, lo, hi) in enumerate(br):
            if i == 0:
                continue
            Tp, lop, hip = br[i - 1]; fp = F.px(Tp, max_age=300)
            if fp is not None:
                basis_at[T] = fp - (lop + hip) / 2
        for tk, m in meta.items():
            c = cand.get(tk); b = basis_at.get(m["close"])
            if not c or b is None or m["floor"] is None:
                continue
            won = m["result"] == "yes"
            for mins in range(step, horizon_min + 1, step):
                t = m["close"] - mins * 60
                if t + delay >= m["close"]:
                    continue
                ft = F.px(t, max_age=600); sg = F.sigma(t, sig_n); q = quote(c, t + delay)
                if ft is None or not sg or not q:
                    continue
                out.append({"s": s, "e": m["e"], "t_close": m["close"], "tau": mins, "S": ft - b, "sig": sg, "K": m["floor"],
                            "ask": q[0], "bid": q[1], "won": won})
    return out


# ----------------------------------------------------------------------------------------------------------------
# Rules (each returns one trade per market: the first decision minute, scanning from the earliest, that fires)

def p15(r: dict, k: float) -> float:
    return N.cdf(r["d"] / (k * r["sig"] * math.sqrt(r["tau"])))


def pday(r: dict, k: float) -> float:
    return N.cdf((r["S"] - r["K"]) / (k * r["sig"] * math.sqrt(r["tau"])))


def taker_rule(rows: list[dict], pf, k: float, thr: float, taus, key=lambda r: r["e"]) -> list[dict]:
    """Buy the side whose model value exceeds the fill price plus fee by thr (a marketable limit order sent one minute
    after the decision, priced from data <= t)."""
    lo, hi = taus   # decision window in minutes before close (inclusive); (8, 8) = only 8 minutes before close
    out = {}
    for r in sorted(rows, key=lambda r: (r["t_close"], -r["tau"])):
        kk = key(r)
        if kk in out or not (lo <= r["tau"] <= hi):
            continue
        p = pf(r, k)
        ey = p - r["ask"] - fee_order(r["ask"]) if 0.02 <= r["ask"] < 1 else -1
        en = (1 - p) - (1 - r["bid"]) - fee_order(1 - r["bid"]) if 0 < r["bid"] <= 0.98 else -1
        if ey > thr:
            out[kk] = trade(r, p, "YES")
        elif en > thr:
            out[kk] = trade(r, p, "NO")
    return list(out.values())


def maker_rule15(rows: list[dict], cand: dict, k: float, m: float, tau0: int, life: int) -> list[dict]:
    """Rest a bid that joins the visible best price at t+60 when the model says the side is worth >= m more; filled
    only if the opposite side trades THROUGH the resting price within `life` minutes; no maker fee (quadratic)."""
    out = []
    for r in rows:
        if r["tau"] != tau0 or r["ask0"] is None:
            continue
        p = p15(r, k); t = r["t_close"] - tau0 * 60
        if p - r["bid0"] > m and r["bid0"] >= 0.03:
            side, px = "YES", r["bid0"]
        elif (1 - p) - (1 - r["ask0"]) > m and r["ask0"] <= 0.97:
            side, px = "NO", 1 - r["ask0"]
        else:
            continue
        filled = False
        for row in cand[r["tk"]]:
            if t + 60 < row[0] <= t + 60 + life * 60 and row[0] < r["t_close"]:
                if side == "YES" and row[3] is not None and row[3] < px:
                    filled = True; break
                if side == "NO" and row[4] is not None and row[4] > 1 - px:
                    filled = True; break
        if filled:
            w = r["won"] if side == "YES" else not r["won"]
            out.append({"e": r["e"], "s": r["s"], "t_close": r["t_close"], "tau": tau0, "side": side, "px": px, "won": w,
                        "ret": ((1.0 if w else 0.0) - px) / px, "p": p})
    return out


# ----------------------------------------------------------------------------------------------------------------
# Discovery grids (all counted) and the three frozen candidates (chosen by the highest discovery t with n >= 40 in
# each sub-family, written here before validation was run)

GRID15 = [(k, thr, taus) for k in (1.0, 1.3) for thr in (0.05, 0.10, 0.15, 0.25)
          for taus in ((2, 2), (3, 3), (5, 5), (8, 8), (12, 12), (2, 14))]
GRIDD = [(ss, k, thr, w) for ss in (("KXGOLDD",), ("KXNATGASD",), ("KXWTI",), tuple(DAILY)) for k in (1.0, 1.5)
         for thr in (0.05, 0.10, 0.20) for w in ((30, 120), (120, 240), (240, 360), (30, 360))]
GRIDM = [(k, m, tau0, life) for k in (1.0, 1.3) for m in (0.03, 0.08, 0.15) for tau0 in (12, 8, 5) for life in (2, 4)]
# extra checks counted as variants: ladder monotonicity arbitrage (1), tie-favoured YES near the money (18),
# late lock z>=2/3 (8)
VARIANTS_EXTRA = 1 + 18 + 8

FROZEN = {
    "C1_15m_taker": {"k": 1.0, "thr": 0.25, "taus": (8, 8)},
    "C2_daily_taker": {"series": tuple(DAILY), "k": 1.0, "thr": 0.05, "w": (120, 240)},
    "C3_15m_maker": {"k": 1.0, "m": 0.15, "tau0": 5, "life": 2},
}


def load_all():
    R15 = [r for r in rows15() if r["tau"] >= 2]
    tk = {}
    cand = {}
    for s in FUT15:
        meta, c = markets(s); cand.update(c)
        for t, m in meta.items():
            tk[m["e"]] = t
    for r in R15:
        r["tk"] = tk[r["e"]]
    RD = rows_daily()
    return R15, cand, RD


def stats(T: list[dict]) -> dict:
    if not T:
        return {"n": 0}
    cs = cell_stats(T); ts = sorted(r["t_close"] for r in T); mid = ts[len(ts) // 2]
    h1 = [r["ret"] for r in T if r["t_close"] < mid]; h2 = [r["ret"] for r in T if r["t_close"] >= mid]
    days = max(1.0, (ts[-1] - ts[0]) / 86400)
    cs.update({"half1": st.mean(h1) if h1 else float("nan"), "half2": st.mean(h2) if h2 else float("nan"),
               "trades_per_day": len(T) / days})
    return cs


def fmt(name: str, v: dict) -> str:
    if not v.get("n"):
        return f"{name}: no trades"
    return (f"{name}: n={v['n']} ev={v['events']} win={v['win']:.2f} px={v['px']:.3f} ret={v['ret']:+.3f} t={v['t']:.2f} "
            f"wo3={v['ret_wo3']:+.3f} halves={v['half1']:+.3f}/{v['half2']:+.3f} per_day={v['trades_per_day']:.1f}")


def discovery(R15, cand, RD) -> list[tuple]:
    c15 = split(R15); D15 = [r for r in R15 if r["t_close"] < c15]
    cutd = {s: split([r for r in RD if r["s"] == s]) for s in DAILY}; DD = [r for r in RD if r["t_close"] < cutd[r["s"]]]
    res = []
    for k, thr, taus in GRID15:
        res.append(("15m_taker", (k, thr, taus), stats(taker_rule(D15, p15, k, thr, taus))))
    for ss, k, thr, w in GRIDD:
        res.append(("daily_taker", (ss, k, thr, w), stats(taker_rule([r for r in DD if r["s"] in ss], pday, k, thr, w,
                                                                     key=lambda r: r["e"] + str(r["K"])))))
    for k, m, tau0, life in GRIDM:
        res.append(("15m_maker", (k, m, tau0, life), stats(maker_rule15(D15, cand, k, m, tau0, life))))
    return res


def validation(R15, cand, RD) -> dict:
    c15 = split(R15); V15 = [r for r in R15 if r["t_close"] >= c15]
    cutd = {s: split([r for r in RD if r["s"] == s]) for s in DAILY}; VD = [r for r in RD if r["t_close"] >= cutd[r["s"]]]
    f = FROZEN
    return {
        "C1_15m_taker": taker_rule(V15, p15, f["C1_15m_taker"]["k"], f["C1_15m_taker"]["thr"], f["C1_15m_taker"]["taus"]),
        "C2_daily_taker": taker_rule([r for r in VD if r["s"] in f["C2_daily_taker"]["series"]], pday, f["C2_daily_taker"]["k"],
                                     f["C2_daily_taker"]["thr"], f["C2_daily_taker"]["w"], key=lambda r: r["e"] + str(r["K"])),
        "C3_15m_maker": maker_rule15(V15, cand, **f["C3_15m_maker"]),
    }


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    R15, cand, RD = load_all()
    what = sys.argv[1] if len(sys.argv) > 1 else "disc"
    if what == "disc":
        res = discovery(R15, cand, RD)
        print("variants in grids:", len(res), "+ extra", VARIANTS_EXTRA)
        for fam in ("15m_taker", "daily_taker", "15m_maker"):
            rows = [x for x in res if x[0] == fam and x[2].get("n", 0) >= 40]
            print(f"\n{fam}: best 6 by t (n>=40)")
            for _, par, v in sorted(rows, key=lambda x: -(x[2]["t"] if x[2]["t"] == x[2]["t"] else -9))[:6]:
                print("  ", fmt(str(par), v))
        (OUT / "discovery.json").write_text(json.dumps([[f, str(p), v] for f, p, v in res], default=str))
    else:
        out = {}
        for name, T in validation(R15, cand, RD).items():
            v = stats(T); out[name] = v; print(fmt(name, v))
        (OUT / "validation.json").write_text(json.dumps(out, default=str))
