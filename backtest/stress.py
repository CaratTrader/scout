"""Stress the backtest: how much edge survives strict quote freshness?"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backtest.backtest as bt  # noqa: E402
from scout.config import settings_from_env  # noqa: E402

settings = settings_from_env()
kl = json.loads(bt.KLINES_CACHE.read_text())
windows = {}
for line in bt.WINDOWS_CACHE.read_text().splitlines():
    try:
        row = json.loads(line)
        windows[int(row["epoch"])] = row
    except Exception:
        continue

out = []
for age in (120.0, 30.0, 10.0):
    bt.MAX_QUOTE_AGE = age
    res = bt.replay(windows, kl, 0.01, settings)
    tl = res.pop("trade_log")
    ages = [t["quote_age"] for t in tl]
    offs = {}
    for t in tl:
        offs[t["t_off"]] = offs.get(t["t_off"], 0) + 1
    res["avg_quote_age"] = round(sum(ages) / len(ages), 1) if ages else None
    res["entries_by_tick"] = offs
    res["trades_per_day"] = round(res["trades"] / 30, 1)
    out.append(res)
    print(json.dumps(res, indent=1))

(bt.BT / "stress.json").write_text(json.dumps(out, indent=1))
