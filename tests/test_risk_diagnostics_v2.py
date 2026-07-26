"""Regression coverage for read-only Risk Diagnostics v2."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from decision_diagnostics import DecisionDiagnostics, RiskDiagnostics
from telegram_bot_v4 import format_riskstats
from telegram_handlers import BOT_COMMANDS_V5


def _decision() -> SimpleNamespace:
    return SimpleNamespace(
        direction="LONG",
        signal="SETUP",
        score=25,
        long_total=25,
        short_total=5,
    )


def _engine(long: float = 10, short: float = 0) -> SimpleNamespace:
    return SimpleNamespace(long=long, short=short)


def test_risk_diagnostics_preserves_legacy_verdict_and_csv_fields(tmp_path: Path) -> None:
    risk = _engine(long=-5, short=-5)
    risk.diagnostic_values = {
        "atr_pct": 2.4,
        "atr_limit": 2.0,
        "price_position": 50,
        "price_zone_low": 35,
        "price_zone_high": 65,
        "risk_reward": 2.0,
        "risk_reward_required": 2.0,
        "stop_distance": 1.2,
        "stop_distance_required": "1 ATR",
        "position_size": 0.01,
        "position_size_limit": "<=0.01",
    }
    diagnostics = DecisionDiagnostics(
        symbol="BTC/USDT",
        csv_file=tmp_path / "decision_diagnostics.csv",
        report_file=tmp_path / "diagnostics_report.json",
        auto_log=False,
    )
    report = diagnostics.analyze(
        _decision(), _engine(), _engine(), _engine(), risk,
    )

    assert report["risk"] == "FAIL"
    assert report["risk_diagnostics"]["passed"] is False
    assert report["risk_diagnostics"]["fail_reason"] == "ATR_TOO_HIGH"
    assert isinstance(
        RiskDiagnostics(
            passed=report["risk_diagnostics"]["passed"],
            checks=[],
        ),
        RiskDiagnostics,
    )

    diagnostics.log(report)
    with diagnostics.csv_file.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    for field in (
        "risk_rr", "risk_rr_required", "risk_atr", "risk_atr_limit",
        "risk_stop_distance", "risk_position_size", "risk_volatility",
        "risk_fail_reason",
    ):
        assert field in row
    assert row["risk_fail_reason"] == "ATR_TOO_HIGH"

    aggregate = diagnostics.build_json_report()
    assert aggregate["risk_fail_reasons"] == {"ATR_TOO_HIGH": 1}
    saved = json.loads(diagnostics.report_file.read_text(encoding="utf-8"))
    assert saved["risk_fail_reasons"] == {"ATR_TOO_HIGH": 1}


def test_passing_legacy_risk_has_no_fail_reason() -> None:
    risk = _engine(long=15, short=-5)
    risk.diagnostic_values = {"atr_pct": 2.4, "atr_limit": 2.0}
    report = DecisionDiagnostics(auto_log=False).analyze(
        _decision(), _engine(), _engine(), _engine(), risk,
    )
    assert report["risk"] == "PASS"
    assert report["risk_diagnostics"]["passed"] is True
    assert report["risk_diagnostics"]["fail_reason"] == ""


def test_riskstats_command_is_registered() -> None:
    assert "riskstats" in {command.command for command in BOT_COMMANDS_V5}
    rendered = format_riskstats()
    assert rendered.startswith("Risk Engine Statistics")
