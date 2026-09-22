"""Fetch every Polymarket US daily-high-temperature ladder (event temp-<city>high-<date>) with all buckets and each
bucket's 1-minute YES ask/bid over the local day. Output: data/lab/us/temp_ladders.json + temp_series/<slug>.json"""
from __future__ import annotations
import datetime as dt, json, sys, time, zoneinfo
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lab.us import xvenue as X

TZ = {"sfo": "America/Los_Angeles", "lax": "America/Los_Angeles", "mdw": "America/Chicago", "nyc": "America/New_York", "mia": "America/New_York"}
OUT = Path("data/lab/us/temp_series"); OUT.mkdir(parents=True, exist_ok=True)
ms = json.load(open("data/lab/us/markets_all.json"))
days = sorted({(m["slug"].split("-")[2][:3], "-".join(m["slug"].split("-")[3:6])) for m in ms if m.get("category") == "climate" and m["slug"].startswith("tc-temp-")})
# add the most recent days not in the crawl
last = dt.date.fromisoformat(max(d for _, d in days))
for k in range(1, (dt.date.today() - last).days + 1):
    for c in TZ:
        days.append((c, (last + dt.timedelta(days=k)).isoformat()))
ladders = {}
n = 0
for city, day in days:
    ev = (X.get(f"{X.US}/events?slug=temp-{city}high-{day}") or {}).get("events") or []
    if not ev:
        continue
    mk = ev[0].get("markets") or []
    ladders[f"{city}:{day}"] = [{k: m.get(k) for k in ("slug", "question", "outcomePrices", "status", "startDate", "endDate", "description")} for m in mk]
    tz = zoneinfo.ZoneInfo(TZ[city]); d0 = dt.datetime.fromisoformat(day).replace(tzinfo=tz)
    t0 = int(d0.timestamp()); t1 = int((d0 + dt.timedelta(hours=23, minutes=59)).timestamp())
    for m in mk:
        f = OUT / f"{m['slug']}.json"
        if f.exists():
            continue
        h = (X.get(f"{X.US}/price-history?symbol={m['slug']}&timestamp.startTimestamp={t0}&timestamp.endTimestamp={t1}&fidelity=1") or {}).get("history", [])
        bbo = (X.get(f"{X.US}/markets/{m['slug']}/bbo") or {}).get("marketData", {})
        f.write_text(json.dumps({"slug": m["slug"], "t0": t0, "t1": t1, "history": h, "sharesTraded": bbo.get("sharesTraded")}))
        n += 1; time.sleep(0.05)
    if len(ladders) % 25 == 0:
        print(f"  ladders {len(ladders)} series fetched {n}", flush=True)
json.dump(ladders, open("data/lab/us/temp_ladders.json", "w"))
print(f"DONE ladders={len(ladders)} new series={n}")
