"""Hourly gate check: log the live-money gate status and raise a macOS notification when
all three conditions pass (or when the bot is down/halted for a long time).
Runs under launchd as com.tradeinc.gatecheck. Never changes anything."""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "gate.log"
FLAG = ROOT / "data" / "gate_notified.json"


def notify(title: str, text: str) -> None:
    try:
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}" sound name "Glass"'], timeout=10)
    except Exception:
        pass


def main() -> None:
    with urllib.request.urlopen("http://127.0.0.1:8787/api/state", timeout=20) as r:
        s = json.loads(r.read().decode())
    g, st, b = s["gate"], s["stats"], s["bankroll"]
    line = (f"{datetime.now().strftime('%Y-%m-%d %H:%M')} status={s['status']} fills={g['fills']['value']}/{g['fills']['target']} "
            f"t={g['t_stat']['value']}/{g['t_stat']['target']} streak={g['positive_days']['value']}/{g['positive_days']['target']} "
            f"realized={b['realized']} today={b['today']} cash={b['cash']} pass={g['all_pass']}")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(line + "\n")
    print(line)
    state = {}
    if FLAG.exists():
        try:
            state = json.loads(FLAG.read_text())
        except Exception:
            state = {}
    if g["all_pass"] and not state.get("gate_open"):
        notify("Scout: live-money gate OPEN", f"{g['fills']['value']} fills, t={g['t_stat']['value']}, {g['positive_days']['value']} positive days. See docs/GO_LIVE_CHECKLIST.md")
        state["gate_open"] = time.time()
    elif not g["all_pass"]:
        state.pop("gate_open", None)
    if s["status"] == "down" and not state.get("down_notified"):
        notify("Scout: bot DOWN", f"no log writes for {int(s['log_age_s'])}s")
        state["down_notified"] = time.time()
    elif s["status"] != "down":
        state.pop("down_notified", None)
    FLAG.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
