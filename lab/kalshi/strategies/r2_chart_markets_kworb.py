"""Free leading data for r2_chart_markets: kworb.net Spotify US weekly song chart (Fri-Thu tracking week, the same
window as the Billboard Hot 100), recovered for past weeks from Wayback Machine snapshots of
kworb.net/spotify/country/us_weekly.html (each page names its week: 'Spotify Weekly Chart - United States - YYYY/MM/DD',
the Thursday that ends the week). Output: data/kalshi_lab/strategies/r2_chart_markets/kworb_us_weekly.json
  {week_end_thursday: {"snap": wayback_ts, "rows": [[pos, artist, title, streams], ...top 20]}}
Usage: python -m lab.kalshi.strategies.r2_chart_markets_kworb"""
from __future__ import annotations
import html as H, json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r2_chart_markets_api import wget, OUT

URL = "kworb.net/spotify/country/us_weekly.html"
DST = OUT / "kworb_us_weekly.json"
TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def parse(page: str) -> tuple[str | None, list]:
    m = re.search(r"Spotify Weekly Chart - United States - (\d{4}/\d{2}/\d{2})", page)
    if not m:
        return None, []
    rows = []
    body = page[page.find("<tbody>"):]
    for tr in TR.findall(body):
        td = TD.findall(tr)   # Pos, P+, Artist and Title, Wks, Pk, (x?), Streams, Streams+, Total
        if len(td) < 7 or not td[0].strip().isdigit():
            continue
        txt = H.unescape(re.sub(r"<[^>]+>", "", td[2])).strip()
        artist, _, title = txt.partition(" - ")
        rows.append([int(td[0]), artist.strip(), title.strip(), int(re.sub(r"[^\d]", "", td[6]) or 0)])
        if len(rows) >= 20:
            break
    return m.group(1).replace("/", "-"), rows


def main() -> None:
    cdx = json.loads(wget(f"http://web.archive.org/cdx/search/cdx?url={URL}&from=2024&output=json&fl=timestamp,statuscode") or "[[]]")
    snaps = [x[0] for x in cdx[1:] if len(x) > 1 and x[1] == "200"]
    have = json.loads(DST.read_text()) if DST.exists() else {}
    for ts in snaps:
        page = wget(f"http://web.archive.org/web/{ts}id_/https://{URL}", pace=1.5)
        wk, rows = parse(page)
        if wk and rows and (wk not in have or ts < have[wk]["snap"]):
            have[wk] = {"snap": ts, "rows": rows}
    DST.write_text(json.dumps(dict(sorted(have.items())), indent=0))
    print(len(snaps), "snapshots ->", len(have), "weeks")


if __name__ == "__main__":
    main()
