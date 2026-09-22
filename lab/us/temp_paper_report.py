"""One-screen report of the Polymarket US temperature paper trader: python -m lab.us.temp_paper_report"""
from __future__ import annotations
import collections, datetime as dt, json, statistics as st
from pathlib import Path

LED = Path("data/ledger_us_temp.json"); JOUR = Path("data/us_temp_journal.jsonl")


def main() -> None:
    led = json.loads(LED.read_text()) if LED.exists() else {}
    ev = [json.loads(l) for l in JOUR.open()] if JOUR.exists() else []
    fills = led.get("fills", []); pos = led.get("positions", [])
    print(f"cash ${led.get('cash', 0):.2f} (start ${led.get('start_cash', 0):.0f})  open positions {len(pos)}  settled fills {len(fills)}  since {str(led.get('created', ''))[:16]}")
    if fills:
        p = [f["pnl"] for f in fills]; stake = sum(f["stake"] for f in fills)
        print(f"settled: win {sum(1 for f in fills if f['won'])/len(fills):.0%}, P&L {sum(p):+.2f} on ${stake:.0f} staked ({sum(p)/stake:+.1%} per $), avg price {st.mean(f['px'] for f in fills):.2f}")
        by = collections.defaultdict(list)
        for f in fills: by[f["rule"]].append(f["pnl"])
        for r, v in by.items(): print(f"  {r}: n={len(v)} win={sum(1 for x in v if x>0)/len(v):.0%} pnl={sum(v):+.2f}")
    for pth in pos:
        print(f"  OPEN {pth['city']} {pth['slug'].split('-')[-1]} {pth['side']} {pth['shares']:.0f} sh @ {pth['px']:.2f} ({pth['rule']}) {pth['why']}")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    sig = [e for e in ev if e.get("event") == "signal" and dt.datetime.fromtimestamp(e["ts"], dt.timezone.utc).date().isoformat() == today]
    fl = [e for e in ev if e.get("event") == "fill" and dt.datetime.fromtimestamp(e["ts"], dt.timezone.utc).date().isoformat() == today]
    skips = collections.Counter(e.get("reason") for e in ev if e.get("event") == "skip")
    errs = [e for e in ev if e.get("event") in ("error", "cycle_error")]
    print(f"today (UTC): {len(sig)} signals, {len(fl)} fills; skips {dict(skips)}; errors {len(errs)}")
    for e in fl[-10:]:
        print(f"  fill {dt.datetime.fromtimestamp(e['ts'], dt.timezone.utc):%H:%M}Z {e['city']} {e['slug'].split('-')[-1]} {e['side']} {e['shares']:.0f} sh @ {e['px']:.2f} {e['rule']}")


if __name__ == "__main__":
    main()
