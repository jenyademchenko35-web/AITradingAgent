"""Read-only fixtures for pre-registered RL2 out-of-sample validation."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research_lab_v2.oos_cutoff_registry import (
    CRYPTO_OOS_CUTOFF_ID,
    get_frozen_oos_cutoff,
)
from research_lab_v2.oos_validation import (
    FROZEN_IS_BENCHMARK,
    build_oos_report,
    freeze_cutoff,
    main,
)

UTC = timezone.utc
CUTOFF = "2026-08-20T09:12:56.327701+00:00"


def _db(path: Path, rows: list[dict[str, object]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE shadow_trade_outcomes (
            shadow_trade_id TEXT, strategy_id TEXT, symbol TEXT, timeframe TEXT, side TEXT,
            entry_time TEXT, exit_time TEXT, status TEXT, pnl_r REAL, mfe_r REAL, mae_r REAL,
            feature_snapshot_json TEXT, join_status TEXT, data_quality TEXT, outcome_id TEXT,
            feature_snapshot_id TEXT, signal_id TEXT, decision_id TEXT, strategy_version TEXT,
            attribution_version TEXT
        )""")
        for row in rows:
            connection.execute("INSERT INTO shadow_trade_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row[key] for key in (
                "shadow_trade_id", "strategy_id", "symbol", "timeframe", "side", "entry_time", "exit_time",
                "status", "pnl_r", "mfe_r", "mae_r", "feature_snapshot_json", "join_status", "data_quality",
                "outcome_id", "feature_snapshot_id", "signal_id", "decision_id", "strategy_version", "attribution_version",
            )))


def _row(index: int, *, entry: datetime, strategy: str = "RISK_CONSERVATIVE", side: str = "LONG",
         pnl: float = 1.0, status: str = "CLOSED", resolved: str = "RESOLVED", quality: str = "COMPLETE",
         symbol: str = "BTC/USDT", regime: str = "RANGE", session: str = "OVERLAP") -> dict[str, object]:
    return {
        "shadow_trade_id": f"rl2-oos-{index}", "strategy_id": strategy, "symbol": symbol, "timeframe": "1h", "side": side,
        "entry_time": entry.isoformat(), "exit_time": (entry + timedelta(hours=1)).isoformat(), "status": status,
        "pnl_r": pnl, "mfe_r": max(pnl, 0.2), "mae_r": min(pnl, -0.1),
        "feature_snapshot_json": json.dumps({"market_regime": regime, "session": session}), "join_status": resolved,
        "data_quality": quality, "outcome_id": f"out-{index}", "feature_snapshot_id": f"fs-{index}",
        "signal_id": f"sig-{index}", "decision_id": f"dec-{index}", "strategy_version": "v1", "attribution_version": "attribution_chain_v1",
    }


def _history(path: Path) -> None:
    path.write_text("shadow_trade_id\nfixture\n", encoding="utf-8")


def test_cutoff_is_deterministic_and_prevents_entry_time_leakage(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"
    frozen = get_frozen_oos_cutoff()
    before = _row(1, entry=frozen - timedelta(microseconds=1))
    at_cutoff = _row(2, entry=frozen, pnl=-1.0)
    _db(database, [before, at_cutoff]); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    assert report["oos_integrity"]["closed"] == 1
    assert report["metrics"]["total"]["net_r"] == -1.0
    frozen = freeze_cutoff(database)
    assert frozen["cutoff"] == "2026-08-20T09:12:56.327702+00:00"


def test_integrity_filter_excludes_bad_evidence_and_nonfinite(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"
    start = get_frozen_oos_cutoff()
    bad = _row(2, entry=start + timedelta(hours=1), resolved="UNRESOLVED")
    bad["feature_snapshot_json"] = "not-json"; bad["pnl_r"] = float("nan")
    _db(database, [_row(1, entry=start), bad]); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    assert report["analysis_population"] == {"eligible_closed_resolved_complete_finite": 1, "excluded": 1}
    assert report["oos_integrity"]["unresolved"] == 1
    assert report["oos_integrity"]["invalid_feature_evidence"] == 1
    assert report["oos_integrity"]["nonfinite_pnl_r"] == 1


def test_frozen_benchmark_is_not_derived_or_mutated(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"
    _db(database, [_row(1, entry=get_frozen_oos_cutoff(), pnl=-1.0)]); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    report["frozen_in_sample_benchmark"]["RISK_CONSERVATIVE"]["net_r"] = 999
    assert FROZEN_IS_BENCHMARK["RISK_CONSERVATIVE"]["net_r"] == 9.0


def test_oos_metrics_and_directional_groups(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"; start = get_frozen_oos_cutoff()
    _db(database, [_row(1, entry=start, pnl=2.0), _row(2, entry=start + timedelta(hours=1), side="SHORT", pnl=-1.0)]); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    assert report["metrics"]["total"]["profit_factor"] == 2.0
    assert {row["side"] for row in report["metrics"]["by_strategy_direction"]} == {"LONG", "SHORT"}


def test_hypothesis_waiting_then_supported_and_contradicted(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"; start = get_frozen_oos_cutoff()
    rows = [_row(index, entry=start + timedelta(hours=index), pnl=1.0 if index < 15 else -1.0) for index in range(20)]
    rows.extend(_row(100 + index, entry=start + timedelta(hours=30 + index), strategy="TREND_CONFIRM", pnl=1.0 if index < 5 else -1.0) for index in range(20))
    _db(database, rows); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    statuses = {item["id"]: item["status"] for item in report["hypotheses"]}
    assert statuses["H1"] == "SUPPORTED_EARLY"
    assert statuses["H3"] == "SUPPORTED_EARLY"
    assert statuses["H2"] == "WAITING_FOR_SAMPLE"


def test_hypotheses_can_be_contradicted_or_inconclusive(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"; start = get_frozen_oos_cutoff()
    contradicted = [_row(index, entry=start + timedelta(hours=index), pnl=1.0 if index < 5 else -1.0) for index in range(20)]
    _db(database, contradicted); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    assert {item["id"]: item["status"] for item in report["hypotheses"]}["H1"] == "CONTRADICTED"
    inconclusive = [_row(index, entry=start + timedelta(hours=index), pnl=1.0 if index % 2 else -1.0) for index in range(20)]
    database.unlink(); _db(database, inconclusive)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    assert {item["id"]: item["status"] for item in report["hypotheses"]}["H1"] == "INCONCLUSIVE"


def test_regime_session_and_bootstrap_are_deterministic(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"; start = get_frozen_oos_cutoff()
    rows = [_row(index, entry=start + timedelta(hours=index), pnl=1.0 if index % 2 else -1.0, regime="RANGE", session="OVERLAP") for index in range(20)]
    _db(database, rows); _history(history)
    left = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=100, bootstrap_seed=7)
    right = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=100, bootstrap_seed=7)
    assert left["metrics"]["bootstrap"] == right["metrics"]["bootstrap"]
    assert left["metrics"]["by_market_regime"][0]["market_regime"] == "RANGE"
    assert left["metrics"]["by_session"][0]["session"] == "OVERLAP"


def test_correlated_pairs_and_empty_oos(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"; start = get_frozen_oos_cutoff()
    same = _row(2, entry=start, strategy="TREND_CONFIRM", pnl=-1.0)
    _db(database, [_row(1, entry=start), same]); _history(history)
    report = build_oos_report(database_path=database, history_path=history, cutoff=CUTOFF, bootstrap_iterations=10)
    assert len(report["correlated_observations"]["pairs"]) == 1
    database.unlink()
    _db(database, [_row(3, entry=start - timedelta(microseconds=1))])
    empty = build_oos_report(database_path=database, history_path=history, bootstrap_iterations=10)
    assert empty["oos_verdict"] == "WAITING_FOR_OOS_SAMPLE"


def test_database_and_csv_are_read_only_and_json_output_is_explicit(tmp_path: Path) -> None:
    database, history, output = tmp_path / "research.db", tmp_path / "history.csv", tmp_path / "oos.json"
    _db(database, [_row(1, entry=get_frozen_oos_cutoff())]); _history(history)
    db_before, csv_before = database.read_bytes(), history.read_bytes()
    assert main(["--db", str(database), "--history", str(history), "--bootstrap-iterations", "10"]) == 0
    assert not output.exists()
    assert main(["--db", str(database), "--history", str(history), "--bootstrap-iterations", "10", "--json-output", str(output)]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["cutoff"] == CUTOFF and saved["cutoff_id"] == CRYPTO_OOS_CUTOFF_ID
    assert database.read_bytes() == db_before and history.read_bytes() == csv_before


def test_registry_is_exact_immutable_and_unaffected_by_database_growth(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"
    frozen = get_frozen_oos_cutoff()
    assert frozen.isoformat() == CUTOFF
    assert get_frozen_oos_cutoff() == frozen
    _db(database, [_row(1, entry=frozen)]); _history(history)
    first = build_oos_report(database_path=database, history_path=history, bootstrap_iterations=10)
    with sqlite3.connect(database) as connection:
        extra = _row(2, entry=frozen + timedelta(days=30))
        connection.execute("INSERT INTO shadow_trade_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(extra[key] for key in (
            "shadow_trade_id", "strategy_id", "symbol", "timeframe", "side", "entry_time", "exit_time",
            "status", "pnl_r", "mfe_r", "mae_r", "feature_snapshot_json", "join_status", "data_quality",
            "outcome_id", "feature_snapshot_id", "signal_id", "decision_id", "strategy_version", "attribution_version",
        )))
    second = build_oos_report(database_path=database, history_path=history, bootstrap_iterations=10)
    assert first["cutoff"] == second["cutoff"] == CUTOFF
    assert first["cutoff_id"] == second["cutoff_id"] == CRYPTO_OOS_CUTOFF_ID


def test_conflicting_cutoff_cannot_override_registry(tmp_path: Path) -> None:
    database, history = tmp_path / "research.db", tmp_path / "history.csv"
    _db(database, [_row(1, entry=get_frozen_oos_cutoff())]); _history(history)
    with pytest.raises(ValueError, match="immutable Crypto OOS registry"):
        build_oos_report(
            database_path=database, history_path=history,
            cutoff="2026-08-21T00:00:00+00:00", bootstrap_iterations=10,
        )
    with pytest.raises(SystemExit):
        main([
            "--db", str(database), "--history", str(history),
            "--cutoff", "2026-08-21T00:00:00+00:00",
        ])
