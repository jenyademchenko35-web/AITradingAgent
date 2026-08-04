import json

from impulse_learning_engine import run_once


def _row(symbol, probability, confirmed, regime="TREND", **extra):
    return {"symbol": symbol, "impulse_probability": probability, "confirmed": confirmed, "market_regime": regime, "adx": 28, "volume_ratio": 1.2, **extra}


def _write_history(tmp_path, rows):
    (tmp_path / "impulse_probability_history.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    (tmp_path / "impulse_accuracy.json").write_text(json.dumps({"status": "READY"}), encoding="utf-8")


def test_insufficient_data_is_honest_and_deterministic(tmp_path):
    _write_history(tmp_path, [_row("BTC", 80, True), _row("ETH", 30, False)])
    first = run_once(tmp_path)
    second = run_once(tmp_path)
    assert first["status"] == "INSUFFICIENT_DATA"
    assert first["training_samples"] == 2
    assert first["calibration"]["80-100"]["confirmation_rate"] == 100.0
    assert first["feature_learning"] == second["feature_learning"]
    assert json.loads((tmp_path / "impulse_learning_recommendations.json").read_text())["status"] == "INSUFFICIENT_DATA"


def test_learning_builds_calibration_symbol_regime_and_drift(tmp_path):
    rows = [_row("BTC", 85, True) for _ in range(15)] + [_row("ETH", 30, False, "RANGE") for _ in range(15)]
    _write_history(tmp_path, rows)
    report = run_once(tmp_path)
    assert report["status"] == "READY"
    assert report["calibration"]["80-100"] == {"predictions": 15, "confirmed": 15, "confirmation_rate": 100.0}
    assert report["symbol_learning"]["BTC"]["success_rate"] == 100.0
    assert report["regime_learning"]["RANGE"]["success_rate"] == 0.0
    assert report["learning_drift"]["status"] in {"STABLE", "DRIFT_DETECTED"}
    assert (tmp_path / "learning_drift.json").exists()


def test_unconfirmed_history_remains_pending_without_invented_rates(tmp_path):
    _write_history(tmp_path, [{"symbol": "BTC", "impulse_probability": 90, "market_regime": "TREND"}])
    report = run_once(tmp_path)
    assert report["training_samples"] == 0
    assert report["pending_samples"] == 1
    assert report["symbol_learning"] == {}
