"""maker_passive: does resting a limit bid (maker, no or reduced fee) and holding to settlement earn >= +10% per $ on Kalshi?

Simulation (no look-ahead):
  * decision at time T uses only the last 1-minute candle at or before T (<= 30 min old, both sides of the book present);
  * the bid price p is set from that quote (join the bid, improve it by 1c, or sit d cents / a fraction below);
  * the order is live from T + 60 s (latency) until a cancel time fixed in advance (T + H, or the market close);
  * FILL only if a later candle shows the opposite side trading THROUGH the price: yes_ask_low < p for a YES bid,
    yes_bid_high > 1 - p for a NO bid (a YES offer at 1 - p).  'touch' (<=, >=) is kept as a diagnostic only;
  * hold to settlement; P&L per contract = payout - p - maker fee (0 on 'quadratic' series; 0.25 x 0.07 x mult x p(1-p),
    rounded up to the cent per 10-contract order, on 'quadratic_with_maker_fees');
  * equal-$ return per trade = pnl / p; t-stat clustered by event.
Adverse selection is measured per cell: the return if every posted order had been filled at p vs the return of the
orders that actually filled, and the win rate of filled vs unfilled orders.

Split: per family, events ordered by close time; DISCOVERY = first 70%, VALIDATION = last 30%.
Usage: python -m lab.kalshi.strategies.maker_passive discover [fam,...]
       python -m lab.kalshi.strategies.maker_passive validate      (frozen candidates in CANDIDATES below)"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import FAMILY
from lab.kalshi.fetch import PLAN, MK, CD

OUT = Path("data/kalshi_lab/strategies/maker_passive")
SPLIT = 0.7
MAX_AGE = 30 * 60
MIN_N = 30

# placement times: minutes before close ("close" families) or minutes relative to expected expiration ("expected")
# and order lives H (minutes; None = until the market's close)
GRID = {
    "crypto15": ((14, 10, 6), (3, None)),
    "commod15": ((14, 10, 6), (3, None)),
    "cryptoH": ((60, 30, 15), (10, None)),
    "indexH": ((60, 30, 15), (10, None)),
    "tempH": ((60, 30, 15), (10, None)),
    "daily_commod": ((360, 180, 60), (30, None)),
    "rain": ((1080, 600, 240, 60), (60, None)),
    "sports": ((-360, -240, -120, -60), (30, 120)),
    "tennis": ((-360, -240, -120, -60), (30, 120)),
    "esports": ((-360, -240, -120, -60), (30, 120)),
}
RULES = ("join", "imp", "d02", "d05", "d10", "f25", "f50")
BANDS = ((0.03, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 0.85), (0.85, 0.97))


def series_meta() -> dict:
    d = json.load(open("data/kalshi_lab/series_all.json"))
    items = d["series"] if isinstance(d, dict) and "series" in d else d
    if isinstance(items, dict):
        items = list(items.values())
    return {s["ticker"]: s for s in items}


META = series_meta()


def maker_fee(p: float, s: str, contracts: int = 10) -> float:
    m = META.get(s, {})
    if m.get("fee_type") != "quadratic_with_maker_fees":
        return 0.0
    mult = float(m.get("fee_multiplier") or 1)
    return math.ceil(100 * 0.25 * 0.07 * mult * contracts * p * (1 - p) - 1e-9) / 100 / contracts


def price(rule: str, bid: float, ask: float) -> float | None:
    mid = (bid + ask) / 2
    if rule == "join":
        p = bid
    elif rule == "imp":
        p = bid + 0.01
        if p > ask - 0.01 + 1e-9:
            return None
    elif rule[0] == "d":
        p = bid - int(rule[1:]) / 100
    elif rule[0] == "f":
        p = math.floor(100 * mid * (1 - int(rule[1:]) / 100) + 1e-9) / 100
    else:
        raise ValueError(rule)
    p = round(p, 3)
    return p if 0.01 - 1e-9 <= p < ask - 1e-9 else None


def band(p: float) -> int | None:
    for i, (lo, hi) in enumerate(BANDS):
        if lo <= p < hi:
            return i
    return None


def family_series(fam: str) -> list[str]:
    return [s for s, f in FAMILY.items() if f == fam and (MK / f"{s}.jsonl").exists() and (CD / f"{s}.jsonl").exists()]


def load(fam: str):
    """Yield (series, meta, candles) for every market of the family that has candles."""
    for s in family_series(fam):
        meta = {m["t"]: m for m in map(json.loads, (MK / f"{s}.jsonl").open())}
        for line in (CD / f"{s}.jsonl").open():
            x = json.loads(line); m = meta.get(x["t"]); c = x["c"]
            if m and c and m.get("close"):
                yield s, m, c


def event_cut(fam: str) -> int:
    """Close time (max over the event's markets) at the 70% point of the family's events."""
    ev = {}
    for s in family_series(fam):
        for m in map(json.loads, (MK / f"{s}.jsonl").open()):
            if m.get("close"):
                ev[m["e"]] = max(ev.get(m["e"], 0), m["close"])
    closes = sorted(ev.values())
    return closes[int(len(closes) * SPLIT)] if closes else 0, ev


def placements(fam: str, s: str, m: dict) -> list[tuple[str, int]]:
    anchor = PLAN[s][2]
    Ts, _ = GRID[fam]
    if anchor == "close":
        return [(f"-{tau}m", m["close"] - tau * 60) for tau in Ts]
    return [(f"exp{off:+d}m", m["exp"] + off * 60) for off in Ts] if m.get("exp") else []


def simulate_market(fam: str, s: str, m: dict, c: list[list]):
    """Yield one record per (placement, life, side, rule) where an order could be posted."""
    won_yes = m["result"] == "yes"
    _, Hs = GRID[fam]
    ts = [r[0] for r in c]
    for lab, T in placements(fam, s, m):
        if T >= m["close"] - 60:
            continue
        # quote at T
        j = -1
        for i, t in enumerate(ts):
            if t <= T:
                j = i
            else:
                break
        if j < 0 or T - ts[j] > MAX_AGE:
            continue
        ask, bid = c[j][1], c[j][2]
        if ask is None or bid is None or ask >= 0.999 or bid <= 0.001 or ask <= bid:
            continue
        for H in Hs:
            end = m["close"] if H is None else min(m["close"], T + 60 + H * 60)
            seg = [r for r in c if T + 120 <= r[0] <= end]
            alows = [(r[3], r[5], r[0]) for r in seg if r[3] is not None]
            bhighs = [(r[4], r[5], r[0]) for r in seg if r[4] is not None]
            amin = min((a for a, _, _ in alows), default=None)
            bmax = max((b for b, _, _ in bhighs), default=None)
            hl = f"H{H}" if H is not None else "Hclose"
            for side in ("YES", "NO"):
                if side == "YES":
                    sb, sa, won = bid, ask, won_yes
                else:
                    sb, sa, won = 1 - ask, 1 - bid, not won_yes
                mid = (sb + sa) / 2
                bd = band(mid)
                if bd is None:
                    continue
                for rule in RULES:
                    p = price(rule, round(sb, 3), round(sa, 3))
                    if p is None:
                        continue
                    if side == "YES":
                        thr = amin is not None and amin < p - 1e-9
                        tch = amin is not None and amin <= p + 1e-9
                        fv = [(v, t) for a, v, t in alows if a < p - 1e-9]
                    else:
                        thr = bmax is not None and bmax > 1 - p + 1e-9
                        tch = bmax is not None and bmax >= 1 - p - 1e-9
                        fv = [(v, t) for b, v, t in bhighs if b > 1 - p + 1e-9]
                    f = maker_fee(p, s)
                    ret = ((1.0 if won else 0.0) - p - f) / p
                    yield {"key": (fam, lab, hl, side, rule, bd), "e": m["e"], "s": s, "T": T, "p": p, "mid": mid, "won": won,
                           "thr": thr, "tch": tch, "thr_vol": any(v > 0 for v, _ in fv), "ret": ret,
                           "fvol": fv[0][0] if fv else 0.0, "ft": fv[0][1] if fv else None, "mclose": m["close"]}


def stats(rows: list[dict]) -> dict:
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]
    n_e = len(em); mean = st.mean(r["ret"] for r in rows)
    sd = st.pstdev(em) if n_e > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(n_e)) if n_e > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    return {"n": len(rows), "events": n_e, "win": sum(r["won"] for r in rows) / len(rows), "px": st.mean(r["p"] for r in rows),
            "ret": mean, "t": t, "ret_wo3": st.mean(rs[3:]) if len(rs) > 3 else float("nan")}


class Cell:
    __slots__ = ("posted", "ret_sum", "won_unf", "n_unf", "mid_sum", "fills", "tch")

    def __init__(self):
        self.posted = 0; self.ret_sum = 0.0; self.won_unf = 0; self.n_unf = 0; self.mid_sum = 0.0; self.fills = []; self.tch = 0

    def add(self, r: dict) -> None:
        self.posted += 1; self.ret_sum += r["ret"]; self.mid_sum += r["mid"]; self.tch += r["tch"]
        if r["thr"]:
            self.fills.append({"e": r["e"], "ret": r["ret"], "won": r["won"], "p": r["p"], "mid": r["mid"], "close": r["close"],
                               "fvol": r["fvol"], "T": r["T"], "s": r["s"]})
        else:
            self.n_unf += 1; self.won_unf += r["won"]

    def summary(self) -> dict:
        rec = {"posted": self.posted, "fills": len(self.fills), "fill_rate": len(self.fills) / self.posted, "touch_rate": self.tch / self.posted,
               "ret_always": self.ret_sum / self.posted, "mid_implied": self.mid_sum / self.posted,
               "win_unfilled": self.won_unf / self.n_unf if self.n_unf else float("nan"),
               "ret_sum_fills": sum(f["ret"] for f in self.fills)}
        if len(self.fills) >= 3:
            rec.update(stats(self.fills))
            rec["fill_mid_implied"] = st.mean(f["mid"] for f in self.fills)
        return rec


def run_family(fam: str, phase: str, keys: set | None = None) -> dict:
    """Aggregate per cell for one phase ('disc' or 'val'); keys restricts to given cells. Returns {key: Cell}."""
    cut, evclose = event_cut(fam)
    cells = defaultdict(Cell)
    for s, m, c in load(fam):
        ec = evclose.get(m["e"], m["close"])
        if (phase == "disc") != (ec < cut):
            continue
        for r in simulate_market(fam, s, m, c):
            if keys is not None and r["key"] not in keys:
                continue
            r["close"] = ec
            cells[r["key"]].add(r)
    return cells


def fmt(k, v) -> str:
    b = BANDS[k[5]]
    s = f"{k[0]:12s} {k[1]:9s} {k[2]:6s} {k[3]:3s} {k[4]:4s} {b[0]:.2f}-{b[1]:.2f} posted={v['posted']:6d} fills={v['fills']:5d} fr={v['fill_rate']:.2f} "
    if "ret" in v:
        s += (f"ev={v['events']:4d} win={v['win']:.2f} px={v['px']:.3f} ret={v['ret']:+.1%} t={v['t']:5.2f} wo3={v['ret_wo3']:+.1%} "
              f"always={v['ret_always']:+.1%}")
    return s


def discover(fams: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / "discovery_cells.json"
    allc = json.loads(f.read_text()) if f.exists() else {}
    for fam in fams:
        cells = {k: c.summary() for k, c in run_family(fam, "disc").items()}
        for k, v in cells.items():
            allc["|".join(map(str, k))] = v
        good = sorted(((k, v) for k, v in cells.items() if v.get("n", 0) >= MIN_N), key=lambda kv: -kv[1]["t"] if kv[1]["t"] == kv[1]["t"] else 0)
        print(f"\n=== {fam}: cells {len(cells)}, with n>={MIN_N} fills: {len(good)}", flush=True)
        for k, v in good[:12]:
            print("  " + fmt(k, v))
        # family-wide adverse-selection summary per side / rule (all placements, bands pooled)
        agg = defaultdict(lambda: [0, 0, 0.0, 0.0])
        for k, v in cells.items():
            a = agg[(k[3], k[4])]
            a[0] += v["posted"]; a[1] += v["fills"]; a[2] += v["ret_always"] * v["posted"]; a[3] += v["ret_sum_fills"]
        print("  side rule  posted  fill%  ret_if_always_filled  ret_of_actual_fills")
        for (side, rule), a in sorted(agg.items()):
            print(f"  {side:3s} {rule:4s} {a[0]:7d} {a[1] / max(a[0], 1):6.1%} {a[2] / max(a[0], 1):+8.1%} {a[3] / max(a[1], 1):+8.1%}")
        f.write_text(json.dumps(allc))


# Frozen 2026-10-08 on DISCOVERY data only, before any validation row was computed. Selection rule, per hypothesis:
# the variant with the highest discovery clustered t among those with n >= 80 fills and mean >= +10% per $.
CANDIDATES = {
    # A. rain YES is overpriced (favourite-longshot / "rain anxiety"): rest a NO bid 5c under the NO bid, 10 h before close.
    #    discovery n=84, ev=29, +12.5%/$, t 2.20
    "A_rain_NO_d05": {"fams": ("rain",), "side": "NO", "lab": "-600m", "H": "Hclose", "rule": "d05", "bands": (3, 4)},
    # B. hourly index ladders: a NO favourite (0.85-0.97) that dips 10c below its bid 30 min before close recovers.
    #    best single discovery cell overall: n=101, ev=39, +11.5%/$, t 4.84 (side-asymmetric: YES side ~0, drift risk)
    "B_indexH_NOfav_d10": {"fams": ("indexH",), "side": "NO", "lab": "-30m", "H": "Hclose", "rule": "d10", "bands": (4,)},
    # C. in-play overreaction: a heavy favourite (0.85-0.97) in sports/tennis/esports; bid at 75% of its mid at exp-120m, live 2 h.
    #    YES side only (each team's market once). discovery n=96, ev=96, +13.6%/$, t 2.07
    "C_inplay_fav_f25": {"fams": ("sports", "tennis", "esports"), "side": "YES", "lab": "exp-120m", "H": "H120", "rule": "f25", "bands": (4,)},
}


def match(cand: dict, r: dict) -> bool:
    k = r["key"]
    return k[0] in cand["fams"] and k[3] == cand["side"] and k[1] == cand["lab"] and k[2] == cand["H"] and k[4] == cand["rule"] and k[5] in cand["bands"]


def evaluate(phase: str) -> dict:
    """Report every frozen candidate on one phase ('disc' = sanity re-run, 'val' = the one-time verdict)."""
    out = {}
    for name, cand in CANDIDATES.items():
        posted = []
        for fam in cand["fams"]:
            cut, evc = event_cut(fam)
            for s, m, c in load(fam):
                ec = evc.get(m["e"], m["close"])
                if (phase == "disc") != (ec < cut):
                    continue
                for r in simulate_market(fam, s, m, c):
                    if match(cand, r):
                        r["close"] = ec; posted.append(r)
        fills = [r for r in posted if r["thr"]]
        rec = {"posted": len(posted), "fills": len(fills), "fill_rate": len(fills) / max(len(posted), 1)}
        if len(fills) >= 3:
            rec.update(stats(fills))
            closes = sorted(r["close"] for r in fills); mid = closes[len(closes) // 2]
            h1 = [r["ret"] for r in fills if r["close"] < mid]; h2 = [r["ret"] for r in fills if r["close"] >= mid]
            rec["half1"] = st.mean(h1) if h1 else float("nan"); rec["half2"] = st.mean(h2) if h2 else float("nan")
            days = (max(r["close"] for r in posted) - min(r["close"] for r in posted)) / 86400 or 1
            rec["trades_per_day"] = len(fills) / days
            rec["median_fill_candle_volume"] = st.median(r["fvol"] for r in fills)
            rec["ret_if_every_posted_order_filled"] = st.mean(r["ret"] for r in posted)
            rec["win_filled"] = st.mean(r["won"] for r in fills)
            unf = [r for r in posted if not r["thr"]]
            rec["win_unfilled"] = st.mean(r["won"] for r in unf) if unf else float("nan")
            rec["mid_at_post_filled"] = st.mean(r["mid"] for r in fills)
            vf = [r for r in posted if r["thr_vol"]]
            rec["volume_confirmed"] = stats(vf) if len(vf) >= 3 else None
            tf = [r for r in posted if r["tch"]]
            rec["touch_fill"] = stats(tf) if len(tf) >= 3 else None
            rec["minutes_post_to_fill_median"] = st.median((r["ft"] - r["T"]) / 60 for r in fills)
            rec["by_family"] = {f: stats([r for r in fills if r["key"][0] == f]) for f in cand["fams"] if sum(r["key"][0] == f for r in fills) >= 3}
        out[name] = rec
        print(name, json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in rec.items() if k not in ("by_family", "volume_confirmed", "touch_fill")}), flush=True)
        for k in ("volume_confirmed", "touch_fill"):
            if rec.get(k):
                print(f"   {k}: n={rec[k]['n']} ret={rec[k]['ret']:+.1%} t={rec[k]['t']:.2f}")
        for f, v in rec.get("by_family", {}).items():
            print(f"   {f}: n={v['n']} ev={v['events']} ret={v['ret']:+.1%} t={v['t']:.2f}")
    return out


def adverse() -> dict:
    """Descriptive only (not used for any choice): per family and price band of the posted side, over the WHOLE sample,
    pooled over decision times, lives and the four shallow rules (join, imp, d02, d05): fill rate, win rate of filled vs
    unfilled orders, the return if every posted order had filled at its price, and the return of the orders that did fill."""
    out = {}
    for fam in GRID:
        agg = defaultdict(lambda: {"posted": 0, "fills": 0, "won_f": 0, "won_u": 0, "ret_all": 0.0, "ret_f": 0.0})
        for s, m, c in load(fam):
            for r in simulate_market(fam, s, m, c):
                if r["key"][4] not in ("join", "imp", "d02", "d05"):
                    continue
                a = agg[r["key"][5]]
                a["posted"] += 1; a["ret_all"] += r["ret"]
                if r["thr"]:
                    a["fills"] += 1; a["won_f"] += r["won"]; a["ret_f"] += r["ret"]
                else:
                    a["won_u"] += r["won"]
        out[fam] = {}
        for b, a in sorted(agg.items()):
            nu = a["posted"] - a["fills"]
            out[fam][f"{BANDS[b][0]:.2f}-{BANDS[b][1]:.2f}"] = {
                "posted": a["posted"], "fill_rate": round(a["fills"] / a["posted"], 3),
                "win_filled": round(a["won_f"] / a["fills"], 3) if a["fills"] else None,
                "win_unfilled": round(a["won_u"] / nu, 3) if nu else None,
                "ret_if_all_filled": round(a["ret_all"] / a["posted"], 4), "ret_actual_fills": round(a["ret_f"] / a["fills"], 4) if a["fills"] else None}
        print(fam, json.dumps(out[fam]), flush=True)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discover"
    if cmd == "adverse":
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "adverse_selection.json").write_text(json.dumps(adverse(), indent=1))
    if cmd == "discover":
        discover(sys.argv[2].split(",") if len(sys.argv) > 2 else list(GRID))
    elif cmd in ("check", "validate"):
        res = evaluate("disc" if cmd == "check" else "val")
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / ("candidates_discovery.json" if cmd == "check" else "validation.json")).write_text(json.dumps(res, indent=1, default=str))
