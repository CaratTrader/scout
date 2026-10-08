"""r2_launch_window step 2a: which daily product types launched inside the live /markets window (since ~2026-08-01)?
One call per candidate: settled markets closing before 2026-09-10 (live window). Empty -> launched after 09-10 (or
inactive); earliest close well after 08-01 -> launch date; earliest close at the window edge -> older product.
Candidates = daily-cadence, non-sports series active in data/kalshi_lab/survey.json (Oct 5-7) that are not known
round-1 series. Output: data/kalshi_lab/strategies/r2_launch_window/launch_probe.json
Usage: .venv/bin/python -m lab.kalshi.strategies.r2_launch_window_discover [SERIES ...]"""
from __future__ import annotations
import datetime as dt, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies.r2_launch_window_api import kget, used, OUT

CAND = ["KXAAAGASDFL", "KXTXERCOTPEAKD", "KXSOFRD", "KXCOPPERD", "KXBRENTD", "KXSILVERD", "KXSHIBAD", "KXSHIBA", "KXDIESELD",
        "KXTRUEV", "KXTRUTHSOCIALD", "KXINXDUD", "KXUST10AD", "KXUSDCADAD", "KXCS2MAP", "KXVHGCB", "KXTRUMPAPPROVE"]
MAXC = int(dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc).timestamp())


def iso(s):
    return s[:16] if s else None


def probe(series: list[str]) -> dict:
    f = OUT / "launch_probe.json"
    res = json.loads(f.read_text()) if f.exists() else {}
    for s in series:
        d = kget(f"/markets?series_ticker={s}&status=settled&max_close_ts={MAXC}&limit=1000")
        ms = d.get("markets") or []
        closes = sorted(m["close_time"] for m in ms if m.get("close_time"))
        opens = sorted(m["open_time"] for m in ms if m.get("open_time"))
        res[s] = {"n_before_0910": len(ms), "cursor": bool(d.get("cursor")), "first_close": iso(closes[0]) if closes else None,
                  "last_close": iso(closes[-1]) if closes else None, "first_open": iso(opens[0]) if opens else None,
                  "events": len({m["event_ticker"] for m in ms})}
        print(s, res[s], "| calls", used(), flush=True)
    f.write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    probe(sys.argv[1:] or CAND)
