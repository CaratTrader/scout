"""Counted, cached Kalshi fetch for the r2_chart_markets researcher (budget 200 HTTP attempts, logged to calls.log).
Waits for the paper bot's idle window (lab.us.data_refresh.bot_idle); retries only 429 / 5xx / network errors.
Also a polite cached fetcher for free outside sources (kworb, Netflix Top 10, Wayback)."""
from __future__ import annotations
import hashlib, json, sys, time, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import bot_idle, K

OUT = Path("data/kalshi_lab/strategies/r2_chart_markets")
RAW = OUT / "raw"
LOG = OUT / "calls.log"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def _cache(key: str) -> Path:
    return RAW / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".json")


def kget(path: str, tries: int = 4, cache: bool = True) -> dict:
    OUT.mkdir(parents=True, exist_ok=True); RAW.mkdir(parents=True, exist_ok=True)
    cf = _cache(path)
    if cache and cf.exists():
        return json.loads(cf.read_text())
    for i in range(tries):
        if used() >= BUDGET:
            raise RuntimeError("Kalshi call budget exhausted")
        bot_idle()
        code = 200; txt = ""
        try:
            with urllib.request.urlopen(urllib.request.Request(K + path, headers={"User-Agent": "scout-research", "Accept": "application/json"}), timeout=120) as r:
                txt = r.read().decode()
        except urllib.error.HTTPError as e:
            code = e.code; txt = e.read().decode()[:300]
        except Exception as e:
            code = -1; txt = str(e)[:200]
        with LOG.open("a") as f:
            f.write(f"{int(time.time())}\t{code}\t{len(txt)}\t{path[:220]}\n")
        time.sleep(1.15)
        if code == 200:
            d = json.loads(txt)
            if cache:
                cf.write_text(txt)
            return d
        if code in (429, -1) or code >= 500:
            time.sleep(6 * (i + 1)); continue
        return {"_error": code, "_body": txt}
    return {"_error": "retries"}


def wget(url: str, pace: float = 1.5, tries: int = 3, ua: str = "Mozilla/5.0 (research; standard-library urllib)") -> str:
    """Cached GET of an outside (non-Kalshi) page."""
    sub = OUT / "web"; sub.mkdir(parents=True, exist_ok=True)
    cf = sub / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".txt")
    if cf.exists():
        return cf.read_text()
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=90) as r:
                txt = r.read().decode("utf-8", "replace")
            cf.write_text(txt); time.sleep(pace); return txt
        except urllib.error.HTTPError as e:
            if e.code == 404:
                cf.write_text(""); return ""
            time.sleep(5 * (i + 1))
        except Exception:
            time.sleep(5 * (i + 1))
    return ""
