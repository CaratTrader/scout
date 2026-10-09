"""r6_earnings_transcript_prior_taker: free public earnings-call transcripts -> per-word hit counts (never the text).

Source: The Motley Fool's free transcript pages (www.fool.com/earnings/call-transcripts/..., allowed by its robots.txt),
found through its public monthly sitemaps (www.fool.com/sitemap/YYYY/MM). Coverage is sparse before 2025 (about 300
transcripts a month in 2023-24) and dense in 2026 (about 2,000 a month), so a company has between 0 and ~12 earlier
calls on file. Other free sources were probed and dropped: Yahoo's historical transcripts are a premium feature (not
circumvented), discountingcashflows / insidermonkey answer non-browsers with a bot challenge, roic.ai returns 403.

What is stored (data/kalshi_lab/strategies/r6_earnings_transcript_prior_taker/):
  fool_months/<YYYY_MM>.txt  transcript URLs listed in that month's sitemap (URLs only)
  tx/<TK>.jsonl              one line per transcript: url, call date and start time (ET) from the page header, parse
                             format, speaker/word totals, and for every Kalshi word of the company the hit count in
                             company + operator speech ("c") and in all speech incl. analysts ("a"). Nothing else.

Kalshi's word rule (rules_primary / rules_secondary of these series): said by any company representative, including
the operator, anywhere in the call incl. Q&A; the exact word or phrase or its plural / possessive form; other
inflections do not count. Alternatives are separated by " / "; "(3+ times)" sets a count threshold.

Polite fetching: one request at a time, >= 1.6 s apart, browser user agent, 3 retries with back-off."""
from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/r6_earnings_transcript_prior_taker")
MONTHS = OUT / "fool_months"
TXD = OUT / "tx"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_last = [0.0]
WEB_LOG = OUT / "web_calls.log"

# Kalshi series suffix -> (stock ticker used in Fool slugs / titles, extra slug tickers, company-name slug prefixes)
ALIAS = {"COINBASE": ("COIN", [], ["coinbase"]), "PSKY": ("PSKY", ["para"], ["paramount"]), "NBIS": ("NBIS", [], ["nebius"]),
         "GOOGL": ("GOOGL", ["goog"], ["alphabet"]), "SPCX": ("SPCX", [], ["spacex"])}


def get(url: str, tries: int = 3) -> str:
    for k in range(tries):
        wait = 1.6 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=40) as r:
                txt = r.read().decode("utf-8", "replace")
            OUT.mkdir(parents=True, exist_ok=True)
            with WEB_LOG.open("a") as f:
                f.write(f"{int(time.time())}\t200\t{len(txt)}\t{url[:200]}\n")
            return txt
        except urllib.error.HTTPError as e:
            with WEB_LOG.open("a") as f:
                f.write(f"{int(time.time())}\t{e.code}\t0\t{url[:200]}\n")
            if e.code == 404:
                return ""
            time.sleep(5 * (k + 1))
        except Exception as e:  # noqa: BLE001
            with WEB_LOG.open("a") as f:
                f.write(f"{int(time.time())}\tERR\t0\t{url[:200]}\t{str(e)[:80]}\n")
            time.sleep(5 * (k + 1))
    return ""


# ----------------------------------------------------------------------------------------------------------- index
def month_urls(y: int, m: int) -> list[str]:
    MONTHS.mkdir(parents=True, exist_ok=True)
    f = MONTHS / f"{y}_{m:02d}.txt"
    now = dt.date.today()
    if f.exists() and (y, m) != (now.year, now.month):
        return f.read_text().split()
    txt = get(f"https://www.fool.com/sitemap/{y}/{m:02d}")
    urls = sorted(set(re.findall(r"<loc>(https://www\.fool\.com/earnings/call-transcripts/[^<]+)</loc>", txt)))
    if txt:
        f.write_text("\n".join(urls))
    return urls


def build_index(y0: int = 2022, m0: int = 6, y1: int = 2026, m1: int = 10) -> list[str]:
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        u = month_urls(y, m)
        print(f"{y}-{m:02d}: {len(u)} transcript urls", flush=True)
        out += u
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def all_urls() -> list[str]:
    return sorted({u for f in MONTHS.glob("*.txt") for u in f.read_text().split()})


def slug_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def url_date(url: str) -> dt.date:
    m = re.search(r"/call-transcripts/(\d{4})/(\d{2})/(\d{2})/", url)
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def name_prefixes(name: str) -> list[str]:
    """Slug prefix for a company name: 'The Walt Disney Company' -> ['walt-disney']; 'Apple Inc.' -> ['apple']."""
    n = name.lower().replace("&", " ").replace("'", "").replace(".com", "")
    n = re.sub(r"\b(the|inc|corp|corporation|company|co|group|holdings|plc|ltd|limited|n\.v|nv|s\.a|sa|technologies|technology|"
               r"platforms|communications|global|internet|wholesale|athletica|old country store|markets|enterprise|foods|beauty)\b\.?", " ", n)
    toks = [t for t in re.split(r"[^a-z0-9]+", n) if t]
    if not toks:
        return []
    return ["-".join(toks[:2])] if len(toks) >= 2 else [toks[0]]


def company_urls(series_suffix: str, name: str, urls: list[str]) -> list[str]:
    """Candidate transcript URLs: slug has the ticker as a token before the period label, or starts with the company
    name. Every page is verified against its title after the fetch."""
    tk, extra, pref = ALIAS.get(series_suffix, (series_suffix, [], []))
    tks = [tk.lower()] + extra
    pref = pref + name_prefixes(name)
    out = []
    for u in urls:
        s = slug_of(u)
        if not re.search(r"earnings|transcript", s):
            continue
        hit_tk = any(re.search(rf"(^|-){re.escape(t)}-(q[1-4]|fy|full|h[12]|earn|20\d\d|fiscal|first|second|third|fourth|annual)", s) for t in tks)
        hit_nm = any(s.startswith(p + "-") for p in pref)
        if hit_tk or hit_nm:
            out.append(u)
    return sorted(set(out))


def title_ok(title: str, series_suffix: str, name: str) -> bool:
    """The page is this company's call: its ticker appears in the title (as '(TK)' or a bare token), or the title starts
    with the company name and carries no other ticker in parentheses."""
    tk, extra, pref = ALIAS.get(series_suffix, (series_suffix, [], []))
    t = title.split("|")[0]
    for x in [tk] + extra:
        if re.search(rf"(\(|\b){re.escape(x.upper())}(\)|\b)", t):
            return True
    if re.search(r"\(([A-Z.]{1,6})\)", t):
        return False
    tl = re.sub(r"[^a-z0-9 ]", "", t.lower().replace("-", " "))
    return any(tl.startswith(p.replace("-", " ") + " ") for p in pref + name_prefixes(name))


# ----------------------------------------------------------------------------------------------------------- parse
def _txt(s: str) -> str:
    return htmlmod.unescape(re.sub(r"<[^>]+>", " ", s))


def _norm_name(s: str) -> str:
    s = re.sub(r"[^a-z ]", " ", s.lower())
    return " ".join(t for t in s.split() if len(t) > 1)


MONTHS_RE = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"


def _parse_dt(s: str):
    """'Tuesday, Oct. 7, 2025, at 8 a.m. ET' or 'Feb 02, 2023, 5:00 p.m. ET' -> (date, minutes after midnight ET or None)."""
    m = re.search(MONTHS_RE + r"\s+(\d{1,2}),\s*(\d{4})", s)
    if not m:
        return None, None
    mon = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1)[:3].lower()) + 1
    d = dt.date(int(m.group(3)), mon, int(m.group(2)))
    t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", s[m.end():], re.I)
    mins = None
    if t:
        h = int(t.group(1)) % 12 + (12 if t.group(3).lower() == "p" else 0)
        mins = h * 60 + int(t.group(2) or 0)
    return d, mins


def parse(page: str) -> dict | None:
    """Speaker turns of a Fool transcript page. Returns {title, date, mins, fmt, turns: [(speaker, is_company, text)]}."""
    tm = re.search(r"<title>(.*?)</title>", page, re.S)
    title = _txt(tm.group(1)).strip() if tm else ""
    a = page.find("article-body")
    if a < 0:
        return None
    body = page[a:]
    end = min([i for i in (body.find(">Read Next<"), body.find("Premium Investing Services")) if i > 0] or [len(body)])
    body = body[:end]
    fmt = None
    if "Full Conference Call Transcript" in body:
        fmt = "new"
        hdr = body[: body.find("Full Conference Call Transcript")]
        dm = re.search(r'id="date"[^>]*>.*?</h2>\s*<p>(.*?)</p>', hdr, re.S)
        d, mins = _parse_dt(_txt(dm.group(1))) if dm else (None, None)
        pm = re.search(r'id="call-participants".*?<ul>(.*?)</ul>', hdr, re.S)
        parts = [_txt(x) for x in re.findall(r"<li>(.*?)</li>", pm.group(1), re.S)] if pm else []
        company = set(); analysts = set()
        for p in parts:
            nm = p.split("—")[-1].split(" -- ")[-1].strip()
            (analysts if "analyst" in p.lower() else company).add(_norm_name(nm))
        txt = body[body.find("Full Conference Call Transcript"):]
    elif "Prepared Remarks:" in body:
        fmt = "old"
        hdr = body[: body.find("Prepared Remarks:")]
        d, mins = _parse_dt(_txt(hdr))
        cp = body.find("Call participants:")
        plist = body[cp:] if cp > 0 else ""
        company = set(); analysts = set()
        for nm, role in re.findall(r"<strong>(.*?)</strong>\s*--\s*<em>(.*?)</em>", plist, re.S):
            (analysts if "analyst" in role.lower() else company).add(_norm_name(_txt(nm)))
        txt = body[body.find("Prepared Remarks:"): cp if cp > 0 else len(body)]
    else:
        return None
    raw = []
    cur = None
    for p in re.findall(r"<p[^>]*>(.*?)</p>", txt, re.S):
        sm = re.match(r"\s*<strong>(.*?)</strong>(.*)", p, re.S)
        rest = p
        if sm:
            spk = _txt(sm.group(1)).strip().rstrip(":").strip()
            if spk and len(spk) < 60 and not spk.lower().startswith(("need a quote", "duration")):
                cur = spk
                rest = sm.group(2)
                if fmt == "old":
                    rest = re.sub(r"^\s*--\s*<em>.*?</em>", "", rest, flags=re.S)
        t = _txt(rest).strip()
        if cur is None or not t:
            continue
        raw.append((cur, t))
    # analysts named by the operator ("our next question comes from Erik Woodring with Morgan Stanley")
    intro = set()
    for spk, t in raw:
        if _norm_name(spk) == "operator":
            for m in re.finditer(r"(?:question|questions|line)\s+(?:\w+\s+){0,3}?(?:from|of)\s+(?:the line of\s+)?"
                                 r"([A-Z][\w.'\-]+(?:\s+[A-Z][\w.'\-]+){0,3})", t):
                intro.add(_norm_name(m.group(1)).split()[-1] if _norm_name(m.group(1)) else "")
    intro.discard("")
    turns = []
    for spk, t in raw:
        nn = _norm_name(spk)
        last = nn.split()[-1] if nn else ""
        if nn == "operator":
            isc = True
        elif nn in analysts:
            isc = False
        elif nn in company:
            isc = True
        elif company and any(last and c.split()[-1:] == [last] for c in company):
            isc = True
        elif last in intro or any(last and a.split()[-1:] == [last] for a in analysts):
            isc = False
        elif fmt == "new":   # unlisted and never introduced as a questioner
            isc = not company      # empty participant list: company speaker; otherwise an analyst
        else:
            isc = not analysts
        turns.append((spk, isc, t))
    if not turns:
        return None
    return {"title": title, "date": d.isoformat() if d else None, "mins": mins, "fmt": fmt, "turns": turns,
            "n_company_names": len(company), "n_analyst_names": len(analysts), "n_intro": len(intro)}


# ----------------------------------------------------------------------------------------------------------- words
def word_spec(word: str) -> tuple[list[str], int]:
    """'Shutdown / Shut Down' -> (['Shutdown', 'Shut Down'], 1); 'Dividend (3+ times)' -> (['Dividend'], 3)."""
    thr = 1
    m = re.search(r"\((\d+)\+\s*times?\)", word, re.I)
    if m:
        thr = int(m.group(1))
        word = word[: m.start()] + word[m.end():]
    alts = [a.strip() for a in word.split(" / ") if a.strip()]
    if len(alts) == 1 and "/" in alts[0] and not re.search(r"\w/\w", alts[0]):
        alts = [a.strip() for a in alts[0].split("/") if a.strip()]
    return alts, thr


def alt_regex(alt: str) -> re.Pattern:
    """Exact word/phrase, or a plural or possessive form of it. Tokens may be joined by spaces or hyphens.
    Short all-caps acronyms (<= 4 letters, e.g. 'US', 'AR', 'AI') are matched case-sensitively."""
    toks = [t for t in re.split(r"[\s\-]+", alt.strip()) if t]
    if not toks:
        return re.compile(r"(?!x)x")
    last = toks[-1]
    stem = re.escape(last)
    forms = [stem]
    if re.search(r"[^aeiou]y$", last, re.I):
        forms.append(re.escape(last[:-1]) + "ies")
    suffix = r"(?:s|es|'s|’s|s'|s’)?"
    body = r"[\s\-]+".join([re.escape(t) for t in toks[:-1]] + [f"(?:{'|'.join(forms)})"])
    flags = 0 if (re.fullmatch(r"[A-Z0-9+&]{1,4}", alt.replace(" ", "")) is not None) else re.I
    return re.compile(rf"(?<![\w]){body}{suffix}(?![\w])", flags)


_RX: dict = {}


def count_word(word: str, text: str) -> int:
    alts, _ = word_spec(word)
    n = 0
    for a in alts:
        rx = _RX.get(a)
        if rx is None:
            rx = _RX[a] = alt_regex(a)
        n += len(rx.findall(text))
    return n


def counts_for(parsed: dict, words: list[str]) -> dict:
    comp = "\n".join(t for _, isc, t in parsed["turns"] if isc)
    allt = "\n".join(t for _, _, t in parsed["turns"])
    return {w: [count_word(w, comp), count_word(w, allt)] for w in words}


# ----------------------------------------------------------------------------------------------------------- fetch
def fetch_company(series_suffix: str, name: str, words: list[str], urls: list[str], max_fetch: int = 60) -> list[dict]:
    """Fetch (once) every matched transcript of a company; append count rows to tx/<suffix>.jsonl. If the word list grew
    since a transcript was counted, it is re-fetched and re-counted (text is never kept)."""
    TXD.mkdir(parents=True, exist_ok=True)
    f = TXD / f"{series_suffix}.jsonl"
    have = {}
    if f.exists():
        for l in f.open():
            r = json.loads(l)
            have[r["url"]] = r
    todo = [u for u in company_urls(series_suffix, name, urls) if u not in have or set(words) - set(have[u].get("counts", {}))]
    n = 0
    for u in todo[:max_fetch]:
        page = get(u)
        n += 1
        row = {"url": u, "pub": url_date(u).isoformat(), "ok": False}
        if page:
            p = parse(page)
            if p:
                t = p["title"]
                row.update({"ok": title_ok(t, series_suffix, name), "title": t[:120], "date": p["date"], "mins": p["mins"], "fmt": p["fmt"],
                            "n_turns": len(p["turns"]), "n_company_turns": sum(1 for _, c, _ in p["turns"] if c),
                            "n_words_company": sum(len(x.split()) for _, c, x in p["turns"] if c),
                            "n_words_all": sum(len(x.split()) for _, _, x in p["turns"]),
                            "n_company_names": p["n_company_names"], "n_analyst_names": p["n_analyst_names"], "n_intro": p["n_intro"],
                            "counts": counts_for(p, words)})
        have[u] = row
    with f.open("w") as fh:
        for u in sorted(have):
            fh.write(json.dumps(have[u]) + "\n")
    print(f"{series_suffix}: matched {len(company_urls(series_suffix, name, urls))}, fetched {n}, on file {len(have)}", flush=True)
    return list(have.values())


def load_tx(series_suffix: str) -> list[dict]:
    f = TXD / f"{series_suffix}.jsonl"
    return [json.loads(l) for l in f.open()] if f.exists() else []


if __name__ == "__main__":
    if sys.argv[1:2] == ["index"]:
        build_index()
