"""How stale is our websocket book versus the venue? During the last 60 s of the next N
5m windows, sample the favourite side every 0.5 s: websocket best ask (what the bot decides on)
vs REST best ask (what the order actually meets). Prints per-sample rows and a summary.

  .venv/bin/python -m lab.book_lag_probe --windows 3
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os  # noqa: E402

for k, v in {"CRYPTO_ONLY": "1", "CRYPTO_ASSETS": "btc,eth,sol", "CRYPTO_WINDOWS_MIN": "5", "CRYPTO_EVENT_CACHE_SECONDS": "20"}.items():
    os.environ.setdefault(k, v)

from scout import streams  # noqa: E402
from scout.agent import _refresh_crypto_books, collect_universe  # noqa: E402
from scout.config import settings_from_env  # noqa: E402
from scout.crypto_lag import is_crypto_updown, parse_window  # noqa: E402


def best_ask(book) -> float | None:
    asks = getattr(book, "asks", None) or []
    prices = [float(getattr(l, "price", None) or l.get("price")) for l in asks]
    return min(prices) if prices else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=3)
    args = ap.parse_args()
    from polymarket import PublicClient

    settings = settings_from_env()
    streams.start_streams()
    client = PublicClient()
    done = 0
    rows = []
    while done < args.windows:
        uni = [m for m in collect_universe(settings) if is_crypto_updown(m)]
        _refresh_crypto_books(uni)
        now = time.time()
        live = [(parse_window(m), m) for m in uni]
        live = [(w, m) for w, m in live if w and 0 < w["end"] - now <= 65]
        if not live:
            time.sleep(2)
            continue
        end = live[0][0]["end"]
        print(f"--- window ending {time.strftime('%H:%M:%S', time.gmtime(end))} ({len(live)} markets)", flush=True)
        while time.time() < end - 3:
            t0 = time.time()
            _refresh_crypto_books([m for _, m in live])
            for w, m in live:
                fav = "YES" if float(m.get("yes_ask") or 1) >= 0.5 else "NO"
                ws_ask = float(m.get("yes_ask" if fav == "YES" else "no_ask") or 0)
                tok = m.get("yes_token" if fav == "YES" else "no_token")
                try:
                    rest_ask = best_ask(client.get_order_book(token_id=tok))
                except Exception:
                    rest_ask = None
                left = end - time.time()
                rows.append((w["asset"], round(left), ws_ask, rest_ask))
                if ws_ask and 0.9 <= ws_ask <= 0.995:
                    print(f"{time.strftime('%H:%M:%S')} {w['asset']:4} left={left:4.0f}s ws_ask={ws_ask:.3f} rest_ask={rest_ask} {'MISMATCH' if rest_ask is None or abs(rest_ask - ws_ask) > 0.005 else ''}", flush=True)
            time.sleep(max(0.0, 0.5 - (time.time() - t0)))
        done += 1
        time.sleep(10)
    band = [r for r in rows if 0.9 <= r[2] <= 0.995]
    agree = sum(1 for r in band if r[3] is not None and abs(r[3] - r[2]) <= 0.005)
    gone = sum(1 for r in band if r[3] is None)
    worse = sum(1 for r in band if r[3] is not None and r[3] - r[2] > 0.005)
    print(f"\nsamples with ws ask in 0.90-0.995: {len(band)}  REST agrees {agree}  REST has no asks {gone}  REST higher {worse}")
    client.close()


if __name__ == "__main__":
    main()
