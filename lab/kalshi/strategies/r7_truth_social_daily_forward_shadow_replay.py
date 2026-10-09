"""DESCRIPTIVE ONLY (run after the freeze; not evidence, not used for any choice): the frozen rule D1 replayed on the 7
KXTRUTHSOCIALD launch events (2026-10-01..07) with the hourly candles cached by r2_launch_window and the Roll Call posts
cached by r6 (flags as of each decision time). Its only purpose is a rough signal rate and price level for the power
statement. Hourly bars cannot reproduce the forward fill (no 1-minute delay, no book, quotes carried up to 3 h), so
returns here say nothing about the edge.
Usage: .venv/bin/python -m lab.kalshi.strategies.r7_truth_social_daily_forward_shadow_replay"""
from __future__ import annotations
import bisect, datetime as dt, json, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from lab.kalshi.strategies import r7_truth_social_daily_forward_shadow_logger as L

ET = ZoneInfo("America/New_York")
R2 = ROOT / "data/kalshi_lab/strategies/r2_launch_window"
POSTS = ROOT / "data/kalshi_lab/strategies/r6_truth_social_count_endgame/posts.json"


def main() -> dict:
    M = L.load_model()
    mk = defaultdict(list)
    for line in (R2 / "markets.jsonl").open():
        m = json.loads(line)
        if m.get("series") == L.DAILY:
            mk[m["e"]].append(m)
    cand = {}
    for line in (R2 / "candles_h.jsonl").open():
        c = json.loads(line)
        if c["t"].startswith(L.DAILY):
            cand[c["t"]] = c["c"]
    raw = json.loads(POSTS.read_text())
    allp = sorted(({"ts": p["ts"], "deleted": p["deleted"],
                    "flag_ts": int(dt.datetime.fromisoformat(p["deleted_date"]).timestamp()) if p.get("deleted_date") else None}
                   for p in raw.values()), key=lambda r: r["ts"])
    TS = [r["ts"] for r in allp]
    sigs, inplay_hours, hours = [], 0, 0
    for e, ms in sorted(mk.items()):
        A = int(dt.datetime.strptime(e.split("-")[1], "%y%b%d").replace(tzinfo=ET).timestamp())
        B = A + 86400
        brs = sorted(({"t": m["t"], "lo": (0 if m["type"] == "less" else int(m["floor"])),
                       "hi": (int(m["cap"]) - 1 if m["type"] == "less" else (L.BIG if m["type"] == "greater_or_equal" else int(m["cap"]))),
                       "won": m["result"] == "yes"} for m in ms), key=lambda b: b["lo"])
        taken = set()
        for h in range(10, 24):
            t = A + h * 3600
            lo_i, hi_i = bisect.bisect_left(TS, t - L.LOOKBACK_S - 3600), bisect.bisect_right(TS, t)
            rows = [{"ts": r["ts"], "deleted": r["deleted"] and r["flag_ts"] is not None and r["flag_ts"] <= t,
                     "flag_ts": r["flag_ts"] if (r["deleted"] and r["flag_ts"] is not None and r["flag_ts"] <= t) else None}
                    for r in allp[lo_i:hi_i]]
            mod = L.daily_model(M, L.Posts(rows, t), t, A, B, brs)
            hours += 1; any_ip = False
            for b in brs:
                c = [x for x in cand.get(b["t"], []) if x[0] <= t]
                if not c or t - c[-1][0] > 3 * 3600 or c[-1][1] is None or c[-1][2] is None:
                    continue
                ask, bid = c[-1][1], c[-1][2]
                any_ip |= 0 < bid < ask < 1 and 0.05 <= (ask + bid) / 2 <= 0.95
                p = mod["p"]["d1"][b["t"]]
                for side, px, pw, won in (("YES", ask if 0 < ask < 1 else None, p, b["won"]),
                                          ("NO", round(1 - bid, 4) if 0 < bid < 1 else None, 1 - p, not b["won"])):
                    if px is None or (b["t"], side) in taken or not (L.PX_LO <= px <= L.PX_HI) or pw - px < L.THETA:
                        continue
                    taken.add((b["t"], side))
                    f = L.fee_order(px, 10) / 10
                    sigs.append({"e": e, "t": b["t"][-4:], "side": side, "hour": h, "px": px, "model": round(pw, 3), "seen": mod["seen"],
                                 "won": won, "ret": round(((1.0 if won else 0.0) - px - f) / px, 3)})
            inplay_hours += any_ip
    ev = defaultdict(list)
    for s in sigs:
        ev[s["e"]].append(s["ret"])
    out = {"note": "descriptive only; hourly bars, no books, not evidence; not used for any choice",
           "events": len(mk), "decision_hours": hours, "hours_with_an_inplay_two_sided_quote": inplay_hours,
           "signals": len(sigs), "events_with_signal": len(ev), "by_side": {s: sum(1 for x in sigs if x["side"] == s) for s in ("YES", "NO")},
           "mean_px": round(st.mean(s["px"] for s in sigs), 3) if sigs else None,
           "unit_stake_mean_ret_descriptive": round(st.mean(s["ret"] for s in sigs), 3) if sigs else None,
           "win": round(sum(s["won"] for s in sigs) / len(sigs), 3) if sigs else None, "list": sigs}
    (L.OUT / "replay_descriptive_7_launch_events.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "list"}, indent=1))
    for s in sigs:
        print(s)
    return out


if __name__ == "__main__":
    main()
