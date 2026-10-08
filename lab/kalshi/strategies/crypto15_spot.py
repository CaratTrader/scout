"""crypto15_spot: does a spot model (Coinbase 1-minute candles -> Brownian / fat-tailed probability that the final 60-s
average ends above the 15-minute window's reference price) beat Kalshi's KX{BTC,ETH,SOL,XRP,DOGE}15M prices after
fees, when the trader reacts one minute late?

Rows: one per (market, decision time t = close - tau minutes, tau = 2..14). Everything in a row's decision features is
known at t (Coinbase candles that closed <= t, the market's reference price published at open, the Kalshi quote of the
minute ending at t). The fill is the Kalshi quote of the minute ending at t + 60 s (YES at yes_ask, NO at 1 - yes_bid),
skipped when older than 30 minutes; fee 0.07 p (1-p) per contract, rounded up to the cent per order.

Usage:
  python -m lab.kalshi.strategies.crypto15_spot build      # rows -> data/kalshi_lab/strategies/crypto15_spot/rows.jsonl
  python -m lab.kalshi.strategies.crypto15_spot explore    # discovery-only diagnostics (calibration, model vs market)
  python -m lab.kalshi.strategies.crypto15_spot discover   # rule grid on discovery, counts variants
  python -m lab.kalshi.strategies.crypto15_spot validate   # frozen candidates, run once on validation
"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import quote
from lab.kalshi.strategies.crypto15_spot_data import load, SERIES

OUT = Path("data/kalshi_lab/strategies/crypto15_spot")
ROWS = OUT / "rows.jsonl"
N01 = NormalDist()
TAUS = tuple(range(2, 15))
SPLIT = 0.7


def fee_order(p: float, n: int = 10) -> float:
    """Kalshi taker fee per contract for an n-contract order (rounded up to the cent per order)."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def t4_cdf(x: float) -> float:
    """Student-t (nu=4) CDF, closed form."""
    u = x * x / 4
    return 0.5 + 0.375 * x / math.sqrt(1 + u) * (1 - x * x / (12 * (1 + u)))


def p_t4(z: float) -> float:
    """P(up) when the standardised move is Student-t(4) rescaled to unit variance."""
    return t4_cdf(z * math.sqrt(2))


def spot_at(cb: dict, t: int) -> float | None:
    """Coinbase close of the latest 1-minute candle that ended at or before t (candle key = minute start)."""
    for back in range(0, 4):
        k = cb.get(t - 60 - 60 * back)
        if k:
            return k[3]
    return None


def realised_sigma(cb: dict, t: int, mins: int) -> float | None:
    """Std of 1-minute log returns over the last `mins` minutes ending at t (per minute)."""
    closes = [cb[m][3] for m in range(t - 60 * (mins + 1), t, 60) if m in cb]
    if len(closes) < max(10, mins // 2):
        return None
    r = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    return math.sqrt(sum(x * x for x in r) / len(r))  # zero-mean estimator


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = ROWS.open("w"); n = 0
    for asset, s in SERIES.items():
        cb = load(asset)
        ms = sorted((json.loads(l) for l in open(f"data/kalshi_lab/markets/{s}.jsonl")), key=lambda m: m["close"])
        cd = {}
        for line in open(f"data/kalshi_lab/candles/{s}.jsonl"):
            x = json.loads(line); cd[x["t"]] = x["c"]
        basis_hist: list[float] = []
        for m in ms:
            k = cb.get(m["open"] - 60)
            if k:  # the reference (60-s BRTI average before open) vs Coinbase over the same minute: known at open
                basis_hist.append(math.log(m["floor"] / ((k[0] + k[3]) / 2)))
            c = cd.get(m["t"])
            if not c or not m.get("floor"):
                continue
            basis = st.median(basis_hist[-16:]) if basis_hist else 0.0
            for tau in TAUS:
                t = m["close"] - tau * 60
                S = spot_at(cb, t)
                s15, s60, s240 = (realised_sigma(cb, t, w) for w in (15, 60, 240))
                if not S or not s60:
                    continue
                q0 = quote(c, t); q1 = quote(c, t + 60); qm = quote(c, t - 60)
                S1 = spot_at(cb, t - 60); S3 = spot_at(cb, t - 180)
                d = math.log(S / m["floor"]) + basis
                row = {"a": asset, "s": s, "tk": m["t"], "e": m["e"], "close": m["close"], "tau": tau, "res": 1 if m["result"] == "yes" else 0,
                       "d": d, "s15": s15, "s60": s60, "s240": s240, "basis": basis,
                       "r1": math.log(S / S1) if S1 else None, "r3": math.log(S / S3) if S3 else None,
                       "q0": q0, "q1": q1, "qm": qm, "vol": m.get("vol")}
                out.write(json.dumps(row) + "\n"); n += 1
    out.close(); print("rows", n)


def load_rows() -> list[dict]:
    return [json.loads(l) for l in ROWS.open()]


def split_cut(rows: list[dict]) -> int:
    closes = sorted({r["close"] for r in rows})
    return closes[int(len(closes) * SPLIT)]


def p_model(r: dict, sig: str = "s60", dist: str = "n", basis: bool = True, k: float = 1.0) -> float:
    """Model probability that the final 60-s average ends >= the reference. k scales the volatility."""
    sg = r.get(sig) or r["s60"]
    if sig == "blend":
        sg = math.sqrt((r["s15"] ** 2 + r["s60"] ** 2 + (r["s240"] or r["s60"]) ** 2) / 3) if r["s15"] else r["s60"]
    var = (k * sg) ** 2 * (r["tau"] - 2 / 3) + (1.5e-4) ** 2   # remaining Brownian variance of the final average + basis noise
    d = r["d"] if basis else r["d"] - r["basis"]
    z = d / math.sqrt(var)
    return N01.cdf(z) if dist == "n" else p_t4(z)


def stats(tr: list[dict], key: str = "e") -> dict:
    """Equal-$ return per trade, t clustered by `key` (event; or close time = all assets of one window)."""
    if not tr:
        return {"n": 0}
    ev = defaultdict(list)
    for x in tr:
        ev[x[key]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    sd = st.pstdev(em)
    rs = sorted((x["ret"] for x in tr), reverse=True)
    return {"n": len(tr), "events": len(em), "win": round(sum(x["won"] for x in tr) / len(tr), 4), "avg_px": round(st.mean(x["px"] for x in tr), 4),
            "ret": round(st.mean(x["ret"] for x in tr), 4), "t": round(st.mean(em) / (sd / math.sqrt(len(em))), 2) if len(em) > 2 and sd > 0 else float("nan"),
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else float("nan")}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()
    else:
        from lab.kalshi.strategies import crypto15_spot_research as R
        getattr(R, cmd)()
