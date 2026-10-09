"""Counted, cached Kalshi GET for the r3_tsa_weekly_accumulator researcher. Every Kalshi call goes through
lab.us.data_refresh.fetch (waits for the paper bot's idle window, retries 429s) and is logged to calls.log.
Hard budget: 200 calls for the whole task. Responses are cached on disk by URL so re-runs never spend budget twice.
Also: the tsa.gov daily throughput table (cached HTML per year) and a seed of Kalshi responses that the
quake_entertain researcher already downloaded (same URLs, so no budget is spent twice across researchers)."""
from __future__ import annotations
import datetime as dt, hashlib, json, re, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path("data/kalshi_lab/strategies/r3_tsa_weekly_accumulator")
LOG = OUT / "calls.log"
CACHE = OUT / "api_cache"
TSA_RAW = OUT / "tsa_raw"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str, cache_only: bool = False) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if cache_only:
        return {"_error": "not cached"}
    if used() >= BUDGET:
        raise RuntimeError("Kalshi call budget exhausted")
    txt = fetch(K + path, pace=1.15)
    with LOG.open("a") as g:
        g.write(f"{int(time.time())}\t{len(txt)}\t{path[:240]}\n")
    if not txt:
        return {"_error": "empty"}
    f.write_text(txt)
    return json.loads(txt)


def seed(path: str, src: Path) -> None:
    """Put a response another researcher already fetched (same URL) into this cache without a call."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (hashlib.sha1(path.encode()).hexdigest() + ".json")
    if not f.exists() and src.exists():
        f.write_text(src.read_text())


def tsa_daily() -> dict[str, int]:
    """{YYYY-MM-DD: travelers screened} from the cached tsa.gov tables (one HTML per year + the current page)."""
    out: dict[str, int] = {}
    for f in sorted(TSA_RAW.glob("*.html")):
        cells = re.findall(r'<td class="text-align-center">([^<]*)</td>', f.read_text())
        for d, n in zip(cells[0::2], cells[1::2]):
            try:
                m, dd, y = map(int, d.strip().split("/"))
                out[dt.date(y, m, dd).isoformat()] = int(n.replace(",", "").strip())
            except ValueError:
                continue
    return out


def tsa_refresh(years=(2019, 2022, 2023, 2024, 2025)) -> None:
    TSA_RAW.mkdir(parents=True, exist_ok=True)
    for y in years:
        f = TSA_RAW / f"{y}.html"
        if f.exists():
            continue
        req = urllib.request.Request(f"https://www.tsa.gov/travel/passenger-volumes/{y}", headers={"User-Agent": "Mozilla/5.0 scout-research (personal, polite)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            f.write_text(r.read().decode())
        time.sleep(3)


def tsa_vintage(era: str = "disc") -> dict[str, int]:
    """Daily numbers as the market could have seen them (TSA revises old numbers: most 2025 days were revised by
    +0.01..0.1% after Dec 2025, a few June 2025 days by +2.5..3%; 2026 numbers have not been revised).
    Each date takes its value from the earliest archived snapshot of tsa.gov taken after that date (first-print proxy);
    dates no snapshot covers keep today's value. era='val' (2026 decisions) uses today's table except that 2025 days take
    the /2025 page as archived in 2026 (the version visible during the validation weeks)."""
    import glob
    cur = tsa_daily()
    snaps = []
    for f in glob.glob(str(OUT / "wayback" / "tables" / "*.json")):
        name = Path(f).stem
        d = json.loads(Path(f).read_text())
        if not d.get("table"):
            continue
        real = re.search(r"/web/(\d{14})", d.get("url", ""))
        ts = real.group(1) if real else name.split("_")[-1]
        tab = {}
        for k, v in d["table"].items():
            try:
                tab[dt.datetime.strptime(k, "%m/%d/%Y").date().isoformat()] = v
            except ValueError:
                pass
        snaps.append((ts, name.startswith("y"), tab))
    out = dict(cur)
    if era == "disc":
        for ts, is_year, tab in sorted(snaps, key=lambda s: s[0], reverse=True):     # earliest snapshot wins (written last)
            snap_day = dt.date(int(ts[:4]), int(ts[4:6]), int(ts[6:8])).isoformat()
            if ts >= "20260301000000":
                continue                              # discovery decisions end 2026-03-01
            for k, v in tab.items():
                if k < snap_day:
                    out[k] = v
    else:
        y25 = sorted((s for s in snaps if s[1] and s[0] >= "20260301000000" and s[0] < "20261001000000" and any(k.startswith("2025") for k in s[2])), key=lambda s: s[0])
        if y25:
            for k, v in y25[0][2].items():
                if k.startswith("2025"):
                    out[k] = v
    return out
