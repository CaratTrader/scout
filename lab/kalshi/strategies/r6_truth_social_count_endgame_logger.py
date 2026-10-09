"""r6_truth_social_count_endgame - forward paper logger and paper-harness plug-in (frozen 2026-10-09, before any
forward data). Rule C1 is the primary pre-registered rule; C2/C3 are logged as diagnostics only.

Every 5 min from Sat 10:00 ET (close - 24 h) to Sat 23:59 ET (end of the count window) of each KXTRUTHSOCIAL week:
  1. Roll Call / Factba.se Truth Social feed (the resolution source): pages of 50, newest first, until the oldest post
     is before the window start (Sun 00:00 ET); count = posts in the window minus posts flagged deleted.
  2. ONE Kalshi call: GET /markets?series_ticker=KXTRUTHSOCIAL&status=open (all brackets' bid/ask; through
     lab.us.data_refresh.fetch, paced, logged to calls.log).
  3. Frozen negative-binomial model (frozen_model.json, fitted on the 23 discovery weeks) -> P(final in bracket).
  4. C1 signal: buy NO on a bracket when model P(NO) - NO ask >= 0.10, NO ask in [0.03, 0.97]; one signal per
     bracket per week; paper fill at the NO ask of this pass (1 - yes_bid); fee ceil-to-cent on a 10-lot.
A settlement pass (any time after Sun 10:30 ET) reads the settled markets (1 Kalshi call) and writes settled.jsonl.

Standalone: python -m lab.kalshi.strategies.r6_truth_social_count_endgame_logger [pass|settle|status] [--force]
Plug-in:    registry {"module": "r6_truth_social_count_endgame_logger", "poll_s": 300, "params": {"q_hat": 0.75}}
Outputs:    data/kalshi_lab/strategies/r6_truth_social_count_endgame/{forward.jsonl, signals_forward.jsonl, settled.jsonl}"""
from __future__ import annotations
import datetime as dt, json, math, sys, time
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r6_truth_social_count_endgame_data import OUT, RC_URL, http, KBASE, _log
from lab.kalshi.strategies.r6_truth_social_count_endgame import Model, fee10

ET = ZoneInfo("America/New_York")
RULES = {"C1": {"theta": 0.10, "side": "NO"}, "C2": {"theta": 0.15, "side": None}, "C3": {"theta": 0.20, "side": "YES"}}
PRIMARY = "C1"
PX_LO, PX_HI = 0.03, 0.97
LAG_S = 120   # the backtest's assumed source latency; live data is used as fetched


def week_bounds(now: dt.datetime) -> tuple[int, int, int]:
    """(window start Sun 00:00 ET, window end Sun 00:00 ET next week, Kalshi close Sun 09:59 ET) of the week containing now
    (a week runs Sun..Sat ET; between Sun 00:00 and the 09:59 close, the previous window is the one still trading)."""
    lt = now.astimezone(ET)
    sun = (lt - dt.timedelta(days=(lt.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    if lt.weekday() == 6 and lt.hour < 10:
        sun -= dt.timedelta(days=7)
    end = sun + dt.timedelta(days=7)
    return int(sun.timestamp()), int(end.timestamp()), int((end + dt.timedelta(hours=9, minutes=59)).timestamp())


def rollcall_window(A: int, max_pages: int = 12) -> list[dict]:
    rows = {}
    for p in range(1, max_pages + 1):
        txt = http(RC_URL.format(p=p))
        data = (json.loads(txt).get("data") if txt else None) or []
        if not data:
            break
        for x in data:
            ts = int(dt.datetime.fromisoformat(x["date"]).timestamp())
            s = x.get("social") or {}
            dd = s.get("deleted_date")
            rows[x["id"]] = {"ts": ts, "deleted": bool(x.get("deleted_flag")),
                             "flag_ts": int(dt.datetime.fromisoformat(dd).timestamp()) if dd else None}
        if min(int(dt.datetime.fromisoformat(x["date"]).timestamp()) for x in data) < A:
            break
        time.sleep(1.0)
    return [r for r in rows.values() if r["ts"] >= A]


class LivePosts:
    """Same interface as r6_truth_social_count_endgame.Posts for the model (seen / all / dele)."""
    def __init__(self, rows: list[dict], now: int):
        self.all = sorted(r["ts"] for r in rows)
        self.dele = sorted((r["ts"], r["flag_ts"] or now) for r in rows if r["deleted"])
        self.keep = sorted(r["ts"] for r in rows if not r["deleted"])
        self.lag = 0

    def seen(self, A: int, t: int) -> int:
        import bisect
        n = bisect.bisect_right(self.all, t) - bisect.bisect_left(self.all, A)
        return n - sum(1 for ts, fl in self.dele if A <= ts <= t and fl <= t)


def bracket(m: dict) -> dict:
    stype = m["strike_type"]; lo = m.get("floor_strike"); hi = m.get("cap_strike")
    if stype == "between":
        L, H = int(lo), int(hi)
    elif stype == "less":
        L, H = 0, int(hi) - 1
    else:
        L, H = int(lo) + 1, 10 ** 6
    return {"t": m["ticker"], "lo": L, "hi": H, "won": None}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate(markets: list[dict], rows: list[dict], now: dt.datetime, model: Model) -> dict:
    A, B, close = week_bounds(now)
    t = int(now.timestamp())
    ms = [m for m in markets if m.get("close_time") and abs(int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp()) - close) < 6 * 3600]
    x = {"A": A, "B": B, "close": close, "brackets": sorted((bracket(m) for m in ms), key=lambda b: b["lo"])}
    LP = LivePosts(rows, t)
    c, seen, mu = model.dist(x, LP, t)
    pr = model.bracket_probs(x, LP, t) if x["brackets"] else {}
    out = {"ts": t, "iso": now.astimezone(ET).isoformat(timespec="seconds"), "event": ms[0]["event_ticker"] if ms else None,
           "seen": seen, "kept_est": round(c, 2), "mu_remaining": round(mu, 2), "h_to_window_end": round((B - t) / 3600, 2), "brackets": [], "signals": []}
    for m in ms:
        tk = m["ticker"]; ask = _f(m.get("yes_ask_dollars")); bid = _f(m.get("yes_bid_dollars"))
        p = pr.get(tk)
        out["brackets"].append({"t": tk, "sub": m.get("yes_sub_title"), "bid": bid, "ask": ask, "ask_size": _f(m.get("yes_ask_size_fp")),
                                "bid_size": _f(m.get("yes_bid_size_fp")), "model": None if p is None else round(p, 4)})
        if p is None or ask is None or bid is None or t >= B or t < close - 24 * 3600:
            continue
        for name, r in RULES.items():
            for side, px, pw in (("YES", ask, p), ("NO", 1 - bid, 1 - p)):
                if r["side"] and side != r["side"]:
                    continue
                if PX_LO <= px <= PX_HI and pw - px >= r["theta"]:
                    out["signals"].append({"rule": name, "t": tk, "side": side, "px": round(px, 4), "model": round(pw, 4),
                                           "fee": fee10(px), "size_at_px": _f(m.get("yes_bid_size_fp" if side == "NO" else "yes_ask_size_fp"))})
    return out


def load_model() -> Model:
    return Model.from_frozen(json.loads((OUT / "frozen_model.json").read_text()))


def kalshi_open() -> list[dict]:
    from lab.us.data_refresh import fetch
    url = f"{KBASE}/markets?series_ticker=KXTRUTHSOCIAL&status=open&limit=100"
    txt = fetch(url, pace=1.15); _log(url, len(txt))
    return (json.loads(txt).get("markets") if txt else None) or []


def one_pass(force: bool = False) -> dict | None:
    now = dt.datetime.now(dt.timezone.utc)
    A, B, close = week_bounds(now)
    if not force and not (close - 24 * 3600 <= now.timestamp() < B):
        return None
    rows = rollcall_window(A)
    markets = kalshi_open()
    rec = evaluate(markets, rows, now, load_model())
    with (OUT / "forward.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    # first signal per rule, bracket and side is the paper position
    sf = OUT / "signals_forward.jsonl"
    have = set()
    if sf.exists():
        have = {(s["rule"], s["t"], s["side"]) for s in map(json.loads, sf.open())}
    with sf.open("a") as fh:
        for s in rec["signals"]:
            k = (s["rule"], s["t"], s["side"])
            if k not in have and not force:
                have.add(k); fh.write(json.dumps({**s, "ts": rec["ts"], "iso": rec["iso"], "event": rec["event"], "seen": rec["seen"]}) + "\n")
    return rec


def settle() -> list[dict]:
    from lab.us.data_refresh import fetch
    sf = OUT / "signals_forward.jsonl"; df = OUT / "settled.jsonl"
    if not sf.exists():
        return []
    done = {(s["rule"], s["t"], s["side"]) for s in map(json.loads, df.open())} if df.exists() else set()
    todo = [s for s in map(json.loads, sf.open()) if (s["rule"], s["t"], s["side"]) not in done]
    out = []
    for ev in sorted({s["event"] for s in todo if s.get("event")}):
        url = f"{KBASE}/markets?event_ticker={ev}&limit=100"
        txt = fetch(url, pace=1.15); _log(url, len(txt))
        res = {m["ticker"]: m.get("result") for m in ((json.loads(txt).get("markets") if txt else None) or [])}
        for s in todo:
            r = res.get(s["t"])
            if s["event"] != ev or r not in ("yes", "no"):
                continue
            won = (r == "yes") == (s["side"] == "YES")
            pnl = (1.0 if won else 0.0) - s["px"] - s["fee"]
            out.append({**s, "result": r, "won": won, "ret_per_dollar": round(pnl / s["px"], 4)})
    with df.open("a") as fh:
        for s in out:
            fh.write(json.dumps(s) + "\n")
    return out


# ---------------------------------------------------------------- paper-harness plug-in (scout/kalshi_lab_paper.py)
try:
    from scout.kalshi_lab_strategies.base import Strategy, fetcher

    @fetcher("rollcall_truth", ttl=240)
    def _rollcall_truth(A: int) -> list[dict]:
        return rollcall_window(int(A))

    class TruthSocialEndgame(Strategy):
        series = ("KXTRUTHSOCIAL",)
        poll_s = 300

        def _active(self, now: dt.datetime) -> bool:
            A, B, close = week_bounds(now)
            return close - 24 * 3600 <= now.timestamp() < B

        def series_now(self, now):
            return list(self.series) if self._active(now) else []

        def external_now(self, now):
            return {"rollcall_truth": {"A": week_bounds(now)[0]}} if self._active(now) else {}

        def decide(self, now, market_quotes, external_data):
            if not self._active(now):
                return []
            q = market_quotes.get("KXTRUTHSOCIAL"); rows = external_data.get("rollcall_truth")
            if not q or rows is None:
                return []
            rec = evaluate(list(q.values()), rows, now, load_model())
            self.notes.append(f"seen {rec['seen']} mu {rec['mu_remaining']}")
            return [{"ticker": s["t"], "side": s["side"], "max_price": s["px"], "why": f"C1 model {s['model']:.2f} vs {s['px']:.2f}, seen {rec['seen']}"}
                    for s in rec["signals"] if s["rule"] == PRIMARY]

    STRATEGY = TruthSocialEndgame
except Exception:  # noqa: BLE001  (standalone use without the scout package)
    pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pass"
    force = "--force" in sys.argv
    if cmd == "pass":
        r = one_pass(force)
        print("outside the Saturday window" if r is None else json.dumps({k: r[k] for k in ("iso", "event", "seen", "mu_remaining", "signals")}))
    elif cmd == "settle":
        print(json.dumps(settle(), indent=1))
    elif cmd == "status":
        print(json.dumps({"week": week_bounds(dt.datetime.now(dt.timezone.utc))}))
