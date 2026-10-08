"""Counted, cached Kalshi GET for r2_infra_timestamp_fill_audit.

Every request goes through lab.us.data_refresh.fetch(url, pace) (waits for the paper bot's idle window, retries 429s)
and is logged with send/receive wall-clock times to data/kalshi_lab/strategies/r2_infra_timestamp_fill_audit/calls.log.
Hard budget: 195 calls for the task (task cap 200). kget() caches by URL; klive() never caches (live snapshots)."""
from __future__ import annotations
import hashlib, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K

OUT = Path(__file__).resolve().parents[3] / "data/kalshi_lab/strategies/r2_infra_timestamp_fill_audit"
API = OUT / "api"
LOG = OUT / "calls.log"
BUDGET = 195


def used() -> int:
    return sum(1 for _ in LOG.open()) if LOG.exists() else 0


def _call(url: str, pace: float) -> tuple[str, float, float]:
    if used() >= BUDGET:
        raise RuntimeError(f"Kalshi call budget {BUDGET} exhausted")
    t0 = time.time(); txt = fetch(url, pace=0); t1 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps({"t0": t0, "t1": t1, "url": url, "ok": bool(txt)}) + "\n")
    if pace:
        time.sleep(pace)
    return txt, t0, t1


def kget(path: str, pace: float = 1.15) -> dict:
    """Cached GET of K + path (path starts with '/')."""
    API.mkdir(parents=True, exist_ok=True)
    f = API / (hashlib.sha1(path.encode()).hexdigest()[:16] + ".json")
    if f.exists():
        return json.loads(f.read_text())["body"]
    txt, t0, t1 = _call(K + path, pace)
    body = json.loads(txt) if txt else {}
    if txt:
        f.write_text(json.dumps({"path": path, "t0": t0, "t1": t1, "body": body}))
    return body


def klive(path: str, pace: float = 0.0) -> tuple[dict, float, float]:
    """Uncached GET (live snapshot); returns (body, send time, receive time)."""
    txt, t0, t1 = _call(K + path, pace)
    return (json.loads(txt) if txt else {}), t0, t1
