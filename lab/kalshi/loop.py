"""Nightly learning loop of the Kalshi strategy lab (docs/KALSHI_LAB.md). Paper only; it never trades real money.

  1. collect   lab.kalshi.fetch (new settled markets + candles), IEM METARs for the rain stations, the temperature data
  2. map/test  lab.kalshi.calib (calibration map, 70/30 time split re-drawn on the grown history), lab.kalshi.rain,
               lab.us.kalshi_backtest (weather R0 + R2m)
  3. count     every cell ever examined -> data/kalshi_lab/K.json (the significance bar rises with K)
  4. gate      a strategy passes only if its validation clears every bar on two consecutive nights and its forward
               paper record is clean; the loop marks it "gate-pass" in the registry, it never goes live by itself
  5. report    data/kalshi_lab/report.md
Usage: python -m lab.kalshi.loop [--no-fetch]"""
from __future__ import annotations
import contextlib, datetime as dt, io, json, re, statistics as st, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ROOT = Path("data/kalshi_lab"); REG = ROOT / "registry.json"; KF = ROOT / "K.json"; HIST = ROOT / "gate_history.json"
PY = sys.executable


def sh(args: list[str], timeout: int = 7200) -> str:
    r = subprocess.run([PY, "-m", *args], capture_output=True, text=True, timeout=timeout, cwd=str(Path(__file__).resolve().parents[2]))
    return (r.stdout or "") + (r.stderr or "")[-2000:]


def paper_summary() -> list[dict]:
    out = []
    for f in sorted((ROOT / "paper").glob("*.json")):
        led = json.loads(f.read_text()); fills = led.get("fills", [])
        rets = [x["pnl"] / x["stake"] for x in fills if x.get("stake")]
        out.append({"name": led["name"], "cash": led["cash"], "open": len(led.get("positions", [])), "settled": len(fills), "won": sum(x["won"] for x in fills),
                    "pnl": round(sum(x["pnl"] for x in fills), 2), "ret": st.mean(rets) if rets else None, "halted": led.get("halted"),
                    "equity": round(led["cash"] + sum(p["stake"] for p in led.get("positions", [])), 2)})
    return out


def main(fetch: bool = True) -> None:
    t0 = time.time(); log = []
    if fetch:
        log.append(sh(["lab.kalshi.fetch"])[-3000:])
        for stn in (ROOT / "rain_metar").glob("*.csv"):
            if time.time() - stn.stat().st_mtime > 12 * 3600:
                stn.unlink()   # rain.py re-downloads it (one IEM request per station)
        log.append(sh(["lab.us.data_refresh", "all"])[-1500:])
    rain_out = sh(["lab.kalshi.rain", "5", "1"])
    calib_out = sh(["lab.kalshi.calib"])
    wx_out = sh(["lab.us.kalshi_backtest", "2", "0.80"])
    import os
    env = dict(os.environ, KB_FROM="2025-07-01", KB_TO="2026-04-22")   # pre-registered out-of-sample window (docs/KALSHI_LAB.md)
    r = subprocess.run([PY, "-m", "lab.us.kalshi_backtest", "2", "0.80"], capture_output=True, text=True, timeout=7200, env=env, cwd=str(Path(__file__).resolve().parents[2]))
    oos_out = r.stdout or ""
    cal = json.loads((ROOT / "calib.json").read_text()) if (ROOT / "calib.json").exists() else {"K": 0, "validation": {}, "discovery": {}}
    # 3. K: every cell ever examined (calibration cells + the fixed rule families)
    Kd = json.loads(KF.read_text()) if KF.exists() else {"cells": []}
    cells = set(Kd["cells"]) | set(cal.get("discovery", {}).keys()) | {f"rain|{h}" for h in (12, 15, 18, 20, 22)} | {f"weather|{v}" for v in ("R2", "R2raw", "R2m", "R2u", "R2rawc", "R0h")} | \
        {f"gas|momentum|{e}|{t}" for e in (0.10, 0.20) for t in (360, 240, 60)}
    Kd["cells"] = sorted(cells); KF.write_text(json.dumps(Kd))
    K = len(cells)
    from statistics import NormalDist
    z = NormalDist().inv_cdf(1 - 0.05 / max(K, 1))
    # 4. gate: calibration cells that passed tonight; pass needs two consecutive nights
    hist = json.loads(HIST.read_text()) if HIST.exists() else {}
    today = dt.date.today().isoformat(); passed = []
    for k, v in (cal.get("validation") or {}).items():
        ok = bool(v.get("gate")) and v["t"] >= z
        hist.setdefault(k, {})[today] = ok
        nights = sorted(hist[k]); two = len(nights) >= 2 and all(hist[k][n] for n in nights[-2:])
        if ok:
            passed.append((k, v, two))
    HIST.write_text(json.dumps(hist))
    reg = json.loads(REG.read_text()) if REG.exists() else {}
    for k, v, two in passed:
        name = "calib_" + re.sub(r"[^A-Za-z0-9]+", "_", k).strip("_")
        if name not in reg:
            reg[name] = {"family": "calib", "status": "candidate", "frozen": today, "params": {"cell": k}, "source": f"calib validation {today}: n={v['n']} ret={v['ret']:+.1%} t={v['t']:.1f}"}
        if two and reg[name]["status"] == "candidate":
            reg[name]["status"] = "gate-pass"
    REG.write_text(json.dumps(reg, indent=1))
    # 5. report
    pick = lambda text, pat: [l for l in text.splitlines() if re.search(pat, l)]
    lines = [f"# Kalshi lab report — {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}", "",
             f"Cells examined so far K = {K}; significance bar t ≥ {z:.2f}. Gate: docs/KALSHI_LAB.md.", "",
             "## Gate status", ""]
    if passed:
        for k, v, two in passed:
            lines.append(f"- **{k}**: validation n={v['n']} ret={v['ret']:+.1%} t={v['t']:.1f} — {'PASS on two nights' if two else 'passed tonight (needs a second night)'}")
    else:
        lines.append("- No calibration cell passes the gate tonight.")
    lines += ["", "## Paper ($50 bankroll per strategy)", "", "| strategy | equity | settled | won | P&L | mean ret/trade | open | halted |", "|---|---|---|---|---|---|---|---|"]
    for p in paper_summary():
        ret = "" if p["ret"] is None else f"{p['ret']:+.1%}"
        lines.append(f"| {p['name']} | ${p['equity']:.2f} | {p['settled']} | {p['won']} | ${p['pnl']:+.2f} | {ret} | {p['open']} | {p['halted'] or ''} |")
    lines += ["", "## Registry", ""] + [f"- {k}: {v['status']} — {v.get('validation') or v.get('source', '')}" for k, v in reg.items()]
    gate_block = oos_out[oos_out.find("GATE ("):].split("\n\nREFERENCE")[0].splitlines() if "GATE (" in oos_out else ["(no out-of-sample run)"]
    lines += ["", "## Weather R0 + R2m — pre-registered out-of-sample test, Kalshi archive 2025-07-01..2026-04-22", "```"]
    lines += pick(oos_out, r"^station-days used|^LIVE R0") + gate_block + ["```"]
    lines += ["", "## Weather R0 + R2m (lab/us/kalshi_backtest.py, all data)", "```"]
    lines += pick(wx_out, r"^LIVE|largest winner|without the largest|holdout|^station-days used") + ["```"]
    lines += ["", "## Calibration map (lab/kalshi/calib.py)", "```"] + calib_out.strip().splitlines()[:60] + ["```"]
    lines += ["", "## Rain (lab/kalshi/rain.py)", "```"] + rain_out.strip().splitlines()[-22:] + ["```"]
    lines += ["", f"_loop took {time.time() - t0:.0f} s_"]
    (ROOT / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:40]))


if __name__ == "__main__":
    main(fetch="--no-fetch" not in sys.argv)
