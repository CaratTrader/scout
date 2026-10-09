"""r4_mve_combo_no: is NO on Kalshi multivariate combos (RFQ parlays) a +10%/$ taker trade?

Hypothesis (round-3 lead): combos are the archetypal overpriced retail longshot, so NO on mid-priced combos (NO
0.50-0.85) bought as a taker earns >= +10% after fees.

Feasibility (calls 1-7, calls.log): combos have no secondary book. 0 of 1,037 open combos showed a resting bid or ask;
each combo print is an RFQ fill, the requester accepting a market maker's two-sided quote. So "1 - yes_bid an hour
after creation" does not exist. The only NO price a bot can get is its own RFQ, and the public record of what such an
RFQ pays is the prints with taker_side == "no" (new NO positions and YES holders cashing out both pay 1 - yes_price).
This script therefore measures:
  (1) calibration of combo trade prices by band and taker side (YES fills = what YES requesters pay),
  (2) NO-taker returns by NO-price band (the hypothesis cell is NO 0.50-0.85),
  (3) for combos whose legs are all cached game-winner / 15-minute series, the price vs the product of leg mids
      (independent legs only; same-game legs are flagged as correlated),
with a 70/30 split by game-day (ET date of the fill), every parameter chosen on discovery, <= 3 frozen candidates
judged once on validation, t clustered by game-day.

Fees: taker 0.07*p*(1-p) per contract, rounded UP to the cent per order of $5 (n = floor(5/p) contracts).
Data: data/kalshi_lab/strategies/r4_mve_combo_no/{fills.jsonl, markets.json} from r4_mve_combo_no_data.py.
Usage: python lab/kalshi/strategies/r4_mve_combo_no.py"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import Counter, defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.calib import quote

OUT = ROOT / "data/kalshi_lab/strategies/r4_mve_combo_no"
CD = ROOT / "data/kalshi_lab/candles"; MK = ROOT / "data/kalshi_lab/markets"
ET = dt.timezone(dt.timedelta(hours=-4))
STAKE = 5.0
VARIANTS: list[str] = []   # every cell / rule examined, counted once


def ts(s: str | None) -> float | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def fee_pc(p: float) -> float:
    """Per-contract taker fee for a $5 order at price p, rounded up to the cent per order."""
    n = max(1, int(STAKE / p))
    return math.ceil(round(0.07 * n * p * (1 - p) * 100, 6)) / 100 / n


def game_key(ev: str | None) -> str:
    """Same-game key: event ticker without its series prefix (KXNFLGAME-26SEP09NESEA and KXNFLSPREAD-26SEP09NESEA share it)."""
    return ev.split("-", 1)[1] if ev and "-" in ev else (ev or "")


def stats(rows: list[dict], key: str = "day") -> dict:
    if not rows:
        return {"n": 0}
    cl = defaultdict(list)
    for r in rows:
        cl[r[key]].append(r["ret"])
    em = [st.mean(v) for v in cl.values()]; k = len(em)
    se = st.pstdev(em) / math.sqrt(k) if k > 2 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    days = sorted({r["day"] for r in rows}); mid = days[len(days) // 2] if days else ""
    h1 = [r["ret"] for r in rows if r["day"] < mid]; h2 = [r["ret"] for r in rows if r["day"] >= mid]
    return {"n": len(rows), "events": k, "win": round(sum(r["won"] for r in rows) / len(rows), 4), "avg_px": round(st.mean(r["px"] for r in rows), 4),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(st.mean(em) / se, 2) if se and se == se and se > 0 else float("nan"),
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else float("nan"),
            "half1": round(st.mean(h1), 4) if h1 else float("nan"), "half2": round(st.mean(h2), 4) if h2 else float("nan"),
            "median_capacity_contracts": st.median(r["count"] for r in rows)}


def binom_lb(rows: list[dict]) -> float:
    """Gate amendment (d): one-sided 95% exact-binomial lower bound on the win rate over unique combos, turned into a
    return per $ at the mean price and fee. Normal approximation to the Beta bound is adequate for n >= 30."""
    u = {}
    for r in rows:
        u.setdefault((r["ticker"], r["side"]), r)
    x = list(u.values()); n = len(x)
    if n < 5:
        return float("nan")
    w = sum(r["won"] for r in x) / n; lb = max(0.0, w - 1.645 * math.sqrt(w * (1 - w) / n))
    px = st.mean(r["px"] for r in x); f = st.mean(fee_pc(r["px"]) for r in x)
    return round((lb - px - f) / px, 4)


# ---------------------------------------------------------------- leg fair values (cached candles only)
_leg_cache: dict[str, dict] = {}


def leg_series_cached() -> set[str]:
    return {p.stem for p in CD.glob("*.jsonl")}


def load_legs(tickers: set[str]) -> dict[str, list]:
    want = defaultdict(set)
    for t in tickers:
        want[t.split("-")[0]].add(t)
    out = {}
    for s, ts_ in want.items():
        f = CD / f"{s}.jsonl"
        if not f.exists():
            continue
        for line in f.open():
            if not any(t in line[:120] for t in ts_):
                continue
            x = json.loads(line)
            if x["t"] in ts_:
                out[x["t"]] = x["c"]
    return out


def build() -> list[dict]:
    markets = json.loads((OUT / "markets.json").read_text())
    fills = [json.loads(l) for l in (OUT / "fills.jsonl").open()]
    cached = leg_series_cached()
    need = set()
    for m in markets.values():
        if m["legs"] and all(l[0].split("-")[0] in cached for l in m["legs"]):
            need |= {l[0] for l in m["legs"]}
    legc = load_legs(need)
    rows, skipped = [], Counter()
    collected = ts("2026-10-09T00:30:00Z")
    for f in fills:
        m = markets.get(f["ticker"])
        if not m:
            skipped["no_market"] += 1; continue
        if f["block"]:
            skipped["block"] += 1; continue
        t = ts(f["ts"]); exp = ts(m["exp"])
        if exp is None or exp > collected - 86400:   # known at the fill: scheduled expiry too close to the data pull
            skipped["exp_after_pull"] += 1; continue
        if m["result"] not in ("yes", "no"):
            skipped["unsettled_" + m["status"]] += 1; continue
        if not (0.005 <= f["yes"] <= 0.995):
            skipped["px_extreme"] += 1; continue
        side = f["side"]; px = f["yes"] if side == "yes" else 1 - f["yes"]
        won = m["result"] == side
        ret = ((1.0 if won else 0.0) - px - fee_pc(px)) / px
        legs = m["legs"]; fam = sorted({l[0].split("-")[0] for l in legs})
        gk = [game_key(l[2]) for l in legs]
        corr = len(gk) != len(set(gk))
        fair = None
        if legs and all(l[0] in legc for l in legs) and not corr:
            p = 1.0
            for lt, ls, _ in legs:
                q = quote(legc[lt], int(t))
                if not q:
                    p = None; break
                mid = (q[0] + q[1]) / 2
                p *= mid if ls == "yes" else 1 - mid
            fair = p
        sports = all(not s.endswith("15M") for s in fam)
        rows.append({"ticker": f["ticker"], "win": f["win"], "day": dt.datetime.fromtimestamp(t, ET).date().isoformat(), "t": t,
                     "side": side, "yes": f["yes"], "px": px, "won": won, "ret": ret, "count": f["count"],
                     "age": t - ts(m["created"]), "nlegs": len(legs), "corr": corr, "fam": fam, "sports": sports,
                     "hrs_to_exp": (exp - t) / 3600, "fair": fair, "vol": m["vol"]})
    print("fills", len(fills), "rows", len(rows), "skipped", dict(skipped))
    return rows, dict(skipped)


BANDS_YES = [(0.005, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.995)]
BANDS_NO = [(0.30, 0.50), (0.50, 0.65), (0.65, 0.75), (0.75, 0.85), (0.50, 0.85), (0.85, 0.95), (0.95, 0.995)]


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "n=0"
    return (f"n={s['n']:5d} ev={s['events']:3d} win={s['win']:.3f} px={s['avg_px']:.3f} ret={s['ret_per_dollar']:+.1%} t={s['t']:5.2f} "
            f"wo3={s['ret_wo3']:+.1%} h={s['half1']:+.1%}/{s['half2']:+.1%} cap={s['median_capacity_contracts']:.0f}")


def main() -> None:
    rows, skipped = build()
    days = sorted({r["day"] for r in rows}); cut = days[int(len(days) * 0.7)]
    disc = [r for r in rows if r["day"] < cut]; val = [r for r in rows if r["day"] >= cut]
    print(f"days {len(days)} ({days[0]}..{days[-1]}), cut {cut}: discovery {len(disc)} fills, validation {len(val)}")
    res = {"skipped": skipped, "cut": cut, "n_days": len(days)}

    print("\nSIDE MIX (discovery):", Counter(r["side"] for r in disc), "opening fills (age<120 s):", sum(r["age"] < 120 for r in disc))
    print("\n(1) CALIBRATION of combo trade prices, discovery: realised YES rate vs YES trade price, by taker side")
    calib = {}
    for side in ("yes", "no", "all"):
        for lo, hi in BANDS_YES:
            x = [r for r in disc if lo <= r["yes"] < hi and (side == "all" or r["side"] == side)]
            if len(x) < 20:
                continue
            yrate = sum((r["won"] if r["side"] == "yes" else not r["won"]) for r in x) / len(x)
            py = st.mean(r["yes"] for r in x)
            calib[f"{side}|{lo}-{hi}"] = {"n": len(x), "yes_px": round(py, 4), "yes_rate": round(yrate, 4), "ratio": round(py / yrate, 2) if yrate else None}
            print(f"  taker {side:3s} yes {lo:.3f}-{hi:.3f} n={len(x):5d} yes_px={py:.3f} yes_rate={yrate:.3f} price/realised={py / yrate if yrate else float('inf'):.2f}")
    res["calibration_discovery"] = calib

    print("\n(2) TAKER RETURNS per $ after fees by side and taker-price band, discovery")
    cells = {}
    for side in ("yes", "no"):
        bands = BANDS_NO if side == "no" else [(lo, hi) for lo, hi in BANDS_YES]
        for lo, hi in bands:
            x = [r for r in disc if r["side"] == side and lo <= r["px"] < hi]
            VARIANTS.append(f"disc|{side}|px{lo}-{hi}")
            s = stats(x); cells[f"{side}|px{lo}-{hi}"] = s
            print(f"  {side:3s} px {lo:.3f}-{hi:.3f} {fmt(s)}")
    res["cells_discovery"] = cells

    print("\n    NO 0.50-0.85 split by features (discovery)")
    no_mid = [r for r in disc if r["side"] == "no" and 0.50 <= r["px"] < 0.85]
    feats = {
        "opening(age<120s)": lambda r: r["age"] < 120, "later(age>=120s)": lambda r: r["age"] >= 120,
        "legs2": lambda r: r["nlegs"] == 2, "legs3-4": lambda r: 3 <= r["nlegs"] <= 4, "legs5+": lambda r: r["nlegs"] >= 5,
        "corr(same-game)": lambda r: r["corr"], "indep": lambda r: not r["corr"],
        "sports_only": lambda r: r["sports"], "has_15m_leg": lambda r: not r["sports"],
        "exp<3h": lambda r: r["hrs_to_exp"] < 3, "exp3-24h": lambda r: 3 <= r["hrs_to_exp"] < 24, "exp>=24h": lambda r: r["hrs_to_exp"] >= 24,
    }
    fcells = {}
    for name, fn in feats.items():
        x = [r for r in no_mid if fn(r)]; VARIANTS.append(f"disc|no|0.50-0.85|{name}")
        s = stats(x); fcells[name] = s
        print(f"  {name:20s} {fmt(s)}")
    res["no_mid_features_discovery"] = fcells
    # all NO fills (any band) by the same features, to see where NO is least bad
    print("\n    ALL NO fills px 0.30-0.95 by features (discovery)")
    no_all = [r for r in disc if r["side"] == "no" and 0.30 <= r["px"] < 0.95]
    for name, fn in feats.items():
        x = [r for r in no_all if fn(r)]; VARIANTS.append(f"disc|no|0.30-0.95|{name}")
        print(f"  {name:20s} {fmt(stats(x))}")

    print("\n(3) PRICE vs PRODUCT OF LEG MIDS (independent legs, all legs in cached series, quote <= 30 min old)")
    fm = [r for r in rows if r["fair"] is not None and r["fair"] > 0]
    res["fair_n"] = len(fm)
    for side in ("yes", "no"):
        x = [r for r in fm if r["side"] == side and r["day"] < cut]
        if len(x) < 10:
            print(f"  {side}: n={len(x)} (too few)"); continue
        ratio = st.median(r["yes"] / r["fair"] for r in x); diff = st.mean(r["yes"] - r["fair"] for r in x)
        yr = sum((r["won"] if side == "yes" else not r["won"]) for r in x) / len(x)
        print(f"  taker {side}: n={len(x)} median yes_px/fair={ratio:.2f} mean yes_px-fair={diff:+.3f} mean fair={st.mean(r['fair'] for r in x):.3f} "
              f"mean yes_px={st.mean(r['yes'] for r in x):.3f} realised yes={yr:.3f}")
        res[f"fair_{side}_discovery"] = {"n": len(x), "median_ratio": round(ratio, 3), "mean_diff": round(diff, 4), "realised_yes": round(yr, 4)}
    VARIANTS.append("disc|fair_vs_price|yes"); VARIANTS.append("disc|fair_vs_price|no")
    # model rule on discovery: NO taker where the NO price is below 1 - fair by >= m
    for m_ in (0.0, 0.03, 0.06):
        x = [r for r in fm if r["side"] == "no" and r["day"] < cut and (1 - r["yes"]) <= (1 - r["fair"]) - m_ and 0.3 <= r["px"] < 0.95]
        VARIANTS.append(f"disc|no_model_edge>={m_}")
        print(f"  NO fill priced >= {m_:.2f} below 1-fair: {fmt(stats(x))}")

    # ---------------------------------------------------------------- frozen candidates (chosen above, judged once)
    cands = {
        "C1 NO taker, NO px 0.50-0.85 (pre-registered hypothesis cell)": lambda r: r["side"] == "no" and 0.50 <= r["px"] < 0.85,
        "C2 NO taker, NO px 0.50-0.85, opening fills (age<120s)": lambda r: r["side"] == "no" and 0.50 <= r["px"] < 0.85 and r["age"] < 120,
        "C3 NO taker, NO px 0.85-0.995 (favourite NO)": lambda r: r["side"] == "no" and 0.85 <= r["px"] < 0.995,
    }
    res["frozen"] = list(cands)
    print("\nVALIDATION (frozen candidates, judged once)")
    vres = []
    for name, fn in cands.items():
        x = [r for r in val if fn(r)]; d = [r for r in disc if fn(r)]
        s = stats(x); s["rule"] = name; s["binom_lb_ret"] = binom_lb(x); s["discovery"] = stats(d)
        s["trades_per_day"] = round(len(x) / max(1, len({r['day'] for r in val})), 1)
        s["by_window_t"] = stats(x, key="win").get("t") if x else None
        vres.append(s)
        print(f"  {name}\n     disc {fmt(s['discovery'])}\n     VAL  {fmt(s)} binomLB={s['binom_lb_ret']} t(by window)={s['by_window_t']}")
    res["validation"] = vres
    res["variants_examined"] = len(VARIANTS)
    print("\nvariants examined:", len(VARIANTS))
    (OUT / "analysis.json").write_text(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
