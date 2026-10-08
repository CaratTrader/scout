"""Write data/kalshi_lab/strategies/r2_sports_lines_ext/result.json from the study's outputs (kill.json, discovery.json,
validation.json, live_report.json, live_lag.json). Usage: python -m lab.kalshi.strategies.r2_sports_lines_ext_result"""
from __future__ import annotations
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies import r2_sports_lines_ext_fetch as F

OUT = F.OUT


def main() -> dict:
    kill = json.loads((OUT / "kill.json").read_text())
    disc = json.loads((OUT / "discovery.json").read_text())
    val = json.loads((OUT / "validation.json").read_text())
    live = json.loads((OUT / "live_report.json").read_text()) if (OUT / "live_report.json").exists() else {}
    lag = json.loads((OUT / "live_lag.json").read_text()) if (OUT / "live_lag.json").exists() else {}
    ok = [x for x in disc if x[2]["n"] >= 30]
    best = max(ok, key=lambda x: x[2]["ret_per_dollar"])
    vrows = []
    for s in val:
        vrows.append({k: s.get(k) for k in ("rule", "n", "events", "win", "avg_px", "ret_per_dollar", "t", "ret_wo3", "half1", "half2", "trades_per_day")})
    res = {
        "name": "r2_sports_lines_ext",
        "verdict": "dead",
        "kalshi_calls_used": F.calls(),
        "variants_examined": len(disc),
        "discovery_best": {"rule": best[0], "n": best[2]["n"], "ret_per_dollar": best[2]["ret_per_dollar"], "t": best[2]["t"]},
        "validation": vrows,
        "kill_test_L15": kill["kill_L15"],
        "calibration_L15": kill["calibration_L15_all"],
        "lead_drift": kill["lead_drift"],
        "recon": {"disc": kill["recon_disc"], "all": kill["recon_all"]},
        "live_snapshots": {k: v["summary"].get("all") for k, v in live.items()},
        "live_lag": {k: v for k, v in lag.items() if k != "moves"},
    }
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    return res


if __name__ == "__main__":
    print(json.dumps(main(), indent=1)[:3000])
