from scout.performance import summarize_performance


def test_performance_includes_fees_latency_and_calibration():
    ledger = {
        "fills": [
            {
                "market_id": "1",
                "side": "YES",
                "stake": 4,
                "fee": 0.1,
                "cost_basis": 4.1,
                "reason": "crypto_lag edge=0.2",
            },
            {"market_id": "1", "side": "CLOSE_YES", "price": 1, "pnl": 1.9},
        ]
    }
    events = [
        {"event": "submit", "id": "1", "fair": 0.8, "signal_age_ms": 120},
        {"event": "execution_reject", "reason": "stale crypto signal: age=9"},
    ]
    report = summarize_performance(ledger, events)
    assert report["realized_pnl"] == 1.9
    assert report["recorded_entry_fees"] == 0.1
    assert report["calibration"] == {"resolved_predictions": 1, "brier": 0.04}
    assert report["execution"]["median_signal_age_ms"] == 120
    assert report["execution"]["rejects"] == {"stale crypto signal": 1}
