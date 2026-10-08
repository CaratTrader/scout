"""r2_thin_city_anchor data helpers (no Kalshi calls here).

  cli   : NWS CLI daily highs 2015-2025 from IEM (json/cli.py, one call per station-year) for the thin cities and
          their liquid neighbours -> data/kalshi_lab/strategies/r2_thin_city_anchor/cli_hist.json
Usage: .venv/bin/python lab/kalshi/strategies/r2_thin_city_anchor_data.py cli"""
from __future__ import annotations
import json, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data/kalshi_lab/strategies/r2_thin_city_anchor"
OUT.mkdir(parents=True, exist_ok=True)
STATIONS = ("KEWR", "KNYC", "KTTN", "KPHL", "KSAN", "KLAX", "KSDF")
YEARS = range(2015, 2026)


def get(url: str) -> str:
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "scout-research"}), timeout=120) as r:
                return r.read().decode()
        except Exception as e:
            print("retry", i, str(e)[:80], flush=True); time.sleep(5 * (i + 1))
    return ""


def cli() -> None:
    f = OUT / "cli_hist.json"
    have = json.loads(f.read_text()) if f.exists() else {}
    for st in STATIONS:
        d = have.setdefault(st, {})
        for y in YEARS:
            if any(k.startswith(str(y)) for k in d):
                continue
            txt = get(f"https://mesonet.agron.iastate.edu/json/cli.py?station={st}&year={y}")
            try:
                rows = json.loads(txt).get("results") or []
            except Exception:
                rows = []
            n0 = len(d)
            for r in rows:
                h = r.get("high")
                if isinstance(h, (int, float)):
                    d[r["valid"]] = float(h)
            print(st, y, "days", len(d) - n0, flush=True)
            f.write_text(json.dumps(have))
            time.sleep(1.5)


if __name__ == "__main__":
    {"cli": cli}[sys.argv[1]]()
