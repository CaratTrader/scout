"""Frozen parse rules for the r5_scheduled_release_poller sources (shared by the backtest and the forward logger).

Written before any Kalshi price was joined to an article time. Changing anything here after the freeze date in
preregistration.json invalidates the forward test.

Billboard 200 (KXTOPALBUM): the source fires on the first English billboard.com post published on the reveal day whose
title names the Billboard 200 together with a No. 1 phrase (B200_RX + NO1_RX). The album is the first quoted span of
the title (curly or straight quotes). Matching to Kalshi yes_sub_title: normalised equality, else containment either
way (shorter side >= 3 characters); exactly one market may match. Zero matches: the #1 is unlisted, so every listed
market is decided NO, but only markets whose normalised sub shares no token of >= 3 letters with the album name are
traded, and only if difflib similarity of the normalised strings is < 0.6 (guards against reissue / deluxe / stylised
naming such as '$ome $exy $ongs 4 U'). '$' is read as 's'. Two or more matches: ambiguous, no trade.
Amendment before any price was joined (2026-10-08): the '$'->'s' mapping and the difflib guard were added after the
parse was checked against settled results (1 wrong-side NO out of 88 parsed weeks, KXTOPALBUM-25MAR01); no price or
return was looked at.
"""
from __future__ import annotations
import difflib, html, re, unicodedata

B200_RX = re.compile(r"billboard\s*200", re.I)
NO1_RX = re.compile(r"(no\.\s*1\b|number one|\batop\b|\btops\b|\btopping\b|\brules\b|\breigns?\b|\bsummit\b|\bcrowned\b|"
                     r"\bthrone\b|\btop spot\b|\bleads?\b|\bfirst week at\b|\bback on top\b|\bhits the top\b|\bat the top\b)", re.I)
NOT_RX = re.compile(r"(global|excl\.|streaming albums|album sales|latin albums|country albums|rap albums|r&b|"
                    r"rock albums|soundtrack albums|catalog albums|vinyl albums|year-end|decade|all-time|of all time|"
                    r"chart rewind|ask billboard|podcast|predict|forecast|on track|\bcould\b|\baims\b|\beyes\b|headed|set to)", re.I)
NO_OTHER_RX = re.compile(r"no\.\s*([2-9]|1[0-9])\b", re.I)
OPENERS = "\u2018\u201c'\""
CLOSERS = "\u2019\u201d'\""


def clean_title(t: str) -> str:
    return html.unescape(t or "").replace("\u00a0", " ").strip()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", clean_title(s)).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("'", "").replace("\u2019", "").replace("$", "s")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_b200_no1_title(title: str) -> bool:
    t = clean_title(title)
    if NO_OTHER_RX.search(t) and not re.search(r"no\.\s*1\b", t, re.I):
        return False
    return bool(B200_RX.search(t) and NO1_RX.search(t) and not NOT_RX.search(t))


def album_from_title(title: str) -> str | None:
    """First quoted span. An opener is a quote mark at the start or after whitespace/'('; a closer is a quote mark
    followed by a non-letter or the end (so possessives "Wallen's" and contractions "I'm" are not boundaries)."""
    t = clean_title(title)
    i = 0
    while i < len(t):
        c = t[i]
        if c in OPENERS and (i == 0 or t[i - 1].isspace() or t[i - 1] in "(\u2014-") and c != "\u2019":
            j = i + 1
            while j < len(t):
                if t[j] in CLOSERS and (j + 1 == len(t) or not t[j + 1].isalpha()) and j > i + 1:
                    a = t[i + 1:j].strip().rstrip(",")
                    return a or None
                j += 1
            return None
        i += 1
    return None


def tokens(s: str) -> set[str]:
    return {w for w in norm(s).split() if len(w) >= 3 and w not in {"the", "and", "for", "you", "deluxe", "edition", "version"}}


def match_markets(album: str, subs: dict[str, str]) -> dict:
    """subs: ticker -> yes_sub_title. Returns {"yes": ticker|None, "no": [tickers], "status": ...} (frozen rule)."""
    a = norm(album)
    if len(a) < 2:
        return {"yes": None, "no": [], "status": "unparsed"}
    exact = [t for t, s in subs.items() if norm(s) == a]
    if len(exact) == 1:
        return {"yes": exact[0], "no": [t for t in subs if t != exact[0]], "status": "exact"}
    cont = [t for t, s in subs.items() if min(len(norm(s)), len(a)) >= 3 and (norm(s) in a or a in norm(s))]
    if len(exact) > 1 or len(cont) > 1:
        return {"yes": None, "no": [], "status": "ambiguous"}
    if len(cont) == 1:
        return {"yes": cont[0], "no": [t for t in subs if t != cont[0]], "status": "contain"}
    at = tokens(album)
    safe = [t for t, s in subs.items() if not (tokens(s) & at) and difflib.SequenceMatcher(None, norm(s), a).ratio() < 0.6]
    return {"yes": None, "no": safe, "status": "unlisted"}
