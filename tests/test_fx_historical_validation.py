"""Offline regression coverage for FX historical validation."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fx_research.historical_data import HistoricalDataError, load_historical
from fx_research.historical_validation import (
    _close,
    _metric,
    _open_trade,
    build_report,
    main,
    replay,
)
from fx_research.provider import FXCandle

UTC = timezone.utc


def _rows(symbol: str, *, count: int = 60) -> list[dict[str, object]]:
    start = datetime(2026, 8, 17, tzinfo=UTC)  # Monday; fixture never reaches weekend.
    result = []
    for index in range(count):
        close = (1.10 if symbol == "EUR/USD" else 1.30) + (index - 12) * .001 if index >= 12 else (1.10 if symbol == "EUR/USD" else 1.30) + ((-1) ** index) * .001
        result.append({
            "timestamp": (start + timedelta(hours=index)).isoformat(), "open": close,
            "high": close + (.02 if index >= 15 else .0005), "low": close - .0005,
            "close": close, "volume": "" if index % 2 else None, "source": "deterministic_export",
        })
    return result


def _csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("timestamp", "open", "high", "low", "close", "volume", "source"))
        writer.writeheader(); writer.writerows(rows)


def _candle(*, at: datetime, high: float, low: float, close: float = 1.10) -> FXCandle:
    return FXCandle.from_mapping({"symbol": "EUR/USD", "timeframe": "1h", "candle_open_at": at.isoformat(),
                                  "open": close, "high": high, "low": low, "close": close,
                                  "volume": None, "source": "fixture", "fetched_at": at.isoformat()})


def _trade() -> dict[str, object]:
    stamp = datetime(2026, 8, 17, 14, tzinfo=UTC)
    snapshot = {"symbol": "EUR/USD", "timeframe": "1h", "candle_open_at": stamp.isoformat(),
                "current_price": 1.10, "atr": .01, "feature_snapshot_id": "fxfs-fixture"}
    return _open_trade(strategy_id="FX_TREND_CONFIRM", snapshot=snapshot, side="LONG", split="TRAIN", fold="FOLD_1")


def test_csv_loader_validates_missing_volume_and_weekend_gap(tmp_path: Path) -> None:
    rows = _rows("EUR/USD", count=2)
    rows[0]["timestamp"] = "2026-08-21T21:00:00+00:00"  # Friday 21:00
    rows[1]["timestamp"] = "2026-08-23T22:00:00+00:00"  # Sunday market-open
    source = tmp_path / "eurusd.csv"; _csv(source, rows)
    series = load_historical(source, symbol="EUR/USD")
    assert series.quality.candles_loaded == 2
    assert series.quality.gaps == 0 and series.quality.missing_volume_pct == 100.0


def test_json_loader_and_duplicate_or_invalid_ohlc_fail_closed(tmp_path: Path) -> None:
    rows = _rows("EUR/USD", count=2)
    source = tmp_path / "eurusd.json"; source.write_text(json.dumps({"candles": rows}), encoding="utf-8")
    assert len(load_historical(source, symbol="EUR/USD").candles) == 2
    rows[1]["timestamp"] = rows[0]["timestamp"]
    _csv(tmp_path / "duplicate.csv", rows)
    with pytest.raises(HistoricalDataError, match="duplicate"):
        load_historical(tmp_path / "duplicate.csv", symbol="EUR/USD")
    rows = _rows("EUR/USD", count=1); rows[0]["high"] = float(rows[0]["low"]) - .01
    _csv(tmp_path / "bad.csv", rows)
    with pytest.raises(HistoricalDataError, match="invalid"):
        load_historical(tmp_path / "bad.csv", symbol="EUR/USD")


def test_nonfinite_and_fake_weekend_candles_fail_closed(tmp_path: Path) -> None:
    rows = _rows("EUR/USD", count=1); rows[0]["close"] = "NaN"
    _csv(tmp_path / "nonfinite.csv", rows)
    with pytest.raises(HistoricalDataError) as nonfinite:
        load_historical(tmp_path / "nonfinite.csv", symbol="EUR/USD")
    assert nonfinite.value.quality.nonfinite_prices == 1
    rows = _rows("EUR/USD", count=1); rows[0]["timestamp"] = "2026-08-22T12:00:00+00:00"  # Saturday
    _csv(tmp_path / "weekend.csv", rows)
    with pytest.raises(HistoricalDataError, match="weekend"):
        load_historical(tmp_path / "weekend.csv", symbol="EUR/USD")


def test_replay_is_chronological_and_never_uses_future_candles(tmp_path: Path) -> None:
    path = tmp_path / "eurusd.csv"; _csv(path, _rows("EUR/USD", count=20))
    series = load_historical(path, symbol="EUR/USD")
    result = replay(series)
    accepted = [row for row in result["decisions"] if row["accepted"]]
    assert accepted and accepted[0]["timestamp"] == series.candles[14].candle_open_at.isoformat()
    assert all(row["entry_time"] < row["exit_time"] for row in result["outcomes"])
    extended = _rows("EUR/USD", count=21)
    extended[-1].update({"open": 9.99, "high": 10.0, "low": 9.98, "close": 9.99})  # Future must not affect earlier decision.
    _csv(tmp_path / "extended.csv", extended)
    extended_result = replay(load_historical(tmp_path / "extended.csv", symbol="EUR/USD"))
    assert [row for row in result["decisions"] if row["timestamp"] == accepted[0]["timestamp"]] == [
        row for row in extended_result["decisions"] if row["timestamp"] == accepted[0]["timestamp"]
    ]


def test_tp_sl_ambiguous_and_mfe_mae_semantics() -> None:
    stamp = datetime(2026, 8, 17, 15, tzinfo=UTC)
    take_profit = _close(_trade(), _candle(at=stamp, high=1.13, low=1.099))
    stop_loss = _close(_trade(), _candle(at=stamp, high=1.101, low=1.08))
    ambiguous = _close(_trade(), _candle(at=stamp, high=1.13, low=1.08))
    assert take_profit and take_profit["exit_reason"] == "TAKE_PROFIT" and take_profit["pnl_r"] == 2.0
    assert stop_loss and stop_loss["exit_reason"] == "STOP_LOSS" and stop_loss["pnl_r"] == -1.0
    assert ambiguous and ambiguous["exit_reason"] == "AMBIGUOUS_INTRABAR" and ambiguous["pnl_r"] is None
    assert float(take_profit["mfe_r"]) >= 2.0 and float(stop_loss["mae_r"]) <= -1.0


def test_splits_folds_metrics_and_bootstrap_are_deterministic(tmp_path: Path) -> None:
    eurusd, gbpusd = tmp_path / "eurusd.csv", tmp_path / "gbpusd.csv"
    _csv(eurusd, _rows("EUR/USD")); _csv(gbpusd, _rows("GBP/USD"))
    first = build_report(eurusd_path=eurusd, gbpusd_path=gbpusd, bootstrap_iterations=100, bootstrap_seed=7)
    second = build_report(eurusd_path=eurusd, gbpusd_path=gbpusd, bootstrap_iterations=100, bootstrap_seed=7)
    assert first["metrics"]["bootstrap"] == second["metrics"]["bootstrap"]
    assert {row["split"] for row in first["metrics"]["by_split"]} == {"TRAIN", "VALIDATION", "HOLDOUT"}
    assert {row["fold"] for row in first["metrics"]["by_fold"]} == {"FOLD_1", "FOLD_2", "FOLD_3", "FOLD_4"}
    assert first["metrics"]["full"]["ambiguous"] == 0
    assert all(row["strategy_id"] in {"FX_TREND_CONFIRM", "FX_RISK_CONSERVATIVE"} for row in first["metrics"]["by_strategy"])


def test_ambiguous_is_excluded_from_resolved_metrics(tmp_path: Path) -> None:
    eurusd, gbpusd = tmp_path / "eurusd.csv", tmp_path / "gbpusd.csv"
    rows = _rows("EUR/USD"); rows[15]["low"] = float(rows[15]["close"]) - .02
    _csv(eurusd, rows); _csv(gbpusd, _rows("GBP/USD"))
    report = build_report(eurusd_path=eurusd, gbpusd_path=gbpusd, bootstrap_iterations=10)
    assert report["metrics"]["full"]["ambiguous"] > 0
    assert report["metrics"]["full"]["trades"] < report["outcomes"]["closed_or_ambiguous"]


def test_metric_profit_factor_and_drawdown_are_r_based_and_ambiguous_safe() -> None:
    metrics = _metric([
        {"status": "CLOSED", "pnl_r": 2.0, "mfe_r": 2.0, "mae_r": -.2, "duration_seconds": 3600},
        {"status": "CLOSED", "pnl_r": -1.0, "mfe_r": .3, "mae_r": -1.0, "duration_seconds": 7200},
        {"status": "CLOSED", "pnl_r": -1.0, "mfe_r": .2, "mae_r": -1.0, "duration_seconds": 3600},
        {"status": "AMBIGUOUS_INTRABAR", "pnl_r": None, "mfe_r": 3.0, "mae_r": -2.0, "duration_seconds": 3600},
    ])
    assert metrics["trades"] == 3 and metrics["ambiguous"] == 1
    assert metrics["profit_factor"] == 1.0 and metrics["net_r"] == 0.0 and metrics["max_drawdown_r"] == -2.0


def test_default_execution_never_creates_runtime_or_crypto_artifacts(tmp_path: Path) -> None:
    eurusd, gbpusd = tmp_path / "eurusd.csv", tmp_path / "gbpusd.csv"
    _csv(eurusd, _rows("EUR/USD")); _csv(gbpusd, _rows("GBP/USD"))
    before = {path.name: path.read_bytes() for path in (eurusd, gbpusd)}
    report = build_report(eurusd_path=eurusd, gbpusd_path=gbpusd, bootstrap_iterations=10)
    assert report["asset_class"] == "FX"
    assert {path.name: path.read_bytes() for path in (eurusd, gbpusd)} == before
    assert not any((tmp_path / name).exists() for name in ("research.db", "fx_research.db", "fx_shadow_open.json", "research_lab_v2_shadow_open.json"))


def test_json_output_is_only_created_when_explicit(tmp_path: Path) -> None:
    eurusd, gbpusd, output = tmp_path / "eurusd.csv", tmp_path / "gbpusd.csv", tmp_path / "report.json"
    _csv(eurusd, _rows("EUR/USD")); _csv(gbpusd, _rows("GBP/USD"))
    assert main(["--eurusd", str(eurusd), "--gbpusd", str(gbpusd), "--bootstrap-iterations", "10"]) == 0
    assert not output.exists()
    assert main(["--eurusd", str(eurusd), "--gbpusd", str(gbpusd), "--bootstrap-iterations", "10", "--json-output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["mode"] == "OFFLINE_HISTORICAL_VALIDATION"
