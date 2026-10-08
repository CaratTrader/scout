"""arb_ladder live checks (read-only public REST, paced through lab.us.data_refresh.fetch; never places orders).

  snap   : one /markets call per series (weather daily-high, monotone ladders, BTC/ETH range) -> raw JSON + an analysis of
           every open ladder: overround (sum of asks - 1), sum of bids, best monotone gap min(ask(K1) - bid(K2)), touch sizes.
  poll N : N rounds of batched /markets?tickers=... over the weather ladders found by the last snap (persistence check).
  cross N: N snapshots of KXBTC (range) + KXBTCD (above) [and ETH] for the nearest common close: cross-series consistency.
Every call is counted in live/calls.json (budget 200 for the whole task)."""
from __future__ import annotations
import datetime as dt, json, math, sys, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

LIVE = Path("data/kalshi_lab/strategies/arb_ladder/live"); LIVE.mkdir(parents=True, exist_ok=True)
CALLS = LIVE / "calls.json"
WX_SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHTSFO", "KXHIGHTBOS", "KXHIGHTDC", "KXHIGHPHIL", "KXHIGHTATL",
             "KXHIGHDEN", "KXHIGHAUS", "KXHIGHTDAL", "KXHIGHTMIN", "KXHIGHTPHX", "KXHIGHTSEA", "KXHIGHTLV", "KXHIGHTSAN", "KXHIGHTHOU",
             "KXHIGHTOKC", "KXHIGHTSATX", "KXHIGHTNOLA", "KXHIGHTEWR", "KXHIGHTTTN", "KXHIGHTSDF"]
MONO_SERIES = ["KXBTCD", "KXETHD", "KXINXU", "KXNASDAQ100U", "KXGOLDD", "KXWTI", "KXNATGASD", "KXAAAGASD"]
FEE = 0.07
fee = lambda p: FEE * p * (1 - p)


def call(url: str) -> dict:
    n = json.loads(CALLS.read_text()) if CALLS.exists() else {"n": 0, "log": []}
    if n["n"] >= 190:
        raise SystemExit("Kalshi call budget reached")
    txt = fetch(url, pace=1.15)
    n["n"] += 1; n["log"].append([int(time.time()), url[:160]]); CALLS.write_text(json.dumps(n))
    try:
        return json.loads(txt or "{}")
    except Exception:
        return {}


def f(x):
    try:
        return float(x)
    except Exception:
        return None


def norm(m: dict) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "type": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
            "ask": f(m.get("yes_ask_dollars")), "bid": f(m.get("yes_bid_dollars")), "ask_sz": f(m.get("yes_ask_size_fp")),
            "bid_sz": f(m.get("yes_bid_size_fp")), "close": m.get("close_time"), "status": m.get("status"), "vol24": f(m.get("volume_24h_fp"))}


def list_open(series: str, extra: str = "") -> list[dict]:
    out = []; cursor = ""
    while True:
        d = call(f"{K}/markets?series_ticker={series}&status=open&limit=1000{extra}" + (f"&cursor={cursor}" if cursor else ""))
        out += [norm(m) for m in d.get("markets") or []]
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets"):
            return out


def excl_view(mk: list[dict]) -> dict:
    """Exhaustive ladder: cost to buy every YES, and the all-NO edge."""
    asks = [m["ask"] for m in mk]; bids = [m["bid"] for m in mk]
    have_all_asks = all(a is not None and 0.01 <= a <= 0.99 for a in asks)
    ey = 1 - sum(a + fee(a) for a in asks) if have_all_asks else None
    S = [m for m in mk if m["bid"] and 0.01 <= m["bid"] <= 0.99 and m["bid"] - fee(1 - m["bid"]) > 0]
    en = sum(m["bid"] - fee(1 - m["bid"]) for m in S) - 1 if len(S) >= 2 else None
    # size of the arb = min touch size over the legs
    sz_y = min((m["ask_sz"] or 0) for m in mk) if ey is not None else None
    sz_n = min((m["bid_sz"] or 0) for m in S) if en is not None else None
    return {"n": len(mk), "sum_ask": sum(a for a in asks if a is not None), "sum_bid": sum(b for b in bids if b is not None),
            "edge_allyes": ey, "edge_allno": en, "size_allyes": sz_y, "size_allno": sz_n,
            "touch_ask_sz": [m["ask_sz"] for m in mk], "touch_bid_sz": [m["bid_sz"] for m in mk]}


def mono_view(mk: list[dict]) -> dict:
    mk = sorted([m for m in mk if m["floor"] is not None], key=lambda m: m["floor"])
    best = None; mincost = 9; jmin = None; viol = 0; gap = None
    for j, m in enumerate(mk):
        b = m["bid"]
        if b and 0.01 <= b <= 0.99 and jmin is not None:
            edge = 1 - mincost - ((1 - b) + fee(1 - b))
            g = mk[jmin]["ask"] - b
            gap = g if gap is None else min(gap, g)
            if edge > 0:
                viol += 1
            if best is None or edge > best[0]:
                best = (edge, mk[jmin]["t"], m["t"], mk[jmin]["ask"], b, mk[jmin]["ask_sz"], m["bid_sz"])
        a = m["ask"]
        if a and 0.01 <= a <= 0.99 and a + fee(a) < mincost:
            mincost, jmin = a + fee(a), j
    live = [m for m in mk if m["bid"] and m["ask"] and 0.05 <= (m["bid"] + m["ask"]) / 2 <= 0.95]
    return {"n": len(mk), "violating_pairs_(K2_with_edge>0)": viol, "best_edge": best[0] if best else None, "best_pair": best,
            "min_gap_ask_low_minus_bid_high": gap, "n_live_strikes_5_95": len(live),
            "median_touch_ask_sz_live": sorted(m["ask_sz"] or 0 for m in live)[len(live) // 2] if live else None,
            "median_touch_bid_sz_live": sorted(m["bid_sz"] or 0 for m in live)[len(live) // 2] if live else None,
            "median_spread_live": sorted(m["ask"] - m["bid"] for m in live)[len(live) // 2] if live else None}


def snap(groups: str = "wx,mono") -> dict:
    ts = int(time.time()); res = {"ts": ts, "wx": {}, "mono": {}}
    raw = {}
    if "wx" in groups:
        for s in WX_SERIES:
            mk = list_open(s); raw[s] = mk
            ev = defaultdict(list)
            for m in mk:
                ev[m["e"]].append(m)
            for e, v in ev.items():
                res["wx"][e] = excl_view(v)
    if "mono" in groups:
        for s in MONO_SERIES:
            mk = list_open(s); raw[s] = mk
            ev = defaultdict(list)
            for m in mk:
                ev[m["e"]].append(m)
            for e, v in ev.items():
                res["mono"][e] = mono_view(v)
    (LIVE / f"snap_{ts}.json").write_text(json.dumps({"analysis": res, "raw": raw}))
    return res


def poll(rounds: int, gap_s: float = 20) -> list:
    snaps = sorted(LIVE.glob("snap_*.json"))
    raw = json.loads(snaps[-1].read_text())["raw"]
    tick = [m["t"] for s in WX_SERIES for m in raw.get(s, [])]
    chunks = [tick[i:i + 100] for i in range(0, len(tick), 100)]
    hist = []
    for r in range(rounds):
        t0 = time.time(); mk = []
        for ch in chunks:
            d = call(f"{K}/markets?tickers={','.join(ch)}&limit=1000")
            mk += [norm(m) for m in d.get("markets") or []]
        ev = defaultdict(list)
        for m in mk:
            if m["status"] in ("active", "open", None):
                ev[m["e"]].append(m)
        row = {"ts": int(t0), "events": {}}
        for e, v in ev.items():
            if len(v) == 6:
                x = excl_view(v); row["events"][e] = {k: x[k] for k in ("sum_ask", "sum_bid", "edge_allyes", "edge_allno", "size_allyes", "size_allno")}
        hist.append(row)
        arbs = {e: x for e, x in row["events"].items() if (x["edge_allyes"] or -1) > 0 or (x["edge_allno"] or -1) > 0}
        print(f"round {r}: {len(row['events'])} ladders, arbs {list(arbs)}", flush=True)
        with (LIVE / "poll.jsonl").open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        time.sleep(max(0, gap_s - (time.time() - t0)))
    return hist


def cross(rounds: int, gap_s: float = 30, pairs=(("KXBTC", "KXBTCD"), ("KXETH", "KXETHD"))) -> None:
    """Range buckets (KXBTC) vs above-K (KXBTCD) at the same close. With buckets B_j = [lo_j, hi_j] (plus tails),
    'above K' = union of buckets with lo_j >= K when K sits on a bucket edge. Riskless combos:
      (i)  YES(above K) + YES(every bucket entirely below K)   pays >= 1
      (ii) NO(above K)  + YES(every bucket entirely above K)   pays >= 1
    Reported: best edge over all aligned K, and how often aligned K exist."""
    for r in range(rounds):
        t0 = time.time(); out = {"ts": int(t0)}
        for rs, ab in pairs:
            R = list_open(rs); A = list_open(ab)
            byc = defaultdict(lambda: {"R": [], "A": []})
            for m in R:
                byc[m["close"]]["R"].append(m)
            for m in A:
                byc[m["close"]]["A"].append(m)
            for c, g in byc.items():
                if not g["R"] or not g["A"]:
                    continue
                res = cross_view(g["R"], g["A"])
                out[f"{rs}|{c}"] = res
                print(f"{rs} close {c}: range mkts {len(g['R'])} above mkts {len(g['A'])} aligned K {res['aligned']} best edge {res['best']}", flush=True)
        with (LIVE / "cross.jsonl").open("a") as fh:
            fh.write(json.dumps(out) + "\n")
        time.sleep(max(0, gap_s - (time.time() - t0)))


def cross_view(R: list[dict], A: list[dict]) -> dict:
    # range bucket bounds: between -> [floor, cap]; less -> (-inf, cap); greater -> (floor, inf)
    B = []
    for m in R:
        lo = -math.inf if m["type"] == "less" else m["floor"]; hi = math.inf if m["type"] == "greater" else m["cap"]
        if lo is None or hi is None:
            continue
        B.append((lo, hi, m))
    B.sort(key=lambda x: x[0])
    best = None; aligned = 0
    for a in A:
        K = a["floor"]
        if K is None:
            continue
        below = [b for b in B if b[1] <= K + 1e-6]; above = [b for b in B if b[0] >= K - 1e-6]
        # alignment: every bucket is entirely below or entirely above K, and the buckets cover the whole line
        if len(below) + len(above) != len(B) or not below or not above:
            continue
        aligned += 1
        cy = sum(b[2]["ask"] + fee(b[2]["ask"]) if b[2]["ask"] and b[2]["ask"] <= 0.99 else 9 for b in below)
        cy2 = sum(b[2]["ask"] + fee(b[2]["ask"]) if b[2]["ask"] and b[2]["ask"] <= 0.99 else 9 for b in above)
        if a["ask"] and a["ask"] <= 0.99:
            e1 = 1 - (a["ask"] + fee(a["ask"])) - cy
            if best is None or e1 > best[0]:
                best = (round(e1, 4), "YES_above+YES_below_buckets", a["t"], a["ask"], round(cy, 3))
        if a["bid"] and a["bid"] >= 0.01:
            e2 = 1 - ((1 - a["bid"]) + fee(1 - a["bid"])) - cy2
            if best is None or e2 > best[0]:
                best = (round(e2, 4), "NO_above+YES_above_buckets", a["t"], a["bid"], round(cy2, 3))
    return {"aligned": aligned, "best": best, "n_range": len(B)}


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "snap":
        r = snap(a[1] if len(a) > 1 else "wx,mono")
        wx = r["wx"]
        print("weather ladders:", len(wx))
        for e, x in sorted(wx.items()):
            print(f"  {e:24s} n={x['n']} sum_ask={x['sum_ask']:.2f} sum_bid={x['sum_bid']:.2f} allyes={x['edge_allyes']} allno={x['edge_allno']} "
                  f"ask_sz={x['touch_ask_sz']} bid_sz={x['touch_bid_sz']}")
        for e, x in sorted(r["mono"].items()):
            print(f"  {e:28s} {json.dumps(x)}")
    elif a[0] == "poll":
        poll(int(a[1]), float(a[2]) if len(a) > 2 else 20)
    elif a[0] == "cross":
        cross(int(a[1]), float(a[2]) if len(a) > 2 else 30)


# ------------------------------------------------------------------ cross-series, simultaneous (one batched call per round)
def cross_arbs(R: list[dict], A: list[dict], max_s: int = 6) -> list[tuple]:
    """Riskless 3+-leg combos between range buckets R and above-K strikes A of the same close.
    For aligned K1 < K2 with S = buckets inside [K1, K2) (|S| <= max_s):
      A: YES(>=K1) + NO(>=K2) + NO(each bucket in S)   pays 1 + |S|   (K2 may be +inf: no NO(>=K2) leg, pays |S|)
      B: NO(>=K1)  + YES(>=K2) + YES(each bucket in S) pays exactly 1 (K2 = +inf: no YES(>=K2) leg)
    Returns [(edge_per_set, kind, legs[(ticker, side, px, size)])] for edge > -0.02."""
    B = []
    for m in R:
        lo = -math.inf if m["type"] == "less" else m["floor"]; hi = math.inf if m["type"] == "greater" else m["cap"]
        if lo is not None and hi is not None:
            B.append((lo, hi, m))
    B.sort(key=lambda x: x[0])
    Ks = sorted([(a["floor"], a) for a in A if a["floor"] is not None], key=lambda x: x[0])
    out = []
    okA = lambda p: p is not None and 0.01 <= p <= 0.99
    def idx_edge(K):   # index of the first bucket starting at K (bucket lo == K rounded up to the cent)
        for i, (lo, hi, m) in enumerate(B):
            if abs(lo - K) < 0.02:
                return i
        return None
    for i1, (K1, a1) in enumerate(Ks):
        j = idx_edge(K1)
        if j is None:
            continue
        cands = [(Ks[i2][0], Ks[i2][1]) for i2 in range(i1 + 1, len(Ks))] + [(math.inf, None)]
        for K2, a2 in cands:
            S = [b for b in B[j:] if b[1] <= K2 + 0.02] if K2 < math.inf else B[j:]
            if not S or len(S) > max_s:
                continue
            # S must tile [K1, K2) exactly: contiguous buckets (only polled buckets are known), ending on K2 / the top tail
            if any(abs(b2[0] - (b1[1] + 0.01)) > 0.02 for b1, b2 in zip(S, S[1:])):
                continue
            if K2 < math.inf and abs(S[-1][1] - K2) > 0.02:
                continue
            if K2 == math.inf and S[-1][1] != math.inf:
                continue
            # A
            legs = [(a1["t"], "YES", a1["ask"], a1["ask_sz"])] + ([(a2["t"], "NO", 1 - a2["bid"] if a2["bid"] else None, a2["bid_sz"])] if a2 else []) + \
                   [(b[2]["t"], "NO", 1 - b[2]["bid"] if b[2]["bid"] else None, b[2]["bid_sz"]) for b in S]
            if all(okA(p) for _, _, p, _ in legs):
                pay = len(S) + (1 if a2 else 0)
                out.append((pay - sum(p + fee(p) for _, _, p, _ in legs), "A", legs))
            legs = [(a1["t"], "NO", 1 - a1["bid"] if a1["bid"] else None, a1["bid_sz"])] + ([(a2["t"], "YES", a2["ask"], a2["ask_sz"])] if a2 else []) + \
                   [(b[2]["t"], "YES", b[2]["ask"], b[2]["ask_sz"]) for b in S]
            if all(okA(p) for _, _, p, _ in legs):
                out.append((1 - sum(p + fee(p) for _, _, p, _ in legs), "B", legs))
    return sorted([o for o in out if o[0] > -0.02], key=lambda o: -o[0])


def cross2(rounds: int, gap_s: float = 15, pairs=(("KXBTC", "KXBTCD"), ("KXETH", "KXETHD")), per_series: int = 24) -> None:
    """Pick the nearest common close, keep near-money tickers of both series, then poll them together in ONE call per
    round (simultaneous quotes)."""
    tick = []
    for rs, ab in pairs:
        R = list_open(rs); A = list_open(ab)
        soon = dt.datetime.utcfromtimestamp(time.time() + 600).strftime("%Y-%m-%dT%H:%M:%SZ")
        closes = sorted(c for c in {m["close"] for m in R} & {m["close"] for m in A} if c > soon)
        if not closes:
            continue
        c = closes[0]
        for grp in (R, A):
            g = [m for m in grp if m["close"] == c]
            g.sort(key=lambda m: abs(((m["bid"] or 0) + (m["ask"] or 1)) / 2 - (0.5 if grp is A else 0.25)))
            tick += [m["t"] for m in g[:per_series]]
        print(f"{rs}/{ab} close {c}: polling {per_series}+{per_series} near-money tickers", flush=True)
    for r in range(rounds):
        t0 = time.time()
        d = call(f"{K}/markets?tickers={','.join(tick)}&limit=1000")
        mk = [norm(m) for m in d.get("markets") or []]
        row = {"ts": int(t0), "n": len(mk), "best": {}}
        for rs, ab in pairs:
            R = [m for m in mk if m["t"].startswith(rs + "-")]; A = [m for m in mk if m["t"].startswith(ab + "-")]
            arbs = cross_arbs(R, A)
            row["best"][rs] = arbs[:3]
            # within-series monotone check on the polled strikes too
            row["mono_" + ab] = mono_view(A)["best_edge"]
        row["raw"] = [[m["t"], m["type"], m["floor"], m["cap"], m["ask"], m["bid"], m["ask_sz"], m["bid_sz"]] for m in mk]
        with (LIVE / "cross2.jsonl").open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"round {r} n={row['n']} " + " ".join(f"{k}: {(v[0][0], v[0][1], len(v[0][2])) if v else None}" for k, v in row["best"].items()), flush=True)
        time.sleep(max(0, gap_s - (time.time() - t0)))


if __name__ == "__main__" and sys.argv[1] == "cross2":
    cross2(int(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 15)
