"""Download hourly+special METAR temperatures (Iowa State ASOS archive) for the five temperature-market stations."""
import time, urllib.request
from pathlib import Path
ST = {"SFO": "America/Los_Angeles", "LAX": "America/Los_Angeles", "MDW": "America/Chicago", "NYC": "America/New_York", "MIA": "America/New_York"}
OUT = Path("data/lab/us/asos_raw"); OUT.mkdir(parents=True, exist_ok=True)
for st, tz in ST.items():
    parts = []
    for m in range(4, 10):
        d1 = 20 if m == 4 else 1; d2 = 22 if m == 9 else 30
        url = (f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={st}&data=tmpf,metar&year1=2026&month1={m}&day1={d1}&year2=2026&month2={m}&day2={d2}"
               f"&tz={tz}&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no&report_type=1&report_type=3&report_type=4")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=300) as r:
                    txt = r.read().decode()
                lines = txt.strip().splitlines()
                parts.append(lines if not parts else lines[1:])
                print(st, m, "rows", len(lines) - 1, flush=True); break
            except Exception as e:
                print(st, m, "retry", attempt, str(e)[:80], flush=True); time.sleep(5 * (attempt + 1))
        time.sleep(2)
    (OUT / f"{st}.csv").write_text("\n".join(l for p in parts for l in p) + "\n")
print("DONE")
