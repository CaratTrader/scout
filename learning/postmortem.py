"""Forensic post-mortem of every closed trade: what the model saw, what the
market knew, what actually happened. Writes POSTMORTEM.md + refreshes the
learning database (lessons.json / postmortems.jsonl)."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout.crypto_lag import parse_window_from_question  # noqa: E402
from scout.lessons import paired_trades, refresh  # noqa: E402

DATA = ROOT / "data"
BTCACHE = ROOT / "backtest" / "cache"
OUT = Path(__file__).resolve().parent / "POSTMORTEM.md"
UA = {"User-Agent": "scout-postmortem/0.1", "Accept": "application/json"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def load_klines() -> dict:
    try:
        return json.loads((BTCACHE / "klines.json").read_text())
    except Exception:
        return {"opens": {}, "closes": {}}


def binance_path(kl: dict, start: int, end: int) -> list[tuple[int, float]]:
    path = []
    for m in range(start, end + 60, 60):
        px = kl["opens"].get(str(m))
        if px is None:
            try:
                rows = get(
                    "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
                    f"&startTime={m * 1000}&limit=1"
                )
                px = float(rows[0][1]) if rows else None
            except Exception:
                px = None
        if px is not None:
            path.append((m, float(px)))
    return path


def ts_epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def analyze(trade: dict, kl: dict) -> dict:
    win = parse_window_from_question(str(trade.get("question") or ""))
    if not win:
        return {**trade, "analysis": "window unparseable"}
    S, E = int(win["start"]), int(win["end"])
    path = binance_path(kl, S, E)
    if len(path) < 2:
        return {**trade, "analysis": "no price data"}
    open_px = path[0][1]
    close_px = path[-1][1]
    entry_t = ts_epoch(trade["ts"]) if trade.get("ts") else S  # close ts; entry ~ same window
    chg_close = (close_px - open_px) / open_px * 10000
    # price position at each minute relative to open
    marks = [(m - S, round((px - open_px) / open_px * 10000, 1)) for m, px in path]
    # was the window direction ever on the trade's side?
    side_up = trade["side"] == "YES"
    led = sum(1 for _, bps in marks[1:] if (bps > 0) == side_up)
    flipped = any(
        (marks[i][1] > 0) != (marks[i + 1][1] > 0)
        for i in range(1, len(marks) - 1)
        if marks[i][1] != 0
    )
    market_implied = trade["entry_price"] if side_up else 1 - trade["entry_price"]
    return {
        **trade,
        "window_start_utc": datetime.fromtimestamp(S, tz=timezone.utc).strftime("%H:%M"),
        "open_px": round(open_px, 2),
        "close_px": round(close_px, 2),
        "final_move_bps": round(chg_close, 1),
        "minute_marks_bps": marks,
        "minutes_on_our_side": led,
        "lead_changed": flipped,
        "market_implied_p_yes": round(market_implied if side_up else 1 - market_implied, 2),
    }


def narrative(a: dict) -> str:
    q = (a.get("question") or "").replace("Bitcoin Up or Down - ", "")
    head = f"### {q} — {a['side']} @ {a['entry_price']:.2f} → {'WON +' if a['won'] else 'LOST −'}${abs(a['pnl']):.2f}\n"
    if "final_move_bps" not in a:
        return head + f"- {a.get('analysis')}\n"
    lines = [head]
    lines.append(
        f"- BTC {a['open_px']:,} → {a['close_px']:,} ({a['final_move_bps']:+.1f} bps); "
        f"path by minute (bps vs open): {', '.join(f'{m}s:{b:+.1f}' for m, b in a['minute_marks_bps'][1:])}"
    )
    p_mkt = a['entry_price'] if a['side'] == 'YES' else 1 - a['entry_price']
    lines.append(
        f"- Market implied P({a['side']} wins) ≈ {p_mkt:.0%} at entry; "
        f"claimed model edge {a['edge_claimed']:+.1%}" if a.get("edge_claimed") is not None else
        f"- Market implied P({a['side']} wins) ≈ {p_mkt:.0%} at entry"
    )
    if not a["won"]:
        if a["mode"] == "cheap_longshot_vs_momentum":
            lines.append(
                f"- **Why it lost:** bought a {a['entry_price']:.0%} longshot against established momentum. "
                f"The move never reversed (on our side {a['minutes_on_our_side']}/{len(a['minute_marks_bps']) - 1} minutes). "
                "The book was right; the Gaussian tail model overestimates reversal odds — "
                "backtest: this price band wins 7.5% needing ~11%, −36%/$ staked."
            )
        elif a["mode"] == "coin_flip_variance":
            lines.append(
                f"- **Why it lost:** near-even entry ({'lead changed' if a['lead_changed'] else 'steady drift'} during window); "
                "single-window noise, not a structural error — the band is +EV over the 30d replay."
            )
        else:
            lines.append("- **Why it lost:** favorite failed late; check for a last-minute spike.")
    else:
        lines.append(f"- Worked as modeled: momentum persisted ({a['minutes_on_our_side']}/{len(a['minute_marks_bps']) - 1} minutes on our side).")
    return "\n".join(lines) + "\n"


def main() -> None:
    ledger = json.loads((DATA / "ledger.json").read_text())
    kl = load_klines()
    trades = paired_trades(ledger)
    analyzed = [analyze(t, kl) for t in trades]
    losses = [a for a in analyzed if not a["won"]]
    wins = [a for a in analyzed if a["won"]]
    modes: dict[str, list] = {}
    for a in losses:
        modes.setdefault(a["mode"], []).append(a)

    md = ["# Trade post-mortems — Scout BTC 5m\n",
          f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(analyzed)} closed trades · "
          f"{len(wins)}W-{len(losses)}L · net ${sum(a['pnl'] for a in analyzed):+.2f}_\n",
          "## Loss patterns\n"]
    for mode, rows in sorted(modes.items(), key=lambda kv: -len(kv[1])):
        total = sum(r["pnl"] for r in rows)
        md.append(f"- **{mode}**: {len(rows)} losses, ${total:+.2f} — " + (
            "FIXED: CRYPTO_MIN_ASK=0.25 now blocks this band, and the lessons veto refuses it from the backtest prior."
            if mode == "cheap_longshot_vs_momentum" else
            "expected variance in a +EV band; no action." if mode == "coin_flip_variance" else
            "monitor."))
    md.append("\n## Every trade\n")
    for a in analyzed:
        md.append(narrative(a))
    OUT.write_text("\n".join(md), encoding="utf-8")
    refresh(ledger)
    print(f"postmortems: {len(analyzed)} trades -> {OUT}")
    print(f"learning db refreshed: {len(modes)} loss modes")
    for mode, rows in modes.items():
        print(f"  {mode}: {len(rows)} losses ${sum(r['pnl'] for r in rows):+.2f}")


if __name__ == "__main__":
    main()
