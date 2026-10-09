"""Free, keyless, timestamped tallies for r7_public_tally_count_ladder_census (no Kalshi calls here).

  White House  https://www.whitehouse.gov/presidential-actions/feed/?paged=N   (RSS, 30 items per page, pubDate UTC,
               categories, full post text). Used for executive orders (category 'Executive Orders', earliest public
               signal) and for nominations (posts titled 'Nomination(s) Sent to the Senate', one nomination per line).
  Fed. Reg.    https://www.federalregister.gov/api/v1/documents.json (EOs with signing_date: the KXEOWEEK settlement
               field) and /public-inspection-documents/current.json (EOs filed for the next issue, audit only).
  SPC          https://www.spc.noaa.gov/climo/summary/<year>/ruf/NAT/NAT.json  (preliminary 'rough log' national
               totals by month, convective day and hour: what the SPC 'Preliminary Report Summary' page displays).

Downloaded content is parsed as data only (regex/JSON), never executed."""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r7_public_tally_count_ladder_census"
RAW = OUT / "raw"
ET = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (research; Tradeinc lab; polite)"
WH_FEED = "https://www.whitehouse.gov/presidential-actions/feed/?paged={p}"
FR_DOCS = ("https://www.federalregister.gov/api/v1/documents.json?conditions%5Bpresidential_document_type%5D=executive_order"
           "&conditions%5Bpresident%5D=donald-trump&conditions%5Bsigning_date%5D%5Bgte%5D={since}&per_page=1000&order=oldest"
           "&fields%5B%5D=executive_order_number&fields%5B%5D=signing_date&fields%5B%5D=publication_date&fields%5B%5D=title"
           "&fields%5B%5D=document_number")
FR_PI = "https://www.federalregister.gov/api/v1/public-inspection-documents/current.json"
SPC = "https://www.spc.noaa.gov/climo/summary/{y}/ruf/NAT/NAT.json"


def http(url: str, timeout: int = 40, tries: int = 3) -> str:
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            err = e; time.sleep(2 + 3 * i)
    raise RuntimeError(f"fetch failed {url}: {err}")


# ---------------------------------------------------------------- White House feed
def parse_feed(xml: str) -> list[dict]:
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, flags=re.S):
        g = lambda tag: (re.search(rf"<{tag}>(.*?)</{tag}>", it, flags=re.S) or [None, ""])[1]  # noqa: E731
        title = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", g("title"))).strip()
        pub = g("pubDate").strip()
        try:
            ts = int(dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").timestamp())
        except ValueError:
            continue
        cats = [html.unescape(c) for c in re.findall(r"<category><!\[CDATA\[(.*?)\]\]></category>", it)]
        m = re.search(r"<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>", it, flags=re.S)
        body = m.group(1) if m else ""
        out.append({"title": title, "link": g("link").strip(), "ts": ts, "date_et": dt.datetime.fromtimestamp(ts, ET).date().isoformat(),
                    "cats": cats, "n_nominations": count_nominations(title, body) if is_nomination_post(title) else 0})
    return out


def is_nomination_post(title: str) -> bool:
    """'Nomination(s) Sent to the Senate' and 'Nomination(s) and Withdrawal(s) Sent to the Senate' (not withdrawals only)."""
    t = title.lower()
    return "sent to the senate" in t and "nomination" in t


NOM_LINE = re.compile(r"^[^,]{2,120}, of (?:the )?[A-Z][^,]{1,60},.{0,200}?\bto be\b")


def count_nominations(title: str, body: str) -> int:
    """One line per nomination ('Name, of State, [...] to be ...'); a person nominated to two posts counts twice, as the
    KXTRUMPNOMNUM rules say. Counting stops at a WITHDRAWAL(S) heading (withdrawn names use the same line format) and at
    the feed footer ('The post ...'). Newer posts carry a 'NOMINATIONS SENT TO THE SENATE:' heading, older ones none."""
    txt = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    lines = [ln.strip() for ln in txt.split("\n") if ln.strip()]
    n = 0
    for ln in lines:
        u = ln.upper()
        if u.startswith("WITHDRAWAL") or u == "THE POST":
            break
        if NOM_LINE.match(ln):
            n += 1
    return n


def wh_items(pages: int = 1, cache_dir: Path | None = None, sleep: float = 1.5) -> list[dict]:
    """Latest `pages` feed pages (live). With cache_dir, pages are read from / written to disk (history build)."""
    items = {}
    for p in range(1, pages + 1):
        f = cache_dir / f"wh_feed_p{p}.xml" if cache_dir else None
        if f is not None and f.exists():
            xml = f.read_text()
        else:
            xml = http(WH_FEED.format(p=p))
            if f is not None:
                f.parent.mkdir(parents=True, exist_ok=True); f.write_text(xml)
            time.sleep(sleep)
        got = parse_feed(xml)
        if not got:
            break
        for x in got:
            items[x["link"] or (x["title"], x["ts"])] = x
    return sorted(items.values(), key=lambda x: x["ts"])


def wh_items_since(A: int, max_pages: int = 4) -> list[dict]:
    """Live: feed pages until the oldest item precedes A (the start of the count window)."""
    items = {}
    for p in range(1, max_pages + 1):
        got = parse_feed(http(WH_FEED.format(p=p)))
        for x in got:
            items[x["link"]] = x
        if not got or min(x["ts"] for x in got) < A:
            break
        time.sleep(1.0)
    return sorted(items.values(), key=lambda x: x["ts"])


def wh_eo(items: list[dict]) -> list[dict]:
    return [x for x in items if "Executive Orders" in x["cats"]]


def wh_nominations(items: list[dict]) -> list[dict]:
    return [x for x in items if x["n_nominations"] > 0]


# ---------------------------------------------------------------- Federal Register
def fr_eos(since: str) -> list[dict]:
    d = json.loads(http(FR_DOCS.format(since=since)))
    return [{"eo": r.get("executive_order_number"), "signing_date": r.get("signing_date"), "publication_date": r.get("publication_date"),
             "title": r.get("title"), "doc": r.get("document_number")} for r in d.get("results") or []]


def fr_public_inspection_eos() -> list[dict]:
    d = json.loads(http(FR_PI))
    out = []
    for r in d.get("results") or []:
        t = r.get("title") or ""
        m = re.search(r"\(EO (\d{5})\)", t)
        if r.get("type") == "Presidential Document" and m:
            out.append({"eo": m.group(1), "title": t.strip(), "filed_at": r.get("filed_at"), "publication_date": r.get("publication_date")})
    return out


# ---------------------------------------------------------------- SPC preliminary (rough log) tornado tally
def spc_year(y: int, cache: bool = False) -> dict:
    f = RAW / f"spc_ruf_NAT_{y}.json"
    if cache and f.exists():
        return json.loads(f.read_text())
    d = json.loads(http(SPC.format(y=y)))
    if cache:
        RAW.mkdir(parents=True, exist_ok=True); f.write_text(json.dumps(d))
    return d


def spc_month_count(d: dict, month: int) -> int:
    return int((d.get("month") or {}).get(str(month), {}).get("torn", 0))
