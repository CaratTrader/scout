"""Plug-in harness of the Kalshi lab paper trader: loading, shared Kalshi calls, the per-minute call budget, and the two
ported strategies (rain_n, weather mirror) run end to end on fake data. No network: kget and fetchers are faked, and
conftest points every data path at tmp_path."""
import datetime as dt
import json
import types

import pytest

from scout import kalshi_lab_paper as L
from scout.kalshi_lab_strategies import base, rain_n, weather_mirror

UTC = dt.timezone.utc

PLUGINS = '''
from scout.kalshi_lab_strategies.base import Strategy


class Buyer(Strategy):
    series = ("KXTEST",)
    poll_s = 60

    def decide(self, now, market_quotes, external_data):
        mk = market_quotes["KXTEST"] or {}
        return [{"ticker": t, "side": "YES", "max_price": 0.40, "why": "test"} for t in mk if t == self.params["ticker"]]


class Watcher(Strategy):
    series = ("KXTEST",)
    poll_s = 60

    def decide(self, now, market_quotes, external_data):
        self.notes.append(f"saw {len(market_quotes['KXTEST'] or {})}")
        return []


class Multi(Strategy):
    poll_s = 30

    def __init__(self, name, spec):
        super().__init__(name, spec)
        self.series = tuple(self.params["series"])

    def decide(self, now, market_quotes, external_data):
        return []
'''


@pytest.fixture
def plugdir(tmp_path, monkeypatch):
    pkg = tmp_path / "klabplugs"; pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "demo.py").write_text(PLUGINS)
    (pkg / "single.py").write_text("from scout.kalshi_lab_strategies.base import Strategy\n\n\nclass Only(Strategy):\n    pass\n")
    (pkg / "empty.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    return pkg


class FakeKalshi:
    """Records every Kalshi URL; serves open markets, order books and results from dicts."""

    def __init__(self, markets=None, books=None, results=None):
        self.urls, self.markets, self.books, self.results = [], markets or {}, books or {}, results or {}

    def __call__(self, url):
        self.urls.append(url)
        if url.endswith("/orderbook"):
            return {"orderbook_fp": self.books.get(url.split("/markets/")[1].split("/")[0], {})}
        if "/markets?" in url:
            return {"markets": self.markets.get(url.split("series_ticker=")[1].split("&")[0], [])}
        return {"market": {"result": self.results.get(url.rsplit("/", 1)[1], "")}}

    def count(self, part):
        return sum(part in u for u in self.urls)


def signals():
    return [json.loads(x) for x in L.SIGNALS.read_text().splitlines()] if L.SIGNALS.exists() else []


# ------------------------------------------------------------------ loading
def test_registry_entries_without_module_fall_back_to_their_family():
    assert type(L.load_plugin("rain_n15", {"family": "rain", "params": {"hour": 15}})) is rain_n.RainN
    assert type(L.load_plugin("weather", {"family": "weather", "params": {}})) is weather_mirror.WeatherMirror
    assert L.load_plugin("calib_x", {"family": "calib", "params": {}}) is None   # nightly-loop cells have no plug-in
    assert type(L.load_plugin("r", {"module": "rain_n", "params": {}})) is rain_n.RainN   # bare name: scout.kalshi_lab_strategies


def test_module_field_loads_dotted_names_and_named_classes(plugdir):
    s = L.load_plugin("solo", {"module": "klabplugs.single", "params": {"x": 1}})
    assert type(s).__name__ == "Only" and s.name == "solo" and s.params == {"x": 1}
    b = L.load_plugin("buy", {"module": "klabplugs.demo:Buyer", "params": {}})
    assert type(b).__name__ == "Buyer" and b.series == ("KXTEST",) and b.poll_s == 60
    with pytest.raises(ImportError, match="found 3"):
        L.load_plugin("x", {"module": "klabplugs.demo"})     # three classes, none chosen
    with pytest.raises(ImportError, match="found 0"):
        L.load_plugin("x", {"module": "klabplugs.empty"})
    with pytest.raises(ImportError, match="no plug-in module"):
        L.load_plugin("x", {"module": "no_such_plugin_xyz"})


def test_refresh_loads_active_entries_and_reports_a_broken_one_once(plugdir, capsys):
    L.REG.write_text(json.dumps({
        "rain_n15": {"family": "rain", "status": "monitor", "params": {"hour": 15, "cap": 0.97, "q_hat": 0.81}},
        "buy": {"module": "klabplugs.demo:Buyer", "status": "paper", "params": {"ticker": "T1", "q_hat": 0.9}},
        "old": {"module": "klabplugs.demo:Watcher", "status": "retired", "params": {}},
        "calib_c1": {"family": "calib", "status": "gate-pass", "params": {}},
        "broken": {"module": "no_such_plugin_xyz", "status": "paper", "params": {}},
    }))
    h = L.Harness(); h.refresh()
    assert sorted(h.strats) == ["buy", "rain_n15"]
    h.refresh()
    assert capsys.readouterr().out.count("broken plug-in error") == 1
    assert [json.loads(x)["event"] for x in L.JOURNAL.read_text().splitlines()] == ["plugin_error"]
    assert h.interval(h.strats["buy"]) == 60 and h.interval(h.strats["rain_n15"]) == L.POLL_S


# ------------------------------------------------------------------ shared calls and fills
def test_one_open_markets_call_per_series_and_order_books_only_for_signals(plugdir, monkeypatch):
    fake = FakeKalshi(markets={"KXTEST": [{"ticker": "T1", "close_time": "2099-01-01T00:00:00Z"}, {"ticker": "T2", "close_time": "2099-01-01T00:00:00Z"}]},
                      books={"T1": {"no_dollars": [["0.60", "100"], ["0.55", "40"]]}})
    monkeypatch.setattr(L, "kget", fake)
    L.REG.write_text(json.dumps({"buy": {"module": "klabplugs.demo:Buyer", "status": "paper", "params": {"ticker": "T1", "q_hat": 0.9}},
                                 "watch": {"module": "klabplugs.demo:Watcher", "status": "paper", "params": {}}}))
    h = L.Harness(); h.refresh()
    h.run(list(h.strats.values()), dt.datetime(2026, 10, 8, 19, 0, tzinfo=UTC))
    assert fake.count("/markets?series_ticker=KXTEST&status=open&limit=200") == 1
    assert fake.urls[1:] == [f"{L.K}/markets/T1/orderbook"]
    led = L.load("buy"); pos = led["positions"][0]
    assert pos["ticker"] == "T1" and pos["side"] == "YES" and pos["px"] == 0.40 and pos["close"] == "2099-01-01T00:00:00Z"
    assert pos["shares"] == round(L.stake_for({"cash": 50.0, "positions": []}, 0.40, 0.9) / 0.40, 2)
    rec = signals()
    assert len(rec) == 1 and rec[0]["strategy"] == "buy" and rec[0]["book"] == [[0.4, 100.0], [0.45, 40.0]]
    assert L.load("watch")["positions"] == [] and h.strats["watch"].notes == ["saw 2"]
    # after the close: settled on the Kalshi result, one market call
    led["positions"][0]["close"] = "2026-01-01T00:00:00Z"; L.save(led)
    fake.results["T1"] = "yes"; fake.urls.clear()
    h.run([h.strats["buy"]], dt.datetime(2026, 10, 8, 21, 0, tzinfo=UTC))
    led = L.load("buy")
    assert led["positions"] == [] and led["fills"][0]["won"] and led["fills"][0]["pnl"] > 0 and fake.urls[1:] == [f"{L.K}/markets/T1"]
    assert len(signals()) == 1 and led["signalled"] == ["T1|YES"]   # the repeated signal was dropped: no book, no log, no fill


def test_halted_bankroll_still_logs_the_unit_stake_signal(plugdir, monkeypatch):
    fake = FakeKalshi(markets={"KXTEST": [{"ticker": "T1"}]}, books={"T1": {"no_dollars": [["0.60", "100"]]}})
    monkeypatch.setattr(L, "kget", fake)
    L.REG.write_text(json.dumps({"buy": {"module": "klabplugs.demo:Buyer", "status": "paper", "params": {"ticker": "T1", "q_hat": 0.9}}}))
    led = L.load("buy"); led["streak"] = 3; L.save(led)
    h = L.Harness(); h.refresh(); h.run(list(h.strats.values()), dt.datetime(2026, 10, 8, 19, 0, tzinfo=UTC))
    led = L.load("buy")
    assert led["positions"] == [] and led["halted"] == "3 losses in a row" and len(signals()) == 1


# ------------------------------------------------------------------ call budget
class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


def max_in_window(stamps, w=60.0):
    return max(sum(1 for u in stamps if t <= u < t + w) for t in stamps)


def test_call_budget_allows_at_most_25_requests_in_any_minute():
    clk = Clock(); b = L.CallBudget(25, clock=clk.time); stamps = []
    for _ in range(80):
        clk.sleep(b.wait_s()); b.take(); stamps.append(clk.t)
    assert max_in_window(stamps) == 25 and stamps[25] - stamps[0] >= 60 and b.used() <= 25


def test_kget_paces_and_caps_kalshi_calls(monkeypatch):
    clk = Clock(); calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def urlopen(req, timeout=None):
        calls.append(clk.t); return Resp()

    monkeypatch.setattr(L, "time", types.SimpleNamespace(time=clk.time, sleep=clk.sleep, monotonic=clk.time))
    monkeypatch.setattr(L, "BUDGET", L.CallBudget(25, clock=clk.time))
    monkeypatch.setattr(L, "_LAST", [0.0])
    monkeypatch.setattr(L.urllib.request, "urlopen", urlopen)
    for _ in range(70):
        assert L.kget(f"{L.K}/markets/X") == {}
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert len(calls) == 70 and min(gaps) >= 1.2 - 1e-9 and max_in_window(calls) <= 25


def test_poll_intervals_stretch_when_planned_calls_exceed_the_budget_share(plugdir):
    many = {f"m{i}": {"module": "klabplugs.demo:Multi", "status": "paper", "params": {"series": [f"KXS{i}A", f"KXS{i}B", f"KXS{i}C", f"KXS{i}D"]}} for i in range(3)}
    L.REG.write_text(json.dumps(many))
    h = L.Harness(); h.refresh()
    assert h.planned_per_min() == 24.0                                   # 12 series every 30 s
    assert h.stretch == pytest.approx(24.0 / (L.PLAN_SHARE * L.MAX_CALLS_PER_MIN))
    assert h.planned_per_min() / h.stretch <= L.PLAN_SHARE * L.MAX_CALLS_PER_MIN + 1e-9
    assert h.interval(h.strats["m0"]) == pytest.approx(30 * h.stretch)
    # two strategies on the same series plan (and fetch) it once
    L.REG.write_text(json.dumps({"a": {"module": "klabplugs.demo:Multi", "status": "paper", "params": {"series": ["KXS"]}},
                                 "b": {"module": "klabplugs.demo:Multi", "status": "paper", "params": {"series": ["KXS"]}, "poll_s": 120}}))
    h.refresh()
    assert h.planned_per_min() == 2.0 and h.stretch == 1.0 and h.interval(h.strats["b"]) == 120


def test_strategies_with_the_same_interval_are_due_together():
    h = L.Harness(); h.refresh()   # default registry: rain_n15 + weather at KLAB_POLL_S
    assert {s.name for s in h.due(0.0)} == {"rain_n15", "weather"}
    h.next_due = {"rain_n15": 600.0, "weather": 600.0}
    assert h.due(599.0) == [] and len(h.due(600.0)) == 2


# ------------------------------------------------------------------ ported strategies
def _metar(t, raw):
    return {"obsTime": t.timestamp(), "rawOb": raw, "icaoId": raw[:4]}


def test_rain_plugin_end_to_end(monkeypatch):
    utc = UTC
    dry = [_metar(dt.datetime(2026, 10, 7, h, 53, tzinfo=utc), f"KATL 07{h:02d}53Z 18008KT 10SM BKN040 22/17 A3001 RMK AO2") for h in (15, 16, 17, 18)]
    asked = []

    def fake_metar(stations, hours=30):
        asked.append(stations); return {"ATL": dry}

    monkeypatch.setitem(base.FETCHERS, "metar", (fake_metar, 60))
    fake = FakeKalshi(markets={"KXRAIN": [{"ticker": "KXRAIN-26OCT07-ATL", "yes_bid_dollars": "0.2800", "close_time": "2099-01-01T05:00:00Z"},
                                          {"ticker": "KXRAIN-26OCT07-BOS", "yes_bid_dollars": "0.9900", "close_time": "2099-01-01T05:00:00Z"}]},
                      books={"KXRAIN-26OCT07-ATL": {"yes_dollars": [["0.28", "50"]]}})
    monkeypatch.setattr(L, "kget", fake)
    h = L.Harness(); h.refresh(); rain = h.strats["rain_n15"]
    h.run([rain], dt.datetime(2026, 10, 7, 19, 5, tzinfo=utc))   # 15:05 EDT: the eastern cities are due
    eastern = [c for c, s in rain_n.RAIN_STATIONS.items() if rain_n.TZ[s] == "America/New_York"]
    assert asked == [sorted(rain_n.RAIN_STATIONS[c] for c in eastern)]
    assert fake.urls == [f"{L.K}/markets?series_ticker=KXRAIN&status=open&limit=200", f"{L.K}/markets/KXRAIN-26OCT07-ATL/orderbook"]
    led = L.load("rain_n15")
    assert sorted(led["decided"]) == sorted(f"{c}:2026-10-07" for c in eastern)
    pos = led["positions"][0]
    assert (pos["ticker"], pos["side"], pos["px"], pos["close"]) == ("KXRAIN-26OCT07-ATL", "NO", 0.72, "2099-01-01T05:00:00Z")
    assert pos["shares"] == round(L.stake_for({"cash": 50.0, "positions": []}, 0.72, 0.81) / 0.72, 2)
    assert pos["why"] == "no measurable rain by 15:00, last 3 reports dry"
    rec = signals()
    assert len(rec) == 1 and list(rec[0]) == ["ts", "strategy", "ticker", "side", "px", "close", "book"] and rec[0]["book"] == [[0.72, 50.0]]
    notes = "; ".join(rain.notes)
    assert "ATL: NO @ 0.72 x3" in notes and "BOS: skip (too few reports)" in notes and "NYC: no open market" in notes
    # five minutes later everything due is decided: no Kalshi call, no METAR call
    fake.urls.clear(); asked.clear()
    h.run([rain], dt.datetime(2026, 10, 7, 19, 10, tzinfo=utc))
    assert fake.urls == [] and asked == [] and len(L.load("rain_n15")["positions"]) == 1


def test_rain_plugin_retries_when_data_is_missing(monkeypatch):
    monkeypatch.setitem(base.FETCHERS, "metar", (lambda stations, hours=30: (_ for _ in ()).throw(OSError("down")), 60))
    monkeypatch.setattr(L, "kget", FakeKalshi())
    h = L.Harness(); h.refresh()
    h.run([h.strats["rain_n15"]], dt.datetime(2026, 10, 7, 19, 5, tzinfo=UTC))
    assert L.load("rain_n15")["decided"] == []


def test_weather_mirror_end_to_end(monkeypatch):
    bot = {"positions": [{"ticker": "KXHIGHNY-26OCT08-B70.5", "side": "NO", "px": 0.85, "shares": 10.0, "opened": "2026-10-08T15:00:00+00:00",
                          "end_date": "2026-10-09T04:59:00Z", "why": "R0 dead bucket", "rule": "R0"}],
           "fills": [{"ticker": "KXHIGHCHI-26OCT07-T80", "side": "NO", "px": 0.80, "shares": 3.0, "opened": "2026-10-07T18:00:00+00:00", "end_date": "x",
                      "why": "R2m", "rule": "R2m", "won": True, "settled": "2026-10-08T06:00:00+00:00"},
                     {"ticker": "KXHIGHMIA-26OCT06-T90", "side": "NO", "px": 0.80, "shares": 3.0, "opened": "2026-10-06T18:00:00+00:00", "won": False}]}
    weather_mirror.SRC.write_text(json.dumps(bot))
    fake = FakeKalshi(); monkeypatch.setattr(L, "kget", fake)
    h = L.Harness(); h.refresh()
    h.run([h.strats["weather"]], dt.datetime(2026, 10, 8, 19, 0, tzinfo=UTC))
    led = L.load("weather")
    assert fake.urls == []                                                    # the mirror makes no Kalshi calls
    assert led["mirrored"] == ["KXHIGHNY-26OCT08-B70.5|NO", "KXHIGHCHI-26OCT07-T80|NO"]   # before START: ignored
    assert [p["ticker"] for p in led["positions"]] == ["KXHIGHNY-26OCT08-B70.5"]
    assert led["positions"][0]["opened"] == "2026-10-08T15:00:00+00:00" and led["positions"][0]["close"] == "2026-10-09T04:59:00Z"
    stake = L.stake_for({"cash": 50.0, "positions": []}, 0.85, 0.89)
    assert led["positions"][0]["shares"] == round(min(stake / 0.85, 10.0), 2)
    f = led["fills"][0]
    assert f["ticker"] == "KXHIGHCHI-26OCT07-T80" and f["won"] and f["settled"] == "2026-10-08T06:00:00+00:00" and f["shares"] == 3.0   # capped by the bot's 3 contracts
    assert [list(r) for r in signals()] == [["ts", "strategy", "ticker", "side", "px", "rule"]] * 2
    # the bot settles the NY position: the mirror settles it on the next poll without a Kalshi call
    bot["fills"].append({**bot["positions"].pop(), "won": False, "settled": "2026-10-09T06:00:00+00:00"})
    weather_mirror.SRC.write_text(json.dumps(bot))
    h.run([h.strats["weather"]], dt.datetime(2026, 10, 9, 7, 0, tzinfo=UTC))
    led = L.load("weather")
    assert led["positions"] == [] and [x["won"] for x in led["fills"]] == [True, False] and led["streak"] == 1 and fake.urls == []
