"""Deadline parsing and nested-ladder detection for r4_nested_deadline_term_structure (pure functions, no I/O).

A nested 'by date' event: >= 2 binary markets of one event whose rules_primary texts are identical once the date is
masked, and whose parsed deadlines differ. YES on rung j = 'the event happens before D_j', so prices must be
non-decreasing in D_j. The deadline comes from text fixed at listing (rules_primary / yes_sub_title / title), never
from close_time (a rung closes early when the event happens, which would leak the result)."""
from __future__ import annotations
import datetime as dt, re

MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_MONTH = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
# 'before Jul 1, 2026' / 'by July 25, 2026' / 'on or before Dec 31, 2025' / 'before Jul 1st, 2026' / 'before 11:59 PM ET on Jul 3, 2026'
RX_FULL = re.compile(r"\b(before|by|through|until|prior to)\b[^.]{0,40}?" + _MONTH + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d\d)", re.I)
RX_MON_YEAR = re.compile(r"\b(before|by)\s+(?:the\s+end\s+of\s+)?" + _MONTH + r",?\s+(20\d\d)\b", re.I)
RX_YEAR = re.compile(r"\bbefore\s+(20\d\d)\b", re.I)
RX_MASK = re.compile(_MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d\d|" + _MONTH + r",?\s+20\d\d|\b20\d\d\b|\b\d{1,2}:\d\d\s*(?:AM|PM)?\s*(?:ET|EST|EDT|UTC)?", re.I)
ET = dt.timezone(dt.timedelta(hours=-4))


def _m(s: str) -> int:
    return MON[s[:3].upper()]


def deadline(m: dict) -> tuple[int | None, str]:
    """Unix ts of the rung deadline (the instant after which the event no longer counts). 'before D' -> D 00:00 ET;
    'by D' / 'through D' / 'on or before D' -> D+1 00:00 ET. Month-only 'before July 2026' -> Jul 1 00:00 ET;
    'before 2027' -> Jan 1 00:00 ET."""
    for src in ("rules_primary", "yes_sub_title", "title"):
        txt = m.get(src) or ""
        x = RX_FULL.search(txt)
        if x:
            word = x.group(1).lower(); d = dt.datetime(int(x.group(4)), _m(x.group(2)), int(x.group(3)), tzinfo=ET)
            pre = txt[max(0, x.start() - 8):x.start()].lower()
            inclusive = word in ("by", "through", "until") or "on or" in pre or "on or before" in txt[x.start():x.end()].lower()
            if inclusive:
                d += dt.timedelta(days=1)
            return int(d.timestamp()), f"{src}:{word}{'+1d' if inclusive else ''}"
        x = RX_MON_YEAR.search(txt)
        if x:
            d = dt.datetime(int(x.group(3)), _m(x.group(2)), 1, tzinfo=ET)
            if "end of" in x.group(0).lower() or x.group(1).lower() == "by":
                d = dt.datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=ET)
            return int(d.timestamp()), f"{src}:month"
        x = RX_YEAR.search(txt)
        if x and src != "title":
            return int(dt.datetime(int(x.group(1)), 1, 1, tzinfo=ET).timestamp()), f"{src}:year"
    return None, "none"


def mask(rules: str) -> str:
    return re.sub(r"\s+", " ", RX_MASK.sub("<D>", rules or "")).strip().lower()


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def compact(m: dict, tier: str) -> dict:
    D, src = deadline(m)
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0], "tier": tier,
            "open": ts(m.get("open_time")), "close": ts(m.get("close_time")), "created": ts(m.get("created_time")),
            "latest_exp": ts(m.get("latest_expiration_time")), "settled": ts(m.get("settlement_ts")),
            "result": m.get("result"), "status": m.get("status"), "type": m.get("market_type"),
            "D": D, "D_src": src, "mask": mask(m.get("rules_primary") or ""),
            "vol": float(m.get("volume_fp") or m.get("volume") or 0), "title": (m.get("title") or "")[:140],
            "sub": (m.get("yes_sub_title") or "")[:80]}


def nested_events(markets: list[dict], min_rungs: int = 2) -> dict[str, list[dict]]:
    """event -> rungs (sorted by deadline) for events with >= min_rungs binary markets sharing one masked rule text
    and distinct deadlines. Duplicated deadlines (re-listed rungs) keep the earliest-opened market."""
    from collections import defaultdict
    by = defaultdict(list)
    for m in markets:
        if m["D"] and m["type"] in (None, "binary") and m["mask"]:
            by[(m["e"], m["mask"])].append(m)
    out = {}
    for (e, _), ms in by.items():
        seen = {}
        for m in sorted(ms, key=lambda m: (m["D"], m["open"] or 0)):
            seen.setdefault(m["D"], m)
        rungs = sorted(seen.values(), key=lambda m: m["D"])
        if len(rungs) >= min_rungs and (e not in out or len(rungs) > len(out[e])):
            out[e] = rungs
    return out


def thin(rungs: list[dict], min_gap_days: float = 7.0) -> list[dict]:
    """Keep a ladder with deadlines >= min_gap_days apart (greedy from the earliest deadline). Daily rungs listed in
    the days before a scheduled launch are a short-dated product; this family is about weeks-to-months decay.
    Uses listing fields only."""
    out = []
    for r in sorted(rungs, key=lambda r: r["D"]):
        if not out or r["D"] - out[-1]["D"] >= min_gap_days * 86400:
            out.append(r)
    return out
