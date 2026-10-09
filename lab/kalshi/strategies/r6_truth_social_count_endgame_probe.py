"""r6_truth_social_count_endgame - live latency probe of the resolution source (Roll Call / Factba.se), no Kalshi calls.

Polls page 1 of the Roll Call Truth Social feed every POLL_S seconds for DURATION_S and logs, per post id, when it was
first seen versus its post time (the Truth Social status id also encodes creation time: id >> 16 = unix ms).
Output: data/kalshi_lab/strategies/r6_truth_social_count_endgame/latency_probe.jsonl
Usage: python -m lab.kalshi.strategies.r6_truth_social_count_endgame_probe [duration_s] [poll_s]"""
from __future__ import annotations
import datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_truth_social_count_endgame_data import OUT, RC_URL, http


def main(duration: int = 7200, poll: int = 60) -> None:
    f = OUT / "latency_probe.jsonl"; seen = set()
    if f.exists():
        seen = {json.loads(l)["id"] for l in f.open() if l.strip()}
    t_end = time.time() + duration; first = not seen
    while time.time() < t_end:
        now = time.time(); txt = http(RC_URL.format(p=1))
        try:
            rows = json.loads(txt).get("data") or []
        except Exception:  # noqa: BLE001
            rows = []
        with f.open("a") as fh:
            for x in rows:
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                ts = dt.datetime.fromisoformat(x["date"]).timestamp()
                id_ts = (int(x["id"]) >> 16) / 1000.0
                fh.write(json.dumps({"id": x["id"], "post_ts": ts, "id_ts": id_ts, "first_seen": now, "baseline": first,
                                     "lag_s": None if first else round(now - ts, 1), "repost": (x.get("social") or {}).get("repost_flag"),
                                     "deleted": x.get("deleted_flag")}) + "\n")
        first = False
        time.sleep(max(1.0, poll - (time.time() - now)))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 7200, int(sys.argv[2]) if len(sys.argv) > 2 else 60)
