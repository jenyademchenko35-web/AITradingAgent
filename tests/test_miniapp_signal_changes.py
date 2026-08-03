from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, repository, write_csv


def test_changes_report_real_deltas_transitions_and_three_cycles_ago(tmp_path):
    rows = [
        decision_row(timestamp="2026-08-03T10:00:00Z", cycle_id="0", signal="WAIT", confidence="80", score="20", blockers="WEAK_MOMENTUM"),
        decision_row(timestamp="2026-08-03T10:05:00Z", cycle_id="1", signal="WATCH", confidence="84", score="23", blockers="WEAK_MOMENTUM"),
        decision_row(timestamp="2026-08-03T10:10:00Z", cycle_id="2", signal="SETUP", confidence="88", score="25", blockers=""),
        decision_row(timestamp="2026-08-03T10:15:00Z", cycle_id="3", signal="SETUP", confidence="91", score="27", blockers="", confirmations="trend alignment"),
    ]
    write_csv(tmp_path / "decision_debug.csv", rows, DECISION_FIELDS)
    result = repository(tmp_path).signal_changes("BTCUSDT", "1h")
    assert result.status == "OK"
    assert result.deltas["confidence"] == 3
    assert result.three_cycles_ago["cycle_id"] == "0"
    assert result.blockers_removed == ()
    assert len(result.series) == 4


def test_changes_do_not_label_unknown_metric_direction(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row()], DECISION_FIELDS)
    result = repository(tmp_path).signal_changes("BTCUSDT", "1h")
    assert result.status == "INSUFFICIENT_HISTORY"
    assert not any("improved" in str(value).lower() or "worse" in str(value).lower() for value in result.model_dump().values())
