"""Probe gamma + CLOB history endpoints for backtest feasibility."""
import json
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "scout-backtest/0.1", "Accept": "application/json"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


now = int(time.time())
base = now - (now % 300)
slugs = [f"btc-updown-5m-{base - 300 * k}" for k in range(13, 16)]  # ~1h ago
qs = "&".join(f"slug={s}" for s in slugs)
rows = get(f"https://gamma-api.polymarket.com/markets?{qs}")
print("gamma batch returned:", len(rows))
m = rows[0]
print("keys sample:", sorted(m.keys())[:18])
print("slug:", m.get("slug"), "closed:", m.get("closed"))
print("outcomes:", m.get("outcomes"), "outcomePrices:", m.get("outcomePrices"))
tokens = json.loads(m["clobTokenIds"])
print("yes token:", tokens[0][:20], "...")

slug_epoch = int(m["slug"].rsplit("-", 1)[1])
url = (
    "https://clob.polymarket.com/prices-history?market=" + tokens[0]
    + f"&startTs={slug_epoch - 60}&endTs={slug_epoch + 360}&fidelity=1"
)
hist = get(url)
pts = hist.get("history", [])
print("prices-history points:", len(pts))
for p in pts[:8]:
    print("  t=+%ds price=%s" % (p["t"] - slug_epoch, p["p"]))

# old window from ~30 days ago — does history go back that far?
old_epoch = base - 30 * 86400
old_epoch -= old_epoch % 300
old = get(f"https://gamma-api.polymarket.com/markets?slug=btc-updown-5m-{old_epoch}")
print("30d-old market found:", len(old))
if old:
    otok = json.loads(old[0]["clobTokenIds"])[0]
    oh = get(
        "https://clob.polymarket.com/prices-history?market=" + otok
        + f"&startTs={old_epoch - 60}&endTs={old_epoch + 360}&fidelity=1"
    )
    print("30d-old history points:", len(oh.get("history", [])), "outcome:", old[0].get("outcomePrices"))
