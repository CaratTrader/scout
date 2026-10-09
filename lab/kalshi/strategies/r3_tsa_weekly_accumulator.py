"""r3_tsa_weekly_accumulator: does a YoY model of the TSA weekly mean beat the KXTSAW ladder?

KXTSAW settles on the mean of the 7 daily TSA screening numbers Mon..Sun (tsa.gov). The market opens Monday 10:00 ET
and closes Sunday 23:59 ET. tsa.gov publishes on weekday mornings only (Wayback snapshots): Monday's release carries
Fri+Sat+Sun, Tue..Fri releases carry the previous day. So at close Mon..Thu are known and Fri..Sun are not.

Steps (python -m lab.kalshi.strategies.r3_tsa_weekly_accumulator <step>):
  rows      build (event, strike, decision time) rows: model P (as-of), quote at the decision, quote one hour later
  kill      discovery kill test: Brier(model) vs Brier(mid) at release+60 min, by weekday
  grid      discovery grid of taker rules (model - ask >= edge after fee), every variant counted
  validate  run the frozen candidates (frozen.json) once on validation events

Execution: hourly candles only (the per-market historical endpoint refuses a week of 1-minute candles), so the
decision uses the last hourly close at or before t and the fill uses the hourly close at t + 60 min (stricter than
the 1-minute protocol: the market gets an extra hour to absorb the same release). Quotes are carried forward at most
CARRY hours. Fee 0.07 p (1-p) per contract, rounded up to the cent per 10-contract order."""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r3_tsa_weekly_accumulator_api import OUT, tsa_daily, tsa_vintage
from lab.kalshi.strategies.r3_tsa_weekly_accumulator_data import load_markets, week_monday, select_strikes
from lab.kalshi.strategies.r3_tsa_weekly_accumulator_model import Model

ET = ZoneInfo("America/New_York")
D = dt.timedelta
CARRY = 12          # hours a quote may be carried forward
RELEASE_HOUR = 11   # tsa.gov weekday release seen between ~07:00 and 10:15 ET on Wayback; treat it as known only from 11:00 ET
SPLIT = 0.7


def fee_order(p: float, n: int = 10) -> float:
    """Per-contract fee for an n-contract taker order, Kalshi rounds the order fee up to the cent."""
    return math.ceil(round(0.07 * p * (1 - p) * n * 100, 6)) / 100 / n


def known_through(t: dt.datetime) -> dt.date:
    """Last TSA date published at ET time t (weekday releases by RELEASE_HOUR; Monday covers Fri-Sun)."""
    d = t.date()
    if t.weekday() < 5 and t.hour < RELEASE_HOUR:      # today's release not out yet
        d -= D(1)
        while d.weekday() >= 5:
            d -= D(1)
    elif t.weekday() >= 5:
        d -= D(t.weekday() - 4)                         # weekend: Friday's release is the latest
    # the release on business day d carries the previous calendar days up to d - 1
    return d - D(1)


def quote_at(c: list, ts: int) -> tuple[float, float] | None:
    best = None
    for r in c:
        if r[0] <= ts:
            best = r
        else:
            break
    if not best or ts - best[0] > CARRY * 3600 or best[1] is None or best[2] is None:
        return None
    return float(best[1]), float(best[2])


def decision_times(mon: dt.date) -> list[tuple[str, dt.datetime]]:
    out = [("Mon", dt.datetime.combine(mon, dt.time(RELEASE_HOUR, 0), ET))]
    for i, name in enumerate(("Tue", "Wed", "Thu", "Fri"), start=1):
        out.append((name, dt.datetime.combine(mon + D(i), dt.time(RELEASE_HOUR, 0), ET)))
    out.append(("Fri18", dt.datetime.combine(mon + D(4), dt.time(18, 0), ET)))
    out.append(("Sat12", dt.datetime.combine(mon + D(5), dt.time(12, 0), ET)))
    out.append(("Sun12", dt.datetime.combine(mon + D(6), dt.time(12, 0), ET)))
    return out


VINTAGE = True       # use first-print (as-published) TSA numbers instead of today's revised table


def build_rows(L: int = 7, anchor: bool = True) -> list[dict]:
    if VINTAGE:
        mods = {era: Model({dt.date.fromisoformat(k): v for k, v in tsa_vintage(era).items()}, L, anchor) for era in ("disc", "val")}
    else:
        m0 = Model({dt.date.fromisoformat(k): v for k, v in tsa_daily().items()}, L, anchor)
        mods = {"disc": m0, "val": m0}
    E = list(load_markets().items())
    cut = int(len(E) * SPLIT)
    split = {e: ("disc" if i < cut else "val") for i, (e, _) in enumerate(E)}
    C = {}
    f = OUT / "candles.jsonl"
    for line in f.open():
        x = json.loads(line)
        C[x["t"]] = sorted(x["c"], key=lambda r: r[0])
    rows = []
    for e, v in E:
        ms = [m for m in v if m["t"] in C]
        if not ms:
            continue
        mon = week_monday(v[0])
        mod = mods[split[e]]
        sel = {m["t"] for m in select_strikes(v, 2)}
        for name, t in decision_times(mon):
            ts = int(t.timestamp())
            if ts + 3600 > v[0]["close"]:
                continue
            last = known_through(t)
            P = mod.prob_above(mon, last, [m["floor"] for m in ms], asof=t.date())
            if not P:
                continue
            mu = mod.mean_pred(mon, last)
            for m in ms:
                q0 = quote_at(C[m["t"]], ts); q1 = quote_at(C[m["t"]], ts + 3600)
                rows.append({"e": e, "t": m["t"], "split": split[e], "mon": mon.isoformat(), "dec": name, "ts": ts, "strike": m["floor"],
                             "p": P[m["floor"]], "mu": mu, "sel": m["t"] in sel, "known": (last - mon).days + 1, "q0": q0, "q1": q1, "won": m["result"] == "yes",
                             "close": v[0]["close"]})
    return rows


def brier(rows: list[dict], key) -> float:
    return st.mean((key(r) - (1.0 if r["won"] else 0.0)) ** 2 for r in rows)


def mid(q):
    return (q[0] + q[1]) / 2


def kill(rows: list[dict], which: str = "disc") -> dict:
    out = {}
    R = [r for r in rows if r["split"] == which and r["sel"] and r["q0"] and r["q0"][0] - r["q0"][1] <= 0.5 and r["q0"][0] > r["q0"][1]]
    for name in ["ALL"] + sorted({r["dec"] for r in R}, key=lambda s: ["Mon", "Tue", "Wed", "Thu", "Fri", "Fri18", "Sat12", "Sun12"].index(s)):
        x = R if name == "ALL" else [r for r in R if r["dec"] == name]
        if not x:
            continue
        bm, bq = brier(x, lambda r: r["p"]), brier(x, lambda r: mid(r["q0"]))
        # blend: does the model add information to the mid?  (logit average, equal weights)
        def lg(p): p = min(max(p, 0.01), 0.99); return math.log(p / (1 - p))
        bb = brier(x, lambda r: 1 / (1 + math.exp(-(lg(r["p"]) + lg(mid(r["q0"]))) / 2)))
        ev = defaultdict(list)
        for r in x:
            y = 1.0 if r["won"] else 0.0
            ev[r["e"]].append((mid(r["q0"]) - y) ** 2 - (r["p"] - y) ** 2)
        em = [st.mean(z) for z in ev.values()]
        se = st.pstdev(em) / math.sqrt(len(em)) if len(em) > 2 else float("nan")
        out[name] = {"n": len(x), "events": len(ev), "brier_model": round(bm, 4), "brier_mid": round(bq, 4), "brier_blend": round(bb, 4),
                     "mid_minus_model": round(bq - bm, 4), "t_clustered": round(st.mean(em) / se, 2) if se and se > 0 else None,
                     "spread": round(st.mean(r["q0"][0] - r["q0"][1] for r in x), 3)}
    return out


def trade_ret(r: dict, side: str) -> tuple[float, float] | None:
    if not r["q1"]:
        return None
    ask, bid = r["q1"]
    px = ask if side == "YES" else 1 - bid
    if not (0.02 <= px <= 0.98):
        return None
    won = r["won"] if side == "YES" else not r["won"]
    return px, ((1.0 if won else 0.0) - px - fee_order(px)) / px


def run_rule(rows: list[dict], rule: dict, which: str) -> list[dict]:
    """Taker rule: at decision time, if model - (ask + fee) >= edge (YES) or (1-model) - (1-bid + fee) >= edge (NO), using
    the quote AT the decision; fill at the quote one hour later (must still satisfy px <= max price = decision px + slip).
    Once per market (first qualifying decision)."""
    done, out = set(), []
    for r in sorted((r for r in rows if r["split"] == which), key=lambda r: r["ts"]):
        if r["t"] in done or r["dec"] not in rule["decs"] or not r["q0"] or (rule.get("sel_only", True) and not r["sel"]):
            continue
        ask0, bid0 = r["q0"]
        if ask0 - bid0 > rule.get("max_spread", 1.0):
            continue
        for side in rule["sides"]:
            px0 = ask0 if side == "YES" else 1 - bid0
            pm = r["p"] if side == "YES" else 1 - r["p"]
            if not (rule.get("pmin", 0.0) <= px0 <= rule.get("pmax", 1.0)):
                continue
            if pm - px0 - fee_order(px0) >= rule["edge"]:
                tr = trade_ret(r, side)
                if tr is None:
                    break
                px, ret = tr
                if px > px0 + rule.get("slip", 0.03):
                    break                                   # limit price: do not chase
                won = r["won"] if side == "YES" else not r["won"]
                out.append({"e": r["e"], "t": r["t"], "dec": r["dec"], "side": side, "px": px, "won": won, "ret": ret, "p": pm, "close": r["close"]})
                done.add(r["t"])
                break
    return out


def stats(tr: list[dict]) -> dict:
    if not tr:
        return {"n": 0}
    ev = defaultdict(list)
    for x in tr:
        ev[x["e"]].append(x["ret"])
    em = [st.mean(v) for v in ev.values()]
    t = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))) if len(em) > 2 and st.pstdev(em) > 0 else float("nan")
    rs = sorted((x["ret"] for x in tr), reverse=True)
    cl = sorted(x["close"] for x in tr); midc = cl[len(cl) // 2]
    h1 = [x["ret"] for x in tr if x["close"] < midc]; h2 = [x["ret"] for x in tr if x["close"] >= midc]
    return {"n": len(tr), "events": len(ev), "win": round(sum(x["won"] for x in tr) / len(tr), 3), "avg_px": round(st.mean(x["px"] for x in tr), 3),
            "ret_per_dollar": round(st.mean(x["ret"] for x in tr), 4), "t": round(t, 2), "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(h1), 4) if h1 else None, "half2": round(st.mean(h2), 4) if h2 else None}


if __name__ == "__main__":
    step = sys.argv[1]
    if "--current" in sys.argv:
        VINTAGE = False
    rows = build_rows()
    (OUT / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    if step == "kill":
        k = kill(rows, "disc")
        for name, v in k.items():
            print(name, v)
        (OUT / "kill_test.json").write_text(json.dumps(k, indent=1))


def model_variants() -> dict:
    """Kill-test robustness: can any re-calibration of the model (chosen on discovery) close the Brier gap to the mid?"""
    import lab.kalshi.strategies.r3_tsa_weekly_accumulator_model as MM
    out = {}
    orig = MM.Model.prob_above

    def make(infl: float, wide: float):
        def pa(self, mon, last, strikes, asof):
            kn, rem, n_unk = self.project(mon, last)
            if n_unk == 0:
                return orig(self, mon, last, strikes, asof)
            res = sorted(r * infl for w, r in self.residuals(n_unk) if w + D(6) < asof)
            if len(res) < self.min_hist:
                return {}
            sd = st.pstdev(res)
            o = {}
            for s in strikes:
                need = 7 * s - kn
                if need <= 0:
                    o[s] = 1.0; continue
                z = math.log(need / rem)
                pe = sum(1 - MM.ND.cdf((z - r) / self.bw) for r in res) / len(res)
                pw = 1 - MM.ND.cdf(z / (3 * sd))
                o[s] = (1 - wide) * pe + wide * pw
            return o
        return pa
    for L in (7, 14, 28):
        for infl in (1.0, 1.5, 2.0):
            for wide in (0.0, 0.2):
                MM.Model.prob_above = make(infl, wide)
                rows = build_rows(L=L)
                k = kill(rows, "disc")["ALL"]
                out[f"L{L}_infl{infl}_wide{wide}"] = k
                print(L, infl, wide, k, flush=True)
    MM.Model.prob_above = orig
    return out


if __name__ == "__main__" and sys.argv[1] == "variants":
    (OUT / "kill_variants.json").write_text(json.dumps(model_variants(), indent=1))


DECSETS = {"Mon": ["Mon"], "Tue": ["Tue"], "Wed": ["Wed"], "Thu": ["Thu"], "Fri": ["Fri"], "TueThu": ["Tue", "Wed", "Thu"],
           "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"], "late": ["Fri", "Fri18", "Sat12", "Sun12"], "all": ["Mon", "Tue", "Wed", "Thu", "Fri", "Fri18", "Sat12", "Sun12"]}


def grid(L: int) -> list[dict]:
    rows = build_rows(L=L)
    res = []
    for dname, decs in DECSETS.items():
        for sides in (["YES"], ["NO"], ["YES", "NO"]):
            for edge in (0.05, 0.10, 0.15, 0.20):
                for msp in (0.10, 1.0):
                    rule = {"L": L, "decs": decs, "sides": sides, "edge": edge, "max_spread": msp, "pmin": 0.05, "pmax": 0.95, "slip": 0.03}
                    s = stats(run_rule(rows, rule, "disc"))
                    res.append({"rule": {**rule, "decs": dname}, **s})
    return res


if __name__ == "__main__" and sys.argv[1] == "grid":
    allres = grid(7) + grid(28)
    (OUT / "discovery_grid.json").write_text(json.dumps(allres, indent=0))
    ok = [r for r in allres if r.get("n", 0) >= 15]
    print("variants", len(allres), "with n>=15:", len(ok))
    for r in sorted(ok, key=lambda r: -r["ret_per_dollar"])[:15]:
        print(r)
    print("share of variants with ret>0:", round(sum(r["ret_per_dollar"] > 0 for r in ok) / max(len(ok), 1), 3),
          " mean ret:", round(st.mean(r["ret_per_dollar"] for r in ok), 4))


def validate() -> dict:
    fz = json.loads((OUT / "frozen.json").read_text())
    rows = build_rows(L=fz["model"]["L"])
    C = {}
    for line in (OUT / "candles.jsonl").open():
        x = json.loads(line); C[x["t"]] = x["c"]
    val_events = sorted({r["e"] for r in rows if r["split"] == "val"})
    days = (max(r["close"] for r in rows if r["split"] == "val") - min(r["close"] for r in rows if r["split"] == "val")) / 86400 + 7
    out = {"kill_validation": kill(rows, "val"), "validation_events_with_candles": len(val_events), "candidates": []}
    for c in fz["candidates"]:
        tr = run_rule(rows, c, "val")
        s = stats(tr)
        # capacity proxy: contracts traded in the fill hour and the 3 hours after it
        caps = []
        for x in tr:
            ts = [r["ts"] for r in rows if r["t"] == x["t"] and r["dec"] == x["dec"]][0]
            caps.append(sum(float(k[3] or 0) for k in C[x["t"]] if ts < k[0] <= ts + 4 * 3600))
        s["median_contracts_traded_4h"] = sorted(caps)[len(caps) // 2] if caps else 0
        s["trades_per_day"] = round(len(tr) / days, 3)
        full = stats(run_rule(rows, {**c, "sel_only": False}, "val"))
        out["candidates"].append({"id": c["id"], "rule": c, "validation": s, "trades": tr, "full_ladder_recent_events": full})
        print(c["id"], s, "| full ladder:", full)
    for k, v in out["kill_validation"].items():
        print("kill val", k, v)
    (OUT / "validation.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__" and sys.argv[1] == "validate":
    validate()
