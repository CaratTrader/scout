"""Frozen definitions for r7_announced_date_launch_ladder_forward (forward-only shadow, gate amendment (l)).

Written 2026-10-09 before any forward data. Nothing here may be changed after the first forward look except bug fixes
that are logged in preregistration.json's amendments list (and only if they cannot move a past decision).

Universe (listing fields only; never prices, volumes or results):
  series level  : the catalog title matches SERIES_INC and not SERIES_EXC, category not Sports / Mentions / Elections,
                  frequency one_off / custom / annual / monthly / weekly, not in EXCLUDE_SERIES; plus SEED_SERIES
                  (the product-release series of the r5/r6 samples that justified this test).
  market level  : binary, not a multivariate combo; early_close_condition is a hazard ("close and expire early if ...",
                  "If this event occurs, the market will close ...") and not a data release / winner / announcement of
                  results (r6 HAZ / NOT_HAZ, verbatim); the deadline D parses from rules_primary with r6's parser
                  (verbatim copy below) and latest_expiration_time lies in [D - 1 d, D + 16 d] (r6 sanity check);
                  rules_primary or title asks whether a product is released / launched / announced (PRODUCT_RX) and
                  is not a court, government, tariff, document, prisoner, IPO or corporate-action question (EXCL_RX).
Rule (frozen):  once a day in the 12:00 ET hour, every open qualifying rung with D - t in [24 h, 168 h] is looked at;
                the FIRST look with NO = 1 - yes_bid in [0.70, 0.95] is a paper taker buy of NO, held to settlement.
Fill:           worse of the look quote and the order book walked for a $5 stake (one orderbook call), fee rounded up to
                the cent on a 10-contract order of 0.07 p (1 - p).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
H, DAY = 3600, 86400

# ------------------------------------------------------------------ frozen rule parameters
BAND_LO, BAND_HI = 0.70, 0.95          # r6 lead: first-entry NO 0.70-0.95 (launch/release sub-pattern)
H_MIN, H_MAX = 24 * H, 168 * H         # D 1-7 days ahead (r6 decision grid 24..168 h)
DECISION_HOUR_ET = 12                  # looks are made only by passes in [12:00, 13:00) ET
STAKE = 5.0                            # $ per paper trade (live profile cap)
LOT_FOR_FEE = 10                       # fee convention of earlier rounds: ceil-to-cent on a 10-contract order

# ------------------------------------------------------------------ r6 hazard filters (verbatim)
HAZ = re.compile(r"(close and expire early if|if this event occurs, the market will close)", re.I)
NOT_HAZ = re.compile(r"(data is released|data become|economic data|winner is declared|title holder|federal register|"
                     r"results are|is announced|are announced)", re.I)

# ------------------------------------------------------------------ r6 deadline parser (verbatim copy of
# lab/kalshi/strategies/r6_near_deadline_nothing_happens_census_data.deadline; selftest checks equality on r6's frame)
_MON = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTH = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
RX_FULL = re.compile(r"\b(before|by|through|until|prior to|and|on or before)\s+(?:11:59\s*(?:PM|pm)\s*(?:ET|EST|EDT)?\s*(?:on\s+)?)?"
                     + MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)", re.I)
RX_MONYR = re.compile(r"\b(before|by|in|during)\s+(?:the\s+end\s+of\s+)?" + MONTH + r",?\s+(20\d\d)\b", re.I)
RX_YR = re.compile(r"\b(before|by|in|during)\s+(?:the\s+end\s+of\s+)?(20\d\d)\b", re.I)


def _et(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, tzinfo=ET).timestamp())


def _next_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def deadline(m: dict) -> tuple[int | None, str]:
    txt = m.get("rules_primary") or ""
    x = RX_FULL.search(txt)
    if x:
        w = x.group(1).lower()
        pre = txt[max(0, x.start() - 30):x.start()].lower()
        if w == "and" and "between" not in pre:
            x = None
        if x:
            try:
                d = _et(int(x.group(4)), _MON[x.group(2)[:3].lower()], int(x.group(3)))
            except Exception:
                d = None
            if d:
                incl = w in ("by", "through", "until", "and", "on or before") or "on or" in pre[-8:]
                return (d + DAY if incl else d), "rules:" + w
    x = RX_MONYR.search(txt)
    if x:
        w = x.group(1).lower(); y = int(x.group(3)); mo = _MON[x.group(2)[:3].lower()]
        if w == "before" and "end of" not in x.group(0).lower():
            return _et(y, mo, 1), "rules:before-month"
        y2, m2 = _next_month(y, mo)
        return _et(y2, m2, 1), "rules:" + w + "-month"
    x = RX_YR.search(txt)
    if x:
        w = x.group(1).lower(); y = int(x.group(2))
        if w == "before" and "end of" not in x.group(0).lower():
            return _et(y, 1, 1), "rules:before-year"
        return _et(y + 1, 1, 1), "rules:" + w + "-year"
    return None, "none"


# ------------------------------------------------------------------ product-release classification (frozen)
PRODUCT_RX = re.compile(r"\b(releas\w*|launch\w*|come[s]? out|premier\w*|debut\w*|unveil\w*|announc\w*|reveal\w*|"
                        r"drops?|dropped|available to the public|goes? live|go on sale|ships?)\b", re.I)
EXCL_RX = re.compile(
    r"(\bcourt\b|supreme|scotus|\bjudge|ruling|lawsuit|\bsues?\b|indict|tariff|congress|senate|house of representatives|"
    r"\bbill\b|\blaw\b|legislat|government|administration|white house|executive order|department of|\bdoj\b|\bfbi\b|"
    r"\bcia\b|federal|pentagon|\bepstein|\bfiles\b|documents?|\btapes?\b|\bmemo\b|autopsy|transcript|\brecords\b|"
    r"\breport\b|minutes|\bipo\b|initial public offering|go(?:es)? public|merger|acqui|\bagi\b|chatbot arena|lmarena|"
    r"benchmark|prisoner|hostage|detain|custody|\bjail|prison|freed|released from|\bfree\b|ceasefire|sanction|"
    r"\bfed\b|interest rate|\bcpi\b|inflation|payroll|unemployment|\bgdp\b|economic data|reserves?\b|"
    r"\bstrategic\b|election|ballot|\bpoll\b|treaty|\bmou\b|agreement|\bdeal\b|nominat|confirm(?:ed|ation)|"
    r"\bstock\b|shares|market cap|earnings|revenue|downloads|app store rank|token price|price of)", re.I)
# intraday launch-time ladders ("before Mar 6, 2025 at 7:30pm EST": r6's parser would read midnight) and launch-outcome
# props ("If, at the next SpaceX Starship launch ..., X occurs"): both excluded by r5 as not release-date questions
INTRADAY_RX = re.compile(r"(\b(?!11:59)\d{1,2}:\d{2}\s*(?:am|pm)|\bat the next\b)", re.I)   # "by 11:59 PM ET on <date>" is a day deadline
# kind of product, for reporting only (never used to select)
KIND = [("space_launch", r"starship|spacex|rocket|launch(?:es)? (?:of|to)|new glenn|vulcan|artemis|falcon|blue origin|neutron|rocket lab|orbit"),
        ("ai_model", r"\bgpt|gemini|claude|grok|llama|deepseek|qwen|sora|\bo\d\b|openai|anthropic|\bmodel\b|\bai\b|mistral|kimi"),
        ("music", r"album|song|single|mixtape|\bep\b|spotify|music|track"),
        ("game_media", r"\bgta\b|game|trailer|movie|film|season|episode|series|show|elder scrolls|switch|playstation|ps\d|xbox"),
        ("device_product", r"iphone|ipad|apple|airtag|vision pro|tesla|optimus|car\b|phone|device|hardware|watch|console"),
        ("other_product", r".")]

SERIES_INC = re.compile(
    r"(releas|launch|come out|comes out|drop(?:s)? (?:a |an |the |new )|debut(?!s? at)|unveil|trailer|new album|album release|"
    r"release date|mixtape|starship|rocket|new glenn|vulcan|artemis|falcon|iphone|ipad|pixel \d|galaxy s\d|switch 2|ps6|"
    r"playstation|xbox|gta|gpt|gemini|claude|grok|llama|deepseek|qwen|sora|veo|wrapped|new season|season \d+ (?:premiere|release))", re.I)
SERIES_EXC = re.compile(
    r"(court|supreme|scotus|ruling|judge|tariff|decision|congress|senate|\bhouse\b|\bbill\b|government|epstein|files|\bipo\b|"
    r"merger|acqui|\bfed\b|\brate\b|cpi|jobs|payroll|gdp|election|nominee|primary|\bwin\b|winner|\btop\b|#1|number one|chart|"
    r"billboard|streams|box office|rotten|score|ranking|\brank|how many|views|subscribers|mention|\bsay|price|above|below|"
    r"weather|temperature|approval|poll|best |grammy|oscar|emmy|award|who will|which |tweet|post|feature|land|explode|succe|"
    r"fail|crash|count|record|sales|revenue|earnings|stock|market cap|lawsuit|sue\b|ban\b|restrict|report|tax|budget|"
    r"sanction|prison|jail|arrest|indict|pardon|release from|hostage|prisoner|detain|freed|free\b)", re.I)
SERIES_CATS_SKIP = {"Sports", "Mentions", "Elections"}
SERIES_FREQ = {"one_off", "custom", "annual", "monthly", "weekly"}
# Catalog categories read by the weekly catalog diff (one call each; <= 10 per pass)
CATALOG_CATEGORIES = ["Entertainment", "Science and Technology", "Companies", "Financials", "Social", "Crypto",
                      "Commodities", "World", "Politics", "AI"]
# Product-release series of the r5 / r6 samples (seed; the market-level filter still applies to every rung)
SEED_SERIES = sorted({
    "KXSPACEXSTARSHIP", "SPACEXSTARSHIP", "KXSPCXLAUNCH", "KXARTEMISII", "KXNEXTVULCAN", "KXNEWGLENN",
    "KXGEMINI", "KXGPT", "KXCLAUDE", "KXCLAUDE5", "KXO3RELEASE", "KXDEEPSEEKR2RELEASE", "KXDEEPSEEKV4RELEASE", "KXGROK",
    "KXOAIPERSONALAGENT", "KXSPOTIFYWRAPPEDRELEASE", "GTA6", "KXGTA6", "KXGTA6ONTIME", "KXGTATRAILER",
    "KXMEDIARELEASEADDTRAILER", "KXMEDIARELEASEICEMAN", "KXALBUMRELEASEDATE", "KXSPOTIFYALBUMRELEASEDATEKANYE",
    "KXSPOTIFYALBUMRELEASEDATEDRAKE", "KXALBUMRELEASEDATEASAP", "KXSPOTIFYALBUMRELEASE", "KXSPOTIFYALBUMRELEASEDATEJACKBOYS2",
    "KXJACKBOYS2", "KXSPOTIFYALBUMRELEASEDATEBABYBOI", "KXALBUMRELEASEDATEUZI", "NEWALBUM", "KXIPHONERELEASE",
    "KXSWITCH2RELEASE", "KXVISIONPRO", "KXBIDENBOOK", "KXSTARSHIPFL",
})
# Clear non-products among the title matches (people, government texts, statistics, awards, counts), written from
# catalog titles only, before any forward data.
EXCLUDE_SERIES = {
    "KXRELEASEKILMAR", "KXRELEASEKHAN", "KXISTANBULMAYOR", "KXUSIRANMOU", "KXBOATSTRIKERELEASE", "KXBIDENTAPES", "KXIEAOIL",
    "KXAIREVIEW", "GPTPARAM", "KXGPTPARAM", "KXGPT55Y", "KXGBT55OY", "KXGEMINI35Y", "KXGEMINI35OY", "KXDEEPVREQ",
    "KXDEEPSHARE", "KXGPTFEES", "KXGPTCOST", "KXGPTAPP", "KXCLAUDEAPP", "KXGROKAPP", "KXGEMINIAPP", "SPACEXCOUNT",
    "KXSPACEXCOUNT", "KXLAUNCHCOUNTM", "KXLAUNCHES", "KXRKLBA", "KXRKLB", "KXAPPSTOREIPHONEGAME", "KXAPPSTOREIPADAPP",
    "KXAPPSTOREIPHONEAPP", "KXAPPSTOREIPADGAME", "KXGTA6SONGS", "KXGTA6ARTISTS", "KXSTREAMKAIGTAIV", "KXXBANNEDGROK",
    "KXMODELHIGH", "OAIAGI", "KXTARIFFDECISIONRELEASE", "KXEPSTEIN",
    "KXSTARSHIPLAUNCH", "KXSTARSHIP",   # launch-outcome props and intraday launch-time ladders (excluded by r5 too)
}


def series_candidate(s: dict) -> bool:
    """Catalog-level candidate (title wording, category, frequency); s is one /series row."""
    tk = s.get("ticker") or ""
    if tk in EXCLUDE_SERIES:
        return False
    if tk in SEED_SERIES:
        return True
    t = s.get("title") or ""
    return (s.get("frequency") in SERIES_FREQ and s.get("category") not in SERIES_CATS_SKIP
            and bool(SERIES_INC.search(t)) and not SERIES_EXC.search(t))


def ts_of(s) -> int | None:
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def qualify(m: dict) -> tuple[bool, str, int | None]:
    """(qualifies, reason, D) for one market row of a /markets listing. Listing fields only."""
    tk = m.get("ticker") or ""
    if m.get("market_type") not in (None, "binary"):
        return False, "not_binary", None
    if tk.startswith("KXMVE") or (m.get("event_ticker") or "").startswith("KXMVE"):
        return False, "mve", None
    ecc = m.get("early_close_condition") or ""
    if not HAZ.search(ecc) or NOT_HAZ.search(ecc):
        return False, "not_hazard", None
    D, how = deadline(m)
    if D is None:
        return False, "no_deadline", None
    le = ts_of(m.get("latest_expiration_time"))
    if le is not None and not (D - DAY <= le <= D + 16 * DAY):
        return False, "deadline_sanity", D
    txt = (m.get("rules_primary") or "") + " || " + (m.get("title") or "")
    if not PRODUCT_RX.search(txt):
        return False, "not_product_wording", D
    if EXCL_RX.search(txt):
        return False, "excluded_topic", D
    if INTRADAY_RX.search(m.get("rules_primary") or ""):
        return False, "intraday_or_outcome_prop", D
    return True, "ok:" + how, D


def kind(series: str, title: str, rules: str) -> str:
    s = f"{series} {title} {rules}".lower()
    for name, rx in KIND:
        if re.search(rx, s):
            return name
    return "other_product"


def fnum(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fee_per_contract(p: float) -> float:
    """Taker fee per contract on a 10-lot, rounded up to the cent per order (earlier rounds' convention)."""
    return math.ceil(round(LOT_FOR_FEE * 0.07 * p * (1 - p) * 100, 6)) / (LOT_FOR_FEE * 100)


def ret_per_dollar(px: float, won: bool) -> float:
    f = fee_per_contract(px)
    return ((1.0 - px - f) if won else (-px - f)) / px


def cp_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided exact-binomial (Clopper-Pearson) lower bound on the win rate (k successes of n)."""
    if n <= 0 or k <= 0:
        return 0.0
    if k == n:
        return alpha ** (1.0 / n)

    def tail(p):  # P(X >= k | n, p)
        return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if tail(mid) < alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def beta_bound_ret(k_win_products: int, n_products: int, avg_px: float, avg_fee: float) -> float:
    """Amendment (d): per-$ return at the 95% lower bound of the product-level win rate."""
    lb = cp_lower(k_win_products, n_products)
    return lb / avg_px - 1 - avg_fee / avg_px


def book_yes_bids(ob: dict) -> list[tuple[float, float]]:
    """YES bid levels [(price $, size)], best (highest) first, from an /orderbook response (either format)."""
    fp = ob.get("orderbook_fp") or {}
    if fp:
        lv = [(fnum(p), fnum(q)) for p, q in (fp.get("yes_dollars") or [])]
    else:
        o = ob.get("orderbook") or {}
        lv = [((fnum(p) or 0) / 100.0, fnum(q)) for p, q in (o.get("yes") or [])]
    lv = [(p, q) for p, q in lv if p is not None and q is not None and q > 0 and 0 < p < 1]
    return sorted(lv, key=lambda x: -x[0])


def walk_no(levels: list[tuple[float, float]], stake: float = STAKE) -> dict:
    """Buy NO for `stake` dollars against the YES bids (NO price = 1 - YES bid): VWAP, contracts, best level size."""
    if not levels:
        return {"best_no": None, "best_size": 0.0, "vwap": None, "contracts": 0.0, "filled_usd": 0.0, "short": True,
                "size_within_2c": 0.0}
    best_no = round(1 - levels[0][0], 4)
    left, cost, n = stake, 0.0, 0.0
    for yb, q in levels:
        p = 1 - yb
        take = min(q, left / p)
        if take <= 0:
            break
        cost += take * p; n += take; left -= take * p
        if left <= 1e-9:
            break
    within = sum(q for yb, q in levels if (1 - yb) <= best_no + 0.02 + 1e-9)
    return {"best_no": best_no, "best_size": levels[0][1], "vwap": round(cost / n, 4) if n else None,
            "contracts": round(n, 2), "filled_usd": round(cost, 2), "short": left > 1e-6, "size_within_2c": within}


def et_now(t: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(t, ET)


def et_date(t: float) -> str:
    return et_now(t).strftime("%Y-%m-%d")
