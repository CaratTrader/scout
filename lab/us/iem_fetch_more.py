"""Extend the raw METAR archive: add KBOS and refresh September (through today) for all stations; CLI highs for KBOS."""
import time, urllib.request, json, datetime as dt
from pathlib import Path
ST = {"BOS": "America/New_York", "SFO": "America/Los_Angeles", "LAX": "America/Los_Angeles", "MDW": "America/Chicago", "NYC": "America/New_York", "MIA": "America/New_York"}
OUT = Path("data/lab/us/asos_raw"); OUT.mkdir(parents=True, exist_ok=True)
def fetch(url):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=300) as r:
                return r.read().decode()
        except Exception as e:
            print("retry", attempt, str(e)[:60], flush=True); time.sleep(6 * (attempt + 1))
    return ""
today = dt.date.today()
for st, tz in ST.items():
    months = range(4, today.month + 1) if st == "BOS" else [today.month]
    parts = []
    f = OUT / f"{st}.csv"
    existing = f.read_text().splitlines() if f.exists() and st != "BOS" else []
    for m in months:
        d1 = 20 if (m == 4) else 1; d2 = today.day if m == today.month else 30
        url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={st}&data=tmpf,metar&year1=2026&month1={m}&day1={d1}&year2=2026&month2={m}&day2={d2}"
               f"&tz={tz}&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=1&report_type=3&report_type=4")
        lines = fetch(url).strip().splitlines(); parts.append(lines); print(st, m, "rows", max(0, len(lines) - 1), flush=True); time.sleep(3)
    if st == "BOS":
        out = parts[0] + [l for p in parts[1:] for l in p[1:]]
    else:  # replace this month's rows in the existing file
        keep = [l for l in existing if not l.startswith(f"{st},2026-{today.month:02d}-")]
        out = keep + [l for l in parts[0][1:]]
    f.write_text("\n".join(out) + "\n")
# CLI highs for KBOS
txt = fetch("https://mesonet.agron.iastate.edu/json/cli.py?station=KBOS&year=2026")
try:
    rows = json.loads(txt).get("results") or json.loads(txt).get("data") or []
    cli = json.load(open("data/lab/us/asos/cli_high.json"))
    cli["KBOS"] = {r["valid"]: float(r["high"]) for r in rows if r.get("high") not in (None, "M", "")}
    json.dump(cli, open("data/lab/us/asos/cli_high.json", "w")); print("KBOS CLI days", len(cli["KBOS"]))
except Exception as e:
    print("KBOS CLI fail", str(e)[:80])
print("DONE")
