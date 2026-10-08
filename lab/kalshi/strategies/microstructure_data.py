"""microstructure data: every Kalshi lab candle family as per-market 1-minute grids.

For each settled market with candles (data/kalshi_lab/candles/<SERIES>.jsonl) we build a minute grid over its decision
window (lab.kalshi.fetch.window). Each grid minute i covers the candle that ENDS at T_i = g0 + 60 i (Kalshi's
end_period_ts), so the quote at index i is known at time T_i. Values are carried forward from the last candle for at most
MAX_AGE minutes (older -> None). Minutes at or after the market's close are dropped (a fill must happen before close).

Grid arrays: ask[i], bid[i] (None when stale/missing), vol[i] (contracts traded in that minute, 0 when no candle),
fresh[i] (a candle ended exactly at T_i), mid[i] (only when 0 < bid and ask < 1, i.e. both sides quoted).
The data split is per data family (lab.kalshi.calib.FAMILY): events ordered by close time, discovery = first 70%."""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.fetch import PLAN, MK, CD, window
from lab.kalshi.calib import FAMILY

MAX_AGE = 30
SPLIT = 0.7


class Grid:
    __slots__ = ("t", "e", "series", "fam", "close", "exp", "won", "type", "floor", "cap", "sub", "g0", "ask", "bid", "vol", "fresh", "mid", "n", "alow", "bhigh")

    def __init__(self, m: dict, fam: str, c: list[list], w_end: int):
        self.t, self.e, self.series, self.fam = m["t"], m["e"], m["series"], fam
        self.close, self.exp, self.won = m["close"], m["exp"], m["result"] == "yes"
        self.type, self.floor, self.cap, self.sub = m.get("type"), m.get("floor"), m.get("cap"), m.get("sub")
        c = [r for r in c if r[0] < self.close]           # quotes strictly before close
        self.g0 = (c[0][0] // 60) * 60 if c else 0
        # the grid runs to the last whole minute that ends at least 60 s before close (and inside the fetched window), so a
        # fill at grid index i+1 is always a quote observed a full minute before the market closed
        end = min(self.close - 60, w_end)
        n = (end - self.g0) // 60 + 1 if c and end >= self.g0 else 0
        self.n = n
        ask = [None] * n; bid = [None] * n; vol = [0.0] * n; fresh = [False] * n; alow = [None] * n; bhigh = [None] * n
        by = {}
        for r in c:
            if r[0] <= end:
                by[(r[0] - self.g0) // 60] = r
        last = None; age = 10 ** 9
        for i in range(n):
            r = by.get(i)
            if r is not None:
                last = r; age = 0; fresh[i] = True; vol[i] = r[5] or 0.0; alow[i] = r[3]; bhigh[i] = r[4]
            else:
                age += 1
            if last is not None and age <= MAX_AGE and last[1] is not None and last[2] is not None:
                ask[i], bid[i] = last[1], last[2]
        self.ask, self.bid, self.vol, self.fresh, self.alow, self.bhigh = ask, bid, vol, fresh, alow, bhigh
        self.mid = [(a + b) / 2 if a is not None and b is not None and b > 0 and a < 1 and a > b else None for a, b in zip(ask, bid)]

    def T(self, i: int) -> int:
        return self.g0 + 60 * i


def load_series(s: str) -> list[Grid]:
    mf, cf = MK / f"{s}.jsonl", CD / f"{s}.jsonl"
    if not (mf.exists() and cf.exists()):
        return []
    meta = {}
    for l in mf.open():
        m = json.loads(l)
        if m.get("close") and m.get("result") in ("yes", "no"):
            meta[m["t"]] = m
    out = []
    seen = set()
    for line in cf.open():
        x = json.loads(line)
        m = meta.get(x["t"])
        if not m or not x["c"] or x["t"] in seen:
            continue
        seen.add(x["t"])
        w = window(m, PLAN[s])
        if not w:
            continue
        g = Grid(m, FAMILY[s], x["c"], w[1])
        if g.n >= 2:
            out.append(g)
    return out


def cutoffs(grids_by_fam: dict[str, list[Grid]]) -> dict[str, int]:
    """Per data family: the close time of the event at the 70% position (events ordered by close time).
    Discovery = events closing before the cutoff, validation = at/after."""
    cut = {}
    for fam, gs in grids_by_fam.items():
        ev = {}
        for g in gs:
            ev[g.e] = max(ev.get(g.e, 0), g.close)
        closes = sorted(ev.values())
        cut[fam] = closes[int(len(closes) * SPLIT)] if closes else 0
    return cut


def event_close(gs: list[Grid]) -> dict[str, int]:
    ev = {}
    for g in gs:
        ev[g.e] = max(ev.get(g.e, 0), g.close)
    return ev


ALL_SERIES = list(FAMILY)
