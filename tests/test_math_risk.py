from scout.config import Settings
from scout.grok_fair import _parse_scores, grok_research_eligible
from scout.math_risk import completeness_edge, kelly_buy, parse_json_field, shares_for_stake, stake_usd
from scout.signals import attach_sizing, build_candidates, crypto_clip


def test_completeness_edge_flags_eight_percent():
    assert completeness_edge(0.45, 0.45) == 0.10
    assert completeness_edge(0.50, 0.50) == 0.0
    assert completeness_edge(0, 0.4) == 0.0


def test_kelly_and_cap():
    assert kelly_buy(0.70, 0.50) == 0.4
    assert stake_usd(50, 0.70, 0.50, kelly_mult=0.5, cap=0.06) == 3.0
    assert stake_usd(50, 0.40, 0.50) == 0.0
    assert shares_for_stake(3.0, 0.50) == 6.0


def test_parse_json_field():
    assert parse_json_field('["Yes", "No"]') == ["Yes", "No"]
    assert parse_json_field(["Yes", "No"]) == ["Yes", "No"]


def _market(**overrides):
    row = {
        "id": "1",
        "question": "Test?",
        "url": "https://example.com",
        "yes_ask": 0.40,
        "no_ask": 0.40,
        "yes_bid": 0.38,
        "no_bid": 0.38,
        "completeness_edge": 0.20,
        "volume_24h": 10_000,
        "days_to_end": 7,
    }
    row.update(overrides)
    return row


def test_build_candidates_completeness():
    settings = Settings(completeness_min_edge=0.02, grok_min_edge=0.08)
    rows = build_candidates([_market()], settings, grok_scores=None)
    assert len(rows) == 1
    assert rows[0]["kind"] == "completeness"
    assert rows[0]["side"] == "BOTH"


def test_grok_needs_confidence_and_edge():
    settings = Settings(completeness_min_edge=0.99, grok_min_edge=0.08, min_confidence=0.75)
    market = _market(yes_ask=0.40, no_ask=0.60, completeness_edge=0.0)
    weak = {"1": {"p_yes": 0.55, "confidence": 0.4, "thesis": "meh", "sources": []}}
    assert build_candidates([market], settings, grok_scores=weak) == []
    strong = {"1": {"p_yes": 0.45, "confidence": 0.9, "thesis": "ok", "sources": ["https://x.com"]}}
    assert build_candidates([market], settings, grok_scores=strong) == []
    hit = {
        "1": {
            "p_yes": 0.70,
            "confidence": 0.9,
            "already_priced": False,
            "edge_type": "stale_news",
            "thesis": "evidence",
            "sources": ["https://x.com"],
        }
    }
    rows = build_candidates([market], settings, grok_scores=hit)
    assert len(rows) == 1
    assert rows[0]["side"] == "YES"
    assert rows[0]["kind"] == "grok"
    assert rows[0]["limit_price"] == 0.40
    assert rows[0]["taker"] is True


def test_crypto_lag_skips_penny_corpse():
    settings = Settings(completeness_min_edge=0.99, crypto_min_edge=0.05, min_confidence=0.5)
    market = _market(yes_ask=0.99, no_ask=0.01, completeness_edge=0.0)
    scores = {
        "1": {
            "p_yes": 0.84,
            "confidence": 0.8,
            "already_priced": False,
            "edge_type": "crypto_lag",
            "thesis": "stale",
            "sources": [],
        }
    }
    assert build_candidates([market], settings, grok_scores=scores) == []
    mid = _market(yes_ask=0.48, no_ask=0.53, completeness_edge=0.0)
    scores["1"] = {**scores["1"], "p_yes": 0.70}
    assert build_candidates([mid], settings, grok_scores=scores) == []
    cheap = _market(yes_ask=0.18, no_ask=0.83, completeness_edge=0.0)
    scores["1"] = {**scores["1"], "p_yes": 0.45}
    assert build_candidates([cheap], settings, grok_scores=scores) == []
    lottery = _market(yes_ask=0.32, no_ask=0.2469, completeness_edge=0.0)
    scores["1"] = {**scores["1"], "p_yes": 0.22}
    assert build_candidates([lottery], settings, grok_scores=scores) == []
    fav = _market(yes_ask=0.88, no_ask=0.13, completeness_edge=0.0)
    scores["1"] = {**scores["1"], "p_yes": 0.94}
    rows = build_candidates([fav], settings, grok_scores=scores)
    assert rows and rows[0]["side"] == "YES"


def test_grok_research_skips_2028_dust():
    dust = _market(yes_ask=0.002, days_to_end=803)
    mid = _market(yes_ask=0.21, days_to_end=1.0)
    late = _market(yes_ask=0.21, days_to_end=5)
    assert grok_research_eligible(dust) is False
    assert grok_research_eligible(mid) is True
    assert grok_research_eligible(late) is False


def test_grok_research_skips_long_slams():
    slam = _market(
        yes_ask=0.16,
        days_to_end=17,
        question="Will Coco Gauff win the 2026 Women’s US Open?",
    )
    soon_slam = _market(
        yes_ask=0.22,
        days_to_end=3.3,
        question="Will Scottie Scheffler win the 2026 TOUR Championship?",
    )
    gossip = _market(yes_ask=0.14, days_to_end=3, question="Kristi Noem divorce by August 31?")
    bosa = _market(
        yes_ask=0.15,
        days_to_end=3.3,
        question="Will Joey Bosa play for Indianapolis Colts in 2026-27?",
    )
    soon = _market(yes_ask=0.42, days_to_end=1.2, question="Does the data print this week?")
    week = _market(yes_ask=0.31, days_to_end=11, question="Does the data print this week?")
    mlb = _market(
        yes_ask=0.48,
        days_to_end=0.4,
        question="Yankees vs Red Sox: Over 8.5 total runs?",
    )
    mlb_future = _market(
        yes_ask=0.48,
        days_to_end=5,
        question="Yankees vs Red Sox: Over 8.5 total runs?",
    )
    assert grok_research_eligible(slam) is False
    assert grok_research_eligible(soon_slam) is False
    assert grok_research_eligible(gossip) is False
    assert grok_research_eligible(bosa) is False
    assert grok_research_eligible(soon) is True
    assert grok_research_eligible(week) is False
    assert grok_research_eligible(mlb) is True
    assert grok_research_eligible(mlb_future) is False
    assert grok_research_eligible(mlb, survival=True) is True
    assert grok_research_eligible(soon, survival=True) is False


def test_parse_scores_structural_fields():
    text = '[{"id":"1","p_yes":0.60,"confidence":0.8,"already_priced":true,"edge_type":"stale_news","thesis":"x","sources":[]}]'
    row = _parse_scores(text)["1"]
    assert row["already_priced"] is True
    assert row["edge_type"] == "stale_news"
    assert row["confidence"] < 0.8  # round 0.60 gets a haircut


def test_crypto_clip_recovery_then_scales():
    s = Settings(max_crypto_stake=12, max_fraction=0.40)
    low = crypto_clip(45, 0.10, s, fair=0.82, price=0.78)
    high = crypto_clip(45, 0.10, s, fair=0.90, price=0.78)
    assert 0 < low < high <= 8.0
    assert high < crypto_clip(150, 0.10, s, fair=0.90, price=0.78)
    tiny = Settings(max_crypto_stake=8, max_fraction=0.50)
    assert 2.0 <= crypto_clip(7.6, 0.08, tiny, fair=0.88, price=0.72) <= 3.5


def test_sizing_skips_dust():
    settings = Settings(min_trade=1.0, max_fraction=0.06, kelly_mult=0.5)
    rows = attach_sizing(
        [{"side": "YES", "fair": 0.51, "price": 0.50, "kind": "grok", "edge": 0.01}],
        cash=50,
        settings=settings,
    )
    assert rows == []


def test_candidates_quote_crypto_before_grok():
    settings = Settings(completeness_min_edge=0.99, grok_min_edge=0.05, crypto_min_edge=0.05, min_confidence=0.5)
    crypto = _market(id="c", yes_ask=0.78, no_ask=0.23, completeness_edge=0.0)
    news = _market(id="g", yes_ask=0.40, no_ask=0.61, completeness_edge=0.0, days_to_end=2)
    scores = {
        "c": {
            "p_yes": 0.92,
            "confidence": 0.8,
            "already_priced": False,
            "edge_type": "crypto_lag",
            "thesis": "twap",
            "sources": [],
        },
        "g": {
            "p_yes": 0.90,
            "confidence": 0.9,
            "already_priced": False,
            "edge_type": "stale_news",
            "thesis": "fact",
            "sources": ["https://example.com"],
        },
    }
    rows = build_candidates([news, crypto], settings, grok_scores=scores)
    assert [row["kind"] for row in rows][0] == "crypto_lag"
    assert "grok" in {row["kind"] for row in rows}


def test_crypto_sizing_recovery_clip_at_45():
    settings = Settings(max_crypto_stake=12, max_fraction=0.4)
    rows = attach_sizing(
        [{"side": "YES", "fair": 0.90, "price": 0.78, "yes_ask": 0.78,
          "kind": "crypto_lag", "edge": 0.12}],
        cash=45,
        settings=settings,
    )
    assert 4 <= rows[0]["stake"] <= 8

def test_grok_clip_is_five_percent_once_book_is_alive():
    settings = Settings(max_grok_stake=4, grok_bankroll_frac=0.05, max_fraction=0.5, kelly_mult=1.0)
    rows = attach_sizing(
        [{"side": "YES", "fair": 0.80, "price": 0.50, "yes_ask": 0.50,
          "kind": "grok", "edge": 0.25}],
        cash=60,
        settings=settings,
    )
    assert rows
    assert rows[0]["stake"] <= 3.0 + 1e-9
