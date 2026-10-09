"""On-disk launch data for r6_new_series_honeymoon (0 Kalshi calls).

Every source below is a settled-market list plus candles that an earlier round already downloaded. Nothing is chosen by
outcome: a series is "new" when its first market opened inside the data (launch date = first open of the series, checked
against the earlier launch probes), and a market is "honeymoon" when it opened < 28 days after its series' launch.

Sources (candle row format [end_ts, yes_ask, yes_bid, ...]):
  WX_HIGH  data/lab/us/kalshi/markets.json + one JSON per ticker (1-minute, last 14 h before close, sparse).
           New: KXHIGHT SAN (08-18), EWR/TTN (08-25), SDF (08-27). Established: the 7 KXHIGH cities and the KXHIGHT cities
           with data from 07-18. HOU/NOLA/OKC/SATX first appear 07-29 (relaunch check in the census; kept as their own group).
  WX_LOW   data/kalshi_lab/strategies/weather_lows (hourly, 26-38 h before close). New: KXLOWT SAN/EWR/TTN/SDF.
  r2 new products  data/kalshi_lab/strategies/r2_launch_window (hourly, close-15 h .. close-5 h; SOFR/TRUMPAPPROVE/VHGCB longer).
  RAIN     KXRAIN (launched 07-14 19:09 UTC): rain_history (1-min, last 18 h), microstructure arch_candles (1-min),
           lab candles data/kalshi_lab/candles/KXRAIN.jsonl (1-min). In-sample: KXRAIN is the motivating case of round 1.
  QUAKE    quake_entertain KXBIGGESTQUAKE (1-min). Launch 09-02 (first market in the data; r2 probe).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
S = os.path.join(ROOT, "data/kalshi_lab/strategies")
WXD = os.path.join(ROOT, "data/lab/us/kalshi")

NEW_HIGH = {"KXHIGHTSAN", "KXHIGHTEWR", "KXHIGHTTTN", "KXHIGHTSDF"}
RELAUNCH_HIGH = {"KXHIGHTHOU", "KXHIGHTNOLA", "KXHIGHTOKC", "KXHIGHTSATX"}
NEW_LOW = {"KXLOWTSAN", "KXLOWTEWR", "KXLOWTTTN", "KXLOWTSDF"}
FAMILY = {"KXAAAGASDCA": "GAS_STATE", "KXAAAGASDFL": "GAS_STATE", "KXAAAGASDNC": "GAS_STATE", "KXAAAGASDOH": "GAS_STATE",
          "KXAAAGASDSC": "GAS_STATE", "KXSOFRD": "SOFR", "KXTRUMPAPPROVE": "TRUMPAPPROVE", "KXTRUTHSOCIALD": "TRUTHD",
          "KXTXERCOTPEAKD": "ERCOT", "KXVHGCB": "VHGCB", "KXRAIN": "RAIN", "KXBIGGESTQUAKE": "QUAKE"}
# Launch dates of the r2 new products (first open, r2 launch_probe / settled lists; KXVHGCB, KXTRUTHSOCIALD: first open in data)
RAIN_LAUNCH = int(dt.datetime(2026, 7, 14, 19, 9, tzinfo=dt.timezone.utc).timestamp())


def ts(s: str | None) -> int | None:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def _f(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _jsonl(p):
    if not os.path.exists(p):
        return []
    with open(p) as fh:
        return [json.loads(l) for l in fh if l.strip()]


def _rows(c: list) -> list:
    """[ts, ask, bid] sorted, dropping rows without both quotes."""
    out = []
    for r in c:
        a, b = _f(r[1]), _f(r[2])
        if a is None or b is None:
            continue
        out.append((int(r[0]), a, b))
    out.sort()
    return out


def load_weather_high() -> list[dict]:
    mk = json.load(open(os.path.join(WXD, "markets.json")))
    out = []
    for t, m in mk.items():
        f = os.path.join(WXD, f"{t}.json")
        if not os.path.exists(f) or m.get("result") not in ("yes", "no"):
            continue
        cs = json.load(open(f))
        c = []
        for x in cs:
            a = _f((x.get("yes_ask") or {}).get("close_dollars")); b = _f((x.get("yes_bid") or {}).get("close_dollars"))
            if a is not None and b is not None:
                c.append((int(x["end_period_ts"]), a, b))
        c.sort()
        s = m["series"]
        grp = "NEW" if s in NEW_HIGH else ("RELAUNCH" if s in RELAUNCH_HIGH else "EST")
        out.append({"t": t, "e": m["event_ticker"], "series": s, "fam": "WX_HIGH", "grp": grp, "open": ts(m["open_time"]),
                    "close": ts(m["close_time"]), "result": m["result"], "c": c, "res": "min",
                    "vol": _f(m.get("volume_fp")) or 0.0})
    return out


def load_weather_low() -> list[dict]:
    d = os.path.join(S, "weather_lows")
    ms = {m["t"]: m for m in _jsonl(os.path.join(d, "markets.jsonl"))}
    out = []
    for x in _jsonl(os.path.join(d, "candles_h.jsonl")):
        m = ms.get(x["t"])
        if not m or m.get("result") not in ("yes", "no"):
            continue
        s = m["series"]
        out.append({"t": m["t"], "e": m["e"], "series": s, "fam": "WX_LOW", "grp": "NEW" if s in NEW_LOW else "EST",
                    "open": m["open"], "close": m["close"], "result": m["result"], "c": _rows(x["c"]), "res": "hour",
                    "vol": m.get("vol") or 0.0})
    return out


def load_r2_products() -> list[dict]:
    d = os.path.join(S, "r2_launch_window")
    ms = {m["t"]: m for m in _jsonl(os.path.join(d, "markets.jsonl"))}
    out = []
    for x in _jsonl(os.path.join(d, "candles_h.jsonl")):
        m = ms.get(x["t"])
        if not m or m.get("result") not in ("yes", "no") or m["series"] not in FAMILY:
            continue
        out.append({"t": m["t"], "e": m["e"], "series": m["series"], "fam": FAMILY[m["series"]], "grp": "NEW",
                    "open": m["open"], "close": m["close"], "result": m["result"], "c": _rows(x["c"]), "res": "hour",
                    "vol": m.get("vol") or 0.0})
    return out


def load_rain() -> list[dict]:
    ms = {}
    for p in (os.path.join(S, "rain_history/markets.jsonl"), os.path.join(ROOT, "data/kalshi_lab/markets/KXRAIN.jsonl")):
        for m in _jsonl(p):
            if m["t"].startswith("KXRAIN-"):
                ms.setdefault(m["t"], m)
    cand = {}
    for p in (os.path.join(S, "rain_history/candles.jsonl"), os.path.join(S, "microstructure/arch_candles.jsonl"),
              os.path.join(ROOT, "data/kalshi_lab/candles/KXRAIN.jsonl")):
        for x in _jsonl(p):
            if x["t"].startswith("KXRAIN-") and x.get("c"):
                r = _rows(x["c"])
                if len(r) > len(cand.get(x["t"], [])):
                    cand[x["t"]] = r
    out = []
    for t, c in cand.items():
        m = ms.get(t)
        if not m or m.get("result") not in ("yes", "no"):
            continue
        out.append({"t": t, "e": m["e"], "series": "KXRAIN", "fam": "RAIN", "grp": "NEW", "open": m["open"],
                    "close": m["close"], "result": m["result"], "c": c, "res": "min", "vol": m.get("vol") or 0.0})
    return out


def load_quake() -> list[dict]:
    d = os.path.join(S, "quake_entertain")
    ms = {m["t"]: m for m in _jsonl(os.path.join(d, "quake_markets.jsonl"))}
    out = []
    for x in _jsonl(os.path.join(d, "quake_candles.jsonl")):
        m = ms.get(x["t"])
        if not m or m.get("result") not in ("yes", "no") or not x.get("c"):
            continue
        out.append({"t": m["t"], "e": m["e"], "series": m["series"], "fam": "QUAKE", "grp": "NEW", "open": m["open"],
                    "close": m["close"], "result": m["result"], "c": _rows(x["c"]), "res": "min", "vol": m.get("vol") or 0.0})
    return out


def load_all() -> list[dict]:
    rows = load_weather_high() + load_weather_low() + load_r2_products() + load_rain() + load_quake()
    first = defaultdict(lambda: 1 << 62)
    for r in rows:
        first[r["series"]] = min(first[r["series"]], r["open"])
    first["KXRAIN"] = min(first["KXRAIN"], RAIN_LAUNCH)
    for r in rows:
        r["launch"] = first[r["series"]] if r["grp"] in ("NEW", "RELAUNCH") else None
        r["age_d"] = (r["open"] - r["launch"]) / 86400 if r["launch"] else None
    return rows


if __name__ == "__main__":
    rows = load_all()
    agg = defaultdict(lambda: [0, 0, None])
    for r in rows:
        k = (r["fam"], r["grp"], r["series"]); a = agg[k]; a[0] += 1; a[1] += bool(r["c"])
        a[2] = r["launch"]
    for k, v in sorted(agg.items()):
        print(k, v[0], v[1], dt.datetime.utcfromtimestamp(v[2]).date() if v[2] else "")
