"""r4_long_dated_maker_no_logger: forward PAPER log of the long-dated maker-NO rule (and its taker comparator).

One pass per invocation (recommended: hourly at minute 7, e.g. launchd StartCalendarInterval Minute=7; the owner
installs it). At most 10 Kalshi calls per pass (R4LD_MAX_CALLS / --max-calls may only lower it), every call through
lab.us.data_refresh.fetch (temperature-bot idle window, 429 retries) and logged in calls.log. It never places orders.

Frozen rule (data/kalshi_lab/strategies/r4_long_dated_maker_no/preregistration.json, written before the first pass):
  Universe: open binary markets of the SERIES list below with scheduled deadline S (r3_long_dated_longshot_no_repro.
  deadline() on the REST market object, known at listing) and S - open_time >= 45 days.
  Signal: for D in {60, 45, 30}: at the first pass at or after T = S - D days + 1 h (no later than T + 6 h, else the
  pair is logged 'missed' and never entered), read the market quote (one /markets?event_ticker call per event).
  In band when the NO taker price 1 - yes_bid is in [0.70, 0.95).
  Arms, all virtual, one per (market, D):
    taker : buy NO at the logged executable price 1 - yes_bid, N = floor($5 / price) contracts, size capped by the
            YES-bid depth when an order-book probe was made; taker fee 0.07 * mult * p (1 - p) per order, rounded up.
    C1    : if yes_ask - yes_bid >= 2c, rest a sell-YES at s = yes_ask - 1c (buy NO at 1 - s) for N = floor(5 / (1 - s))
            contracts for 7 days (cancel at T + 7 d or the market's close, whichever is first).
    C2    : the same order re-priced daily: at T + k days (k = 0..6; first pass in [T + k d, T + k d + 6 h]) the
            order is moved to that quote's ask - 1c if the spread is >= 2c and the NO taker price is in [0.70, 0.98);
            otherwise it rests nowhere that day. C3 = the C2 orders whose signal NO price is in [0.80, 0.92) (summary).
  Maker fills (gate amendment c): Kalshi trade prints (/markets/trades) strictly after post + 120 s and before the
  cancel, block trades excluded. 'through' = yes_price > s (primary), 'any' = yes_price >= s; size-weighted against N.
  C2: the first day whose own window has through prints fills min(N, through count) of that day's order (no carry).
  Maker fees: 0 for 'quadratic', 0.25 x the taker formula for 'quadratic_with_maker_fees' (series_all.json).
Stages of a pass, in priority order: due signals (event quotes); C2 re-pricing; order-book probes (<= 2 per pass,
capacity at the signal); fill jobs (orders whose window ended: trade prints); settlement checks (closed markets with
arms); series discovery (rotation, each series at most once per 20 h).
Files (data/kalshi_lab/strategies/r4_long_dated_maker_no/): forward.jsonl (append-only rows typed listing / signal /
missed / reprice / probe / fill / settle / pass), state.json, trades/<ticker>_<D>.json, calls.log, .lock.
Usage: .venv/bin/python -m lab.kalshi.strategies.r4_long_dated_maker_no_logger [--max-calls N] [--selftest]"""
from __future__ import annotations
import calendar, fcntl, json, math, os, sys, tempfile, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from lab.kalshi.strategies.r4_long_dated_maker_no_api import Budget, BudgetExhausted

OUT = Path(os.environ.get("R4LD_OUT", "/Users/roomyhome/Tradeinc/data/kalshi_lab/strategies/r4_long_dated_maker_no"))
SERIES_ALL = Path("/Users/roomyhome/Tradeinc/data/kalshi_lab/series_all.json")

# ------------------------------------------------------------------------------------------------ frozen constants
SERIES = ["KXFEDCHAIRNOM", "KXGOVSHUT", "KXTRUMPPARDON", "KXCABOUT", "KXTRUMPADMINLEAVE", "KXUSAIRANAGREEMENT", "KXNOBELPEACE",
          "KXGREENLAND", "KXLLM1", "KXALIENS", "KXSPACEXSTARSHIP", "KXOAIAGI", "KXTIKTOKBAN", "KXOAIPROFIT", "KXRATECUTCOUNT",
          "KXLAYOFFSYINFO", "KXFEDDECISION", "KXGOVSHUTLENGTH", "KXSUPERBOWLAD", "KXFED", "KXHORMUZNORM", "KXGOVTSHUTDOWN",
          "KXCITRINI", "KXSPACEXCOUNT", "KXGOVTSHUTLENGTH", "KXDHSFUND", "KXTRUMPUFC", "KXATTENDSOTU", "KXGDP", "KXSAVEACT",
          "KXSECAG", "KXGOVTCUTS", "KXSECDEF", "KXSECHHS", "KXTRUMPOUT27", "KXNEXTAG", "KXBIDENPARDON", "KXRATECUT",
          "KXTRUMPATTEND", "KXLEAVEPOWELL", "KXTRUMPOUT", "KXCRYPTOSTRUCTURE", "KXRECSSNBER", "KXFEDCHAIRCONFIRM",
          "KXTRUMPAPPROVE", "KXPRESNOMFEDCHAIR",
          # round-4 fresh series (this family's mini-census)
          "KXFEDHIKE", "KXTRUMPPHOTO", "KXMJSCHEDULE", "KXCLOSEHORMUZ", "KXGOLDCARDS", "CABINETMUSK", "KXNEXTPRESSEC",
          "KXLEAVEWALZ", "KXSENMAJORITY", "KXSHUTDOWNBY", "KXVOTERFK", "KXLEAVEADMIN", "KXBONDIOUT", "KXEOCOUNTDAY1",
          "KXHORMUZWEEKLY", "KXSECTREASURY", "KXDNI", "KXMODELHIGH", "KXINAUG", "KXEOCOUNTDAY1B", "KXDJTVOSTARIFFS", "KXIMPEACH"]
DS = (60, 45, 30)
BAND = (0.70, 0.95)
REFRESH_HI = 0.98             # C2 re-pricing keeps the order while the NO taker price is in [0.70, 0.98)
C3_BAND = (0.80, 0.92)
W_DAYS = 7
MIN_LIFE = 45 * 86400
LATE_S = 6 * 3600             # a due signal / re-price may be read up to 6 h late, else it is missed
POST_GAP_S = 120
STAKE = 5.0
MAX_CALLS = 10
PROBES_PER_PASS = 2
DISC_GAP_S = 20 * 3600
DISC_MAX = 5                  # series listings per pass (68 series / 20 h at hourly passes needs ~3.4)
DISC_RESERVE = 2              # calls kept after discovery for signals that discovery found due right now
SETTLE_GAP_S = 12 * 3600
CLOSED = {"closed", "settled", "determined", "finalized", "disputed", "amended"}


# ------------------------------------------------------------------------------------------------ helpers
def ts_of(s) -> float | None:
    if not s:
        return None
    s = str(s).replace("Z", "")
    try:
        base = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    frac = s[19:]
    return base + (float("0" + frac) if frac.startswith(".") else 0.0)


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def cents(x) -> int | None:
    try:
        return int(round(float(x) * 100))
    except (TypeError, ValueError):
        return None


def fnum(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def deadline(m: dict):
    from lab.kalshi.strategies.r3_long_dated_longshot_no_repro import deadline as d
    return d(m)


def fee_table() -> dict:
    try:
        d = json.loads(SERIES_ALL.read_text())
        items = d.get("series", d) if isinstance(d, dict) else d
        items = items if isinstance(items, list) else list(items.values())
        return {x["ticker"]: (x.get("fee_type") or "quadratic", float(x.get("fee_multiplier") if x.get("fee_multiplier") is not None else 1)) for x in items}
    except Exception:
        return {}


def fee_order(p: float, n: int, kind: str, ftype: str, mult: float) -> float:
    """Fee in $ for one order of n contracts at price p, rounded up to the cent."""
    if kind == "maker":
        if ftype != "quadratic_with_maker_fees":
            return 0.0
        rate = 0.25 * 0.07 * mult
    else:
        rate = 0.07 * mult
    return math.ceil(round(rate * n * p * (1 - p) * 100, 9)) / 100 if rate > 0 else 0.0


def n_for(p: float) -> int:
    return max(1, int(STAKE // p)) if p > 0 else 1


def book_depth(ob: dict) -> tuple[list, list]:
    """(YES bids, NO bids) as [[price_dollars, size], ...] best last (Kalshi order), from orderbook_fp or legacy cents."""
    fp = ob.get("orderbook_fp") or {}
    if fp:
        return [[fnum(p), fnum(q)] for p, q in fp.get("yes_dollars") or []], [[fnum(p), fnum(q)] for p, q in fp.get("no_dollars") or []]
    o = ob.get("orderbook") or {}
    return [[p / 100, fnum(q)] for p, q in o.get("yes") or []], [[p / 100, fnum(q)] for p, q in o.get("no") or []]


def parse_prints(d: dict) -> list:
    out = []
    for x in d.get("trades") or []:
        t = ts_of(x.get("created_time"))
        if t is None:
            continue
        out.append([t, fnum(x.get("yes_price_dollars") or (fnum(x.get("yes_price")) / 100)), fnum(x.get("count_fp") or x.get("count")),
                    x.get("taker_side"), bool(x.get("is_block_trade"))])
    return sorted(out)


def fill_from_prints(prints: list, s: float, N: int, lo: float, hi: float) -> dict:
    win = [p for p in prints if lo < p[0] < hi and not p[4]]
    thr = sum(p[2] for p in win if p[1] > s + 1e-9)
    anyc = sum(p[2] for p in win if p[1] >= s - 1e-9)
    t1 = next((p[0] for p in win if p[1] > s + 1e-9), None)
    return {"n_prints": len(win), "through_cnt": round(thr, 2), "any_cnt": round(anyc, 2), "f_through": round(min(N, thr) / N, 4),
            "f_any": round(min(N, anyc) / N, 4), "t_first_through": t1}


# ------------------------------------------------------------------------------------------------ the logger
class Logger:
    def __init__(self, out: Path, budget, now: float | None = None) -> None:
        self.out = out; self.B = budget; self.now = now or time.time(); self._t0 = time.time()
        self.rows: list[dict] = []; self.stage = defaultdict(int); self.errors: list[str] = []
        self.fees = fee_table()
        sf = out / "state.json"
        self.st = json.loads(sf.read_text()) if sf.exists() else {}
        for k in ("series_checked", "markets", "arms", "settle_checked"):
            self.st.setdefault(k, {})
        self.st.setdefault("started", self.now)
        self.st.setdefault("prereg_present_at_start", (out / "preregistration.json").exists())

    def clock(self) -> float:
        return self.now + (time.time() - self._t0)

    def row(self, typ: str, **kw) -> None:
        r = {"type": typ, "ts": round(kw.pop("ts", None) or self.clock(), 1)}
        r.update(kw); self.rows.append(r)

    def call(self, path: str) -> dict:
        d = self.B.get(path)
        if not d:
            self.errors.append("empty:" + path[:80])
        return d

    # ---- market tracking
    def track(self, m: dict, series: str, via: str) -> None:
        t = m.get("ticker")
        if not t or m.get("market_type", "binary") != "binary":
            return
        X = self.st["markets"].get(t)
        close = ts_of(m.get("close_time"))
        if X is not None:
            X["close"] = close or X.get("close"); X["mstatus"] = m.get("status")
            if m.get("result") in ("yes", "no"):
                X["result"] = m["result"]
            return
        S, how = deadline(m)
        op = ts_of(m.get("open_time"))
        if S is None or op is None or S - op < MIN_LIFE:
            self.st["markets"][t] = {"eligible": False, "e": m.get("event_ticker"), "series": series}
            return
        X = {"eligible": True, "e": m["event_ticker"], "series": series, "S": S, "S_how": how, "open": op, "close": close,
             "mstatus": m.get("status"), "result": m.get("result") if m.get("result") in ("yes", "no") else None, "D": {}}
        for D in DS:
            T = S - D * 86400 + 3600
            if T < op:
                X["D"][str(D)] = "before_open"
            elif T + LATE_S < self.now:
                X["D"][str(D)] = "past_at_discovery"
            else:
                X["D"][str(D)] = "pending"
        self.st["markets"][t] = X
        self.row("listing", ticker=t, e=m["event_ticker"], series=series, S=S, S_how=how, open=op, close=close, via=via,
                 D_status=X["D"], title=(m.get("title") or "")[:120])

    def due(self) -> dict[str, list]:
        """event -> [(ticker, D, T)] whose signal is due now; marks missed pairs."""
        by = defaultdict(list)
        for t, X in self.st["markets"].items():
            if not X.get("eligible"):
                continue
            for D in DS:
                if X["D"].get(str(D)) != "pending":
                    continue
                T = X["S"] - D * 86400 + 3600
                if self.now >= T + LATE_S:
                    X["D"][str(D)] = "missed"
                    self.row("missed", ticker=t, D=D, T=T, reason="no_pass_in_window")
                elif self.now >= T:
                    by[X["e"]].append((t, D, T))
        return by

    def reprice_due(self) -> dict[str, list]:
        by = defaultdict(list)
        for aid, a in self.st["arms"].items():
            if a["arm"] != "C2" or a.get("closed_book"):
                continue
            for k in range(1, W_DAYS):
                key = str(k)
                Tk = a["T"] + k * 86400
                if key in a["days"]:
                    continue
                if self.now >= Tk + LATE_S:
                    a["days"][key] = {"status": "missed"}
                elif self.now >= Tk:
                    by[self.st["markets"][a["ticker"]]["e"]].append((aid, k, Tk))
        return by

    # ---- stage 1 and 2: event quotes
    def event_quotes(self, e: str) -> dict:
        d = self.call(f"/markets?event_ticker={e}&limit=1000")
        tq = self.clock()
        ms = {m["ticker"]: m for m in d.get("markets") or []}
        for m in ms.values():
            self.track(m, self.st["markets"].get(m["ticker"], {}).get("series") or m["ticker"].split("-")[0], "event")
        return {"tq": tq, "m": ms}

    def signals(self, quotes: dict) -> list[str]:
        posted = []
        for e, items in self.due().items():
            if self.B.left <= 0:
                break
            if e not in quotes:
                q = self.event_quotes(e); self.stage["event_calls"] += 1
                if not q["m"]:
                    continue
                quotes[e] = q
            q = quotes[e]
            for t, D, T in items:
                X = self.st["markets"][t]; m = q["m"].get(t)
                X["D"][str(D)] = "done"
                posted += self.evaluate(t, D, T, m, q["tq"])
        return posted

    def evaluate(self, t: str, D: int, T: float, m: dict | None, tq: float) -> list[str]:
        X = self.st["markets"][t]
        ftype, mult = self.fees.get(X["series"], ("quadratic", 1.0))
        base = {"ticker": t, "e": X["e"], "series": X["series"], "D": D, "T": T, "lag_s": round(tq - T, 1), "S": X["S"], "ftype": ftype, "mult": mult}
        if m is None or m.get("status") != "active":
            self.row("signal", ts=tq, in_band=False, reason="market_not_active" if m else "market_missing", **base)
            return []
        ask, bid = fnum(m.get("yes_ask_dollars")), fnum(m.get("yes_bid_dollars"))
        no_t = round(1 - bid, 4)
        base.update(ask=ask, bid=bid, no_taker=no_t, ask_size=fnum(m.get("yes_ask_size_fp")), bid_size=fnum(m.get("yes_bid_size_fp")),
                    vol=fnum(m.get("volume_fp")), oi=fnum(m.get("open_interest_fp")))
        if not (0 < bid and ask < 1 and ask > bid):
            self.row("signal", ts=tq, in_band=False, reason="no_two_sided_quote", **base)
            return []
        inb = BAND[0] <= no_t < BAND[1]
        self.row("signal", ts=tq, in_band=inb, reason=None if inb else "out_of_band", **base)
        if not inb:
            return []
        close = X.get("close") or X["S"]
        cancel = min(tq + W_DAYS * 86400, close)
        ids = []
        # taker arm (comparator)
        nt = n_for(no_t)
        aid = f"{t}|{D}|taker"
        self.st["arms"][aid] = {"arm": "taker", "ticker": t, "D": D, "T": T, "post": tq, "px": no_t, "N": nt,
                                "fee": fee_order(no_t, nt, "taker", ftype, mult), "probe": None, "settled": False}
        ids.append(aid)
        if ask - bid >= 0.02 - 1e-9:
            s = round(ask - 0.01, 4); pm = round(1 - s, 4); nm = n_for(pm)
            for arm in ("C1", "C2"):
                aid = f"{t}|{D}|{arm}"
                a = {"arm": arm, "ticker": t, "D": D, "T": T, "post": tq, "s": s, "px": pm, "N": nm, "cancel": cancel, "no_signal": no_t,
                     "fee": fee_order(pm, nm, "maker", ftype, mult), "fill": None, "fill_done": False, "settled": False}
                if arm == "C2":
                    a["days"] = {"0": {"status": "posted", "t": tq, "s": s, "N": nm, "px": pm}}
                    a["c3"] = C3_BAND[0] <= no_t < C3_BAND[1]
                self.st["arms"][aid] = a; ids.append(aid)
        else:
            # C2 may still post on a later day when the spread opens; C1 cannot (spread < 2c at the signal)
            aid = f"{t}|{D}|C2"
            self.st["arms"][aid] = {"arm": "C2", "ticker": t, "D": D, "T": T, "post": tq, "s": None, "px": None, "N": None, "cancel": cancel,
                                    "no_signal": no_t, "fee": 0.0, "fill": None, "fill_done": False, "settled": False,
                                    "days": {"0": {"status": "spread_lt_2c", "t": tq}}, "c3": C3_BAND[0] <= no_t < C3_BAND[1]}
            ids.append(aid)
            self.row("order", ts=tq, arm="C1", posted=False, reason="spread_lt_2c", **base)
        for aid in ids:
            a = self.st["arms"][aid]
            if a.get("s") is not None or a["arm"] == "taker":
                self.row("order", ts=tq, arm=a["arm"], posted=True, aid=aid, s=a.get("s"), px=a["px"], N=a["N"], cancel=a.get("cancel"), fee=a["fee"], **base)
        self.stage["signals_in_band"] += 1
        return [t]

    def reprice(self, quotes: dict) -> None:
        for e, items in self.reprice_due().items():
            if self.B.left <= 0:
                break
            if e not in quotes:
                q = self.event_quotes(e); self.stage["event_calls"] += 1
                if not q["m"]:
                    continue
                quotes[e] = q
            q = quotes[e]
            for aid, k, Tk in items:
                a = self.st["arms"][aid]; m = q["m"].get(a["ticker"])
                X = self.st["markets"][a["ticker"]]
                ftype, mult = self.fees.get(X["series"], ("quadratic", 1.0))
                if q["tq"] >= a["cancel"] or m is None or m.get("status") != "active":
                    a["days"][str(k)] = {"status": "inactive", "t": q["tq"]}
                    continue
                ask, bid = fnum(m.get("yes_ask_dollars")), fnum(m.get("yes_bid_dollars"))
                no_t = 1 - bid
                if ask - bid >= 0.02 - 1e-9 and 0 < bid and ask < 1 and BAND[0] <= no_t < REFRESH_HI:
                    s = round(ask - 0.01, 4); pm = round(1 - s, 4)
                    a["days"][str(k)] = {"status": "posted", "t": q["tq"], "s": s, "N": n_for(pm), "px": pm, "fee": fee_order(pm, n_for(pm), "maker", ftype, mult)}
                else:
                    a["days"][str(k)] = {"status": "no_order", "t": q["tq"], "ask": ask, "bid": bid}
                self.row("reprice", ts=q["tq"], aid=aid, k=k, ask=ask, bid=bid, **a["days"][str(k)])

    # ---- stage 3: order-book probes at the signal
    def probes(self, new_tickers: list[str]) -> None:
        n = 0
        for t in dict.fromkeys(new_tickers):
            if n >= PROBES_PER_PASS or self.B.left <= 1:
                break
            ob = self.call(f"/markets/{t}/orderbook"); n += 1; self.stage["probes"] += 1
            if not ob:
                continue
            yes, no = book_depth(ob)
            yb = max((p for p, _ in yes), default=None); nb = max((p for p, _ in no), default=None)
            depth_yes_bid = sum(q for p, q in yes if yb is not None and abs(p - yb) < 1e-9)
            depth_no_bid = sum(q for p, q in no if nb is not None and abs(p - nb) < 1e-9)
            for aid, a in self.st["arms"].items():
                if a["ticker"] == t and a["arm"] == "taker" and a.get("probe") is None and abs(a["post"] - self.clock()) < 3600:
                    a["probe"] = {"yes_bid": yb, "depth_at_no_ask": depth_yes_bid, "no_bid": nb, "depth_no_bid": depth_no_bid}
            self.row("probe", ticker=t, yes_best_bid=yb, depth_yes_bid=depth_yes_bid, no_best_bid=nb, depth_no_bid=depth_no_bid,
                     yes_levels=yes[-5:], no_levels=no[-5:])

    # ---- stage 4: fill jobs
    def fills(self) -> None:
        for aid, a in self.st["arms"].items():
            if self.B.left <= 0:
                break
            if a["arm"] == "taker" or a.get("fill_done"):
                continue
            X = self.st["markets"][a["ticker"]]
            end = min(a["cancel"], X.get("close") or a["cancel"])
            if self.clock() < end + 600:
                continue
            if a["arm"] == "C2" and not any(d.get("status") == "posted" for d in a["days"].values()):
                if len(a["days"]) >= W_DAYS or self.clock() > a["T"] + W_DAYS * 86400 + LATE_S:
                    a["fill_done"] = True; a["fill"] = {"f_through": 0.0, "f_any": 0.0, "never_posted": True}
                    self.row("fill", aid=aid, arm=a["arm"], ticker=a["ticker"], D=a["D"], **a["fill"])
                continue
            if a["arm"] == "C1" and a.get("s") is None:
                a["fill_done"] = True; continue
            prints = self.prints(a["ticker"], a["D"], a["post"] + POST_GAP_S, end)
            if prints is None:
                continue
            if a["arm"] == "C1":
                f = fill_from_prints(prints, a["s"], a["N"], a["post"] + POST_GAP_S, end)
                f.update(px=a["px"], N=a["N"], s=a["s"], fee=a["fee"])
            else:
                f = {"f_through": 0.0, "f_any": 0.0, "day": None}
                for k in sorted(a["days"], key=int):
                    d = a["days"][k]
                    if d.get("status") != "posted":
                        continue
                    lo = d["t"] + POST_GAP_S; hi = min(end, d["t"] + 86400 if int(k) < W_DAYS - 1 else end)
                    x = fill_from_prints(prints, d["s"], d["N"], lo, hi)
                    if x["f_through"] > 0 and f["day"] is None:
                        f = dict(x, day=int(k), px=d["px"], N=d["N"], s=d["s"], fee=d.get("fee", a["fee"]))
                    if f["day"] is None and x["f_any"] > f.get("f_any", 0):
                        f["f_any"] = x["f_any"]; f["any_day"] = int(k)
            a["fill"] = f; a["fill_done"] = True
            self.row("fill", aid=aid, arm=a["arm"], ticker=a["ticker"], D=a["D"], c3=a.get("c3"), **{k: v for k, v in f.items()})

    def prints(self, t: str, D: int, lo: float, hi: float) -> list | None:
        tf = self.out / "trades" / f"{t}_{D}.json"
        if tf.exists():
            j = json.loads(tf.read_text())
            if j.get("complete") and j["lo"] <= lo and j["hi"] >= hi:
                return j["prints"]
        out, cur = [], ""
        for _ in range(3):
            if self.B.left <= 0:
                return None
            d = self.call(f"/markets/trades?ticker={t}&min_ts={int(lo)}&max_ts={int(hi)}&limit=1000" + (f"&cursor={cur}" if cur else ""))
            self.stage["trade_calls"] += 1
            if "trades" not in d:
                return None
            out += parse_prints(d)
            cur = d.get("cursor") or ""
            if not cur or not d.get("trades"):
                tf.parent.mkdir(parents=True, exist_ok=True)
                tf.write_text(json.dumps({"lo": lo, "hi": hi, "complete": True, "prints": sorted(out)}))
                return sorted(out)
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(json.dumps({"lo": lo, "hi": hi, "complete": False, "prints": sorted(out)}))
        return sorted(out)

    # ---- stage 5: settlement
    def settle(self) -> None:
        need = defaultdict(list)
        for aid, a in self.st["arms"].items():
            if a.get("settled"):
                continue
            X = self.st["markets"][a["ticker"]]
            if X.get("result") in ("yes", "no"):
                self.close_arm(aid, a, X["result"])
                continue
            close = X.get("close") or X["S"]
            if self.clock() >= close + 1800:
                need[a["ticker"]].append(aid)
        for t in sorted(need, key=lambda t: self.st["settle_checked"].get(t, 0)):
            if self.B.left <= 0:
                break
            if self.clock() - self.st["settle_checked"].get(t, 0) < SETTLE_GAP_S:
                continue
            d = self.call(f"/markets/{t}"); self.stage["settle_calls"] += 1
            self.st["settle_checked"][t] = self.clock()
            m = d.get("market") or {}
            if m:
                self.track(m, self.st["markets"][t]["series"], "settle")
            res = m.get("result")
            if res in ("yes", "no"):
                for aid in need[t]:
                    self.close_arm(aid, self.st["arms"][aid], res)

    def close_arm(self, aid: str, a: dict, res: str) -> None:
        if a["arm"] != "taker" and not a.get("fill_done"):
            return                      # wait for the fill job (prints) before settling a maker arm
        won = res == "no"
        if a["arm"] == "taker":
            px, n, fee = a["px"], a["N"], a["fee"]
            if a.get("probe") and a["probe"].get("depth_at_no_ask") is not None:
                n_fill = min(n, a["probe"]["depth_at_no_ask"])
            else:
                n_fill = n
            f = n_fill / n if n else 0.0
        else:
            f0 = a.get("fill") or {}
            px, n, fee = f0.get("px", a.get("px")), f0.get("N", a.get("N")), f0.get("fee", a.get("fee", 0.0))
            f = f0.get("f_through", 0.0)
        ret = (((1.0 if won else 0.0) - px) * n - fee) / (px * n) if px and n else None
        a["settled"] = True
        self.row("settle", aid=aid, arm=a["arm"], ticker=a["ticker"], D=a["D"], result=res, won=won, px=px, N=n, fee=fee,
                 f_through=round(f, 4), f_any=(a.get("fill") or {}).get("f_any"), ret_per_dollar=round(ret, 5) if ret is not None else None,
                 c3=a.get("c3"))

    # ---- stage 6: discovery
    def discover(self) -> None:
        order = sorted(SERIES, key=lambda s: self.st["series_checked"].get(s, 0))
        n = 0
        for s in order:
            if self.B.left <= DISC_RESERVE or n >= DISC_MAX:
                break
            if self.clock() - self.st["series_checked"].get(s, 0) < DISC_GAP_S:
                continue
            d = self.call(f"/markets?series_ticker={s}&status=open&limit=1000"); self.stage["disc_calls"] += 1; n += 1
            self.st["series_checked"][s] = self.clock()
            for m in d.get("markets") or []:
                self.track(m, s, "discovery")

    # ---- the pass
    def run(self) -> dict:
        quotes = {}
        try:
            new = self.signals(quotes)
            self.reprice(quotes)
            self.probes(new)
            self.fills()
            self.settle()
            self.discover()
            # new markets found by discovery may be due right now
            new2 = self.signals(quotes)
            self.probes(new2)
        except BudgetExhausted:
            self.errors.append("budget_exhausted")
        self.row("pass", calls=self.B.used, stage=dict(self.stage), errors=self.errors[:20],
                 n_markets=len(self.st["markets"]), n_eligible=sum(1 for x in self.st["markets"].values() if x.get("eligible")),
                 n_arms=len(self.st["arms"]), pending_signals=sum(1 for x in self.st["markets"].values() if x.get("eligible") for v in x["D"].values() if v == "pending"))
        self.save()
        return self.rows[-1]

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        with (self.out / "forward.jsonl").open("a") as f:
            for r in self.rows:
                f.write(json.dumps(r, default=str) + "\n")
        tmp = self.out / "state.json.tmp"
        tmp.write_text(json.dumps(self.st, default=str))
        os.replace(tmp, self.out / "state.json")


# ------------------------------------------------------------------------------------------------ self-test (no calls)
class FakeBudget:
    def __init__(self, now: float, S: float, cap: int = 10) -> None:
        self.cap = cap; self.used = 0; self.now = now; self.S = S

    @property
    def left(self) -> int:
        return self.cap - self.used

    def get(self, path: str) -> dict:
        if self.used >= self.cap:
            raise BudgetExhausted(path)
        self.used += 1
        S = self.S
        mk = {"ticker": "KXTEST-26DEC31", "event_ticker": "KXTEST-26", "market_type": "binary", "status": "active",
              "open_time": iso(S - 90 * 86400), "close_time": iso(S), "rules_primary": "If X happens before " + time.strftime("%b %d, %Y", time.gmtime(S)) + ", YES.",
              "yes_ask_dollars": "0.1500", "yes_bid_dollars": "0.1100", "volume_fp": "1000", "open_interest_fp": "500"}
        if path.startswith("/markets?series_ticker=KXFEDCHAIRNOM") or path.startswith("/markets?event_ticker="):
            return {"markets": [mk]}
        if path.startswith("/markets/KXTEST-26DEC31/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [["0.1000", "50"], ["0.1100", "20"]], "no_dollars": [["0.8400", "30"], ["0.8500", "12"]]}}
        if path.startswith("/markets/trades"):
            return {"trades": [{"created_time": iso(self.S - 60 * 86400 + 3600 + 8000), "yes_price_dollars": "0.1500", "count_fp": "10", "taker_side": "yes"}], "cursor": ""}
        return {"markets": []}


def selftest() -> None:
    d = Path(tempfile.mkdtemp())
    S = (int(time.time()) // 86400 + 61) * 86400        # a future 00:00 UTC deadline
    now = S - 60 * 86400 + 7200                          # D60 signal due 1 h ago
    lg = Logger(d, FakeBudget(now, S), now)
    r = lg.run()
    arms = lg.st["arms"]
    assert any(a["arm"] == "C1" and a["s"] == 0.14 for a in arms.values()), arms
    assert any(a["arm"] == "taker" and abs(a["px"] - 0.89) < 1e-9 for a in arms.values()), arms
    # move the clock past the C1 window: fill job must see the 0.15 print (> s = 0.14) as through
    lg2 = Logger(d, FakeBudget(now + 8 * 86400, S), now + 8 * 86400)
    lg2.run()
    f = [x for x in lg2.rows if x["type"] == "fill" and x["arm"] == "C1"]
    assert f and f[0]["f_through"] == 1.0, f
    print("selftest ok:", r["stage"], "C1 fill", f[0]["f_through"])


def main() -> None:
    a = sys.argv[1:]
    if "--selftest" in a:
        selftest(); return
    cap = MAX_CALLS
    if os.environ.get("R4LD_MAX_CALLS"):
        cap = min(cap, int(os.environ["R4LD_MAX_CALLS"]))
    if "--max-calls" in a:
        cap = min(cap, int(a[a.index("--max-calls") + 1]))
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / ".lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another pass is running"); return
        r = Logger(OUT, Budget(cap, tag="logger")).run()
        print(json.dumps(r, default=str))


if __name__ == "__main__":
    main()
