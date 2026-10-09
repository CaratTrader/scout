"""Discovery grid, frozen candidates, validation and diagnostics for r4_xvenue_long_dated_gap.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_xvenue_long_dated_gap_analysis [discovery|validate|diag]"""
from __future__ import annotations
import datetime as dt, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_xvenue_long_dated_gap import (load, trades, stats, fmt, panel, XS, BANDS, H, DAY, ALIGN)
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_api import OUT

COUNT = {"n": 0}


def cell(sel, part, **kw):
    COUNT["n"] += 1
    T = [x for x in trades(sel, **kw) if part == "all" or x["part"] == part]
    return T, stats(T)


def discovery(sel, cut, lines):
    lines.append(f"pairs priced {len(sel)}; events {len({r['e'] for r in sel})}; split at event close {dt.datetime.utcfromtimestamp(cut):%Y-%m-%d}; "
                 f"discovery events {len({r['e'] for r in sel if r['part']=='disc'})}, validation events {len({r['e'] for r in sel if r['part']=='val'})}")
    out = {}
    lines.append("\n== DISCOVERY grid: NO side, first signal per market")
    for X in XS:
        for band in BANDS:
            for tier in ("all", "A"):
                T, s = cell(sel, "disc", X=X, side="NO", band=band, tier=tier)
                k = f"NO X={X:.2f} band={band} tier={tier}"
                out[k] = s; lines.append(fmt(k, s))
    lines.append("\n== DISCOVERY diagnostics (counted as variants)")
    for X in XS:
        T, s = cell(sel, "disc", X=X, side="YES"); k = f"YES mirror X={X:.2f}"; out[k] = s; lines.append(fmt(k, s))
    for X in XS:
        T, s = cell(sel, "disc", X=X, side="NO", mode="every"); k = f"NO X={X:.2f} every signal day"; out[k] = s; lines.append(fmt(k, s))
    for X in XS:
        T, s = cell(sel, "disc", X=X, side="NO", fill_delay=False); k = f"NO X={X:.2f} fill at signal quote"; out[k] = s; lines.append(fmt(k, s))
    lines.append("\n== DISCOVERY post-hoc reversal (Kalshi leads; counted): follow the Kalshi side of the gap")
    for X in XS:
        for side in ("FOLLOW_YES", "FOLLOW_NO"):
            T, s = cell(sel, "disc", X=X, side=side); k = f"{side} X={X:.2f}"; out[k] = s; lines.append(fmt(k, s))
    return out


def by_group(T, key):
    g = defaultdict(list)
    for x in T:
        g[x[key]].append(x)
    return {k: stats(v) for k, v in sorted(g.items())}


def clustered_t(vals_by_event):
    em = [st.mean(v) for v in vals_by_event.values() if v]
    if len(em) < 3 or st.pstdev(em) == 0:
        return float("nan"), (st.mean(em) if em else float("nan")), len(em)
    return st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))), st.mean(em), len(em)


def diagnostics(sel, lines, X_entry=0.04):
    clip = lambda p: min(max(p, 0.01), 0.99)
    out = {}
    # (a) gap distribution and (b) which venue is closer to the outcome, over every priced pair-day
    rows = []
    for r in sel:
        y = 1.0 if r["result"] == "yes" else 0.0
        for d in panel(r):
            mid = (d["ask"] + d["bid"]) / 2
            rows.append({"e": r["e"], "fam": r["fam"], "y": y, "mid": mid, "bid": d["bid"], "ask": d["ask"], "poly": d["poly"],
                         "spread": d["ask"] - d["bid"], "part": r["part"]})
    n = len(rows)
    gb = sorted(x["bid"] - x["poly"] for x in rows)
    out["pair_days"] = n
    out["gap_bid_quantiles"] = {q: gb[int(q * (n - 1))] for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)} if n else {}
    out["share_gap_bid_ge"] = {X: sum(g >= X for g in gb) / n for X in XS} if n else {}
    out["median_spread"] = sorted(x["spread"] for x in rows)[n // 2] if n else None
    lines.append(f"\n== DIAGNOSTICS on {n} priced pair-days ({len(sel)} pairs)")
    lines.append(f"  Kalshi bid - Polymarket price quantiles: " + ", ".join(f"q{int(q*100)}={v:+.3f}" for q, v in out["gap_bid_quantiles"].items()))
    lines.append(f"  share of pair-days with bid - poly >= X: " + ", ".join(f"{X:.2f}: {v:.1%}" for X, v in out["share_gap_bid_ge"].items())
                 + f"; median Kalshi spread {out['median_spread']:.3f}")
    for nm, sub in (("all days", rows), ("days with bid - poly >= 0.04", [x for x in rows if x["bid"] - x["poly"] >= 0.04]),
                    ("days with poly - ask >= 0.04", [x for x in rows if x["poly"] - x["ask"] >= 0.04])):
        if not sub:
            continue
        dl = defaultdict(list); db = defaultdict(list)
        for x in sub:
            lk = -(x["y"] * math.log(clip(x["mid"])) + (1 - x["y"]) * math.log(1 - clip(x["mid"])))
            lp = -(x["y"] * math.log(clip(x["poly"])) + (1 - x["y"]) * math.log(1 - clip(x["poly"])))
            dl[x["e"]].append(lk - lp); db[x["e"]].append((x["mid"] - x["y"]) ** 2 - (x["poly"] - x["y"]) ** 2)
        tl, ml, ne = clustered_t(dl); tb, mb, _ = clustered_t(db)
        ybar = st.mean(x["y"] for x in sub); kb = st.mean(x["mid"] for x in sub); pb = st.mean(x["poly"] for x in sub)
        out[f"accuracy|{nm}"] = {"n_days": len(sub), "events": ne, "yes_rate": ybar, "kalshi_mid": kb, "poly": pb,
                                 "logloss_kalshi_minus_poly": ml, "t_logloss": tl, "brier_kalshi_minus_poly": mb, "t_brier": tb}
        lines.append(f"  {nm:30s} days={len(sub):5d} ev={ne:3d} YES rate {ybar:.3f} | Kalshi mid {kb:.3f} | Poly {pb:.3f} | "
                     f"logloss K-P {ml:+.4f} (t {tl:+.2f}) | Brier K-P {mb:+.4f} (t {tb:+.2f})")
    # (c) gap closing after first-signal entries
    T = trades(sel, X=X_entry, side="NO")
    P = {r["k"]: panel(r) for r in sel}
    clo = []
    for x in T:
        pd = P[x["k"]]
        i = next(j for j, d in enumerate(pd) if d["t"] == x["t"])
        e0 = pd[i]
        row = {"k": x["k"], "gap0": e0["bid"] - e0["poly"], "won": x["won"]}
        for lag, nm in ((1, "d1"), (7, "d7"), (10 ** 6, "last")):
            j = min(i + lag, len(pd) - 1)
            if j == i:
                continue
            d = pd[j]
            row[nm] = {"gap": d["bid"] - d["poly"], "dk_mid": (d["ask"] + d["bid"]) / 2 - (e0["ask"] + e0["bid"]) / 2, "dpoly": d["poly"] - e0["poly"]}
        clo.append(row)
    out["gap_closing"] = {}
    for nm in ("d1", "d7", "last"):
        xs = [c for c in clo if nm in c]
        if not xs:
            continue
        g0 = st.mean(c["gap0"] for c in xs); g1 = st.mean(c[nm]["gap"] for c in xs)
        dk = st.mean(c[nm]["dk_mid"] for c in xs); dp = st.mean(c[nm]["dpoly"] for c in xs)
        closed = sum(c[nm]["gap"] < 0.02 for c in xs) / len(xs)
        out["gap_closing"][nm] = {"n": len(xs), "gap_entry": g0, "gap_after": g1, "kalshi_mid_move": dk, "poly_move": dp, "share_gap_below_2c": closed}
        lines.append(f"  gap closing after NO entries at X={X_entry:.2f} [{nm:4s}] n={len(xs):3d} gap {g0:+.3f} -> {g1:+.3f}; Kalshi mid moved {dk:+.3f}, "
                     f"Polymarket moved {dp:+.3f}; gap < 2c in {closed:.0%}")
    return out


def validate(sel, cands, lines):
    out = {}
    lines.append("\n== VALIDATION of the frozen candidates (evaluated once)")
    for c in cands:
        kw = {k: c[k] for k in ("X", "side", "band", "tier") if k in c}
        T, s = cell(sel, "val", **kw)
        s["rule"] = c["name"]
        out[c["name"]] = s
        lines.append(fmt(c["name"], s))
        for k2, s2 in by_group(T, "fam").items():
            lines.append(fmt("    " + k2, s2))
        tf = defaultdict(list)
        for x in T:
            tf[x["fam"]].append(x["ret"])
        s["t_by_family"], _, s["n_families"] = clustered_t(tf)
        (OUT / f"val_trades_{c['name'].replace(' ', '_').replace('=', '').replace('<', 'lt').replace('>', 'ge')}.json").write_text(json.dumps(T, indent=0, default=str))
    return out


def main(which="discovery"):
    sel, cut, order = load()
    lines = []
    if len(sys.argv) > 2:
        ALIGN["lag"] = int(sys.argv[2])
    lines.append(f"decision lag after 14:00:00 UTC = {ALIGN['lag']} s")
    which_tag = f"{which}_lag{ALIGN['lag']}"
    if which in ("discovery", "all"):
        D = discovery(sel, cut, lines)
        for X in XS:
            T = [x for x in trades(sel, X=X, side="NO") if x["part"] == "disc"]
            lines.append(f"\n  by family, NO X={X:.2f} (discovery)")
            for k, s in by_group(T, "fam").items():
                lines.append(fmt("    " + k, s))
        (OUT / f"discovery_lag{ALIGN['lag']}.json").write_text(json.dumps(D, indent=1, default=str))
    if which in ("diag_disc", "diag", "all"):
        part = "disc" if which == "diag_disc" else None
        Dg = diagnostics([r for r in sel if part is None or r["part"] == part], lines)
        (OUT / f"diagnostics{'_disc' if part else ''}.json").write_text(json.dumps(Dg, indent=1, default=str))
    if which in ("validate", "all"):
        cands = json.loads((OUT / "candidates.json").read_text())["candidates"]
        V = validate(sel, cands, lines)
        (OUT / "validation.json").write_text(json.dumps(V, indent=1, default=str))
    txt = "\n".join(lines); print(txt)
    (OUT / f"analysis_{which_tag}.txt").write_text(txt)
    print("cells evaluated", COUNT["n"])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "discovery")
