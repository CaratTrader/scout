"""macro_releases: free nowcast/naive model vs Kalshi price before US macro releases (taker, model-vs-price).

Sub-families (each split 70/30 by event close time; parameters chosen on discovery only):
  claims  KXJOBLESSCLAIMS  model X ~ N(previous week's first print, sigma_disc)      (sigma from discovery events)
  cpi     KXCPI            model X ~ N(Cleveland Fed nowcast dated before release day + bias, sigma)
  core    KXCPICORE        same with the core nowcast; bias/sigma from 2013-07..2022-11 nowcast errors (pre-sample)
Decision at an hourly candle end t = close - tau hours (quote at t known); fill as taker at the quote of the NEXT
hourly candle (t + 1 h; more conservative than the 1-minute rule, the model input does not change within the hour).
Trade the side with the larger edge = model prob - fill-side price - fee, if edge >= theta. Fee 0.07 p(1-p),
rounded up to the cent on a 10-contract order. Return per $ = pnl / price; t clustered by event.
Usage: python -m lab.kalshi.strategies.macro_releases"""
from __future__ import annotations
import json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.macro_releases_data import claims_events, cpi_events, nowcasts, CANDLES, OUT

N01 = NormalDist()
TAUS = (1, 3, 12, 24, 48, 96)            # hours before close (decision candle end)
THETAS = (0.03, 0.05, 0.10, 0.15, 0.20)
SPLIT = 0.7


def fee10(p: float) -> float:
    """Per-contract taker fee on a 10-contract order, rounded up to the cent per order."""
    return math.ceil(round(0.07 * p * (1 - p) * 10 * 100, 6)) / 100 / 10


def px(d) -> float | None:
    if not isinstance(d, dict):
        return None
    v = d.get("close_dollars", d.get("close"))
    return float(v) if v not in (None, "") else None


def load_candles() -> dict[str, list[tuple[int, float, float, float]]]:
    out = {}
    for f in CANDLES.glob("*.json"):
        for tk, cs in json.loads(f.read_text()).items():
            rows = []
            for c in cs:
                a, b = px(c.get("yes_ask")), px(c.get("yes_bid"))
                vol = float(c.get("volume_fp", c.get("volume")) or 0)
                if a is not None and b is not None:
                    rows.append((int(c["end_period_ts"]), a, b, vol))
            out[tk] = sorted(rows)
    return out


def quote_at(rows, t: int, max_age: int = 3 * 3600):   # hourly candles omit hours without updates: carry <= 3 h
    best = None
    for r in rows:
        if r[0] <= t:
            best = r
        else:
            break
    if best is None or t - best[0] > max_age:
        return None
    return best


def model_prob(fam: str, ev: dict, k: float, prm: dict) -> float:
    if fam == "claims":
        mu = ev["p1"] + prm.get("bias", 0.0); s = prm["sigma"]
        return 1 - N01.cdf((k - 500 - mu) / s)                     # YES iff X >= k (integers in units of 1,000)
    mu = ev["nowcast"] + prm.get("bias", 0.0); s = prm["sigma"]
    return 1 - N01.cdf((k + 0.05 - mu) / s)                         # YES iff round1(X) > k  <=>  X >= k + 0.05


def rows_for(fam: str, evs: list[dict], C: dict, prm: dict, tau: int, theta: float, same_snapshot: bool = False,
             only: set | None = None) -> list[dict]:
    out = []
    for ev in evs:
        top = (ev["close"] // 3600) * 3600                          # last full hour before close
        t_dec = top - (tau - 1) * 3600 - (0 if same_snapshot else 3600)
        t_fill = t_dec if same_snapshot else t_dec + 3600
        for m in ev["markets"]:
            if only is not None and m["t"] not in only:
                continue
            rows = C.get(m["t"])
            if not rows:
                continue
            qd, qf = quote_at(rows, t_dec), quote_at(rows, t_fill)
            if not qd or not qf or t_fill >= ev["close"]:
                continue
            p = model_prob(fam, ev, m["k"], prm)
            ask_d, bid_d = qd[1], qd[2]
            e_yes = p - ask_d - fee10(ask_d) if 0.01 <= ask_d <= 0.99 else -9
            e_no = (1 - p) - (1 - bid_d) - fee10(1 - bid_d) if 0.01 <= 1 - bid_d <= 0.99 else -9
            side = "YES" if e_yes >= e_no else "NO"; edge = max(e_yes, e_no)
            if edge < theta:
                continue
            price = qf[1] if side == "YES" else 1 - qf[2]
            if not (0.01 <= price <= 0.99):
                continue
            won = (m["result"] == "yes") == (side == "YES")
            pnl = (1.0 if won else 0.0) - price - fee10(price)
            out.append({"e": ev["e"], "t": m["t"], "close": ev["close"], "side": side, "px": price, "won": won, "ret": pnl / price,
                        "p_model": p, "edge": edge, "vol_hour": qf[3]})
    return out


def no_rows(evs: list[dict], C: dict, tau: int, lo: float, hi: float, same_snapshot: bool = False) -> list[dict]:
    """Model-free diagnostic rule found while checking market calibration on claims discovery: buy NO on every fetched
    (near-money: bracketing the previous print) strike whose mid at the decision time is in [lo, hi)."""
    out = []
    for ev in evs:
        top = (ev["close"] // 3600) * 3600
        t_dec = top - tau * 3600 + (3600 if same_snapshot else 0); t_fill = t_dec if same_snapshot else t_dec + 3600
        for m in ev["markets"]:
            r = C.get(m["t"])
            if not r:
                continue
            qd, qf = quote_at(r, t_dec), quote_at(r, t_fill)
            if not qd or not qf or t_fill >= ev["close"]:
                continue
            if not (lo <= (qd[1] + qd[2]) / 2 < hi):
                continue
            price = 1 - qf[2]
            if not (0.01 <= price <= 0.99):
                continue
            won = m["result"] == "no"
            out.append({"e": ev["e"], "t": m["t"], "close": ev["close"], "side": "NO", "px": price, "won": won,
                        "ret": ((1.0 if won else 0.0) - price - fee10(price)) / price, "vol_hour": qf[3]})
    return out


def stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    ev = defaultdict(list)
    for r in rows:
        ev[r["e"]].append(r["ret"])
    em = [st.mean(v) for v in ev.values()]; n_e = len(em)
    sd = st.pstdev(em) if n_e > 1 else 0
    t = st.mean(em) / (sd / math.sqrt(n_e)) if n_e > 2 and sd > 0 else float("nan")
    rs = sorted((r["ret"] for r in rows), reverse=True)
    rows_s = sorted(rows, key=lambda r: r["close"]); h = len(rows_s) // 2
    return {"n": len(rows), "events": n_e, "win": round(sum(r["won"] for r in rows) / len(rows), 3), "avg_px": round(st.mean(r["px"] for r in rows), 3),
            "ret_per_dollar": round(st.mean(r["ret"] for r in rows), 4), "t": round(t, 2) if t == t else None,
            "ret_wo3": round(st.mean(rs[3:]), 4) if len(rs) > 3 else None,
            "half1": round(st.mean(r["ret"] for r in rows_s[:h]), 4) if h else None, "half2": round(st.mean(r["ret"] for r in rows_s[h:]), 4) if h else None}


def split(evs: list[dict]) -> tuple[list, list]:
    evs = sorted(evs, key=lambda e: e["close"]); c = int(len(evs) * SPLIT)
    return evs[:c], evs[c:]


def families(C: dict):
    cl = [e for e in claims_events() if e["p1"] is not None and any(m["t"] in C for m in e["markets"])]
    cpi = [e for e in cpi_events("KXCPI") if any(m["t"] in C for m in e["markets"])]
    core = [e for e in cpi_events("KXCPICORE") if any(m["t"] in C for m in e["markets"])]
    return {"claims": cl, "cpi": cpi, "core": core}


def presample_nowcast_err(which: int) -> tuple[float, float, float]:
    nc = nowcasts(); ak = "act_cpi" if which == 1 else "act_core"; errs = []
    for tgt, x in nc.items():
        y, m = map(int, tgt.split("-"))
        if not ((2013, 7) <= (y, m) < (2022, 12)):
            continue
        last = [r[which] for r in x["rows"] if r[which] is not None]
        if last and x[ak] is not None:
            errs.append(x[ak] - last[-1])
    med = st.median(errs)
    return st.mean(errs), st.pstdev(errs), 1.4826 * st.median([abs(e - med) for e in errs])


def main() -> None:
    C = load_candles(); F = families(C)
    res = {"families": {}, "grid": {}}
    b1, s1, r1 = presample_nowcast_err(1); b2, s2, r2 = presample_nowcast_err(2)
    disc_cl, _ = split(F["claims"])
    d = [e["actual"] - e["p1"] for e in disc_cl if e["actual"] is not None]
    s_cl = st.pstdev(d)
    prms = {"claims": [{"sigma": s_cl, "bias": 0.0, "name": f"N(p1,{s_cl:.0f})"}],
            "cpi": [{"sigma": s1, "bias": b1, "name": f"N(nc+{b1:.3f},{s1:.3f})"}, {"sigma": r1, "bias": b1, "name": f"N(nc+{b1:.3f},{r1:.3f})"}],
            "core": [{"sigma": s2, "bias": b2, "name": f"N(nc+{b2:.3f},{s2:.3f})"}, {"sigma": r2, "bias": b2, "name": f"N(nc+{b2:.3f},{r2:.3f})"}]}
    K = 0; disc_rows = []
    for fam, evs in F.items():
        disc, val = split(evs)
        res["families"][fam] = {"events": len(evs), "disc_events": len(disc), "val_events": len(val),
                                "disc_from": disc[0]["e"] if disc else None, "val_from": val[0]["e"] if val else None}
        for prm in prms[fam]:
            for tau in TAUS:
                for th in THETAS:
                    K += 1
                    s = stats(rows_for(fam, disc, C, prm, tau, th))
                    key = f"{fam}|{prm['name']}|tau{tau}h|th{th}"
                    res["grid"][key] = s
                    if s.get("n", 0) >= 15:
                        disc_rows.append((key, s))
    res["K_variants"] = K
    disc_rows.sort(key=lambda x: -(x[1]["t"] or -9))
    res["disc_top"] = disc_rows[:15]
    # calibration: model vs market vs outcome on discovery, near-money markets, tau 24h
    # baseline: market (mid at tau 24h) calibration vs model on discovery, all fetched markets
    res["calib_disc"] = {}
    for fam, evs in F.items():
        disc, _ = split(evs); bm = []; mm = []
        for ev in disc:
            top = (ev["close"] // 3600) * 3600
            for m in ev["markets"]:
                q = quote_at(C.get(m["t"], []), top - 23 * 3600)
                if not q:
                    continue
                y = 1.0 if m["result"] == "yes" else 0.0; mid = (q[1] + q[2]) / 2
                bm.append((mid - y) ** 2); mm.append((model_prob(fam, ev, m["k"], prms[fam][0]) - y) ** 2)
        res["calib_disc"][fam] = {"n": len(bm), "brier_market_mid": round(st.mean(bm), 4) if bm else None, "brier_model": round(st.mean(mm), 4) if mm else None}
    (OUT / "grid.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["families"], indent=1)); print("K", K); print(json.dumps(res["calib_disc"]))
    for k, s in disc_rows[:15]:
        print(k, s)
    return res, F, C, prms


def validate(cands: list[str]) -> list[dict]:
    """Frozen candidates 'fam|model|tauXh|thY' evaluated once on validation."""
    C = load_candles(); F = families(C); out = []
    b1, s1, r1 = presample_nowcast_err(1); b2, s2, r2 = presample_nowcast_err(2)
    disc_cl, _ = split(F["claims"]); s_cl = st.pstdev([e["actual"] - e["p1"] for e in disc_cl if e["actual"] is not None])
    names = {f"N(p1,{s_cl:.0f})": {"sigma": s_cl, "bias": 0.0}, f"N(nc+{b1:.3f},{s1:.3f})": {"sigma": s1, "bias": b1},
             f"N(nc+{b1:.3f},{r1:.3f})": {"sigma": r1, "bias": b1}, f"N(nc+{b2:.3f},{s2:.3f})": {"sigma": s2, "bias": b2},
             f"N(nc+{b2:.3f},{r2:.3f})": {"sigma": r2, "bias": b2}}
    for c in cands:
        if c.startswith("claimsNO"):
            _, tau, band = c.split("|"); tau = int(tau[3:-1]); lo, hi = map(float, band[3:].split("-"))
            _, val = split(F["claims"])
            rows = no_rows(val, C, tau, lo, hi); s = stats(rows); s["rule"] = c
            s["same_snapshot_fill"] = stats(no_rows(val, C, tau, lo, hi, same_snapshot=True)).get("ret_per_dollar")
            disc, _ = split(F["claims"]); s["discovery"] = stats(no_rows(disc, C, tau, lo, hi))
        else:
            fam, mname, tau, th = c.split("|"); tau = int(tau[3:-1]); th = float(th[2:])
            disc, val = split(F[fam])
            rows = rows_for(fam, val, C, names[mname], tau, th)
            s = stats(rows); s["rule"] = c
            s["same_snapshot_fill"] = stats(rows_for(fam, val, C, names[mname], tau, th, same_snapshot=True)).get("ret_per_dollar")
            s["discovery"] = stats(rows_for(fam, disc, C, names[mname], tau, th))
        vols = sorted(r["vol_hour"] for r in rows); s["median_fill_hour_volume"] = vols[len(vols) // 2] if vols else None
        s["trades"] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]
        out.append(s)
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        r = validate(sys.argv[2:])
        (OUT / "validation.json").write_text(json.dumps(r, indent=1))
        for x in r:
            print({k: v for k, v in x.items() if k != "trades"})
    else:
        main()
