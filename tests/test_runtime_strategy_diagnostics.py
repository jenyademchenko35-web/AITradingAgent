from runtime_strategy_diagnostics import blocker_statistics, diagnostics, episodes, trade_quality, trade_result, validate_trade

def test_money_pnl_never_becomes_r():
    assert trade_result({"pnl": "-577"}) == (None, -577.0, "money")

def test_invalid_time_order_is_excluded():
    assert validate_trade({"opened_at":"2026-01-02T00:00:00+00:00", "closed_at":"2026-01-01T00:00:00+00:00"})[1] == "INVALID_TIME_ORDER"

def test_missing_reason_is_missing_telemetry_not_unknown():
    report=blocker_statistics([{"symbol":"BTC"}], [])
    assert report["missing_blocker_data"] == 1
    assert report["unknown_unmapped_value"] == 0

def test_repeated_signal_rows_are_one_episode():
    rows=[{"timestamp":"2026-01-01T00:00:00+00:00","symbol":"BTC","direction":"LONG","signal":"SETUP"},{"timestamp":"2026-01-01T00:05:00+00:00","symbol":"BTC","direction":"LONG","signal":"SETUP"},{"timestamp":"2026-01-01T00:10:00+00:00","symbol":"BTC","direction":"LONG","signal":"NO TRADE"}]
    assert episodes(rows, [])["summary"]["total_episodes"] == 1

def test_empty_sources_are_honest(tmp_path):
    report=diagnostics(tmp_path)
    assert report["feature_importance"]["status"] == "INSUFFICIENT_TELEMETRY"
    assert report["recommendations"]

def test_bars_unknown_without_timeframe_and_mfe_not_invented():
    valid=[{"row":{"symbol":"BTC","direction":"LONG","entry":"10","stop_loss":"9"},"opened":__import__("datetime").datetime(2026,1,1,tzinfo=__import__("datetime").timezone.utc),"closed":__import__("datetime").datetime(2026,1,1,1,tzinfo=__import__("datetime").timezone.utc),"pnl_r":None,"pnl_money":None,"metric_basis":"unknown"}]
    item=trade_quality(valid, [], [])["items"][0]
    assert item["bars_alive"] is None and item["mfe_status"] == "NO_PRICE_SNAPSHOTS"
