"""When does tsa.gov publish each day's number? Reads Wayback Machine snapshots of tsa.gov/travel/passenger-volumes
and records the latest date shown in each snapshot (free, no key, polite pacing, cached).
Usage: python -m lab.kalshi.strategies.r3_tsa_weekly_accumulator_wayback 20260710 20260810"""
import gzip, json, os, re, sys, time, urllib.request

OUT = 'data/kalshi_lab/strategies/r3_tsa_weekly_accumulator/wayback'
UA = {"User-Agent": "scout-research (personal, polite)"}


def get(url: str, tries: int = 5) -> str:
    for i in range(tries):
        try:
            b = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()
            if b[:2] == b'\x1f\x8b':
                b = gzip.decompress(b)
            return b.decode('utf-8', 'replace')
        except Exception as e:
            print('err', url[-60:], str(e)[:80], flush=True)
            time.sleep(15 * (i + 1))
    return ""


def main(fr: str, to: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    cdx = get(f"https://web.archive.org/cdx/search/cdx?url=tsa.gov/travel/passenger-volumes&from={fr}&to={to}&output=json&fl=timestamp,statuscode&filter=statuscode:200")
    rows = json.loads(cdx)[1:] if cdx else []
    rf = f'{OUT}/latest_{fr}_{to}.json'
    res = json.load(open(rf)) if os.path.exists(rf) else {}
    for ts, _ in rows:
        if ts in res:
            continue
        h = get(f"https://web.archive.org/web/{ts}id_/https://www.tsa.gov/travel/passenger-volumes")
        cells = re.findall(r'<td[^>]*text-align[^>]*>\s*([^<]*?)\s*</td>', h)
        res[ts] = cells[0].strip() if cells else None
        json.dump(res, open(rf, 'w'))
        time.sleep(1.5)
    print(len(rows), len(res))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])


def parse_table(h: str) -> dict:
    """Both page layouts: 2025+ (two centred cells per row) and 2024 (views table: date / this year / last year)."""
    if 'view-field-travel-number-date-table-column' in h:
        ds = re.findall(r'headers="view-field-travel-number-date-table-column"[^>]*>\s*([^<]*?)\s*</td>', h)
        ns = re.findall(r'headers="view-field-travel-number-table-column"[^>]*>\s*([^<]*?)\s*</td>', h)
    else:
        cells = re.findall(r'<td[^>]*text-align[^>]*>\s*([^<]*?)\s*</td>', h)
        ds, ns = cells[0::2], cells[1::2]
    return {d.strip(): int(n.replace(',', '').strip()) for d, n in zip(ds, ns) if n.strip().replace(',', '').isdigit()}


def table_at(ts: str) -> tuple[str, dict]:
    """Full table shown by the snapshot nearest to ts (YYYYMMDDhhmmss): (actual snapshot url, {M/D/YYYY: number})."""
    os.makedirs(f'{OUT}/tables', exist_ok=True)
    f = f'{OUT}/tables/{ts}.json'
    if os.path.exists(f):
        d = json.load(open(f)); return d['url'], d['table']
    url = f"https://web.archive.org/web/{ts}id_/https://www.tsa.gov/travel/passenger-volumes"
    for i in range(4):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)
            b = r.read(); real = r.geturl()
            if b[:2] == b'\x1f\x8b':
                b = gzip.decompress(b)
            tab = parse_table(b.decode('utf-8', 'replace'))
            json.dump({'url': real, 'table': tab}, open(f, 'w'))
            time.sleep(2)
            return real, tab
        except Exception as e:
            print('err', ts, str(e)[:80], flush=True); time.sleep(20 * (i + 1))
    return '', {}


def year_page_at(year: int, ts: str) -> tuple[str, dict]:
    """The /travel/passenger-volumes/<year> page as archived nearest to ts."""
    os.makedirs(f'{OUT}/tables', exist_ok=True)
    f = f'{OUT}/tables/y{year}_{ts}.json'
    if os.path.exists(f):
        d = json.load(open(f)); return d['url'], d['table']
    url = f"https://web.archive.org/web/{ts}id_/https://www.tsa.gov/travel/passenger-volumes/{year}"
    for i in range(5):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)
            b = r.read(); real = r.geturl()
            if b[:2] == b'\x1f\x8b':
                b = gzip.decompress(b)
            tab = parse_table(b.decode('utf-8', 'replace'))
            json.dump({'url': real, 'table': tab}, open(f, 'w'))
            time.sleep(2)
            return real, tab
        except Exception as e:
            print('err', year, ts, str(e)[:80], flush=True); time.sleep(20 * (i + 1))
    return '', {}


VINTAGE_TS = ['20250105120000', '20250201120000', '20250301120000', '20250401120000', '20250501120000', '20250601120000',
              '20250801120000', '20250901120000', '20251001120000', '20251101120000', '20251201120000', '20260115120000',
              '20260215120000', '20260305120000']


def vintage_batch() -> None:
    for ts in VINTAGE_TS:
        u, t = table_at(ts)
        print('main', ts, u[28:42], len(t), flush=True)
    for y, ts in ((2024, '20250115120000'), (2024, '20240701120000'), (2025, '20260201120000'), (2025, '20260401120000'), (2025, '20260801120000')):
        u, t = year_page_at(y, ts)
        print('year', y, ts, u[28:42], len(t), flush=True)
