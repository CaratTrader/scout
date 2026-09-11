from datetime import datetime, timedelta, timezone

from scout.config import Settings
from scout.intel import dual_source_headlines, kalshi_gap, kalshi_prior, match_kalshi, market_kind
from scout.ledger import empty_ledger
from scout.risk import grok_dump, veto


def test_market_kind_and_kalshi_match():
    assert market_kind("Yankees vs Astros: Over 8.5 total runs") == "total"
    kalshi = [
        {
            "title": "Houston vs New York Y: Total Runs Over 8.5 runs scored",
            "yes_ask": 0.62,
            "kind": "total",
        }
    ]
    hit = match_kalshi("Astros vs Yankees Over 8.5 total runs?", kalshi)
    assert hit is not None
    assert abs(kalshi_gap(0.48, hit["yes_ask"]) - 0.14) < 1e-9


def test_dual_source_needs_two_origins():
    headlines = [
        {"title": "Red Sea shipping attacks close Bab el-Mandeb lane", "origin": "bbc"},
        {"title": "Bab el-Mandeb shipping attacks disrupt Red Sea traffic", "origin": "nyt"},
        {"title": "Unrelated local election story from one desk", "origin": "bbc"},
    ]
    confirmed = dual_source_headlines(headlines)
    assert any("shipping" in title.lower() or "bab" in title.lower() for title in confirmed)
    assert all("election" not in title.lower() for title in confirmed)


def test_kalshi_prior_requires_seven_cents():
    market = {"yes_ask": 0.50, "kalshi_yes": 0.54, "kalshi_gap": 0.04, "kalshi_title": "x"}
    assert kalshi_prior(market) is None
    market = {"yes_ask": 0.48, "kalshi_yes": 0.62, "kalshi_gap": 0.14, "kalshi_title": "HOU vs NYY total"}
    row = kalshi_prior(market)
    assert row is not None
    assert row["edge_type"] == "related_inconsistency"
    assert len(row["sources"]) >= 2


def test_kett_dumps_loser_inside_a_minute():
    settings = Settings(fast_dump_pct=0.10, fast_dump_seconds=90)
    opened = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    pos = {
        "side": "YES",
        "entry_price": 0.50,
        "reason": "grok edge=0.12",
        "opened_at": opened,
        "question": "Yankees vs Astros Over 8.5?",
        "slug": "mlb-total",
    }
    assert grok_dump(pos, 0.44, settings)[1] == "fast_dump"
    crypto = {**pos, "question": "Bitcoin Up or Down", "slug": "btc-updown-5m-1", "reason": "crypto_lag"}
    assert grok_dump(crypto, 0.20, settings) is None


def test_grok_veto_one_source():
    settings = Settings(live=False, max_fraction=0.5, min_trade=1, grok_min_sources=2)
    ledger = empty_ledger(settings)
    ledger["cash"] = 20
    ledger["risk_baseline_cash"] = 20
    ledger["last_equity"] = 20
    cand = {
        "id": "s1",
        "kind": "grok",
        "side": "YES",
        "stake": 1.2,
        "price": 0.40,
        "limit_price": 0.40,
        "edge": 0.12,
        "fair": 0.62,
        "edge_type": "stale_news",
        "already_priced": False,
        "days_to_end": 0.4,
        "sources": ["https://x.com/one"],
        "volume_24h": 8000,
    }
    assert veto(cand, ledger, settings) == "one_source"
    cand["kalshi_yes"] = 0.61
    assert veto(cand, ledger, settings) is None
