from strategy_diagnostics import build_report

def test_report_is_shadow_only_and_calculates_metrics():
    report = build_report([
        {"strategy_id": "TREND_PULLBACK", "status": "CLOSED", "pnl_r": "2", "holding_candles": "4", "exit_reason": "TAKE_PROFIT"},
        {"strategy_id": "TREND_PULLBACK", "status": "CLOSED", "pnl_r": "-1", "holding_candles": "2", "exit_reason": "STOP_LOSS"},
    ], [{"blocking_reasons": '["ADX_TOO_LOW"]'}])
    item = report["strategies"]["TREND_PULLBACK"]
    assert report["source"] == "RESEARCH_LAB_SHADOW_ONLY"
    assert item["metrics"]["net_r"] == 1
    assert item["average_holding_candles"] == 3
    assert report["decision_blocking_reasons"] == {"ADX_TOO_LOW": 1}
