from runtime_strategy_diagnostics import diagnostics, episodes

def test_episodes_close_on_no_trade_and_missing_inputs_are_safe(tmp_path):
    assert episodes([], []) == []
    (tmp_path / "signals.csv").write_text("timestamp,symbol,direction,signal,reason\n2026-01-01T00:00:00+00:00,BTC,LONG,SETUP,\n2026-01-01T00:05:00+00:00,BTC,LONG,NO TRADE,Risk\n", encoding="utf-8")
    report = diagnostics(tmp_path)
    assert report["episodes"]["count"] == 1
    assert report["blockers"]["Risk"]["count"] == 1
    assert report["trade_quality"]["count"] == 0
