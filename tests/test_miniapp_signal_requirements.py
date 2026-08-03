from tests.miniapp_intelligence_support import DECISION_FIELDS, decision_row, repository, write_csv


def test_requirements_only_include_exact_saved_thresholds(tmp_path):
    row = decision_row(
        signal="NO TRADE", requirements_missing="ADX:17<20;Risk/Reward:1.3<2.0",
        blockers="MOMENTUM:18<25;UNKNOWN_BLOCKER", entry="", stop_loss="", take_profit="",
    )
    write_csv(tmp_path / "decision_debug.csv", [row], DECISION_FIELDS)
    result = repository(tmp_path).signal_requirements("BTCUSDT", "1h")
    assert result.status == "OK"
    values = {(item.metric, item.current_value, item.required_value) for item in result.items}
    assert ("ADX", 17, 20) in values
    assert ("RISK/REWARD", 1.3, 2.0) in values
    assert ("MOMENTUM", 18, 25) in values
    assert all(item.source == "SAVED_EVIDENCE" for item in result.items)
    assert "UNKNOWN_BLOCKER" not in {item.metric for item in result.items}


def test_unknown_threshold_is_not_invented(tmp_path):
    write_csv(tmp_path / "decision_debug.csv", [decision_row(signal="WAIT", blockers="WEAK_ADX")], DECISION_FIELDS)
    result = repository(tmp_path).signal_requirements("BTCUSDT", "1h")
    assert result.status == "NO_KNOWN_REQUIREMENTS" and result.items == ()


def test_exact_snapshot_threshold_is_used_when_saved(tmp_path):
    write_csv(
        tmp_path / "decision_debug.csv",
        [decision_row(signal="WAIT", adx="17", required_adx="20")],
        DECISION_FIELDS,
    )
    result = repository(tmp_path).signal_requirements("BTCUSDT", "1h")
    assert len(result.items) == 1
    assert result.items[0].model_dump() == {
        "metric": "ADX", "current_value": 17.0, "required_value": 20.0,
        "comparison": ">=", "status": "NOT_MET",
        "source": "SNAPSHOT_FIELD:required_adx",
    }
