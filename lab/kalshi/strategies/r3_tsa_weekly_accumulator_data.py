"""Data for r3_tsa_weekly_accumulator: settled KXTSAW / KXTSAMAX markets (live + /historical, paginated) and
hourly event candles. Writes data/kalshi_lab/strategies/r3_tsa_weekly_accumulator/markets.jsonl and candles.jsonl.
Usage: python -m lab.kalshi.strategies.r3_tsa_weekly_accumulator_data markets|candles|book"""
from __future__ import annotations
import datetime as dt, json, shutil, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_tsa_weekly_accumulator_api import OUT, kget, used

SEED = Path("data/kalshi_lab/strategies/quake_entertain/raw")
SEEDS = {"1a884d514f7a710fa0c9": "settled_p1", "63bb02951ad90b7fd66f": "open", "fa18322c73537e7f4dcf": "hist_p1"}


def ts(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def slim(m: dict) -> dict:
    return {"t": m["ticker"], "e": m["event_ticker"], "series": m["event_ticker"].split("-")[0], "open": ts(m.get("open_time")),
            "close": ts(m.get("close_time")), "exp": ts(m.get("expected_expiration_time")), "settle": ts(m.get("settlement_ts")),
            "result": m.get("result"), "type": m.get("strike_type"), "floor": m.get("floor_strike"), "cap": m.get("cap_strike"),
            "sub": m.get("yes_sub_title"), "vol": float(m.get("volume_fp") or m.get("volume") or 0), "ev": m.get("expiration_value"),
            "created": ts(m.get("created_time")), "rules": m.get("rules_primary", "")[:300]}


def pages(base: str, first: dict | None = None, maxp: int = 12) -> list[dict]:
    out, d, n = [], first, 0
    while True:
        if d is None:
            d = kget(base)
        out += d.get("markets", [])
        cur = d.get("cursor")
        n += 1
        if not cur or n >= maxp or not d.get("markets"):
            return out
        d = kget(base + f"&cursor={cur}")


def markets() -> None:
    (OUT / "seed").mkdir(parents=True, exist_ok=True)
    for h, name in SEEDS.items():
        if (SEED / f"{h}.json").exists() and not (OUT / "seed" / f"{name}.json").exists():
            shutil.copy(SEED / f"{h}.json", OUT / "seed" / f"{name}.json")
    allm: dict[str, dict] = {}
    hist1 = json.loads((OUT / "seed" / "hist_p1.json").read_text())
    for s in ("KXTSAW",):
        for m in pages(f"/historical/markets?series_ticker={s}&limit=1000", hist1 if s == "KXTSAW" else None):
            allm[m["ticker"]] = m
        for m in pages(f"/markets?series_ticker={s}&status=settled&limit=1000"):
            allm[m["ticker"]] = m
    for s in ("KXTSAMAX", "TSAW"):
        for m in pages(f"/historical/markets?series_ticker={s}&limit=1000"):
            allm[m["ticker"]] = m
        for m in pages(f"/markets?series_ticker={s}&status=settled&limit=1000"):
            allm[m["ticker"]] = m
    rows = sorted((slim(m) for m in allm.values() if m.get("result") in ("yes", "no")), key=lambda r: (r["close"] or 0, r["t"]))
    with (OUT / "markets.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    ev = {}
    for r in rows:
        ev.setdefault((r["series"], r["e"]), []).append(r)
    print(f"markets {len(rows)} events {len(ev)}; calls used {used()}")
    for (s, e), v in sorted(ev.items(), key=lambda kv: kv[1][0]["close"]):
        print(s, e, len(v), dt.datetime.utcfromtimestamp(v[0]["open"]).isoformat(), dt.datetime.utcfromtimestamp(v[0]["close"]).isoformat(),
              "vol", int(sum(x["vol"] for x in v)), "ev", v[0]["ev"])


def load_markets() -> dict[str, list[dict]]:
    ev: dict[str, list[dict]] = {}
    for line in (OUT / "markets.jsonl").open():
        m = json.loads(line)
        if m["series"] == "KXTSAW":
            if m["floor"] is None and m["t"].split("-A")[-1].replace(".", "").isdigit():
                m["floor"] = round(float(m["t"].split("-A")[-1]) * 1e6)   # 3 early markets lack floor_strike
            ev.setdefault(m["e"], []).append(m)
    return dict(sorted(ev.items(), key=lambda kv: kv[1][0]["close"]))


def week_monday(m: dict) -> dt.date:
    """Monday of the settlement week (close is Sunday 23:59 ET = Monday 03:59/04:59 UTC)."""
    return (dt.datetime.utcfromtimestamp(m["close"]) - dt.timedelta(hours=6)).date() - dt.timedelta(days=6)


def select_strikes(ev: list[dict], n: int = 2) -> list[dict]:
    """Ex-ante strike choice: the n strikes nearest the model's weekly mean at market open (data through the prior
    Sunday, released Monday morning). Uses no information after the first decision time."""
    import datetime as _dt
    from lab.kalshi.strategies.r3_tsa_weekly_accumulator_api import tsa_daily
    from lab.kalshi.strategies.r3_tsa_weekly_accumulator_model import Model
    global _MOD
    try:
        _MOD
    except NameError:
        _MOD = Model({_dt.date.fromisoformat(k): v for k, v in tsa_daily().items()}, 7, True)
    mon = week_monday(ev[0])
    mu = _MOD.mean_pred(mon, mon - _dt.timedelta(days=1))
    return sorted(ev, key=lambda m: (abs(m["floor"] - mu), m["floor"]))[:n]   # uniform 50k grid: the 2 nearest bracket mu


def candles(which: str, n_events: int, n_strikes: int) -> None:
    """Hourly candles. which = disc (most recent n_events discovery events) | val (all validation events)."""
    E = list(load_markets().items())
    cut = int(len(E) * 0.7)
    sel = E[:cut][-n_events:] if which == "disc" else E[cut:]
    f = OUT / "candles.jsonl"
    have = {json.loads(l)["t"] for l in f.open()} if f.exists() else set()
    for e, v in sel:
        ms = select_strikes(v, n_strikes)
        recent = v[0]["close"] > 1786147200 + 86400 * 2    # events settled after 2026-08-08 are not in /historical
        if recent:
            if all(m["t"] in have for m in v):
                continue
            m0 = v[0]
            d = kget(f"/series/KXTSAW/events/{e}/candlesticks?start_ts={m0['open']}&end_ts={m0['close']}&period_interval=60")
            for tk, cs in zip(d.get("market_tickers", []), d.get("market_candlesticks", [])):
                if tk in have:
                    continue
                rows = [[c["end_period_ts"], c["yes_ask"].get("close_dollars"), c["yes_bid"].get("close_dollars"), c.get("volume_fp")] for c in cs]
                with f.open("a") as g:
                    g.write(json.dumps({"t": tk, "e": e, "src": "event", "c": rows}) + "\n")
                have.add(tk)
            continue
        for m in ms:
            if m["t"] in have:
                continue
            d = kget(f"/historical/markets/{m['t']}/candlesticks?start_ts={m['open']}&end_ts={m['close']}&period_interval=60")
            rows = [[c["end_period_ts"], c["yes_ask"].get("close"), c["yes_bid"].get("close"), c.get("volume")] for c in d.get("candlesticks", [])]
            with f.open("a") as g:
                g.write(json.dumps({"t": m["t"], "e": e, "src": "hist", "c": rows}) + "\n")
            have.add(m["t"])
        print(e, [m["t"][-5:] for m in ms], "calls", used(), flush=True)


if __name__ == "__main__":
    a = sys.argv
    if a[1] == "markets":
        markets()
    elif a[1] == "candles":
        candles(a[2], int(a[3]), int(a[4]))
