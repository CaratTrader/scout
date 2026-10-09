"""Adversarial reproduction of r5_seeded_book_placeholder_split (own code, written from the claim text only).

Nothing from lab/kalshi/strategies/r5_seeded_book_placeholder_split*.py is imported or copied, and none of its
outputs (archive_rows.jsonl, archive_split.json, prints_check*.json) is read as an input. Inputs are the RAW Kalshi
API responses cached by the four source researchers (api_cache/*.json: candlesticks and trade prints) plus their
market lists / plan / order files (data, not code). No Kalshi call is made.

Claim under test: rest SELL-YES at s = yes_ask - 0.01 (= buy NO at q = 1 - s, N = floor(5 / q)) at the first hour
>= open + 1 h of each mention market; split the orders by the post quote into P (placeholder: yes_bid <= 0.04 and
yes_ask >= 0.79) and NP; through-only fills (a print strictly above s after the post and before the cancel),
filled contracts f = min(N, through volume) (candle-volume proxy unless exact prints exist); maker fee 0;
size-weighted return per $ = sum f*(win - q) / sum f*q with an event-clustered ratio t. Validation = last 30% of
each sample's events. Claimed pooled validation P: n 55, 16 events, +97.5%/$, t 1.77; NP n 1112, 97 events, +11.7%.

Samples rebuilt here:
  r3   KXEARNINGSMENTION* live tier, 46 events: C1E with the pre-call cancel (15:00 ET afternoon / 07:00 ET morning,
       never after call start - 1 h or the first close); the call hour is located from the first hourly price jump
       (>= 2 markets go from mid <= 0.60 to a print or mid >= 0.90 within 30 h before the first close). Fills from
       hourly candles (and daily candles in the gaps) wholly inside (post, cancel].
  r2   21 round-2 mention series: first eligible hour (open + 1 h, before any close of the event), s = ask - 1c if
       s > bid; cancel = the candle containing the event's first close (window B); entries whose candle fetch window
       began after the market's open are dropped (lead_round2 'rule consistent').
       r2 / r3 primary: a candle's volume proxy is replaced by the exact count when a SOURCE-side cached print
       response (source api_cache, round-2 repro trades/) covers that candle. Variants: pure candle proxy, and exact
       full-window prints incl. the r5 researcher's cached raw /markets/trades responses (raw API data only).
       Diagnostic only (after the run): the claimed archive_rows.jsonl is diffed row by row against repro_rows.jsonl.
  r4e  r4 earnings archive plan (one market per event): C1E at H1, quote <= 3 h old, cancel 07:00 ET on the call
       day (busiest-hour guard), exact prints where fetched else candle bounds (unresolved dropped).
  r4s  r4 single-appearance orders.jsonl (the source's own per-order candle evidence), exact prints recomputed
       from the source's trades.jsonl where fetched.
Usage: .venv/bin/python -m lab.kalshi.strategies.r5_seeded_book_placeholder_split_repro [run]"""
from __future__ import annotations
import datetime as dt, hashlib, json, math, os, re, statistics as st, sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.calib import cell_stats

S = Path("data/kalshi_lab/strategies")
OUT = S / "r5_seeded_book_placeholder_split" / "repro"
ET = ZoneInfo("America/New_York")
EPS = 1e-9
SPLIT = 0.7
MON = dict(JAN=1, FEB=2, MAR=3, APR=4, MAY=5, JUN=6, JUL=7, AUG=8, SEP=9, OCT=10, NOV=11, DEC=12)


def iso(s):
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def fl(x):
    return None if x in (None, "") else float(x)


def is_P(bid, ask):
    return bid <= 0.04 + EPS and ask >= 0.79 - EPS


def is_S2(bid, ask):
    return bid <= 0.05 + EPS and ask >= 0.75 - EPS


# ------------------------------------------------------------------------------------------------ raw cache readers
def raw_candles(cache: Path) -> dict:
    """{period: {ticker: {end_ts: (ask_c, bid_c, px_hi, px_lo, vol)}}} from every cached candlestick response."""
    out = defaultdict(lambda: defaultdict(dict))
    for fn in sorted(os.listdir(cache)):
        x = json.load(open(cache / fn))
        if not (isinstance(x, dict) and isinstance(x.get("markets"), list) and x["markets"] and "candlesticks" in x["markets"][0]):
            continue
        ends = sorted({c["end_period_ts"] for m in x["markets"] for c in m["candlesticks"]})
        g = 0
        for a, b in zip(ends, ends[1:]):
            g = math.gcd(g, b - a)
        per = 86400 if g and g % 86400 == 0 else 3600
        for m in x["markets"]:
            for c in m["candlesticks"]:
                pr = c.get("price") or {}
                out[per][m["market_ticker"]][c["end_period_ts"]] = (
                    fl((c.get("yes_ask") or {}).get("close_dollars")), fl((c.get("yes_bid") or {}).get("close_dollars")),
                    fl(pr.get("high_dollars")), fl(pr.get("low_dollars")), fl(c.get("volume_fp")) or 0.0)
    return {p: {t: sorted(v.items()) for t, v in d.items()} for p, d in out.items()}


def raw_prints(cache: Path, log: Path, path_col: int) -> dict:
    """{ticker: [(lo, hi, complete, [(ts, yes_price, count, block)])]} from cached /markets/trades responses whose
    request path (calls.log) gives the window; complete = no cursor left."""
    path_of = {}
    for line in log.open():
        p = line.rstrip("\n").split("\t")
        if len(p) > path_col:
            path_of[hashlib.sha1(p[path_col].encode()).hexdigest()[:24]] = p[path_col]
    out = defaultdict(list)
    for fn in sorted(os.listdir(cache)):
        p = path_of.get(fn[:-5], "")
        if not p.startswith("/markets/trades?"):
            continue
        x = json.load(open(cache / fn))
        q = dict(kv.split("=", 1) for kv in p.split("?", 1)[1].split("&"))
        lo = int(q["min_ts"]) if "min_ts" in q else 0
        hi = int(q["max_ts"]) if "max_ts" in q else 1 << 40
        pr = [(iso(t["created_time"]), float(t.get("yes_price_dollars") or 0), float(t.get("count_fp") or t.get("count") or 0),
               bool(t.get("is_block_trade"))) for t in x.get("trades") or []]
        if "cursor=" in p:      # a continuation page: merge into the first page of the same window
            continue
        out[q["ticker"]].append((lo, hi, not x.get("cursor"), pr))
    return out


def exact_fill(prints_for: list, a: int, b: int, s: float, N: float):
    """Through / any contracts in (a, b] from a cached, complete response covering the whole window, else None."""
    for lo, hi, complete, pr in prints_for or []:
        if complete and lo <= a + 1 and hi >= b - 1:
            w = [p for p in pr if a < p[0] <= b and not p[3]]
            thr = sum(p[2] for p in w if p[1] > s + EPS); anyp = sum(p[2] for p in w if p[1] >= s - EPS)
            return min(N, thr), min(N, anyp)
    return None


def candle_exact(prints_for: list, a: int, b: int, s: float):
    """Exact (through, any) contracts in (a, b] (one candle's span clipped to the order window) if a cached complete
    response covers it, else None. Used to replace that candle's volume proxy (the source's 'exact_part')."""
    for lo, hi, complete, pr in prints_for or []:
        if complete and lo <= a + 1 and hi >= b - 1:
            w = [p for p in pr if a < p[0] <= b and not p[3]]
            return sum(p[2] for p in w if p[1] > s + EPS), sum(p[2] for p in w if p[1] >= s - EPS)
    return None


def split_by(keys: list, frac=SPLIT, rnd=False):
    """keys = [(time, event)]; first 70% (int, or round for r4e) = discovery."""
    ks = sorted(set(keys))
    cut = int(round(len(ks) * frac)) if rnd else int(len(ks) * frac)
    return {e for _, e in ks[:cut]}, {e for _, e in ks[cut:]}


# ------------------------------------------------------------------------------------------------ r3 earnings C1E
def mid(a, b):
    return (a + b) / 2 if a is not None and b is not None and 0 <= b < a <= 1 else None


def call_hour(ms, H, need, hi_thr, lo_thr):
    first_close = min(iso(m["close_time"]) for m in ms)
    hits = defaultdict(int)
    for m in ms:
        hist = []
        for end, c in H.get(m["ticker"], []):
            md = mid(c[0], c[1])
            before = [v for t, v in hist if end - 6 * 3600 <= t <= end - 3600 and v is not None]
            now = (c[2] is not None and c[2] >= hi_thr) or (md is not None and md >= hi_thr)
            if before and min(before) <= lo_thr and now and first_close - 30 * 3600 <= end <= first_close + 3600:
                hits[end] += 1
            hist.append((end, md))
    for end in sorted(hits):
        if hits[end] >= need:
            return end
    return None


def call_hour_busiest(ms, H):
    """Alternative locator (robustness only): start of the event's busiest hour (summed volume) in the 30 h before the
    first close."""
    fc = min(iso(m["close_time"]) for m in ms)
    v = defaultdict(float)
    for m in ms:
        for end, c in H.get(m["ticker"], []):
            if fc - 30 * 3600 < end <= fc + 3600:
                v[end] += c[4]
    return max(v, key=v.get) if v else None


def r3_cancel(call_end, first_close, mode="rule"):
    start = call_end - 3600
    d = dt.datetime.fromtimestamp(start, ET)
    c = int(d.replace(hour=15 if d.hour >= 12 else 7, minute=0, second=0, microsecond=0).timestamp())
    if mode == "rule":
        c = min(c, start - 3600)
    return min(c, first_close)


def build_r3(locator="jump"):
    src = S / "r3_earnings_call_mentions"
    C = raw_candles(src / "api_cache"); H, D = C.get(3600, {}), C.get(86400, {})
    ms_all = {}
    for l in (src / "markets_live.jsonl").open():
        m = json.loads(l); ms_all[m["ticker"]] = m
    ev = defaultdict(list)
    for m in ms_all.values():
        if m["event_ticker"].startswith("KXEARNINGSMENTION"):
            ev[m["event_ticker"]].append(m)
    ev = {e: x for e, x in ev.items() if all(m["result"] in ("yes", "no") for m in x) and any(m["ticker"] in H for m in x)}
    disc, val = split_by([(max(iso(m["close_time"]) for m in x), e) for e, x in ev.items()])
    sprints = raw_prints(src / "api_cache", src / "calls.log", 2)
    prints = dict(sprints)
    p5 = raw_prints(S / "r5_seeded_book_placeholder_split" / "api_cache", S / "r5_seeded_book_placeholder_split" / "calls.log", 3)
    for k, v in p5.items():
        prints[k] = prints.get(k, []) + v
    rows, skip, calls = [], defaultdict(int), {}
    for e, ms in ev.items():
        if locator == "jump":
            ce = None
            for need, hi, lo in ((2, 0.90, 0.60), (1, 0.90, 0.60), (1, 0.85, 0.70)):
                ce = call_hour(ms, H, need, hi, lo)
                if ce is not None:
                    break
        else:
            ce = call_hour_busiest(ms, H)
        calls[e] = ce
        if ce is None:
            skip["no_call"] += len(ms); continue
        fc = min(iso(m["close_time"]) for m in ms); lc = max(iso(m["close_time"]) for m in ms)
        cancel = r3_cancel(ce, fc)
        for m in ms:
            tk = m["ticker"]; op = iso(m["open_time"])
            t0 = -(-(op + 3600) // 3600) * 3600
            if t0 >= cancel:
                skip["post_after_cancel"] += 1; continue
            cs = H.get(tk, [])
            q = None
            for end, c in cs:
                if end <= t0:
                    q = (end, c)
                else:
                    break
            if q is None or q[0] < op or t0 - q[0] > 48 * 3600:
                skip["no_quote"] += 1; continue
            ask, bid = q[1][0], q[1][1]
            if ask is None or bid is None or not (0 < bid < ask < 1) or ask - bid < 0.02 - EPS:
                skip["no_signal"] += 1; continue
            s = round(ask - 0.01, 2); qn = round(1 - s, 4); N = math.floor(5 / qn + EPS)
            thr = anyf = False; v_thr = v_any = 0.0; covered = []
            n_exact = 0; v_c = 0.0
            for end, c in cs:
                if end - 3600 >= t0 and end <= cancel:
                    covered.append(end)
                    xc = candle_exact(sprints.get(tk), end - 3600, end, s) if c[2] is not None and c[2] >= s - EPS else None
                    n_exact += xc is not None
                    if c[2] is not None and c[2] > s + EPS:
                        thr = True; v_thr += c[4] if xc is None else xc[0]; v_c += c[4]
                    if c[2] is not None and c[2] >= s - EPS:
                        anyf = True; v_any += c[4] if xc is None else xc[1]
            for end, c in D.get(tk, []):
                if end - 86400 >= t0 and end <= cancel:
                    dup = any(end - 86400 < h <= end for h in covered)
                    if c[2] is not None and c[2] > s + EPS:
                        thr = True; v_thr += 0 if dup else c[4]; v_c += 0 if dup else c[4]
                    if c[2] is not None and c[2] >= s - EPS:
                        anyf = True; v_any += 0 if dup else c[4]
            x = exact_fill(prints.get(tk), t0 + 120, cancel, s, N)
            rows.append({"sample": "r3", "e": e, "t": tk, "part": "disc" if e in disc else "val", "tclose": lc, "post": t0, "cancel": cancel,
                         "ask": ask, "bid": bid, "s": s, "q": qn, "N": N, "won": m["result"] == "no",
                         "f": min(N, v_thr) if thr else 0.0, "f_any": min(N, v_any) if anyf else 0.0, "exact_candles": n_exact,
                         "f_candle_only": min(N, v_c) if thr else 0.0,
                         "x_f": None if x is None else x[0], "x_f_any": None if x is None else x[1]})
    return rows, dict(skip), calls


# ------------------------------------------------------------------------------------------------ r2 mention C1
def build_r2():
    src = S / "r2_mentions_baserate"
    H = raw_candles(src / "api_cache").get(3600, {})
    mk = [json.loads(l) for l in (src / "markets_recent.jsonl").open()]
    mk = [m for m in mk if m["series"] != "KXTRUMPSAY" and m["ticker"] in H and m["result"] in ("yes", "no")]
    ev = defaultdict(list)
    for m in mk:
        m["o"], m["c"] = iso(m["open_time"]), iso(m["close_time"]); ev[m["event_ticker"]].append(m)
    fc = {e: min(m["c"] for m in x) for e, x in ev.items()}; lc = {e: max(m["c"] for m in x) for e, x in ev.items()}
    lo_fetch = {e: (max(min(m["o"] for m in x), lc[e] - 96 * 3600) // 3600) * 3600 - 3600 for e, x in ev.items()}
    disc, val = split_by([(lc[e], e) for e in ev])
    sprints = raw_prints(src / "api_cache", src / "calls.log", 2)
    rd = src / "repro" / "trades"            # prints fetched by the round-2 adversarial repro (source-side data)
    if rd.exists():
        for fn in sorted(os.listdir(rd)):
            x = json.load(open(rd / fn)); lo, hi = x["window"]
            pr = [(iso(t[0]), float(t[1]), float(t[2]), False) for t in x.get("trades") or []]
            sprints.setdefault(x["order"]["t"], []).append((lo, hi, bool(x.get("complete")), pr))
    prints = {k: list(v) for k, v in sprints.items()}
    p5 = raw_prints(S / "r5_seeded_book_placeholder_split" / "api_cache", S / "r5_seeded_book_placeholder_split" / "calls.log", 3)
    for k, v in p5.items():
        prints[k] = prints.get(k, []) + v
    rows, skip = [], defaultdict(int)
    for m in mk:
        e = m["event_ticker"]; cs = H[m["ticker"]]
        cand = None
        for end, c in cs:
            if end < m["o"] + 3600 or end >= m["c"] or end >= fc[e] or c[0] is None or c[1] is None:
                continue
            if 0 < c[1] <= c[0] < 1:
                cand = (end, c[0], c[1]); break
        if cand is None:
            skip["no_candidate"] += 1; continue
        Hh, ask, bid = cand
        s = round(ask - 0.01, 2)
        if not s > bid + EPS:
            skip["spread_lt_2c"] += 1; continue
        if m["o"] < lo_fetch[e]:
            skip["truncated_entry"] += 1; continue
        qn = round(1 - s, 4); N = math.floor(5 / qn + EPS)
        end_w = min(fc[e] + 3600, m["c"] + 3600)
        thr = anyf = False; v_thr = v_any = v_c = 0.0; n_exact = 0
        for end, c in cs:
            if end <= Hh or end > end_w:
                continue
            xc = candle_exact(sprints.get(m["ticker"]), max(end - 3600, Hh), min(end, end_w), s) if c[2] is not None and c[2] >= s - EPS else None
            n_exact += xc is not None
            if c[2] is not None and c[2] > s + EPS:
                thr = True; v_thr += c[4] if xc is None else xc[0]; v_c += c[4]
            if c[2] is not None and c[2] >= s - EPS:
                anyf = True; v_any += c[4] if xc is None else xc[1]
        x = exact_fill(prints.get(m["ticker"]), Hh + 120, end_w, s, N)
        rows.append({"sample": "r2", "e": e, "t": m["ticker"], "part": "disc" if e in disc else "val", "tclose": lc[e], "post": Hh,
                     "cancel": end_w, "ask": ask, "bid": bid, "s": s, "q": qn, "N": N, "won": m["result"] == "no",
                     "f": min(N, v_thr) if thr else 0.0, "f_any": min(N, v_any) if anyf else 0.0, "exact_candles": n_exact,
                     "f_candle_only": min(N, v_c) if thr else 0.0,
                     "x_f": None if x is None else x[0], "x_f_any": None if x is None else x[1]})
    return rows, dict(skip)


# ------------------------------------------------------------------------------------------------ r4 earnings archive
def ticker_date(e):
    g = re.search(r"-(\d\d)([A-Z]{3})(\d\d)$", e)
    return dt.date(2000 + int(g[1]), MON[g[2]], int(g[3])) if g else None


def r4e_cancel(r, cs):
    """07:00 ET on the call day D (ticker date if the first close is 0-2 days after it, else the ET date of first close
    - 6 h); moved earlier if the market's busiest hour in the 30 h before the first close starts before it; never later
    than first close - 1 h or the end of candle coverage."""
    fc = r["first_close"]; td = ticker_date(r["e"]); fcd = dt.datetime.fromtimestamp(fc, ET).date()
    D = td if (td is not None and 0 <= (fcd - td).days <= 2) else dt.datetime.fromtimestamp(fc - 6 * 3600, ET).date()
    c = int(dt.datetime(D.year, D.month, D.day, 7, tzinfo=ET).timestamp())
    w = [x for x in cs if fc - 30 * 3600 < x[0] <= fc + 3600 and x[5] > 0]
    if w:
        V = max(w, key=lambda x: x[5])[0] - 3600
        if V < c:
            d2 = dt.datetime.fromtimestamp(V, ET).date()
            c = min(int(dt.datetime(d2.year, d2.month, d2.day, 7, tzinfo=ET).timestamp()), V - 7200)
    c = min(c, fc - 3600)
    if r["hi"] < fc + 3600:
        c = min(c, r["hi"])
    return c


def build_r4e():
    src = S / "r4_earnings_seeded_book_48h_forward"
    plan = json.loads((src / "plan.json").read_text())["rows"]
    Cd = {}
    for l in (src / "candles.jsonl").open():
        x = json.loads(l); Cd[x["t"]] = x["c"]     # [end, ask_c, bid_c, px_hi, px_lo, vol, bid_hi, ask_lo]
    T = {}
    for l in (src / "trades.jsonl").open():
        x = json.loads(l)
        if x["t"] in T:
            T[x["t"]]["prints"] += x["prints"]; T[x["t"]]["cursor"] = x["cursor"]
        else:
            T[x["t"]] = x
    orders, skip = [], defaultdict(int)
    for r in plan:
        cs = Cd.get(r["t"])
        if not cs:
            skip["no_candles"] += 1; continue
        CA = r4e_cancel(r, cs); P = r["post"]
        if P >= CA:
            skip["post_after_cancel"] += 1; continue
        q = None
        for c in cs:
            if c[0] <= P:
                q = c
            else:
                break
        if q is None or q[0] < r["open"] or P - q[0] > 3 * 3600 or q[1] is None or q[2] is None:
            skip["no_quote"] += 1; continue
        ask, bid = q[1], q[2]
        if not (0 < bid < ask < 1) or ask - bid < 0.02 - EPS:
            skip["no_signal"] += 1; continue
        s = round(ask - 0.01, 2); qn = round(1 - s, 4); N = int(5 // qn)
        tr = T.get(r["t"])
        if tr is not None and not tr["cursor"]:
            w = [p for p in tr["prints"] if P + 120 < p[0] <= CA and not p[4]]
            f = float(min(N, sum(p[2] for p in w if p[1] > s + EPS))); fa = float(min(N, sum(p[2] for p in w if p[1] >= s - EPS))); srcf = "prints"
        else:
            L = U = La = Ua = 0.0
            for c in cs:
                if c[0] > P and c[0] - 3600 < CA and c[3] is not None:
                    U += c[5] if c[3] > s + EPS else 0; Ua += c[5] if c[3] >= s - EPS else 0
                if c[0] - 3600 >= P + 3600 and c[0] <= CA and c[4] is not None:
                    L += c[5] if c[4] > s + EPS else 0; La += c[5] if c[4] >= s - EPS else 0

            def res(L, U):
                return 0.0 if U == 0 else float(N) if L >= N else float(min(N, L)) if abs(L - U) < EPS else None
            f, fa, srcf = res(L, U), res(La, Ua), "candles"
            if f is None:
                skip["unresolved"] += 1; continue
        orders.append({"sample": "r4e", "e": r["e"], "t": r["t"], "tclose": r["first_close"], "post": P, "cancel": CA, "ask": ask, "bid": bid,
                       "s": s, "q": qn, "N": N, "won": r["result"] == "no", "f": f, "f_any": fa if fa is not None else f, "src": srcf,
                       "x_f": None, "x_f_any": None})
    disc, val = split_by([(o["tclose"], o["e"]) for o in orders], rnd=True)
    for o in orders:
        o["part"] = "disc" if o["e"] in disc else "val"
    return orders, dict(skip)


# ------------------------------------------------------------------------------------------------ r4 single appearance
def build_r4s():
    src = S / "r4_single_appearance_seeded_books"
    O = [json.loads(l) for l in (src / "orders.jsonl").open()]
    T = {}
    for l in (src / "trades.jsonl").open():
        x = json.loads(l); T[x["t"]] = x
    disc, val = split_by([(o["ev_last_close"], o["e"]) for o in O])
    rows = []
    for o in O:
        if not o["posted"]:
            continue
        s = o["s"]; qn = round(1 - s, 4); N = math.floor(5 / qn + EPS)
        f = min(N, o["P"]["vthr"]) if o["P"]["thr"] else 0.0
        fa = min(N, o["P"]["vany"]) if o["P"]["any"] else 0.0
        x = T.get(o["t"]); xf = xfa = None
        if x is not None:
            a, b = o["tq"] + 120, o["C"]
            w = [p for p in x["trades"] if a < p[0] <= b and not p[4]]
            xf = min(N, sum(p[2] for p in w if p[1] > s + EPS)); xfa = min(N, sum(p[2] for p in w if p[1] >= s - EPS))
            f, fa = xf, xfa                          # exact prints override the candle evidence (as the source does)
        rows.append({"sample": "r4s", "e": o["e"], "t": o["t"], "part": "disc" if o["e"] in disc else "val", "tclose": o["ev_last_close"],
                     "post": o["tq"], "cancel": o["C"], "ask": o["ask"], "bid": o["bid"], "s": s, "q": qn, "N": N, "won": not o["yes"],
                     "f": f, "f_any": fa, "x_f": xf, "x_f_any": xfa})
    return rows


# ------------------------------------------------------------------------------------------------ statistics
def cp_lower(k, n, a=0.05):
    """One-sided Clopper-Pearson lower bound: p with P(X >= k | n, p) = a."""
    if k == 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        p = (lo + hi) / 2
        tail = sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
        lo, hi = (p, hi) if tail < a else (lo, p)
    return (lo + hi) / 2


def sw(rows, key="f"):
    """Size-weighted through-only return per $ with an event-clustered ratio t."""
    R = [r for r in rows if (r[key] or 0) > 0]
    if not R:
        return {"n": 0}
    stake = sum(r[key] * r["q"] for r in R); pnl = sum(r[key] * ((1.0 if r["won"] else 0.0) - r["q"]) for r in R)
    ret = pnl / stake
    ev = defaultdict(lambda: [0.0, 0.0])
    for r in R:
        ev[r["e"]][0] += r[key] * ((1.0 if r["won"] else 0.0) - r["q"]); ev[r["e"]][1] += r[key] * r["q"]
    G = len(ev)
    u2 = sum((p - ret * s) ** 2 for p, s in ev.values())
    se = math.sqrt(G / (G - 1) * u2) / stake if G > 1 else float("nan")
    se0 = math.sqrt(u2) / stake if G > 1 else float("nan")
    srt = sorted(R, key=lambda r: -r[key] * ((1.0 if r["won"] else 0.0) - r["q"]))
    rest = srt[3:]
    wo3 = (sum(r[key] * ((1.0 if r["won"] else 0.0) - r["q"]) for r in rest) / sum(r[key] * r["q"] for r in rest)) if rest else float("nan")
    tc = sorted(r["tclose"] for r in R); m = tc[len(tc) // 2]

    def ratio(x):
        sx = sum(r[key] * r["q"] for r in x)
        return sum(r[key] * ((1.0 if r["won"] else 0.0) - r["q"]) for r in x) / sx if sx else None
    return {"n": len(R), "events": G, "win": round(sum(r["won"] for r in R) / len(R), 4), "avg_px": round(st.mean(r["q"] for r in R), 4),
            "ret_per_dollar": round(ret, 4), "t": round(ret / se, 3) if se and se == se else float("nan"),
            "t_no_small_sample_corr": round(ret / se0, 3) if se0 and se0 == se0 else float("nan"),
            "ret_wo3": round(wo3, 4), "half1": ratio([r for r in R if r["tclose"] < m]), "half2": ratio([r for r in R if r["tclose"] >= m]),
            "staked": round(stake, 2), "pnl": round(pnl, 2), "median_filled_contracts": st.median(r[key] for r in R)}


def describe(rows, key="f"):
    if not rows:
        return {"posted": 0}
    filled = [r for r in rows if (r[key] or 0) > 0]; unf = [r for r in rows if not (r[key] or 0) > 0]
    out = {"posted": len(rows), "events_posted": len({r["e"] for r in rows}), "filled": len(filled), "sw_through": sw(rows, key)}
    if filled:
        eq = cell_stats([{"e": r["e"], "ret": ((1.0 if r["won"] else 0.0) - r["q"]) / r["q"], "won": r["won"], "px": r["q"]} for r in filled])
        out["eq_through"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in eq.items()}
        first = {}
        for r in sorted(filled, key=lambda r: (r["post"], r["t"])):
            first.setdefault(r["e"], r)
        k = sum(r["won"] for r in first.values()); n = len(first); px = st.mean(r["q"] for r in first.values())
        lo = cp_lower(k, n)
        out["binomial_first_fill_per_event"] = {"events": n, "wins": k, "avg_px": round(px, 4), "win_lo95": round(lo, 4), "ret_at_lo95": round(lo / px - 1, 4)}
        out["filled_NO_win"] = round(st.mean(r["won"] for r in filled), 3)
        days = (max(r["tclose"] for r in filled) - min(r["tclose"] for r in filled)) / 86400
        out["fills_per_day"] = round(len(filled) / days, 2) if days > 0 else None
    out["unfilled_NO_win"] = round(st.mean(r["won"] for r in unf), 3) if unf else None
    return out


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    r3, sk3, calls3 = build_r3()
    r2, sk2 = build_r2()
    r4e, sk4e = build_r4e()
    r4s = build_r4s()
    rows = r2 + r3 + r4e + r4s
    for r in rows:
        r["arm"] = "P" if is_P(r["bid"], r["ask"]) else "NP"; r["S2"] = is_S2(r["bid"], r["ask"])
    res = {"skips": {"r3": sk3, "r2": sk2, "r4e": sk4e}, "samples": {}}
    for smp in ("r2", "r3", "r4e", "r4s", "pooled"):
        R = rows if smp == "pooled" else [r for r in rows if r["sample"] == smp]
        res["samples"][smp] = {"census": {"posted": len(R), "events": len({r["e"] for r in R}),
                                          "P_share": round(sum(r["arm"] == "P" for r in R) / len(R), 3) if R else None}}
        for arm in ("P", "NP"):
            for part in ("disc", "val", "all"):
                x = [r for r in R if r["arm"] == arm and (part == "all" or r["part"] == part)]
                res["samples"][smp][f"{arm}|{part}"] = describe(x)
    # exact-print variant (r2 / r3 orders whose whole window has cached complete prints; others keep candle proxies)
    ex = []
    for r in rows:
        y = dict(r)
        if r["sample"] in ("r2", "r3") and r["x_f"] is not None:
            y["f"] = r["x_f"]; y["src_ex"] = "prints"
        ex.append(y)
    res["exact_print_variant"] = {}
    for smp in ("r3", "pooled"):
        for part in ("val", "all"):
            x = [r for r in ex if r["arm"] == "P" and r["part"] in ({"val"} if part == "val" else {"disc", "val"}) and (smp == "pooled" or r["sample"] == smp)]
            res["exact_print_variant"][f"{smp}|P|{part}"] = {"sw": sw(x), "orders_with_prints": sum(1 for r in x if r.get("src_ex") == "prints"),
                                                             "filled_with_prints": sum(1 for r in x if r.get("src_ex") == "prints" and r["f"] > 0)}
    # pure candle-volume proxy (no per-candle exact override) for r2 / r3
    co = [dict(r, f=r["f_candle_only"]) if "f_candle_only" in r else r for r in rows]
    res["candle_only_variant"] = {f"pooled|{arm}|{part}": sw([r for r in co if r["arm"] == arm and (part == "all" or r["part"] == part)])
                                  for arm in ("P", "NP") for part in ("val", "all")}
    # S2 robustness
    for part in ("val", "all"):
        x = [r for r in rows if r["S2"] and (part == "all" or r["part"] == part)]
        res[f"S2|{part}"] = sw(x)
    # r3 locator robustness: busiest-hour call locator
    r3b, _, calls_b = build_r3("busiest")
    for r in r3b:
        r["arm"] = "P" if is_P(r["bid"], r["ask"]) else "NP"
    res["r3_busiest_hour_locator"] = {f"{arm}|{part}": sw([r for r in r3b if r["arm"] == arm and (part == "all" or r["part"] == part)])
                                      for arm in ("P", "NP") for part in ("val", "all")}
    agree = sum(1 for e in calls3 if calls3[e] and calls_b.get(e) and abs(calls3[e] - calls_b[e]) <= 3 * 3600)
    res["r3_locator_agreement_within_3h"] = f"{agree}/{len(calls3)}"
    # per-event breakdown of validation P (where the money is)
    pe = defaultdict(lambda: {"fills": 0, "stake": 0.0, "pnl": 0.0, "wins": 0})
    for r in rows:
        if r["arm"] == "P" and r["part"] == "val" and r["f"] > 0:
            d = pe[r["sample"] + ":" + r["e"]]; d["fills"] += 1; d["stake"] += r["f"] * r["q"]; d["pnl"] += r["f"] * ((1.0 if r["won"] else 0.0) - r["q"]); d["wins"] += r["won"]
    res["val_P_per_event"] = {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in sorted(pe.items(), key=lambda kv: -kv[1]["pnl"])}
    ev_pnl = sorted((v["pnl"], v["stake"]) for v in pe.values())
    if len(ev_pnl) > 2:
        rest = ev_pnl[:-2]
        res["val_P_without_best_2_events"] = round(sum(p for p, _ in rest) / sum(s for _, s in rest), 4)
        rest1 = ev_pnl[:-1]
        res["val_P_without_best_event"] = round(sum(p for p, _ in rest1) / sum(s for _, s in rest1), 4)
    (OUT / "repro_rows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (OUT / "repro.json").write_text(json.dumps(res, indent=1, default=str))
    p = res["samples"]["pooled"]
    for k in ("P|disc", "P|val", "P|all", "NP|val", "NP|all"):
        v = p[k]["sw_through"]
        print(f"pooled {k:7s} posted {p[k]['posted']:5d} filled {p[k]['filled']:5d} n {v.get('n')} ev {v.get('events')} win {v.get('win')} "
              f"px {v.get('avg_px')} ret {v.get('ret_per_dollar')} t {v.get('t')} wo3 {v.get('ret_wo3')} h {v.get('half1')}/{v.get('half2')} "
              f"med {v.get('median_filled_contracts')}")
    for smp in ("r2", "r3", "r4e", "r4s"):
        for k in ("P|val", "P|all", "NP|all"):
            v = res["samples"][smp][k].get("sw_through", {})
            print(f"  {smp:4s} {k:7s} posted {res['samples'][smp][k].get('posted')} n {v.get('n')} ev {v.get('events')} ret {v.get('ret_per_dollar')} t {v.get('t')} staked {v.get('staked')}")
    print("census", {s: res["samples"][s]["census"] for s in res["samples"]})
    print("skips", res["skips"])
    print("exact", json.dumps(res["exact_print_variant"])[:1500])
    print("S2", res["S2|val"], res["S2|all"])
    print("busiest", res["r3_busiest_hour_locator"], res["r3_locator_agreement_within_3h"])
    print("val P per event", json.dumps(res["val_P_per_event"]))
    print("wo best 2 ev", res.get("val_P_without_best_2_events"), "wo best ev", res.get("val_P_without_best_event"))
    return res


if __name__ == "__main__":
    run()
