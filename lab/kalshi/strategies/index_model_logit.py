"""index_model: does the model add information beyond the Kalshi mid? Logistic regression of the outcome on logit(mid) and
(logit(model) - logit(mid)) on discovery rows (YES side, spread <= 0.10), with an event-cluster bootstrap for the model term.
Usage: python lab/kalshi/strategies/index_model_logit.py [vol,k,dist]"""
import math, statistics as st, random, sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3]))
import lab.kalshi.strategies.index_model as M
from collections import defaultdict
lg = lambda p: math.log(p / (1 - p))
def fit(X, y, iters=30):
    k = len(X[0]); b = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k; H = [[0.0] * k for _ in range(k)]
        for x, yy in zip(X, y):
            z = sum(bi * xi for bi, xi in zip(b, x)); p = 1 / (1 + math.exp(-max(min(z, 30), -30)))
            for i in range(k):
                g[i] += (yy - p) * x[i]
                for j in range(k):
                    H[i][j] += p * (1 - p) * x[i] * x[j]
        # solve H d = g (gaussian elimination)
        A = [H[i][:] + [g[i]] for i in range(k)]
        for i in range(k):
            piv = max(range(i, k), key=lambda r: abs(A[r][i])); A[i], A[piv] = A[piv], A[i]
            for r in range(k):
                if r != i:
                    f = A[r][i] / A[i][i]
                    A[r] = [a - f * c for a, c in zip(A[r], A[i])]
        d = [A[i][k] / A[i][i] for i in range(k)]
        b = [bi + di for bi, di in zip(b, d)]
    return b
evs = M.load_events(); cut = M.split(evs); disc = [x for x in evs if x["close"] < cut]
model = ("rv12", 1.25, "t") if len(sys.argv) < 2 else tuple(sys.argv[1].split(","))
model = (model[0], float(model[1]), model[2])
for taus in ((5,), (10,), (15,), (20,), (30,), (45,), (60,), (10, 15, 20, 30, 45, 60)):
    rows = [r for tau in taus for r in M.candidates(disc, tau, *model) if r["side"] == "YES" and 0.03 < r["mid0"] < 0.97 and r["spread"] <= 0.10]
    X = [[1.0, lg(r["mid0"]), lg(r["p"]) - lg(r["mid0"])] for r in rows]; y = [1.0 if r["won"] else 0.0 for r in rows]
    b = fit(X, y)
    # cluster bootstrap by event for the model coefficient
    ev = defaultdict(list)
    for x, yy, r in zip(X, y, rows): ev[r["e"]].append((x, yy))
    keys = list(ev); random.seed(1); bs = []
    for _ in range(60):
        smp = [ev[random.choice(keys)] for _ in keys]; XX = [a for s in smp for a, _ in s]; YY = [c for s in smp for _, c in s]
        bs.append(fit(XX, YY, 12)[2])
    print(f"taus {taus}: n={len(rows)} ev={len(ev)}  a={b[0]:+.3f} b_mid={b[1]:.3f} b_model-minus-mid={b[2]:+.3f} (boot sd {st.pstdev(bs):.3f}, z={b[2]/st.pstdev(bs):+.2f})")
