"""quake_entertain: USGS ComCat data. (1) catalog of M>=4.7 quakes during the Kalshi period with every superseded
origin version (first-post time, initial and revised magnitudes) -> usgs_versions.json; (2) M>=5.0 history
2010-01-01..2026-08-31 (before the first Kalshi event) for base rates -> usgs_hist.csv."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.quake_entertain_fetch import ext, OUT

Q = "https://earthquake.usgs.gov/fdsnws/event/1/query"


def versions(start="2026-09-01", end="2026-10-09", minmag=4.7):
    d = json.loads(ext(f"{Q}?format=geojson&starttime={start}&endtime={end}&minmagnitude={minmag}&orderby=time-asc", refresh=True))
    out = {}
    for f in d["features"]:
        eid = f["id"]; p = f["properties"]
        t = ext(f"{Q}?eventid={eid}&format=geojson&includesuperseded=true", pace=0.2)
        try:
            pr = json.loads(t)["properties"]["products"]
        except Exception:
            print("fail", eid, flush=True); continue
        vs = sorted([(o["updateTime"], o["source"], o["status"], o["properties"].get("magnitude"), o["properties"].get("magnitude-type"))
                     for o in pr.get("origin", [])])
        out[eid] = {"time": p["time"], "mag": p["mag"], "magType": p["magType"], "updated": p["updated"], "status": p["status"], "place": p["place"],
                    "versions": vs}
    (OUT / "usgs_versions.json").write_text(json.dumps(out))
    print("versions", len(out), flush=True)


def hist():
    rows = []
    for a, b in (("2010-01-01", "2014-01-01"), ("2014-01-01", "2018-01-01"), ("2018-01-01", "2022-01-01"), ("2022-01-01", "2026-09-01")):
        t = ext(f"{Q}?format=csv&starttime={a}&endtime={b}&minmagnitude=5.0&orderby=time-asc")
        lines = t.strip().splitlines(); rows += lines[1:] if rows else lines
    (OUT / "usgs_hist.csv").write_text("\n".join(rows) + "\n")
    print("hist rows", len(rows) - 1, flush=True)


if __name__ == "__main__":
    hist(); versions()
