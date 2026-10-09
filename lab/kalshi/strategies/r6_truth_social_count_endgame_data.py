"""r6_truth_social_count_endgame - data fetchers (stdlib only).

1. Roll Call / Factba.se Truth Social posts (the KXTRUTHSOCIAL resolution source), paged newest-first, 50 per page,
   cached raw per page under data/kalshi_lab/strategies/r6_truth_social_count_endgame/raw/rollcall/ and merged into
   posts.json {id: {date_utc, ts, repost, quote, deleted, reply, text60}}.
2. Kalshi (via lab.us.data_refresh.fetch, paced, logged to calls.log, cached by URL hash):
   - settled market lists (re-used from the round-5 lead cache: 0 calls)
   - event candlesticks (hourly / 1-minute) for every event

Usage: python -m lab.kalshi.strategies.r6_truth_social_count_endgame_data rollcall [stop_iso]
       python -m lab.kalshi.strategies.r6_truth_social_count_endgame_data kalshi_probe
"""
from __future__ import annotations
import datetime as dt, hashlib, json, os, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

OUT = Path("data/kalshi_lab/strategies/r6_truth_social_count_endgame")
RAW = OUT / "raw"; RC = RAW / "rollcall"; KC = RAW / "kalshi"
LEAD_CACHE = Path("data/kalshi_lab/strategies/lead_round5/api_cache")
KBASE = "https://api.elections.kalshi.com/trade-api/v2"
RC_URL = "https://rollcall.com/wp-json/factbase/v1/twitter?platform=truth+social&sort=date&sort_order=desc&page={p}&format=json"


def http(url: str, tries: int = 5) -> str:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research; stdlib)", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode()
        except Exception as e:  # noqa: BLE001
            print("retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return ""


def rollcall(stop_iso: str = "2026-02-01T00:00:00", max_pages: int = 400, refresh_pages: int = 0) -> dict:
    """Page newest-first until the oldest post on a page is before stop_iso. Pages already cached are re-used except the
    first `refresh_pages` (new posts shift page boundaries; duplicates are merged by id)."""
    RC.mkdir(parents=True, exist_ok=True)
    posts = {}
    pf = OUT / "posts.json"
    if pf.exists():
        posts = json.loads(pf.read_text())
    for p in range(1, max_pages + 1):
        f = RC / f"page_{p:04d}.json"
        if f.exists() and p > refresh_pages:
            txt = f.read_text()
        else:
            txt = http(RC_URL.format(p=p)); time.sleep(1.2)
            if not txt:
                print("page", p, "failed"); break
            f.write_text(txt)
        d = json.loads(txt); rows = d.get("data") or []
        if not rows:
            break
        for x in rows:
            s = x.get("social") or {}
            t = dt.datetime.fromisoformat(x["date"])
            posts[x["id"]] = {"ts": int(t.timestamp()), "date": x["date"], "repost": bool(s.get("repost_flag")),
                              "quote": bool(s.get("quote_flag")), "deleted": bool(x.get("deleted_flag")),
                              "deleted_date": s.get("deleted_date"), "reply": s.get("in_reply_to_screen_name"),
                              "media": x.get("media_type"), "text60": (x.get("text") or "")[:60]}
        oldest = min(dt.datetime.fromisoformat(x["date"]) for x in rows)
        if p % 10 == 0:
            print("page", p, "oldest", oldest.isoformat(), "posts", len(posts), flush=True)
        if oldest.replace(tzinfo=None) < dt.datetime.fromisoformat(stop_iso):
            break
    pf.write_text(json.dumps(posts))
    print("posts", len(posts))
    return posts


# ---------------------------------------------------------------- Kalshi
def _log(url: str, n: int) -> None:
    with open(OUT / "calls.log", "a") as fh:
        fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {n} {url}\n")


def calls_used() -> int:
    f = OUT / "calls.log"
    return sum(1 for _ in f.open()) if f.exists() else 0


def kget(path: str, budget: int = 200) -> dict:
    url = path if path.startswith("http") else KBASE + path
    KC.mkdir(parents=True, exist_ok=True)
    f = KC / (hashlib.sha1(url.encode()).hexdigest()[:24] + ".json")
    if f.exists():
        return json.loads(f.read_text())
    if calls_used() >= budget:
        raise RuntimeError("Kalshi call budget exhausted")
    from lab.us.data_refresh import fetch
    txt = fetch(url, pace=1.15)
    _log(url, len(txt))
    if not txt:
        return {}
    f.write_text(txt)
    return json.loads(txt)


def lead_markets() -> list[dict]:
    """Both the archive list (/historical/markets) and the live settled list cached by the round-5 lead."""
    out = {}
    for f in LEAD_CACHE.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        for m in d.get("markets") or []:
            if str(m.get("ticker", "")).startswith("KXTRUTHSOCIAL-"):
                out[m["ticker"]] = m
    return sorted(out.values(), key=lambda m: (m["close_time"], m.get("floor_strike") or -1))


def event_candles(event: str, start: int, end: int, period: int) -> dict:
    return kget(f"/series/KXTRUTHSOCIAL/events/{event}/candlesticks?start_ts={start}&end_ts={end}&period_interval={period}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rollcall"
    if cmd == "rollcall":
        rollcall(sys.argv[2] if len(sys.argv) > 2 else "2026-02-01T00:00:00",
                 refresh_pages=int(os.environ.get("RC_REFRESH", "0")))
    elif cmd == "kalshi_probe":
        ms = lead_markets(); print(len(ms), "markets")


# ---------------------------------------------------------------- candles (normalised to calib.quote format)
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_candles(cs: list[dict]) -> list[list]:
    """-> [[end_ts, yes_ask_close, yes_bid_close, yes_ask_low, yes_bid_high, volume], ...] (historical and live formats)."""
    out = []
    for c in cs:
        a, b = c.get("yes_ask") or {}, c.get("yes_bid") or {}
        ask = _f(a.get("close_dollars", a.get("close"))); bid = _f(b.get("close_dollars", b.get("close")))
        alo = _f(a.get("low_dollars", a.get("low"))); bhi = _f(b.get("high_dollars", b.get("high")))
        vol = _f(c.get("volume_fp", c.get("volume"))) or 0.0
        out.append([int(c["end_period_ts"]), ask, bid, alo, bhi, vol])
    out.sort()
    return out


CANDLES = OUT / "candles_1m.json"


def load_candles() -> dict:
    return json.loads(CANDLES.read_text()) if CANDLES.exists() else {}


def fetch_archive_market(ticker: str, start: int, end: int, store: dict) -> None:
    d = kget(f"/historical/markets/{ticker}/candlesticks?start_ts={start}&end_ts={end}&period_interval=1")
    store[ticker] = norm_candles(d.get("candlesticks") or [])


def fetch_live_markets(tickers: list[str], start: int, end: int, store: dict, max_cells: int = 10000) -> None:
    """Batch endpoint: refuses > 10,000 candles counted as markets x span-minutes; split the tickers accordingly."""
    span = (end - start) // 60 + 1; per = max(1, max_cells // span)
    for i in range(0, len(tickers), per):
        grp = tickers[i:i + per]
        d = kget(f"/markets/candlesticks?market_tickers={','.join(grp)}&start_ts={start}&end_ts={end}&period_interval=1")
        for x in d.get("markets") or []:
            store[x["market_ticker"]] = norm_candles(x.get("candlesticks") or [])
