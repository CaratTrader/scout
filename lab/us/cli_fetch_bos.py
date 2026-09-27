import datetime as dt, json, time, urllib.request
from pathlib import Path
OUT = Path("data/lab/us/cli_intraday"); OUT.mkdir(parents=True, exist_ok=True)
def get(u):
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "scout-research"}), timeout=60) as r:
                return r.read().decode()
        except Exception:
            time.sleep(4 * (i + 1))
    return ""
day = dt.date(2026, 7, 1); n = 0
while day <= dt.date.today():
    try:
        rows = json.loads(get(f"https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?pil=CLIBOS&date={day.isoformat()}")).get("data") or []
    except Exception:
        rows = []
    for r in rows:
        ent = (r.get("entered") or "").replace("-", "").replace(":", ""); f = OUT / f"CLIBOS_{day.isoformat()}_{ent[9:13]}Z.txt"
        if f.exists() or not r.get("product_id"):
            continue
        txt = get(f"https://mesonet.agron.iastate.edu/api/1/nwstext/{r['product_id']}")
        if txt:
            f.write_text(txt); n += 1
        time.sleep(0.4)
    time.sleep(0.3); day += dt.timedelta(days=1)
print("DONE", n)
