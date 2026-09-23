"""Download every NWS Daily Climate Report issuance (CLI<station>) for the five temperature-market stations from the
Iowa State AFOS archive, 2026-04-20 .. today, into data/lab/us/cli_intraday/<PIL>_<YYYY-MM-DD>_<HHMMZ>.txt"""
import datetime as dt, json, time, urllib.request
from pathlib import Path
OUT = Path("data/lab/us/cli_intraday"); OUT.mkdir(parents=True, exist_ok=True)
def get(u):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "scout-research"}), timeout=60) as r:
                return r.read().decode()
        except Exception as e:
            time.sleep(3 * (i + 1))
    return ""
day = dt.date(2026, 4, 20); n = 0
while day <= dt.date.today():
    for pil in ("CLIMIA", "CLINYC", "CLIMDW", "CLISFO", "CLILAX"):
        lst = get(f"https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?pil={pil}&date={day.isoformat()}")
        try:
            rows = json.loads(lst).get("data") or []
        except Exception:
            rows = []
        for r in rows:
            ent = (r.get("entered") or "").replace("-", "").replace(":", "")
            f = OUT / f"{pil}_{day.isoformat()}_{ent[9:13]}Z.txt"
            if f.exists() or not r.get("product_id"):
                continue
            txt = get(f"https://mesonet.agron.iastate.edu/api/1/nwstext/{r['product_id']}")
            if txt:
                f.write_text(txt); n += 1
            time.sleep(0.25)
        time.sleep(0.15)
    if day.day % 10 == 0:
        print(day, "files", n, flush=True)
    day += dt.timedelta(days=1)
print("DONE", n)
