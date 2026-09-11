"""Empirical calibration of the p_up model from the 30-day window cache.

The Gaussian z-score model provably overestimates tail reversals (longshots won
7.5% where it modeled ~15%+). Replace belief with measurement: bucket every
(z, seconds_left) observation, record the actual win frequency, and let
p_up_from_move shrink toward the empirical table.

Walk-forward honesty: fit on the FIRST 20 days, evaluate on the LAST 10 the
table never saw. Writes scout/calibration.json.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

BT = Path(__file__).resolve().parent
ROOT = BT.parent
sys.path.insert(0, str(ROOT))

from scout.crypto_lag import realized_window_sigma  # noqa: E402

Z_EDGES = [-3.0, -2.0, -1.4, -1.0, -0.7, -0.45, -0.25, -0.1, 0.0,
           0.1, 0.25, 0.45, 0.7, 1.0, 1.4, 2.0, 3.0]
TIME_KEYS = [240, 180, 120, 60]
NOISE = 0.00015
OUT = ROOT / "scout" / "calibration.json"


def z_bin(z: float) -> int:
    for i, edge in enumerate(Z_EDGES):
        if z < edge:
            return i
    return len(Z_EDGES)


def load_cache():
    kl = json.loads((BT / "cache" / "klines.json").read_text())
    windows = {}
    for line in (BT / "cache" / "windows.jsonl").read_text().splitlines():
        try:
            row = json.loads(line)
            if "up_won" in row:
                windows[int(row["epoch"])] = row["up_won"]
        except Exception:
            continue
    return kl, windows


def observations(kl, windows, epochs):
    opens, closes = kl["opens"], kl["closes"]
    for S in epochs:
        up_won = windows.get(S)
        if up_won is None:
            continue
        open_px = opens.get(str(S))
        recent = [closes[str(S - 60 * i)] for i in range(31, 0, -1) if str(S - 60 * i) in closes]
        if not open_px or len(recent) < 6:
            continue
        sigma = realized_window_sigma(recent, 300)
        for off in (60, 120, 180, 240):
            spot = opens.get(str(S + off))
            if not spot:
                continue
            seconds_left = 300 - off
            chg = (spot - open_px) / open_px
            t = max(seconds_left / 300, 0.02)
            sigma_t = math.sqrt((sigma * math.sqrt(t)) ** 2 + NOISE**2)
            z = chg / (sigma_t + 1e-12)
            yield seconds_left, z, up_won


def fit(kl, windows, epochs) -> dict:
    n_bins = len(Z_EDGES) + 1
    counts = {t: [0] * n_bins for t in TIME_KEYS}
    ups = {t: [0] * n_bins for t in TIME_KEYS}
    for seconds_left, z, up_won in observations(kl, windows, epochs):
        b = z_bin(z)
        counts[seconds_left][b] += 1
        ups[seconds_left][b] += 1 if up_won else 0
    table = {
        str(t): [
            round(ups[t][i] / counts[t][i], 4) if counts[t][i] else None
            for i in range(n_bins)
        ]
        for t in TIME_KEYS
    }
    return {
        "z_edges": Z_EDGES,
        "time_keys": TIME_KEYS,
        "table": table,
        "counts": {str(t): counts[t] for t in TIME_KEYS},
        "pseudo_count": 60,
    }


def brier_eval(kl, windows, epochs, doc) -> tuple[float, float, int]:
    """Brier score on holdout: gaussian vs calibrated."""
    from scout.crypto_lag import p_up_from_move

    g_sum = c_sum = 0.0
    n = 0
    for seconds_left, z, up_won in observations(kl, windows, epochs):
        t = max(seconds_left / 300, 0.02)
        p_g = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        p_g = min(0.985, max(0.015, p_g))
        b = z_bin(z)
        emp = doc["table"][str(seconds_left)][b]
        cnt = doc["counts"][str(seconds_left)][b]
        m = doc["pseudo_count"]
        p_c = (cnt * emp + m * p_g) / (cnt + m) if emp is not None else p_g
        y = 1.0 if up_won else 0.0
        g_sum += (p_g - y) ** 2
        c_sum += (p_c - y) ** 2
        n += 1
    return g_sum / n, c_sum / n, n


def main() -> None:
    kl, windows = load_cache()
    epochs = sorted(windows)
    split = epochs[0] + 20 * 86400
    fit_epochs = [e for e in epochs if e < split]
    holdout = [e for e in epochs if e >= split]
    print(f"fit windows: {len(fit_epochs)}  holdout: {len(holdout)}")
    doc = fit(kl, windows, fit_epochs)
    g, c, n = brier_eval(kl, windows, holdout, doc)
    print(f"holdout Brier ({n} obs): gaussian={g:.4f}  calibrated={c:.4f}  "
          f"({'BETTER' if c < g else 'WORSE'} by {abs(g - c):.4f})")
    doc["holdout_brier"] = {"gaussian": round(g, 5), "calibrated": round(c, 5), "n": n}
    # refit on ALL data for production now that the method is validated out-of-sample
    doc_full = fit(kl, windows, epochs)
    doc_full["holdout_brier"] = doc["holdout_brier"]
    OUT.write_text(json.dumps(doc_full, indent=1))
    print("written:", OUT)


if __name__ == "__main__":
    main()
