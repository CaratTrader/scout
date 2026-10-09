"""Context checks for r4_xvenue_long_dated_gap on the same 204 priced pairs (no selection, all periods; counted as variants):
1. Is there a Kalshi YES premium over Polymarket on the same question? mean(Kalshi mid - Polymarket) and
   mean(Kalshi bid - Polymarket) by Polymarket price band, over all priced pair-days (14:01 UTC), clustered by event.
2. The round-3 lead rule (NO 0.80-0.92 at D in {60, 30, 14} days before the deadline), replayed on both venues on these
   pairs: Kalshi taker NO at 1 - yes_bid one hour later; Polymarket NO at 1 - price + 1c, same Kalshi fee for comparability.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_xvenue_long_dated_gap_context"""
from __future__ import annotations
import bisect, json, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_xvenue_long_dated_gap import load, panel, stats, fmt, fee_pc, last_le, ALIGN, H, DAY
from lab.kalshi.strategies.r4_xvenue_long_dated_gap_api import OUT


def main():
    ALIGN["lag"] = 60
    sel, cut, order = load()
    lines, out = [], {}
    bands = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 0.85), (0.85, 0.95), (0.95, 1.0)]
    g = defaultdict(lambda: defaultdict(list)); gb = defaultdict(lambda: defaultdict(list)); yy = defaultdict(list); kk = defaultdict(list); pp = defaultdict(list)
    for r in sel:
        y = 1.0 if r["result"] == "yes" else 0.0
        for d in panel(r):
            b = next(i for i, (lo, hi) in enumerate(bands) if lo <= d["poly"] < hi or (hi == 1.0 and d["poly"] >= lo))
            g[b][r["e"]].append((d["ask"] + d["bid"]) / 2 - d["poly"]); gb[b][r["e"]].append(d["bid"] - d["poly"])
            yy[b].append(y); kk[b].append((d["ask"] + d["bid"]) / 2); pp[b].append(d["poly"])
    lines.append("1) Kalshi vs Polymarket on the same question, by Polymarket price band (pair-days, t clustered by event)")
    for b, (lo, hi) in enumerate(bands):
        em = [st.mean(v) for v in g[b].values()]; emb = [st.mean(v) for v in gb[b].values()]
        if len(em) < 3:
            continue
        t = st.mean(em) / (st.pstdev(em) / math.sqrt(len(em))) if st.pstdev(em) > 0 else float("nan")
        row = {"days": len(yy[b]), "events": len(em), "mid_minus_poly": st.mean(em), "t": t, "bid_minus_poly": st.mean(emb),
               "yes_rate": st.mean(yy[b]), "kalshi_mid": st.mean(kk[b]), "poly": st.mean(pp[b])}
        out[f"band {lo:.2f}-{hi:.2f}"] = row
        lines.append(f"  poly {lo:.2f}-{hi:.2f}: days={row['days']:5d} ev={row['events']:3d} Kalshi mid - poly {row['mid_minus_poly']:+.4f} (t {t:+.2f}); "
                     f"bid - poly {row['bid_minus_poly']:+.4f}; YES rate {row['yes_rate']:.3f} vs Kalshi mid {row['kalshi_mid']:.3f} vs poly {row['poly']:.3f}")
    # 2) round-3 lead rule on both venues
    K, P = [], []
    for r in sel:
        for D in (60, 30, 14):
            t0 = (r["S"] - D * DAY) // DAY * DAY + 14 * H
            t = t0 + 60
            if not (r["w0"] <= t0 <= r["w1"] and (r["k_open"] or 0) <= t < (r["k_close"] or 0)):
                continue
            q = last_le(r["kts"], r["kc"], t, 24 * H); f = last_le(r["kts"], r["kc"], t0 + H, 24 * H) if (r["k_close"] or 0) > t0 + H else None
            p = last_le(r["pts"], r["ph"], t, 3 * H); pf = last_le(r["pts"], r["ph"], t0 + H + 60, 3 * H)
            won = r["result"] == "no"
            base = {"k": r["k"], "e": r["e"], "fam": r["fam"], "t_close": r["t_close"], "won": won, "vol24": 0, "D": D}
            if q and f and 0.80 <= 1 - q[2] < 0.92:
                px = 1 - f[2]
                if 0.01 <= px <= 0.99:
                    K.append(dict(base, px=px, fee=fee_pc(px), ret=((1.0 if won else 0.0) - px - fee_pc(px)) / px))
            if p and pf and 0.80 <= 1 - p[1] < 0.92:
                px = min(1 - pf[1] + 0.01, 0.99)
                P.append(dict(base, px=px, fee=fee_pc(px), ret=((1.0 if won else 0.0) - px - fee_pc(px)) / px))
    sK, sP = stats(K), stats(P)
    out["r3_rule_kalshi"], out["r3_rule_poly"] = sK, sP
    lines.append("\n2) Round-3 lead rule (NO 0.80-0.92 at D 60/30/14) on these pairs, all periods")
    lines.append(fmt("Kalshi taker NO", sK)); lines.append(fmt("Polymarket NO (price + 1c, Kalshi fee)", sP))
    txt = "\n".join(lines); print(txt)
    (OUT / "context.txt").write_text(txt); (OUT / "context.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
