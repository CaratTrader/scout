"""Which Kalshi series settle often and trade enough to test strategies quickly? Lists every market settled in the
last N days (multivariate combos excluded) and aggregates count / volume by series. Kalshi calls are synced to the
paper bot's idle window (lab.us.data_refresh.fetch). Output: data/kalshi_lab/survey.json
Usage: python -m lab.kalshi.survey [days]"""
from __future__ import annotations
import collections, datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab"); OUT.mkdir(parents=True, exist_ok=True)


def main(days: float = 2.0) -> None:
    min_close = int(time.time() - days * 86400); cursor = ""; agg = collections.defaultdict(lambda: {"n": 0, "vol": 0.0, "events": set()}); pages = 0
    while True:
        d = json.loads(fetch(f"{K}/markets?status=settled&min_close_ts={min_close}&mve_filter=exclude&limit=1000" + (f"&cursor={cursor}" if cursor else ""), pace=1.15) or "{}")
        pages += 1
        for m in d.get("markets") or []:
            s = m["event_ticker"].split("-")[0]; a = agg[s]
            a["n"] += 1; a["vol"] += float(m.get("volume_fp") or m.get("volume") or 0); a["events"].add(m["event_ticker"])
        cursor = d.get("cursor") or ""
        if not cursor or not d.get("markets") or pages > 400:
            break
    rows = sorted(({"series": s, "markets": a["n"], "events": len(a["events"]), "volume": round(a["vol"])} for s, a in agg.items()), key=lambda r: -r["volume"])
    (OUT / "survey.json").write_text(json.dumps({"days": days, "pages": pages, "rows": rows}, indent=0))
    print("pages", pages, "series", len(rows), "markets", sum(r["markets"] for r in rows))
    for r in rows[:60]:
        print(f"{r['series']:28s} events={r['events']:5d} markets={r['markets']:6d} volume={r['volume']:>12,}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 2.0)
