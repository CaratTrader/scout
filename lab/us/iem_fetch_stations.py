"""Raw METAR (IEM ASOS archive) from 2026-07-01 and NWS CLI highs for the given stations: python lab/us/iem_fetch_stations.py DCA:America/New_York PHL:America/New_York ..."""
import sys, time, json, datetime as dt, urllib.request
from pathlib import Path
OUT = Path("data/lab/us/asos_raw"); OUT.mkdir(parents=True, exist_ok=True)
def fetch(url):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=300) as r:
                return r.read().decode()
        except Exception as e:
            print("retry", attempt, str(e)[:60], flush=True); time.sleep(8 * (attempt + 1))
    return ""
today = dt.date.today(); cli = json.load(open("data/lab/us/asos/cli_high.json"))
for arg in sys.argv[1:]:
    st, tz = arg.split(":", 1); parts = []
    for m in range(7, today.month + 1):
        d2 = today.day if m == today.month else 31 if m in (7, 8) else 30
        url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={st}&data=tmpf,metar&year1=2026&month1={m}&day1=1&year2=2026&month2={m}&day2={d2}"
               f"&tz={tz}&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=1&report_type=3&report_type=4")
        lines = fetch(url).strip().splitlines(); parts.append(lines); print(st, m, "rows", max(0, len(lines) - 1), flush=True); time.sleep(4)
    (OUT / f"{st}.csv").write_text("\n".join(l for i, p in enumerate(parts) for l in (p if i == 0 else p[1:])) + "\n")
    txt = fetch(f"https://mesonet.agron.iastate.edu/json/cli.py?station=K{st}&year=2026")
    try:
        rows = json.loads(txt).get("results") or json.loads(txt).get("data") or []
        cli[f"K{st}"] = {r["valid"]: float(r["high"]) for r in rows if r.get("high") not in (None, "M", "")}
        print(st, "CLI days", len(cli[f"K{st}"]), flush=True)
    except Exception as e:
        print(st, "CLI fail", str(e)[:60], flush=True)
    json.dump(cli, open("data/lab/us/asos/cli_high.json", "w")); time.sleep(3)
print("DONE")
