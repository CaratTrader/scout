"""Data for r2_infra_timestamp_fill_audit (all Kalshi calls counted + cached via r2_infra_timestamp_fill_audit_api):

  live_prints   - /markets/trades for every ticker of the live recording over [first snapshot - 300 s, last + 120 s],
                  paged by cursor up to MAX_PAGES[series] pages (newest first) -> prints_live.json {ticker: [raw]}
  live_candles  - one /markets/candlesticks batch (period 1 min) for the same tickers and window -> candles_live.json
  inxu          - prints for a few settled KXINXU markets whose 1-minute candles are on disk (candle-alignment check on
                  a fourth series) -> prints_inxu.json
  commod        - prints for KXGOLDD / KXWTI / KXNATGASD markets over windows where r2_daily_commod_maker has 1-minute
                  candles with trade prints (fill-model calibration for the commodity maker) -> prints_commod.json
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_infra_timestamp_fill_audit_data live|inxu|commod [n]"""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_api import kget, OUT, used

MAX_PAGES = {"KXBTC15M": 9, "KXBTCD": 3}


def prints(tk: str, lo: int, hi: int, max_pages: int = 1) -> tuple[list[dict], int]:
    """All prints in [lo, hi] (up to max_pages x 1000, newest first). Returns (prints, oldest second fully covered)."""
    out = []; cur = None
    for _ in range(max_pages):
        d = kget(f"/markets/trades?ticker={tk}&min_ts={lo}&max_ts={hi}&limit=1000" + (f"&cursor={cur}" if cur else ""))
        out += d.get("trades") or []; cur = d.get("cursor")
        if not cur or not d.get("trades"):
            return out, lo
    return out, None   # truncated: coverage starts at the oldest print returned


def live() -> None:
    rows = [json.loads(l) for l in (OUT / "live.jsonl").open()]
    tks = sorted({r["tk"] for r in rows if r["v"] == "M" and r.get("status") not in ("initialized",)})
    lo = int(min(r["t0"] for r in rows)) - 300; hi = int(max(r["t1"] for r in rows)) + 120
    res = {}; cov = {}
    for tk in tks:
        p, c = prints(tk, lo, hi, MAX_PAGES.get(tk.split("-")[0], 1)); res[tk] = p
        cov[tk] = [c if c is not None else (min(x["created_time"] for x in p) if p else lo), hi]
        print(tk, len(p), cov[tk], "calls", used(), flush=True)
    (OUT / "prints_live.json").write_text(json.dumps({"lo": lo, "hi": hi, "coverage": cov, "prints": res}))
    d = kget(f"/markets/candlesticks?market_tickers={','.join(tks)}&start_ts={lo - 600}&end_ts={hi + 120}&period_interval=1")
    (OUT / "candles_live.json").write_text(json.dumps(d))
    print("candles", len(d.get("markets", [])), "calls", used())


def inxu(n: int = 4) -> None:
    """Prints for the n highest-volume settled KXINXU markets of the latest events on disk (outcome-blind: volume)."""
    ROOT = OUT.parents[3]
    ms = [json.loads(l) for l in (ROOT / "data/kalshi_lab/markets/KXINXU.jsonl").open()]
    cand = {json.loads(l)["t"] for l in (ROOT / "data/kalshi_lab/candles/KXINXU.jsonl").open()}
    ms = [m for m in ms if m["t"] in cand]
    evs = sorted({m["e"] for m in ms}, key=lambda e: -max(x["close"] for x in ms if x["e"] == e))[:6]
    pick = sorted([m for m in ms if m["e"] in evs], key=lambda m: -m["vol"])[:n]
    res = {}
    for m in pick:
        lo, hi = m["close"] - 70 * 60, m["close"]
        p, c = prints(m["t"], lo, hi, 1)
        res[m["t"]] = {"prints": p, "lo": c if c is not None else min(x["created_time"] for x in p), "hi": hi}
        if isinstance(res[m["t"]]["lo"], str):
            from lab.kalshi.strategies.r2_infra_timestamp_fill_audit_fill import ts
            res[m["t"]]["lo"] = ts(res[m["t"]]["lo"])
        print(m["t"], m["vol"], len(p), "calls", used(), flush=True)
    (OUT / "prints_inxu.json").write_text(json.dumps(res))


if __name__ == "__main__":
    a = sys.argv[1:]
    {"live": live, "inxu": inxu}[a[0]]()
