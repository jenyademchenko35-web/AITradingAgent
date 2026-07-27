import csv
import json
from pathlib import Path

import pytest

import walk_forward_validation as wfv
from ai_research_dashboard import AIResearchDashboard, load_walk_forward


def trade(index, strategy="LIVE_BASELINE", result=1.0, status="WIN", regime="bullish"):
    return {
        "closed_at": f"2026-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00",
        "symbol": "BTC/USDT", "direction": "LONG", "status": status,
        "pnl_r": str(result), "candidate_id": strategy, "market_regime": regime,
    }


def prepared(count=120, strategy="LIVE_BASELINE"):
    rows, _ = wfv.prepare_trades([
        trade(i, strategy, 1.0 if i % 3 == 0 else -0.4) for i in range(count)
    ])
    return rows


def write_csv(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def write_config(path: Path):
    path.write_text(json.dumps({
        "MOMENTUM_RELAXED": {
            "enabled": True, "shadow_only": True,
            "overrides": {"momentum_threshold_delta": -2},
        }
    }), encoding="utf-8")


def test_data_is_sorted_by_time():
    rows, _ = wfv.prepare_trades([trade(2), trade(0), trade(1)])
    assert [row["timestamp"] for row in rows] == sorted(row["timestamp"] for row in rows)


def test_future_data_does_not_enter_train():
    rows = prepared()
    windows, _ = wfv.build_windows(len(rows), 60, 15, 15)
    for window in windows:
        assert rows[window.train_end - 1]["_timestamp"] < rows[window.test_start]["_timestamp"]


def test_train_and_test_do_not_overlap():
    windows, _ = wfv.build_windows(120, 60, 15, 15)
    assert all(set(range(w.train_start, w.train_end)).isdisjoint(
        range(w.test_start, w.test_end)
    ) for w in windows)


def test_windows_are_created_correctly():
    windows, config = wfv.build_windows(120, 60, 15, 15)
    assert len(windows) == 4
    assert windows[0] == wfv.Window(1, 0, 60, 60, 75)
    assert config["step_size"] == 15


def test_duplicates_are_excluded():
    rows, audit = wfv.prepare_trades([trade(0), trade(0)])
    assert len(rows) == 1
    assert audit["skip_reasons"]["duplicate"] == 1


def test_open_trades_are_excluded():
    rows, audit = wfv.prepare_trades([trade(0, status="OPEN")])
    assert rows == []
    assert audit["skip_reasons"]["not_closed"] == 1


def test_profit_factor_is_correct():
    assert wfv.profit_factor([2, 1, -1, -0.5]) == pytest.approx(2.0)


def test_max_drawdown_is_correct():
    assert wfv.max_drawdown([2, -1, -2, 1]) == pytest.approx(3.0)


def test_empty_data_is_handled(tmp_path):
    source = tmp_path / "empty.csv"
    source.write_text("", encoding="utf-8")
    rows, audit = wfv.load_trades(source)
    assert rows == []
    assert audit["valid_closed_trades"] == 0


def test_insufficient_data_is_reported(tmp_path):
    source, config = tmp_path / "trades.csv", tmp_path / "config.json"
    write_csv(source, [trade(0)])
    write_config(config)
    report, _ = wfv.run_validation(data_path=source, config_path=config)
    assert report["status"] == "INSUFFICIENT_DATA"


def test_damaged_csv_does_not_crash(tmp_path):
    source = tmp_path / "bad.csv"
    source.write_bytes(b"\xff\xfe\x00")
    rows, audit = wfv.load_trades(source)
    assert rows == []
    assert "read_error" in audit


def test_missing_candidate_config_is_reported(tmp_path):
    source, config = tmp_path / "trades.csv", tmp_path / "config.json"
    write_csv(source, [trade(0)])
    config.write_text("{}", encoding="utf-8")
    report, _ = wfv.run_validation(data_path=source, config_path=config)
    assert report["status"] == "CANDIDATE_CONFIG_NOT_FOUND"


def test_baseline_is_not_modified():
    baseline = prepared()
    original = [dict(row) for row in baseline]
    wfv.calculate_metrics(baseline)
    assert baseline == original


def test_json_report_is_created(tmp_path):
    report = {"status": "INSUFFICIENT_DATA"}
    wfv.save_outputs(report, [], tmp_path / "r.json", tmp_path / "s.txt", tmp_path / "w.csv")
    assert json.loads((tmp_path / "r.json").read_text())["status"] == "INSUFFICIENT_DATA"


def test_windows_csv_is_created(tmp_path):
    wfv.save_outputs({}, [], tmp_path / "r.json", tmp_path / "s.txt", tmp_path / "w.csv")
    assert (tmp_path / "w.csv").read_text().startswith("window_id,")


def test_dashboard_survives_missing_and_damaged_report(tmp_path):
    assert load_walk_forward(tmp_path)["status"] == "NOT_RUN"
    (tmp_path / "walk_forward_report.json").write_text("{", encoding="utf-8")
    assert AIResearchDashboard(tmp_path).build_report()["walk_forward"]["status"] == "NOT_RUN"


def test_telegram_formatter_does_not_run_validation(monkeypatch):
    import telegram_bot_v4
    monkeypatch.setattr(telegram_bot_v4, "read_json", lambda path: {})
    monkeypatch.setattr(wfv, "run_validation", lambda **kwargs: pytest.fail("must not run"))
    assert "ещё не запускалась" in telegram_bot_v4.format_walkforward()


def test_ready_for_shadow_requires_pf_at_least_one():
    baseline = {"profit_factor": 0.5, "net_r": -5, "max_drawdown_r": 10}
    candidate = {
        "profit_factor": 0.9, "net_r": 2, "max_drawdown_r": 8,
        "windows": 5, "trades": 100, "profitable_windows": 4,
    }
    status = wfv.classify_status(
        baseline, candidate, {"better_windows_share": 0.8},
        {"top_1_window_profit_share": 0.2}, {},
    )
    assert status != "READY_FOR_SHADOW"


def test_profit_concentration_is_detected():
    stability, warnings = wfv.concentration_analysis(
        [{"net_r": 8}, {"net_r": 1}, {"net_r": 1}], 0.5,
    )
    assert stability["top_1_window_profit_share"] == pytest.approx(0.8)
    assert "RESULT_CONCENTRATED_IN_SINGLE_WINDOW" in warnings


def test_bootstrap_is_reproducible():
    candidate = prepared(80, "MOMENTUM_RELAXED")
    baseline = prepared(80)
    first = wfv.bootstrap_analysis(candidate, baseline, seed=17)
    second = wfv.bootstrap_analysis(candidate, baseline, seed=17)
    assert first == second
