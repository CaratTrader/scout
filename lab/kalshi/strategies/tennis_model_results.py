"""Kalshi tennis match results (names from yes_sub_title) for an Elo burn-in, newest first, back to a start date.
Recent (/markets?status=settled, ~68 days) plus archive (/historical/markets, settled before 2026-08-08).
Output: data/kalshi_lab/strategies/tennis_model/results/<series>.jsonl (one market per line).
Usage: python -m lab.kalshi.strategies.tennis_model_results 2026-05-01 [max_calls_per_series]"""
from __future__ import annotations
import datetime as dt, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.tennis_model_fetch import kget, calls, OUT
from lab.kalshi.strategies.tennis_model_data import SERIES

RES = OUT / "results"


def ts(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def compact(m: dict) -> dict:
    rp = m.get("rules_primary") or ""
    mt = re.search(r"match in the (.+?) after a ball", rp)
    return {"t": m["ticker"], "e": m["event_ticker"], "open": ts(m.get("open_time")), "close": ts(m.get("close_time")),
            "exp": ts(m.get("expected_expiration_time")), "result": m.get("result"), "sub": m.get("yes_sub_title"),
            "settle": m.get("settlement_value_dollars"), "vol": float(m.get("volume_fp") or 0), "tour": mt.group(1) if mt else ""}


def run(start: str, max_per_series: int = 25) -> None:
    RES.mkdir(parents=True, exist_ok=True)
    t_start = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp())
    for s in SERIES:
        f = RES / f"{s}.jsonl"
        have = {json.loads(l)["t"]: json.loads(l) for l in f.open()} if f.exists() else {}
        n0 = calls(); oldest = None
        lab_mk = Path(f"data/kalshi_lab/markets/{s}.jsonl")
        t_end = min(json.loads(l)["close"] for l in lab_mk.open()) if lab_mk.exists() else int(dt.datetime.now().timestamp())
        for path in (f"/markets?series_ticker={s}&status=settled&limit=1000&min_close_ts={t_start}&max_close_ts={t_end}", f"/historical/markets?series_ticker={s}&limit=1000"):
            cursor = ""
            while calls() - n0 < max_per_series:
                d = kget(path + (f"&cursor={cursor}" if cursor else ""))
                ms = d.get("markets") or []
                for m in ms:
                    c = compact(m)
                    if c["close"] and c["close"] >= t_start:
                        have[c["t"]] = c
                    oldest = min(oldest or 10**12, c["close"] or 10**12)
                cursor = d.get("cursor") or ""
                if not cursor or not ms or min((ts(m.get("close_time")) or 10**12) for m in ms) < t_start:
                    break
        f.write_text("".join(json.dumps(m) + "\n" for m in sorted(have.values(), key=lambda m: (m["close"] or 0, m["t"]))))
        print(s, "markets", len(have), "oldest", dt.datetime.utcfromtimestamp(min(m["close"] for m in have.values())) if have else None,
              "calls used", calls() - n0, "total", calls(), flush=True)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "2026-05-01", int(sys.argv[2]) if len(sys.argv) > 2 else 25)
