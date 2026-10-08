"""multi_signal feature builder: one row per (market, decision time t) over every candle family on disk.

Everything in a row's features is known at t: the Kalshi quotes of candles that closed <= t (carried forward <= 30 min),
volume of candles <= t, the scheduled close/expiration (never the actual close of a sports market), the ladder of the
same event at t. The fill is the quote at t + 60 s (YES at yes_ask, NO at 1 - yes_bid), skipped when older than 30 min.

Decision grid = lab/kalshi/calib.py's (TAU_CLOSE before a fixed close inside the fetched window, TAU_EXP around the
scheduled end for sports/tennis/esports). Rows with mid outside [0.03, 0.97] are dropped (nothing to learn or trade).
Usage: python -m lab.kalshi.strategies.multi_signal_features   -> data/kalshi_lab/strategies/multi_signal/rows.jsonl
"""
from __future__ import annotations
import bisect, json, math, sys
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import FAMILY, TAU_CLOSE, TAU_EXP, MAX_AGE
from lab.kalshi.fetch import PLAN, MK, CD

OUT = Path("data/kalshi_lab/strategies/multi_signal")
ROWS = OUT / "rows.jsonl"
DELAY = 60
MID_LO, MID_HI = 0.03, 0.97


def lg(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


class Q:
    """Carry-forward quote lookup on one market's 1-minute candles [ts, ask, bid, ask_lo, bid_hi, vol]."""
    def __init__(self, c: list[list]):
        self.c = c; self.ts = [r[0] for r in c]
        self.cumv = [0.0]
        for r in c:
            self.cumv.append(self.cumv[-1] + float(r[5] or 0))

    def at(self, t: int):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return None
        r = self.c[i]
        if t - r[0] > MAX_AGE * 60 or r[1] is None or r[2] is None:
            return None
        return r[1], r[2]

    def mid(self, t: int):
        q = self.at(t)
        return None if q is None else (q[0] + q[1]) / 2

    def vol(self, t0: int, t1: int) -> float:
        """Volume of candles with t0 < ts <= t1."""
        return self.cumv[bisect.bisect_right(self.ts, t1)] - self.cumv[bisect.bisect_right(self.ts, t0)]


def build_series(s: str) -> list[dict]:
    mf, cf = MK / f"{s}.jsonl", CD / f"{s}.jsonl"
    if not (mf.exists() and cf.exists()):
        return []
    fam = FAMILY[s]; days, win, anchor = PLAN[s]
    meta = {m["t"]: m for m in map(json.loads, mf.open())}
    qs = {}
    for line in cf.open():
        x = json.loads(line)
        if x["t"] in meta and x["c"]:
            qs[x["t"]] = Q(x["c"])
    # events: all listed markets (to know whether the ladder is complete), sorted by strike
    ev = defaultdict(list)
    for m in meta.values():
        ev[m["e"]].append(m)
    out = []
    for tk, q in qs.items():
        m = meta[tk]; sibs = ev[m["e"]]
        excl = fam in ("sports", "tennis", "esports")             # one winner among the event's markets
        ladder = (not excl) and len(sibs) >= 3 and m["type"] in ("greater", "greater_or_equal") and m["floor"] is not None
        if ladder:
            sibs = sorted((x for x in sibs if x["floor"] is not None), key=lambda x: x["floor"])
            pos = next(i for i, x in enumerate(sibs) if x["t"] == tk)
        if anchor == "close":
            pts = [(m["close"] - tau * 60, tau) for tau in TAU_CLOSE if tau <= win]
        else:
            pts = [(m["exp"] + off * 60, off) for off in TAU_EXP]
        for t, lab in pts:
            if t + DELAY >= m["close"] or (m["open"] and t < m["open"]):
                continue   # not open at t, or closed before the fill (known when the order is rejected)
            qt = q.at(t); qf = q.at(t + DELAY)
            if qt is None or qf is None:
                continue
            ask, bid = qt; mid = (ask + bid) / 2
            if not (MID_LO <= mid <= MID_HI):
                continue
            m5 = q.mid(t - 300); m30 = q.mid(t - 1800)
            L = lg(mid)
            f = {"L": L, "spr": ask - bid, "d5": L - lg(m5) if m5 is not None else 0.0, "d30": L - lg(m30) if m30 is not None else 0.0,
                 "lv60": math.log1p(q.vol(t - 3600, t)),
                 "tt": math.log1p((m["close"] - t) / 60) if anchor == "close" else (t - m["exp"]) / 3600,
                 "ladr": 0.0, "ovr": 0.0, "nrm": 0.0}
            if ladder:
                nb = [qs[sibs[j]["t"]].mid(t) if sibs[j]["t"] in qs else None for j in (pos - 1, pos + 1) if 0 <= j < len(sibs)]
                if len(nb) == 2 and None not in nb:
                    f["ladr"] = mid - (nb[0] + nb[1]) / 2   # >0: priced above its neighbours' interpolation
            if excl and len(sibs) >= 2:
                ms = [qs[x["t"]].mid(t) if x["t"] in qs else None for x in sibs]
                if None not in ms and sum(ms) > 0:
                    S = sum(ms); f["ovr"] = S - 1; f["nrm"] = lg(mid / S) - L
            out.append({"fam": fam, "s": s, "e": m["e"], "tk": tk, "t_close": m["close"], "t": t, "lab": lab,
                        "mid": mid, "ask_t": ask, "bid_t": bid, "ask": qf[0], "bid": qf[1], "vf": q.vol(t, t + DELAY),
                        "won": 1 if m["result"] == "yes" else 0, "f": f})
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    series = sorted(FAMILY, key=lambda s: -(CD / f"{s}.jsonl").stat().st_size if (CD / f"{s}.jsonl").exists() else 0)
    n = 0
    with Pool(8) as pool, ROWS.open("w") as fh:
        for rows in pool.imap_unordered(build_series, series):
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
            n += len(rows)
            if rows:
                print(f"{rows[0]['s']:22s} {len(rows):7d} rows", flush=True)
    print("rows", n)


if __name__ == "__main__":
    main()
