"""Frozen same-question pair mapping for r4_xvenue_long_dated_gap (metadata only: titles, rules, dates; NO prices).

Kalshi side: settled binary markets from the cached /historical/markets and /markets listings (r3 researchers' raw
caches, read as data) plus this family's own live-tier listings. Polymarket side: gamma events found by search.
Each Kalshi market is paired with at most one Polymarket market whose resolution is the same question:

  tier A  identical condition, deadline and source (Fed meeting buckets, rate cut by date, number of cuts, aliens,
          Biden pardons, Fed-chair announcement dates, Trump attends UFC N)
  tier B  same question, immaterial wording difference (pardon vs "pardon, commutation or reprieve"; shutdown "on"
          the lapse date vs "by" it; Nobel joint-winner precedence; Trump-out death clause; Starship "before D+1"
          vs "by D"; first-100-days vs before May 1)
  excluded (tier C, listed in the file for the record, never traded): material rule differences
          (LLM leaderboard style control / rank vs score, Iran deal "formal signed deal" vs "announced agreement",
          TikTok "law in effect" vs "app unavailable", recession NBER-only vs NBER-or-GDP).

Decision window (ex-ante): S = min(Kalshi scheduled deadline parsed from the rules / ticker, Polymarket question
deadline); trade days are 14:00 UTC of days with S - 60 d <= t <= S - 1 d, after both markets are listed.
Kalshi scheduled life (S - open) must be >= 45 d. Written by build(); freeze() stores the sha256 and the time.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_xvenue_long_dated_gap_map [listings|build|freeze]"""
from __future__ import annotations
import calendar, datetime as dt, hashlib, json, re, sys, time, unicodedata
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_api import kget, used, OUT
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_poly import event as pevent
from lab.kalshi.strategies import r3_long_dated_longshot_no_repro as R3   # listing URLs + deadline parser (read only)

DAY = 86400
WINDOW_D = 60
ARCHIVE_CUT = 1786147200   # 2026-08-08: markets settled later are served by the live endpoints only
MIN_LIFE_D = 45
MAP = OUT / "mapping.json"
FREEZE = OUT / "mapping_freeze.json"
LIVE_LISTINGS = [   # live tier (settled after the 2026-08-08 archive cutoff); metadata only
    "/markets?series_ticker=KXFEDDECISION&status=settled&min_close_ts=1785542400&limit=1000",
    "/markets?series_ticker=KXTRUMPOUT27&status=settled&min_close_ts=1785542400&limit=1000",
    "/markets?series_ticker=KXALIENS&status=settled&min_close_ts=1785542400&limit=1000",
    "/markets?series_ticker=KXGOVSHUT&status=settled&min_close_ts=1785542400&limit=1000",
]
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})


def ts(s):
    if not s:
        return None
    s = str(s).replace("Z", "").replace(" ", "T")[:19]
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        try:
            return calendar.timegm(time.strptime(s[:10], "%Y-%m-%d"))
        except Exception:
            return None


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\u2018\u2019'`]", "", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", s)).strip()


def clean_label(s: str, fam: str = "") -> str:
    """Outcome name without the film / artist suffix, quotes or bracketed notes ('Adrien Brody – "The Brutalist"' -> 'Adrien Brody')."""
    s = re.sub(r"\[[^\]]*\]", "", s or "")
    parts = re.split(r"\s+[-\u2013\u2014]\s+", s.strip())
    s = parts[0] if parts and parts[0].strip() else s
    if fam == "grammys":
        s = re.sub(r"\s+by\s+.*$", "", s)
    return s.strip(" '\"\u2018\u2019\u201c\u201d")


def lastname(s: str) -> str:
    w = [x for x in norm(s).split() if x not in ("jr", "sr", "ii", "iii")]
    return w[-1] if w else ""


# ----------------------------------------------------------------------------------------------- Kalshi universe
def kalshi_raw() -> dict[str, dict]:
    out = {}
    urls = list(R3.listing_urls()) + LIVE_LISTINGS + AWARD_LISTINGS
    for u in urls:
        d = kget(u, allow_fetch=u in LIVE_LISTINGS + AWARD_LISTINGS)
        for m in (d or {}).get("markets", []) or []:
            if m.get("market_type") == "binary" and m.get("result") in ("yes", "no"):
                m = dict(m); m["_live"] = (ts(m.get("close_time")) or 0) >= ARCHIVE_CUT
                out[m["ticker"]] = m
    return out


def kalshi_S(m: dict) -> int | None:
    S, _ = R3.deadline(m)
    return S


# ----------------------------------------------------------------------------------------------- Polymarket side
def pmarkets(slug: str) -> list[dict]:
    e = pevent(slug)
    if not e:
        return []
    out = []
    for m in e.get("markets") or []:
        try:
            outs, toks, prices = json.loads(m.get("outcomes") or "[]"), json.loads(m.get("clobTokenIds") or "[]"), json.loads(m.get("outcomePrices") or "[]")
        except Exception:
            continue
        if outs != ["Yes", "No"] or len(toks) != 2:
            continue
        out.append({"id": m["id"], "slug": slug, "label": (m.get("groupItemTitle") or "").strip(), "q": m.get("question", ""),
                    "start": ts(m.get("startDate") or m.get("createdAt")), "end": ts(m.get("endDate")), "closed": ts(m.get("closedTime")),
                    "tok": toks[0], "vol": float(m.get("volumeNum") or 0), "resolved": prices in (["1", "0"], ["0", "1"]),
                    "yes_won": prices == ["1", "0"], "desc": (m.get("description") or e.get("description") or "")[:600]})
    return out


def date_in(text: str, year_hint: int | None = None) -> int | None:
    """'by May 31', 'January 31, 2026', 'By May 22' -> 00:00 UTC of that day (the 'by D' deadline day)."""
    x = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", text, re.I)
    if not x:
        return None
    y = int(x.group(3)) if x.group(3) else year_hint
    if not y:
        return None
    try:
        return calendar.timegm((y, MONTHS[x.group(1).lower()[:3] if x.group(1).lower() not in MONTHS else x.group(1).lower()], int(x.group(2)), 0, 0, 0))
    except Exception:
        return None


# ----------------------------------------------------------------------------------------------- family specs
FED = {  # Kalshi Fed-meeting event -> Polymarket event (meeting dates checked in build())
    "FEDDECISION-23MAY": "fed-interest-rates-may-2023", "FEDDECISION-23JUN": "fed-interest-rates-june-2023",
    "FEDDECISION-23JUL": "fed-interest-rates-july-2023", "FEDDECISION-23SEP": "fed-interest-rates-september-2023",
    "FEDDECISION-23NOV": "fed-interest-rates-november-2023", "FEDDECISION-23DEC": "fed-interest-rates-december-2023",
    "FEDDECISION-24JAN31": "fed-interest-rates-january-2024", "FEDDECISION-24MAR20": "fed-interest-rates-march-2024",
    "FEDDECISION-24MAY": "fed-interest-rates-may-2024", "FEDDECISION-24JUN": "fed-interest-rates-june-2024",
    "FEDDECISION-24JUL": "fed-interest-rates-july-2024", "FEDDECISION-24SEP": "fed-interest-rates-september-2024",
    "FEDDECISION-24NOV": "fed-interest-rates-november-2024", "KXFEDDECISION-24DEC": "fed-interest-rates-december-2024",
    "KXFEDDECISION-25JAN": "fed-interest-rates-january-2025", "KXFEDDECISION-25MAR": "fed-decision-in-march",
    "KXFEDDECISION-25MAY": "fed-decision-in-may-2025", "KXFEDDECISION-25JUN": "fed-decision-in-june",
    "KXFEDDECISION-25JUL": "fed-decision-in-july", "KXFEDDECISION-25SEP": "fed-decision-in-september",
    "KXFEDDECISION-25OCT": "fed-decision-in-october", "KXFEDDECISION-25DEC": "fed-decision-in-december",
    "KXFEDDECISION-26JAN": "fed-decision-in-january", "KXFEDDECISION-26MAR": "fed-decision-in-march-885",
    "KXFEDDECISION-26APR": "fed-decision-in-april", "KXFEDDECISION-26JUN": "fed-decision-in-june-825",
    "KXFEDDECISION-26JUL": "fed-decision-in-july-181", "KXFEDDECISION-26SEP": "fed-decision-in-september-762",
}
FED_BUCKET = {"cut 25bps": "25 bps decrease", "cut >25bps": "50+ bps decrease", "hike 0bps": "no change",
              "no change": "no change", "fed maintains rate": "no change", "no cut/hike": "no change",
              "hike 25bps": "25 bps increase", "hike >25bps": "50+ bps increase", "hike 50bps": "50 bps increase"}


def fed_norm(label: str) -> str:
    s = re.sub(r"\s+", " ", (label or "").lower().replace("?", "")).strip()
    s = re.sub(r" after .*meeting$", "", s).strip()
    return "no change" if s in ("0 bps increase", "no change") else s


NAMED = [  # (family, tier, Kalshi event prefix, Polymarket slug)  -- outcome = person, matched by name
    ("nobel_peace_2025", "B", "KXNOBELPEACE-25", "nobel-peace-prize-winner-2025"),
    ("trump_pardon_2025", "B", "KXTRUMPPARDON-26JAN01", "who-will-trump-pardon-in-2025"),
    ("trump_pardon_100d", "B", "KXTRUMPPARDON-25MAY01", "trump-pardons"),
    ("biden_pardon", "A", "KXBIDENPARDON-25JAN21", "who-will-biden-pardon"),
    ("fed_chair_nominee", "A", "KXFEDCHAIRNOM-29", "who-will-trump-nominate-as-fed-chair"),
    ("cabinet_confirm_def", "B", "KXSECDEF-26DEC31", "which-trump-picks-will-be-confirmed"),
    ("cabinet_confirm_ag", "B", "KXSECAG-26DEC31", "which-trump-picks-will-be-confirmed"),
    ("cabinet_confirm_hhs", "B", "KXSECHHS-26DEC31", "which-trump-picks-will-be-confirmed"),
    ("time_poy", "B", "TIME-24", "time-2024-person-of-the-year"),
    ("time_poy", "B", "KXTIME-25", "time-2025-person-of-the-year"),
    ("game_awards", "A", "GAMEAWARDS-2024", "game-of-the-year-2024"),
    ("eurovision", "A", "KXEUROVISION-26", "eurovision-winner-2026"),
] + [("oscars", "A", f"{k}-{y}", slug) for y, cats in (
        ("24", {"OSCARPIC": "oscars-2024-best-picture", "OSCARACTO": "oscars-2024-best-actor", "OSCARACTR": "oscars-2024-best-actress",
                "OSCARDIR": "oscars-2024-best-director", "OSCARSUPACTO": "oscars-2024-best-supporting-actor", "OSCARSUPACTR": "oscars-2024-best-supporting-actress"}),
        ("25", {"KXOSCARPIC": "oscars-best-picture", "KXOSCARACTO": "oscars-best-actor", "KXOSCARACTR": "oscars-best-actress",
                "KXOSCARDIR": "oscars-best-director", "KXOSCARSUPACTO": "oscars-best-supporting-actor", "KXOSCARSUPACTR": "oscars-best-supporting-actress"}),
        ("26", {"KXOSCARPIC": "oscars-2026-best-picture-winner", "KXOSCARACTO": "oscars-2026-best-actor-winner", "KXOSCARACTR": "oscars-2026-best-actress-winner",
                "KXOSCARDIR": "oscars-2026-best-director-winner", "KXOSCARSUPACTO": "oscars-2026-best-supporting-actor-winner",
                "KXOSCARSUPACTR": "oscars-2026-best-supporting-actress-winner"})) for k, slug in cats.items()] \
  + [("grammys", "A", f"{k}-{y}", slug) for y, cats in (
        ("66", {"GRAMAOTY": "grammys-2024-album-of-the-year", "GRAMSOTY": "grammys-2024-song-of-the-year", "GRAMROTY": "grammys-2024-record-of-the-year",
                "GRAMBNA": "grammys-2024-best-new-artist"}),
        ("67", {"KXGRAMAOTY": "grammys-album-of-the-year-2025", "KXGRAMSOTY": "grammys-song-of-the-year-2025", "KXGRAMROTY": "grammys-record-of-the-year",
                "KXGRAMBNA": "grammys-best-new-artist"}),
        ("68", {"KXGRAMAOTY": "grammys-album-of-the-year-winner", "KXGRAMSOTY": "grammys-song-of-the-year-winner",
                "KXGRAMROTY": "grammys-record-of-the-year-winner", "KXGRAMBNA": "grammys-best-new-artist-winner"})) for k, slug in cats.items()]
AWARD_LISTINGS = [f"/historical/markets?series_ticker={s}&limit=1000" for s in (
    "KXOSCARPIC", "KXOSCARACTO", "KXOSCARACTR", "KXOSCARDIR", "KXOSCARSUPACTO", "KXOSCARSUPACTR", "KXTIME", "KXGRAMAOTY", "KXGRAMSOTY",
    "KXGRAMROTY", "KXGRAMBNA", "KXGAMEAWARDS", "KXEUROVISION")]
ALIAS = {"sbf": "bankman fried", "sam bankman fried": "bankman fried", "diddy": "combs", "sean combs": "combs",
         "cz": "zhao", "changpeng zhao": "zhao", "rfk jr": "kennedy", "robert f kennedy jr": "kennedy",
         "fauci": "fauci", "anthony fauci": "fauci", "hillary clinton": "clinton", "joe exotic the tiger king": "exotic",
         "joe exotic": "exotic", "pope leo xiv": "leo", "jim biden": "jim biden", "james biden": "jim biden",
         "hunter biden": "hunter biden", "himself": "trump", "donald trump": "trump"}

SINGLE = [  # (family, tier, Kalshi ticker, Polymarket slug, Polymarket label or "" for single-market events)
    ("aliens", "A", "KXALIENS-26", "will-the-us-confirm-that-aliens-exist-in-2025", ""),
    ("aliens", "A", "KXALIENS-27-JUL26", "will-the-us-confirm-that-aliens-exist-before-2027", "June 30"),
    ("aliens", "A", "KXALIENS-26OCT", "will-the-us-confirm-that-aliens-exist-before-2027", "September 30"),
    ("shutdown", "B", "KXGOVSHUT-25OCT01", "us-government-shutdown-by-october-1", ""),
    ("shutdown", "B", "KXGOVSHUT-26JAN31", "will-there-be-another-us-government-shutdown-by-january-31", ""),
    ("shutdown", "B", "GOVSHUT-24JAN20", "will-there-be-a-us-government-shutdown-by-jan-20", ""),
    ("shutdown", "B", "KXGOVSHUT-26OCT01", "government-shutdown-by-october-1-20260610162414910", ""),
    ("trump_out", "B", "KXTRUMPOUT-26-TRUMP", "trump-out-as-president-in-2025", ""),
    ("trump_out", "B", "KXTRUMPOUT27-27-26APR01", "trump-out-as-president-by-march-31", ""),
    ("trump_out", "B", "KXTRUMPOUT27-27-26AUG01", "trump-out-as-president-by-july-31-20260626202639374", ""),
    ("trump_out", "B", "KXTRUMPOUT27-27-26SEP01", "trump-out-as-president-by-august-31-20260730185655928", ""),
    ("fed_chair_announce", "A", "KXPRESNOMFEDCHAIR-25", "trump-announces-fed-chair-nominee-by", "December 31, 2025"),
    ("fed_chair_announce", "A", "KXPRESNOMFEDCHAIR-25-26FEB", "trump-announces-fed-chair-nominee-by", "January 31, 2026"),
    ("fed_chair_announce", "A", "KXPRESNOMFEDCHAIR-25-26FEB15", "trump-announces-fed-chair-nominee-by", "February 14, 2026"),
    ("fed_chair_announce", "A", "KXPRESNOMFEDCHAIR-25-26MAR01", "trump-announces-fed-chair-nominee-by", "February 28, 2026"),
    ("openai_forprofit", "B", "KXOAIPROFIT-26-JAN01", "openai-becomes-a-for-profit-in-2025", ""),
    ("trump_attends", "A", "KXTRUMPATTEND-25OCT31", "will-trump-attend-ufc-320", ""),
    ("trump_attends", "A", "KXTRUMPUFC-UFC327", "will-trump-attend-ufc-327", ""),
    ("trump_attends", "A", "KXTRUMPUFC-324", "will-trump-attend-ufc-324", ""),
    ("trump_attends", "A", "KXTRUMPUFC-325", "will-trump-attend-ufc-325", ""),
    ("trump_attends", "A", "KXTRUMPATTEND-25-TRU", "will-trump-attend-ufc-319", ""),
    ("trump_attends", "A", "KXTRUMPATTEND", "president-trump-to-attend-world-cup-final-20260608152749044", ""),
    ("rate_cut_by", "A", "RATECUT-24JAN31", "fed-rate-cut-by", "January 31"),
    ("rate_cut_by", "A", "RATECUT-24MAR20", "fed-rate-cut-by", "March 20"),
    ("rate_cut_by", "A", "RATECUT-24MAY01", "fed-rate-cut-by", "May 1"),
    ("rate_cut_by", "A", "RATECUT-24JUN12", "fed-rate-cut-by", "June 12"),
    ("rate_cut_by", "A", "RATECUT-24JUL31", "fed-rate-cut-by", "July 31"),
    ("rate_cut_by", "A", "RATECUT-24SEP18", "fed-rate-cut-by", "September 18"),
    ("rate_cut_by", "A", "RATECUT-24NOV07", "fed-rate-cut-by", "November 7"),
    ("rate_cut_by", "B", "RATECUT-24DEC31", "fed-rate-cut-by", "December 18"),
]
CUTCOUNT = [("RATECUTCOUNT-24DEC31", "how-many-fed-rate-cuts-this-year"), ("KXRATECUTCOUNT-25DEC31", "how-many-fed-rate-cuts-in-2025")]
STARSHIP = {"KXSPACEXSTARSHIP-7": "spacex-flight-test-7", "KXSPACEXSTARSHIP-8": "spacex-starship-flight-test-8",
            "KXSPACEXSTARSHIP-9": "spacex-starship-flight-test-9", "KXSPACEXSTARSHIP": "spacex-starship-flight-test-10",
            "KXSPACEXSTARSHIP-11": "spacex-starship-flight-test-11", "KXSPACEXSTARSHIP-12": "spacex-starship-flight-test-12",
            "KXSPACEXSTARSHIP-13": "spacex-starship-flight-test-13", "KXSPACEXSTARSHIP-14": "spacex-starship-flight-test-14"}
EXCLUDED = {"KXLLM1": "LMArena: Kalshi 'rank' column (style control default) vs Polymarket 'arena score, style control off'",
            "KXUSAIRANAGREEMENT": "Kalshi requires a formal signed deal with enrichment limits and sanctions relief; Polymarket an announced mutual agreement",
            "TIKTOKBAN": "Kalshi: ban law in effect (illegal to distribute); Polymarket: app unavailable to most Americans",
            "RECSSNBER": "Kalshi NBER only; Polymarket NBER or two negative GDP quarters",
            "KXGDP": "Kalshi 'above X' thresholds vs Polymarket ranges (no single-market equivalent)"}


def row(k: dict, p: dict, fam: str, tier: str, how: str, S_p: int | None) -> dict:
    S_k = kalshi_S(k)
    S = min(x for x in (S_k, S_p) if x) if (S_k or S_p) else None
    op = ts(k.get("open_time"))
    return {"k": k["ticker"], "e": k["event_ticker"], "fam": fam, "tier": tier, "how": how,
            "k_title": (k.get("title") or "")[:140], "k_sub": (k.get("yes_sub_title") or "")[:80], "k_rule": (k.get("rules_primary") or "")[:300],
            "k_open": op, "k_close": ts(k.get("close_time")), "k_S": S_k, "S": S,
            "p_id": p["id"], "p_slug": p["slug"], "p_label": p["label"], "p_q": p["q"][:160], "p_tok": p["tok"], "p_start": p["start"],
            "p_end": p["end"], "p_S": S_p, "p_desc": p["desc"][:300], "k_hist": not k.get("_live", False),
            "life_ok": bool(S and op and S - op >= MIN_LIFE_D * DAY)}


def build() -> list[dict]:
    K = kalshi_raw()
    byev = defaultdict(list)
    for m in K.values():
        byev[m["event_ticker"]].append(m)
    rows, log = [], []
    # Fed meeting buckets
    for ke, slug in FED.items():
        ks = byev.get(ke, [])
        P = pmarkets(slug)
        if not ks or not P:
            log.append(f"FED {ke}: kalshi {len(ks)} poly {len(P)} -> skip"); continue
        meet = max(ts(k.get("close_time")) for k in ks)
        pend = max(p["end"] or 0 for p in P)
        if abs(pend - meet) > 3 * DAY:
            log.append(f"FED {ke}: meeting {meet} vs poly end {pend} mismatch -> skip"); continue
        plab = {fed_norm(p["label"]): p for p in P}
        for k in ks:
            want = FED_BUCKET.get(re.sub(r"\s+", " ", (k.get("yes_sub_title") or "").strip().lower()))
            p = plab.get(want) if want else None
            if p:
                rows.append(row(k, p, "fed_meeting", "A", f"bucket {k.get('yes_sub_title')} = {p['label']}", meet))
            else:
                log.append(f"FED {k['ticker']} '{k.get('yes_sub_title')}' no identical Poly bucket among {sorted(plab)}")
    # named outcomes: full normalized name first, then a unique last-word match
    for fam, tier, pre, slug in NAMED:
        P = pmarkets(slug)
        pfull, plast = defaultdict(list), defaultdict(list)
        for p in P:
            lab = p["label"] or p["q"]
            if p["vol"] <= 0 or re.match(r"^(person|individual|company|other|tie)\b", lab.strip().lower()) or "arch" in p["q"][:5].lower():
                continue
            lab = clean_label(lab, fam)
            pfull[ALIAS.get(norm(lab), norm(lab))].append(p)
            plast[ALIAS.get(norm(lab), lastname(lab))].append(p)
        S_p = max((p["end"] or 0) for p in P) if P else None
        for e, ks in byev.items():
            if not (e == pre or e.startswith(pre + "-") or (e.startswith(pre) and len(e) == len(pre) + 1)):
                continue
            for k in ks:
                sub = clean_label((k.get("yes_sub_title") or "").strip(), fam)
                if not sub or sub.lower() == "tie":
                    continue
                c = pfull.get(ALIAS.get(norm(sub), norm(sub)), [])
                if len(c) != 1:
                    c = plast.get(ALIAS.get(norm(sub), lastname(sub)), [])
                if len(c) == 1:
                    rows.append(row(k, c[0], fam, tier, f"name {sub} = {c[0]['label']}", S_p or None))
                elif len(c) > 1:
                    log.append(f"{fam} {k['ticker']} '{sub}' ambiguous: {[p['label'] for p in c]}")
    # single questions / ladders with an explicit label
    for fam, tier, kt, slug, lab in SINGLE:
        k = K.get(kt)
        P = pmarkets(slug)
        if not k or not P:
            log.append(f"{fam} {kt}: kalshi {'ok' if k else 'missing'} poly {len(P)} -> skip"); continue
        c = [p for p in P if (not lab and len(P) == 1) or norm(p["label"]) == norm(lab)]
        if len(c) != 1:
            log.append(f"{fam} {kt}: label '{lab}' -> {len(c)} candidates"); continue
        p = c[0]
        yh = dt.datetime.utcfromtimestamp(kalshi_S(k) or ts(k.get("close_time")) or p["end"]).year
        S_p = date_in(lab or p["q"], yh)
        S_p = S_p + DAY if S_p else p["end"]   # 'by D' -> deadline end of day D
        rows.append(row(k, p, fam, tier, f"single {lab or p['q'][:60]}", S_p))
    # number of cuts in a year (exact counts only)
    for pre, slug in CUTCOUNT:
        P = pmarkets(slug)
        pby = {}
        for p in P:
            x = re.match(r"^(\d+)(?:\s*\(|$)", p["label"].strip())
            if x and "more" not in p["label"] and "+" not in p["label"]:
                pby[int(x.group(1))] = p
        S_p = max((p["end"] or 0) for p in P) if P else None
        for k in byev.get(pre, []):
            sub = (k.get("yes_sub_title") or "").lower()
            x = re.match(r"^(?:exactly\s+)?(\d+)\s+cuts?$", sub.strip())
            if x and int(x.group(1)) in pby:
                rows.append(row(k, pby[int(x.group(1))], "cut_count", "A", f"count {sub} = {pby[int(x.group(1))]['label']}", S_p))
    # Starship flight-test date ladders: Kalshi 'before D+1' == Polymarket 'by D'
    for ke, slug in STARSHIP.items():
        P = [p for p in pmarkets(slug) if re.search(r"launch by", p["q"], re.I)]
        for k in byev.get(ke, []):
            Sk = kalshi_S(k)
            if not Sk:
                continue
            yh = dt.datetime.utcfromtimestamp(Sk).year
            c = []
            for p in P:
                d = date_in(p["label"] or p["q"], yh) or date_in(p["q"], yh)
                if d and d + DAY == Sk:
                    c.append(p)
            if len(c) >= 1:
                p = sorted(c, key=lambda p: -p["vol"])[0]
                rows.append(row(k, p, "starship", "B", f"date {k.get('yes_sub_title')} = {p['label']}", Sk))
    # one pair per Kalshi market and one Kalshi market per Polymarket market (earliest-listed Kalshi market kept)
    seen, seen_p, out = set(), set(), []
    for r in sorted(rows, key=lambda r: (r["k_open"] or 0, r["k"])):
        if r["k"] not in seen and r["p_id"] not in seen_p:
            seen.add(r["k"]); seen_p.add(r["p_id"]); out.append(r)
    out.sort(key=lambda r: (r["fam"], r["S"] or 0, r["k"]))
    for r in out:
        w0 = max(r["S"] - WINDOW_D * DAY, r["k_open"] or 0, r["p_start"] or 0) if r["S"] else None
        w1 = (r["S"] - DAY) if r["S"] else None
        r["w0"], r["w1"] = w0, w1
        r["window_ok"] = bool(w0 and w1 and w1 > w0 and r["life_ok"] and (r["k_close"] or 0) > w0)
    doc = {"built": int(time.time()), "rule": __doc__, "excluded_families": EXCLUDED, "log": log, "pairs": out}
    MAP.write_text(json.dumps(doc, indent=1))
    print("pairs", len(out), "window_ok", sum(r["window_ok"] for r in out), "events", len({r["e"] for r in out if r["window_ok"]}))
    fam = defaultdict(lambda: [0, 0, set()])
    for r in out:
        f = fam[r["fam"]]; f[0] += 1; f[1] += r["window_ok"]; f[2].add(r["e"]) if r["window_ok"] else None
    for k, v in sorted(fam.items()):
        print(f"  {k:22s} pairs {v[0]:4d} tradeable {v[1]:4d} events {len(v[2]):3d}")
    print("log lines", len(log), "| kalshi calls used", used())
    return out


def freeze() -> None:
    txt = MAP.read_text()
    FREEZE.write_text(json.dumps({"sha256": hashlib.sha256(txt.encode()).hexdigest(), "frozen_at": int(time.time()),
                                  "frozen_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                                  "note": "written before any Kalshi candle or Polymarket price of a mapped pair was read by this family"}, indent=1))
    print(FREEZE.read_text())


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "build"
    if w == "listings":
        kalshi_raw(); print("calls used", used())
    elif w == "build":
        build()
    elif w == "freeze":
        freeze()
