"""Live recorder for xvenue_polymarket: true-timestamp quotes of the current BTC 15-minute market on both venues.
Kalshi: /markets?series_ticker=KXBTC15M&status=open every ~20 s via the shared paced fetch (counts toward the budget).
Polymarket: CLOB /midpoint and /book of the "Up" token every ~3 s. Used to (1) pin down the timestamp semantics of the
historical series (Kalshi candle end_period_ts, Polymarket prices-history t) and (2) measure lead-lag with real clocks.
Usage: python -m lab.kalshi.strategies.xvenue_polymarket_live MINUTES [kalshi_every_s]"""
from __future__ import annotations
import json, sys, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.us.data_refresh import fetch, K
from lab.kalshi.strategies.xvenue_polymarket_fetch import get, G, C, OUT

stop = time.time() + float(sys.argv[1]) * 60
KEV = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
out = (OUT / "live_btc.jsonl").open("a"); lock = threading.Lock(); kcalls = [0]


def w(rec):
    with lock:
        out.write(json.dumps(rec) + "\n"); out.flush()


def kalshi_loop():
    while time.time() < stop:
        t0 = time.time()
        txt = fetch(f"{K}/markets?series_ticker=KXBTC15M&status=open&limit=5", pace=0); kcalls[0] += 1
        t1 = time.time()
        try:
            for m in json.loads(txt)["markets"]:
                w({"v": "K", "t0": t0, "t1": t1, "tk": m["ticker"], "ask": m.get("yes_ask_dollars"), "bid": m.get("yes_bid_dollars"),
                   "ask_sz": m.get("yes_ask_size_fp"), "bid_sz": m.get("yes_bid_size_fp"), "floor": m.get("floor_strike"), "upd": m.get("updated_time")})
        except Exception as e:
            w({"v": "Kerr", "t0": t0, "err": str(e)[:80]})
        time.sleep(max(0.0, KEV - (time.time() - t0)))


def pm_loop():
    tok = None; cur = None
    while time.time() < stop:
        o = int(time.time()) // 900 * 900
        if o != cur:
            ev = get(f"{G}/events?slug=btc-updown-15m-{o}")
            if ev and ev[0].get("markets"):
                tok = json.loads(ev[0]["markets"][0]["clobTokenIds"])[0]; cur = o
                w({"v": "Ptok", "open": o, "tok": tok})
            else:
                time.sleep(2); continue
        t0 = time.time()
        b = get(f"{C}/book?token_id={tok}") or {}
        t1 = time.time()
        bids = sorted((float(x["price"]), float(x["size"])) for x in b.get("bids", []))
        asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
        w({"v": "P", "t0": t0, "t1": t1, "open": o, "bid": bids[-1] if bids else None, "ask": asks[0] if asks else None, "ts": b.get("timestamp")})
        time.sleep(max(0.0, 3 - (time.time() - t0)))


th = [threading.Thread(target=kalshi_loop), threading.Thread(target=pm_loop)]
[t.start() for t in th]; [t.join() for t in th]
print("kalshi calls", kcalls[0])
