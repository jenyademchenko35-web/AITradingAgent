"""Tests for immutable, offline frozen FX OOS validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fx_research import frozen_oos_validation
from fx_research.frozen_oos_validation import (
    FROZEN_OOS_CUTOFF,
    analyze_outcomes,
    build_report,
    main,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _outcome(
    *,
    strategy: str = "FX_RISK_CONSERVATIVE",
    session: str = "LONDON",
    side: str = "LONG",
    symbol: str = "EUR/USD",
    pnl: float | None = 1.0,
    status: str = "CLOSED",
    entry: str = "2026-08-20T01:00:00+00:00",
    exit: str = "2026-08-20T02:00:00+00:00",
    index: int = 0,
) -> dict[str, object]:
    return {
        "strategy_id": strategy,
        "symbol": symbol,
        "side": side,
        "status": status,
        "pnl_r": pnl,
        "entry_time": entry,
        "exit_time": exit,
        "mfe_r": 2.0,
        "mae_r": -1.0,
        "duration_seconds": 3600,
        "feature_snapshot": {"session": session, "candle_open_at": entry, "feature_snapshot_id": f"f-{index}"},
    }


def _population(*, pnl: list[float], session: str = "LONDON", side: str = "LONG", count: int = 100) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(count):
        day, hour = 20 + index // 24, index % 24
        entry = f"2026-08-{day:02d}T{hour:02d}:00:00+00:00"
        exit_day, exit_hour = day + (hour == 23), (hour + 1) % 24
        exit = f"2026-08-{exit_day:02d}T{exit_hour:02d}:00:00+00:00"
        rows.append(_outcome(
            symbol="EUR/USD" if index % 2 == 0 else "GBP/USD",
            session=session,
            side=side,
            pnl=pnl[index % len(pnl)],
            entry=entry,
            exit=exit,
            index=index,
        ))
    return rows


def _hypothesis(report: dict[str, object], hypothesis_id: str) -> dict[str, object]:
    return next(item for item in report["hypotheses"] if item["hypothesis_id"] == hypothesis_id)  # type: ignore[index, return-value]


def test_cutoff_is_immutable_and_pre_cutoff_outcomes_do_not_count() -> None:
    assert FROZEN_OOS_CUTOFF.isoformat() == "2026-08-20T00:00:00+00:00"
    report = analyze_outcomes([_outcome(entry="2026-08-19T23:00:00+00:00", exit="2026-08-20T00:00:00+00:00")])
    assert report["overall"] == "WAITING_FOR_OOS_DATA"  # type: ignore[index]
    assert _hypothesis(report, "FX-DIAG-V2-01")["metrics"]["resolved"] == 0  # type: ignore[index]


def test_exact_pre_registered_conditions_and_ambiguous_exclusion() -> None:
    rows = [
        _outcome(session="LONDON", side="LONG"),
        _outcome(session="OVERLAP", side="SHORT", symbol="GBP/USD", index=1),
        _outcome(session="OVERLAP", side="LONG", index=2),
        _outcome(strategy="FX_TREND_CONFIRM", session="LONDON", index=3),
        _outcome(session="LONDON", status="AMBIGUOUS_INTRABAR", pnl=None, index=4),
    ]
    report = analyze_outcomes(rows)
    london, overlap = _hypothesis(report, "FX-DIAG-V2-01"), _hypothesis(report, "FX-DIAG-V2-02")
    assert london["metrics"]["total_candidate_outcomes"] == 2 and london["metrics"]["resolved"] == 1 and london["metrics"]["ambiguous"] == 1  # type: ignore[index]
    assert london["metrics"]["mfe_r"] == {"mean": 2.0, "median": 2.0} and london["metrics"]["duration_seconds"] == {"mean": 3600, "median": 3600}  # type: ignore[index]
    assert overlap["metrics"]["total_candidate_outcomes"] == 1 and overlap["metrics"]["resolved"] == 1  # type: ignore[index]


def test_staged_sample_gates_and_deterministic_bootstrap() -> None:
    waiting = analyze_outcomes(_population(pnl=[1.0, -1.0], count=49), bootstrap_iterations=100, bootstrap_seed=7)
    early = analyze_outcomes(_population(pnl=[1.0, -1.0], count=50), bootstrap_iterations=100, bootstrap_seed=7)
    first = analyze_outcomes(_population(pnl=[1.0, -1.0], count=100), bootstrap_iterations=100, bootstrap_seed=7)
    second = analyze_outcomes(_population(pnl=[1.0, -1.0], count=100), bootstrap_iterations=100, bootstrap_seed=7)
    assert _hypothesis(waiting, "FX-DIAG-V2-01")["verdict"] == "WAITING_FOR_SAMPLE"  # type: ignore[index]
    assert _hypothesis(early, "FX-DIAG-V2-01")["verdict"] == "EARLY_SIGNAL"  # type: ignore[index]
    assert _hypothesis(first, "FX-DIAG-V2-01")["verdict"] == "INCONCLUSIVE"  # type: ignore[index]
    assert _hypothesis(first, "FX-DIAG-V2-01")["bootstrap"] == _hypothesis(second, "FX-DIAG-V2-01")["bootstrap"]  # type: ignore[index]


def test_pass_fail_and_per_symbol_gates() -> None:
    passed = analyze_outcomes(_population(pnl=[1.0, 1.0, -1.0]), bootstrap_iterations=100, bootstrap_seed=1)
    failed = analyze_outcomes(_population(pnl=[-1.0], count=100), bootstrap_iterations=100, bootstrap_seed=1)
    one_symbol = _population(pnl=[1.0, 1.0, -1.0])
    for row in one_symbol:
        row["symbol"] = "EUR/USD"
    inconclusive = analyze_outcomes(one_symbol, bootstrap_iterations=100, bootstrap_seed=1)
    assert _hypothesis(passed, "FX-DIAG-V2-01")["verdict"] == "PASS"  # type: ignore[index]
    assert _hypothesis(failed, "FX-DIAG-V2-01")["verdict"] == "FAIL"  # type: ignore[index]
    assert _hypothesis(inconclusive, "FX-DIAG-V2-01")["verdict"] == "INCONCLUSIVE"  # type: ignore[index]


def test_invalid_input_and_future_leakage_anomaly_are_data_invalid() -> None:
    bad = _outcome(entry="2026-08-20T01:00:00+00:00", exit="2026-08-20T00:00:00+00:00")
    report = analyze_outcomes([bad])
    assert report["overall"] == "DATA_INVALID"  # type: ignore[index]
    assert _hypothesis(report, "FX-DIAG-V2-01")["verdict"] == "DATA_INVALID"  # type: ignore[index]


def test_current_pre_cutoff_canonical_input_waits_and_json_is_opt_in(tmp_path: Path, monkeypatch) -> None:
    header = "timestamp,open,high,low,close,volume,source,symbol,timeframe\n"
    row = "2026-08-19T23:00:00+00:00,1.1,1.2,1.0,1.15,,TEST,EUR/USD,1h\n"
    eurusd, gbpusd = tmp_path / "eur.csv", tmp_path / "gbp.csv"
    eurusd.write_text(header + row, encoding="utf-8")
    gbpusd.write_text((header + row).replace("EUR/USD", "GBP/USD"), encoding="utf-8")
    report = build_report(eurusd_path=eurusd, gbpusd_path=gbpusd)
    assert report["overall"] == "WAITING_FOR_OOS_DATA"
    assert all(item["metrics"]["resolved"] == 0 for item in report["hypotheses"])  # type: ignore[index]
    output = tmp_path / "report.json"
    monkeypatch.setattr(frozen_oos_validation, "build_report", lambda **_: report)
    assert main(["--eurusd", str(eurusd), "--gbpusd", str(gbpusd)]) == 0
    assert not output.exists()
    assert main(["--eurusd", str(eurusd), "--gbpusd", str(gbpusd), "--json-output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["overall"] == "WAITING_FOR_OOS_DATA"


def test_invalid_canonical_schema_is_data_invalid(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.csv"
    invalid.write_text("timestamp,open,high,low,close,source,symbol,timeframe\n2026-08-20T00:00:00+00:00,1,1,1,1,,EUR/USD,1h\n", encoding="utf-8")
    report = build_report(eurusd_path=invalid, gbpusd_path=invalid)
    assert report["overall"] == "DATA_INVALID"
    assert all(item["verdict"] == "DATA_INVALID" for item in report["hypotheses"])  # type: ignore[index]


def test_module_help_executes_argparse_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "fx_research.frozen_oos_validation", "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout and "--eurusd" in result.stdout and "--gbpusd" in result.stdout


def test_module_cli_writes_explicit_json_for_pre_cutoff_inputs(tmp_path: Path) -> None:
    header = "timestamp,open,high,low,close,volume,source,symbol,timeframe\n"
    row = "2026-08-19T23:00:00+00:00,1.1,1.2,1.0,1.15,,TEST,EUR/USD,1h\n"
    eurusd, gbpusd, output = tmp_path / "eur.csv", tmp_path / "gbp.csv", tmp_path / "report.json"
    eurusd.write_text(header + row, encoding="utf-8")
    gbpusd.write_text((header + row).replace("EUR/USD", "GBP/USD"), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "fx_research.frozen_oos_validation",
            "--eurusd",
            str(eurusd),
            "--gbpusd",
            str(gbpusd),
            "--json-output",
            str(output),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "FX FROZEN OOS VALIDATION" in result.stdout and "Overall: WAITING_FOR_OOS_DATA" in result.stdout
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["overall"] == "WAITING_FOR_OOS_DATA"
    assert _hypothesis(report, "FX-DIAG-V2-01")["verdict"] == "WAITING_FOR_SAMPLE"
    assert _hypothesis(report, "FX-DIAG-V2-02")["verdict"] == "WAITING_FOR_SAMPLE"
