import csv
import json
from datetime import datetime, timezone
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


def timed_trade(index, count, strategy, start, end):
    fraction = index / (count - 1)
    stamp = start + (end - start) * fraction
    return {
        **trade(index, strategy, 1 if index % 3 == 0 else -0.4),
        "closed_at": stamp.isoformat().replace("+00:00", "Z"),
        "shadow_trade_id": f"{strategy}-{index}",
    }


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


def test_distinct_shadow_trade_ids_are_not_collapsed():
    first = {**trade(0), "shadow_trade_id": "shadow-1"}
    second = {**trade(0), "shadow_trade_id": "shadow-2"}
    rows, audit = wfv.prepare_trades([first, second])
    assert len(rows) == 2
    assert audit["skip_reasons"].get("duplicate", 0) == 0


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


def test_validation_builds_three_shared_time_windows_with_107_per_strategy(tmp_path):
    source, config = tmp_path / "trades.csv", tmp_path / "config.json"
    rows = [trade(i, "LIVE_BASELINE", 1 if i % 3 == 0 else -0.4) for i in range(107)]
    rows += [trade(i, "MOMENTUM_RELAXED", 1 if i % 2 == 0 else -0.5) for i in range(107)]
    write_csv(source, rows)
    write_config(config)
    report, windows = wfv.run_validation(data_path=source, config_path=config)
    assert report["configuration"]["possible_windows"] == 3
    assert report["candidate"]["windows"] == 3
    assert len(windows) == 6


def test_server_time_range_with_107_baseline_and_120_candidate_builds_three_windows(tmp_path):
    source, config = tmp_path / "trades.csv", tmp_path / "config.json"
    overlap_start = datetime(2026, 7, 27, 22, 28, 28, tzinfo=timezone.utc)
    overlap_end = datetime(2026, 7, 31, 7, 11, 30, tzinfo=timezone.utc)
    rows = [
        timed_trade(i, 107, "LIVE_BASELINE", overlap_start, overlap_end)
        for i in range(107)
    ] + [
        timed_trade(i, 120, "MOMENTUM_RELAXED", overlap_start, overlap_end)
        for i in range(120)
    ]
    write_csv(source, rows)
    write_config(config)

    report, window_rows = wfv.run_validation(
        data_path=source, config_path=config,
        train_size=60, test_size=15, step_size=15,
    )

    assert report["status"] != "INSUFFICIENT_WALK_FORWARD_WINDOWS"
    assert report["configuration"]["possible_windows"] == 3
    assert report["candidate"]["windows"] == 3
    assert len(window_rows) == 6
    assert {row["window_id"] for row in window_rows} == {1, 2, 3}
    candidate_windows = [
        row for row in window_rows if row["strategy"] == "MOMENTUM_RELAXED"
    ]
    for previous, current in zip(candidate_windows, candidate_windows[1:]):
        assert previous["test_end"] <= current["test_start"]


def test_shared_windows_have_equal_periods_and_no_leakage():
    baseline = prepared(107, "LIVE_BASELINE")
    candidate = prepared(107, "MOMENTUM_RELAXED")
    windows, _ = wfv.build_time_windows(baseline, candidate)
    rows, _, _ = wfv.evaluate_windows(baseline, candidate, windows)
    for window in windows:
        assert window.train_end == window.test_start
        assert window.test_start < window.test_end
    for window_id in range(1, 4):
        pair = [row for row in rows if row["window_id"] == window_id]
        assert len(pair) == 2
        assert pair[0]["test_start"] == pair[1]["test_start"]
        assert pair[0]["test_end"] == pair[1]["test_end"]


def test_exact_minimum_sample_includes_final_oos_trade():
    baseline = prepared(105, "LIVE_BASELINE")
    candidate = prepared(105, "MOMENTUM_RELAXED")
    windows, config = wfv.build_time_windows(baseline, candidate)
    rows, baseline_oos, candidate_oos = wfv.evaluate_windows(baseline, candidate, windows)
    assert config["possible_windows"] == 3
    assert len(rows) == 6
    assert len(baseline_oos) == len(candidate_oos) == 45


def test_alias_columns_and_invalid_rows_are_diagnosed():
    good = {
        "close_time": "2026-01-01T00:00:00Z", "strategy_name": "momentum relaxed",
        "result": "WIN", "pnl_r": "1.2", "symbol": "BTC/USDT", "side": "LONG",
    }
    rows, audit = wfv.prepare_trades([good, {**good, "close_time": "bad"}])
    assert rows[0]["strategy"] == "MOMENTUM_RELAXED"
    assert audit["closed_rows"] == 2
    assert audit["skip_reasons"] == {"invalid_timestamp": 1}
    assert audit["rows_by_strategy"] == {"MOMENTUM_RELAXED": 2}
    assert audit["valid_by_strategy"] == {"MOMENTUM_RELAXED": 1}


def test_insufficient_windows_include_actionable_reason(tmp_path):
    source, config = tmp_path / "trades.csv", tmp_path / "config.json"
    rows = [trade(i, "LIVE_BASELINE") for i in range(70)]
    rows += [trade(i, "MOMENTUM_RELAXED") for i in range(70)]
    write_csv(source, rows)
    write_config(config)
    report, windows = wfv.run_validation(data_path=source, config_path=config)
    assert report["status"] == "INSUFFICIENT_WALK_FORWARD_WINDOWS"
    assert "minimum per strategy=105" in report["reason"]
    assert windows == []


def test_dashboard_shows_reason_instead_of_zero_metrics(tmp_path):
    (tmp_path / "walk_forward_report.json").write_text(json.dumps({
        "status": "INSUFFICIENT_WALK_FORWARD_WINDOWS",
        "reason": "Only 0 shared windows; 3 are required.",
        "candidate": {"name": "Momentum Relaxed"},
    }), encoding="utf-8")
    loaded = load_walk_forward(tmp_path)
    assert loaded["oos_pf"] is None
    assert loaded["better_windows"] == "N/A"
    assert "3 are required" in loaded["reason"]


def test_dashboard_rejects_report_after_source_csv_changes(tmp_path):
    source, config = tmp_path / "trades.csv", tmp_path / "config.json"
    rows = [trade(i, "LIVE_BASELINE") for i in range(107)]
    rows += [trade(i, "MOMENTUM_RELAXED") for i in range(107)]
    write_csv(source, rows)
    write_config(config)
    report, windows = wfv.run_validation(data_path=source, config_path=config)
    wfv.save_outputs(
        report, windows, tmp_path / "walk_forward_report.json",
        tmp_path / "walk_forward_summary.txt", tmp_path / "walk_forward_windows.csv",
    )
    assert load_walk_forward(tmp_path)["status"] != "STALE_WALK_FORWARD_REPORT"

    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    loaded = load_walk_forward(tmp_path)
    assert loaded["status"] == "STALE_WALK_FORWARD_REPORT"
    assert loaded["oos_pf"] is None
    assert "explicitly" in loaded["reason"]
