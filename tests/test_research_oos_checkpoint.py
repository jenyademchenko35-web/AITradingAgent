"""Behavioral tests for the unified read-only Crypto + FX OOS checkpoint."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import research_oos_checkpoint as checkpoint
from research_lab_v2.oos_cutoff_registry import (
    CRYPTO_OOS_CUTOFF_ID,
    get_frozen_oos_cutoff,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CUTOFF = get_frozen_oos_cutoff().isoformat()


def _crypto(*, eligible: int = 0, overall: str = "WAITING_FOR_OOS_SAMPLE",
            status: str = "WAITING_FOR_SAMPLE") -> dict[str, object]:
    return {
        "cutoff": CUTOFF,
        "cutoff_id": CRYPTO_OOS_CUTOFF_ID,
        "analysis_population": {"eligible_closed_resolved_complete_finite": eligible},
        "oos_verdict": overall,
        "oos_integrity": {
            "duplicate_shadow_trade_id": 0, "invalid_feature_evidence": 0,
            "nonfinite_pnl_r": 0,
        },
        "hypotheses": [{"id": "H1", "status": status}],
    }


def _fx(*, overall: str = "WAITING_FOR_OOS_DATA",
        verdict: str = "WAITING_FOR_SAMPLE", valid: bool = True) -> dict[str, object]:
    return {
        "cutoff": CUTOFF,
        "overall": overall,
        "integrity": {"valid": valid, "issues": [] if valid else ["invalid canonical input"]},
        "hypotheses": [{
            "hypothesis_id": "FX-DIAG-V2-01", "verdict": verdict,
            "metrics": {"resolved": 2, "profit_factor": 0.0, "net_r": -2.0, "expectancy_r": -1.0},
        }],
    }


def _build(**kwargs: object) -> dict[str, object]:
    return checkpoint.build_checkpoint(
        crypto_db="unused.db", crypto_history="unused.csv",
        eurusd_path="eur.csv", gbpusd_path="gbp.csv", **kwargs,
    )


def test_waiting_checkpoint_uses_existing_crypto_and_fx_reports() -> None:
    calls: list[str] = []

    def crypto_builder(**kwargs: object) -> dict[str, object]:
        calls.append("crypto")
        assert "cutoff" not in kwargs
        return _crypto()

    def fx_builder(**_kwargs: object) -> dict[str, object]:
        calls.append("fx")
        return _fx()

    report = _build(crypto_builder=crypto_builder, fx_builder=fx_builder)
    assert calls == ["crypto", "fx"]
    assert report["crypto"]["eligible_oos_outcomes"] == 0  # type: ignore[index]
    assert report["crypto"]["cutoff"] == CUTOFF  # type: ignore[index]
    assert report["crypto"]["cutoff_id"] == CRYPTO_OOS_CUTOFF_ID  # type: ignore[index]
    assert report["checkpoint"] == {  # type: ignore[index]
        "crypto_sample": "INSUFFICIENT", "fx_sample": "INSUFFICIENT",
        "integrity": "PASS", "action": "COLLECT_MORE_OOS_DATA",
    }


def test_evidence_present_reports_conservative_review_only_action() -> None:
    report = _build(
        crypto_builder=lambda **_: _crypto(eligible=25, overall="OOS_EARLY_SIGNAL", status="SUPPORTED_EARLY"),
        fx_builder=lambda **_: _fx(overall="OOS_EVIDENCE_AVAILABLE", verdict="PASS"),
    )
    assert report["checkpoint"]["action"] == "OOS_EVIDENCE_READY_FOR_REVIEW"  # type: ignore[index]
    assert "PROMOTE" not in json.dumps(report) and "TRADE" not in json.dumps(report)


def test_fx_data_invalid_is_preserved_and_degrades_checkpoint() -> None:
    report = _build(
        crypto_builder=lambda **_: _crypto(),
        fx_builder=lambda **_: _fx(overall="DATA_INVALID", verdict="DATA_INVALID", valid=False),
    )
    assert report["fx"]["overall"] == "DATA_INVALID"  # type: ignore[index]
    assert report["checkpoint"]["integrity"] == "DEGRADED"  # type: ignore[index]
    assert report["checkpoint"]["action"] == "REVIEW_DATA_INTEGRITY"  # type: ignore[index]


def test_crypto_unavailable_does_not_hide_fx_report() -> None:
    report = _build(
        crypto_builder=lambda **_: (_ for _ in ()).throw(OSError("database unavailable")),
        fx_builder=lambda **_: _fx(),
    )
    assert report["crypto"]["overall"] == "UNAVAILABLE"  # type: ignore[index]
    assert report["fx"]["hypotheses"][0]["resolved"] == 2  # type: ignore[index]
    assert report["checkpoint"]["action"] == "REVIEW_DATA_INTEGRITY"  # type: ignore[index]


def test_checkpoint_crypto_cutoff_cannot_move_when_database_grows(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"
    columns = (
        "shadow_trade_id, strategy_id, symbol, timeframe, side, entry_time, exit_time, "
        "status, pnl_r, mfe_r, mae_r, feature_snapshot_json, join_status, data_quality, "
        "outcome_id, feature_snapshot_id, signal_id, decision_id, strategy_version, attribution_version"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE shadow_trade_outcomes ({columns})")
        for index, entry in enumerate((get_frozen_oos_cutoff(), get_frozen_oos_cutoff() + timedelta(days=30))):
            connection.execute(
                "INSERT INTO shadow_trade_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"id-{index}", "RISK_CONSERVATIVE", "BTC/USDT", "1h", "LONG", entry.isoformat(),
                 (entry + timedelta(hours=1)).isoformat(), "CLOSED", 1.0, 1.0, -1.0, "{}",
                 "RESOLVED", "COMPLETE", f"out-{index}", f"feature-{index}", f"signal-{index}",
                 f"decision-{index}", "v1", "attribution_chain_v1"),
            )
    history.write_text("shadow_trade_id\n", encoding="utf-8")
    common = {
        "crypto_db": database, "crypto_history": history, "eurusd_path": "unused-eur.csv",
        "gbpusd_path": "unused-gbp.csv", "fx_builder": lambda **_: _fx(),
    }
    first = checkpoint.build_checkpoint(**common)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE shadow_trade_outcomes SET entry_time=? WHERE shadow_trade_id='id-1'", ((get_frozen_oos_cutoff() + timedelta(days=60)).isoformat(),))
    second = checkpoint.build_checkpoint(**common)
    assert first["crypto"]["cutoff"] == second["crypto"]["cutoff"] == CUTOFF  # type: ignore[index]
    assert first["crypto"]["cutoff_id"] == second["crypto"]["cutoff_id"] == CRYPTO_OOS_CUTOFF_ID  # type: ignore[index]


def test_compact_stdout_excludes_raw_evidence(capsys) -> None:
    report = _build(crypto_builder=lambda **_: _crypto(), fx_builder=lambda **_: _fx())
    checkpoint.print_checkpoint(report)
    output = capsys.readouterr().out
    assert "OOS RESEARCH CHECKPOINT" in output and "FX-DIAG-V2-01" in output
    assert "feature_snapshot" not in output and "raw outcomes" not in output
    assert len(output.splitlines()) < 35


def test_cli_writes_only_explicit_json_output(tmp_path: Path, monkeypatch) -> None:
    report = _build(crypto_builder=lambda **_: _crypto(), fx_builder=lambda **_: _fx())
    monkeypatch.setattr(checkpoint, "build_checkpoint", lambda **_: report)
    output = tmp_path / "checkpoint.json"
    base = [
        "--eurusd", "eur.csv", "--gbpusd", "gbp.csv",
    ]
    assert checkpoint.main(base) == 0
    assert not output.exists()
    assert checkpoint.main([*base, "--json-output", str(output)]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["checkpoint"]["action"] == "COLLECT_MORE_OOS_DATA"
    assert saved["crypto"]["cutoff"] == CUTOFF
    assert saved["crypto"]["cutoff_id"] == CRYPTO_OOS_CUTOFF_ID


def test_module_help_runs_without_creating_output() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "research_oos_checkpoint", "--help"],
        cwd=PROJECT_ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert "--crypto-cutoff" not in result.stdout and "--eurusd" in result.stdout
