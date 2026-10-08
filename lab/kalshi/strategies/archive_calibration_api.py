"""Counted Kalshi fetch for the archive_calibration researcher: every HTTP attempt is logged (budget 200).
Waits for the paper bot's idle window (lab.us.data_refresh.bot_idle); retries only 429 / 5xx / network errors."""
from __future__ import annotations
import json, sys, time, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import bot_idle, K

OUT = Path("data/kalshi_lab/strategies/archive_calibration")
LOG = OUT / "calls.log"
BUDGET = 200


def used() -> int:
    return len(LOG.read_text().splitlines()) if LOG.exists() else 0


def kget(path: str, tries: int = 4) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
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
            f.write(f"{int(time.time())}\t{code}\t{len(txt)}\t{path[:200]}\n")
        time.sleep(1.15)
        if code == 200:
            return json.loads(txt)
        if code in (429, -1) or code >= 500:
            time.sleep(6 * (i + 1)); continue
        return {"_error": code, "_body": txt}
    return {"_error": "retries"}
